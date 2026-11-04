`timescale 1ns / 1ps
`default_nettype none

// tb_cmi_slave - generic behavioural CMI slave for unit tests (simulation only).
//
// - ready = !busy && rnd (rnd is 1 with probability ready_pct/100)
// - latency drawn from [lat_min, lat_max] (1 = response in the cycle after the
//   handshake); response for one cycle
// - read data = {TAG, addr[23:0]} so a test can tell which slave answered
// - rsp_err = 1 when the address lies in [err_lo, err_hi] (if err_en)
// - counts accepted requests and keeps the fields of the last one
// - rst drops a pending response
// - spurious_rsp(): emit one response although nothing is outstanding
//   (protocol violation on purpose, to test that a router ignores it)

module tb_cmi_slave #(
    parameter [7:0] TAG = 8'hA0
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        req_valid,
    output wire        req_ready,
    input  wire [31:0] req_addr,
    input  wire        req_we,
    input  wire [3:0]  req_wstrb,
    input  wire [31:0] req_wdata,
    output reg         rsp_valid,
    output reg  [31:0] rsp_rdata,
    output reg         rsp_err
);
    integer    ready_pct = 100;
    integer    lat_min   = 1;
    integer    lat_max   = 1;
    integer    seed      = 1;
    reg        err_en    = 1'b0;
    reg [31:0] err_lo    = 32'd0, err_hi = 32'd0;

    integer    n_req, n_write;
    reg [31:0] last_addr, last_wdata;
    reg        last_we;
    reg [3:0]  last_wstrb;

    reg        busy, rnd, spurious;
    integer    cnt, lat;
    reg [31:0] p_addr;

    task configure;
        input integer t_ready_pct; input integer t_lat_min; input integer t_lat_max; input integer t_seed;
        begin ready_pct = t_ready_pct; lat_min = t_lat_min; lat_max = t_lat_max; seed = t_seed; end
    endtask

    task set_error_window;
        input [31:0] lo; input [31:0] hi;
        begin err_en = 1'b1; err_lo = lo; err_hi = hi; end
    endtask

    task spurious_rsp;
        begin spurious = 1'b1; end
    endtask

    function integer urand;
        input integer m;
        integer r;
        begin r = $random(seed); if (r < 0) r = -r; urand = r % m; end
    endfunction

    assign req_ready = !busy && rnd;

    task respond;
        input [31:0] a;
        begin
            rsp_valid <= 1'b1;
            rsp_rdata <= {TAG, a[23:0]};
            rsp_err   <= err_en && a >= err_lo && a <= err_hi;
        end
    endtask

    initial begin n_req = 0; n_write = 0; spurious = 1'b0; end

    always @(posedge clk) begin
        if (rst) begin
            busy <= 1'b0; rsp_valid <= 1'b0; rsp_err <= 1'b0; rnd <= 1'b0; cnt <= 0;
            spurious <= 1'b0;
        end else begin
            rnd       <= urand(100) < ready_pct;
            rsp_valid <= 1'b0;
            rsp_err   <= 1'b0;
            if (spurious) begin
                spurious <= 1'b0;
                respond(32'hBAD0_0000);
            end
            if (busy) begin
                if (cnt <= 1) begin respond(p_addr); busy <= 1'b0; end
                else cnt <= cnt - 1;
            end else if (req_valid && req_ready) begin
                n_req = n_req + 1;
                if (req_we) n_write = n_write + 1;
                last_addr = req_addr; last_we = req_we; last_wstrb = req_wstrb; last_wdata = req_wdata;
                lat = lat_min + urand(lat_max - lat_min + 1);
                if (lat == 1) respond(req_addr);
                else begin busy <= 1'b1; p_addr <= req_addr; cnt <= lat - 1; end
            end
        end
    end
endmodule

`default_nettype wire
