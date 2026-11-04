`timescale 1ns / 1ps
`default_nettype none

// kv260_cpu_dbg - KV260 bring-up wrapper of the CPU subsystem (stage 6A).
//
// Instantiates the unchanged rv32_soc_top and passes its AXI4-Lite master
// straight through (wires only, so the block design address space stays on
// this cell's m_axi). In addition it packs the verification/debug outputs and
// the AXI4-Lite channel signals into four wide buses for one ILA: the Vivado
// BASIC license allows at most 5 probes per ILA (Chipscope 16-620), and the
// System ILA AXI slot alone exceeds that. Packing is wiring only, except for
// two OR terms (trace_event, axi_hs) used as ILA storage qualifier.
//
// ila_ctrl  [63:0]
//   0 retire_valid   1 retire_rd_we   2 trap_valid     3 dmem_req_valid
//   4 dmem_req_ready 5 dmem_req_we    6 dmem_rsp_valid 7 dmem_rsp_err
//   8 halted         9 tohost_wr     10 aresetn       11 pl_resetn0
//  12 trace_event   13 axi_hs        14 imem_req_valid 15 imem_req_ready
//  19:16 dmem_req_wstrb  23:20 trap_cause  28:24 retire_rd
//  32 awvalid 33 awready 34 wvalid 35 wready 36 bvalid 37 bready
//  38 arvalid 39 arready 40 rvalid 41 rready 43:42 bresp 45:44 rresp 49:46 wstrb
//  trace_event = retire | data request handshake | data response | trap | TOHOST write | axi_hs
//  axi_hs      = any AXI4-Lite handshake (AW, W, B, AR or R) on m_axi
// ila_cpu   [159:0] {dmem_req_wdata, dmem_req_addr, retire_rd_wdata, retire_insn, retire_pc}
// ila_trap  [95:0]  {tohost_value, trap_tval, trap_pc}
// ila_axi   [127:0] {rdata, araddr, wdata, awaddr}

