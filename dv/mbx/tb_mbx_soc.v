`timescale 1ns / 1ps
`default_nettype none

// tb_mbx_soc - E4a system test: the real RV32I V1 runs a mailbox firmware image
// (firmware/rv32/mailbox) that drives the E3c pipeline and publishes every CNN result
// to the shared mailbox BRAM; a PS-side consumer (mbx_ps_reader.vh) reads the records
// through an AXI4-Lite master at the PS address of the window, concurrently.
//   default (Icarus) rv32_soc_top #(E3C = 1, MBOX = 1) -> tb_axil_xbar -> SYSCTRL + E3c;
//           mailbox port A -> tb_bram_tdp <- tb_axil_bram_ctrl <- PS master
//   MBX_BD  (Vivado xsim) block design mbx_bd: the same RTL behind the real AMD AXI
//           SmartConnect, Block Memory Generator (true dual port) and AXI BRAM
//           Controller; the PS master drives the BD port S_AXI_PS (-> ps_smc -> controller)
// Checks: as tb_e3c_soc (TOHOST, trap, AXI protocol, +golden=1 per-layer golden checks,
// +period_check timing) plus mbx_monitor.vh (producer publish order / ownership / no
// overwrite) and the reader (every record vs the E2 golden decision, sequence, gaps).
// Plusargs (in addition to tb_e3c_soc's and mbx_ps_reader.vh's):
//   +rd_golden=1        load +vec for the reader's content checks without +golden
//   +seq_base=H         replay SEQ_BASE of the firmware (default 00A00000)
//   +exp_rx=N +exp_gaps=N +exp_resync=N   reader totals (-1 / absent: not checked)
//   +cpu_reset_on_rec=K reset the SoC (not the PS side) +cpu_reset_delay cycles after the
//                       K-th record invalidation, i.e. in the middle of a record write
//   +exp_epochs=N       producer epochs started (default 1)
//   +exp_published=N +exp_drop=N   header PUBLISHED_CNT / DROP_CNT at the end (any reader mode)
//   +mon_off=1          no protocol monitor checks (mbx_mmio: raw window accesses)
//   +exp_min_drop=N     header DROP_CNT >= N at the end
//   +exp_min_coll=N     (Icarus) at least N same-word read/write collisions in the BRAM

`ifdef MBX_BD
  `define SOC u_dut.mbx_bd_i.cpu_subsys.inst
  `define SYS u_dut.mbx_bd_i.sysctrl.inst
  `define E3C u_dut.mbx_bd_i.e3c.inst
`else
  `define SOC u_soc
  `define SYS u_sys
  `define E3C u_e3c
`endif

module tb_mbx_soc;
    // watchdog (hang guard only, normal end: +max_cycles): 2.5 s covers the 2 kHz runs (mbx_mg6_rt1: 1.8 s)
    localparam [63:0] TB_TIMEOUT_NS = 64'd2500000000;
    `include "tb_check.vh"
    `include "e3c_paths.vh"
    `include "mbx_layout.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0, ps_aresetn = 1'b0;
    integer cyc = 0;
    always @(posedge clk) cyc <= cyc + 1;

    // ---------------- PS-side AXI4-Lite master ----------------
    wire [31:0] ps_awaddr, ps_wdata, ps_araddr, ps_rdata;
    wire [3:0]  ps_wstrb;
    wire [1:0]  ps_bresp, ps_rresp;
    wire        ps_awvalid, ps_awready, ps_wvalid, ps_wready, ps_bvalid, ps_bready;
    wire        ps_arvalid, ps_arready, ps_rvalid, ps_rready;
    tb_axil_master #(.AW(32)) u_ps (.clk(clk),
        .awaddr(ps_awaddr), .awvalid(ps_awvalid), .awready(ps_awready), .wdata(ps_wdata), .wstrb(ps_wstrb),
        .wvalid(ps_wvalid), .wready(ps_wready), .bresp(ps_bresp), .bvalid(ps_bvalid), .bready(ps_bready),
        .araddr(ps_araddr), .arvalid(ps_arvalid), .arready(ps_arready), .rdata(ps_rdata), .rresp(ps_rresp),
        .rvalid(ps_rvalid), .rready(ps_rready));

