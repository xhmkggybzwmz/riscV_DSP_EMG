`timescale 1ns / 1ps
`default_nettype none

// tb_mbx_unit - RTL unit test of the E4a mailbox hardware path:
//   CMI master (test) -> soc_io_split -+-> port a: tb_cmi_slave (stands for cmi2axil)
//                                      +-> port b: cmi_bram_port -> tb_bram_tdp port A
//   PS AXI4-Lite master -> tb_axil_bram_ctrl -> tb_bram_tdp port B
// Directed checks
//   - window routing: 0x2002_0000..0x2002_0FFC to the BRAM port, 0x2001_FFFC and
//     0x2002_1000 to port a (the router decides what is mapped at all)
//   - CMI response exactly one cycle after the accepting edge, read data of the BRAM
//   - WSTRB: byte / halfword writes from the CPU, visible to the PS; PS byte write
//   - word offsets wrap inside the 4 KB window only through the address bits used
//   - CPU and PS on the same word in the same cycle: READ_FIRST, the reader gets the
//     old word, the write lands (tb_bram_tdp counts it)
//   - reset during a CPU write burst: no response after reset, the next access works
//   - PS reads a stream of CPU-published words in order (single writer, monotonic)
// +seed=N for the random part.

module tb_mbx_unit;
    localparam TB_TIMEOUT_NS = 20000000;
    `include "tb_check.vh"

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst = 1'b1;

    // CMI master signals
    reg         req_valid = 1'b0, req_we = 1'b0;
    reg  [31:0] req_addr = 32'h0, req_wdata = 32'h0;
    reg  [3:0]  req_wstrb = 4'h0;
    wire        req_ready, rsp_valid, rsp_err;
    wire [31:0] rsp_rdata;

    wire        a_valid, a_ready, a_we, a_rsp_valid, a_rsp_err;
    wire [31:0] a_addr, a_wdata, a_rsp_rdata;
    wire [3:0]  a_wstrb;
    wire        b_valid, b_ready, b_we, b_rsp_valid, b_rsp_err;
    wire [31:0] b_addr, b_wdata, b_rsp_rdata;
    wire [3:0]  b_wstrb;

    soc_io_split #(.B_BASE(32'h2002_0000), .B_SIZE(32'h1000)) u_split (
        .clk(clk), .rst(rst),
        .s_req_valid(req_valid), .s_req_ready(req_ready), .s_req_addr(req_addr), .s_req_we(req_we),
        .s_req_wstrb(req_wstrb), .s_req_wdata(req_wdata), .s_rsp_valid(rsp_valid), .s_rsp_rdata(rsp_rdata),
        .s_rsp_err(rsp_err),
        .a_req_valid(a_valid), .a_req_ready(a_ready), .a_req_addr(a_addr), .a_req_we(a_we),
        .a_req_wstrb(a_wstrb), .a_req_wdata(a_wdata), .a_rsp_valid(a_rsp_valid), .a_rsp_rdata(a_rsp_rdata),
        .a_rsp_err(a_rsp_err),
        .b_req_valid(b_valid), .b_req_ready(b_ready), .b_req_addr(b_addr), .b_req_we(b_we),
        .b_req_wstrb(b_wstrb), .b_req_wdata(b_wdata), .b_rsp_valid(b_rsp_valid), .b_rsp_rdata(b_rsp_rdata),
        .b_rsp_err(b_rsp_err));
    tb_cmi_slave #(.TAG(8'hA5)) u_axi_side (.clk(clk), .rst(rst),
        .req_valid(a_valid), .req_ready(a_ready), .req_addr(a_addr), .req_we(a_we), .req_wstrb(a_wstrb),
        .req_wdata(a_wdata), .rsp_valid(a_rsp_valid), .rsp_rdata(a_rsp_rdata), .rsp_err(a_rsp_err));

    wire        ma_clk, ma_rst, ma_en, mb_en;
    wire [3:0]  ma_we, mb_we;
    wire [31:0] ma_addr, ma_wdata, ma_rdata, mb_addr, mb_wdata, mb_rdata;
    cmi_bram_port #(.SIZE(4096)) u_port (.clk(clk), .rst(rst),
        .req_valid(b_valid), .req_ready(b_ready), .req_addr(b_addr), .req_we(b_we), .req_wstrb(b_wstrb),
        .req_wdata(b_wdata), .rsp_valid(b_rsp_valid), .rsp_rdata(b_rsp_rdata), .rsp_err(b_rsp_err),
        .bram_clk(ma_clk), .bram_rst(ma_rst), .bram_en(ma_en), .bram_we(ma_we), .bram_addr(ma_addr),
        .bram_wrdata(ma_wdata), .bram_rddata(ma_rdata));
    tb_bram_tdp #(.SIZE(4096)) u_mem (
        .clka(ma_clk), .ena(ma_en), .wea(ma_we), .addra(ma_addr), .dina(ma_wdata), .douta(ma_rdata),
        .clkb(clk), .enb(mb_en), .web(mb_we), .addrb(mb_addr), .dinb(mb_wdata), .doutb(mb_rdata));

    wire [31:0] ps_awaddr, ps_wdata, ps_araddr, ps_rdata;
    wire [3:0]  ps_wstrb;
    wire [1:0]  ps_bresp, ps_rresp;
    wire        ps_awvalid, ps_awready, ps_wvalid, ps_wready, ps_bvalid, ps_bready;
    wire        ps_arvalid, ps_arready, ps_rvalid, ps_rready;
    tb_axil_master #(.AW(32)) u_ps (.clk(clk),
        .awaddr(ps_awaddr), .awvalid(ps_awvalid), .awready(ps_awready), .wdata(ps_wdata), .wstrb(ps_wstrb),
        .wvalid(ps_wvalid), .wready(ps_wready), .bresp(ps_bresp), .bvalid(ps_bvalid), .bready(ps_bready),
        .araddr(ps_araddr), .arvalid(ps_arvalid), .arready(ps_arready), .rdata(ps_rdata), .rresp(ps_rresp),
        .rvalid(ps_rvalid), .rready(ps_rready));
    reg ps_aresetn = 1'b0;
    initial #40 ps_aresetn = 1'b1;
    tb_axil_bram_ctrl #(.SIZE(4096)) u_ctl (.aclk(clk), .aresetn(ps_aresetn),
        .s_awaddr(ps_awaddr), .s_awvalid(ps_awvalid), .s_awready(ps_awready), .s_wdata(ps_wdata),
        .s_wstrb(ps_wstrb), .s_wvalid(ps_wvalid), .s_wready(ps_wready), .s_bresp(ps_bresp),
        .s_bvalid(ps_bvalid), .s_bready(ps_bready), .s_araddr(ps_araddr), .s_arvalid(ps_arvalid),
        .s_arready(ps_arready), .s_rdata(ps_rdata), .s_rresp(ps_rresp), .s_rvalid(ps_rvalid),
        .s_rready(ps_rready), .bram_en(mb_en), .bram_we(mb_we), .bram_addr(mb_addr),
        .bram_wrdata(mb_wdata), .bram_rddata(mb_rdata));

    // ---------------- CMI master: one access, response latency measured ----------------
    integer lat;
    reg [31:0] rd;
    reg        er;
    task cmi(input we, input [31:0] addr, input [31:0] data, input [3:0] strb,
             output [31:0] rdata, output err, output integer latency);
        begin
            @(negedge clk);
            req_valid = 1'b1; req_we = we; req_addr = addr; req_wdata = data; req_wstrb = we ? strb : 4'h0;
            @(posedge clk);
            while (!req_ready) @(posedge clk);
            @(negedge clk);
            req_valid = 1'b0; req_we = 1'b0;
            latency = 1;
            while (!rsp_valid) begin @(posedge clk); #1; if (!rsp_valid) latency = latency + 1; end
            rdata = rsp_rdata; err = rsp_err;
            @(posedge clk);
        end
    endtask
    task cpu_wr(input [31:0] addr, input [31:0] data, input [3:0] strb);
        cmi(1'b1, addr, data, strb, rd, er, lat);
    endtask
    task cpu_rd(input [31:0] addr, output [31:0] data);
        cmi(1'b0, addr, 32'h0, 4'h0, data, er, lat);
    endtask

    // ---------------- PS stream reader (concurrent with the CPU writer) ----------------
    integer ps_n = 0, ps_bad = 0, ps_reads = 0;
    reg ps_run = 1'b0;
    reg [31:0] ps_last = 32'h0, ps_v;
    always @(posedge clk) if (ps_run) begin : ps_proc
        u_ps.read(32'hA010_0100, ps_v);
        ps_reads = ps_reads + 1;
        if (ps_v < ps_last) ps_bad = ps_bad + 1;      // single writer, increasing: never backwards
        ps_last = ps_v;
    end

    integer i, k, a_before;
    reg [31:0] v;
    initial begin
        tb_init;
        u_axi_side.lat_min = 1; u_axi_side.lat_max = 3;
        repeat (5) @(posedge clk);
        rst = 1'b0;
        repeat (2) @(posedge clk);

        // routing and latency
        a_before = u_axi_side.n_req;
        cpu_wr(32'h2002_0000, 32'h1111_0000, 4'hF);
        tb_check("first word: CMI response 1 cycle after accept", lat, 1);
        cpu_wr(32'h2002_0FFC, 32'h2222_0FFC, 4'hF);
        cpu_rd(32'h2002_0000, v);
        tb_check("read back first word", v, 32'h1111_0000);
        tb_check("read: response 1 cycle after accept", lat, 1);
        tb_check("read: no error", er, 1'b0);
        cpu_rd(32'h2002_0FFC, v);
        tb_check("read back last word", v, 32'h2222_0FFC);
        tb_check("window accesses never reach port a", u_axi_side.n_req - a_before, 0);
        cpu_rd(32'h2001_FFFC, v);
        tb_check("0x2001_FFFC -> port a", v[31:24], 8'hA5);
        cpu_rd(32'h2002_1000, v);
        tb_check("0x2002_1000 -> port a", v[31:24], 8'hA5);
        tb_check("two accesses at port a", u_axi_side.n_req - a_before, 2);

        // WSTRB from the CPU, observed by the PS
        cpu_wr(32'h2002_0010, 32'hAABB_CCDD, 4'hF);
        cpu_wr(32'h2002_0010, 32'h0000_0011, 4'h1);
        cpu_wr(32'h2002_0010, 32'h2200_0000, 4'h8);
        cpu_wr(32'h2002_0010, 32'h0033_0000, 4'h4);
        u_ps.read(32'hA010_0010, v);
        tb_check("CPU byte writes visible to the PS", v, 32'h2233_CC11);
        cpu_wr(32'h2002_0014, 32'hFFFF_FFFF, 4'hF);
        cpu_wr(32'h2002_0014, 32'h0000_1234, 4'h3);
        u_ps.read(32'hA010_0014, v);
        tb_check("CPU halfword write visible to the PS", v, 32'hFFFF_1234);
        u_ps.write(32'hA010_0040, 32'h0000_00EE, 4'h1);
        cpu_rd(32'h2002_0040, v);
        tb_check("PS byte write visible to the CPU", v[7:0], 8'hEE);
        u_ps.write(32'hA010_0044, 32'h1234_5678, 4'hF);
        cpu_rd(32'h2002_0044, v);
        tb_check("PS word write visible to the CPU", v, 32'h1234_5678);

        // same word, same cycle: CPU writes while the PS reads it (READ_FIRST -> old value)
        cpu_wr(32'h2002_0020, 32'h0000_0001, 4'hF);
        k = u_mem.n_rw_coll;
        fork
            u_ps.read(32'hA010_0020, v);
            begin
                // mb_en is stable at the negedge: the next posedge is the controller's BRAM read
                @(negedge clk); while (!(mb_en && mb_addr == 32'h20)) @(negedge clk);
                force u_mem.ena = 1'b1; force u_mem.wea = 4'hF; force u_mem.addra = 32'h20; force u_mem.dina = 32'h0000_0002;
                @(posedge clk); #1;
                release u_mem.ena; release u_mem.wea; release u_mem.addra; release u_mem.dina;
            end
        join
        tb_check("collision: PS read returned the old word", v, 32'h0000_0001);
        tb_check("collision: write landed", u_mem.mem[8], 32'h0000_0002);
        tb_check("collision counted", u_mem.n_rw_coll - k, 1);

        // reset in the middle of a CPU access: no stale response afterwards
        @(negedge clk);
        req_valid = 1'b1; req_we = 1'b1; req_addr = 32'h2002_0030; req_wdata = 32'h5555_5555; req_wstrb = 4'hF;
        @(posedge clk); #1;
        rst = 1'b1; req_valid = 1'b0;
        @(posedge clk); #1;
        tb_check("no response while in reset", rsp_valid, 1'b0);
        repeat (3) @(posedge clk);
        rst = 1'b0;
        @(posedge clk); #1;
        tb_check("no stale response after reset", rsp_valid, 1'b0);
        cpu_rd(32'h2002_0030, v);
        tb_check("access after reset works (write before reset kept)", v, 32'h5555_5555);
        cpu_wr(32'h2002_0034, 32'h6666_6666, 4'hF);
        cpu_rd(32'h2002_0034, v);
        tb_check("write after reset", v, 32'h6666_6666);

        // single writer, concurrent PS reader: counter word only increases
        ps_run = 1'b1;
        for (i = 1; i <= 300; i = i + 1) begin
            cpu_wr(32'h2002_0100, i, 4'hF);
            if ($random(tb_seed) & 1) repeat ($random(tb_seed) & 3) @(posedge clk);
        end
        ps_run = 1'b0;
        repeat (20) @(posedge clk);
        u_ps.read(32'hA010_0100, v);
        tb_check("PS stream read: last value", v, 300);
        tb_check("PS stream read: never backwards", ps_bad, 0);
        tb_check("PS stream read: reads done", ps_reads > 50, 1);
        tb_check("BRAM write/write collisions", u_mem.n_ww_coll, 0);
        tb_check("PS AXI error responses", u_ps.bresp_err + u_ps.rresp_err, 0);
        $display("INFO: read/write collisions during the stream: %0d", u_mem.n_rw_coll);
        tb_finish;
    end
endmodule

`default_nettype wire
