`timescale 1ns / 1ps
`default_nettype none

// tb_mem_model - behavioural CMI slave with an instruction port (I) and a data
// port (D) sharing one RAM. Simulation only.
//
// Address map (matches docs/soc/memory_map.md):
//   [0, MEM_BYTES)   RAM, both ports
//   TOHOST_ADDR      D port only. Write: records the value (tohost_valid/value).
//                    Read: returns 0.
//   anything else    error response (unmapped)
// Optional error-injection windows (per port, inclusive) also return an error;
// an errored write does not modify memory.
//
// CMI behaviour per port:
//   - ready = !busy && rnd, where rnd is a registered random bit that is 1 with
//     probability ready_pct/100 (100 = always ready). Only one transaction per
//     port is accepted at a time.
//   - Latency L is drawn uniformly from [lat_min, lat_max] (L >= 1) at accept.
//     L = 1: response is visible in the cycle right after the handshake
//     (like a synchronous BRAM). Larger L adds L-1 wait cycles.
//   - The access (read data, write, error decision) is performed on the edge
//     that raises rsp_valid. rsp_valid is high for exactly one cycle.
//   - rst drops any accepted-but-unanswered transaction; no stale response
//     is ever produced afterwards. RAM contents survive rst.
//
// Configuration: call the tasks below from the testbench before releasing rst.

