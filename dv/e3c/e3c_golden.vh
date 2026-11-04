// e3c_golden.vh - golden-vector checker shared by the E3c testbenches (included
// inside a module). Same comparison as dv/e3/tb_emg_cnn.v: every CNN debug event
// (conv1..3 activation and accumulator, GAP sum, FC score) and every DSP output
// frame is compared, element by element, with the E2 reference files of one
// build/e3/vec/chain_* case. Expects in the including module:
//   clk; dbg_valid, dbg_layer, dbg_ch, dbg_step, dbg_seg, dbg_acc, dbg_act;
//   dsp_hs (DSP output handshake), dsp_data (its tdata); g_check_on (1 = check; keep it on until
//   the first frame of a later pass has been accepted by the CNN, n_dsp_hs <= nframes)
// Provides: g_load(vec, use_acc) task, g_errs, counters p1 p2 p3 pg pf pd, the
// arrays g_dec / g_log / frames (for result checks) and seg_start[].

    reg [175:0]  frames [0:39999];
    reg [159:0]  g_act1 [0:19999];
    reg [287:0]  g_act2 [0:9999];
    reg [287:0]  g_act3 [0:1999];
    reg [543:0]  g_acc1 [0:19999];
    reg [1055:0] g_acc2 [0:9999];
    reg [1055:0] g_acc3 [0:1999];
    reg [543:0]  g_gap  [0:999];
    reg [223:0]  g_log  [0:999];
    reg [71:0]   g_dec  [0:999];
    reg [127:0]  g_dsp  [0:39999];
    integer seg_start [0:255];
    integer g_nframes, g_n1, g_n2, g_n3, g_ngap, g_nlog, g_ndsp, g_use_acc, g_nseg;
    integer p1 = 0, p2 = 0, p3 = 0, pg = 0, pf = 0, pd = 0, g_errs = 0;

    task g_fail;
        input [8*24-1:0] what;
        input integer layer, ch, step, seg;
        input [31:0] exp, got;
        begin
            g_errs = g_errs + 1;
            if (g_errs <= 10)
                $display("ERROR: golden mismatch #%0d: %0s layer=%0d ch=%0d step=%0d seg=%0d exp=%h got=%h (t=%0t)",
                         g_errs, what, layer, ch, step, seg, exp, got, $time);
        end
    endtask

    function integer count_lines;                    // number of words in a hex file (stops at 0-word gap)
        input [8*512-1:0] f;
        integer fd, r;
        reg [8*600-1:0] line;
        begin
            count_lines = 0;
            fd = $fopen(f, "r");
            if (fd != 0) begin
                while (!$feof(fd)) begin
                    r = $fgets(line, fd);
                    if (r > 1) count_lines = count_lines + 1;
                end
                $fclose(fd);
            end
        end
    endfunction

    reg [8*512-1:0] g_fname;
    integer gi;
    task g_load;
        input [8*512-1:0] vec;
        input integer use_acc;
        begin
            g_use_acc = use_acc;
            $sformat(g_fname, "%0s/input_frames.hex", vec); g_nframes = count_lines(g_fname); $readmemh(g_fname, frames, 0, g_nframes - 1);
            $sformat(g_fname, "%0s/dsp_out.hex", vec);   g_ndsp = count_lines(g_fname); $readmemh(g_fname, g_dsp, 0, g_ndsp - 1);
            $sformat(g_fname, "%0s/conv1_act.hex", vec); g_n1 = count_lines(g_fname); $readmemh(g_fname, g_act1, 0, g_n1 - 1);
            $sformat(g_fname, "%0s/conv2_act.hex", vec); g_n2 = count_lines(g_fname); $readmemh(g_fname, g_act2, 0, g_n2 - 1);
            $sformat(g_fname, "%0s/conv3_act.hex", vec); g_n3 = count_lines(g_fname); $readmemh(g_fname, g_act3, 0, g_n3 - 1);
            if (use_acc) begin
                $sformat(g_fname, "%0s/conv1_acc.hex", vec); $readmemh(g_fname, g_acc1, 0, g_n1 - 1);
                $sformat(g_fname, "%0s/conv2_acc.hex", vec); $readmemh(g_fname, g_acc2, 0, g_n2 - 1);
                $sformat(g_fname, "%0s/conv3_acc.hex", vec); $readmemh(g_fname, g_acc3, 0, g_n3 - 1);
            end
            $sformat(g_fname, "%0s/gap_sum.hex", vec);   g_ngap = count_lines(g_fname); $readmemh(g_fname, g_gap, 0, g_ngap - 1);
            $sformat(g_fname, "%0s/logits.hex", vec);    g_nlog = count_lines(g_fname); $readmemh(g_fname, g_log, 0, g_nlog - 1);
            $sformat(g_fname, "%0s/decisions.hex", vec); $readmemh(g_fname, g_dec, 0, g_nlog - 1);
            g_nseg = 0;
            for (gi = 0; gi < g_nframes; gi = gi + 1)
                if (frames[gi][128 + 0] || frames[gi][128 + 1]) begin   // SOS or GAP starts a segment
                    seg_start[g_nseg] = gi;
                    g_nseg = g_nseg + 1;
                end
            $display("INFO: golden %0s: frames=%0d dsp=%0d conv=%0d/%0d/%0d gap=%0d results=%0d segments=%0d",
                     vec, g_nframes, g_ndsp, g_n1, g_n2, g_n3, g_ngap, g_nlog, g_nseg);
        end
    endtask

    // ---------------- DSP output frames ----------------
    // (frames beyond the golden count belong to a later pass/run: the frame counters of the
    // pages are checked separately)
    always @(posedge clk) if (g_check_on && dsp_hs && pd < g_ndsp) begin
        if (dsp_data !== g_dsp[pd]) g_fail("DSP output frame", 0, 0, pd, 0, g_dsp[pd][31:0], dsp_data[31:0]);
        pd = pd + 1;
    end

    // ---------------- CNN debug trace ----------------
    reg [31:0] e_acc;
    reg [7:0]  e_act;
    reg [23:0] e_step;
    reg [7:0]  e_seg;
    always @(posedge clk) if (g_check_on && dbg_valid) begin
        case (dbg_layer)
            3'd0: begin
                e_act = g_act1[p1][8*dbg_ch +: 8]; e_step = g_act1[p1][128 +: 24]; e_seg = g_act1[p1][152 +: 8];
                e_acc = g_acc1[p1][32*dbg_ch +: 32];
                if (p1 >= g_n1) g_fail("conv1 extra output", 1, dbg_ch, dbg_step, dbg_seg, 0, 0);
                else begin
                    if (dbg_act !== e_act) g_fail("conv1 activation", 1, dbg_ch, e_step, e_seg, e_act, dbg_act);
                    if (g_use_acc && dbg_acc !== e_acc) g_fail("conv1 accumulator", 1, dbg_ch, e_step, e_seg, e_acc, dbg_acc);
                    if (dbg_step !== e_step || dbg_seg !== e_seg) g_fail("conv1 step/seg", 1, dbg_ch, e_step, e_seg, {e_seg, e_step}, {dbg_seg, dbg_step});
                end
                if (dbg_ch == 15) p1 = p1 + 1;
            end
            3'd1: begin
                e_act = g_act2[p2][8*dbg_ch +: 8]; e_step = g_act2[p2][256 +: 24]; e_seg = g_act2[p2][280 +: 8];
                e_acc = g_acc2[p2][32*dbg_ch +: 32];
                if (p2 >= g_n2) g_fail("conv2 extra output", 2, dbg_ch, dbg_step, dbg_seg, 0, 0);
                else begin
                    if (dbg_act !== e_act) g_fail("conv2 activation", 2, dbg_ch, e_step, e_seg, e_act, dbg_act);
                    if (g_use_acc && dbg_acc !== e_acc) g_fail("conv2 accumulator", 2, dbg_ch, e_step, e_seg, e_acc, dbg_acc);
                    if (dbg_step !== e_step || dbg_seg !== e_seg) g_fail("conv2 step/seg", 2, dbg_ch, e_step, e_seg, {e_seg, e_step}, {dbg_seg, dbg_step});
                end
                if (dbg_ch == 31) p2 = p2 + 1;
            end
            3'd2: begin
                e_act = g_act3[p3][8*dbg_ch +: 8]; e_step = g_act3[p3][256 +: 24]; e_seg = g_act3[p3][280 +: 8];
                e_acc = g_acc3[p3][32*dbg_ch +: 32];
                if (p3 >= g_n3) g_fail("conv3 extra output", 3, dbg_ch, dbg_step, dbg_seg, 0, 0);
                else begin
                    if (dbg_act !== e_act) g_fail("conv3 activation", 3, dbg_ch, e_step, e_seg, e_act, dbg_act);
                    if (g_use_acc && dbg_acc !== e_acc) g_fail("conv3 accumulator", 3, dbg_ch, e_step, e_seg, e_acc, dbg_acc);
                    if (dbg_step !== e_step || dbg_seg !== e_seg) g_fail("conv3 step/seg", 3, dbg_ch, e_step, e_seg, {e_seg, e_step}, {dbg_seg, dbg_step});
                end
                if (dbg_ch == 31) p3 = p3 + 1;
            end
            3'd3: begin
                e_acc = {16'd0, g_gap[pg][16*dbg_ch +: 16]}; e_step = g_gap[pg][512 +: 24]; e_seg = g_gap[pg][536 +: 8];
                if (pg >= g_ngap) g_fail("GAP extra output", 4, dbg_ch, dbg_step, dbg_seg, 0, 0);
                else begin
                    if (dbg_acc !== e_acc) g_fail("GAP sum", 4, dbg_ch, e_step, e_seg, e_acc, dbg_acc);
                    if (dbg_step !== e_step || dbg_seg !== e_seg) g_fail("GAP end/seg", 4, dbg_ch, e_step, e_seg, {e_seg, e_step}, {dbg_seg, dbg_step});
                end
                if (dbg_ch == 31) pg = pg + 1;
            end
            default: begin
                e_acc = g_log[pf][32*dbg_ch +: 32]; e_step = g_log[pf][192 +: 24]; e_seg = g_log[pf][216 +: 8];
                if (pf >= g_nlog) g_fail("FC extra output", 5, dbg_ch, dbg_step, dbg_seg, 0, 0);
                else begin
                    if (dbg_acc !== e_acc) g_fail("FC score", 5, dbg_ch, e_step, e_seg, e_acc, dbg_acc);
                    if (dbg_step !== e_step || dbg_seg !== e_seg) g_fail("FC end/seg", 5, dbg_ch, e_step, e_seg, {e_seg, e_step}, {dbg_seg, dbg_step});
                end
                if (dbg_ch == 5) pf = pf + 1;
            end
        endcase
    end

    // all golden data consumed exactly
    task g_check_complete;
        begin
            tb_check("DSP frames compared == golden", pd, g_ndsp);
            tb_check("conv1 outputs == golden", p1, g_n1);
            tb_check("conv2 outputs == golden", p2, g_n2);
            tb_check("conv3 outputs == golden", p3, g_n3);
            tb_check("GAP sums == golden", pg, g_ngap);
            tb_check("FC score vectors == golden", pf, g_nlog);
            tb_check("golden mismatches", g_errs, 0);
        end
    endtask
