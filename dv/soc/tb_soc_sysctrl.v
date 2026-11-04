`timescale 1ns / 1ps
`default_nettype none

// tb_soc_sysctrl - self-checking unit test for rtl/soc/soc_sysctrl.v
//
// AXI4-Lite master tasks drive AW and W independently (fork/join with separate
// delays: AW first, W first, same cycle) and delay BREADY/RREADY; an AXI4-Lite
// monitor checks the protocol. Register checks: ID/VERSION, RO writes ignored,
// SCRATCH/LED with WSTRB, TOHOST (one write event per write, value, STATUS,
// reads 0), CYCLE_LO/CYCLE_HI latching, reserved offsets, reset values, and
// reset with a half-received write.

module tb_soc_sysctrl;

    localparam TB_TIMEOUT_NS = 50000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    reg aresetn = 1'b0;
    always #5 clk = ~clk;

`ifdef DUMP_WAVES
    initial begin $dumpfile("wave.vcd"); $dumpvars(0, tb_soc_sysctrl); end
`endif

    reg  [11:0] awaddr = 0, araddr = 0;
    reg         awvalid = 0, wvalid = 0, bready = 0, arvalid = 0, rready = 0;
    reg  [31:0] wdata = 0;
    reg  [3:0]  wstrb = 0;
    wire        awready, wready, bvalid, arready, rvalid;
    wire [1:0]  bresp, rresp;
    wire [31:0] rdata;
    wire [7:0]  led;
    wire        tohost_valid, tohost_wr;
    wire [31:0] tohost_value;

    soc_sysctrl dut (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awprot(3'b000), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(wstrb), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arprot(3'b000), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .led(led), .tohost_valid(tohost_valid), .tohost_value(tohost_value), .tohost_wr(tohost_wr)
    );

    wire [31:0] ax_err, n_aw, n_w, n_b, n_ar, n_r, n_berr, n_rerr;
    tb_axil_monitor #(.NAME("sysctrl"), .SINGLE(1)) u_mon (
        .aclk(clk), .aresetn(aresetn),
        .awaddr({20'd0, awaddr}), .awprot(3'b000), .awvalid(awvalid), .awready(awready),
        .wdata(wdata), .wstrb(wstrb), .wvalid(wvalid), .wready(wready),
        .bresp(bresp), .bvalid(bvalid), .bready(bready),
        .araddr({20'd0, araddr}), .arprot(3'b000), .arvalid(arvalid), .arready(arready),
        .rdata(rdata), .rresp(rresp), .rvalid(rvalid), .rready(rready),
        .errors(ax_err), .n_aw(n_aw), .n_w(n_w), .n_b(n_b), .n_ar(n_ar), .n_r(n_r),
        .n_berr(n_berr), .n_rerr(n_rerr));

    integer tohost_events = 0;
    always @(posedge clk) if (tohost_wr) tohost_events = tohost_events + 1;

    reg [8*64-1:0] msg;
    reg [31:0] rd;
    reg [1:0]  resp;

    // AXI4-Lite write: AW and W start after their own delays (in cycles);
    // BREADY is raised b_delay cycles after BVALID.
    task axi_write;
        input [11:0] a; input [31:0] d; input [3:0] s;
        input integer aw_delay; input integer w_delay; input integer b_delay;
        begin
            fork
                begin
                    repeat (aw_delay) @(negedge clk);
                    @(negedge clk); awaddr = a; awvalid = 1'b1;
                    @(posedge clk); while (!awready) @(posedge clk);
                    #1 awvalid = 1'b0;
                end
                begin
                    repeat (w_delay) @(negedge clk);
                    @(negedge clk); wdata = d; wstrb = s; wvalid = 1'b1;
                    @(posedge clk); while (!wready) @(posedge clk);
                    #1 wvalid = 1'b0;
                end
            join
            @(posedge clk); while (!bvalid) @(posedge clk);
            #1;
            repeat (b_delay) @(negedge clk);
            @(negedge clk); bready = 1'b1; resp = bresp;
            @(posedge clk); #1 bready = 1'b0;
        end
    endtask

    task axi_read;
        input [11:0] a; input integer r_delay;
        begin
            @(negedge clk); araddr = a; arvalid = 1'b1;
            @(posedge clk); while (!arready) @(posedge clk);
            #1 arvalid = 1'b0;
            @(posedge clk); while (!rvalid) @(posedge clk);
            #1;
            repeat (r_delay) @(negedge clk);
            @(negedge clk); rready = 1'b1; rd = rdata; resp = rresp;
            @(posedge clk); #1 rready = 1'b0;
        end
    endtask

    task wr; input [11:0] a; input [31:0] d; input [3:0] s;
        begin axi_write(a, d, s, 0, 0, 0); tb_check("write BRESP OKAY", resp, 2'b00); end
    endtask
    task rd_expect; input [11:0] a; input [31:0] e;
        begin
            axi_read(a, 0);
            $sformat(msg, "read offset %h", a); tb_check(msg, rd, e);
            tb_check("read RRESP OKAY", resp, 2'b00);
        end
    endtask

    integer ev0, i, o;
    reg [31:0] c_lo1, c_hi1, c_lo2, c_hi2;

    initial begin
        tb_init;
        repeat (3) @(negedge clk);
        aresetn = 1'b1;
        repeat (2) @(negedge clk);

        // reset values / constants
        rd_expect(12'h000, 32'h5359_5331);
        rd_expect(12'h004, 32'h0001_0000);
        rd_expect(12'h00C, 32'h0);
        rd_expect(12'h010, 32'h0);
        rd_expect(12'h014, 32'h0);
        rd_expect(12'h008, 32'h0);
        tb_check("tohost_valid after reset", tohost_valid, 1'b0);

        // RO / reserved writes ignored
        ev0 = tohost_events;
        wr(12'h000, 32'hFFFF_FFFF, 4'hF); rd_expect(12'h000, 32'h5359_5331);
        wr(12'h004, 32'hFFFF_FFFF, 4'hF); rd_expect(12'h004, 32'h0001_0000);
        wr(12'h014, 32'hFFFF_FFFF, 4'hF); rd_expect(12'h014, 32'h0);
        for (o = 32; o < 4096; o = o + 500) begin
            wr(o[11:0] & 12'hFFC, 32'hDEAD_BEEF, 4'hF);
            rd_expect(o[11:0] & 12'hFFC, 32'h0);
        end
        rd_expect(12'hFFC, 32'h0);
        tb_check("no TOHOST event from other offsets", tohost_events - ev0, 0);

        // SCRATCH / LED with byte strobes
        wr(12'h00C, 32'h1122_3344, 4'hF); rd_expect(12'h00C, 32'h1122_3344);
        wr(12'h00C, 32'hAABB_CCDD, 4'h5); rd_expect(12'h00C, 32'h11BB_33DD);
        wr(12'h00C, 32'h0000_EE00, 4'h2); rd_expect(12'h00C, 32'h11BB_EEDD);
        wr(12'h010, 32'h0000_00A5, 4'h1); rd_expect(12'h010, 32'h0000_00A5);
        tb_check("led output", led, 8'hA5);

        // TOHOST: one event per write, value, STATUS, reads 0
        ev0 = tohost_events;
        wr(12'h008, 32'h0000_0007, 4'hF);
        tb_check("TOHOST event count", tohost_events - ev0, 1);
        tb_check("TOHOST value (fail code 3)", tohost_value, 32'h7);
        rd_expect(12'h014, 32'h1);                  // written, not PASS
        rd_expect(12'h008, 32'h0);
        wr(12'h008, 32'h0000_0001, 4'hF);
        tb_check("TOHOST event count after 2 writes", tohost_events - ev0, 2);
        tb_check("TOHOST PASS value", tohost_value, 32'h1);
        rd_expect(12'h014, 32'h3);                  // written and PASS
        wr(12'h008, 32'hFFFF_FF01, 4'h1);           // only byte 0 written, others 0
        tb_check("TOHOST WSTRB: unselected bytes are 0", tohost_value, 32'h1);

        // AW/W order and delayed BREADY/RREADY: exactly one event each time
        ev0 = tohost_events;
        axi_write(12'h008, 32'h1, 4'hF, 0, 4, 0);     // AW first
        axi_write(12'h008, 32'h1, 4'hF, 4, 0, 0);     // W first
        axi_write(12'h008, 32'h1, 4'hF, 0, 0, 7);     // same cycle, BREADY late
        axi_write(12'h00C, 32'h5555_AAAA, 4'hF, 2, 9, 3);
        axi_read(12'h00C, 6);  tb_check("RREADY late: data", rd, 32'h5555_AAAA);
        tb_check("TOHOST events for 3 ordered writes", tohost_events - ev0, 3);
        for (i = 0; i < 300; i = i + 1) begin
            axi_write(12'h00C, i, 4'hF, {$random(tb_seed)} % 4, {$random(tb_seed)} % 4, {$random(tb_seed)} % 3);
            axi_read(12'h00C, {$random(tb_seed)} % 3);
            if (rd !== i) begin $sformat(msg, "random SCRATCH %0d", i); tb_check(msg, rd, i); end
        end
        tb_check("random SCRATCH sequence", rd, 299);

        // cycle counter: CYCLE_HI is the value latched by the CYCLE_LO read
        axi_read(12'h018, 0); c_lo1 = rd;
        axi_read(12'h01C, 0); c_hi1 = rd;
        repeat (50) @(negedge clk);
        axi_read(12'h018, 0); c_lo2 = rd;
        axi_read(12'h01C, 0); c_hi2 = rd;
        tb_check("cycle counter high word (short run)", c_hi1, 32'h0);
        tb_check("cycle counter advances by >= 50", (c_lo2 - c_lo1) >= 50, 1'b1);
        dut.cycles = 64'h0000_0001_FFFF_FFF0;     // jump near a 32-bit carry
        axi_read(12'h018, 0); c_lo1 = rd;
        repeat (40) @(negedge clk);
        axi_read(12'h01C, 0);
        tb_check("CYCLE_HI holds the value latched by CYCLE_LO", rd, 32'h1);
        axi_read(12'h018, 0); axi_read(12'h01C, 0);
        tb_check("CYCLE_HI after the carry", rd, 32'h2);

        // reset with a half-received write (AW accepted, W not yet sent)
        ev0 = tohost_events;
        @(negedge clk); awaddr = 12'h008; awvalid = 1'b1;
        @(posedge clk); while (!awready) @(posedge clk);
        #1 awvalid = 1'b0;
        repeat (2) @(negedge clk);
        aresetn = 1'b0;
        repeat (3) @(negedge clk);
        aresetn = 1'b1;
        repeat (2) @(negedge clk);
        wvalid = 1'b0;
        repeat (5) @(negedge clk);
        tb_check("half write abandoned by reset: no TOHOST event", tohost_events - ev0, 0);
        tb_check("tohost_valid cleared by reset", tohost_valid, 1'b0);
        rd_expect(12'h00C, 32'h0);                  // SCRATCH reset
        tb_check("LED reset", led, 8'h00);
        wr(12'h008, 32'h1, 4'hF);
        tb_check("TOHOST works after reset", tohost_events - ev0, 1);

        tb_check("AXI protocol monitor violations", ax_err, 0);
        tb_finish;
    end

endmodule

`default_nettype wire
