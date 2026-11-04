module tb_emg_spi_monitor;
    reg clk = 0, resetn = 0, sclk = 0, mosi = 0, cs_n = 1;
    wire ready;
    wire awready, wready, bvalid, arready, rvalid;
    wire [1:0] bresp, rresp;
    wire [31:0] rdata;
    always #5 clk = ~clk;

    emg_spi_monitor_axil dut (
        .aclk(clk), .aresetn(resetn), .spi_sclk(sclk), .spi_mosi(mosi),
        .spi_cs_n(cs_n), .spi_ready(ready),
        .s_axi_awaddr(12'd0), .s_axi_awprot(3'd0), .s_axi_awvalid(1'b0), .s_axi_awready(awready),
        .s_axi_wdata(32'd0), .s_axi_wstrb(4'd0), .s_axi_wvalid(1'b0), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(1'b1),
        .s_axi_araddr(12'd0), .s_axi_arprot(3'd0), .s_axi_arvalid(1'b0), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(1'b1)
    );

    function [15:0] crc_byte;
        input [15:0] c0; input [7:0] d;
        integer j; reg [15:0] c;
        begin
            c = c0 ^ {d, 8'h00};
            for (j=0; j<8; j=j+1) c = c[15] ? (c << 1) ^ 16'h1021 : c << 1;
            crc_byte = c;
        end
    endfunction

    task send_byte;
        input [7:0] b;
        integer j;
        begin
            for (j=7; j>=0; j=j-1) begin
                mosi = b[j]; #250; sclk = 1; #250; sclk = 0;
            end
        end
    endtask

    task send_byte_fast;
        input [7:0] b;
        integer j;
        begin
            for (j=7; j>=0; j=j-1) begin
                mosi = b[j]; #20; sclk = 1; #20; sclk = 0;
            end
        end
    endtask

    task send_crc_byte;
        input [7:0] b;
        inout [15:0] c;
        begin c = crc_byte(c, b); send_byte(b); end
    endtask

    task send_packet;
        input [31:0] idx;
        input corrupt_crc;
        integer i; reg [15:0] c; reg [15:0] v;
        begin
            c = 16'hffff; cs_n = 0; #100;
            send_crc_byte("E",c); send_crc_byte("M",c); send_crc_byte("G",c); send_crc_byte("1",c);
            send_crc_byte(8'd1,c); send_crc_byte(8'd1,c); send_crc_byte(8'd1,c); send_crc_byte(8'd0,c);
            send_crc_byte(8'ha0,c); send_crc_byte(8'h0f,c); send_crc_byte(8'd0,c); send_crc_byte(8'd0,c);
            send_crc_byte(idx[7:0],c); send_crc_byte(idx[15:8],c);
            send_crc_byte(idx[23:16],c); send_crc_byte(idx[31:24],c);
            send_crc_byte(8'h80,c); send_crc_byte(8'h00,c);
            send_crc_byte(8'h00,c); send_crc_byte(8'h01,c);
            for (i=0; i<128; i=i+1) begin
                v = i;
                send_crc_byte(v[7:0],c); send_crc_byte(v[15:8],c);
            end
            send_byte(c[7:0] ^ (corrupt_crc ? 8'h01 : 8'h00)); send_byte(c[15:8]);
            #100; cs_n = 1; #200;
        end
    endtask

    initial begin
        repeat (10) @(posedge clk); resetn = 1; repeat (10) @(posedge clk);
        if (!ready) $fatal(1, "READY low after reset");
        send_packet(0, 0);
        if (dut.good_frames != 1 || dut.crc_errors != 0 || dut.seq_errors != 0)
            $fatal(1, "first packet counters");
        if (!dut.valid_bank || dut.bank1[0] != 0 || dut.bank1[127] != 127)
            $fatal(1, "first packet samples");
        send_packet(128, 1);
        if (dut.good_frames != 1 || dut.crc_errors != 1)
            $fatal(1, "CRC error not counted");
        send_packet(256, 0);
        if (dut.good_frames != 2 || dut.seq_errors != 1 || dut.last_index != 256)
            $fatal(1, "sequence gap not counted");
        cs_n = 0; #100; send_byte("E"); send_byte("M"); cs_n = 1; #200;
        if (dut.frame_errors != 1) $fatal(1, "CS abort not counted");
        cs_n = 0; #100; send_byte_fast("E"); cs_n = 1; #200;
        if (dut.overflow_errors == 0) $fatal(1, "fast SCLK not counted");
        resetn = 0; repeat (10) @(posedge clk); resetn = 1; repeat (10) @(posedge clk);
        if (!ready || dut.good_frames != 0 || dut.overflow_errors != 0)
            $fatal(1, "reset did not restore idle state");
        send_packet(0, 0);
        if (dut.good_frames != 1) $fatal(1, "packet after reset failed");
        $display("PASS tb_emg_spi_monitor");
        $finish;
    end
endmodule
