`timescale 1ns / 1ps
`default_nettype none

// tb_cmi2axil - self-checking unit test for rtl/soc/cmi2axil.v
//
// CMI master task -> cmi2axil -> tb_axil_slave_bfm, with a CMI monitor on the
// CMI side and an AXI4-Lite protocol monitor on the AXI side. Checks:
//   - every write = exactly one AW, one W, one B; every read = one AR, one R
//   - write data/strobes land in the BFM memory, reads return it
//   - CMI response exactly one cycle after the B/R handshake, error mapping
//     OKAY -> 0, SLVERR/DECERR/EXOKAY -> 1
//   - AW/W order modes (random, AW first, W first, both together), independent
//     back-pressure on every channel, B/R latencies, long waits
//   - reset during each phase: no VALID in reset, no extra AXI transaction,
//     no stale CMI response, adapter usable afterwards

module tb_cmi2axil;

    localparam TB_TIMEOUT_NS = 200000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;
    wire aresetn = ~rst;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_cmi2axil); end
`endif

    reg         c_valid = 1'b0, c_we = 1'b0;
    reg  [31:0] c_addr = 32'd0, c_wdata = 32'd0;
    reg  [3:0]  c_wstrb = 4'd0;
    wire        c_ready, c_rsp_valid, c_rsp_err;
    wire [31:0] c_rsp_rdata;

    wire [31:0] awaddr, wdata, araddr, rdata;
    wire [2:0]  awprot, arprot;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;

    cmi2axil dut (
        .clk(clk), .rst(rst),
        .req_valid(c_valid), .req_ready(c_ready), .req_addr(c_addr), .req_we(c_we),
        .req_wstrb(c_wstrb), .req_wdata(c_wdata),
        .rsp_valid(c_rsp_valid), .rsp_rdata(c_rsp_rdata), .rsp_err(c_rsp_err),
        .m_axi_awaddr(awaddr), .m_axi_awprot(awprot), .m_axi_awvalid(awvalid), .m_axi_awready(awready),
        .m_axi_wdata(wdata), .m_axi_wstrb(wstrb), .m_axi_wvalid(wvalid), .m_axi_wready(wready),
        .m_axi_bresp(bresp), .m_axi_bvalid(bvalid), .m_axi_bready(bready),
        .m_axi_araddr(araddr), .m_axi_arprot(arprot), .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rvalid(rvalid), .m_axi_rready(rready)
    );

    tb_axil_slave_bfm #(.WORDS(1024)) u_bfm (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awprot(awprot), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arprot(arprot), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready)
    );

    wire [31:0] ax_err, n_aw, n_w, n_b, n_ar, n_r, n_berr, n_rerr;
    tb_axil_monitor #(.NAME("cmi2axil"), .SINGLE(1)) u_axmon (
        .aclk(clk), .aresetn(aresetn),
        .awaddr(awaddr), .awprot(awprot), .awvalid(awvalid), .awready(awready),
        .wdata(wdata), .wstrb(wstrb), .wvalid(wvalid), .wready(wready),
        .bresp(bresp), .bvalid(bvalid), .bready(bready),
        .araddr(araddr), .arprot(arprot), .arvalid(arvalid), .arready(arready),
        .rdata(rdata), .rresp(rresp), .rvalid(rvalid), .rready(rready),
        .errors(ax_err), .n_aw(n_aw), .n_w(n_w), .n_b(n_b), .n_ar(n_ar), .n_r(n_r),
        .n_berr(n_berr), .n_rerr(n_rerr));

    wire [31:0] cm_err, cm0, cm1, cm2, cm3;
    wire        cm_out;
    tb_cmi_monitor #(.NAME("cmi2axil CMI")) u_cmon (
        .clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(c_valid), .req_ready(c_ready), .req_addr(c_addr), .req_we(c_we),
        .req_wstrb(c_wstrb), .req_wdata(c_wdata), .rsp_valid(c_rsp_valid), .rsp_err(c_rsp_err),
        .errors(cm_err), .n_handshakes(cm0), .n_write_handshakes(cm1), .n_responses(cm2),
        .n_error_responses(cm3), .outstanding(cm_out));

    // CMI response must come exactly one cycle after the B or R handshake
    reg axi_done_d = 1'b0;
    always @(posedge clk) begin
        if (rst) axi_done_d <= 1'b0;
        else begin
            if (c_rsp_valid !== axi_done_d)
                tb_fail("CMI response not exactly one cycle after the B/R handshake");
            axi_done_d <= (bvalid & bready) | (rvalid & rready);
        end
    end

    // observed AW/W handshake order per write (coverage of the independent channels)
    integer n_aw_first = 0, n_w_first = 0, n_same = 0;
    reg     seen_aw = 1'b0, seen_w = 1'b0;
    always @(posedge clk) begin
        if (rst) begin seen_aw <= 1'b0; seen_w <= 1'b0; end
        else begin
            if (awvalid && awready && wvalid && wready && !seen_aw && !seen_w) n_same = n_same + 1;
            else if (awvalid && awready && !seen_w) n_aw_first = n_aw_first + 1;
            else if (wvalid && wready && !seen_aw) n_w_first = n_w_first + 1;
            if (awvalid && awready) seen_aw <= 1'b1;
            if (wvalid && wready)   seen_w  <= 1'b1;
            if (bvalid && bready) begin seen_aw <= 1'b0; seen_w <= 1'b0; end
        end
    end

    // ---------------- model ----------------
    reg [31:0] model [0:1023];
    reg [8*64-1:0] msg;
    reg [31:0] rd;
    reg        er;
    integer    aw0, w0, b0, ar0, r0;

    task cmi;
        input [31:0] addr; input we; input [3:0] strb; input [31:0] wd;
        integer t;
        begin
            aw0 = n_aw; w0 = n_w; b0 = n_b; ar0 = n_ar; r0 = n_r;
            @(negedge clk);
            c_valid = 1'b1; c_addr = addr; c_we = we; c_wstrb = we ? strb : 4'd0; c_wdata = wd;
            @(posedge clk);
            t = 0;
            while (!c_ready && t < 10000) begin @(posedge clk); t = t + 1; end
            #1;
            c_valid = 1'b0; c_we = 1'b0; c_wstrb = 4'd0;
            t = 0;
            while (!c_rsp_valid && t < 100000) begin @(posedge clk); #1; t = t + 1; end
            if (!c_rsp_valid) tb_fail("timeout waiting for the CMI response");
            rd = c_rsp_rdata; er = c_rsp_err;
            @(posedge clk); #1;
            if (we) begin
                tb_check("write: exactly one AW", n_aw - aw0, 1);
                tb_check("write: exactly one W", n_w - w0, 1);
                tb_check("write: exactly one B", n_b - b0, 1);
                tb_check("write: no AR/R", (n_ar - ar0) + (n_r - r0), 0);
            end else begin
                tb_check("read: exactly one AR", n_ar - ar0, 1);
                tb_check("read: exactly one R", n_r - r0, 1);
                tb_check("read: no AW/W/B", (n_aw - aw0) + (n_w - w0) + (n_b - b0), 0);
            end
        end
    endtask

    function [1:0] exp_resp;
        input [31:0] a;
        begin
            if (a >= 32'h1000_0F00 && a <= 32'h1000_0F3F)      exp_resp = 2'b11;  // DECERR window
            else if (a >= 32'h1000_0E00 && a <= 32'h1000_0E3F) exp_resp = 2'b10;  // SLVERR window
            else if (a >= 32'h1000_0D00 && a <= 32'h1000_0D3F) exp_resp = 2'b01;  // EXOKAY window
            else                                                exp_resp = 2'b00;
        end
    endfunction

    task write_check;
        input [31:0] addr; input [3:0] strb; input [31:0] wd;
        integer k;
        begin
            cmi(addr, 1'b1, strb, wd);
            $sformat(msg, "write %h strb %b: rsp_err", addr, strb);
            tb_check(msg, er, exp_resp(addr) != 2'b00);
            if (exp_resp(addr) == 2'b00)
                for (k = 0; k < 4; k = k + 1)
                    if (strb[k]) model[addr[11:2]][8*k +: 8] = wd[8*k +: 8];
            $sformat(msg, "write %h: AWADDR word aligned, AWPROT 0", addr);
            tb_check(msg, {u_bfm.aw_a[1:0]}, 2'b00);
        end
    endtask

    task read_check;
        input [31:0] addr;
        begin
            cmi(addr, 1'b0, 4'd0, 32'd0);
            $sformat(msg, "read %h: rsp_err", addr);
            tb_check(msg, er, exp_resp(addr) != 2'b00);
            if (exp_resp(addr) == 2'b00) begin
                $sformat(msg, "read %h: data", addr);
                tb_check(msg, rd, model[addr[11:2]]);
            end
        end
    endtask

    task random_traffic;
        input integer n;
        integer i;
        reg [31:0] a;
        begin
            for (i = 0; i < n; i = i + 1) begin
                a = 32'h1000_0000 | ($random(tb_seed) & 32'h0000_0FFC) | ($random(tb_seed) & 3);
                if ($random(tb_seed) & 1) write_check(a, ({$random(tb_seed)} % 15) + 1, $random(tb_seed));
                else read_check(a);
            end
        end
    endtask

    // Reset while the adapter is in a given phase; afterwards nothing may happen.
    integer phase, aw1, w1, ar1, guard;
    task reset_in_phase;
        input integer which;        // 0 AW wait, 1 W wait, 2 B wait, 3 AR wait, 4 R wait
        begin
            case (which)
                0: u_bfm.configure(0, 100, 100, 2, 0, 0, 0, 0, tb_seed);     // W first; AW never ready
                1: u_bfm.configure(100, 0, 100, 1, 0, 0, 0, 0, tb_seed);     // AW first; W never ready
                2: u_bfm.configure(100, 100, 100, 0, 60, 60, 0, 0, tb_seed); // B after 60 cycles
                3: u_bfm.configure(100, 100, 0, 0, 0, 0, 0, 0, tb_seed);     // AR never ready
                default: u_bfm.configure(100, 100, 100, 0, 0, 0, 60, 60, tb_seed); // R after 60 cycles
            endcase
            @(negedge clk);
            c_valid = 1'b1; c_addr = 32'h1000_0100; c_we = (which <= 2); c_wstrb = (which <= 2) ? 4'hF : 4'd0;
            c_wdata = 32'h5EB7_1E11;
            @(posedge clk); #1;            // adapter is IDLE: accepted now
            c_valid = 1'b0; c_we = 1'b0; c_wstrb = 4'd0;
            guard = 0;
            case (which)
                0: while (!(wvalid === 1'b0 && awvalid === 1'b1) && guard < 50) begin @(posedge clk); #1; guard = guard + 1; end
                1: while (!(awvalid === 1'b0 && wvalid === 1'b1) && guard < 50) begin @(posedge clk); #1; guard = guard + 1; end
                2: while (!(bready === 1'b1) && guard < 50) begin @(posedge clk); #1; guard = guard + 1; end
                3: repeat (5) @(posedge clk);
                default: while (!(rready === 1'b1) && guard < 50) begin @(posedge clk); #1; guard = guard + 1; end
            endcase
            #1;
            $sformat(msg, "reset phase %0d reached", which);
            tb_check(msg, guard < 50, 1'b1);
            aw1 = n_aw; w1 = n_w; ar1 = n_ar;
            @(negedge clk); rst = 1'b1;
            repeat (3) @(negedge clk); rst = 1'b0;
            u_bfm.configure(100, 100, 100, 0, 0, 2, 0, 2, tb_seed);
            repeat (20) begin
                @(posedge clk); #1;
                if (c_rsp_valid) tb_fail("stale CMI response after reset");
                if (awvalid || wvalid || arvalid) tb_fail("AXI request after reset without a CMI request");
            end
            $sformat(msg, "reset phase %0d: no new AXI handshakes after reset", which);
            tb_check(msg, n_aw + n_w + n_ar, 0);          // monitor counters restart at 0 in reset
            read_check(32'h1000_0004);                     // usable again
        end
    endtask

    integer m, j, s;

    initial begin
        tb_init;
        for (j = 0; j < 1024; j = j + 1) begin model[j] = 32'h0; u_bfm.mem[j] = 32'h0; end
        u_bfm.set_errors(32'h1000_0E00, 32'h1000_0E3F, 32'h1000_0F00, 32'h1000_0F3F,
                         32'h1000_0D00, 32'h1000_0D3F);
        repeat (3) @(negedge clk);
        rst = 1'b0;
        repeat (2) @(negedge clk);

        // 1. zero-wait slave: basic writes/reads, every non-zero strobe
        u_bfm.configure(100, 100, 100, 0, 0, 0, 0, 0, tb_seed);
        for (s = 1; s < 16; s = s + 1) begin
            write_check(32'h1000_0000 + 4 * s, 4'hF, 32'h0);
            write_check(32'h1000_0000 + 4 * s + (s & 3), s[3:0], 32'hDDCC_BBAA);
            read_check(32'h1000_0000 + 4 * s);
        end
        // 2. error responses
        write_check(32'h1000_0E04, 4'hF, 32'h1); read_check(32'h1000_0E04);    // SLVERR
        write_check(32'h1000_0F08, 4'hF, 32'h2); read_check(32'h1000_0F08);    // DECERR
        write_check(32'h1000_0D0C, 4'hF, 32'h3); read_check(32'h1000_0D0C);    // EXOKAY
        read_check(32'h1000_0000);                                              // OKAY again

        // 3. order modes x random back-pressure x latencies
        for (m = 0; m < 4; m = m + 1) begin
            u_bfm.configure(100, 100, 100, m, 0, 0, 0, 0, tb_seed + m);  random_traffic(150);
            u_bfm.configure(40, 60, 50, m, 0, 3, 0, 3, tb_seed + m);     random_traffic(300);
            u_bfm.configure(15, 15, 15, m, 0, 8, 0, 8, tb_seed + m);     random_traffic(300);
        end
        // 4. one channel very slow at a time
        u_bfm.configure(5, 100, 100, 0, 0, 0, 0, 0, tb_seed);  random_traffic(60);   // AWREADY slow
        u_bfm.configure(100, 5, 100, 0, 0, 0, 0, 0, tb_seed);  random_traffic(60);   // WREADY slow
        u_bfm.configure(100, 100, 5, 0, 0, 0, 0, 0, tb_seed);  random_traffic(60);   // ARREADY slow
        u_bfm.configure(100, 100, 100, 0, 20, 30, 0, 0, tb_seed); random_traffic(60); // BVALID late
        u_bfm.configure(100, 100, 100, 0, 0, 0, 20, 30, tb_seed); random_traffic(60); // RVALID late
        // 5. long waits (no timeout in the adapter)
        u_bfm.configure(100, 100, 100, 0, 400, 400, 400, 400, tb_seed);
        write_check(32'h1000_0040, 4'hF, 32'hCAFE_F00D);
        read_check(32'h1000_0040);
        // 6. back-to-back transactions, zero wait
        u_bfm.configure(100, 100, 100, 0, 0, 0, 0, 0, tb_seed);
        random_traffic(500);

        // 7. reset in every phase
        for (phase = 0; phase < 5; phase = phase + 1)
            reset_in_phase(phase);

        $display("INFO: AW/W order: AW first %0d, W first %0d, same cycle %0d", n_aw_first, n_w_first, n_same);
        tb_check("coverage: AW handshake before W seen", n_aw_first > 0, 1'b1);
        tb_check("coverage: W handshake before AW seen", n_w_first > 0, 1'b1);
        tb_check("coverage: AW and W in the same cycle seen", n_same > 0, 1'b1);
        tb_check("AXI protocol monitor violations", ax_err, 0);
        tb_check("CMI monitor violations", cm_err, 0);
        tb_finish;
    end

endmodule

`default_nettype wire
