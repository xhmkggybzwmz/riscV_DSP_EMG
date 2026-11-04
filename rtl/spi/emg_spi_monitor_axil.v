`timescale 1ns / 1ps
`default_nettype none

// EMG1 SPI receiver and PS-visible monitor.  The external SPI clock is sampled
// by a 100 MHz PL clock; use mode 0 and keep SCLK <= 2 MHz.  One active-low CS
// interval carries exactly one 278-byte, one-channel EMG1 packet.
module emg_spi_monitor_axil (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    input  wire        aresetn,
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,
    output wire        spi_ready,

    input  wire [11:0] s_axi_awaddr,
    input  wire [2:0]  s_axi_awprot,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [11:0] s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready
);
    localparam [31:0] ID_VALUE = 32'h454d_4731; // "EMG1"
    localparam [31:0] VERSION  = 32'h0001_0000;

    // Synchronizers.  The oversampling limit gives several aclk periods on
    // both sides of every SCLK edge; MOSI is sampled after the rising edge has
    // traversed the same synchronizer depth.
    (* ASYNC_REG = "TRUE" *) reg [2:0] sclk_sync, mosi_sync, cs_sync;
    reg sclk_d, cs_d;
    wire sclk_rise = sclk_sync[2] & ~sclk_d;
    wire sclk_edge = sclk_sync[2] ^ sclk_d;
    wire cs_fall = ~cs_sync[2] & cs_d;
    wire cs_rise = cs_sync[2] & ~cs_d;
    assign spi_ready = aresetn;

    reg [7:0] shift;
    reg [2:0] bit_count;
    reg [7:0] edge_gap;
    wire [7:0] rx_byte = {shift[6:0], mosi_sync[2]};
    wire byte_strobe = sclk_rise && !cs_sync[2] && bit_count == 3'd7;

    reg active, complete, frame_bad;
    reg [8:0] byte_count;
    reg [15:0] crc, received_crc;
    reg [31:0] packet_index, packet_rate, expected_index, last_index;
    reg [7:0] sample_low;
    reg valid_bank, work_bank, have_valid, have_expected;
    reg [15:0] bank0 [0:127];
    reg [15:0] bank1 [0:127];

    reg [31:0] good_frames, crc_errors, seq_errors, frame_errors, overflow_errors;
    reg clear_stats;

    function [15:0] crc16_byte;
        input [15:0] c0;
        input [7:0] d;
        integer i;
        reg [15:0] c;
        begin
            c = c0 ^ {d, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                c = c[15] ? (c << 1) ^ 16'h1021 : (c << 1);
            crc16_byte = c;
        end
    endfunction

    integer k;
    always @(posedge aclk) begin
        if (!aresetn) begin
            sclk_sync <= 3'b000; mosi_sync <= 3'b000; cs_sync <= 3'b111;
            sclk_d <= 1'b0; cs_d <= 1'b1; shift <= 8'd0; bit_count <= 3'd0;
            edge_gap <= 8'hff; active <= 1'b0; complete <= 1'b0;
            frame_bad <= 1'b0; byte_count <= 9'd0; crc <= 16'hffff;
            received_crc <= 16'd0; packet_index <= 32'd0; packet_rate <= 32'd0;
            expected_index <= 32'd0; last_index <= 32'd0; sample_low <= 8'd0;
            valid_bank <= 1'b0; work_bank <= 1'b1; have_valid <= 1'b0;
            have_expected <= 1'b0; good_frames <= 32'd0; crc_errors <= 32'd0;
            seq_errors <= 32'd0; frame_errors <= 32'd0; overflow_errors <= 32'd0;
            for (k = 0; k < 128; k = k + 1) begin bank0[k] <= 16'd0; bank1[k] <= 16'd0; end
        end else begin
            sclk_sync <= {sclk_sync[1:0], spi_sclk};
            mosi_sync <= {mosi_sync[1:0], spi_mosi};
            cs_sync   <= {cs_sync[1:0], spi_cs_n};
            sclk_d <= sclk_sync[2]; cs_d <= cs_sync[2];
            if (edge_gap != 8'hff) edge_gap <= edge_gap + 1'b1;
            if (sclk_edge) begin
                if (!cs_sync[2] && edge_gap < 8'd4) begin
                    overflow_errors <= overflow_errors + 1'b1;
                    frame_bad <= 1'b1;
                end
                edge_gap <= 8'd0;
            end

            if (clear_stats) begin
                good_frames <= 32'd0; crc_errors <= 32'd0; seq_errors <= 32'd0;
                frame_errors <= 32'd0; overflow_errors <= 32'd0;
                have_expected <= 1'b0;
            end

            if (cs_fall) begin
                active <= 1'b1; complete <= 1'b0; frame_bad <= 1'b0;
                byte_count <= 9'd0; bit_count <= 3'd0; crc <= 16'hffff;
                packet_index <= 32'd0; packet_rate <= 32'd0;
                work_bank <= ~valid_bank;
            end else if (cs_rise) begin
                if (active && !complete) frame_errors <= frame_errors + 1'b1;
                active <= 1'b0; bit_count <= 3'd0;
            end else if (sclk_rise && !cs_sync[2]) begin
                shift <= {shift[6:0], mosi_sync[2]};
                bit_count <= bit_count + 1'b1;
            end

            if (byte_strobe && active) begin
                if (byte_count <= 9'd275) crc <= crc16_byte(crc, rx_byte);
                case (byte_count)
                    9'd0:  if (rx_byte != "E") frame_bad <= 1'b1;
                    9'd1:  if (rx_byte != "M") frame_bad <= 1'b1;
                    9'd2:  if (rx_byte != "G") frame_bad <= 1'b1;
                    9'd3:  if (rx_byte != "1") frame_bad <= 1'b1;
                    9'd4:  if (rx_byte != 8'd1) frame_bad <= 1'b1;
                    9'd5:  if (rx_byte != 8'd1) frame_bad <= 1'b1;
                    9'd6:  if (rx_byte != 8'd1) frame_bad <= 1'b1;
                    9'd8:  packet_rate[7:0]   <= rx_byte;
                    9'd9:  packet_rate[15:8]  <= rx_byte;
                    9'd10: packet_rate[23:16] <= rx_byte;
                    9'd11: packet_rate[31:24] <= rx_byte;
                    9'd12: packet_index[7:0]   <= rx_byte;
                    9'd13: packet_index[15:8]  <= rx_byte;
                    9'd14: packet_index[23:16] <= rx_byte;
                    9'd15: packet_index[31:24] <= rx_byte;
                    9'd16: if (rx_byte != 8'h80) frame_bad <= 1'b1;
                    9'd17: if (rx_byte != 8'h00) frame_bad <= 1'b1;
                    9'd18: if (rx_byte != 8'h00) frame_bad <= 1'b1;
                    9'd19: if (rx_byte != 8'h01) frame_bad <= 1'b1;
                    9'd276: received_crc[7:0] <= rx_byte;
                    9'd277: begin
                        received_crc[15:8] <= rx_byte;
                        complete <= 1'b1;
                        if (frame_bad) begin
                            frame_errors <= frame_errors + 1'b1;
                        end else if ({rx_byte, received_crc[7:0]} != crc) begin
                            crc_errors <= crc_errors + 1'b1;
                        end else begin
                            good_frames <= good_frames + 1'b1;
                            if (have_expected && packet_index != expected_index)
                                seq_errors <= seq_errors + 1'b1;
                            expected_index <= packet_index + 32'd128;
                            last_index <= packet_index;
                            have_expected <= 1'b1; have_valid <= 1'b1;
                            valid_bank <= work_bank;
                        end
                    end
                    default: begin
                        if (byte_count >= 9'd20 && byte_count <= 9'd275) begin
                            if (!byte_count[0]) sample_low <= rx_byte;
                            else if (work_bank) bank1[(byte_count - 9'd20) >> 1] <= {rx_byte, sample_low};
                            else bank0[(byte_count - 9'd20) >> 1] <= {rx_byte, sample_low};
                        end
                    end
                endcase
                if (byte_count < 9'd278) byte_count <= byte_count + 1'b1;
            end
        end
    end

    // Minimal, single-outstanding AXI4-Lite register interface.
    reg aw_hold, w_hold;
    reg [9:0] aw_word;
    reg [31:0] w_data;
    assign s_axi_awready = aresetn && !aw_hold && !s_axi_bvalid;
    assign s_axi_wready  = aresetn && !w_hold  && !s_axi_bvalid;
    assign s_axi_bresp = 2'b00; assign s_axi_rresp = 2'b00;
    always @(posedge aclk) begin
        clear_stats <= 1'b0;
        if (!aresetn) begin
            aw_hold <= 1'b0; w_hold <= 1'b0; s_axi_bvalid <= 1'b0;
        end else begin
            if (s_axi_awvalid && s_axi_awready) begin aw_hold <= 1'b1; aw_word <= s_axi_awaddr[11:2]; end
            if (s_axi_wvalid && s_axi_wready) begin w_hold <= 1'b1; w_data <= s_axi_wdata; end
            if (aw_hold && w_hold) begin
                if (aw_word == 10'h001 && w_data[0]) clear_stats <= 1'b1;
                aw_hold <= 1'b0; w_hold <= 1'b0; s_axi_bvalid <= 1'b1;
            end
            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 1'b0;
        end
    end

    assign s_axi_arready = aresetn && !s_axi_rvalid;
    always @(posedge aclk) begin
        if (!aresetn) begin s_axi_rvalid <= 1'b0; s_axi_rdata <= 32'd0; end
        else begin
            if (s_axi_arvalid && s_axi_arready) begin
                case (s_axi_araddr[11:2])
                    10'h000: s_axi_rdata <= ID_VALUE;
                    10'h001: s_axi_rdata <= VERSION;
                    10'h002: s_axi_rdata <= {27'd0, have_expected, have_valid, active, cs_sync[2], spi_ready};
                    10'h003: s_axi_rdata <= good_frames;
                    10'h004: s_axi_rdata <= crc_errors;
                    10'h005: s_axi_rdata <= seq_errors;
                    10'h006: s_axi_rdata <= frame_errors;
                    10'h007: s_axi_rdata <= overflow_errors;
                    10'h008: s_axi_rdata <= last_index;
                    10'h009: s_axi_rdata <= expected_index;
                    10'h00a: s_axi_rdata <= packet_rate;
                    10'h00b: s_axi_rdata <= 32'h0100_0080;
                    default: begin
                        if (s_axi_araddr >= 12'h100 && s_axi_araddr < 12'h300)
                            s_axi_rdata <= valid_bank ? {16'd0, bank1[(s_axi_araddr - 12'h100) >> 2]}
                                                      : {16'd0, bank0[(s_axi_araddr - 12'h100) >> 2]};
                        else s_axi_rdata <= 32'd0;
                    end
                endcase
                s_axi_rvalid <= 1'b1;
            end else if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 1'b0;
        end
    end
endmodule

`default_nettype wire
