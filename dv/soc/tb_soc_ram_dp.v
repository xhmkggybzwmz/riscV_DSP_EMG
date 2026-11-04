`timescale 1ns / 1ps
`default_nettype none

// tb_soc_ram_dp - self-checking unit test for rtl/soc/soc_ram_dp.v
//
// Drives both CMI ports following the CMI rules (a CMI monitor checks every
// cycle) and checks against a shadow model:
//   - INIT_FILE initialisation (second instance, dv/soc/data/ram_init_test.hex)
//   - every word written/read through port B, read back through port A
//   - first/last word and byte, all 16 byte-enable patterns
//   - out-of-range addresses: error response, no access, no wrap-around
//   - response exactly one cycle after acceptance, for one cycle
//   - reset: response register cleared, no write while rst is high
//   - random interleaved traffic on both ports

module tb_soc_ram_dp;

    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    localparam MEM_BYTES = 65536;
    localparam WORDS     = MEM_BYTES / 4;

    reg clk = 1'b0;
    reg rst = 1'b1;
    always #5 clk = ~clk;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_soc_ram_dp); end
`endif

    reg         a_valid = 1'b0;
    reg  [31:0] a_addr  = 32'd0;
    wire        a_ready, a_rsp_valid, a_rsp_err;
    wire [31:0] a_rsp_rdata;
    reg         b_valid = 1'b0, b_we = 1'b0;
    reg  [31:0] b_addr  = 32'd0, b_wdata = 32'd0;
    reg  [3:0]  b_wstrb = 4'd0;
    wire        b_ready, b_rsp_valid, b_rsp_err;
    wire [31:0] b_rsp_rdata;

    soc_ram_dp #(.MEM_BYTES(MEM_BYTES)) dut (
        .clk(clk), .rst(rst),
        .a_req_valid(a_valid), .a_req_ready(a_ready), .a_req_addr(a_addr),
        .a_rsp_valid(a_rsp_valid), .a_rsp_rdata(a_rsp_rdata), .a_rsp_err(a_rsp_err),
        .b_req_valid(b_valid), .b_req_ready(b_ready), .b_req_addr(b_addr), .b_req_we(b_we),
        .b_req_wstrb(b_wstrb), .b_req_wdata(b_wdata),
        .b_rsp_valid(b_rsp_valid), .b_rsp_rdata(b_rsp_rdata), .b_rsp_err(b_rsp_err)
    );

    // second instance: INIT_FILE, and the "no write during reset" check (no monitor:
    // that check deliberately drives valid while rst is high)
    reg         i_valid = 1'b0, i_we = 1'b0, i_rst = 1'b1;
    reg  [31:0] i_addr  = 32'd0, i_wdata = 32'd0;
    wire        i_ready, i_rsp_valid, i_rsp_err, ia_ready, ia_rsp_valid, ia_rsp_err;
    wire [31:0] i_rsp_rdata, ia_rsp_rdata;
    soc_ram_dp #(.MEM_BYTES(64), .INIT_FILE("../../../dv/soc/data/ram_init_test.hex")) u_init (
        .clk(clk), .rst(i_rst),
        .a_req_valid(1'b0), .a_req_ready(ia_ready), .a_req_addr(32'd0),
        .a_rsp_valid(ia_rsp_valid), .a_rsp_rdata(ia_rsp_rdata), .a_rsp_err(ia_rsp_err),
        .b_req_valid(i_valid), .b_req_ready(i_ready), .b_req_addr(i_addr), .b_req_we(i_we),
        .b_req_wstrb(4'hF), .b_req_wdata(i_wdata),
        .b_rsp_valid(i_rsp_valid), .b_rsp_rdata(i_rsp_rdata), .b_rsp_err(i_rsp_err)
    );

    wire [31:0] ma_err, ma_hs, ma_whs, ma_rsp, ma_ersp, mb_err, mb_hs, mb_whs, mb_rsp, mb_ersp;
    wire        ma_out, mb_out;
    tb_cmi_monitor #(.NAME("ram A"), .CHECK_ALIGN(1)) u_mon_a (
        .clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(a_valid), .req_ready(a_ready), .req_addr(a_addr), .req_we(1'b0),
        .req_wstrb(4'd0), .req_wdata(32'd0), .rsp_valid(a_rsp_valid), .rsp_err(a_rsp_err),
        .errors(ma_err), .n_handshakes(ma_hs), .n_write_handshakes(ma_whs),
        .n_responses(ma_rsp), .n_error_responses(ma_ersp), .outstanding(ma_out));
    tb_cmi_monitor #(.NAME("ram B"), .CHECK_ALIGN(0)) u_mon_b (
        .clk(clk), .rst(rst), .halted(1'b0),
        .req_valid(b_valid), .req_ready(b_ready), .req_addr(b_addr), .req_we(b_we),
        .req_wstrb(b_wstrb), .req_wdata(b_wdata), .rsp_valid(b_rsp_valid), .rsp_err(b_rsp_err),
        .errors(mb_err), .n_handshakes(mb_hs), .n_write_handshakes(mb_whs),
        .n_responses(mb_rsp), .n_error_responses(mb_ersp), .outstanding(mb_out));

    // ---------------- shadow model ----------------
    reg [31:0] shadow  [0:WORDS-1];
    reg        written [0:WORDS-1];
    reg [8*64-1:0] msg;
    reg [31:0] rd;
    reg        er;

    // One port-B access, CMI timing: valid for one cycle (ready is always 1),
    // response must be present in the next cycle and only there.
    task b_access;
        input [31:0] addr; input we; input [3:0] strb; input [31:0] wd; input exp_err;
        integer k;
        begin
            @(negedge clk);
            b_valid = 1'b1; b_addr = addr; b_we = we; b_wstrb = we ? strb : 4'd0; b_wdata = wd;
            @(negedge clk);
            b_valid = 1'b0; b_we = 1'b0; b_wstrb = 4'd0;
            $sformat(msg, "B %0s %h: response one cycle after acceptance", we ? "write" : "read", addr);
            tb_check(msg, b_rsp_valid, 1'b1);
            $sformat(msg, "B %0s %h: rsp_err", we ? "write" : "read", addr);
            tb_check(msg, b_rsp_err, exp_err);
            rd = b_rsp_rdata; er = b_rsp_err;
            if (!exp_err && addr < MEM_BYTES && we) begin
                for (k = 0; k < 4; k = k + 1)
                    if (strb[k]) shadow[addr[15:2]][8*k +: 8] = wd[8*k +: 8];
                written[addr[15:2]] = 1'b1;
            end
            @(negedge clk);
            $sformat(msg, "B %h: response lasts one cycle", addr);
            tb_check(msg, b_rsp_valid, 1'b0);
        end
    endtask

    task a_read;
        input [31:0] addr; input exp_err;
        begin
            @(negedge clk);
            a_valid = 1'b1; a_addr = addr;
            @(negedge clk);
            a_valid = 1'b0;
            $sformat(msg, "A read %h: response one cycle after acceptance", addr);
            tb_check(msg, a_rsp_valid, 1'b1);
            $sformat(msg, "A read %h: rsp_err", addr);
            tb_check(msg, a_rsp_err, exp_err);
            rd = a_rsp_rdata; er = a_rsp_err;
            @(negedge clk);
            tb_check("A response lasts one cycle", a_rsp_valid, 1'b0);
        end
    endtask

    task check_word;              // read one word through both ports
        input [31:0] addr;
        begin
            b_access(addr, 1'b0, 4'd0, 32'd0, 1'b0);
            $sformat(msg, "B read %h data", addr);
            tb_check(msg, rd, shadow[addr[15:2]]);
            a_read(addr, 1'b0);
            $sformat(msg, "A read %h data", addr);
            tb_check(msg, rd, shadow[addr[15:2]]);
        end
    endtask

    integer n, w, s;
    reg [31:0] addr, data;

    initial begin
        tb_init;
        for (w = 0; w < WORDS; w = w + 1) begin shadow[w] = 32'd0; written[w] = 1'b0; end
        repeat (3) @(negedge clk);
        rst = 1'b0; i_rst = 1'b0;

        // ---- 1. INIT_FILE ----
        for (w = 0; w < 8; w = w + 1) begin
            @(negedge clk); i_valid = 1'b1; i_addr = 4 * w; i_we = 1'b0;
            @(negedge clk); i_valid = 1'b0;
            $sformat(msg, "INIT_FILE word %0d", w);
            case (w)
                0: tb_check(msg, i_rsp_rdata, 32'h13579bdf);  1: tb_check(msg, i_rsp_rdata, 32'h2468ace0);
                2: tb_check(msg, i_rsp_rdata, 32'h00000000);  3: tb_check(msg, i_rsp_rdata, 32'hffffffff);
                4: tb_check(msg, i_rsp_rdata, 32'h80000001);  5: tb_check(msg, i_rsp_rdata, 32'h7ffffffe);
                6: tb_check(msg, i_rsp_rdata, 32'hdeadbeef);  default: tb_check(msg, i_rsp_rdata, 32'h0badf00d);
            endcase
        end

        // ---- 2. every word: write through B, read back through B and A ----
        for (w = 0; w < WORDS; w = w + 1)
            b_access(4 * w, 1'b1, 4'hF, {w[15:0], ~w[15:0]}, 1'b0);
        for (w = 0; w < WORDS; w = w + 1) begin
            b_access(4 * w, 1'b0, 4'd0, 32'd0, 1'b0);
            if (rd !== shadow[w]) begin $sformat(msg, "B readback word %0d", w); tb_check(msg, rd, shadow[w]); end
            else tb_check("B readback", 1'b1, 1'b1);
        end
        for (w = 0; w < WORDS; w = w + 97) check_word(4 * w);

        // ---- 3. first/last word and byte, every non-zero byte-enable pattern ----
        b_access(32'h0000_0000, 1'b1, 4'hF, 32'hA5A5_0001, 1'b0); check_word(32'h0000_0000);
        b_access(MEM_BYTES - 4, 1'b1, 4'hF, 32'h5A5A_FFFC, 1'b0); check_word(MEM_BYTES - 4);
        b_access(MEM_BYTES - 1, 1'b1, 4'h8, 32'hEE00_0000, 1'b0); check_word(MEM_BYTES - 4);
        for (s = 1; s < 16; s = s + 1) begin      // wstrb = 0 is not a legal CMI write
            addr = 32'h0000_1000 + 4 * s;
            b_access(addr, 1'b1, 4'hF, 32'h0000_0000, 1'b0);
            b_access(addr + (s & 3), 1'b1, s[3:0], 32'hDDCC_BBAA, 1'b0);   // low address bits ignored
            check_word(addr);
        end

        // ---- 4. out of range: error, no access, no wrap-around ----
        b_access(32'h0000_0000, 1'b1, 4'hF, 32'h0000_C0DE, 1'b0);
        b_access(MEM_BYTES, 1'b1, 4'hF, 32'hBAD0_0001, 1'b1);              // would alias word 0
        b_access(MEM_BYTES + 4, 1'b1, 4'hF, 32'hBAD0_0002, 1'b1);          // would alias word 1
        b_access(32'h0FFF_FFFC, 1'b1, 4'hF, 32'hBAD0_0003, 1'b1);
        b_access(32'h8000_0000, 1'b1, 4'hF, 32'hBAD0_0004, 1'b1);
        b_access(32'hFFFF_FFFC, 1'b1, 4'hF, 32'hBAD0_0005, 1'b1);
        b_access(MEM_BYTES, 1'b0, 4'd0, 32'd0, 1'b1);
        a_read(MEM_BYTES, 1'b1);
        a_read(32'h1000_0000, 1'b1);
        check_word(32'h0000_0000);                                          // still 0000C0DE
        check_word(32'h0000_0004);
        check_word(MEM_BYTES - 4);

        // ---- 5. reset: response cleared, contents kept ----
        b_access(32'h0000_2000, 1'b1, 4'hF, 32'h1111_2222, 1'b0);
        @(negedge clk); b_valid = 1'b1; b_addr = 32'h0000_2000; b_we = 1'b0; b_wstrb = 4'd0;
        @(negedge clk); b_valid = 1'b0;                     // response cycle: assert reset now
        rst = 1'b1;
        @(negedge clk);
        tb_check("response register cleared by reset", b_rsp_valid, 1'b0);
        repeat (2) @(negedge clk);
        rst = 1'b0;
        repeat (2) @(negedge clk);
        tb_check("no response after reset release", b_rsp_valid, 1'b0);
        check_word(32'h0000_2000);                          // contents survive reset
        // no write while rst is high (u_init: valid driven during reset on purpose)
        @(negedge clk); i_rst = 1'b1; i_valid = 1'b1; i_we = 1'b1; i_addr = 32'd4; i_wdata = 32'hFFFF_0000;
        @(negedge clk); i_valid = 1'b0; i_we = 1'b0;
        tb_check("no response for a request during reset", i_rsp_valid, 1'b0);
        @(negedge clk); i_rst = 1'b0;
        @(negedge clk); i_valid = 1'b1; i_addr = 32'd4;
        @(negedge clk); i_valid = 1'b0;
        tb_check("no write while rst is high", i_rsp_rdata, 32'h2468ace0);

        // ---- 6. random interleaved traffic ----
        for (n = 0; n < 20000; n = n + 1) begin
            addr = ($random(tb_seed) & (MEM_BYTES - 1)) & ~32'd3;
            case ($random(tb_seed) & 3)
                0: b_access(addr, 1'b1, ({$random(tb_seed)} % 15) + 1, $random(tb_seed), 1'b0);
                1: begin
                       b_access(addr, 1'b0, 4'd0, 32'd0, 1'b0);
                       $sformat(msg, "random B read %h", addr);
                       tb_check(msg, rd, shadow[addr[15:2]]);
                   end
                2: begin
                       a_read(addr, 1'b0);
                       $sformat(msg, "random A read %h", addr);
                       tb_check(msg, rd, shadow[addr[15:2]]);
                   end
                default: b_access(MEM_BYTES + addr, ($random(tb_seed) & 1), 4'hF, 32'hBAD, 1'b1);
            endcase
        end

        tb_check("port A monitor violations", ma_err, 0);
        tb_check("port B monitor violations", mb_err, 0);
        tb_finish;
    end

endmodule

`default_nettype wire
