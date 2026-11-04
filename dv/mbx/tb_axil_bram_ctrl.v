`timescale 1ns / 1ps
`default_nettype none

// tb_axil_bram_ctrl - behavioural AXI4-Lite -> BRAM port bridge for the Icarus mailbox
// tests (stands in for the AMD AXI BRAM Controller in AXI4-Lite mode; the xsim tests
// use the real IP). One transaction at a time, write before read when both wait:
//   write: AW and W accepted together, BRAM write in the next cycle, then B (OKAY)
//   read : AR accepted, BRAM enable in the next cycle, R (OKAY) with the BRAM data
//          one cycle later
// The window offset is addr[AW-1:0] (the interconnect decodes the base); the BRAM
// port uses byte addresses like cmi_bram_port.

module tb_axil_bram_ctrl #(
    parameter SIZE = 4096
) (
    input  wire        aclk,
    input  wire        aresetn,
    input  wire [31:0] s_awaddr,
    input  wire        s_awvalid,
    output reg         s_awready,
    input  wire [31:0] s_wdata,
    input  wire [3:0]  s_wstrb,
    input  wire        s_wvalid,
    output reg         s_wready,
    output wire [1:0]  s_bresp,
    output reg         s_bvalid,
    input  wire        s_bready,
    input  wire [31:0] s_araddr,
    input  wire        s_arvalid,
    output reg         s_arready,
    output reg  [31:0] s_rdata,
    output wire [1:0]  s_rresp,
    output reg         s_rvalid,
    input  wire        s_rready,
    output reg         bram_en,
    output reg  [3:0]  bram_we,
    output reg  [31:0] bram_addr,
    output reg  [31:0] bram_wrdata,
    input  wire [31:0] bram_rddata
);
    localparam AW = $clog2(SIZE);
    localparam S_IDLE = 3'd0, S_WR = 3'd1, S_B = 3'd2, S_RD = 3'd3, S_RDATA = 3'd4, S_R = 3'd5;
    reg [2:0] st = S_IDLE;
    initial begin
        s_awready = 1'b0; s_wready = 1'b0; s_bvalid = 1'b0; s_arready = 1'b0; s_rvalid = 1'b0; s_rdata = 32'h0;
        bram_en = 1'b0; bram_we = 4'h0; bram_addr = 32'h0; bram_wrdata = 32'h0;
    end
    assign s_bresp = 2'b00;
    assign s_rresp = 2'b00;

    always @(posedge aclk) begin
        if (!aresetn) begin
            st <= S_IDLE;
            s_awready <= 1'b0; s_wready <= 1'b0; s_bvalid <= 1'b0;
            s_arready <= 1'b0; s_rvalid <= 1'b0; s_rdata <= 32'h0;
            bram_en <= 1'b0; bram_we <= 4'h0; bram_addr <= 32'h0; bram_wrdata <= 32'h0;
        end else begin
            s_awready <= 1'b0; s_wready <= 1'b0; s_arready <= 1'b0;
            bram_en <= 1'b0; bram_we <= 4'h0;
            case (st)
                S_IDLE:
                    if (s_awvalid && s_wvalid && !s_awready) begin
                        s_awready <= 1'b1; s_wready <= 1'b1;
                        bram_addr <= {{(32 - AW){1'b0}}, s_awaddr[AW-1:2], 2'b00};
                        bram_wrdata <= s_wdata;
                        bram_we <= s_wstrb;
                        st <= S_WR;
                    end else if (s_arvalid && !s_arready) begin
                        s_arready <= 1'b1;
                        bram_addr <= {{(32 - AW){1'b0}}, s_araddr[AW-1:2], 2'b00};
                        st <= S_RD;
                    end
                S_WR: begin                         // bram_we was set with the handshake
                    bram_en <= 1'b1; bram_we <= bram_we;
                    st <= S_B;
                end
                S_B: begin
                    s_bvalid <= 1'b1;
                    if (s_bvalid && s_bready) begin s_bvalid <= 1'b0; st <= S_IDLE; end
                end
                S_RD: begin bram_en <= 1'b1; st <= S_RDATA; end
                S_RDATA: st <= S_R;
                S_R: begin
                    if (!s_rvalid) begin s_rvalid <= 1'b1; s_rdata <= bram_rddata; end
                    else if (s_rready) begin s_rvalid <= 1'b0; st <= S_IDLE; end
                end
                default: st <= S_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire
