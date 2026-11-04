`timescale 1ns / 1ps
`default_nettype none

// cmi2axil - CMI slave -> AXI4-Lite master adapter
// (docs/soc/soc_bus_architecture.md section 4).
//
// One transaction at a time:
//   read : CMI request -> AR -> R  -> CMI response
//   write: CMI request -> AW + W (independent handshakes) -> B -> CMI response
//
// FSM: IDLE -> WR_ADDR_DATA -> WR_RESP -> IDLE
//      IDLE -> RD_ADDR      -> RD_DATA -> IDLE
//
//   IDLE         req_ready = 1; on the CMI handshake the request is latched and
//                AWVALID+WVALID (write) or ARVALID (read) are raised together.
//   WR_ADDR_DATA AWVALID and WVALID are held until their own handshakes and
//                dropped independently (AW first, W first or both in one cycle);
//                the master never waits for one READY before raising the other
//                VALID. When both are done -> WR_RESP with BREADY = 1.
//   WR_RESP      wait for BVALID; on the B handshake the CMI response is
//                registered (visible in the next cycle) and the FSM returns to
//                IDLE.
//   RD_ADDR      ARVALID held until ARREADY; then RD_DATA with RREADY = 1.
//   RD_DATA      wait for RVALID; on the R handshake RDATA/RRESP are registered
//                into the CMI response (next cycle) and the FSM returns to IDLE.
//
// All AXI outputs come from registers (no combinational path from an AXI input
// to an AXI output); the three VALIDs are additionally masked by rst. Addresses are issued word aligned ({addr[31:2], 2'b00});
// byte selection uses WSTRB only. AxPROT = 3'b000. BRESP/RRESP other than OKAY
// (SLVERR, DECERR, and EXOKAY which AXI4-Lite does not allow) give rsp_err = 1.
// No timeout: a slave that never answers stalls the adapter (by design, see
// the spec section 4.7). rst returns to IDLE with every VALID/READY low; a
// transaction in flight is abandoned (the AXI side is reset in the same domain).

module cmi2axil (
    input  wire        clk,
    input  wire        rst,

    // CMI slave side
    input  wire        req_valid,
    output wire        req_ready,
    input  wire [31:0] req_addr,
    input  wire        req_we,
    input  wire [3:0]  req_wstrb,
    input  wire [31:0] req_wdata,
    output reg         rsp_valid,
    output reg  [31:0] rsp_rdata,
    output reg         rsp_err,

    // AXI4-Lite master side
    output reg  [31:0] m_axi_awaddr,
    output wire [2:0]  m_axi_awprot,
    output wire        m_axi_awvalid,
    input  wire        m_axi_awready,
    output reg  [31:0] m_axi_wdata,
    output reg  [3:0]  m_axi_wstrb,
    output wire        m_axi_wvalid,
    input  wire        m_axi_wready,
    input  wire [1:0]  m_axi_bresp,
    input  wire        m_axi_bvalid,
    output reg         m_axi_bready,
    output reg  [31:0] m_axi_araddr,
    output wire [2:0]  m_axi_arprot,
    output wire        m_axi_arvalid,
    input  wire        m_axi_arready,
    input  wire [31:0] m_axi_rdata,
    input  wire [1:0]  m_axi_rresp,
    input  wire        m_axi_rvalid,
    output reg         m_axi_rready
);

    localparam [2:0] S_IDLE    = 3'd0,
                     S_WR_AD   = 3'd1,
                     S_WR_RESP = 3'd2,
                     S_RD_ADDR = 3'd3,
                     S_RD_DATA = 3'd4;

    reg [2:0] state;
    reg       awvalid_q, wvalid_q, arvalid_q;

    // VALIDs are masked by rst: the registers only clear at the first reset
    // edge, and AXI requires VALID low during reset (rst is the global reset,
    // not an AXI input, so this is not an input-to-output path of the interface).
    assign m_axi_awvalid = awvalid_q & ~rst;
    assign m_axi_wvalid  = wvalid_q  & ~rst;
    assign m_axi_arvalid = arvalid_q & ~rst;

    assign req_ready    = (state == S_IDLE) & ~rst;
    assign m_axi_awprot = 3'b000;
    assign m_axi_arprot = 3'b000;

    wire aw_hs = m_axi_awvalid & m_axi_awready;
    wire w_hs  = m_axi_wvalid  & m_axi_wready;

    always @(posedge clk) begin
        if (rst) begin
            state         <= S_IDLE;
            awvalid_q <= 1'b0;
            wvalid_q  <= 1'b0;
            m_axi_bready  <= 1'b0;
            arvalid_q <= 1'b0;
            m_axi_rready  <= 1'b0;
            rsp_valid     <= 1'b0;
            rsp_err       <= 1'b0;
        end else begin
            rsp_valid <= 1'b0;
            case (state)
                S_IDLE: begin
                    if (req_valid) begin                       // req_ready = 1 in IDLE
                        if (req_we) begin
                            m_axi_awaddr  <= {req_addr[31:2], 2'b00};
                            m_axi_wdata   <= req_wdata;
                            m_axi_wstrb   <= req_wstrb;
                            awvalid_q <= 1'b1;
                            wvalid_q  <= 1'b1;
                            state         <= S_WR_AD;
                        end else begin
                            m_axi_araddr  <= {req_addr[31:2], 2'b00};
                            arvalid_q <= 1'b1;
                            state         <= S_RD_ADDR;
                        end
                    end
                end

                S_WR_AD: begin
                    if (aw_hs) awvalid_q <= 1'b0;
                    if (w_hs)  wvalid_q  <= 1'b0;
                    // both channels done, counting a handshake in this very cycle
                    if ((!awvalid_q || aw_hs) && (!wvalid_q || w_hs)) begin
                        m_axi_bready <= 1'b1;
                        state        <= S_WR_RESP;
                    end
                end

                S_WR_RESP: begin
                    if (m_axi_bvalid) begin                    // BREADY = 1 here
                        m_axi_bready <= 1'b0;
                        rsp_valid    <= 1'b1;
                        rsp_err      <= (m_axi_bresp != 2'b00);
                        state        <= S_IDLE;
                    end
                end

                S_RD_ADDR: begin
                    if (m_axi_arready) begin                   // ARVALID = 1 here
                        arvalid_q <= 1'b0;
                        m_axi_rready  <= 1'b1;
                        state         <= S_RD_DATA;
                    end
                end

                S_RD_DATA: begin
                    if (m_axi_rvalid) begin                    // RREADY = 1 here
                        m_axi_rready <= 1'b0;
                        rsp_valid    <= 1'b1;
                        rsp_rdata    <= m_axi_rdata;
                        rsp_err      <= (m_axi_rresp != 2'b00);
                        state        <= S_IDLE;
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire
