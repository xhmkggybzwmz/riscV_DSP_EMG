`timescale 1ns / 1ps
`default_nettype none

// tb_axi_ddr_model - AXI4 read-only memory standing in for the PS DDR behind S_AXI_HP0_FPD in the
// xsim system tests (real AMD AXI DMA). Checks what the PS port requires: INCR bursts, full-width
// beats, no 4 KB crossing, aligned start (the DMA has no data realignment engine).
// Plusargs: +ddr=<hex file, one 64-bit word per line> +ddr_base=<hex> byte address of word 0
//           +ddr_lat=N  read latency in cycles   +ddr_stall_pct=P random gaps between beats

module tb_axi_ddr_model #(
    parameter DATA_W = 128,
    parameter ADDR_W = 40,
    parameter WORDS  = 32768
) (
    input  wire              aclk,
    input  wire              aresetn,
    input  wire [ADDR_W-1:0] araddr,
    input  wire [7:0]        arlen,
    input  wire [2:0]        arsize,
    input  wire [1:0]        arburst,
    input  wire              arvalid,
    output reg               arready,
    output reg  [DATA_W-1:0] rdata,
    output reg  [1:0]        rresp,
    output reg               rlast,
    output reg               rvalid,
    input  wire              rready,
    output integer           errors,
    output integer           n_bursts,
    output integer           n_beats
);
    localparam BYTES = DATA_W / 8;
    reg [63:0] mem [0:WORDS-1];
    reg [8*512-1:0] file;
    reg [ADDR_W-1:0] ddr_base = 0;
    integer lat = 0, stall_pct = 0, seed = 23;
    initial begin
        errors = 0; n_bursts = 0; n_beats = 0;
        arready = 1'b0; rvalid = 1'b0; rlast = 1'b0; rresp = 2'b00; rdata = 0;
        if ($value$plusargs("ddr=%s", file)) $readmemh(file, mem);
        if (!$value$plusargs("ddr_base=%h", ddr_base)) ddr_base = 0;
        if (!$value$plusargs("ddr_lat=%d", lat)) lat = 0;
        if (!$value$plusargs("ddr_stall_pct=%d", stall_pct)) stall_pct = 0;
    end
    function [DATA_W-1:0] word_at(input [ADDR_W-1:0] a);
        integer i;
        reg [ADDR_W-1:0] w;
        begin
            word_at = 0;
            for (i = 0; i < DATA_W / 64; i = i + 1) begin
                w = ((a - ddr_base) >> 3) + i;
                word_at[64 * i +: 64] = (w < WORDS) ? mem[w] : 64'hDEAD_DEAD_DEAD_DEAD;
            end
        end
    endfunction

    reg              busy = 1'b0;
    reg [ADDR_W-1:0] addr = 0;
    reg [8:0]        left = 0;
    integer          wait_c = 0;
    task err(input [8*48-1:0] what);
        begin
            errors = errors + 1;
            if (errors <= 10) $display("ERROR: DDR model: %0s (addr %h len %0d t=%0t)", what, araddr, arlen, $time);
        end
    endtask
    always @(posedge aclk) begin
        if (!aresetn) begin
            arready <= 1'b0; rvalid <= 1'b0; rlast <= 1'b0; rresp <= 2'b00; busy <= 1'b0;
        end else begin
            arready <= ~busy & ~arready;
            if (arvalid && arready) begin
                n_bursts = n_bursts + 1;
                if (arburst !== 2'b01) err("burst type is not INCR");
                if ((1 << arsize) !== BYTES) err("narrow burst");
                if ((araddr & (BYTES - 1)) !== 0) err("unaligned start address");
                if (((araddr & 4095) + (arlen + 1) * BYTES) > 4096) err("burst crosses a 4 KB boundary");
                if (araddr < ddr_base || ((araddr - ddr_base) >> 3) >= WORDS) err("read outside the data buffer");
                busy <= 1'b1; arready <= 1'b0;
                addr <= araddr; left <= arlen + 1; wait_c <= lat;
            end
            if (rvalid && rready) begin
                rvalid <= 1'b0; rlast <= 1'b0;
            end
            if (busy && left != 0 && (!rvalid || rready)) begin
                if (wait_c > 0) wait_c <= wait_c - 1;
                else if (stall_pct == 0 || ({$random(seed)} % 100) >= stall_pct) begin
                    rvalid <= 1'b1;
                    rdata  <= word_at(addr);
                    rresp  <= 2'b00;
                    rlast  <= (left == 1);
                    addr   <= addr + BYTES;
                    left   <= left - 1;
                    n_beats = n_beats + 1;
                    if (left == 1) busy <= 1'b0;
                end
            end
        end
    end
endmodule

`default_nettype wire
