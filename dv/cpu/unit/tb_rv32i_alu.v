`timescale 1ns / 1ps
`default_nettype none

// tb_rv32i_alu - self-checking unit test for rtl/cpu/rv32i_alu.v
//
// The reference model below is written in a deliberately different style from
// the RTL (bit-by-bit shifts, two's-complement subtraction, sign-bit based
// signed compare) so that a shared Verilog signedness mistake is not hidden.
// Known-answer vectors anchor the reference model itself.

module tb_rv32i_alu;

    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"
    `include "rv32i_defs.vh"

    reg  [3:0]  alu_op;
    reg  [31:0] op_a, op_b;
    wire [31:0] result;
    wire        eq, lt, ltu;

    rv32i_alu dut (
        .alu_op(alu_op), .op_a(op_a), .op_b(op_b),
        .result(result), .eq(eq), .lt(lt), .ltu(ltu)
    );

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_rv32i_alu); end
`endif

    // ---------------- reference model ----------------
    function [31:0] ref_shift;           // dir: 0=left logical, 1=right logical, 2=right arithmetic
        input [31:0] a;
        input [4:0]  s;
        input [1:0]  dir;
        integer t;
        reg [31:0] r;
        begin
            r = a;
            for (t = 0; t < s; t = t + 1) begin
                case (dir)
                    2'd0:    r = {r[30:0], 1'b0};
                    2'd1:    r = {1'b0, r[31:1]};
                    default: r = {r[31], r[31:1]};
                endcase
            end
            ref_shift = r;
        end
    endfunction

    function ref_ltu;
        input [31:0] a, b;
        reg [32:0] d;
        begin
            d = {1'b0, a} - {1'b0, b};   // borrow out <=> a < b (unsigned)
            ref_ltu = d[32];
        end
    endfunction

    function ref_lt;
        input [31:0] a, b;
        begin
            if (a[31] != b[31]) ref_lt = a[31];    // negative < non-negative
            else                ref_lt = ref_ltu(a, b);
        end
    endfunction

    function [31:0] ref_alu;
        input [3:0]  op;
        input [31:0] a, b;
        begin
            case (op)
                ALU_ADD:  ref_alu = a + b;
                ALU_SUB:  ref_alu = a + ~b + 32'd1;
                ALU_SLL:  ref_alu = ref_shift(a, b[4:0], 2'd0);
                ALU_SLT:  ref_alu = ref_lt(a, b)  ? 32'd1 : 32'd0;
                ALU_SLTU: ref_alu = ref_ltu(a, b) ? 32'd1 : 32'd0;
                ALU_XOR:  ref_alu = (a | b) & ~(a & b);
                ALU_SRL:  ref_alu = ref_shift(a, b[4:0], 2'd1);
                ALU_SRA:  ref_alu = ref_shift(a, b[4:0], 2'd2);
                ALU_OR:   ref_alu = ~(~a & ~b);
                ALU_AND:  ref_alu = ~(~a | ~b);
                default:  ref_alu = 32'd0;
            endcase
        end
    endfunction

    reg [8*64-1:0] msg;

    task apply_and_check;
        input [3:0]  op;
        input [31:0] a, b;
        begin
            alu_op = op; op_a = a; op_b = b; #1;
            $sformat(msg, "op=%0d a=%h b=%h result", op, a, b);
            tb_check(msg, result, ref_alu(op, a, b));
            $sformat(msg, "a=%h b=%h eq", a, b);
            tb_check(msg, eq, (a ^ b) == 32'd0);
            $sformat(msg, "a=%h b=%h lt", a, b);
            tb_check(msg, lt, ref_lt(a, b));
            $sformat(msg, "a=%h b=%h ltu", a, b);
            tb_check(msg, ltu, ref_ltu(a, b));
        end
    endtask

    // Known answer: checks DUT and reference model against a hand-computed value.
    task kat;
        input [3:0]  op;
        input [31:0] a, b, expected;
        begin
            alu_op = op; op_a = a; op_b = b; #1;
            $sformat(msg, "KAT op=%0d a=%h b=%h (dut)", op, a, b);
            tb_check(msg, result, expected);
            $sformat(msg, "KAT op=%0d a=%h b=%h (ref)", op, a, b);
            tb_check(msg, ref_alu(op, a, b), expected);
        end
    endtask

    reg [31:0] edge_v [0:15];
    integer i, j, k, n;
    reg [31:0] a, b;

    function [31:0] biased_rand;         // mix of edge values and fully random words
        input integer sel;
        begin
            case (sel % 4)
                0:       biased_rand = edge_v[$random(tb_seed) & 15];
                1:       biased_rand = {32{1'b1}} << ($random(tb_seed) & 31);
                default: biased_rand = $random(tb_seed);
            endcase
        end
    endfunction

    initial begin
        tb_init;
        edge_v[0]  = 32'h0000_0000;  edge_v[1]  = 32'h0000_0001;
        edge_v[2]  = 32'h0000_0002;  edge_v[3]  = 32'h7FFF_FFFF;
        edge_v[4]  = 32'h8000_0000;  edge_v[5]  = 32'h8000_0001;
        edge_v[6]  = 32'hFFFF_FFFF;  edge_v[7]  = 32'hFFFF_FFFE;
        edge_v[8]  = 32'h0000_FFFF;  edge_v[9]  = 32'hFFFF_0000;
        edge_v[10] = 32'hAAAA_AAAA;  edge_v[11] = 32'h5555_5555;
        edge_v[12] = 32'h0000_001F;  edge_v[13] = 32'h0000_0020;
        edge_v[14] = 32'h0000_0021;  edge_v[15] = 32'hFFFF_FFE0;

        // 1. Known-answer tests (hand computed from the RV32I definitions).
        kat(ALU_ADD,  32'hFFFF_FFFF, 32'h0000_0001, 32'h0000_0000);   // wraps
        kat(ALU_ADD,  32'h7FFF_FFFF, 32'h0000_0001, 32'h8000_0000);   // signed overflow wraps
        kat(ALU_SUB,  32'h0000_0000, 32'h0000_0001, 32'hFFFF_FFFF);
        kat(ALU_SUB,  32'h8000_0000, 32'h0000_0001, 32'h7FFF_FFFF);
        kat(ALU_SLL,  32'h0000_0001, 32'h0000_001F, 32'h8000_0000);
        kat(ALU_SLL,  32'h0000_0001, 32'h0000_0020, 32'h0000_0001);   // shamt = 32 & 31 = 0
        kat(ALU_SRL,  32'h8000_0000, 32'h0000_001F, 32'h0000_0001);
        kat(ALU_SRL,  32'h8000_0000, 32'h0000_0004, 32'h0800_0000);
        kat(ALU_SRA,  32'h8000_0000, 32'h0000_001F, 32'hFFFF_FFFF);
        kat(ALU_SRA,  32'h8000_0000, 32'h0000_0004, 32'hF800_0000);
        kat(ALU_SRA,  32'h7FFF_FFFF, 32'h0000_0004, 32'h07FF_FFFF);
        kat(ALU_SRA,  32'h8000_0000, 32'hFFFF_FFE4, 32'hF800_0000);   // upper bits of op_b ignored
        kat(ALU_SRA,  32'hF000_0000, 32'h0000_0000, 32'hF000_0000);
        kat(ALU_SLT,  32'hFFFF_FFFF, 32'h0000_0001, 32'h0000_0001);   // -1 < 1
        kat(ALU_SLTU, 32'hFFFF_FFFF, 32'h0000_0001, 32'h0000_0000);   // 0xFFFFFFFF > 1
        kat(ALU_SLT,  32'h8000_0000, 32'h7FFF_FFFF, 32'h0000_0001);
        kat(ALU_SLTU, 32'h8000_0000, 32'h7FFF_FFFF, 32'h0000_0000);
        kat(ALU_SLT,  32'h0000_0005, 32'h0000_0005, 32'h0000_0000);
        kat(ALU_XOR,  32'hFF00_FF00, 32'h0F0F_0F0F, 32'hF00F_F00F);
        kat(ALU_OR,   32'hFF00_FF00, 32'h0F0F_0F0F, 32'hFF0F_FF0F);
        kat(ALU_AND,  32'hFF00_FF00, 32'h0F0F_0F0F, 32'h0F00_0F00);

        // 2. All operations x all edge-value pairs.
        for (k = 0; k < 10; k = k + 1)
            for (i = 0; i < 16; i = i + 1)
                for (j = 0; j < 16; j = j + 1)
                    apply_and_check(k[3:0], edge_v[i], edge_v[j]);

        // 3. Every shift amount 0..31, with random garbage in op_b[31:5].
        for (i = 0; i < 16; i = i + 1)
            for (j = 0; j < 32; j = j + 1) begin
                b = ($random(tb_seed) & 32'hFFFF_FFE0) | j;
                apply_and_check(ALU_SLL, edge_v[i], b);
                apply_and_check(ALU_SRL, edge_v[i], b);
                apply_and_check(ALU_SRA, edge_v[i], b);
            end

        // 4. Unused alu_op codes return 0.
        for (k = 10; k < 16; k = k + 1)
            for (i = 0; i < 16; i = i + 1)
                apply_and_check(k[3:0], edge_v[i], edge_v[15 - i]);

        // 5. Random vectors (fixed seed => reproducible).
        for (n = 0; n < 20000; n = n + 1) begin
            a = biased_rand(n);
            b = biased_rand(n >> 2);
            for (k = 0; k < 10; k = k + 1)
                apply_and_check(k[3:0], a, b);
        end

        tb_finish;
    end

endmodule

`default_nettype wire
