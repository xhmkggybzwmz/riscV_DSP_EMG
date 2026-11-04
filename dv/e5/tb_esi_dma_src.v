`timescale 1ns / 1ps
`default_nettype none

// tb_esi_dma_src - directed and random tests of esi_dma_src_axil: AXI4-Stream input as the AMD AXI DMA
// drives it, ESI output into a sink with random back-pressure, PS control page.
// Every received frame is checked against what was sent: data order, SOS, timestamp, seq, tlast.
//   +seed=N  random stalls

module tb_esi_dma_src;
    localparam [63:0] TB_TIMEOUT_NS = 64'd200000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg aresetn = 1'b0, sink_aresetn = 1'b0;

    reg  [63:0] i_data = 64'd0;
    reg  [7:0]  i_keep = 8'hFF;
    reg         i_valid = 1'b0, i_last = 1'b0;
    wire        i_ready;
    wire        o_valid, o_last, src_rst;
    reg         o_ready = 1'b0;
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

    esi_dma_src_axil u_dut (.aclk(clk), .aresetn(aresetn), .sink_aresetn(sink_aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awprot(3'b000), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready), .s_axi_araddr(araddr),
        .s_axi_arprot(3'b000), .s_axi_arvalid(arvalid), .s_axi_arready(arready), .s_axi_rdata(rdata),
        .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .s_axis_tdata(i_data), .s_axis_tkeep(i_keep), .s_axis_tvalid(i_valid), .s_axis_tready(i_ready),
        .s_axis_tlast(i_last),
        .m_tvalid(o_valid), .m_tready(o_ready), .m_tdata(o_data), .m_tuser(o_user), .m_tlast(o_last),
        .src_rst(src_rst));

    integer mon_err, mon_hs;
    esi_monitor #(.NAME("src")) u_mon (.clk(clk), .rst(src_rst | ~sink_aresetn), .tvalid(o_valid), .tready(o_ready),
        .tdata(o_data), .tuser(o_user), .tlast(o_last), .errors(mon_err), .handshakes(mon_hs));

    localparam O_ID = 12'h000, O_VER = 12'h004, O_CTRL = 12'h008, O_ST = 12'h00C, O_PERIOD = 12'h014,
               O_FRAMES = 12'h018, O_LATE = 12'h01C, O_SEQ = 12'h020, O_SEG = 12'h024, O_UNDER = 12'h028,
               O_KEEP = 12'h02C;
    reg [31:0] rv;
    task chk_reg(input [8*40-1:0] what, input [11:0] off, input [31:0] exp);
        begin u_m.read(off, rv); tb_check(what, rv, exp); end
    endtask

    // ---------------- sink: random back-pressure, log of every frame ----------------
    localparam LOGN = 8192;
    reg [63:0] l_data [0:LOGN-1];
    reg [79:0] l_user [0:LOGN-1];
    reg        l_last [0:LOGN-1];
    integer    l_cyc  [0:LOGN-1];
    integer n_rx = 0, cyc = 0, ready_pct = 100, up_err = 0;
    always @(posedge clk) begin
        cyc <= cyc + 1;
        if (o_valid && o_ready) begin
            l_data[n_rx] = o_data[63:0];
            l_user[n_rx] = o_user;
            l_last[n_rx] = o_last;
            l_cyc[n_rx]  = cyc;
            if (o_data[127:64] !== 64'd0) up_err = up_err + 1;
            n_rx = n_rx + 1;
        end
    end
    always @(negedge clk) o_ready = ({$random(tb_seed)} % 100) < ready_pct;

    // ---------------- stream source ----------------
    integer stall_pct = 0;
    function [63:0] pat(input integer s, input integer i);
        pat = {16'hA000 + s[15:0], 16'hB000 + i[15:0], 16'hC000 ^ i[15:0], 16'hD000 + s[15:0] + i[15:0]};
    endfunction
    // frames from..from+len-1 of segment s; TLAST on the last one if last = 1
    task send_frames(input integer s, input integer from, input integer len, input last);
        integer i;
        begin
            for (i = 0; i < len; i = i + 1) begin
                while (({$random(tb_seed)} % 100) < stall_pct) @(negedge clk);
                @(negedge clk);
                i_valid = 1'b1; i_data = pat(s, from + i); i_last = last && (i == len - 1);
                @(posedge clk);
                while (!i_ready) @(posedge clk);
                @(negedge clk) i_valid = 1'b0;
            end
        end
    endtask
    task send_seg(input integer s, input integer len);
        send_frames(s, 0, len, 1'b1);
    endtask
    // the same in the background (while the main sequence watches the sink)
    reg bg_req = 1'b0;
    integer bg_s = 0, bg_len = 0;
    initial forever begin
        wait (bg_req);
        send_frames(bg_s, 0, bg_len, 1'b1);
        bg_req = 1'b0;
    end
    task bg_seg(input integer s, input integer len);
        begin bg_s = s; bg_len = len; bg_req = 1'b1; end
    endtask
    task bg_wait;
        while (bg_req) @(posedge clk);
    endtask

    // frames n0.. of log == segment s, frames 0..len-1; seq from seq0
    task chk_seg(input [8*32-1:0] tag, input integer n0, input integer s, input integer len, input [31:0] seq0);
        integer i, e;
        begin
            e = 0;
            for (i = 0; i < len; i = i + 1) begin
                if (l_data[n0 + i] !== pat(s, i)) e = e + 1;
                if (l_user[n0 + i][15:0] !== ((i == 0) ? 16'h0001 : 16'h0000)) e = e + 1;
                if (l_user[n0 + i][47:16] !== seq0 + i) e = e + 1;
                if (l_user[n0 + i][79:48] !== i) e = e + 1;
                if (l_last[n0 + i] !== ((i == len - 1) || (i % 100 == 99))) e = e + 1;
            end
            tb_check(tag, e, 0);
        end
    endtask

    task wait_rx(input integer n);
        integer w;
        begin
            w = 0;
            while (n_rx < n && w < 2000000) begin @(posedge clk); w = w + 1; end
            tb_check("all frames arrived", n_rx >= n, 1'b1);
        end
    endtask

    task reset_src(input [31:0] period, input [31:0] seq);
        begin
            u_m.write(O_CTRL, 32'h4, 4'hF);
            u_m.write(O_PERIOD, period, 4'hF);
            u_m.write(O_SEQ, seq, 4'hF);
            u_m.write(O_CTRL, 32'h1, 4'hF);
            n_rx = 0;
        end
    endtask

    integer i, base, gaps_ok;
    initial begin
        tb_init;
        u_m.configure(3, 2, 2, tb_seed);
        repeat (5) @(posedge clk);
        #1 begin aresetn = 1'b1; sink_aresetn = 1'b1; end
        repeat (3) @(posedge clk);

        // ---- registers ----
        chk_reg("ID", O_ID, 32'h4553_4431);
        chk_reg("VERSION", O_VER, 32'h0001_0000);
        chk_reg("PERIOD reset value", O_PERIOD, 32'd50000);
        chk_reg("STATUS after reset", O_ST, 32'h0);
        tb_check("no input accepted while RUN = 0", i_ready, 1'b0);

        // ---- flow mode, back-pressure and input stalls, segment lengths 7, 1, 250 ----
        ready_pct = 40; stall_pct = 30;
        reset_src(0, 32'h100);
        send_seg(1, 7); send_seg(2, 1); send_seg(3, 250);
        wait_rx(258);
        chk_seg("flow: segment 7 frames", 0, 1, 7, 32'h100);
        chk_seg("flow: segment 1 frame", 7, 2, 1, 32'h107);
        chk_seg("flow: segment 250 frames", 8, 3, 250, 32'h108);
        repeat (5) @(posedge clk);
        chk_reg("flow: FRAME_CNT", O_FRAMES, 258);
        chk_reg("flow: SEG_CNT", O_SEG, 3);
        chk_reg("flow: LATE_CNT", O_LATE, 0);
        chk_reg("flow: UNDERRUN_CNT (no period)", O_UNDER, 0);
        chk_reg("flow: KEEP_ERR_CNT", O_KEEP, 0);

        // ---- flow mode without any stall: one frame per cycle is not required, order is ----
        ready_pct = 100; stall_pct = 0;
        reset_src(0, 0);
        send_seg(4, 300);
        wait_rx(300);
        chk_seg("flow fast", 0, 4, 300, 0);

        // ---- paced: exactly PERIOD between frames while the input keeps up ----
        reset_src(10, 32'h5000);
        send_seg(5, 120);
        wait_rx(120);
        chk_seg("paced", 0, 5, 120, 32'h5000);
        gaps_ok = 1;
        for (i = 1; i < 120; i = i + 1) if (l_cyc[i] - l_cyc[i - 1] != 10) gaps_ok = 0;
        tb_check("paced: every interval == PERIOD", gaps_ok, 1);
        chk_reg("paced: LATE_CNT", O_LATE, 0);
        chk_reg("paced: UNDERRUN_CNT", O_UNDER, 0);

        // ---- paced, input starved inside a segment: counted once, flagged once, nothing lost ----
        reset_src(10, 0);
        send_frames(6, 0, 4, 1'b0);
        repeat (100) @(posedge clk);                     // starved for several periods
        send_frames(6, 4, 1, 1'b1);
        wait_rx(5);
        chk_reg("starved: UNDERRUN_CNT (one event)", O_UNDER, 1);
        for (i = 0; i < 5; i = i + 1) tb_check("starved: data", l_data[i], pat(6, i));
        tb_check("starved: no flag on the frame before", l_user[3][15:0], 16'h0000);
        tb_check("starved: UNDERRUN flag on the frame after", l_user[4][15:0], 16'h0008);
        tb_check("starved: timestamp continues", l_user[4][79:48], 4);

        // ---- paced, the input may be idle between segments: no underrun ----
        reset_src(10, 0);
        send_seg(8, 3);
        repeat (200) @(posedge clk);
        send_seg(9, 3);
        wait_rx(6);
        chk_reg("idle between segments: UNDERRUN_CNT", O_UNDER, 0);
        chk_seg("idle between segments: first", 0, 8, 3, 0);
        chk_seg("idle between segments: second", 3, 9, 3, 3);

        // ---- paced, the sink stalls: LATE_CNT, frame stays valid, nothing lost ----
        reset_src(10, 0);
        ready_pct = 0;
        repeat (2) @(negedge clk);
        bg_seg(10, 6);
        repeat (80) @(posedge clk);
        u_m.read(O_LATE, rv);
        tb_check("late: counted", rv >= 1, 1'b1);
        ready_pct = 100;
        wait_rx(6);
        bg_wait;
        chk_seg("late: nothing lost", 0, 10, 6, 0);

        // ---- RUN = 0 pauses without losing data ----
        reset_src(4, 0);
        bg_seg(11, 40);
        wait_rx(10);
        u_m.write(O_CTRL, 32'h0, 4'hF);
        repeat (10) @(posedge clk);
        i = n_rx;
        repeat (50) @(posedge clk);
        tb_check("RUN = 0: no frame launched", n_rx, i);
        u_m.read(O_ST, rv);
        tb_check("RUN = 0: STATUS.RUN", rv[0], 1'b0);
        u_m.write(O_CTRL, 32'h1, 4'hF);
        wait_rx(40);
        bg_wait;
        chk_seg("pause: data complete", 0, 11, 40, 0);

        // ---- SOFT_RESET with a frame waiting and one held: both dropped, next frame SOS, counters 0 ----
        reset_src(0, 0);
        ready_pct = 0;
        repeat (2) @(negedge clk);
        send_frames(12, 0, 2, 1'b0);
        repeat (3) @(posedge clk);
        tb_check("before SOFT_RESET: frame waiting", o_valid, 1'b1);
        u_m.write(O_CTRL, 32'h4, 4'hF);
        repeat (2) @(posedge clk);
        tb_check("SOFT_RESET: nothing valid", o_valid, 1'b0);
        chk_reg("SOFT_RESET: FRAME_CNT", O_FRAMES, 0);
        chk_reg("SOFT_RESET: CTRL.RUN", O_CTRL, 0);
        ready_pct = 100;
        reset_src(0, 32'h40);
        send_seg(13, 4);
        wait_rx(4);
        chk_seg("after SOFT_RESET", 0, 13, 4, 32'h40);

        // ---- sink reset with a frame waiting: not offered in reset, comes back as frame 0 with SOS ----
        reset_src(0, 0);
        send_frames(14, 0, 3, 1'b0);
        wait_rx(3);
        ready_pct = 0;
        repeat (2) @(negedge clk);
        send_frames(14, 3, 1, 1'b0);
        repeat (5) @(posedge clk);
        tb_check("sink reset: frame waiting", o_valid, 1'b1);
        #1 sink_aresetn = 1'b0;
        @(posedge clk); #1;
        tb_check("sink reset: not offered in reset", o_valid, 1'b0);
        u_m.read(O_ST, rv);
        tb_check("sink reset: STATUS.SINK_RESET", rv[4], 1'b1);
        repeat (5) @(posedge clk);
        #1 sink_aresetn = 1'b1;
        #1 tb_check("sink reset: not offered right after", o_valid, 1'b0);
        repeat (3) @(posedge clk);
        ready_pct = 100;
        send_frames(14, 4, 1, 1'b1);
        wait_rx(5);
        tb_check("sink reset: waiting frame data", l_data[3], pat(14, 3));
        tb_check("sink reset: waiting frame has SOS", l_user[3][15:0], 16'h0001);
        tb_check("sink reset: waiting frame restarts at 0", l_user[3][79:48], 0);
        tb_check("sink reset: next frame n = 1", l_user[4][79:48], 1);
        tb_check("sink reset: seq continues", l_user[4][47:16], 4);

        // ---- sink reset with nothing waiting: the next frame carries SOS ----
        reset_src(0, 0);
        send_frames(15, 0, 2, 1'b0);
        wait_rx(2);
        repeat (3) @(posedge clk);
        #1 sink_aresetn = 1'b0;
        repeat (3) @(posedge clk);
        #1 sink_aresetn = 1'b1;
        repeat (3) @(posedge clk);
        send_frames(15, 2, 1, 1'b1);
        wait_rx(3);
        tb_check("sink reset, idle: next frame SOS", l_user[2][15:0], 16'h0001);
        tb_check("sink reset, idle: n = 0", l_user[2][79:48], 0);

        // ---- bad TKEEP counted, data passed ----
        reset_src(0, 0);
        i_keep = 8'h0F;
        send_seg(16, 2);
        i_keep = 8'hFF;
        wait_rx(2);
        chk_reg("KEEP_ERR_CNT", O_KEEP, 2);

        tb_check("ESI protocol errors", mon_err, 0);
        tb_check("lanes 4..7 are zero", up_err, 0);
        tb_check("control page: bad write responses", u_m.bresp_err, 0);
        tb_check("control page: bad read responses", u_m.rresp_err, 0);
        tb_finish;
    end
endmodule

`default_nettype wire
