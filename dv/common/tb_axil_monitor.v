`timescale 1ns / 1ps
`default_nettype none

// tb_axil_monitor - passive AXI4-Lite protocol checker (one interface).
//
//   A1  no VALID (AW, W, AR from the master; B, R from the slave) while aresetn
//       is low, nor in the first cycle after aresetn goes high
//   A2  a VALID that has not been accepted stays high, with stable payload
//       (AW: addr/prot, W: data/strb, AR: addr/prot, B: resp, R: data/resp)
//   A3  BVALID only after both the AW and the W handshake of that write
//   A4  RVALID only after an AR handshake
//   A5  SINGLE = 1: at most one transaction in flight (as issued by cmi2axil):
//       no new AW/W/AR while a write or read is incomplete
//   A6  VALID/READY are never X/Z outside reset
// Counters (cleared in reset): handshakes per channel, error responses.

module tb_axil_monitor #(
    parameter NAME   = "axil",
    parameter SINGLE = 1
) (
    input wire        aclk,
    input wire        aresetn,
    input wire [31:0] awaddr, input wire [2:0] awprot, input wire awvalid, input wire awready,
    input wire [31:0] wdata,  input wire [3:0] wstrb,  input wire wvalid,  input wire wready,
    input wire [1:0]  bresp,  input wire bvalid, input wire bready,
    input wire [31:0] araddr, input wire [2:0] arprot, input wire arvalid, input wire arready,
    input wire [31:0] rdata,  input wire [1:0] rresp,  input wire rvalid,  input wire rready,
    output reg [31:0] errors,
    output reg [31:0] n_aw, output reg [31:0] n_w, output reg [31:0] n_b,
    output reg [31:0] n_ar, output reg [31:0] n_r, output reg [31:0] n_berr, output reg [31:0] n_rerr
);
    reg prev_rstn = 1'b0;
    reg p_aw, p_w, p_ar, p_b, p_r;              // pending (valid & !ready) last cycle
    reg [34:0] s_aw, s_ar;
    reg [35:0] s_w;
    reg [1:0]  s_b;
    reg [33:0] s_r;
    reg aw_done, w_done, rd_open;

    initial errors = 0;

    task violation;
        input [8*72-1:0] what;
        begin
            errors = errors + 1;
            $display("ERROR: [%0s AXI monitor] %0s (t=%0t)", NAME, what, $time);
        end
    endtask

    always @(posedge aclk) begin
        prev_rstn <= aresetn;
        if (!aresetn || !prev_rstn) begin
            if (awvalid || wvalid || arvalid || bvalid || rvalid)
                violation(!aresetn ? "A1 VALID asserted during reset"
                                   : "A1 VALID asserted in the first cycle after reset");
        end
        if (!aresetn) begin
            p_aw <= 0; p_w <= 0; p_ar <= 0; p_b <= 0; p_r <= 0;
            aw_done <= 0; w_done <= 0; rd_open <= 0;
            n_aw <= 0; n_w <= 0; n_b <= 0; n_ar <= 0; n_r <= 0; n_berr <= 0; n_rerr <= 0;
        end else begin
            // A6
            if ((^{awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready}) === 1'bx)
                violation("A6 X/Z on a VALID or READY");
            // A2
            if (p_aw && (!awvalid || {awaddr, awprot} !== s_aw)) violation("A2 AW withdrawn or changed before AWREADY");
            if (p_w  && (!wvalid  || {wdata, wstrb}   !== s_w))  violation("A2 W withdrawn or changed before WREADY");
            if (p_ar && (!arvalid || {araddr, arprot} !== s_ar)) violation("A2 AR withdrawn or changed before ARREADY");
            if (p_b  && (!bvalid  || bresp !== s_b))             violation("A2 B withdrawn or changed before BREADY");
            if (p_r  && (!rvalid  || {rdata, rresp} !== s_r))    violation("A2 R withdrawn or changed before RREADY");
            p_aw <= awvalid & ~awready; s_aw <= {awaddr, awprot};
            p_w  <= wvalid  & ~wready;  s_w  <= {wdata, wstrb};
            p_ar <= arvalid & ~arready; s_ar <= {araddr, arprot};
            p_b  <= bvalid  & ~bready;  s_b  <= bresp;
            p_r  <= rvalid  & ~rready;  s_r  <= {rdata, rresp};
            // A3 / A4
            if (bvalid && !(aw_done && w_done)) violation("A3 BVALID before both AW and W handshakes");
            if (rvalid && !rd_open)             violation("A4 RVALID without an accepted AR");
            // A5
            if (SINGLE) begin
                if (awvalid && awready && (aw_done || rd_open)) violation("A5 second transaction (AW) in flight");
                if (wvalid && wready && (w_done || rd_open))    violation("A5 second transaction (W) in flight");
                if (arvalid && arready && (rd_open || aw_done || w_done)) violation("A5 second transaction (AR) in flight");
            end
            // bookkeeping
            if (awvalid && awready) begin aw_done <= 1'b1; n_aw <= n_aw + 1; end
            if (wvalid && wready)   begin w_done  <= 1'b1; n_w  <= n_w + 1;  end
            if (bvalid && bready) begin
                aw_done <= 1'b0; w_done <= 1'b0; n_b <= n_b + 1;
                if (bresp != 2'b00) n_berr <= n_berr + 1;
            end
            if (arvalid && arready) begin rd_open <= 1'b1; n_ar <= n_ar + 1; end
            if (rvalid && rready) begin
                rd_open <= 1'b0; n_r <= n_r + 1;
                if (rresp != 2'b00) n_rerr <= n_rerr + 1;
            end
        end
    end
endmodule

`default_nettype wire
