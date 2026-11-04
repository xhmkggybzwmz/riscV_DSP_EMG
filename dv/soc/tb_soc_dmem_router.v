`timescale 1ns / 1ps
`default_nettype none

// tb_soc_dmem_router - self-checking unit test for rtl/soc/soc_dmem_router.v
//
// A CMI master task drives the router; two tb_cmi_slave models stand for the
// RAM port and the MMIO path (different TAGs in their read data). CMI monitors
// watch all three interfaces. Checks:
//   - address decode with the production windows (RAM 0x0000_0000-0x0000_FFFF,
//     SYSCTRL 0x1000_0000-0x1000_0FFF): each request reaches exactly one target
//     (or the error responder), with unchanged fields; stores are forwarded
//     exactly once; boundary addresses of every window and its neighbours
//   - a rejected (unmapped) request causes no valid cycle at all on the RAM or
//     MMIO side, and exactly one error response
//   - a second instance with three MMIO windows checks the generic decode
//     against a reference function (random and boundary addresses)
//   - the response comes from the selected target (TAG), with its error flag;
//     error-region requests get rsp_err one cycle after acceptance
//   - random ready/latency/error behaviour of the targets
//   - a spurious response from the non-selected target is ignored
//   - reset with an outstanding MMIO request: no stale response afterwards

module tb_soc_dmem_router;

    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_soc_dmem_router); end
