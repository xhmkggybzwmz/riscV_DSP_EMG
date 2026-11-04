// mbx_monitor.vh - independent checker of the producer side of mailbox v1 (included in
// the test bench module). Watches every write of BRAM port A (the RV32I through
// cmi_bram_port): mon_a_en, mon_a_we, mon_a_addr, mon_a_wdata, and the consumer words
// the PS last wrote (mon_ps_cons, mon_ps_epoch, updated by the bench's ps_wr when the
// write is issued, i.e. no later than the BRAM write itself).
// Checks
//   - the CPU writes only CPU-owned words: header 0x000-0x03F and the ring
//   - a record slot: REC_HDR = 0 first, then payload words 1..15, then the commit
//     (REC_HDR != 0) only after all 15 payload words were written since the invalidation
//   - PRODUCER_INDEX (state RUNNING) only +1, and slot (v - 1) holds the committed record
//     with PUBLISH_INDEX = v - 1
//   - no overwrite: a record with PUBLISH_INDEX i is written only when
//     i - consumer < RING_SIZE (consumer = CONSUMER_INDEX of the attached epoch, else
//     EPOCH_START_INDEX)
//   - STATE: INIT -> RUNNING -> DONE (a new epoch starts at INIT again)

    integer mon_err = 0, mon_pub = 0, mon_inval = 0, mon_epochs = 0, mon_off = 0;
    initial if (!$value$plusargs("mon_off=%d", mon_off)) mon_off = 0;   // raw window tests
    reg [31:0] mon_state = 32'hFFFF_FFFF, mon_epoch = 32'h0, mon_start = 32'h0, mon_prod = 32'h0;
    reg [31:0] mon_ps_cons = 32'h0, mon_ps_epoch = 32'h0;
    reg [15:0] mon_mask [0:`MBX_RING_SIZE-1];
    reg        mon_open [0:`MBX_RING_SIZE-1];
    reg        mon_done [0:`MBX_RING_SIZE-1];
    reg [31:0] mon_pidx [0:`MBX_RING_SIZE-1];
    integer mi;
    initial for (mi = 0; mi < `MBX_RING_SIZE; mi = mi + 1) begin
        mon_mask[mi] = 0; mon_open[mi] = 0; mon_done[mi] = 0; mon_pidx[mi] = 0;
    end

    task mon_error(input [8*80-1:0] what);
        begin
            mon_err = mon_err + 1;
            if (mon_err <= 10) $display("ERROR: mailbox monitor: %0s (addr=%h data=%h t=%0t)", what,
                                        mon_a_addr, mon_a_wdata, $time);
        end
    endtask

    reg [31:0] mo, mc;
    integer ms, mw;
    always @(posedge clk) if (mon_a_en && |mon_a_we && !mon_off) begin
        mo = mon_a_addr & (`MBX_SIZE - 1);
        if (mon_a_we != 4'hF) mon_error("partial-word write");
        if (mo >= `MBX_RING_OFFSET && mo < `MBX_RING_OFFSET + `MBX_RING_SIZE * `MBX_RECORD_SIZE) begin
            ms = (mo - `MBX_RING_OFFSET) / `MBX_RECORD_SIZE;
            mw = (mo % `MBX_RECORD_SIZE) / 4;
            if (mw == 0 && mon_a_wdata == 0) begin
                mon_open[ms] = 1; mon_done[ms] = 0; mon_mask[ms] = 0;
                mon_inval = mon_inval + 1;
            end else if (mw == 0) begin
                if (!mon_open[ms]) mon_error("commit without invalidation");
                else if (mon_mask[ms][15:1] != 15'h7FFF) mon_error("commit before all payload words were written");
                mon_open[ms] = 0; mon_done[ms] = 1;
            end else begin
                if (!mon_open[ms]) mon_error("payload write to a slot that was not invalidated");
                mon_mask[ms][mw] = 1'b1;
                if (mw == 1) begin
                    mon_pidx[ms] = mon_a_wdata;
                    mc = (mon_ps_epoch == mon_epoch) ? mon_ps_cons : mon_start;
                    if (mon_a_wdata - mc >= `MBX_RING_SIZE) mon_error("record overwrites an unconsumed slot");
                end
            end
        end else if (mo < 32'h40) begin
            case (mo)
                `MBX_H_STATE: begin
                    if (mon_a_wdata == `MBX_STATE_INIT) mon_epochs = mon_epochs + 1;
                    else if (mon_a_wdata == `MBX_STATE_RUNNING && mon_state != `MBX_STATE_INIT) mon_error("RUNNING not after INIT");
                    else if (mon_a_wdata == `MBX_STATE_DONE && mon_state != `MBX_STATE_RUNNING) mon_error("DONE not after RUNNING");
                    mon_state = mon_a_wdata;
                end
                `MBX_H_RESET_EPOCH: mon_epoch = mon_a_wdata;
                `MBX_H_EPOCH_START_INDEX: mon_start = mon_a_wdata;
                `MBX_H_PRODUCER_INDEX: begin
                    if (mon_state == `MBX_STATE_RUNNING) begin
                        ms = (mon_a_wdata - 1) % `MBX_RING_SIZE;
                        if (mon_a_wdata != mon_prod + 1) mon_error("PRODUCER_INDEX not +1");
                        if (!mon_done[ms] || mon_pidx[ms] != mon_a_wdata - 1) mon_error("PRODUCER_INDEX published before its record");
                        mon_pub = mon_pub + 1;
                    end else if (mon_state != `MBX_STATE_INIT) mon_error("PRODUCER_INDEX written outside INIT/RUNNING");
                    mon_prod = mon_a_wdata;
                end
                default: ;
            endcase
        end else
            mon_error("CPU write outside the CPU-owned words");
    end
