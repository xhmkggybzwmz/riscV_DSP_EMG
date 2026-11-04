`timescale 1ns / 1ps
`default_nettype none

// emg1_spi_rx - SPI receiver and EMG1 packet parser (docs/system/stm32_spi_monitor.md).
// SPI mode 0, MSB first, SCLK <= 2 MHz, oversampled by the 100 MHz aclk. One active-low CS interval
// carries one 278-byte packet: 20 B header, 128 U16_LE samples, 2 B little-endian CRC16-CCITT-FALSE.
// Header: "EMG1", version 1, format 1 (U16_LE), channels 1, reserved, u32 rate, u32 index of the first
// sample, u16 samples = 128, u16 payload = 256.
//
// Samples are reported as they arrive, before the CRC is known: the consumer stores them speculatively
// and keeps them only on frame_good. Exactly one of frame_good / crc_err / fmt_err ends a packet; a packet
// whose CS falls again without any of them never completed (fmt_err is reported at the CS rise).
//   fmt_err       bad header, SCLK too fast inside the packet, CS released early, bytes after byte 277
//   overflow_err  one pulse per SCLK edge closer than the safe oversampling distance

module emg1_spi_rx (
    input  wire        aclk,
    input  wire        aresetn,
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,

    output reg         frame_start,
    output reg         sample_valid,
    output reg  [15:0] sample_data,
    output reg         frame_good,
    output reg         crc_err,
    output reg         fmt_err,
    output reg         overflow_err,
    output reg  [31:0] packet_index,      // valid at frame_good, held until the next frame_start
    output reg  [31:0] packet_rate,       // likewise
    output reg         active,
    output wire        cs_level
);
    // The synchronizer depth is the same for all three: MOSI is sampled as it was when the rising SCLK edge
    // entered the chain, and the 2 MHz limit leaves many aclk periods on both sides of every edge.
    (* ASYNC_REG = "TRUE" *) reg [2:0] sclk_sync, mosi_sync, cs_sync;
    reg sclk_d, cs_d;
    // CS counts only after 8 equal samples (80 ns): on the board, short pulses on the wire cut about one packet in 100
    // in two (seen as 2 FRAME_ERROR + 1 SEQ_ERROR, CRC errors 0, with the STM32 reporting every packet as sent).
    reg       cs_f;
    reg [2:0] cs_cnt;
    wire sclk_rise = sclk_sync[2] & ~sclk_d;
    wire sclk_edge = sclk_sync[2] ^ sclk_d;
    wire cs_fall   = ~cs_f & cs_d;
    wire cs_rise   = cs_f & ~cs_d;
    assign cs_level = cs_f;

    reg [7:0]  shift;
    reg [2:0]  bit_count;
    reg [7:0]  edge_gap;
    reg [8:0]  byte_count;
    reg [15:0] crc;
    reg [7:0]  crc_low, sample_low;
    reg        complete, frame_bad;

    wire [7:0] rx_byte     = {shift[6:0], mosi_sync[2]};
    wire       byte_strobe = sclk_rise && !cs_f && bit_count == 3'd7;

    function [15:0] crc16_byte;
        input [15:0] c0;
        input [7:0]  d;
        integer i;
        reg [15:0] c;
        begin
            c = c0 ^ {d, 8'h00};
            for (i = 0; i < 8; i = i + 1)
                c = c[15] ? ((c << 1) ^ 16'h1021) : (c << 1);
            crc16_byte = c;
        end
    endfunction

    always @(posedge aclk) begin
        frame_start  <= 1'b0;
        sample_valid <= 1'b0;
        frame_good   <= 1'b0;
        crc_err      <= 1'b0;
        fmt_err      <= 1'b0;
        overflow_err <= 1'b0;
        if (!aresetn) begin
            sclk_sync <= 3'b000; mosi_sync <= 3'b000; cs_sync <= 3'b111;
            sclk_d <= 1'b0; cs_d <= 1'b1; cs_f <= 1'b1; cs_cnt <= 3'd0; shift <= 8'd0; bit_count <= 3'd0; edge_gap <= 8'hFF;
            byte_count <= 9'd0; crc <= 16'hFFFF; crc_low <= 8'd0; sample_low <= 8'd0;
            complete <= 1'b0; frame_bad <= 1'b0; active <= 1'b0;
            sample_data <= 16'd0; packet_index <= 32'd0; packet_rate <= 32'd0;
        end else begin
            sclk_sync <= {sclk_sync[1:0], spi_sclk};
            mosi_sync <= {mosi_sync[1:0], spi_mosi};
            cs_sync   <= {cs_sync[1:0], spi_cs_n};
            sclk_d <= sclk_sync[2];
            cs_d   <= cs_f;
            if (cs_sync[2] == cs_f) begin
                cs_cnt <= 3'd0;
            end else begin
                cs_cnt <= cs_cnt + 3'd1;
                if (cs_cnt == 3'd7) begin cs_f <= cs_sync[2]; cs_cnt <= 3'd0; end
            end

            if (edge_gap != 8'hFF) edge_gap <= edge_gap + 8'd1;
            if (sclk_edge) begin
                edge_gap <= 8'd0;
                if (!cs_f && edge_gap < 8'd4) begin
                    overflow_err <= 1'b1;
                    frame_bad    <= 1'b1;
                end
            end

            if (cs_fall) begin
                frame_start <= 1'b1;
                active <= 1'b1; complete <= 1'b0; frame_bad <= 1'b0;
                byte_count <= 9'd0; bit_count <= 3'd0; crc <= 16'hFFFF;
                packet_index <= 32'd0; packet_rate <= 32'd0;
            end else if (cs_rise) begin
                if (active && !complete) fmt_err <= 1'b1;
                active <= 1'b0; bit_count <= 3'd0;
            end else if (sclk_rise && !cs_f) begin
                shift     <= {shift[6:0], mosi_sync[2]};
                bit_count <= bit_count + 3'd1;
            end

            if (byte_strobe && active) begin
                if (byte_count <= 9'd275) crc <= crc16_byte(crc, rx_byte);
                case (byte_count)
                    9'd0:  if (rx_byte != "E")   frame_bad <= 1'b1;
                    9'd1:  if (rx_byte != "M")   frame_bad <= 1'b1;
                    9'd2:  if (rx_byte != "G")   frame_bad <= 1'b1;
                    9'd3:  if (rx_byte != "1")   frame_bad <= 1'b1;
                    9'd4:  if (rx_byte != 8'd1)  frame_bad <= 1'b1;
                    9'd5:  if (rx_byte != 8'd1)  frame_bad <= 1'b1;
                    9'd6:  if (rx_byte != 8'd1)  frame_bad <= 1'b1;
                    9'd8:  packet_rate[7:0]    <= rx_byte;
                    9'd9:  packet_rate[15:8]   <= rx_byte;
                    9'd10: packet_rate[23:16]  <= rx_byte;
                    9'd11: packet_rate[31:24]  <= rx_byte;
                    9'd12: packet_index[7:0]   <= rx_byte;
                    9'd13: packet_index[15:8]  <= rx_byte;
                    9'd14: packet_index[23:16] <= rx_byte;
                    9'd15: packet_index[31:24] <= rx_byte;
                    9'd16: if (rx_byte != 8'h80) frame_bad <= 1'b1;
                    9'd17: if (rx_byte != 8'h00) frame_bad <= 1'b1;
                    9'd18: if (rx_byte != 8'h00) frame_bad <= 1'b1;
                    9'd19: if (rx_byte != 8'h01) frame_bad <= 1'b1;
                    9'd276: crc_low <= rx_byte;
                    9'd277: begin
                        complete <= 1'b1;
                        if (frame_bad)                       fmt_err    <= 1'b1;
                        else if ({rx_byte, crc_low} != crc)  crc_err    <= 1'b1;
                        else                                 frame_good <= 1'b1;
                    end
                    9'd278: ;   // handled below
                    default: if (byte_count >= 9'd20 && byte_count <= 9'd275) begin
                        if (!byte_count[0]) begin
                            sample_low <= rx_byte;
                        end else begin
                            sample_data  <= {rx_byte, sample_low};
                            sample_valid <= 1'b1;
                        end
                    end
                endcase
                // one report for a packet that is too long, whatever its length
                if (byte_count == 9'd278 && complete && !frame_bad) begin
                    fmt_err   <= 1'b1;
                    frame_bad <= 1'b1;
                end
                if (byte_count < 9'd278) byte_count <= byte_count + 9'd1;
            end
        end
    end
endmodule

`default_nettype wire
