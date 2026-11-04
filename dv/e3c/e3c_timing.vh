// e3c_timing.vh - stream integrity and real-time measurements of tb_e3c_soc (included
// inside the module after the observation wires). Active with +period_check=P (> 0).
//
// Per frame (index i since the replay's last reset): cycle of the replay -> DSP
// handshake t_rep[i] and of the DSP -> CNN handshake t_cnn[i]. Per CNN result n:
//   end    window end sample index (core res_end; one segment)
//   t_res  cycle of the core result (res_event)
//   t_poll first CPU read of CNN STATUS that sees RESULT_PENDING after t_res
//   t_ack  cycle of the RESULT_ACK write that removes it
// Reported per result:
//   (1) sample-time interval   t_rep[end_n] - t_rep[end_n-1]       (input sampling instants)
//   (2) result-valid interval  t_res[n] - t_res[n-1]               (FPGA classification outputs)
//   (3) compute latency        t_res[n] - t_cnn[end_n]             (last frame at the CNN -> result)
//   (4) CPU polling latency    t_poll[n] - t_res[n], ACK at t_ack[n] - t_res[n]
// Checks: every replay interval == P; no duplicated / skipped frame on either link
// (sequence numbers +1, both links carry the same frames), no GAP flag, SOS on frame 0
// only; first window end = 399, then +100; (1) == 100 * P exactly; (2) within (1) +- 200.

    integer tm_period = 0;
    initial if (!$value$plusargs("period_check=%d", tm_period)) tm_period = 0;
    integer tm_cyc = 0, tm_nrep = 0, tm_ncnn = 0, tm_nres = 0, tm_bad_iv = 0, tm_bad_seq = 0, tm_gap = 0, tm_sos = 0;
    integer t_rep [0:4095];
    integer t_cnn [0:4095];
    integer t_res [0:63], t_poll [0:63], t_ack [0:63], r_end [0:63];
    reg [31:0] tm_seq_rep, tm_seq_cnn;
    wire tm_rep_hs = `E3C.tap_rep_valid & `E3C.tap_rep_ready;
    wire tm_cnn_hs = `E3C.tap_dsp_valid & `E3C.tap_dsp_ready;
    wire [79:0] tm_rep_user = `E3C.tap_rep_user;
    wire [79:0] tm_cnn_user = `E3C.tap_dsp_user;
    integer tm_ack_pend = 0;

    always @(posedge clk) begin
        tm_cyc = tm_cyc + 1;
        if (tm_period > 0) begin
            // a run starts with frame timestamp 0: restart the record (the firmware resets
            // the pipeline before and after its run, so the record keeps the last run)
            if (tm_rep_hs && tm_rep_user[79:48] == 32'd0) begin
                tm_nrep = 0; tm_nres = 0; tm_ack_pend = 0; tm_bad_iv = 0; tm_bad_seq = 0; tm_gap = 0; tm_sos = 0;
            end
            if (tm_cnn_hs && tm_cnn_user[79:48] == 32'd0) tm_ncnn = 0;
            if (tm_rep_hs) begin
                if (tm_nrep > 0 && tm_cyc - t_rep[tm_nrep - 1] != tm_period) tm_bad_iv = tm_bad_iv + 1;
                if (tm_nrep > 0 && tm_rep_user[47:16] !== tm_seq_rep + 1) tm_bad_seq = tm_bad_seq + 1;
                if (tm_rep_user[1]) tm_gap = tm_gap + 1;
                if (tm_rep_user[0]) tm_sos = tm_sos + 1;
                tm_seq_rep = tm_rep_user[47:16];
                if (tm_nrep < 4096) t_rep[tm_nrep] = tm_cyc;
                tm_nrep = tm_nrep + 1;
            end
            if (tm_cnn_hs) begin
                if (tm_ncnn > 0 && tm_cnn_user[47:16] !== tm_seq_cnn + 1) tm_bad_seq = tm_bad_seq + 1;
                if (tm_cnn_user[1]) tm_gap = tm_gap + 1;
                tm_seq_cnn = tm_cnn_user[47:16];
                if (tm_ncnn < 4096) t_cnn[tm_ncnn] = tm_cyc;
                tm_ncnn = tm_ncnn + 1;
            end
            if (res_event && tm_nres < 64) begin
                t_res[tm_nres] = tm_cyc; r_end[tm_nres] = `E3C.u_cnn.u_cnn.res_end;
                t_poll[tm_nres] = -1; t_ack[tm_nres] = -1;
                tm_nres = tm_nres + 1;
            end
            // CPU reads CNN STATUS (offset 0x00C) while a result is pending
            if (`E3C.u_cnn.rd_en && `E3C.u_cnn.rd_word == 10'h003 && `E3C.u_cnn.pending &&
                tm_ack_pend < tm_nres && t_poll[tm_ack_pend] < 0)
                t_poll[tm_ack_pend] = tm_cyc;
            // RESULT_ACK (offset 0x010) removes the oldest pending result
            if (`E3C.u_cnn.wr_en && `E3C.u_cnn.wr_word == 10'h004 && `E3C.u_cnn.pending && tm_ack_pend < tm_nres) begin
                t_ack[tm_ack_pend] = tm_cyc;
                tm_ack_pend = tm_ack_pend + 1;
            end
        end
    end

    integer tm_k, tm_d1, tm_d2, tm_lat, tm_cpu, tm_bad1 = 0, tm_bad2 = 0, tm_bad_end = 0;
    task tm_report;
        input integer exp_results;
        begin
            $display("TIMING: period=%0d frames rep=%0d cnn=%0d results=%0d", tm_period, tm_nrep, tm_ncnn, tm_nres);
            for (tm_k = 0; tm_k < tm_nres; tm_k = tm_k + 1) begin
                tm_lat = t_res[tm_k] - t_cnn[r_end[tm_k]];
                tm_cpu = (t_poll[tm_k] >= 0) ? t_poll[tm_k] - t_res[tm_k] : -1;
                if (tm_k > 0) begin
                    tm_d1 = t_rep[r_end[tm_k]] - t_rep[r_end[tm_k - 1]];
                    tm_d2 = t_res[tm_k] - t_res[tm_k - 1];
                    if (tm_d1 != 100 * tm_period) tm_bad1 = tm_bad1 + 1;
                    if (tm_d2 < tm_d1 - 200 || tm_d2 > tm_d1 + 200) tm_bad2 = tm_bad2 + 1;
                end else begin
                    tm_d1 = -1; tm_d2 = -1;
                end
                if (r_end[tm_k] != 399 + 100 * tm_k) tm_bad_end = tm_bad_end + 1;
                $display("TIMING: result %0d end=%0d t_sample=%0d t_cnn_in=%0d t_result=%0d d_sample=%0d d_result=%0d latency=%0d cpu_poll=%0d ack=%0d",
                         tm_k, r_end[tm_k], t_rep[r_end[tm_k]], t_cnn[r_end[tm_k]], t_res[tm_k], tm_d1, tm_d2, tm_lat,
                         tm_cpu, (t_ack[tm_k] >= 0) ? t_ack[tm_k] - t_res[tm_k] : -1);
            end
            tb_check("2k: replay frames", tm_nrep, `E3C_REPLAY_FRAMES);
            tb_check("2k: frames delivered to the CNN", tm_ncnn, `E3C_REPLAY_FRAMES);
            tb_check("2k: every replay interval == PERIOD", tm_bad_iv, 0);
            tb_check("2k: no duplicated or skipped frame (seq +1 on both links)", tm_bad_seq, 0);
            tb_check("2k: no GAP flag", tm_gap, 0);
            tb_check("2k: SOS on frame 0 only", tm_sos, 1);
            tb_check("2k: results", tm_nres, exp_results);
            tb_check("2k: window ends 399, 499, ...", tm_bad_end, 0);
            tb_check("2k: sample-time interval == 100 * PERIOD", tm_bad1, 0);
            tb_check("2k: result-valid interval == sample interval +- 200", tm_bad2, 0);
            tb_check("2k: every result polled and acknowledged", tm_ack_pend, tm_nres);
        end
    endtask
