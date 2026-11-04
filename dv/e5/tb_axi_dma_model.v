`timescale 1ns / 1ps
`default_nettype none

// tb_axi_dma_model - behavioural model of the AMD AXI DMA MM2S channel in direct register mode, with
// its own frame memory, for the Icarus system tests (the xsim tests use the real IP and
// tb_axi_ddr_model). Same registers and the same programming sequence as the IP (PG021):
//   0x00 MM2S_DMACR [0] RS [2] Reset (self-clearing)
//   0x04 MM2S_DMASR [0] Halted [1] Idle [5] DMASlvErr [12] IOC_Irq (write 1 to clear)
//   0x18 MM2S_SA  0x1C MM2S_SA_MSB  0x28 MM2S_LENGTH (a write starts the transfer)
// Plusargs: +ddr=<hex file, one 64-bit word per line> +ddr_base=<hex> byte address of word 0
//           +dma_stall_at=N +dma_stall_cycles=C one stall of C cycles before beat N of every transfer
//           +dma_stall_pct=P random gaps between beats

module tb_axi_dma_model #(
    parameter WORDS = 32768
) (
    input  wire         aclk,
    input  wire         aresetn,
    input  wire [31:0]  s_awaddr, input wire s_awvalid, output reg s_awready,
    input  wire [31:0]  s_wdata,  input wire [3:0] s_wstrb, input wire s_wvalid, output reg s_wready,
    output reg  [1:0]   s_bresp,  output reg s_bvalid, input wire s_bready,
    input  wire [31:0]  s_araddr, input wire s_arvalid, output reg s_arready,
    output reg  [31:0]  s_rdata,  output reg [1:0] s_rresp, output reg s_rvalid, input wire s_rready,
    output reg  [63:0]  m_tdata,
    output wire [7:0]   m_tkeep,
    output reg          m_tvalid,
    input  wire         m_tready,
    output reg          m_tlast
);
    reg [63:0] mem [0:WORDS-1];
    reg [8*512-1:0] file;
    reg [31:0] ddr_base = 32'h0;
    integer stall_at = -1, stall_cycles = 0, stall_pct = 0, seed = 11;
    initial begin
        if ($value$plusargs("ddr=%s", file)) $readmemh(file, mem);
        if (!$value$plusargs("ddr_base=%h", ddr_base)) ddr_base = 32'h0;
        if (!$value$plusargs("dma_stall_at=%d", stall_at)) stall_at = -1;
        if (!$value$plusargs("dma_stall_cycles=%d", stall_cycles)) stall_cycles = 0;
        if (!$value$plusargs("dma_stall_pct=%d", stall_pct)) stall_pct = 0;
    end
    assign m_tkeep = 8'hFF;

    reg        rs = 1'b0, ioc = 1'b0, slverr = 1'b0, busy = 1'b0, have_aw = 1'b0, have_w = 1'b0;
    reg [31:0] sa = 32'h0, sa_msb = 32'h0, length = 32'h0, awa = 32'h0, wd = 32'h0;
    wire       halted = ~rs;
    wire       idle   = rs & ~busy;
    wire [31:0] dmasr = {19'd0, ioc, 6'd0, slverr, 3'd0, idle, halted};
    integer beat = 0, beats = 0, wait_c = 0, n_transfers = 0;

    initial begin
        s_awready = 1'b0; s_wready = 1'b0; s_bvalid = 1'b0; s_bresp = 2'b00; s_arready = 1'b0;
        s_rvalid = 1'b0; s_rdata = 32'd0; s_rresp = 2'b00; m_tvalid = 1'b0; m_tlast = 1'b0; m_tdata = 64'd0;
    end

    always @(posedge aclk) begin
        s_awready <= 1'b0; s_wready <= 1'b0; s_arready <= 1'b0;
        if (!aresetn) begin
            s_bvalid <= 1'b0; s_rvalid <= 1'b0; have_aw <= 1'b0; have_w <= 1'b0;
            rs <= 1'b0; ioc <= 1'b0; slverr <= 1'b0; busy <= 1'b0; m_tvalid <= 1'b0; m_tlast <= 1'b0; m_tdata <= 64'd0;
        end else begin
            // ---- register port ----
            if (s_awvalid && !have_aw && !s_awready) begin s_awready <= 1'b1; awa <= s_awaddr; have_aw <= 1'b1; end
            if (s_wvalid && !have_w && !s_wready) begin s_wready <= 1'b1; wd <= s_wdata; have_w <= 1'b1; end
            if (have_aw && have_w && !s_bvalid) begin
                have_aw <= 1'b0; have_w <= 1'b0; s_bvalid <= 1'b1; s_bresp <= 2'b00;
                case (awa[7:0])
                    8'h00: begin
                        if (wd[2]) begin rs <= 1'b0; ioc <= 1'b0; slverr <= 1'b0; busy <= 1'b0; m_tvalid <= 1'b0; end
                        else rs <= wd[0];
                    end
                    8'h04: if (wd[12]) ioc <= 1'b0;
                    8'h18: sa <= wd;
                    8'h1C: sa_msb <= wd;
                    8'h28: if (rs && !busy) begin
                        length <= wd; busy <= 1'b1; beat <= 0; beats <= wd[31:3]; wait_c <= 0;
                        n_transfers <= n_transfers + 1;
                        if (sa_msb != 0 || sa < ddr_base || wd[2:0] != 3'd0 ||
                            ((sa - ddr_base) >> 3) + wd[31:3] > WORDS) begin
                            slverr <= 1'b1; rs <= 1'b0; busy <= 1'b0;
                        end
                    end
                    default: ;
                endcase
            end
            if (s_bvalid && s_bready) s_bvalid <= 1'b0;
            if (s_arvalid && !s_arready && !s_rvalid) begin
                s_arready <= 1'b1; s_rvalid <= 1'b1; s_rresp <= 2'b00;
                case (s_araddr[7:0])
                    8'h00: s_rdata <= {31'd0, rs};
                    8'h04: s_rdata <= dmasr;
                    8'h18: s_rdata <= sa;
                    8'h1C: s_rdata <= sa_msb;
                    8'h28: s_rdata <= length;
                    default: s_rdata <= 32'd0;
                endcase
            end
            if (s_rvalid && s_rready) s_rvalid <= 1'b0;

            // ---- stream ----
            if (m_tvalid && m_tready) begin
                m_tvalid <= 1'b0;
                if (m_tlast) begin busy <= 1'b0; ioc <= 1'b1; m_tlast <= 1'b0; end
            end
            if (busy && beat < beats && (!m_tvalid || m_tready)) begin
                if (beat == stall_at && wait_c < stall_cycles) wait_c <= wait_c + 1;
                else if (stall_pct == 0 || ({$random(seed)} % 100) >= stall_pct) begin
                    m_tvalid <= 1'b1;
                    m_tdata  <= mem[((sa - ddr_base) >> 3) + beat];
                    m_tlast  <= (beat == beats - 1);
                    beat     <= beat + 1;
                end
            end
        end
    end
endmodule

`default_nettype wire
