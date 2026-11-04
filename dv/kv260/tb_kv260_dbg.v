`timescale 1ns / 1ps
`default_nettype none

// tb_kv260_dbg - stage 6A board wrapper check (Icarus): kv260_cpu_dbg (the
// unchanged rv32_soc_top + ILA packing) -> soc_sysctrl directly (no
// SmartConnect), kv260_dbg_obs counters. The program runs twice with a CPU
// reset in between (same as the VIO reset on the board; RAM is not reloaded).
//
// Plusargs: +hex=<image> +exp_retired=N (ISS instruction count incl. the
//           TOHOST store; 0 = do not check) +max_cycles=N
// Checks per run: TOHOST value 1 written exactly once, no trap, obs counters
// (tohost_retired = exp_retired - 1, reset_releases, run_cycles equal in both
// runs), and every cycle: the packed ILA buses equal the signals they carry.

module tb_kv260_dbg;
    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst_n = 1'b0;

    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    wire        halted, retire_valid, tohost_wr, tohost_valid;
    wire [3:0]  trap_cause;
    wire [31:0] trap_pc, trap_tval, imem_addr, tohost_value;
    wire [7:0]  led;
    wire [63:0]  ila_ctrl;
    wire [159:0] ila_cpu;
    wire [95:0]  ila_trap;
    wire [127:0] ila_axi;

    reg [8*256-1:0] hexfile;
    initial if (!$value$plusargs("hex=%s", hexfile)) hexfile = "";

    kv260_cpu_dbg #(.INIT_FILE("")) dut (
        .aclk(clk), .aresetn(rst_n),
        .m_axi_awaddr(awaddr), .m_axi_awprot(awprot), .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_araddr(araddr), .m_axi_arprot(arprot), .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rvalid(rvalid), .m_axi_rready(rready),
        .mbx_bram_clk(), .mbx_bram_rst(), .mbx_bram_en(), .mbx_bram_we(), .mbx_bram_addr(),
        .mbx_bram_wrdata(), .mbx_bram_rddata(32'h0),
        .halted(halted), .trap_cause(trap_cause), .trap_pc(trap_pc), .trap_tval(trap_tval),
        .dbg_retire_valid(retire_valid), .dbg_imem_req_addr(imem_addr),
        .tohost_wr(tohost_wr), .tohost_value(tohost_value), .pl_resetn0(1'b1),
        .ila_ctrl(ila_ctrl), .ila_cpu(ila_cpu), .ila_trap(ila_trap), .ila_axi(ila_axi));

    soc_sysctrl sys (
        .aclk(clk), .aresetn(rst_n),
        .s_axi_awaddr(awaddr[11:0]), .s_axi_awprot(awprot), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr[11:0]), .s_axi_arprot(arprot), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .led(led), .tohost_valid(tohost_valid), .tohost_value(tohost_value), .tohost_wr(tohost_wr));

    wire        ext_reset_n, dcm_locked;
    wire [31:0] run_cycles, retired, tohost_retired, first_tohost, free_cycles;
    wire [7:0]  tohost_writes;
    wire [15:0] reset_releases;
    kv260_dbg_obs obs (
        .aclk(clk), .rst_n(rst_n), .pl_resetn0(1'b0), .ps_rst_override(1'b1),
        .ext_reset_n(ext_reset_n), .dcm_locked(dcm_locked),
        .retire_valid(retire_valid), .tohost_wr(tohost_wr), .tohost_value(tohost_value),
        .run_cycles(run_cycles), .retired(retired), .tohost_retired(tohost_retired),
        .tohost_writes(tohost_writes), .first_tohost(first_tohost), .reset_releases(reset_releases),
        .free_cycles(free_cycles));

    // ---- packing check, every cycle ----
    `define CORE dut.u_soc.u_core
    `define SOC  dut.u_soc
    integer pack_err = 0;
    always @(posedge clk) begin
        if (ila_ctrl[0] !== `SOC.dbg_retire_valid || ila_ctrl[1] !== `SOC.dbg_retire_rd_we ||
            ila_ctrl[2] !== `SOC.dbg_trap_valid || ila_ctrl[3] !== `SOC.dbg_dmem_req_valid ||
            ila_ctrl[4] !== `SOC.dbg_dmem_req_ready || ila_ctrl[5] !== `SOC.dbg_dmem_req_we ||
            ila_ctrl[6] !== `SOC.dbg_dmem_rsp_valid || ila_ctrl[7] !== `SOC.dbg_dmem_rsp_err ||
            ila_ctrl[8] !== halted || ila_ctrl[9] !== tohost_wr || ila_ctrl[10] !== rst_n ||
            ila_ctrl[11] !== 1'b1 || ila_ctrl[14] !== `SOC.dbg_imem_req_valid ||
            ila_ctrl[15] !== `SOC.dbg_imem_req_ready || ila_ctrl[19:16] !== `SOC.dbg_dmem_req_wstrb ||
            ila_ctrl[23:20] !== trap_cause || ila_ctrl[28:24] !== `SOC.dbg_retire_rd ||
            ila_ctrl[41:32] !== {rready, rvalid, arready, arvalid, bready, bvalid, wready, wvalid, awready, awvalid} ||
            ila_ctrl[43:42] !== bresp || ila_ctrl[45:44] !== rresp || ila_ctrl[49:46] !== wstrb ||
            ila_ctrl[13] !== ((awvalid & awready) | (wvalid & wready) | (bvalid & bready) |
                              (arvalid & arready) | (rvalid & rready)) ||
            ila_cpu !== {`SOC.dbg_dmem_req_wdata, `SOC.dbg_dmem_req_addr, `SOC.dbg_retire_rd_wdata,
                         `SOC.dbg_retire_insn, `SOC.dbg_retire_pc} ||
            ila_trap !== {tohost_value, trap_tval, trap_pc} ||
            ila_axi !== {rdata, araddr, wdata, awaddr})
            pack_err = pack_err + 1;
    end

    integer exp_retired, max_cycles, n, run, cyc0;
    reg [8*64-1:0] msg;

    // +ila_csv=<file>: write the samples an ILA trace capture would store in
    // run 2 (storage qualifier ila_ctrl[12]) in the Hardware Manager CSV
    // layout, to test scripts/fpga/kv260_6a.py without the board
    integer csv_fd = 0, csv_n = 0;
    reg [8*256-1:0] csv_name;
    initial if ($value$plusargs("ila_csv=%s", csv_name)) begin
        csv_fd = $fopen(csv_name, "w");
        $fdisplay(csv_fd, "Sample in Buffer,Sample in Window,TRIGGER,kv260_bd_i/cpu_subsys_ila_ctrl[63:0],kv260_bd_i/obs_retired[31:0],kv260_bd_i/cpu_subsys_ila_cpu[159:0],kv260_bd_i/cpu_subsys_ila_trap[95:0],kv260_bd_i/cpu_subsys_ila_axi[127:0]");
        $fdisplay(csv_fd, "Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,HEX,HEX,HEX");
    end
    always @(posedge clk)
        if (csv_fd != 0 && run == 2 && rst_n && ila_ctrl[12]) begin
            $fdisplay(csv_fd, "%0d,%0d,%0d,%h,%h,%h,%h,%h", csv_n, csv_n, csv_n == 0, ila_ctrl, retired,
                      ila_cpu, ila_trap, ila_axi);
            csv_n = csv_n + 1;
        end

    initial begin
        tb_init;
        if (!$value$plusargs("exp_retired=%d", exp_retired)) exp_retired = 0;
        if (!$value$plusargs("max_cycles=%d", max_cycles)) max_cycles = 200000;
        // load the image into the RAM model (same file the bitstream uses as INIT_FILE)
        $readmemh(hexfile, `SOC.u_ram.mem);
        cyc0 = -1;
        for (run = 1; run <= 2; run = run + 1) begin
            rst_n = 1'b0;
            repeat (20) @(posedge clk);
            #1 rst_n = 1'b1;
            n = 0;
            while (tohost_writes == 0 && !halted && n < max_cycles) begin @(posedge clk); n = n + 1; end
            repeat (200) @(posedge clk);
            $sformat(msg, "run %0d: no trap", run);           tb_check(msg, halted, 1'b0);
            $sformat(msg, "run %0d: first TOHOST == 1", run);  tb_check(msg, first_tohost, 32'd1);
            $sformat(msg, "run %0d: one TOHOST write", run);   tb_check(msg, tohost_writes, 8'd1);
            $sformat(msg, "run %0d: reset_releases", run);     tb_check(msg, reset_releases, run);
            if (exp_retired > 0) begin
                $sformat(msg, "run %0d: tohost_retired", run); tb_check(msg, tohost_retired, exp_retired - 1);
            end
            if (cyc0 < 0) cyc0 = run_cycles;
            else begin
                $sformat(msg, "run %0d: same run_cycles", run); tb_check(msg, run_cycles, cyc0);
            end
            $display("PERF: run %0d run_cycles=%0d tohost_retired=%0d", run, run_cycles, tohost_retired);
        end
        tb_check("ILA packing mismatches", pack_err, 0);
        tb_check("obs ext_reset_n = pl_resetn0 | override", ext_reset_n, 1'b1);
        tb_check("obs free_cycles running", free_cycles > 100, 1'b1);
        tb_finish;
    end
endmodule

`default_nettype wire
