`timescale 1ns / 1ps
`default_nettype none

`include "soc_addr_map.vh"

// rv32_soc_top - platform-independent CPU subsystem of the RV32I SoC
// (docs/soc/soc_bus_architecture.md section 3).
//
//   rv32i_core --imem CMI--> soc_ram_dp port A
//              --dmem CMI--> soc_dmem_router --> soc_ram_dp port B   (0x0000_0000 .. MEM_BYTES-1)
//                                            --> cmi2axil --> m_axi  (SYSCTRL 0x1000_0000, 4 KB)
//                                            [MBOX = 1: --> soc_io_split --> cmi_bram_port (mailbox)]
//                                            --> error               (all other addresses)
//
// The only bus to the rest of the SoC is the AXI4-Lite master m_axi. In the
// Vivado block design it connects to the AMD AXI SmartConnect, which routes
// to the AXI slaves. The router forwards only the implemented windows
// (soc_addr_map.vh), so unmapped addresses get a CMI error without any AXI
// transaction; the SmartConnect's DECERR is not relied on (with a single
// address segment it does not produce one).
//
// E3C = 1 adds the E3c windows of soc_addr_map.vh (DSP 0x1000_2000, CNN
// 0x1000_5000, replay 0x1000_6000, 4 KB each) to the forwarded MMIO windows;
// the block design must then have exactly these slaves. E3C = 0 (default) is
// the stage 5/6A design: SYSCTRL only.
//
// MBOX = 1 (E4a, needs E3C = 1) adds the mailbox window SOC_MBX_* (0x2002_0000,
// 4 KB): soc_io_split sends it to cmi_bram_port, whose native BRAM port mbx_bram_*
// is port A of the shared dual-port mailbox BRAM (port B: the PS through an AXI
// BRAM Controller; docs/soc/e4a_mailbox.md). MBOX = 0 (default) keeps the router
// -> cmi2axil connection unchanged and drives mbx_bram_* with constants.
//
// AXI_DECODE_TEST = 1 is for VERIFICATION ONLY: the router then forwards all
// of 0x1000_0000-0x2FFF_FFFF to m_axi (the old, coarse decode) so that a test
// block design with several AXI segments can check the SmartConnect DECERR
// path. Production designs keep the default 0.
//
// Clock/reset: aclk; aresetn is active low and must be synchronous to aclk
// (from a reset synchroniser / proc_sys_reset in the board design). The CPU,
// RAM, router and adapter all use rst = ~aresetn, the same reset as the AXI
// slaves (CMI reset contract).
//
// The dbg_* outputs (retire/trap events, CMI taps) are for verification and
// on-chip debug (ILA); leave them unconnected otherwise.

module rv32_soc_top #(
    parameter [31:0] RESET_VECTOR = 32'h0000_0000,
    parameter        MEM_BYTES    = 65536,
    parameter        INIT_FILE    = "",
    parameter        AXI_DECODE_TEST = 0,
    parameter        E3C          = 0,
    parameter        E3C_REPLAY   = 1,    // 1: the E3c replay page is on m_axi (E5a: 0, the source page belongs to the PS)
    parameter        MBOX         = 0
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF m_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    input  wire        aresetn,

    // AXI4-Lite master
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

    // E4a mailbox: native BRAM port (BRAM_CTRL style, byte address, read latency 1)
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

    // status
    output wire        halted,
    output wire [3:0]  trap_cause,
    output wire [31:0] trap_pc,
    output wire [31:0] trap_tval,

    // debug: retire / trap events
    output wire        dbg_trap_valid,
    output wire        dbg_retire_valid,
    output wire [31:0] dbg_retire_pc,
    output wire [31:0] dbg_retire_insn,
    output wire        dbg_retire_rd_we,
    output wire [4:0]  dbg_retire_rd,
    output wire [31:0] dbg_retire_rd_wdata,

    // debug: CMI taps (core side)
    output wire        dbg_imem_req_valid,
    output wire        dbg_imem_req_ready,
    output wire [31:0] dbg_imem_req_addr,
    output wire        dbg_imem_rsp_valid,
    output wire        dbg_imem_rsp_err,
    output wire        dbg_dmem_req_valid,
    output wire        dbg_dmem_req_ready,
    output wire [31:0] dbg_dmem_req_addr,
    output wire        dbg_dmem_req_we,
    output wire [3:0]  dbg_dmem_req_wstrb,
    output wire [31:0] dbg_dmem_req_wdata,
    output wire        dbg_dmem_rsp_valid,
    output wire [31:0] dbg_dmem_rsp_rdata,
    output wire        dbg_dmem_rsp_err
);

    wire rst = ~aresetn;

    // core <-> imem
    wire        i_req_valid, i_req_ready, i_rsp_valid, i_rsp_err;
    wire [31:0] i_req_addr, i_rsp_rdata;
    // core <-> router
    wire        d_req_valid, d_req_ready, d_req_we, d_rsp_valid, d_rsp_err;
    wire [31:0] d_req_addr, d_req_wdata, d_rsp_rdata;
    wire [3:0]  d_req_wstrb;
    // router <-> RAM port B
    wire        r_req_valid, r_req_ready, r_req_we, r_rsp_valid, r_rsp_err;
    wire [31:0] r_req_addr, r_req_wdata, r_rsp_rdata;
    wire [3:0]  r_req_wstrb;
    // router <-> cmi2axil
    wire        x_req_valid, x_req_ready, x_req_we, x_rsp_valid, x_rsp_err;
    wire [31:0] x_req_addr, x_req_wdata, x_rsp_rdata;
    wire [3:0]  x_req_wstrb;

    rv32i_core #(.RESET_VECTOR(RESET_VECTOR)) u_core (
        .clk(aclk), .rst(rst),
        .imem_req_valid(i_req_valid), .imem_req_ready(i_req_ready), .imem_req_addr(i_req_addr),
        .imem_rsp_valid(i_rsp_valid), .imem_rsp_rdata(i_rsp_rdata), .imem_rsp_err(i_rsp_err),
        .dmem_req_valid(d_req_valid), .dmem_req_ready(d_req_ready), .dmem_req_addr(d_req_addr),
        .dmem_req_we(d_req_we), .dmem_req_wstrb(d_req_wstrb), .dmem_req_wdata(d_req_wdata),
        .dmem_rsp_valid(d_rsp_valid), .dmem_rsp_rdata(d_rsp_rdata), .dmem_rsp_err(d_rsp_err),
        .halted_o(halted), .trap_cause_o(trap_cause), .trap_pc_o(trap_pc), .trap_tval_o(trap_tval),
        .dbg_trap_valid_o(dbg_trap_valid),
        .dbg_retire_valid_o(dbg_retire_valid), .dbg_retire_pc_o(dbg_retire_pc),
        .dbg_retire_insn_o(dbg_retire_insn), .dbg_retire_rd_we_o(dbg_retire_rd_we),
        .dbg_retire_rd_o(dbg_retire_rd), .dbg_retire_rd_wdata_o(dbg_retire_rd_wdata)
    );

    soc_ram_dp #(.MEM_BYTES(MEM_BYTES), .INIT_FILE(INIT_FILE)) u_ram (
        .clk(aclk), .rst(rst),
        .a_req_valid(i_req_valid), .a_req_ready(i_req_ready), .a_req_addr(i_req_addr),
        .a_rsp_valid(i_rsp_valid), .a_rsp_rdata(i_rsp_rdata), .a_rsp_err(i_rsp_err),
        .b_req_valid(r_req_valid), .b_req_ready(r_req_ready), .b_req_addr(r_req_addr),
        .b_req_we(r_req_we), .b_req_wstrb(r_req_wstrb), .b_req_wdata(r_req_wdata),
        .b_rsp_valid(r_rsp_valid), .b_rsp_rdata(r_rsp_rdata), .b_rsp_err(r_rsp_err)
    );

    // MMIO windows forwarded to m_axi (soc_addr_map.vh; AXI_DECODE_TEST, E3C, MBOX: see above)
    localparam        MBX  = (AXI_DECODE_TEST == 0 && E3C != 0 && MBOX != 0) ? 1 : 0;
    localparam        RPL  = (E3C_REPLAY != 0) ? 1 : 0;
    localparam        IO_N = MBX ? 4 + RPL : (AXI_DECODE_TEST == 0 && E3C != 0) ? 3 + RPL : 1;
    localparam [32*IO_N-1:0] IO_BASE_P =
        (AXI_DECODE_TEST != 0) ? 32'h1000_0000 :
        (MBX && !RPL) ? {`SOC_MBX_BASE, `SOC_CNN_BASE, `SOC_DSP_BASE, `SOC_SYSCTRL_BASE} :
        MBX ? {`SOC_MBX_BASE, `SOC_REPLAY_BASE, `SOC_CNN_BASE, `SOC_DSP_BASE, `SOC_SYSCTRL_BASE} :
        (E3C != 0) ? {`SOC_REPLAY_BASE, `SOC_CNN_BASE, `SOC_DSP_BASE, `SOC_SYSCTRL_BASE} : `SOC_SYSCTRL_BASE;
    localparam [32*IO_N-1:0] IO_SIZE_P =
        (AXI_DECODE_TEST != 0) ? 32'h2000_0000 :
        (MBX && !RPL) ? {`SOC_MBX_SIZE, `SOC_CNN_SIZE, `SOC_DSP_SIZE, `SOC_SYSCTRL_SIZE} :
        MBX ? {`SOC_MBX_SIZE, `SOC_REPLAY_SIZE, `SOC_CNN_SIZE, `SOC_DSP_SIZE,
               `SOC_SYSCTRL_SIZE} :
        (E3C != 0) ? {`SOC_REPLAY_SIZE, `SOC_CNN_SIZE, `SOC_DSP_SIZE, `SOC_SYSCTRL_SIZE} : `SOC_SYSCTRL_SIZE;

    // synthesis translate_off
    initial if (MBOX != 0 && MBX == 0) begin
        $display("ERROR: rv32_soc_top: MBOX = 1 needs E3C = 1 and AXI_DECODE_TEST = 0");
        $finish;
    end
    // synthesis translate_on

    soc_dmem_router #(
        .RAM_BYTES(MEM_BYTES), .IO_WINDOWS(IO_N), .IO_BASE(IO_BASE_P), .IO_SIZE(IO_SIZE_P)
    ) u_router (
        .clk(aclk), .rst(rst),
        .s_req_valid(d_req_valid), .s_req_ready(d_req_ready), .s_req_addr(d_req_addr),
        .s_req_we(d_req_we), .s_req_wstrb(d_req_wstrb), .s_req_wdata(d_req_wdata),
        .s_rsp_valid(d_rsp_valid), .s_rsp_rdata(d_rsp_rdata), .s_rsp_err(d_rsp_err),
        .ram_req_valid(r_req_valid), .ram_req_ready(r_req_ready), .ram_req_addr(r_req_addr),
        .ram_req_we(r_req_we), .ram_req_wstrb(r_req_wstrb), .ram_req_wdata(r_req_wdata),
        .ram_rsp_valid(r_rsp_valid), .ram_rsp_rdata(r_rsp_rdata), .ram_rsp_err(r_rsp_err),
        .io_req_valid(x_req_valid), .io_req_ready(x_req_ready), .io_req_addr(x_req_addr),
        .io_req_we(x_req_we), .io_req_wstrb(x_req_wstrb), .io_req_wdata(x_req_wdata),
        .io_rsp_valid(x_rsp_valid), .io_rsp_rdata(x_rsp_rdata), .io_rsp_err(x_rsp_err)
    );

    // cmi2axil input: the router's MMIO port (MBX = 0) or port a of soc_io_split (MBX = 1)
    wire        a_req_valid, a_req_ready, a_req_we, a_rsp_valid, a_rsp_err;
    wire [31:0] a_req_addr, a_req_wdata, a_rsp_rdata;
    wire [3:0]  a_req_wstrb;

    generate
        if (MBX) begin : g_mbx
            wire        b_req_valid, b_req_ready, b_req_we, b_rsp_valid, b_rsp_err;
            wire [31:0] b_req_addr, b_req_wdata, b_rsp_rdata;
            wire [3:0]  b_req_wstrb;
            soc_io_split #(.B_BASE(`SOC_MBX_BASE), .B_SIZE(`SOC_MBX_SIZE)) u_split (
                .clk(aclk), .rst(rst),
                .s_req_valid(x_req_valid), .s_req_ready(x_req_ready), .s_req_addr(x_req_addr),
                .s_req_we(x_req_we), .s_req_wstrb(x_req_wstrb), .s_req_wdata(x_req_wdata),
                .s_rsp_valid(x_rsp_valid), .s_rsp_rdata(x_rsp_rdata), .s_rsp_err(x_rsp_err),
                .a_req_valid(a_req_valid), .a_req_ready(a_req_ready), .a_req_addr(a_req_addr),
                .a_req_we(a_req_we), .a_req_wstrb(a_req_wstrb), .a_req_wdata(a_req_wdata),
                .a_rsp_valid(a_rsp_valid), .a_rsp_rdata(a_rsp_rdata), .a_rsp_err(a_rsp_err),
                .b_req_valid(b_req_valid), .b_req_ready(b_req_ready), .b_req_addr(b_req_addr),
                .b_req_we(b_req_we), .b_req_wstrb(b_req_wstrb), .b_req_wdata(b_req_wdata),
                .b_rsp_valid(b_rsp_valid), .b_rsp_rdata(b_rsp_rdata), .b_rsp_err(b_rsp_err)
            );
            cmi_bram_port #(.SIZE(`SOC_MBX_SIZE)) u_mbx (
                .clk(aclk), .rst(rst),
                .req_valid(b_req_valid), .req_ready(b_req_ready), .req_addr(b_req_addr),
                .req_we(b_req_we), .req_wstrb(b_req_wstrb), .req_wdata(b_req_wdata),
                .rsp_valid(b_rsp_valid), .rsp_rdata(b_rsp_rdata), .rsp_err(b_rsp_err),
                .bram_clk(mbx_bram_clk), .bram_rst(mbx_bram_rst), .bram_en(mbx_bram_en),
                .bram_we(mbx_bram_we), .bram_addr(mbx_bram_addr), .bram_wrdata(mbx_bram_wrdata),
                .bram_rddata(mbx_bram_rddata)
            );
        end else begin : g_nombx
            assign a_req_valid = x_req_valid;
            assign x_req_ready = a_req_ready;
            assign a_req_addr  = x_req_addr;
            assign a_req_we    = x_req_we;
            assign a_req_wstrb = x_req_wstrb;
            assign a_req_wdata = x_req_wdata;
            assign x_rsp_valid = a_rsp_valid;
            assign x_rsp_rdata = a_rsp_rdata;
            assign x_rsp_err   = a_rsp_err;
            assign mbx_bram_clk    = aclk;
            assign mbx_bram_rst    = 1'b1;
            assign mbx_bram_en     = 1'b0;
            assign mbx_bram_we     = 4'b0000;
            assign mbx_bram_addr   = 32'h0;
            assign mbx_bram_wrdata = 32'h0;
        end
    endgenerate

    cmi2axil u_axi (
        .clk(aclk), .rst(rst),
        .req_valid(a_req_valid), .req_ready(a_req_ready), .req_addr(a_req_addr),
        .req_we(a_req_we), .req_wstrb(a_req_wstrb), .req_wdata(a_req_wdata),
        .rsp_valid(a_rsp_valid), .rsp_rdata(a_rsp_rdata), .rsp_err(a_rsp_err),
        .m_axi_awaddr(m_axi_awaddr), .m_axi_awprot(m_axi_awprot), .m_axi_awvalid(m_axi_awvalid),
        .m_axi_awready(m_axi_awready), .m_axi_wdata(m_axi_wdata), .m_axi_wstrb(m_axi_wstrb),
        .m_axi_wvalid(m_axi_wvalid), .m_axi_wready(m_axi_wready), .m_axi_bresp(m_axi_bresp),
        .m_axi_bvalid(m_axi_bvalid), .m_axi_bready(m_axi_bready), .m_axi_araddr(m_axi_araddr),
        .m_axi_arprot(m_axi_arprot), .m_axi_arvalid(m_axi_arvalid), .m_axi_arready(m_axi_arready),
        .m_axi_rdata(m_axi_rdata), .m_axi_rresp(m_axi_rresp), .m_axi_rvalid(m_axi_rvalid),
        .m_axi_rready(m_axi_rready)
    );

    assign dbg_imem_req_valid = i_req_valid;
    assign dbg_imem_req_ready = i_req_ready;
    assign dbg_imem_req_addr  = i_req_addr;
    assign dbg_imem_rsp_valid = i_rsp_valid;
    assign dbg_imem_rsp_err   = i_rsp_err;
    assign dbg_dmem_req_valid = d_req_valid;
    assign dbg_dmem_req_ready = d_req_ready;
    assign dbg_dmem_req_addr  = d_req_addr;
    assign dbg_dmem_req_we    = d_req_we;
    assign dbg_dmem_req_wstrb = d_req_wstrb;
    assign dbg_dmem_req_wdata = d_req_wdata;
    assign dbg_dmem_rsp_valid = d_rsp_valid;
    assign dbg_dmem_rsp_rdata = d_rsp_rdata;
    assign dbg_dmem_rsp_err   = d_rsp_err;

endmodule

`default_nettype wire
