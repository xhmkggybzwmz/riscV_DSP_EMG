`timescale 1ns / 1ps
`default_nettype none

// tb_rv32i_lsu - self-checking unit test for rtl/cpu/rv32i_lsu.v
//
// Checks against docs/cpu/cpu_mem_interface.md section 3 in two ways:
//   a) interface view: exact wstrb / wdata / load_data / misaligned values;
//   b) memory view: a byte-addressed memory model is written with the DUT's
//      wstrb/wdata and read back with the DUT's load path; the bytes at
//      address..address+size-1 must equal the stored data and every other
//      byte must be untouched (independent of the lane-replication choice).
// Exhaustive over funct3 (8) x addr[1:0] (4) x is_store (2) x data patterns,
// plus random vectors.

module tb_rv32i_lsu;

    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg  [2:0]  funct3;
    reg  [1:0]  addr_lo;
    reg         is_store;
    reg  [31:0] store_data, load_word;
    wire        misaligned;
    wire [3:0]  wstrb;
    wire [31:0] wdata, load_data;

    rv32i_lsu dut (
        .funct3(funct3), .addr_lo(addr_lo), .is_store(is_store),
        .store_data(store_data), .load_word(load_word),
        .misaligned(misaligned), .wstrb(wstrb), .wdata(wdata), .load_data(load_data)
    );

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_rv32i_lsu); end
`endif

    reg [8*64-1:0] msg;

    // ---------------- reference model (byte-array based) ----------------
    function integer nbytes;            // access size in bytes, 0 = invalid size
        input [2:0] f3;
        begin
            case (f3[1:0])
                2'b00:   nbytes = 1;
                2'b01:   nbytes = 2;
                2'b10:   nbytes = 4;
                default: nbytes = 0;
            endcase
        end
    endfunction

    function ref_misaligned;
        input [2:0] f3; input [1:0] a;
        begin
            ref_misaligned = (nbytes(f3) != 0) && ((a % nbytes(f3)) != 0);
        end
    endfunction

    function [7:0] byte_of;
        input [31:0] w; input integer idx;
        begin byte_of = (w >> (8 * idx)) & 32'hFF; end
    endfunction

    function [31:0] ref_load;
        input [2:0] f3; input [1:0] a; input [31:0] w;
        reg [31:0] raw;
        integer t, base;
        begin
            raw  = 0;
            base = (f3[1:0] == 2'b01) ? (a & 2) : a;      // half uses the aligned half-word
            for (t = 0; t < nbytes(f3); t = t + 1)
                raw = raw | (byte_of(w, base + t) << (8 * t));
            case (f3)
                3'b000:  ref_load = raw[7]  ? (raw | 32'hFFFF_FF00) : raw;   // LB
                3'b001:  ref_load = raw[15] ? (raw | 32'hFFFF_0000) : raw;   // LH
                3'b010:  ref_load = raw;                                    // LW
                3'b100:  ref_load = raw;                                    // LBU
                3'b101:  ref_load = raw;                                    // LHU
                default: ref_load = 32'd0;
            endcase
        end
    endfunction

    function [3:0] ref_wstrb;
        input [2:0] f3; input [1:0] a; input st;
        integer t;
        begin
            ref_wstrb = 4'b0000;
            if (st && !ref_misaligned(f3, a))
                for (t = 0; t < nbytes(f3); t = t + 1)
                    ref_wstrb[a + t] = 1'b1;
        end
    endfunction

    function [31:0] ref_wdata;          // replication rule of CMI section 3.3
        input [2:0] f3; input [31:0] d;
        begin
            case (f3[1:0])
                2'b00:   ref_wdata = {d[7:0], d[7:0], d[7:0], d[7:0]};
                2'b01:   ref_wdata = {d[15:0], d[15:0]};
                2'b10:   ref_wdata = d;
                default: ref_wdata = 32'd0;
            endcase
        end
    endfunction

    // ---------------- one vector ----------------
    reg [31:0] mem_before, mem_after, readback;
    integer t, lo, hi;

    task check_vector;
        input [2:0] f3; input [1:0] a; input st; input [31:0] sd; input [31:0] lw;
        begin
            funct3 = f3; addr_lo = a; is_store = st; store_data = sd; load_word = lw;
            #1;
            $sformat(msg, "f3=%b a=%0d st=%0d misaligned", f3, a, st);
            tb_check(msg, misaligned, ref_misaligned(f3, a));
            $sformat(msg, "f3=%b a=%0d st=%0d wstrb", f3, a, st);
            tb_check(msg, wstrb, ref_wstrb(f3, a, st));
            $sformat(msg, "f3=%b a=%0d wdata sd=%h", f3, a, sd);
            tb_check(msg, wdata, ref_wdata(f3, sd));
            // load_data is undefined for a misaligned access (the core traps before
            // issuing it, CMI section 3.2); misaligned and wstrb are still checked above.
            if (!ref_misaligned(f3, a)) begin
                $sformat(msg, "f3=%b a=%0d load_data lw=%h", f3, a, lw);
                tb_check(msg, load_data, ref_load(f3, a, lw));
            end

            // memory view: legal store (funct3 SB/SH/SW), aligned
            if (st && f3[2] == 1'b0 && nbytes(f3) != 0 && !ref_misaligned(f3, a)) begin
                mem_before = lw;
                mem_after  = mem_before;
                for (t = 0; t < 4; t = t + 1)
                    if (wstrb[t]) mem_after[8*t +: 8] = wdata[8*t +: 8];
                lo = a; hi = a + nbytes(f3) - 1;
                for (t = 0; t < 4; t = t + 1) begin
                    if (t >= lo && t <= hi) begin
                        $sformat(msg, "mem view f3=%b a=%0d byte%0d written", f3, a, t);
                        tb_check(msg, mem_after[8*t +: 8], byte_of(sd, t - lo));
                    end else begin
                        $sformat(msg, "mem view f3=%b a=%0d byte%0d untouched", f3, a, t);
                        tb_check(msg, mem_after[8*t +: 8], mem_before[8*t +: 8]);
                    end
                end
                // read back through the DUT load path with the unsigned/word load
                funct3 = (f3 == 3'b010) ? 3'b010 : {1'b1, f3[1:0]};
                is_store = 1'b0; load_word = mem_after;
                #1;
                readback = sd & ((nbytes(f3) == 4) ? 32'hFFFF_FFFF : ((32'd1 << (8 * nbytes(f3))) - 1));
                $sformat(msg, "round trip f3=%b a=%0d", f3, a);
                tb_check(msg, load_data, readback);
                $sformat(msg, "round trip f3=%b a=%0d wstrb must be 0 for load", f3, a);
                tb_check(msg, wstrb, 4'b0000);
                // restore the original stimulus so callers can inspect the outputs
                funct3 = f3; is_store = st; load_word = lw;
                #1;
            end
        end
    endtask

    reg [31:0] pat [0:11];
    integer f, a, s, i, j, n;

    initial begin
        tb_init;
        pat[0]  = 32'h0000_0000;  pat[1]  = 32'hFFFF_FFFF;
        pat[2]  = 32'h8080_8080;  pat[3]  = 32'h7F7F_7F7F;
        pat[4]  = 32'h0123_4567;  pat[5]  = 32'h89AB_CDEF;
        pat[6]  = 32'h0000_8000;  pat[7]  = 32'h8000_0080;
        pat[8]  = 32'h7FFF_7FFF;  pat[9]  = 32'h8000_8000;
        pat[10] = 32'hFF00_00FF;  pat[11] = 32'h00FF_FF00;

        // Known answers from the RV32I definitions (anchor the reference model).
        check_vector(3'b000, 2'd3, 1'b0, 32'd0, 32'h80FF_FF7F);
        tb_check("KAT LB  a=3 of 80FFFF7F", load_data, 32'hFFFF_FF80);
        check_vector(3'b100, 2'd3, 1'b0, 32'd0, 32'h80FF_FF7F);
        tb_check("KAT LBU a=3 of 80FFFF7F", load_data, 32'h0000_0080);
        check_vector(3'b000, 2'd0, 1'b0, 32'd0, 32'h80FF_FF7F);
        tb_check("KAT LB  a=0 of 80FFFF7F", load_data, 32'h0000_007F);
        check_vector(3'b001, 2'd2, 1'b0, 32'd0, 32'h8001_7FFF);
        tb_check("KAT LH  a=2 of 80017FFF", load_data, 32'hFFFF_8001);
        check_vector(3'b101, 2'd2, 1'b0, 32'd0, 32'h8001_7FFF);
        tb_check("KAT LHU a=2 of 80017FFF", load_data, 32'h0000_8001);
        check_vector(3'b001, 2'd0, 1'b0, 32'd0, 32'h8001_7FFF);
        tb_check("KAT LH  a=0 of 80017FFF", load_data, 32'h0000_7FFF);
        check_vector(3'b000, 2'd2, 1'b1, 32'h1234_56AB, 32'd0);
        tb_check("KAT SB  a=2 wstrb", wstrb, 4'b0100);
        tb_check("KAT SB  a=2 wdata", wdata, 32'hABAB_ABAB);
        check_vector(3'b001, 2'd2, 1'b1, 32'h1234_CDEF, 32'd0);
        tb_check("KAT SH  a=2 wstrb", wstrb, 4'b1100);
        tb_check("KAT SH  a=2 wdata", wdata, 32'hCDEF_CDEF);
        check_vector(3'b001, 2'd1, 1'b1, 32'h1234_CDEF, 32'd0);
        tb_check("KAT SH  a=1 misaligned", misaligned, 1'b1);
        tb_check("KAT SH  a=1 wstrb blocked", wstrb, 4'b0000);
        check_vector(3'b010, 2'd2, 1'b1, 32'h1234_CDEF, 32'd0);
        tb_check("KAT SW  a=2 misaligned", misaligned, 1'b1);
        tb_check("KAT SW  a=2 wstrb blocked", wstrb, 4'b0000);
        check_vector(3'b010, 2'd0, 1'b0, 32'h1234_CDEF, 32'd0);
        tb_check("KAT LW  a=0 load wstrb", wstrb, 4'b0000);

        // Exhaustive: funct3 x addr x is_store x data patterns.
        for (f = 0; f < 8; f = f + 1)
            for (a = 0; a < 4; a = a + 1)
                for (s = 0; s < 2; s = s + 1)
                    for (i = 0; i < 12; i = i + 1)
                        for (j = 0; j < 12; j = j + 1)
                            check_vector(f[2:0], a[1:0], s[0], pat[i], pat[j]);

        // Random vectors.
        for (n = 0; n < 50000; n = n + 1)
            check_vector($random(tb_seed), $random(tb_seed), $random(tb_seed),
                         $random(tb_seed), $random(tb_seed));

        tb_finish;
    end

endmodule

`default_nettype wire
