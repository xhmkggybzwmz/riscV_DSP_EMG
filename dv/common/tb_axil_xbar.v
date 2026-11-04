`timescale 1ns / 1ps
`default_nettype none

// tb_axil_xbar - behavioural AXI4-Lite 1-to-N address decoder for Icarus SoC
// benches (the production block design uses the AMD SmartConnect; that path is
// verified in Vivado xsim). One transaction per direction at a time, as issued by
// cmi2axil (SINGLE): the target is decoded from AWADDR/ARADDR when no write/read
// is open, latched at the AW/AR handshake and used for W/B resp. R. An address
// in no window is answered by the model itself with DECERR (it never reaches a
// slave). Slave i window: [BASE[32i+:32], BASE + SIZE).

module tb_axil_xbar #(
    parameter N = 4,
    parameter [32*N-1:0] BASE = 0,
    parameter [32*N-1:0] SIZE = 0
) (
    input  wire          clk,
    input  wire          aresetn,
    // master side
    input  wire [31:0]   s_awaddr, input wire [2:0] s_awprot, input wire s_awvalid, output wire s_awready,
    input  wire [31:0]   s_wdata,  input wire [3:0] s_wstrb,  input wire s_wvalid,  output wire s_wready,
    output wire [1:0]    s_bresp,  output wire s_bvalid, input wire s_bready,
    input  wire [31:0]   s_araddr, input wire [2:0] s_arprot, input wire s_arvalid, output wire s_arready,
    output wire [31:0]   s_rdata,  output wire [1:0] s_rresp, output wire s_rvalid, input wire s_rready,
    // slave side (flattened, slave i at [i])
    output wire [32*N-1:0] m_awaddr, output wire [3*N-1:0] m_awprot, output wire [N-1:0] m_awvalid,
    input  wire [N-1:0]    m_awready,
    output wire [32*N-1:0] m_wdata,  output wire [4*N-1:0] m_wstrb, output wire [N-1:0] m_wvalid,
    input  wire [N-1:0]    m_wready,
    input  wire [2*N-1:0]  m_bresp,  input wire [N-1:0] m_bvalid, output wire [N-1:0] m_bready,
    output wire [32*N-1:0] m_araddr, output wire [3*N-1:0] m_arprot, output wire [N-1:0] m_arvalid,
    input  wire [N-1:0]    m_arready,
    input  wire [32*N-1:0] m_rdata,  input wire [2*N-1:0] m_rresp, input wire [N-1:0] m_rvalid,
    output wire [N-1:0]    m_rready
);
    function [7:0] dec;                        // N = no window
        input [31:0] a;
        integer i;
        begin
            dec = N;
            for (i = 0; i < N; i = i + 1)
                if ({1'b0, a} >= {1'b0, BASE[32*i +: 32]} && {1'b0, a} < {1'b0, BASE[32*i +: 32]} + {1'b0, SIZE[32*i +: 32]})
                    dec = i;
        end
    endfunction

    // ---------------- write ----------------
    reg        w_open, aw_got, w_got, de_b;
    reg  [7:0] wt;                              // latched target
    wire [7:0] wt_now = w_open ? wt : dec(s_awaddr);
    wire [7:0] rt_dec = dec(s_araddr);
    reg        r_open, de_r;
    reg  [7:0] rt;
    genvar g;
    generate for (g = 0; g < N; g = g + 1) begin : g_s
        assign m_awaddr[32*g +: 32] = s_awaddr;
        assign m_awprot[3*g +: 3]   = s_awprot;
        assign m_awvalid[g]         = s_awvalid && !aw_got && (wt_now == g);
        assign m_wdata[32*g +: 32]  = s_wdata;
        assign m_wstrb[4*g +: 4]    = s_wstrb;
        assign m_wvalid[g]          = s_wvalid && !w_got && (wt_now == g);
        assign m_bready[g]          = s_bready && w_open && (wt == g);
        assign m_araddr[32*g +: 32] = s_araddr;
        assign m_arprot[3*g +: 3]   = s_arprot;
        assign m_arvalid[g]         = s_arvalid && !r_open && (rt_dec == g);
        assign m_rready[g]          = s_rready && r_open && (rt == g);
    end endgenerate

    assign s_awready = !aw_got && ((wt_now == N) ? 1'b1 : m_awready[wt_now]);
    assign s_wready  = !w_got  && ((wt_now == N) ? 1'b1 : m_wready[wt_now]);
    assign s_bvalid  = w_open && aw_got && w_got && ((wt == N) ? de_b : m_bvalid[wt]);
    assign s_bresp   = (wt == N) ? 2'b11 : m_bresp[2*wt +: 2];

    always @(posedge clk) begin
        if (!aresetn) begin
            w_open <= 1'b0; aw_got <= 1'b0; w_got <= 1'b0; de_b <= 1'b0; wt <= N;
        end else begin
            if (!w_open && (s_awvalid || s_wvalid)) begin w_open <= 1'b1; wt <= dec(s_awaddr); end
            if (s_awvalid && s_awready) aw_got <= 1'b1;
            if (s_wvalid && s_wready) w_got <= 1'b1;
            if (w_open && aw_got && w_got && wt == N) de_b <= 1'b1;
            if (s_bvalid && s_bready) begin w_open <= 1'b0; aw_got <= 1'b0; w_got <= 1'b0; de_b <= 1'b0; end
        end
    end

    // ---------------- read ----------------
    assign s_arready = !r_open && ((rt_dec == N) ? 1'b1 : m_arready[rt_dec]);
    assign s_rvalid  = r_open && ((rt == N) ? de_r : m_rvalid[rt]);
    assign s_rdata   = (rt == N) ? 32'd0 : m_rdata[32*rt +: 32];
    assign s_rresp   = (rt == N) ? 2'b11 : m_rresp[2*rt +: 2];
    always @(posedge clk) begin
        if (!aresetn) begin
            r_open <= 1'b0; de_r <= 1'b0; rt <= N;
        end else begin
            if (s_arvalid && s_arready) begin r_open <= 1'b1; rt <= rt_dec; de_r <= (rt_dec == N); end
            if (s_rvalid && s_rready) begin r_open <= 1'b0; de_r <= 1'b0; end
        end
    end
endmodule

`default_nettype wire