module tb_mem_model #(
    parameter MEM_BYTES   = 65536,
    parameter TOHOST_ADDR = 32'h1000_0008
) (
    input  wire        clk,
    input  wire        rst,

    input  wire        i_req_valid,
    output wire        i_req_ready,
    input  wire [31:0] i_req_addr,
    output reg         i_rsp_valid,
    output reg  [31:0] i_rsp_rdata,
    output reg         i_rsp_err,

    input  wire        d_req_valid,
    output wire        d_req_ready,
    input  wire [31:0] d_req_addr,
    input  wire        d_req_we,
    input  wire [3:0]  d_req_wstrb,
    input  wire [31:0] d_req_wdata,
    output reg         d_rsp_valid,
    output reg  [31:0] d_rsp_rdata,
    output reg         d_rsp_err
);

    localparam WORDS = MEM_BYTES / 4;

    reg [31:0] mem [0:WORDS-1];

    // ---------------- configuration ----------------
    integer    ready_pct = 100;
    integer    lat_min   = 1;
    integer    lat_max   = 1;
    integer    i_seed    = 1;     // separate streams: the two ports never share
    integer    d_seed    = 2;     // a $random call order
    reg        i_err_en  = 1'b0;
    reg [31:0] i_err_lo  = 32'd0, i_err_hi = 32'd0;
    reg        d_err_en  = 1'b0;
    reg [31:0] d_err_lo  = 32'd0, d_err_hi = 32'd0;
    reg        probe_en  = 1'b0;
    reg [31:0] probe_addr = 32'd0;

    // ---------------- observable state (cleared by rst) ----------------
    reg        tohost_valid;
    reg [31:0] tohost_value;
    integer    d_writes_ok;      // successful (committed) writes
    integer    d_writes_err;
    integer    probe_reads;
    integer    probe_writes;

    integer k;

    task configure;
        input integer t_ready_pct;
        input integer t_lat_min;
        input integer t_lat_max;
        input integer t_seed;
        begin
            ready_pct = t_ready_pct;
            lat_min   = (t_lat_min < 1) ? 1 : t_lat_min;
            lat_max   = (t_lat_max < lat_min) ? lat_min : t_lat_max;
            i_seed    = t_seed;
            d_seed    = t_seed ^ 32'h5A5A_1234;
        end
    endtask

    task set_i_error_window;
        input [31:0] lo; input [31:0] hi;
        begin i_err_en = 1'b1; i_err_lo = lo; i_err_hi = hi; end
    endtask

    task set_d_error_window;
        input [31:0] lo; input [31:0] hi;
        begin d_err_en = 1'b1; d_err_lo = lo; d_err_hi = hi; end
    endtask

    task set_probe;
        input [31:0] a;
        begin probe_en = 1'b1; probe_addr = {a[31:2], 2'b00}; end
    endtask

    task clear_ram;                      // deterministic background (all zero)
        begin
            for (k = 0; k < WORDS; k = k + 1) mem[k] = 32'd0;
        end
    endtask

    task load_hex;                       // one 32-bit word per line, from address 0
        input [8*512-1:0] file;
        begin
            $readmemh(file, mem);
        end
    endtask

    task dump_hex;                       // whole RAM, one 32-bit word per line
        input [8*512-1:0] file;
        integer fd, w;
        begin
            fd = $fopen(file, "w");
            for (w = 0; w < WORDS; w = w + 1)
                $fdisplay(fd, "%h", mem[w]);
            $fclose(fd);
        end
    endtask

    function [31:0] i_rand_lat;
        input dummy;
        integer r;
        begin
            r = $random(i_seed);
            if (r < 0) r = -r;
            i_rand_lat = lat_min + (r % (lat_max - lat_min + 1));
        end
    endfunction

    function [31:0] d_rand_lat;
        input dummy;
        integer r;
        begin
            r = $random(d_seed);
            if (r < 0) r = -r;
            d_rand_lat = lat_min + (r % (lat_max - lat_min + 1));
        end
    endfunction

    function i_rand_ready;
        input dummy;
        integer r;
        begin
            r = $random(i_seed);
            if (r < 0) r = -r;
            i_rand_ready = (r % 100) < ready_pct;
        end
    endfunction

    function d_rand_ready;
        input dummy;
        integer r;
        begin
            r = $random(d_seed);
            if (r < 0) r = -r;
            d_rand_ready = (r % 100) < ready_pct;
        end
    endfunction

    function in_ram;
        input [31:0] a;
        begin in_ram = (a < MEM_BYTES); end
    endfunction

    // ---------------- per-port state ----------------
    reg        i_busy, d_busy;
    reg        i_rnd, d_rnd;
    integer    i_cnt, d_cnt;
    integer    i_lat, d_lat;      // blocking temporaries
    reg [31:0] i_addr;
    reg [31:0] d_addr, d_wdata;
    reg        d_we;
    reg [3:0]  d_wstrb;

    assign i_req_ready = !i_busy && i_rnd;
    assign d_req_ready = !d_busy && d_rnd;

    // Perform an I-port access and drive the response registers.
    task i_respond;
        input [31:0] a;
        begin
            i_rsp_valid <= 1'b1;
            if (!in_ram(a) || (i_err_en && a >= i_err_lo && a <= i_err_hi)) begin
                i_rsp_err   <= 1'b1;
                i_rsp_rdata <= 32'hDEAD_BEEF;
            end else begin
                i_rsp_err   <= 1'b0;
                i_rsp_rdata <= mem[a[31:2]];
            end
        end
    endtask

    // Perform a D-port access and drive the response registers.
    task d_respond;
        input [31:0] a; input we; input [3:0] strb; input [31:0] wd;
        reg [31:0] w;
        integer b;
        begin
            d_rsp_valid <= 1'b1;
            if ({a[31:2], 2'b00} == TOHOST_ADDR) begin
                d_rsp_err   <= 1'b0;
                d_rsp_rdata <= 32'd0;
                if (we) begin
                    tohost_valid <= 1'b1;
                    tohost_value <= wd;
                    d_writes_ok  <= d_writes_ok + 1;
                end
            end else if (!in_ram(a) || (d_err_en && a >= d_err_lo && a <= d_err_hi)) begin
                d_rsp_err   <= 1'b1;
                d_rsp_rdata <= 32'hDEAD_BEEF;
                if (we) d_writes_err <= d_writes_err + 1;
            end else begin
                d_rsp_err <= 1'b0;
                if (we) begin
                    w = mem[a[31:2]];
                    for (b = 0; b < 4; b = b + 1)
                        if (strb[b]) w[8*b +: 8] = wd[8*b +: 8];
                    mem[a[31:2]] = w;
                    d_rsp_rdata <= 32'hDEAD_BEEF;
                    d_writes_ok <= d_writes_ok + 1;
                end else begin
                    d_rsp_rdata <= mem[a[31:2]];
                end
                if (probe_en && {a[31:2], 2'b00} == probe_addr) begin
                    if (we) probe_writes <= probe_writes + 1;
                    else    probe_reads  <= probe_reads + 1;
                end
            end
        end
    endtask

    // ---------------- I port ----------------
    always @(posedge clk) begin
        if (rst) begin
            i_busy      <= 1'b0;
            i_rsp_valid <= 1'b0;
            i_rsp_err   <= 1'b0;
            i_rnd       <= 1'b0;
            i_cnt       <= 0;
        end else begin
            i_rnd       <= i_rand_ready(1'b0);
            i_rsp_valid <= 1'b0;
            i_rsp_err   <= 1'b0;
            if (i_busy) begin
                if (i_cnt <= 1) begin
                    i_respond(i_addr);
                    i_busy <= 1'b0;
                end else begin
                    i_cnt <= i_cnt - 1;
                end
            end else if (i_req_valid && i_req_ready) begin
                i_lat = i_rand_lat(1'b0);
                if (i_lat == 1) begin
                    i_respond(i_req_addr);
                end else begin
                    i_busy <= 1'b1;
                    i_addr <= i_req_addr;
                    i_cnt  <= i_lat - 1;
                end
            end
        end
    end

    // ---------------- D port ----------------
    always @(posedge clk) begin
        if (rst) begin
            d_busy       <= 1'b0;
            d_rsp_valid  <= 1'b0;
            d_rsp_err    <= 1'b0;
            d_rnd        <= 1'b0;
            d_cnt        <= 0;
            tohost_valid <= 1'b0;
            tohost_value <= 32'd0;
            d_writes_ok  <= 0;
            d_writes_err <= 0;
            probe_reads  <= 0;
            probe_writes <= 0;
        end else begin
            d_rnd       <= d_rand_ready(1'b0);
            d_rsp_valid <= 1'b0;
            d_rsp_err   <= 1'b0;
            if (d_busy) begin
                if (d_cnt <= 1) begin
                    d_respond(d_addr, d_we, d_wstrb, d_wdata);
                    d_busy <= 1'b0;
                end else begin
                    d_cnt <= d_cnt - 1;
                end
            end else if (d_req_valid && d_req_ready) begin
                d_lat = d_rand_lat(1'b0);
                if (d_lat == 1) begin
                    d_respond(d_req_addr, d_req_we, d_req_wstrb, d_req_wdata);
                end else begin
                    d_busy  <= 1'b1;
                    d_addr  <= d_req_addr;
                    d_we    <= d_req_we;
                    d_wstrb <= d_req_wstrb;
                    d_wdata <= d_req_wdata;
                    d_cnt   <= d_lat - 1;
                end
            end
        end
    end

endmodule

`default_nettype wire