module kv260_cpu_dbg #(
    parameter [31:0] RESET_VECTOR = 32'h0000_0000,
    parameter        MEM_BYTES    = 65536,
    parameter        INIT_FILE    = "",
    parameter        E3C          = 0,          // 1: E3c MMIO windows (stage 6A: 0)
    parameter        E3C_REPLAY   = 1,          // 0: no replay window (E5a: the source page belongs to the PS)
    parameter        MBOX         = 0           // 1: E4a mailbox BRAM port (needs E3C = 1)
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF m_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    input  wire        aresetn,

    output wire [31:0] m_axi_awaddr,
    output wire [2:0]  m_axi_awprot,
    output wire        m_axi_awvalid,
    input  wire        m_axi_awready,
    output wire [31:0] m_axi_wdata,
    output wire [3:0]  m_axi_wstrb,
    output wire        m_axi_wvalid,
    input  wire        m_axi_wready,
    input  wire [1:0]  m_axi_bresp,
    input  wire        m_axi_bvalid,
    output wire        m_axi_bready,
    output wire [31:0] m_axi_araddr,
    output wire [2:0]  m_axi_arprot,
    output wire        m_axi_arvalid,
    input  wire        m_axi_arready,
    input  wire [31:0] m_axi_rdata,
    input  wire [1:0]  m_axi_rresp,
    input  wire        m_axi_rvalid,
    output wire        m_axi_rready,

    // E4a mailbox BRAM port A (rv32_soc_top mbx_bram_*, wires only)
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM CLK" *)
    (* X_INTERFACE_PARAMETER = "MASTER_TYPE BRAM_CTRL, MEM_SIZE 4096, MEM_WIDTH 32, MEM_ECC NONE, READ_LATENCY 1" *)
    (* X_INTERFACE_MODE = "Master" *)
    output wire        mbx_bram_clk,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM RST" *)
    output wire        mbx_bram_rst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM EN" *)
    output wire        mbx_bram_en,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM WE" *)
    output wire [3:0]  mbx_bram_we,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM ADDR" *)
    output wire [31:0] mbx_bram_addr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM DIN" *)
    output wire [31:0] mbx_bram_wrdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:bram:1.0 MBX_BRAM DOUT" *)
    input  wire [31:0] mbx_bram_rddata,

    output wire        halted,
    output wire [3:0]  trap_cause,
    output wire [31:0] trap_pc,
    output wire [31:0] trap_tval,
    output wire        dbg_retire_valid,
    output wire [31:0] dbg_imem_req_addr,

    // observed only
    input  wire        tohost_wr,
    input  wire [31:0] tohost_value,
    input  wire        pl_resetn0,

    output wire [63:0]  ila_ctrl,
    output wire [159:0] ila_cpu,
    output wire [95:0]  ila_trap,
    output wire [127:0] ila_axi
);

    wire        trap_valid, rd_we, i_valid, i_ready, i_rsp_valid, i_rsp_err;
    wire        d_valid, d_ready, d_we, d_rsp_valid, d_rsp_err;
    wire [4:0]  rd;
    wire [3:0]  d_wstrb;
    wire [31:0] r_pc, r_insn, r_wdata, d_addr, d_wdata, d_rdata;

    rv32_soc_top #(.RESET_VECTOR(RESET_VECTOR), .MEM_BYTES(MEM_BYTES), .INIT_FILE(INIT_FILE), .E3C(E3C), .E3C_REPLAY(E3C_REPLAY),
                   .MBOX(MBOX)) u_soc (
        .aclk(aclk), .aresetn(aresetn),
        .m_axi_awaddr(m_axi_awaddr), .m_axi_awprot(m_axi_awprot), .m_axi_awvalid(m_axi_awvalid),
        .m_axi_awready(m_axi_awready), .m_axi_wdata(m_axi_wdata), .m_axi_wstrb(m_axi_wstrb),
        .m_axi_wvalid(m_axi_wvalid), .m_axi_wready(m_axi_wready), .m_axi_bresp(m_axi_bresp),
        .m_axi_bvalid(m_axi_bvalid), .m_axi_bready(m_axi_bready), .m_axi_araddr(m_axi_araddr),
        .m_axi_arprot(m_axi_arprot), .m_axi_arvalid(m_axi_arvalid), .m_axi_arready(m_axi_arready),
        .m_axi_rdata(m_axi_rdata), .m_axi_rresp(m_axi_rresp), .m_axi_rvalid(m_axi_rvalid),
        .m_axi_rready(m_axi_rready),
        .mbx_bram_clk(mbx_bram_clk), .mbx_bram_rst(mbx_bram_rst), .mbx_bram_en(mbx_bram_en),
        .mbx_bram_we(mbx_bram_we), .mbx_bram_addr(mbx_bram_addr), .mbx_bram_wrdata(mbx_bram_wrdata),
        .mbx_bram_rddata(mbx_bram_rddata),
        .halted(halted), .trap_cause(trap_cause), .trap_pc(trap_pc), .trap_tval(trap_tval),
        .dbg_trap_valid(trap_valid), .dbg_retire_valid(dbg_retire_valid), .dbg_retire_pc(r_pc),
        .dbg_retire_insn(r_insn), .dbg_retire_rd_we(rd_we), .dbg_retire_rd(rd),
        .dbg_retire_rd_wdata(r_wdata),
        .dbg_imem_req_valid(i_valid), .dbg_imem_req_ready(i_ready), .dbg_imem_req_addr(dbg_imem_req_addr),
        .dbg_imem_rsp_valid(i_rsp_valid), .dbg_imem_rsp_err(i_rsp_err),
        .dbg_dmem_req_valid(d_valid), .dbg_dmem_req_ready(d_ready), .dbg_dmem_req_addr(d_addr),
        .dbg_dmem_req_we(d_we), .dbg_dmem_req_wstrb(d_wstrb), .dbg_dmem_req_wdata(d_wdata),
        .dbg_dmem_rsp_valid(d_rsp_valid), .dbg_dmem_rsp_rdata(d_rdata), .dbg_dmem_rsp_err(d_rsp_err)
    );

    wire axi_hs = (m_axi_awvalid & m_axi_awready) | (m_axi_wvalid & m_axi_wready) |
                  (m_axi_bvalid & m_axi_bready) | (m_axi_arvalid & m_axi_arready) |
                  (m_axi_rvalid & m_axi_rready);
    wire trace_event = dbg_retire_valid | (d_valid & d_ready) | d_rsp_valid | trap_valid | tohost_wr | axi_hs;

    assign ila_ctrl = {14'd0, m_axi_wstrb, m_axi_rresp, m_axi_bresp,
                       m_axi_rready, m_axi_rvalid, m_axi_arready, m_axi_arvalid, m_axi_bready, m_axi_bvalid,
                       m_axi_wready, m_axi_wvalid, m_axi_awready, m_axi_awvalid,
                       3'd0, rd, trap_cause, d_wstrb,
                       i_ready, i_valid, axi_hs, trace_event, pl_resetn0, aresetn, tohost_wr, halted,
                       d_rsp_err, d_rsp_valid, d_we, d_ready, d_valid, trap_valid, rd_we, dbg_retire_valid};
    assign ila_cpu  = {d_wdata, d_addr, r_wdata, r_insn, r_pc};
    assign ila_trap = {tohost_value, trap_tval, trap_pc};
    assign ila_axi  = {m_axi_rdata, m_axi_araddr, m_axi_wdata, m_axi_awaddr};

endmodule

`default_nettype wire
