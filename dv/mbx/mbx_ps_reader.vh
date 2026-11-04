// mbx_ps_reader.vh - PS-side consumer of mailbox v1 for the test benches (rules of
// host/ps_mailbox/protocol.py, same algorithm as host/ps_mailbox/pynq_reader.py).
// Included inside the test bench module, which provides:
//   clk, cyc (integer cycle counter), ps_rd(off, v) / ps_wr(off, v) (window offset,
//   through the PS-side AXI master), rd_content(...) (content check of one record)
// Plusargs
//   +rd_mode=1        run the consumer (0: no consumer, the producer finds no reader)
//   +rd_gap=N         cycles between polls (default 20)
//   +rd_rec_gap=N     cycles after each consumed record (slow consumer)
//   +rd_start=N       cycles after reset before the first poll (late attach)
//   +rd_policy=0|1    first attach to an epoch: 0 backlog (EPOCH_START_INDEX), 1 live (PRODUCER_INDEX)
//   +rd_restart_after=N  forget the reader state after N records (reader restart: resume
//                     at CONSUMER_INDEX of the same epoch)
//   +rd_hammer=N      collision stress: after every poll N times read HEARTBEAT (written by the
//                     CPU while it waits) and rewrite CONSUMER_INDEX with its current value
//                     (read by the CPU before every publish); both are legal accesses
// Every record: REC_HDR magic/version, PUBLISH_INDEX == consumer index, RESET_EPOCH ==
// attached epoch, REC_HDR and PUBLISH_INDEX unchanged when read again after the payload
// (seqlock), RESULT_INDEX +1 (a larger step only with F_DROP_BEFORE, counted as gap),
// ARGMAX == argmax(scores) (ties -> lowest), GESTURE == ARGMAX (K = 1, T = 0), flags.

    integer rd_exp_state = `MBX_STATE_DONE;            // a live image (firmware/rv32/dma) stays RUNNING
    integer rd_mode = 0, rd_gap = 20, rd_rec_gap = 0, rd_start = 0, rd_policy = 0, rd_restart_after = 0;
    integer rd_hammer = 0, rd_hi;
    reg [31:0] rd_hv;
    integer rd_rx = 0, rd_rx_epoch = 0, rd_err = 0, rd_gaps = 0, rd_resync = 0, rd_retry = 0;
    integer rd_restarts = 0, rd_polls = 0, rd_drop_flags = 0, rd_dup = 0, rd_rereads = 0, rd_resumes = 0;
    integer rd_t_first = -1, rd_t_last = -1;
    reg        rd_stop = 1'b0, rd_attached = 1'b0, rd_have_last = 1'b0, rd_running = 1'b0;
    integer    rd_idle_polls = 0;
    reg [31:0] rd_epoch = 32'h0, rd_cons = 32'h0, rd_last_res = 32'h0;
    reg [31:0] rw [0:15];

    initial begin
        if (!$value$plusargs("rd_mode=%d", rd_mode)) rd_mode = 0;
        if (!$value$plusargs("rd_gap=%d", rd_gap)) rd_gap = 20;
        if (!$value$plusargs("rd_rec_gap=%d", rd_rec_gap)) rd_rec_gap = 0;
        if (!$value$plusargs("rd_start=%d", rd_start)) rd_start = 0;
        if (!$value$plusargs("rd_policy=%d", rd_policy)) rd_policy = 0;
        if (!$value$plusargs("rd_restart_after=%d", rd_restart_after)) rd_restart_after = 0;
        if (!$value$plusargs("rd_hammer=%d", rd_hammer)) rd_hammer = 0;
    end

    task rd_error(input [8*96-1:0] what);
        begin
            rd_err = rd_err + 1;
            if (rd_err <= 10) $display("ERROR: PS reader: %0s (cons=%0d epoch=%0d t=%0t)", what, rd_cons, rd_epoch, $time);
        end
    endtask

    // two consecutive equal reads (a word the producer writes in the same cycle may read stale)
    task rd_stable(input [31:0] off, output [31:0] v);
        reg [31:0] a, b;
        integer t;
        begin
            ps_rd(off, a);
            ps_rd(off, b);
            t = 0;
            while (a !== b && t < 8) begin
                a = b;
                ps_rd(off, b);
                t = t + 1;
                rd_retry = rd_retry + 1;
            end
            if (a !== b) rd_error("word never stable");
            v = b;
        end
    endtask

    // consistent header snapshot: epoch / state read before and after PRODUCER_INDEX
    task rd_header(output ok, output [31:0] epoch, output [31:0] prod);
        reg [31:0] m, ver, e1, s1, e2, s2;
        begin
            rd_stable(`MBX_H_MAGIC, m);
            rd_stable(`MBX_H_VERSION, ver);
            rd_stable(`MBX_H_RESET_EPOCH, e1);
            rd_stable(`MBX_H_STATE, s1);
            rd_stable(`MBX_H_PRODUCER_INDEX, prod);
            rd_stable(`MBX_H_RESET_EPOCH, e2);
            rd_stable(`MBX_H_STATE, s2);
            epoch = e2;
            ok = m == `MBX_MAGIC && ver[31:16] == `MBX_VERSION >> 16 && e1 == e2 && s1 == s2 &&
                 (s1 == `MBX_STATE_RUNNING || s1 == `MBX_STATE_DONE);
        end
    endtask

    // attach to epoch e: resume at CONSUMER_INDEX if this consumer already owned the epoch
    // (reader restart), else backlog / live by +rd_policy
    task rd_attach(input [31:0] e, input [31:0] prod);
        reg [31:0] ce, ci, st;
        begin
            rd_stable(`MBX_H_CONSUMER_EPOCH, ce);
            rd_stable(`MBX_H_CONSUMER_INDEX, ci);
            rd_stable(`MBX_H_EPOCH_START_INDEX, st);
            if (ce == e && prod - ci <= `MBX_RING_SIZE) begin
                rd_cons = ci;
                rd_resumes = rd_resumes + 1;
            end else begin
                rd_cons = rd_policy ? prod : st;
                ps_wr(`MBX_H_CONSUMER_INDEX, rd_cons);
                ps_wr(`MBX_H_CONSUMED_CNT, 32'd0);
                ps_wr(`MBX_H_CONSUMER_EPOCH, e);
                rd_rx_epoch = 0;
            end
            rd_epoch = e;
            rd_attached = 1'b1;
            rd_have_last = 1'b0;
        end
    endtask

    // read and check record rd_cons; ok = 1: consumed. ok = 0 and changed = 1: the epoch
    // changed under the reader (producer restarted), resynchronise without error.
    task rd_one(output ok, output changed);
        reg [31:0] base, h2, p2, e_now;
        reg [31:0] flags;
        integer w, tries, best, c;
        reg valid, cok;
        begin
            base = `MBX_RING_OFFSET + (rd_cons % `MBX_RING_SIZE) * `MBX_RECORD_SIZE;
            tries = 0; valid = 1'b0; changed = 1'b0;
            while (!valid && tries < 3) begin
                for (w = 0; w < 16; w = w + 1) ps_rd(base + 4 * w, rw[w]);
                ps_rd(base, h2);
                ps_rd(base + 4, p2);
                valid = rw[0][31:16] == `MBX_REC_MAGIC && rw[0][15:8] == `MBX_REC_VERSION &&
                        rw[1] == rd_cons && rw[2] == rd_epoch && h2 == rw[0] && p2 == rw[1];
                if (!valid) begin
                    tries = tries + 1;
                    rd_rereads = rd_rereads + 1;
                    rd_stable(`MBX_H_RESET_EPOCH, e_now);
                    if (e_now != rd_epoch) begin changed = 1'b1; tries = 3; end
                end
            end
            ok = valid;
            if (!valid && !changed) begin
                rd_error("record invalid after 3 reads (torn / unpublished)");
                $display("ERROR:   words %h %h %h %h re-read %h %h", rw[0], rw[1], rw[2], rw[3], h2, p2);
            end
            if (valid) begin
                flags = rw[0] & 32'hFF;
                if (rd_have_last) begin
                    if (rw[3] == rd_last_res + 1) begin end
                    else if (rw[3] > rd_last_res + 1 && (flags & `MBX_F_DROP_BEFORE))
                        rd_gaps = rd_gaps + (rw[3] - rd_last_res - 1);
                    else if (rw[3] <= rd_last_res) begin rd_dup = rd_dup + 1; rd_error("RESULT_INDEX not increasing (duplicate)"); end
                    else rd_error("RESULT_INDEX gap without F_DROP_BEFORE");
                end
                if (flags & `MBX_F_DROP_BEFORE) rd_drop_flags = rd_drop_flags + 1;
                if ((flags & (`MBX_F_POSTPROC | `MBX_F_ARGMAX_OK)) != (`MBX_F_POSTPROC | `MBX_F_ARGMAX_OK))
                    rd_error("flags POSTPROC / ARGMAX_OK missing");
                best = 0;
                for (c = 1; c < 6; c = c + 1)
                    if ($signed(rw[7 + c]) > $signed(rw[7 + best])) best = c;
                if (rw[6][15:8] != best) rd_error("ARGMAX does not belong to the scores");
                if (rw[6][7:0] != rw[6][15:8]) rd_error("GESTURE != ARGMAX (K = 1, T = 0)");
                if (rw[15] - rw[14] == 0 || rw[15] - rw[14] > 32'd5000) rd_error("PUBLISH_CYCLE - DETECT_CYCLE implausible");
                rd_content(cok);
                if (!cok) rd_error("record content differs from the E2 golden decision");
                $display("MBX_RX: epoch=%0d pub=%0d res=%0d seq=%0d ts=%0d argmax=%0d gesture=%0d flags=%02h lat=%0d detect=%0d publish=%0d rx_cyc=%0d",
                         rd_epoch, rw[1], rw[3], rw[4], rw[5], rw[6][15:8], $signed(rw[6][7:0]), flags,
                         rw[13], rw[14], rw[15], cyc);
                if (rd_t_first < 0) rd_t_first = cyc;
                rd_t_last = cyc;
                rd_last_res = rw[3];
                rd_have_last = 1'b1;
                rd_cons = rd_cons + 1;
                ps_wr(`MBX_H_CONSUMER_INDEX, rd_cons);
                rd_rx = rd_rx + 1;
                rd_rx_epoch = rd_rx_epoch + 1;
                ps_wr(`MBX_H_CONSUMED_CNT, rd_rx_epoch);
            end
        end
    endtask

    reg        rd_ok, rd_chg;
    reg [31:0] rd_e, rd_p, rd_avail;
    initial begin
        @(posedge clk);
        if (rd_mode) begin
            rd_running = 1'b1;
            repeat (rd_start + 20) @(posedge clk);
            while (!rd_stop) begin
                rd_header(rd_ok, rd_e, rd_p);
                if (rd_ok) begin
                    if (!rd_attached || rd_e != rd_epoch) begin
                        if (rd_attached) rd_resync = rd_resync + 1;
                        rd_attach(rd_e, rd_p);
                    end
                    rd_avail = rd_p - rd_cons;
                    if (rd_avail > `MBX_RING_SIZE) begin
                        rd_error("PRODUCER_INDEX - CONSUMER_INDEX > RING_SIZE");
                        rd_attached = 1'b0;
                        rd_avail = 0;
                    end
                    rd_idle_polls = (rd_avail == 0) ? rd_idle_polls + 1 : 0;
                    while (rd_avail > 0 && !rd_stop) begin
                        rd_one(rd_ok, rd_chg);
                        if (!rd_ok) begin
                            rd_avail = 0;
                            if (rd_chg) rd_attached = 1'b0;
                        end else begin
                            rd_avail = rd_avail - 1;
                            if (rd_restart_after > 0 && rd_rx == rd_restart_after) begin
                                rd_attached = 1'b0;              // reader restart: state forgotten
                                rd_restarts = rd_restarts + 1;
                                rd_avail = 0;
                            end
                            repeat (rd_rec_gap) @(posedge clk);
                        end
                    end
                end
                if (rd_attached)
                    for (rd_hi = 0; rd_hi < rd_hammer; rd_hi = rd_hi + 1) begin
                        rd_stable(`MBX_H_HEARTBEAT, rd_hv);
                        ps_wr(`MBX_H_CONSUMER_INDEX, rd_cons);
                    end
                ps_wr(`MBX_H_CONSUMER_HEARTBEAT, rd_polls);
                rd_polls = rd_polls + 1;
                repeat (rd_gap) @(posedge clk);
            end
            rd_running = 1'b0;
        end
    end

    // final reader checks: exp_* < 0 = not checked
    task rd_report(input integer exp_rx, input integer exp_gaps, input integer exp_resync);
        reg [31:0] pub, drop, res, cons_cnt, st, err;
        begin
            ps_rd(`MBX_H_PUBLISHED_CNT, pub);
            ps_rd(`MBX_H_DROP_CNT, drop);
            ps_rd(`MBX_H_RESULT_CNT, res);
            ps_rd(`MBX_H_CONSUMED_CNT, cons_cnt);
            ps_rd(`MBX_H_STATE, st);
            ps_rd(`MBX_H_ERROR_CNT, err);
            $display("MBX_READER: rx=%0d rx_epoch=%0d gaps=%0d drop_flags=%0d resync=%0d resumes=%0d restarts=%0d stable_retries=%0d rereads=%0d polls=%0d errors=%0d",
                     rd_rx, rd_rx_epoch, rd_gaps, rd_drop_flags, rd_resync, rd_resumes, rd_restarts, rd_retry,
                     rd_rereads, rd_polls, rd_err);
            $display("MBX_HEADER: epoch=%0d state=%0d result_cnt=%0d published=%0d drop=%0d errors=%0d consumed=%0d",
                     rd_epoch, st, res, pub, drop, err, cons_cnt);
            tb_check("PS reader errors", rd_err, 0);
            tb_check("producer ERROR_CNT", err, 0);
            tb_check("STATE at the end", st, rd_exp_state);
            tb_check("published + dropped == results", pub + drop, res);
            tb_check("CONSUMED_CNT == records read in this epoch", cons_cnt, rd_rx_epoch);
            if (rd_restarts == 0 && rd_policy == 0) tb_check("records read in this epoch == PUBLISHED_CNT", rd_rx_epoch, pub);
            // dropped results show up as RESULT_INDEX gaps before a later record, or after the
            // last published one (results numbered 1..RESULT_CNT in the epoch)
            if (rd_restarts == 0 && rd_policy == 0)
                tb_check("gaps + results after the last record == DROP_CNT",
                         rd_gaps + (rd_have_last ? res - rd_last_res : res), drop);
            if (exp_rx >= 0) tb_check("records read", rd_rx, exp_rx);
            if (exp_gaps >= 0) tb_check("RESULT_INDEX gaps", rd_gaps, exp_gaps);
            if (exp_resync >= 0) tb_check("epoch resynchronisations", rd_resync, exp_resync);
        end
    endtask