`ifdef MBX_BD
    mbx_bd_wrapper u_dut (.aclk(clk), .aresetn(aresetn), .ps_aresetn(ps_aresetn),
        .S_AXI_PS_awaddr(ps_awaddr), .S_AXI_PS_awprot(3'b000), .S_AXI_PS_awvalid(ps_awvalid),
        .S_AXI_PS_awready(ps_awready), .S_AXI_PS_wdata(ps_wdata), .S_AXI_PS_wstrb(ps_wstrb),
        .S_AXI_PS_wvalid(ps_wvalid), .S_AXI_PS_wready(ps_wready), .S_AXI_PS_bresp(ps_bresp),
        .S_AXI_PS_bvalid(ps_bvalid), .S_AXI_PS_bready(ps_bready), .S_AXI_PS_araddr(ps_araddr),
        .S_AXI_PS_arprot(3'b000), .S_AXI_PS_arvalid(ps_arvalid), .S_AXI_PS_arready(ps_arready),
        .S_AXI_PS_rdata(ps_rdata), .S_AXI_PS_rresp(ps_rresp), .S_AXI_PS_rvalid(ps_rvalid),
        .S_AXI_PS_rready(ps_rready));
`else
    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    wire        ma_clk, ma_rst, ma_en;
    wire [3:0]  ma_we;
    wire [31:0] ma_addr, ma_wdata, ma_rdata;
    rv32_soc_top #(.E3C(1), .MBOX(1)) u_soc (
        .aclk(clk), .aresetn(aresetn),
        .m_axi_awaddr(awaddr), .m_axi_awprot(awprot), .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_araddr(araddr), .m_axi_arprot(arprot), .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rvalid(rvalid), .m_axi_rready(rready),
        .mbx_bram_clk(ma_clk), .mbx_bram_rst(ma_rst), .mbx_bram_en(ma_en), .mbx_bram_we(ma_we),
        .mbx_bram_addr(ma_addr), .mbx_bram_wrdata(ma_wdata), .mbx_bram_rddata(ma_rdata),
        .halted(), .trap_cause(), .trap_pc(), .trap_tval(),
        .dbg_trap_valid(), .dbg_retire_valid(), .dbg_retire_pc(), .dbg_retire_insn(),
        .dbg_retire_rd_we(), .dbg_retire_rd(), .dbg_retire_rd_wdata(),
        .dbg_imem_req_valid(), .dbg_imem_req_ready(), .dbg_imem_req_addr(), .dbg_imem_rsp_valid(),
        .dbg_imem_rsp_err(), .dbg_dmem_req_valid(), .dbg_dmem_req_ready(), .dbg_dmem_req_addr(),
        .dbg_dmem_req_we(), .dbg_dmem_req_wstrb(), .dbg_dmem_req_wdata(), .dbg_dmem_rsp_valid(),
        .dbg_dmem_rsp_rdata(), .dbg_dmem_rsp_err());

    // mailbox: port A = CPU, port B = PS (behavioural AXI4-Lite BRAM controller)
    wire        mb_en;
    wire [3:0]  mb_we;
    wire [31:0] mb_addr, mb_wdata, mb_rdata;
    tb_bram_tdp #(.SIZE(`MBX_SIZE)) u_mem (
        .clka(ma_clk), .ena(ma_en), .wea(ma_we), .addra(ma_addr), .dina(ma_wdata), .douta(ma_rdata),
        .clkb(clk), .enb(mb_en), .web(mb_we), .addrb(mb_addr), .dinb(mb_wdata), .doutb(mb_rdata));
    tb_axil_bram_ctrl #(.SIZE(`MBX_SIZE)) u_ctl (.aclk(clk), .aresetn(ps_aresetn),
        .s_awaddr(ps_awaddr), .s_awvalid(ps_awvalid), .s_awready(ps_awready), .s_wdata(ps_wdata),
        .s_wstrb(ps_wstrb), .s_wvalid(ps_wvalid), .s_wready(ps_wready), .s_bresp(ps_bresp),
        .s_bvalid(ps_bvalid), .s_bready(ps_bready), .s_araddr(ps_araddr), .s_arvalid(ps_arvalid),
        .s_arready(ps_arready), .s_rdata(ps_rdata), .s_rresp(ps_rresp), .s_rvalid(ps_rvalid),
        .s_rready(ps_rready), .bram_en(mb_en), .bram_we(mb_we), .bram_addr(mb_addr),
        .bram_wrdata(mb_wdata), .bram_rddata(mb_rdata));
    // the PS writes only the consumer words 0x040-0x04F
    always @(posedge clk) if (mb_en && |mb_we && (mb_addr < 32'h40 || mb_addr > 32'h4C))
        $display("ERROR: PS wrote a CPU-owned mailbox word %h (t=%0t)", mb_addr, $time);

    // slaves: 0 SYSCTRL, 1 DSP, 2 CNN, 3 replay (soc_addr_map.vh)
    wire [127:0] x_awaddr, x_wdata, x_araddr, x_rdata;
    wire [11:0]  x_awprot, x_arprot;
    wire [15:0]  x_wstrb;
    wire [7:0]   x_bresp, x_rresp;
    wire [3:0]   x_awvalid, x_awready, x_wvalid, x_wready, x_bvalid, x_bready, x_arvalid, x_arready, x_rvalid, x_rready;
    tb_axil_xbar #(.N(4),
        .BASE({32'h1000_6000, 32'h1000_5000, 32'h1000_2000, 32'h1000_0000}),
        .SIZE({32'h0000_1000, 32'h0000_1000, 32'h0000_1000, 32'h0000_1000})) u_xbar (
        .clk(clk), .aresetn(aresetn),
        .s_awaddr(awaddr), .s_awprot(awprot), .s_awvalid(awvalid), .s_awready(awready),
        .s_wdata(wdata), .s_wstrb(wstrb), .s_wvalid(wvalid), .s_wready(wready),
        .s_bresp(bresp), .s_bvalid(bvalid), .s_bready(bready),
        .s_araddr(araddr), .s_arprot(arprot), .s_arvalid(arvalid), .s_arready(arready),
        .s_rdata(rdata), .s_rresp(rresp), .s_rvalid(rvalid), .s_rready(rready),
        .m_awaddr(x_awaddr), .m_awprot(x_awprot), .m_awvalid(x_awvalid), .m_awready(x_awready),
        .m_wdata(x_wdata), .m_wstrb(x_wstrb), .m_wvalid(x_wvalid), .m_wready(x_wready),
        .m_bresp(x_bresp), .m_bvalid(x_bvalid), .m_bready(x_bready),
        .m_araddr(x_araddr), .m_arprot(x_arprot), .m_arvalid(x_arvalid), .m_arready(x_arready),
        .m_rdata(x_rdata), .m_rresp(x_rresp), .m_rvalid(x_rvalid), .m_rready(x_rready));

    wire [7:0] led; wire th_valid, th_wr; wire [31:0] th_value;
    soc_sysctrl u_sys (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(x_awaddr[11:0]), .s_axi_awprot(x_awprot[2:0]), .s_axi_awvalid(x_awvalid[0]), .s_axi_awready(x_awready[0]),
        .s_axi_wdata(x_wdata[31:0]), .s_axi_wstrb(x_wstrb[3:0]), .s_axi_wvalid(x_wvalid[0]), .s_axi_wready(x_wready[0]),
        .s_axi_bresp(x_bresp[1:0]), .s_axi_bvalid(x_bvalid[0]), .s_axi_bready(x_bready[0]),
        .s_axi_araddr(x_araddr[11:0]), .s_axi_arprot(x_arprot[2:0]), .s_axi_arvalid(x_arvalid[0]), .s_axi_arready(x_arready[0]),
        .s_axi_rdata(x_rdata[31:0]), .s_axi_rresp(x_rresp[1:0]), .s_axi_rvalid(x_rvalid[0]), .s_axi_rready(x_rready[0]),
        .led(led), .tohost_valid(th_valid), .tohost_value(th_value), .tohost_wr(th_wr));

    `define XS(i) \
        .s_axi_``i``_awaddr(x_awaddr[32*IDX_``i +: 12]), .s_axi_``i``_awprot(x_awprot[3*IDX_``i +: 3]), \
        .s_axi_``i``_awvalid(x_awvalid[IDX_``i]), .s_axi_``i``_awready(x_awready[IDX_``i]), \
        .s_axi_``i``_wdata(x_wdata[32*IDX_``i +: 32]), .s_axi_``i``_wstrb(x_wstrb[4*IDX_``i +: 4]), \
        .s_axi_``i``_wvalid(x_wvalid[IDX_``i]), .s_axi_``i``_wready(x_wready[IDX_``i]), \
        .s_axi_``i``_bresp(x_bresp[2*IDX_``i +: 2]), .s_axi_``i``_bvalid(x_bvalid[IDX_``i]), .s_axi_``i``_bready(x_bready[IDX_``i]), \
        .s_axi_``i``_araddr(x_araddr[32*IDX_``i +: 12]), .s_axi_``i``_arprot(x_arprot[3*IDX_``i +: 3]), \
        .s_axi_``i``_arvalid(x_arvalid[IDX_``i]), .s_axi_``i``_arready(x_arready[IDX_``i]), \
        .s_axi_``i``_rdata(x_rdata[32*IDX_``i +: 32]), .s_axi_``i``_rresp(x_rresp[2*IDX_``i +: 2]), \
        .s_axi_``i``_rvalid(x_rvalid[IDX_``i]), .s_axi_``i``_rready(x_rready[IDX_``i])
    localparam IDX_dsp = 1, IDX_cnn = 2, IDX_rep = 3;
    emg_e3c_subsys #(.REPLAY_DEPTH_LOG2(11), .REPLAY_FRAMES(`E3C_REPLAY_FRAMES), .REPLAY_FILE(`E3C_REPLAY_FILE),
        .DSP_COEF_FILE(`DSP_COEF_FILE),
        .W1_FILE(`W1_FILE), .B1_FILE(`B1_FILE), .M1_FILE(`M1_FILE), .SH1_FILE(`SH1_FILE),
        .W2_FILE(`W2_FILE), .B2_FILE(`B2_FILE), .M2_FILE(`M2_FILE), .SH2_FILE(`SH2_FILE),
        .W3_FILE(`W3_FILE), .B3_FILE(`B3_FILE), .M3_FILE(`M3_FILE), .SH3_FILE(`SH3_FILE),
        .FCW_FILE(`FCW_FILE), .FCB_FILE(`FCB_FILE)) u_e3c (
        .aclk(clk), .aresetn(aresetn),
        `XS(rep), `XS(dsp), `XS(cnn),
        .tap_rep_valid(), .tap_rep_ready(), .tap_rep_data(), .tap_rep_user(), .tap_rep_last(), .tap_rep_rst(),
        .tap_dsp_valid(), .tap_dsp_ready(), .tap_dsp_data(), .tap_dsp_user(), .tap_dsp_last(), .tap_dsp_rst(),
        .tap_cnn_rst(), .tap_res_event(),
        .dbg_valid(), .dbg_layer(), .dbg_ch(), .dbg_step(), .dbg_seg(), .dbg_acc(), .dbg_act());
`endif

    // ---------------- observation (hierarchical) ----------------
    wire        halted       = `SOC.halted;
    wire [3:0]  trap_cause   = `SOC.trap_cause;
    wire [31:0] trap_tval    = `SOC.trap_tval;
    wire        tohost_wr    = `SYS.tohost_wr;
    wire [31:0] tohost_value = `SYS.tohost_value;
    wire        dbg_valid    = `E3C.dbg_valid;
    wire [2:0]  dbg_layer    = `E3C.dbg_layer;
    wire [5:0]  dbg_ch       = `E3C.dbg_ch;
    wire [23:0] dbg_step     = `E3C.dbg_step;
    wire [7:0]  dbg_seg      = `E3C.dbg_seg;
    wire [31:0] dbg_acc      = `E3C.dbg_acc;
    wire [7:0]  dbg_act      = `E3C.dbg_act;
    wire        res_event    = `E3C.tap_res_event;
    wire        dsp_hs       = `E3C.tap_dsp_valid & `E3C.tap_dsp_ready;
    wire [127:0] dsp_data    = `E3C.tap_dsp_data;
    wire        cnn_rst      = `E3C.tap_cnn_rst;
    wire        mon_a_en     = `SOC.mbx_bram_en;
    wire [3:0]  mon_a_we     = `SOC.mbx_bram_we;
    wire [31:0] mon_a_addr   = `SOC.mbx_bram_addr;
    wire [31:0] mon_a_wdata  = `SOC.mbx_bram_wrdata;

    wire [31:0] m_awaddr = `SOC.m_axi_awaddr, m_wdata = `SOC.m_axi_wdata, m_araddr = `SOC.m_axi_araddr,
                m_rdata = `SOC.m_axi_rdata;
    wire [31:0] mon_axi_err, n_aw, n_w, n_b, n_ar, n_r, n_berr, n_rerr;
    tb_axil_monitor #(.NAME("cpu m_axi")) u_mon (.aclk(clk), .aresetn(aresetn),
        .awaddr(m_awaddr), .awprot(`SOC.m_axi_awprot), .awvalid(`SOC.m_axi_awvalid), .awready(`SOC.m_axi_awready),
        .wdata(m_wdata), .wstrb(`SOC.m_axi_wstrb), .wvalid(`SOC.m_axi_wvalid), .wready(`SOC.m_axi_wready),
        .bresp(`SOC.m_axi_bresp), .bvalid(`SOC.m_axi_bvalid), .bready(`SOC.m_axi_bready),
        .araddr(m_araddr), .arprot(`SOC.m_axi_arprot), .arvalid(`SOC.m_axi_arvalid), .arready(`SOC.m_axi_arready),
        .rdata(m_rdata), .rresp(`SOC.m_axi_rresp), .rvalid(`SOC.m_axi_rvalid), .rready(`SOC.m_axi_rready),
        .errors(mon_axi_err), .n_aw(n_aw), .n_w(n_w), .n_b(n_b), .n_ar(n_ar), .n_r(n_r), .n_berr(n_berr), .n_rerr(n_rerr));
    wire [31:0] ps_mon_err, ps_aw, ps_w, ps_b, ps_ar, ps_r, ps_berr, ps_rerr;
    tb_axil_monitor #(.NAME("ps axi")) u_psmon (.aclk(clk), .aresetn(ps_aresetn),
        .awaddr(ps_awaddr), .awprot(3'b000), .awvalid(ps_awvalid), .awready(ps_awready),
        .wdata(ps_wdata), .wstrb(ps_wstrb), .wvalid(ps_wvalid), .wready(ps_wready),
        .bresp(ps_bresp), .bvalid(ps_bvalid), .bready(ps_bready),
        .araddr(ps_araddr), .arprot(3'b000), .arvalid(ps_arvalid), .arready(ps_arready),
        .rdata(ps_rdata), .rresp(ps_rresp), .rvalid(ps_rvalid), .rready(ps_rready),
        .errors(ps_mon_err), .n_aw(ps_aw), .n_w(ps_w), .n_b(ps_b), .n_ar(ps_ar), .n_r(ps_r), .n_berr(ps_berr), .n_rerr(ps_rerr));

    // golden checks: first replay pass after the first CNN start only
    reg  golden_en = 1'b0;
    integer n_dsp_hs = 0, n_res = 0, n_cnn_rst = 0, res_errs = 0, rk;
    always @(posedge clk) if (dsp_hs) n_dsp_hs <= n_dsp_hs + 1;
    wire g_check_on;
    `include "e3c_golden.vh"
    assign g_check_on = golden_en && n_dsp_hs <= g_nframes;

    always @(posedge clk) if (golden_en && res_event) begin
        rk = n_res % g_nlog;
        if (`E3C.u_cnn.res_scores !== g_log[rk][191:0] || `E3C.u_cnn.res_argmax !== g_dec[rk][3:0] ||
            `E3C.u_cnn.res_end !== g_dec[rk][63:40]) begin
            res_errs = res_errs + 1;
            if (res_errs <= 5) $display("ERROR: core result %0d differs from golden %0d (t=%0t)", n_res, rk, $time);
        end
        n_res = n_res + 1;
    end
    reg cnn_rst_q = 1'b0;
    always @(posedge clk) begin
        cnn_rst_q <= cnn_rst;
        if (aresetn && cnn_rst && !cnn_rst_q) n_cnn_rst = n_cnn_rst + 1;
    end

    `include "e3c_timing.vh"

    // ---------------- mailbox monitor, PS accesses (window offsets), reader ----------------
    `include "mbx_monitor.vh"

    task ps_rd(input [31:0] off, output [31:0] v);
        u_ps.read(`MBX_PS_BASE + off, v);
    endtask
    task ps_wr(input [31:0] off, input [31:0] v);
        begin
            if (off < 32'h40 || off > 32'h4C) $display("ERROR: PS reader writes a CPU-owned word %h", off);
            if (off == `MBX_H_CONSUMER_INDEX) mon_ps_cons = v;
            if (off == `MBX_H_CONSUMER_EPOCH) mon_ps_epoch = v;
            u_ps.write(`MBX_PS_BASE + off, v, 4'hF);
        end
    endtask

    `include "mbx_ps_reader.vh"

    integer rd_golden = 0;
    reg [31:0] seq_base = 32'h00A0_0000;
    // +rd_expect=<file>: per decision {ts[63:32], seq[31:0]} (scripts/sim/gen_e4c.py rd_expect.hex),
    // for programs whose replay timestamp / seq do not follow one continuous replay file
    reg [63:0] rd_exp [0:999];
    reg [8*512-1:0] rd_exp_file;
    integer rd_use_exp = 0;
    initial if ($value$plusargs("rd_expect=%s", rd_exp_file)) begin
        $readmemh(rd_exp_file, rd_exp);
        rd_use_exp = 1;
    end
    task rd_content(output ok);
        integer r, p, c;
        reg [31:0] fidx, exp_seq;
        begin
            ok = 1'b1;
            if (rd_golden) begin
                r = (rw[3] - 1) % g_nlog;
                p = (rw[3] - 1) / g_nlog;
                fidx = seg_start[g_dec[r][71:64]] + g_dec[r][63:40] + p * `E3C_REPLAY_FRAMES;
                exp_seq = seq_base + fidx;
                if (rd_use_exp) begin
                    fidx = rd_exp[rw[3] - 1][63:32];
                    exp_seq = rd_exp[rw[3] - 1][31:0];
                end
                if (rw[6][15:8] != g_dec[r][3:0]) ok = 1'b0;
                for (c = 0; c < 6; c = c + 1)
                    if (rw[7 + c] != g_log[r][32 * c +: 32]) ok = 1'b0;
                if (rw[5] != fidx || rw[4] != exp_seq) ok = 1'b0;
                if (rw[13] < 100 || rw[13] > 20000) ok = 1'b0;
                if (!ok) $display("ERROR: record res=%0d: argmax %0d/%0d ts %0d/%0d seq %0d", rw[3], rw[6][15:8],
                                  g_dec[r][3:0], rw[5], fidx, rw[4]);
            end
        end
    endtask


    // ---- E4c checks (simulation only, all off by default) ----
    // +rpl_check=1 (needs +golden / +vec): every frame the replay emits == input_frames.hex[n]
    //   (lanes 0..3, lanes 4..7 = 0, flags, seq) and no frame beyond the file;
    //   +rpl_run_len=L: the replay timestamp (k) restarts every L frames (one run per segment run)
    // +rpl_swap_check=1: a replay frame-memory write only while the replay is stopped, no frame is
    //   in flight (replay -> DSP -> CNN) and every CNN result has been acknowledged
    // +mem_guard=<hex>: CPU data stores below this address (.text / .rodata end) are errors;
    //   +stack_floor=<hex>: the lowest sp (x2) value written must stay >= this (end of .bss)
    integer rpl_check = 0, rpl_run_len = 0, rpl_n = 0, rpl_errs = 0, rpl_swap_check = 0, rpl_wr = 0, rpl_swap_errs = 0;
    integer mg_on = 0, mg_bad_st = 0, mg_floor_on = 0;
    reg [31:0] mg_ro_end = 32'h0, mg_floor = 32'h0, mg_min_sp = 32'hFFFF_FFFF;
    initial begin
        if (!$value$plusargs("rpl_check=%d", rpl_check)) rpl_check = 0;
        if (!$value$plusargs("rpl_run_len=%d", rpl_run_len)) rpl_run_len = 0;
        if (!$value$plusargs("rpl_swap_check=%d", rpl_swap_check)) rpl_swap_check = 0;
        if ($value$plusargs("mem_guard=%h", mg_ro_end)) mg_on = 1;
        if ($value$plusargs("stack_floor=%h", mg_floor)) mg_floor_on = 1;
    end
    always @(posedge clk) if (rpl_check && `E3C.r_valid && `E3C.r_ready) begin
        if (rpl_n >= g_nframes ||
            `E3C.r_data[63:0] !== frames[rpl_n][63:0] || `E3C.r_data[127:64] !== 64'd0 ||
            `E3C.r_user[7:0] !== frames[rpl_n][135:128] || frames[rpl_n][143:136] !== 8'd0 ||
            `E3C.r_user[47:16] !== frames[rpl_n][175:144] ||
            (rpl_run_len > 0 && `E3C.r_user[79:48] !== rpl_n % rpl_run_len)) begin
            rpl_errs = rpl_errs + 1;
            if (rpl_errs <= 5)
                $display("ERROR: replay frame %0d differs from input_frames.hex (data %h user %h)", rpl_n,
                         `E3C.r_data[63:0], `E3C.r_user);
        end
        rpl_n = rpl_n + 1;
    end
    always @(posedge clk) if (rpl_swap_check && `E3C.u_rep.mem_we) begin
        rpl_wr = rpl_wr + 1;
        if (`E3C.u_rep.run || `E3C.r_valid || `E3C.d_valid ||
            `E3C.u_cnn.result_cnt !== `E3C.u_cnn.ack_cnt) begin
            rpl_swap_errs = rpl_swap_errs + 1;
            if (rpl_swap_errs <= 5)
                $display("ERROR: replay memory written while busy (run %b r_valid %b d_valid %b results %0d acks %0d)",
                         `E3C.u_rep.run, `E3C.r_valid, `E3C.d_valid, `E3C.u_cnn.result_cnt, `E3C.u_cnn.ack_cnt);
        end
    end
    always @(posedge clk) begin
        if (mg_on && `SOC.u_core.dmem_req_valid && `SOC.u_core.dmem_req_ready && `SOC.u_core.dmem_req_we &&
            `SOC.u_core.dmem_req_addr < mg_ro_end) begin
            mg_bad_st = mg_bad_st + 1;
            if (mg_bad_st <= 5) $display("ERROR: store into code / read-only data at %h", `SOC.u_core.dmem_req_addr);
        end
        // (crt0 clears every register first: the x2 = 0 write is not a stack pointer)
        if (`SOC.u_core.dbg_retire_valid_o && `SOC.u_core.dbg_retire_rd_we_o && `SOC.u_core.dbg_retire_rd_o == 5'd2 &&
            `SOC.u_core.dbg_retire_rd_wdata_o != 32'd0 && `SOC.u_core.dbg_retire_rd_wdata_o < mg_min_sp)
            mg_min_sp = `SOC.u_core.dbg_retire_rd_wdata_o;
    end

    // CPU (SoC) reset in the middle of a record write
    integer cpu_reset_on_rec = 0, cpu_reset_delay = 5, cpu_resets = 0;
    initial begin
        if (!$value$plusargs("cpu_reset_on_rec=%d", cpu_reset_on_rec)) cpu_reset_on_rec = 0;
        if (!$value$plusargs("cpu_reset_delay=%d", cpu_reset_delay)) cpu_reset_delay = 5;
    end
    always @(posedge clk) if (cpu_reset_on_rec > 0 && cpu_resets == 0 &&
                              mon_inval == `MBX_RING_SIZE + cpu_reset_on_rec) begin
        cpu_resets = 1;
        repeat (cpu_reset_delay) @(posedge clk);
        $display("INFO: SoC reset at t=%0t (record write %0d in progress)", $time, cpu_reset_on_rec);
        #1 aresetn = 1'b0;
        repeat (20) @(posedge clk);
        #1 aresetn = 1'b1;
    end

    integer tohost_events = 0;
    always @(posedge clk) if (aresetn && tohost_wr) tohost_events = tohost_events + 1;

    reg [8*512-1:0] hexfile, vec;
    integer max_cycles, exp_trap, exp_cause, exp_results, use_golden, n, exp_rx, exp_gaps, exp_resync, exp_epochs, w;
    integer exp_published, exp_drop, drain_max, exp_min_drop, exp_min_coll;
    reg [31:0] hv;
    reg [31:0] exp_tval;
    initial begin
        tb_init;
        if (!$value$plusargs("hex=%s", hexfile)) begin tb_fail("no +hex"); tb_finish; end
        if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 5000000;
        if (!$value$plusargs("exp_trap=%d", exp_trap)) exp_trap = 0;
        if (!$value$plusargs("exp_cause=%d", exp_cause)) exp_cause = 0;
        if (!$value$plusargs("exp_tval=%h", exp_tval)) exp_tval = 32'h0;
        if (!$value$plusargs("exp_results=%d", exp_results)) exp_results = -1;
        if (!$value$plusargs("golden=%d", use_golden)) use_golden = 0;
        if (!$value$plusargs("rd_golden=%d", rd_golden)) rd_golden = use_golden;
        if (!$value$plusargs("seq_base=%h", seq_base)) seq_base = 32'h00A0_0000;
        if (!$value$plusargs("exp_rx=%d", exp_rx)) exp_rx = -1;
        if (!$value$plusargs("exp_gaps=%d", exp_gaps)) exp_gaps = -1;
        if (!$value$plusargs("exp_resync=%d", exp_resync)) exp_resync = -1;
        if (!$value$plusargs("exp_epochs=%d", exp_epochs)) exp_epochs = 1;
        if (!$value$plusargs("exp_published=%d", exp_published)) exp_published = -1;
        if (!$value$plusargs("exp_drop=%d", exp_drop)) exp_drop = -1;
        if (!$value$plusargs("exp_min_drop=%d", exp_min_drop)) exp_min_drop = 0;
        if (!$value$plusargs("exp_min_coll=%d", exp_min_coll)) exp_min_coll = 0;
        if (use_golden || rd_golden) begin
            if (!$value$plusargs("vec=%s", vec)) begin tb_fail("+golden / +rd_golden need +vec"); tb_finish; end
            g_load(vec, use_golden);
            golden_en = use_golden;
        end
        $readmemh(hexfile, `SOC.u_ram.mem);
        repeat (10) @(posedge clk);
        #1 begin aresetn = 1'b1; ps_aresetn = 1'b1; end
        n = 0;
        while (tohost_events == 0 && !halted && n < max_cycles) begin @(posedge clk); n = n + 1; end
        // let the consumer drain: two polls in a row with nothing new
        if (rd_mode) begin
            w = 0;
            rd_idle_polls = 0;
            if (!$value$plusargs("drain_max=%d", drain_max)) drain_max = 2000000;
            while (rd_idle_polls < 2 && w < drain_max) begin @(posedge clk); w = w + 1; end
            if (rd_idle_polls < 2) $display("ERROR: consumer did not drain within %0d cycles", drain_max);
            rd_stop = 1'b1;
            while (rd_running) @(posedge clk);
        end
        repeat (200) @(posedge clk);
        if (exp_trap) begin
            tb_check("program ended in a trap", halted, 1'b1);
            tb_check("trap cause", trap_cause, exp_cause);
            tb_check("trap tval", trap_tval, exp_tval);
            tb_check("no TOHOST write", tohost_events, 0);
        end else begin
            tb_check("no trap", halted, 1'b0);
            if (halted) $display("ERROR: trap cause=%0d pc=%h tval=%h", trap_cause, `SOC.trap_pc, trap_tval);
            tb_check("TOHOST written once", tohost_events, 1);
            tb_check("TOHOST = 1 (PASS)", tohost_value, 32'd1);
            if (tohost_value != 1 && tohost_value[0])
                $display("ERROR: firmware reported failing check #%0d", tohost_value >> 1);
            if (!mon_off) begin
                tb_check("mailbox monitor errors", mon_err, 0);
                tb_check("producer epochs", mon_epochs, exp_epochs);
            end
            if (rd_mode) rd_report(exp_rx, exp_gaps, exp_resync);
            if (exp_published >= 0) begin
                ps_rd(`MBX_H_PUBLISHED_CNT, hv);
                tb_check("header PUBLISHED_CNT", hv, exp_published);
            end
            if (exp_drop >= 0) begin
                ps_rd(`MBX_H_DROP_CNT, hv);
                tb_check("header DROP_CNT", hv, exp_drop);
            end
            if (exp_min_drop > 0) begin
                ps_rd(`MBX_H_DROP_CNT, hv);
                tb_check("header DROP_CNT >= exp_min_drop", hv >= exp_min_drop, 1'b1);
            end
        end
        if (use_golden) begin
            if (exp_results >= 0) tb_check("core results", n_res, exp_results);
            tb_check("core results == golden", res_errs, 0);
            tb_check("golden mismatches (DSP frames, layers)", g_errs, 0);
            tb_check("DSP frames compared (first pass)", pd, g_ndsp);
            tb_check("FC vectors compared (first pass)", pf, g_nlog);
        end
        if (rpl_check) begin
            tb_check("replay frames == input_frames.hex", rpl_errs, 0);
            tb_check("replay frames emitted == input frames", rpl_n, g_nframes);
        end
        if (rpl_swap_check) begin
            tb_check("replay memory writes only while idle and all results acknowledged", rpl_swap_errs, 0);
            $display("INFO: replay frame-memory writes %0d", rpl_wr);
        end
        if (mg_on) tb_check("CPU stores into code / read-only data", mg_bad_st, 0);
        $display("STACK: min_sp=%h used=%0d bytes", mg_min_sp, 32'h0001_0000 - mg_min_sp);
        if (mg_floor_on) tb_check("stack stays above .bss", mg_min_sp >= mg_floor, 1'b1);
        if (tm_period > 0) tm_report((exp_results >= 0) ? exp_results : 11);
        tb_check("AXI protocol errors (cpu m_axi)", mon_axi_err, 0);
        tb_check("AXI error responses (cpu m_axi)", n_berr + n_rerr, 0);
        tb_check("AXI protocol errors (ps axi)", ps_mon_err, 0);
        tb_check("AXI error responses (ps axi)", ps_berr + ps_rerr, 0);
`ifndef MBX_BD
        tb_check("mailbox BRAM write/write collisions", u_mem.n_ww_coll, 0);
        if (exp_min_coll > 0) tb_check("read/write collisions >= exp_min_coll", u_mem.n_rw_coll >= exp_min_coll, 1'b1);
        $display("MBX_BRAM: read/write collisions=%0d", u_mem.n_rw_coll);
`endif
        $display("MBX_MONITOR: published=%0d invalidations=%0d epochs=%0d", mon_pub, mon_inval, mon_epochs);
        $display("PERF: cycles=%0d axi aw=%0d ar=%0d results=%0d mbx_pub=%0d rd_rx=%0d", n, n_aw, n_ar, n_res, mon_pub, rd_rx);
        tb_finish;
    end
endmodule

`default_nettype wire
