`timescale 1ns / 1ps
`default_nettype none

// e3c_axil_if - AXI4-Lite slave front end shared by the E3c control pages
// (DSP, CNN, replay; docs/soc/e3c_integration.md section 4). Same behaviour as
// the verified soc_sysctrl:
//   - AW and W are accepted independently (any order, or together) and held;
//     when both are held the write is performed exactly once: wr_en is a
//     one-cycle pulse with wr_word / wr_data / wr_strb, then BVALID (OKAY)
//     until BREADY. No new AW/W is accepted until the B handshake.
//   - AR is accepted when no read response is pending; RDATA is registered at
//     the AR handshake from rd_data, which the page computes combinationally
//     from rd_word = s_axi_araddr[11:2] (rd_en marks that cycle, for read side
//     effects - none of the E3c pages has any). RVALID until RREADY.
//   - Responses are always OKAY (unimplemented offsets read 0, writes ignored).
//   - aresetn: synchronous, active low; no VALID during reset.

module e3c_axil_if (
    input  wire        aclk,
    input  wire        aresetn,

    input  wire [11:0] s_axi_awaddr,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,

    output reg         wr_en,
    output reg  [9:0]  wr_word,
    output reg  [31:0] wr_data,
    output reg  [3:0]  wr_strb,
    output wire        rd_en,
    output wire [9:0]  rd_word,
    input  wire [31:0] rd_data
);

    reg       aw_held, w_held;
    reg [9:0] aw_word;

    assign s_axi_awready = ~aw_held & ~s_axi_bvalid & aresetn;
    assign s_axi_wready  = ~w_held  & ~s_axi_bvalid & aresetn;
    assign s_axi_arready = ~s_axi_rvalid & aresetn;
    assign s_axi_bresp   = 2'b00;
    assign s_axi_rresp   = 2'b00;
    assign rd_word       = s_axi_araddr[11:2];
    assign rd_en         = s_axi_arvalid & s_axi_arready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            aw_held      <= 1'b0;
            w_held       <= 1'b0;
            s_axi_bvalid <= 1'b0;
            wr_en        <= 1'b0;
            wr_word      <= 10'd0;
            wr_data      <= 32'd0;
            wr_strb      <= 4'd0;
        end else begin
            wr_en <= 1'b0;
            if (s_axi_awvalid && s_axi_awready) begin
                aw_held <= 1'b1;
                aw_word <= s_axi_awaddr[11:2];
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_held  <= 1'b1;
                wr_data <= s_axi_wdata;
                wr_strb <= s_axi_wstrb;
            end
            if (aw_held && w_held) begin                // perform the write exactly once
                wr_en        <= 1'b1;
                wr_word      <= aw_word;
                aw_held      <= 1'b0;
                w_held       <= 1'b0;
                s_axi_bvalid <= 1'b1;
            end
            if (s_axi_bvalid && s_axi_bready)
                s_axi_bvalid <= 1'b0;
        end
    end

    always @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_rvalid <= 1'b0;
            s_axi_rdata  <= 32'd0;
        end else if (rd_en) begin
            s_axi_rvalid <= 1'b1;
            s_axi_rdata  <= rd_data;
        end else if (s_axi_rvalid && s_axi_rready) begin
            s_axi_rvalid <= 1'b0;
        end
    end

endmodule

`default_nettype wire
