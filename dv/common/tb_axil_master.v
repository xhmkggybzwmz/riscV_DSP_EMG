`timescale 1ns / 1ps
`default_nettype none

// tb_axil_master - AXI4-Lite master BFM for slave tests (E3c control pages).
//
// Tasks (call hierarchically, e.g. u_m.write(a, d, 4'hF)):
//   write(addr, data, strb)   one write; AW/W order and B back-pressure follow `order`
//                             and `bdelay_max` (see configure)
//   read(addr, data)          one read; R back-pressure follows `rdelay_max`
//   configure(order, bmax, rmax, seed)
//       order: 0 AW and W together, 1 AW first (W 1..4 cycles later), 2 W first,
//              3 random choice of 0..2 per transaction
// Every response is checked to be OKAY (bresp_err / rresp_err count others).
// The BFM drives VALID only from negedge to keep the timing simple and race free;
// VALID is held until the handshake (AXI rule).

module tb_axil_master #(
    parameter AW = 12
) (
    input  wire          clk,
    output reg  [AW-1:0] awaddr,
    output reg           awvalid,
    input  wire          awready,
    output reg  [31:0]   wdata,
    output reg  [3:0]    wstrb,
    output reg           wvalid,
    input  wire          wready,
    input  wire [1:0]    bresp,
    input  wire          bvalid,
    output reg           bready,
    output reg  [AW-1:0] araddr,
    output reg           arvalid,
    input  wire          arready,
    input  wire [31:0]   rdata,
    input  wire [1:0]    rresp,
    input  wire          rvalid,
    output reg           rready
);
    integer order_cfg = 0, bmax = 0, rmax = 0, seed = 1;
    integer bresp_err = 0, rresp_err = 0, n_wr = 0, n_rd = 0;

    initial begin
        awaddr = 0; awvalid = 0; wdata = 0; wstrb = 0; wvalid = 0; bready = 0;
        araddr = 0; arvalid = 0; rready = 0;
    end

    task configure(input integer o, input integer bm, input integer rm, input integer s);
        begin order_cfg = o; bmax = bm; rmax = rm; seed = s; end
    endtask

    function integer rnd(input integer maxv);
        integer v;
        begin
            v = $random(seed);
            if (v < 0) v = -v;
            rnd = (maxv > 0) ? (v % (maxv + 1)) : 0;
        end
    endfunction

    reg aw_done, w_done;
    integer o, d;
    task write(input [AW-1:0] a, input [31:0] v, input [3:0] s);
        begin
            o = (order_cfg == 3) ? rnd(2) : order_cfg;
            aw_done = 0; w_done = 0;
            @(negedge clk);
            if (o == 0 || o == 1) begin awaddr = a; awvalid = 1; end
            if (o == 0 || o == 2) begin wdata = v; wstrb = s; wvalid = 1; end
            d = 1 + rnd(3);
            while (!(aw_done && w_done)) begin
                @(posedge clk);
                if (awvalid && awready) aw_done = 1;
                if (wvalid && wready) w_done = 1;
                @(negedge clk);
                if (aw_done) awvalid = 0;
                if (w_done) wvalid = 0;
                if (d > 0) d = d - 1;
                if (d == 0 && o == 1 && !w_done && !wvalid) begin wdata = v; wstrb = s; wvalid = 1; end
                if (d == 0 && o == 2 && !aw_done && !awvalid) begin awaddr = a; awvalid = 1; end
            end
            d = rnd(bmax);
            while (d > 0) begin @(negedge clk); d = d - 1; end
            bready = 1;
            @(posedge clk);
            while (!bvalid) @(posedge clk);
            if (bresp !== 2'b00) bresp_err = bresp_err + 1;
            @(negedge clk) bready = 0;
            n_wr = n_wr + 1;
        end
    endtask

    task read(input [AW-1:0] a, output [31:0] v);
        begin
            @(negedge clk); araddr = a; arvalid = 1;
            @(posedge clk);
            while (!arready) @(posedge clk);
            @(negedge clk) arvalid = 0;
            d = rnd(rmax);
            while (d > 0) begin @(negedge clk); d = d - 1; end
            rready = 1;
            @(posedge clk);
            while (!rvalid) @(posedge clk);
            v = rdata;
            if (rresp !== 2'b00) rresp_err = rresp_err + 1;
            @(negedge clk) rready = 0;
            n_rd = n_rd + 1;
        end
    endtask
endmodule

`default_nettype wire