`endif

    reg         m_valid = 1'b0, m_we = 1'b0;
    reg  [31:0] m_addr = 32'd0, m_wdata = 32'd0;
    reg  [3:0]  m_wstrb = 4'd0;
    wire        m_ready, m_rsp_valid, m_rsp_err;
    wire [31:0] m_rsp_rdata;

    wire        r_valid, r_ready, r_we, r_rsp_valid, r_rsp_err;
    wire [31:0] r_addr, r_wdata, r_rsp_rdata;
    wire [3:0]  r_wstrb;
    wire        i_valid, i_ready, i_we, i_rsp_valid, i_rsp_err;
    wire [31:0] i_addr, i_wdata, i_rsp_rdata;
    wire [3:0]  i_wstrb;

    // production windows (defaults = soc_addr_map.vh)
    soc_dmem_router #(.RAM_BYTES(32'h0001_0000), .IO_WINDOWS(1),
                      .IO_BASE(32'h1000_0000), .IO_SIZE(32'h0000_1000)) dut (
        .clk(clk), .rst(rst),
        .s_req_valid(m_valid), .s_req_ready(m_ready), .s_req_addr(m_addr), .s_req_we(m_we),
        .s_req_wstrb(m_wstrb), .s_req_wdata(m_wdata),
        .s_rsp_valid(m_rsp_valid), .s_rsp_rdata(m_rsp_rdata), .s_rsp_err(m_rsp_err),
        .ram_req_valid(r_valid), .ram_req_ready(r_ready), .ram_req_addr(r_addr), .ram_req_we(r_we),
        .ram_req_wstrb(r_wstrb), .ram_req_wdata(r_wdata),
        .ram_rsp_valid(r_rsp_valid), .ram_rsp_rdata(r_rsp_rdata), .ram_rsp_err(r_rsp_err),
        .io_req_valid(i_valid), .io_req_ready(i_ready), .io_req_addr(i_addr), .io_req_we(i_we),
        .io_req_wstrb(i_wstrb), .io_req_wdata(i_wdata),
        .io_rsp_valid(i_rsp_valid), .io_rsp_rdata(i_rsp_rdata), .io_rsp_err(i_rsp_err)
    );

    tb_cmi_slave #(.TAG(8'hA0)) u_ram (
        .clk(clk), .rst(rst), .req_valid(r_valid), .req_ready(r_ready), .req_addr(r_addr),
        .req_we(r_we), .req_wstrb(r_wstrb), .req_wdata(r_wdata),
        .rsp_valid(r_rsp_valid), .rsp_rdata(r_rsp_rdata), .rsp_err(r_rsp_err));
    tb_cmi_slave #(.TAG(8'hB0)) u_io (
        .clk(clk), .rst(rst), .req_valid(i_valid), .req_ready(i_ready), .req_addr(i_addr),
        .req_we(i_we), .req_wstrb(i_wstrb), .req_wdata(i_wdata),
        .rsp_valid(i_rsp_valid), .rsp_rdata(i_rsp_rdata), .rsp_err(i_rsp_err));

    // decode-only instance with three windows (SYSCTRL, a future SPI page, and a
    // 64 KB window in the shared-buffer region) and a 32 KB RAM; targets always ready
    reg  [31:0] d2_addr = 32'd0;
    reg         d2_valid = 1'b0;
    wire        d2_ram_valid, d2_io_valid, d2_ready, d2_rsp_valid, d2_rsp_err;
    wire [31:0] d2_rsp_rdata, d2_r_addr, d2_i_addr, d2_r_wdata, d2_i_wdata;
    wire        d2_r_we, d2_i_we;
    wire [3:0]  d2_r_wstrb, d2_i_wstrb;
    soc_dmem_router #(.RAM_BYTES(32'h0000_8000), .IO_WINDOWS(3),
                      .IO_BASE({32'h2000_0000, 32'h1000_1000, 32'h1000_0000}),
                      .IO_SIZE({32'h0001_0000, 32'h0000_1000, 32'h0000_1000})) dut2 (
        .clk(clk), .rst(rst),
        .s_req_valid(d2_valid), .s_req_ready(d2_ready), .s_req_addr(d2_addr), .s_req_we(1'b0),
        .s_req_wstrb(4'd0), .s_req_wdata(32'd0),
        .s_rsp_valid(d2_rsp_valid), .s_rsp_rdata(d2_rsp_rdata), .s_rsp_err(d2_rsp_err),
        .ram_req_valid(d2_ram_valid), .ram_req_ready(1'b1), .ram_req_addr(d2_r_addr), .ram_req_we(d2_r_we),
        .ram_req_wstrb(d2_r_wstrb), .ram_req_wdata(d2_r_wdata),
        .ram_rsp_valid(1'b0), .ram_rsp_rdata(32'd0), .ram_rsp_err(1'b0),
        .io_req_valid(d2_io_valid), .io_req_ready(1'b1), .io_req_addr(d2_i_addr), .io_req_we(d2_i_we),
        .io_req_wstrb(d2_i_wstrb), .io_req_wdata(d2_i_wdata),
        .io_rsp_valid(1'b0), .io_rsp_rdata(32'd0), .io_rsp_err(1'b0));

    function [1:0] ref3;                     // dut2: 1 ram, 2 io, 3 err
        input [31:0] a;
        begin
            if (a < 32'h0000_8000) ref3 = 2'd1;
            else if ((a >= 32'h1000_0000 && a <= 32'h1000_1FFF) ||
                     (a >= 32'h2000_0000 && a <= 32'h2000_FFFF)) ref3 = 2'd2;
            else ref3 = 2'd3;
        end
    endfunction

    function [1:0] ref1;                     // production windows: 1 ram, 2 io, 3 err
        input [31:0] a;
        begin
            if (a <= 32'h0000_FFFF) ref1 = 2'd1;
            else if (a >= 32'h1000_0000 && a <= 32'h1000_0FFF) ref1 = 2'd2;
            else ref1 = 2'd3;
        end
    endfunction

    // valid cycles seen by the targets (a rejected request must add none)
    integer n_rv = 0, n_iv = 0;
    always @(posedge clk) begin
        if (r_valid) n_rv = n_rv + 1;
        if (i_valid) n_iv = n_iv + 1;
    end

    reg spurious_window = 1'b0;     // masks the deliberate spurious RAM response from its monitor
    wire [31:0] e_m, e_r, e_i, d0, d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11;
    wire        o_m, o_r, o_i;
    tb_cmi_monitor #(.NAME("router s")) u_mon_m (.clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(m_valid), .req_ready(m_ready), .req_addr(m_addr), .req_we(m_we), .req_wstrb(m_wstrb),
        .req_wdata(m_wdata), .rsp_valid(m_rsp_valid), .rsp_err(m_rsp_err), .errors(e_m),
        .n_handshakes(d0), .n_write_handshakes(d1), .n_responses(d2), .n_error_responses(d3), .outstanding(o_m));
    tb_cmi_monitor #(.NAME("router ram")) u_mon_r (.clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(r_valid), .req_ready(r_ready), .req_addr(r_addr), .req_we(r_we), .req_wstrb(r_wstrb),
        .req_wdata(r_wdata), .rsp_valid(r_rsp_valid & ~spurious_window), .rsp_err(r_rsp_err), .errors(e_r),
        .n_handshakes(d4), .n_write_handshakes(d5), .n_responses(d6), .n_error_responses(d7), .outstanding(o_r));
    tb_cmi_monitor #(.NAME("router io")) u_mon_i (.clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(i_valid), .req_ready(i_ready), .req_addr(i_addr), .req_we(i_we), .req_wstrb(i_wstrb),
        .req_wdata(i_wdata), .rsp_valid(i_rsp_valid), .rsp_err(i_rsp_err), .errors(e_i),
        .n_handshakes(d8), .n_write_handshakes(d9), .n_responses(d10), .n_error_responses(d11), .outstanding(o_i));

    reg [8*64-1:0] msg;
    reg [31:0] rdata;
    reg        rerr;
    integer    lat_cycles;

    // One CMI transaction from the master side (CMI rules R1-R6), with timeout.
    task cmi;
        input [31:0] addr; input we; input [3:0] strb; input [31:0] wd;
        integer t;
        begin
            @(negedge clk);
            m_valid = 1'b1; m_addr = addr; m_we = we; m_wstrb = we ? strb : 4'd0; m_wdata = wd;
            t = 0;
            @(posedge clk);
            while (!m_ready && t < 1000) begin @(posedge clk); t = t + 1; end
            #1;                                    // handshake done at that edge
            m_valid = 1'b0; m_we = 1'b0; m_wstrb = 4'd0;
            lat_cycles = 0;
            while (!m_rsp_valid && lat_cycles < 1000) begin @(posedge clk); #1; lat_cycles = lat_cycles + 1; end
            if (!m_rsp_valid) tb_fail("timeout waiting for the response");
            rdata = m_rsp_rdata; rerr = m_rsp_err;
            @(posedge clk); #1;                    // leave the response cycle (rule R6)
        end
    endtask

    // Issue a request and wait for its handshake only (response handled by the caller).
    task issue;
        input [31:0] addr; input we; input [3:0] strb; input [31:0] wd;
        integer t;
        begin
            @(negedge clk);
            m_valid = 1'b1; m_addr = addr; m_we = we; m_wstrb = we ? strb : 4'd0; m_wdata = wd;
            t = 0;
            @(posedge clk);
            while (!m_ready && t < 1000) begin @(posedge clk); t = t + 1; end
            #1;
            m_valid = 1'b0; m_we = 1'b0; m_wstrb = 4'd0;
        end
    endtask

    // Expectation for a decoded access
    integer nr0, ni0, nrv0, niv0, nresp0;
    task check_route;
        input [31:0] addr; input we; input [3:0] strb; input [31:0] wd; input [1:0] exp_target;  // 1 ram 2 io 3 err
        begin
            nr0 = u_ram.n_req; ni0 = u_io.n_req; nrv0 = n_rv; niv0 = n_iv; nresp0 = d2;
            $sformat(msg, "%h: reference decode", addr);
            tb_check(msg, ref1(addr), exp_target);
            cmi(addr, we, strb, wd);
            $sformat(msg, "%h: exactly one response", addr);
            tb_check(msg, d2 - nresp0, 1);
            $sformat(msg, "%h: RAM requests", addr);
            tb_check(msg, u_ram.n_req - nr0, exp_target == 2'd1);
            $sformat(msg, "%h: MMIO requests", addr);
            tb_check(msg, u_io.n_req - ni0, exp_target == 2'd2);
            if (exp_target == 2'd1) begin
                $sformat(msg, "%h: RAM sees unchanged fields", addr);
                tb_check(msg, {u_ram.last_addr, u_ram.last_we, u_ram.last_wstrb},
                         {addr, we, we ? strb : 4'd0});
                if (we) tb_check("RAM write data", u_ram.last_wdata, wd);
                $sformat(msg, "%h: response from RAM", addr);
                tb_check(msg, rdata[31:24], 8'hA0);
            end else if (exp_target == 2'd2) begin
                $sformat(msg, "%h: MMIO sees unchanged fields", addr);
                tb_check(msg, {u_io.last_addr, u_io.last_we, u_io.last_wstrb},
                         {addr, we, we ? strb : 4'd0});
                if (we) tb_check("MMIO write data", u_io.last_wdata, wd);
                $sformat(msg, "%h: response from MMIO", addr);
                tb_check(msg, rdata[31:24], 8'hB0);
            end else begin
                $sformat(msg, "%h: error response", addr);
                tb_check(msg, rerr, 1'b1);
                $sformat(msg, "%h: no valid cycle on the RAM side", addr);
                tb_check(msg, n_rv - nrv0, 0);
                $sformat(msg, "%h: no valid cycle on the MMIO side", addr);
                tb_check(msg, n_iv - niv0, 0);
                // lat_cycles counts edges after the accepting edge: 0 = response in the next cycle
                $sformat(msg, "%h: error response in the cycle after acceptance", addr);
                tb_check(msg, lat_cycles, 0);
            end
        end
    endtask

    integer n, k, sel, n_w0;
    reg [31:0] a;

    initial begin
        tb_init;
        u_ram.configure(100, 1, 1, tb_seed);
        u_io.configure(100, 1, 1, tb_seed + 1);
        repeat (3) @(negedge clk);
        rst = 1'b0;

        // ---- 1. decode table, read and write ----
        for (k = 0; k < 2; k = k + 1) begin              // k = 0 load, 1 store
            check_route(32'h0000_0000, k, 4'hF, 32'h1111_0000, 2'd1);   // RAM first word
            check_route(32'h0000_FFFC, k, 4'h3, 32'h1111_0001, 2'd1);   // RAM last word
            check_route(32'h0000_FFFF, k, 4'h8, 32'h1111_0002, 2'd1);   // RAM last byte
            check_route(32'h0001_0000, k, 4'hF, 32'h1111_0003, 2'd3);   // just above RAM
            check_route(32'h0FFF_FFFC, k, 4'h8, 32'h1111_0004, 2'd3);
            check_route(32'h0FFF_FFFF, k, 4'h8, 32'h1111_0005, 2'd3);   // just below SYSCTRL
            check_route(32'h1000_0000, k, 4'hF, 32'h2222_0000, 2'd2);   // SYSCTRL first
            check_route(32'h1000_0008, k, 4'hF, 32'h2222_0001, 2'd2);   // TOHOST
            check_route(32'h1000_0FFC, k, 4'hF, 32'h2222_0002, 2'd2);   // SYSCTRL last word
            check_route(32'h1000_0FFF, k, 4'h8, 32'h2222_0003, 2'd2);   // SYSCTRL last byte
            check_route(32'h1000_1000, k, 4'hF, 32'h3333_0000, 2'd3);   // SPI page (not implemented)
            check_route(32'h1000_1008, k, 4'hF, 32'h3333_0001, 2'd3);
            check_route(32'h1000_2000, k, 4'hF, 32'h3333_0002, 2'd3);   // DSP page (not implemented)
            check_route(32'h1000_3000, k, 4'hF, 32'h3333_0003, 2'd3);
            check_route(32'h1000_F000, k, 4'hF, 32'h3333_0004, 2'd3);
            check_route(32'h1000_F008, k, 4'hF, 32'h3333_0005, 2'd3);
            check_route(32'h1001_0008, k, 4'hF, 32'h3333_0006, 2'd3);
            check_route(32'h1FFF_FFFC, k, 4'hF, 32'h3333_0007, 2'd3);
            check_route(32'h2000_0000, k, 4'hF, 32'h3333_0008, 2'd3);   // shared buffer (not implemented)
            check_route(32'h2000_0008, k, 4'hF, 32'h3333_0009, 2'd3);   // would alias TOHOST
            check_route(32'h2001_0000, k, 4'hF, 32'h3333_000A, 2'd3);
            check_route(32'h2FFF_FFFC, k, 4'hF, 32'h3333_000B, 2'd3);
            check_route(32'h3000_0000, k, 4'hF, 32'h3333_000C, 2'd3);
            check_route(32'h8000_0000, k, 4'hF, 32'h3333_000D, 2'd3);   // MSB set
            check_route(32'h9000_0008, k, 4'hF, 32'h3333_000E, 2'd3);
            check_route(32'hFFFF_FFFC, k, 4'hF, 32'h3333_000F, 2'd3);
        end

        // ---- 2. error responses of the targets pass through ----
        u_ram.set_error_window(32'h0000_4000, 32'h0000_40FF);
        u_io.set_error_window(32'h1000_0800, 32'h1000_08FF);
        cmi(32'h0000_4010, 1'b0, 4'd0, 32'd0); tb_check("RAM error passes through", rerr, 1'b1);
        cmi(32'h0000_5010, 1'b0, 4'd0, 32'd0); tb_check("RAM OK passes through", rerr, 1'b0);
        cmi(32'h1000_0804, 1'b1, 4'hF, 32'd5); tb_check("MMIO error passes through", rerr, 1'b1);
        tb_check("MMIO error came from the MMIO target", u_io.last_addr, 32'h1000_0804);
        cmi(32'h1000_0004, 1'b1, 4'hF, 32'd5); tb_check("MMIO OK passes through", rerr, 1'b0);

        // ---- 3. random targets with random waits and latencies ----
        u_ram.configure(50, 1, 3, tb_seed + 2);
        u_io.configure(30, 1, 8, tb_seed + 3);
        n_w0 = u_ram.n_write + u_io.n_write;
        for (n = 0; n < 3000; n = n + 1) begin
            sel = $random(tb_seed) & 7;
            a = $random(tb_seed);
            case (sel)
                0, 1: a = {16'd0, a[15:2], 2'b00};                      // RAM
                2, 3: a = {20'h1000_0, a[11:2], 2'b00};                 // SYSCTRL
                4: a = {4'h1, a[27:2], 2'b00};                          // anywhere in 0x1xxx_xxxx
                5: a = {4'h2, a[27:2], 2'b00};                          // anywhere in 0x2xxx_xxxx
                6: a = {4'h0, a[27:2], 2'b00};                          // anywhere in 0x0xxx_xxxx
                default: a = {a[31:2], 2'b00};                          // anywhere
            endcase
            check_route(a, $random(tb_seed), ({$random(tb_seed)} % 15) + 1, $random(tb_seed), ref1(a));
        end

        // ---- 4. spurious response from the non-selected target is ignored ----
        u_io.configure(100, 6, 6, tb_seed + 4);
        issue(32'h1000_0000, 1'b0, 4'd0, 32'd0);          // accepted by the MMIO slave
        spurious_window = 1'b1;
        u_ram.spurious_rsp;
        repeat (3) begin
            @(posedge clk); #1;
            if (m_rsp_valid) tb_fail("spurious RAM response passed to the core");
        end
        spurious_window = 1'b0;
        while (!m_rsp_valid) begin @(posedge clk); #1; end
        tb_check("real MMIO response after the spurious one", m_rsp_rdata[31:24], 8'hB0);
        @(posedge clk); #1;

        // ---- 5. reset with an outstanding MMIO request ----
        u_io.configure(100, 8, 8, tb_seed + 5);
        issue(32'h1000_0008, 1'b1, 4'hF, 32'd1);          // latency 8: still outstanding below
        repeat (2) @(negedge clk);
        rst = 1'b1;
        repeat (2) @(negedge clk);
        rst = 1'b0;
        repeat (12) begin
            @(posedge clk); #1;
            if (m_rsp_valid) tb_fail("stale response after reset");
        end
        u_io.configure(100, 1, 1, tb_seed + 6);
        check_route(32'h1000_0000, 1'b0, 4'd0, 32'd0, 2'd2);     // works again after reset

        // ---- 6. an unmapped store right after reset release ----
        @(negedge clk); rst = 1'b1;
        repeat (2) @(negedge clk);
        rst = 1'b0;
        check_route(32'h2000_0008, 1'b1, 4'hF, 32'h0000_0001, 2'd3);
        check_route(32'h1000_0008, 1'b1, 4'hF, 32'h0000_0001, 2'd2);

        // ---- 7. generic decode: three windows, random and boundary addresses ----
        for (n = 0; n < 20012; n = n + 1) begin
            a = $random(tb_seed);
            case ($random(tb_seed) & 3)
                0: a = {16'd0, a[15:0]};                                // around the 32 KB RAM edge
                1: a = {16'h1000, a[15:0]};                             // SYSCTRL / SPI pages and beyond
                2: a = {12'h200, a[19:0]};                              // around the 64 KB window
                default: ;
            endcase
            if (n >= 20000) begin
                case (n - 20000)
                    0: a = 32'h0000_7FFF;  1: a = 32'h0000_8000;  2: a = 32'h0FFF_FFFF;  3: a = 32'h1000_0000;
                    4: a = 32'h1000_1FFF;  5: a = 32'h1000_2000;  6: a = 32'h1FFF_FFFF;  7: a = 32'h2000_0000;
                    8: a = 32'h2000_FFFF;  9: a = 32'h2001_0000; 10: a = 32'hFFFF_FFFF; default: a = 32'h1000_0FFF;
                endcase
            end
            d2_addr = a; d2_valid = 1'b1;
            #1;
            $sformat(msg, "3-window decode %h: ram", a);
            tb_check(msg, d2_ram_valid, ref3(a) == 2'd1);
            $sformat(msg, "3-window decode %h: io", a);
            tb_check(msg, d2_io_valid, ref3(a) == 2'd2);
        end
        d2_valid = 1'b0;

        tb_check("router s monitor violations", e_m, 0);
        tb_check("router ram monitor violations", e_r, 0);
        tb_check("router io monitor violations", e_i, 0);
        tb_finish;
    end

endmodule

`default_nettype wire
