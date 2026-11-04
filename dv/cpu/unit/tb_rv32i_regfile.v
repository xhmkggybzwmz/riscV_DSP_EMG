`timescale 1ns / 1ps
`default_nettype none

// tb_rv32i_regfile - self-checking unit test for rtl/cpu/rv32i_regfile.v
//
// Inputs are driven on the falling edge; combinational reads are checked
// before the next rising edge ("pre") and after it ("post").
// The reference is a shadow array; the test never assumes an initial value
// for x1..x31 (every register is written before it is checked).

module tb_rv32i_regfile;

    localparam TB_TIMEOUT_NS = 5000000;
    `include "tb_check.vh"

    reg         clk = 1'b0;
    reg         we;
    reg  [4:0]  waddr;
    reg  [31:0] wdata;
    reg  [4:0]  raddr1, raddr2;
    wire [31:0] rdata1, rdata2;

    rv32i_regfile dut (
        .clk(clk), .we(we), .waddr(waddr), .wdata(wdata),
        .raddr1(raddr1), .rdata1(rdata1),
        .raddr2(raddr2), .rdata2(rdata2)
    );

    always #5 clk = ~clk;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_rv32i_regfile); end
`endif

    reg [31:0] shadow  [0:31];
    reg        written [0:31];
    integer i, j, k, n;
    reg [31:0] v;
    reg [8*64-1:0] msg;

    function [31:0] exp_read;
        input [4:0] a;
        begin
            exp_read = (a == 5'd0) ? 32'd0 : shadow[a];
        end
    endfunction

    // Apply a write on the next falling edge and let the rising edge commit it.
    task do_write;
        input [4:0]  a;
        input [31:0] d;
        begin
            @(negedge clk);
            we = 1'b1; waddr = a; wdata = d;
            @(posedge clk); #1;
            if (a != 5'd0) begin shadow[a] = d; written[a] = 1'b1; end
            we = 1'b0;
        end
    endtask

    // Combinational read of both ports, checked against the shadow model.
    task check_read;
        input [4:0] a1;
        input [4:0] a2;
        begin
            raddr1 = a1; raddr2 = a2; #1;
            $sformat(msg, "read port1 x%0d", a1);
            tb_check(msg, rdata1, exp_read(a1));
            $sformat(msg, "read port2 x%0d", a2);
            tb_check(msg, rdata2, exp_read(a2));
        end
    endtask

    task fill_unique;
        input [31:0] salt;
        begin
            for (i = 1; i < 32; i = i + 1)
                do_write(i[4:0], {i[7:0], ~i[7:0], 8'hA5, i[7:0]} ^ salt);
        end
    endtask

    initial begin
        tb_init;
        we = 1'b0; waddr = 5'd0; wdata = 32'd0; raddr1 = 5'd0; raddr2 = 5'd0;
        for (i = 0; i < 32; i = i + 1) begin shadow[i] = 32'd0; written[i] = 1'b0; end
        repeat (2) @(posedge clk);

        // 1. x0 reads zero before anything has been written.
        check_read(5'd0, 5'd0);

        // 2. Every register x1..x31 holds a unique value; both ports read all of them.
        fill_unique(32'h0000_0000);
        for (i = 0; i < 32; i = i + 1)
            check_read(i[4:0], 5'd31 - i[4:0]);

        // 3. Writing x0 is discarded and does not disturb any other register.
        do_write(5'd0, 32'hFFFF_FFFF);
        do_write(5'd0, 32'h1234_5678);
        for (i = 0; i < 32; i = i + 1)
            check_read(i[4:0], i[4:0]);

        // 4. Data patterns on every register.
        for (i = 1; i < 32; i = i + 1) begin
            for (k = 0; k < 4; k = k + 1) begin
                case (k)
                    0: v = 32'h0000_0000;
                    1: v = 32'hFFFF_FFFF;
                    2: v = 32'hAAAA_AAAA;
                    default: v = 32'h5555_5555;
                endcase
                do_write(i[4:0], v);
                check_read(i[4:0], i[4:0]);
            end
            for (k = 0; k < 32; k = k + 1) begin
                do_write(i[4:0], 32'd1 << k);        // walking one
                check_read(i[4:0], 5'd0);
                do_write(i[4:0], ~(32'd1 << k));     // walking zero
                check_read(5'd0, i[4:0]);
            end
        end

        // 5. Read-port independence: every (port1, port2) address pair.
        fill_unique(32'h5A5A_0000);
        for (i = 0; i < 32; i = i + 1)
            for (j = 0; j < 32; j = j + 1)
                check_read(i[4:0], j[4:0]);

        // 6. Write timing and read-during-write on the same address:
        //    old value before the rising edge, new value after it.
        for (i = 1; i < 32; i = i + 1) begin
            @(negedge clk);
            v = ~shadow[i];
            we = 1'b1; waddr = i[4:0]; wdata = v;
            raddr1 = i[4:0]; raddr2 = i[4:0]; #1;
            $sformat(msg, "pre-edge port1 x%0d keeps old value", i);
            tb_check(msg, rdata1, shadow[i]);
            $sformat(msg, "pre-edge port2 x%0d keeps old value", i);
            tb_check(msg, rdata2, shadow[i]);
            @(posedge clk); #1;
            shadow[i] = v;
            $sformat(msg, "post-edge port1 x%0d has new value", i);
            tb_check(msg, rdata1, v);
            $sformat(msg, "post-edge port2 x%0d has new value", i);
            tb_check(msg, rdata2, v);
            we = 1'b0;
        end

        // 7. we=0 must not write, whatever waddr/wdata are.
        for (i = 1; i < 32; i = i + 1) begin
            @(negedge clk);
            we = 1'b0; waddr = i[4:0]; wdata = ~shadow[i];
            @(posedge clk); #1;
            check_read(i[4:0], i[4:0]);
        end

        // 8. Random traffic: random write each cycle, random reads checked pre/post edge.
        for (n = 0; n < 20000; n = n + 1) begin
            @(negedge clk);
            we     = $random(tb_seed);
            waddr  = $random(tb_seed);
            wdata  = $random(tb_seed);
            raddr1 = $random(tb_seed);
            raddr2 = (n % 7 == 0) ? waddr : $random(tb_seed);   // hit same-address often
            #1;
            tb_check("random pre-edge port1", rdata1, exp_read(raddr1));
            tb_check("random pre-edge port2", rdata2, exp_read(raddr2));
            @(posedge clk); #1;
            if (we && waddr != 5'd0) shadow[waddr] = wdata;
            tb_check("random post-edge port1", rdata1, exp_read(raddr1));
            tb_check("random post-edge port2", rdata2, exp_read(raddr2));
        end

        tb_finish;
    end

endmodule

`default_nettype wire
