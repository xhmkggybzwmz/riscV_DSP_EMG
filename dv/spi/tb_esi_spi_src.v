`timescale 1ns / 1ps
`default_nettype none

// tb_esi_spi_src - directed and random tests of esi_spi_src_axil: SPI master as the STM32 drives it, ESI output
// into a sink with random back-pressure, PS control page. Every received frame is compared with a scoreboard
// filled from what was sent: lane data and conversion, SOS / GAP / OVR / SRC_ID, seq, timestamp, tlast.
//   +seed=N  random stalls and packet errors

module tb_esi_spi_src;
    localparam [63:0] TB_TIMEOUT_NS = 64'd900000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0, sink_aresetn = 1'b0;
    reg sclk = 1'b0, mosi = 1'b0, cs_n = 1'b1;
    wire ready;

    wire         o_valid, o_last, src_rst;
    reg          o_ready = 1'b0;
    wire [127:0] o_data;
    wire [79:0]  o_user;

    wire [11:0] awaddr, araddr;
    wire [31:0] wdata, rdata;
    wire [3:0]  wstrb;
    wire [1:0]  bresp, rresp;
    wire        awvalid, awready, wvalid, wready, bvalid, bready, arvalid, arready, rvalid, rready;
    tb_axil_master #(.AW(12)) u_m (.clk(clk), .awaddr(awaddr), .awvalid(awvalid), .awready(awready),
        .wdata(wdata), .wstrb(wstrb), .wvalid(wvalid), .wready(wready), .bresp(bresp), .bvalid(bvalid),
        .bready(bready), .araddr(araddr), .arvalid(arvalid), .arready(arready), .rdata(rdata), .rresp(rresp),
        .rvalid(rvalid), .rready(rready));

    esi_spi_src_axil u_dut (.aclk(clk), .aresetn(aresetn), .sink_aresetn(sink_aresetn),
        .spi_sclk(sclk), .spi_mosi(mosi), .spi_cs_n(cs_n), .spi_ready(ready),
        .level_energy(32'h1234_5678), .level_seq(32'd77),
        .s_axi_awaddr(awaddr), .s_axi_awprot(3'b000), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready), .s_axi_araddr(araddr),
        .s_axi_arprot(3'b000), .s_axi_arvalid(arvalid), .s_axi_arready(arready), .s_axi_rdata(rdata),
        .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .m_tvalid(o_valid), .m_tready(o_ready), .m_tdata(o_data), .m_tuser(o_user), .m_tlast(o_last),
        .src_rst(src_rst));

    integer mon_err, mon_hs;
    esi_monitor #(.NAME("spi_src")) u_mon (.clk(clk), .rst(src_rst | ~sink_aresetn), .tvalid(o_valid), .tready(o_ready),
        .tdata(o_data), .tuser(o_user), .tlast(o_last), .errors(mon_err), .handshakes(mon_hs));

    localparam O_ID = 12'h000, O_VER = 12'h004, O_ST = 12'h008, O_GOOD = 12'h00C, O_CRC = 12'h010, O_SEQ = 12'h014,
               O_FRAME = 12'h018, O_OVF = 12'h01C, O_LAST = 12'h020, O_EXP = 12'h024, O_RATE = 12'h028, O_FMT = 12'h02C,
               O_CTRL = 12'h030, O_BLOCKS = 12'h034, O_DROP = 12'h038, O_RERR = 12'h03C, O_FRAMES = 12'h040,
               O_GAP = 12'h044, O_OVR = 12'h048, O_OFFSET = 12'h04C, O_SHIFT = 12'h050, O_XRATE = 12'h054;
    reg [31:0] rv;
    task chk_reg(input [8*40-1:0] what, input [11:0] off, input [31:0] exp);
        begin u_m.read(off, rv); tb_check(what, rv, exp); end
    endtask

    // ---------------- scoreboard ----------------
    localparam EXPN = 65536;
    reg [15:0] e_lane  [0:EXPN-1];
    reg [15:0] e_flags [0:EXPN-1];
    reg [31:0] e_seq   [0:EXPN-1];
    reg [31:0] e_ts    [0:EXPN-1];
    reg        e_last  [0:EXPN-1];
    integer e_wr = 0, e_rd = 0, sb_err = 0;

    integer stall_pct = 0;
    integer rseed = 1;
    always @(posedge clk) begin
        if (o_valid && o_ready) begin
            if (e_rd >= e_wr) begin
                sb_err = sb_err + 1;
                $display("ERROR: unexpected frame seq=%h data=%h", o_user[47:16], o_data[15:0]);
            end else begin
                if (o_data !== {112'd0, e_lane[e_rd]} || o_user[15:0] !== e_flags[e_rd] || o_user[47:16] !== e_seq[e_rd] ||
                    o_user[79:48] !== e_ts[e_rd] || o_last !== e_last[e_rd]) begin
                    sb_err = sb_err + 1;
                    if (sb_err < 20)
                        $display("ERROR: frame %0d: data %h/%h flags %h/%h seq %h/%h ts %0d/%0d last %b/%b", e_rd,
                                 o_data, e_lane[e_rd], o_user[15:0], e_flags[e_rd], o_user[47:16], e_seq[e_rd],
                                 o_user[79:48], e_ts[e_rd], o_last, e_last[e_rd]);
                end
                e_rd = e_rd + 1;
            end
        end
        o_ready <= (stall_pct == 0) ? 1'b1 : (stall_pct >= 100) ? 1'b0 : (($random(rseed) & 32'h7FFF_FFFF) % 100 >= stall_pct);
    end

    // ---------------- DUT model ----------------
    integer m_offset = 2048, m_shift = 3;
    reg     m_sos = 1'b1, m_have = 1'b0;
    reg [31:0] m_exp = 0, m_k = 0;

    function [15:0] sample_value(input [31:0] seq, input integer pat);
        reg [31:0] h;
        begin
            h = seq * 32'h9E37_79B1;
            case (pat)
                0: sample_value = {4'd0, h[31:20]};                       // 12-bit codes
                1: sample_value = seq[0] ? 16'hFFFF : 16'h0000;           // saturates
                default: sample_value = h[31:16];
            endcase
        end
    endfunction

    task model_reset; begin m_sos = 1'b1; m_have = 1'b0; m_k = 0; end endtask

    task model_store(input [31:0] idx, input integer pat);
        integer i, w;
        reg gap;
        reg [15:0] u, q, fl;
        reg ovr;
        begin
            gap = m_have && (idx != m_exp);
            for (i = 0; i < 128; i = i + 1) begin
                u = sample_value(idx + i, pat);
                w = (($signed({16'd0, u}) - m_offset) * (1 << m_shift));
                ovr = (w > 32767) || (w < -32768);
                q = (w > 32767) ? 16'h7FFF : (w < -32768) ? 16'h8000 : w[15:0];
                fl = 16'h0010;
                if (i == 0 && m_sos) fl[0] = 1'b1;
                if (i == 0 && gap && !m_sos) fl[1] = 1'b1;
                if (ovr) fl[2] = 1'b1;
                e_lane[e_wr]  = q;
                e_flags[e_wr] = fl;
                e_seq[e_wr]   = idx + i;
                e_ts[e_wr]    = (i == 0 && m_sos) ? 32'd0 : m_k;
                e_last[e_wr]  = (i == 127);
                e_wr = e_wr + 1;
                m_k = ((i == 0 && m_sos) ? 32'd1 : m_k + 32'd1);
                if (i == 0) m_sos = 1'b0;
            end
            m_exp = idx + 32'd128;
            m_have = 1'b1;
        end
    endtask

    // ---------------- SPI master ----------------
    integer hp = 50;                     // half period in ns: 250 = the 2 MHz of the STM32, 50 keeps the tests fast
    reg [15:0] crc;
    reg auto_model = 1'b0, last_accept = 1'b0;
    function [15:0] crc_byte(input [15:0] c0, input [7:0] d);
        integer j;
        reg [15:0] c;
        begin
            c = c0 ^ {d, 8'h00};
            for (j = 0; j < 8; j = j + 1) c = c[15] ? ((c << 1) ^ 16'h1021) : (c << 1);
            crc_byte = c;
        end
    endfunction

    task send_raw(input [7:0] b);
        integer j;
        begin
            for (j = 7; j >= 0; j = j - 1) begin
                mosi = b[j]; #(hp); sclk = 1'b1; #(hp); sclk = 1'b0;
            end
        end
    endtask
    task send_crc(input [7:0] b); begin crc = crc_byte(crc, b); send_raw(b); end endtask

    // kind: 0 good, 1 CRC error, 2 CS released in the middle, 3 bad magic, 4 wrong channel count, 5 one byte too many,
    //       6 good with a 30 ns pulse on CS in the middle
    task send_packet(input [31:0] idx, input [31:0] rate, input integer kind, input integer pat);
        integer i;
        reg [15:0] v;
        begin
            crc = 16'hFFFF;
            cs_n = 1'b0; #(200);               // the STM32 takes microseconds; CS counts after about 110 ns
            last_accept = u_dut.accept;       // the store decided when CS fell
            if (auto_model && kind == 0 && last_accept) model_store(idx, pat);
            send_crc((kind == 3) ? "X" : "E"); send_crc("M"); send_crc("G"); send_crc("1");
            send_crc(8'd1); send_crc(8'd1); send_crc((kind == 4) ? 8'd2 : 8'd1); send_crc(8'd0);
            send_crc(rate[7:0]); send_crc(rate[15:8]); send_crc(rate[23:16]); send_crc(rate[31:24]);
            send_crc(idx[7:0]); send_crc(idx[15:8]); send_crc(idx[23:16]); send_crc(idx[31:24]);
            send_crc(8'h80); send_crc(8'h00); send_crc(8'h00); send_crc(8'h01);
            for (i = 0; i < 128 && !(kind == 2 && i == 60); i = i + 1) begin
                v = sample_value(idx + i, pat);
                if (kind == 6 && i == 60) begin cs_n = 1'b1; #(30); cs_n = 1'b0; end
                send_crc(v[7:0]); send_crc(v[15:8]);
            end
            if (kind != 2) begin
                send_raw(crc[7:0] ^ ((kind == 1) ? 8'h40 : 8'h00));
                send_raw(crc[15:8]);
                if (kind == 5) send_raw(8'hA5);
            end
            #(100); cs_n = 1'b1; #(300);
        end
    endtask

    task idle(input integer n); begin repeat (n) @(posedge clk); end endtask

    task drain;
        integer guard;
        begin
            guard = 0;
            while ((e_rd != e_wr || o_valid) && guard < 2000000) begin @(posedge clk); guard = guard + 1; end
            if (guard >= 2000000) tb_fail("drain timeout");
            idle(20);
            tb_check("scoreboard consumed", e_rd, e_wr);
            tb_check("no frame offered", o_valid, 0);
        end
    endtask

    localparam RATE = 32'd2000;
    integer i, n_good, n_bad, kind, pct;
    reg [31:0] idx, r;

    initial begin
        tb_init;
        rseed = tb_seed;
        idle(10);
        tb_check("READY low in reset", ready, 0);
        tb_check("VALID low in reset", o_valid, 0);
        aresetn = 1'b1; sink_aresetn = 1'b1;
        idle(10);

        // ---- page after reset ----
        chk_reg("ID", O_ID, 32'h4553_5331);
        chk_reg("VERSION", O_VER, 32'h0001_0000);
        chk_reg("FORMAT", O_FMT, 32'h0100_0080);
        chk_reg("CTRL", O_CTRL, 0);
        chk_reg("OFFSET", O_OFFSET, 2048);
        chk_reg("SHIFT", O_SHIFT, 3);
        chk_reg("EXPECT_RATE", O_XRATE, 2000);
        chk_reg("LEVEL_ENERGY", 12'h058, 32'h1234_5678);
        chk_reg("LEVEL_SEQ", 12'h05C, 77);
        chk_reg("unimplemented", 12'h7F0, 0);
        tb_check("READY after reset", ready, 1);

        // ---- RUN = 0: packets are counted, nothing is stored ----
        send_packet(32'd0, RATE, 0, 0);
        idle(20);
        chk_reg("run0 GOOD", O_GOOD, 1);
        chk_reg("run0 BLOCKS", O_BLOCKS, 0);
        chk_reg("run0 LAST", O_LAST, 0);
        chk_reg("run0 EXPECTED", O_EXP, 128);
        chk_reg("run0 RATE", O_RATE, 2000);
        tb_check("run0 no frame", o_valid, 0);

        // ---- first stored packet at the real 2 MHz: SOS, conversion, seq, timestamp, tlast ----
        u_m.write(O_CTRL, 32'h1, 4'hF);
        hp = 250;
        model_store(32'd128, 0);
        send_packet(32'd128, RATE, 0, 0);
        hp = 50;
        drain;
        chk_reg("frames", O_FRAMES, 128);
        chk_reg("gap 0", O_GAP, 0);
        chk_reg("seq ok", O_SEQ, 0);

        // ---- the next one follows without any flag ----
        model_store(32'd256, 0);
        send_packet(32'd256, RATE, 0, 0);
        drain;

        // ---- CRC error: nothing of it on ESI; the successor carries GAP ----
        send_packet(32'd384, RATE, 1, 0);
        idle(50);
        chk_reg("crc count", O_CRC, 1);
        chk_reg("crc blocks", O_BLOCKS, 0);
        tb_check("crc no frame", o_valid, 0);
        model_store(32'd512, 0);
        send_packet(32'd512, RATE, 0, 0);
        drain;
        chk_reg("gap after crc", O_GAP, 1);
        chk_reg("seq after crc", O_SEQ, 1);

        // ---- CS released in the middle ----
        send_packet(32'd640, RATE, 2, 0);
        idle(50);
        chk_reg("abort FRAME", O_FRAME, 1);
        chk_reg("abort BLOCKS", O_BLOCKS, 0);
        tb_check("abort no frame", o_valid, 0);
        model_store(32'd768, 0);
        send_packet(32'd768, RATE, 0, 0);
        drain;

        // ---- a short pulse on CS does not cut the packet ----
        model_store(32'd896, 0);
        send_packet(32'd896, RATE, 6, 0);
        drain;
        chk_reg("CS pulse FRAME", O_FRAME, 1);
        // ---- bad header ----
        send_packet(32'd1024, RATE, 3, 0);
        send_packet(32'd1100, RATE, 4, 0);
        idle(50);
        chk_reg("bad headers", O_FRAME, 3);
        tb_check("bad header no frame", o_valid, 0);

        // ---- too long: stored (the CRC passed before the extra byte), reported once ----
        model_store(32'd1152, 0);
        send_packet(32'd1152, RATE, 5, 0);
        drain;
        chk_reg("too long", O_FRAME, 4);

        // ---- wrong rate: not stored, counted, GAP afterwards; EXPECT_RATE = 0 accepts it ----
        send_packet(32'd1280, 32'd4000, 0, 0);
        idle(50);
        chk_reg("rate err", O_RERR, 1);
        chk_reg("rate last", O_RATE, 4000);
        tb_check("rate no frame", o_valid, 0);
        u_m.write(O_XRATE, 32'd0, 4'hF);
        model_store(32'd1408, 0);
        send_packet(32'd1408, 32'd4000, 0, 0);
        drain;
        u_m.write(O_XRATE, 32'd2000, 4'hF);

        // ---- SCLK too fast: reported, not stored ----
        hp = 15;
        send_packet(32'd1536, RATE, 0, 0);
        hp = 50;
        idle(50);
        u_m.read(O_OVF, r);
        tb_check("overflow seen", r != 0, 1);
        tb_check("fast no frame", o_valid, 0);
        chk_reg("fast blocks", O_BLOCKS, 0);

        // ---- CLEAR: counters and baseline, not the data path ----
        u_m.write(O_VER, 32'h1, 4'hF);
        idle(4);
        chk_reg("clear GOOD", O_GOOD, 0);
        chk_reg("clear CRC", O_CRC, 0);
        chk_reg("clear SEQ", O_SEQ, 0);
        chk_reg("clear FRAME", O_FRAME, 0);
        chk_reg("clear OVF", O_OVF, 0);
        u_m.read(O_ST, r);
        tb_check("clear baseline", r[4], 0);
        tb_check("clear RUN kept", r[5], 1);

        // ---- back-pressure: READY low with 3 blocks, the 5th packet is lost and counted, order kept ----
        stall_pct = 100;
        idle(4);
        idx = 32'd4096;
        for (i = 0; i < 3; i = i + 1) begin
            tb_check("READY before block", ready, 1);
            model_store(idx, 0);
            send_packet(idx, RATE, 0, 0);
            idx = idx + 128;
        end
        idle(20);
        chk_reg("three blocks", O_BLOCKS, 3);
        tb_check("READY low at 3", ready, 0);
        u_m.read(O_ST, r);
        tb_check("STATUS READY", r[0], 0);
        tb_check("STATUS VALID", r[7], 1);
        // RUN cleared: the offered frame stays offered
        u_m.write(O_CTRL, 32'h0, 4'hF);
        idle(20);
        tb_check("VALID kept with RUN = 0", o_valid, 1);
        u_m.write(O_CTRL, 32'h1, 4'hF);
        // the packet already on its way when READY fell
        model_store(idx, 0);
        send_packet(idx, RATE, 0, 0);
        idx = idx + 128;
        idle(20);
        chk_reg("four blocks", O_BLOCKS, 4);
        send_packet(idx, RATE, 0, 0);    // no free block
        idx = idx + 128;
        idle(20);
        chk_reg("drop", O_DROP, 1);
        chk_reg("still four", O_BLOCKS, 4);
        stall_pct = 0;
        drain;
        tb_check("READY back", ready, 1);
        model_store(idx, 0);
        send_packet(idx, RATE, 0, 0);    // follows a lost one: GAP
        idx = idx + 128;
        drain;

        // ---- saturation, OFFSET and SHIFT ----
        u_m.read(O_OVR, r);
        tb_check("no OVR so far", r, 0);
        model_store(idx, 1);
        send_packet(idx, RATE, 0, 1);
        idx = idx + 128;
        drain;
        chk_reg("OVR defaults", O_OVR, 64);       // (0xFFFF - 2048) << 3 saturates, (0 - 2048) << 3 does not
        u_m.write(O_OFFSET, 32'd0, 4'hF);
        u_m.write(O_SHIFT, 32'd0, 4'hF);
        m_offset = 0; m_shift = 0;
        chk_reg("OFFSET written", O_OFFSET, 0);
        chk_reg("SHIFT written", O_SHIFT, 0);
        model_store(idx, 2);
        send_packet(idx, RATE, 0, 2);
        idx = idx + 128;
        drain;
        u_m.write(O_OFFSET, 32'd2048, 4'hF);
        u_m.write(O_SHIFT, 32'd3, 4'hF);
        m_offset = 2048; m_shift = 3;

        // ---- sequence numbers wrap without GAP ----
        u_m.write(O_CTRL, 32'h4, 4'hF);
        model_reset;
        idle(4);
        u_m.write(O_CTRL, 32'h1, 4'hF);
        model_store(32'hFFFF_FF80, 0);
        send_packet(32'hFFFF_FF80, RATE, 0, 0);
        model_store(32'h0000_0000, 0);
        send_packet(32'h0000_0000, RATE, 0, 0);
        drain;
        chk_reg("wrap no gap", O_GAP, 0);
        chk_reg("wrap no seq", O_SEQ, 0);

        // ---- the sink resets: nothing offered, store emptied, SOS; the page stays alive ----
        stall_pct = 100;
        idle(4);
        model_store(32'd128, 0);
        send_packet(32'd128, RATE, 0, 0);
        idle(20);
        tb_check("before sink reset", o_valid, 1);
        sink_aresetn = 1'b0;
        idle(1);
        #1;
        tb_check("VALID low in sink reset", o_valid, 0);
        idle(5);
        chk_reg("sink reset page alive", O_ID, 32'h4553_5331);
        chk_reg("sink reset BLOCKS", O_BLOCKS, 0);
        u_m.read(O_ST, r);
        tb_check("STATUS SINK_RESET", r[6], 1);
        send_packet(32'd256, RATE, 0, 0);          // not stored while the sink is in reset
        idle(20);
        chk_reg("not stored in sink reset", O_BLOCKS, 0);
        sink_aresetn = 1'b1;
        idle(5);
        e_rd = e_wr;                               // the offered block is gone
        model_reset;
        stall_pct = 0;
        idle(5);
        tb_check("nothing after sink reset", o_valid, 0);
        model_store(32'd1000, 0);                  // SOS, no GAP although the index jumps
        send_packet(32'd1000, RATE, 0, 0);
        drain;

        // ---- SOFT_RESET: stops, empties, clears; SOS again ----
        stall_pct = 100;
        idle(4);
        model_store(32'd1128, 0);
        send_packet(32'd1128, RATE, 0, 0);
        idle(20);
        u_m.write(O_CTRL, 32'h5, 4'hF);
        idle(4);
        e_rd = e_wr;
        model_reset;
        chk_reg("soft CTRL", O_CTRL, 0);
        chk_reg("soft BLOCKS", O_BLOCKS, 0);
        chk_reg("soft FRAMES", O_FRAMES, 0);
        chk_reg("soft GOOD", O_GOOD, 0);
        tb_check("soft VALID", o_valid, 0);
        stall_pct = 0;
        idle(4);
        u_m.write(O_CTRL, 32'h1, 4'hF);
        model_store(32'd5000, 0);
        send_packet(32'd5000, RATE, 0, 0);
        drain;

        // ---- random: errors and stalls, the sink sometimes too slow for the packets ----
        idx = 32'd9000;
        n_good = 0; n_bad = 0;
        for (i = 0; i < 40; i = i + 1) begin
            pct = ($random(rseed) & 32'h7FFF_FFFF) % 4;
            stall_pct = (pct == 0) ? 0 : (pct == 1) ? 50 : (pct == 2) ? 90 : 99;
            kind = ($random(rseed) & 32'h7FFF_FFFF) % 10;
            if (kind > 3) kind = 0;
            auto_model = 1'b1;
            send_packet(idx, RATE, kind, 0);
            if (kind == 0 && last_accept) n_good = n_good + 1; else n_bad = n_bad + 1;
            idx = idx + 128;
        end
        auto_model = 1'b0;
        stall_pct = 0;
        drain;
        $display("INFO: random: %0d stored, %0d not stored", n_good, n_bad);

        tb_check("monitor errors", mon_err, 0);
        tb_check("scoreboard errors", sb_err, 0);
        tb_finish;
    end
endmodule

`default_nettype wire
