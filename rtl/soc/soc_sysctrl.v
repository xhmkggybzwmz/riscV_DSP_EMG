`timescale 1ns / 1ps
`default_nettype none

// soc_sysctrl - SYSCTRL, AXI4-Lite slave (4 KB page at 0x1000_0000,
// docs/soc/memory_map.md section 2.1).
//
// Registers (offset, access):
//   0x00 ID        RO  32'h5359_5331 ("SYS1")
//   0x04 VERSION   RO  32'h0001_0000
//   0x08 TOHOST    WO  test result: 1 = PASS, (code << 1) | 1 = FAIL. Reads 0.
//                      Each write handshake is one write event (tohost_wr pulse);
//                      bytes with WSTRB = 0 are written as 0.
//   0x0C SCRATCH   RW  free for software, WSTRB per byte, reset 0
//   0x10 LED       RW  bits [7:0] drive led[7:0], WSTRB per byte, reset 0
//   0x14 STATUS    RO  bit0: TOHOST written since reset, bit1: last TOHOST value == 1
//   0x18 CYCLE_LO  RO  low word of the 64-bit cycle counter (counts from reset);
//                      reading it latches the high word into CYCLE_HI
//   0x1C CYCLE_HI  RO  high word latched by the last CYCLE_LO read
//   other offsets      read 0, writes ignored, OKAY
//
// AXI4-Lite behaviour:
//   - AW and W are accepted independently (any order, or together); the write
//     is performed once both are held, then BVALID/BRESP=OKAY until BREADY.
//     No new AW/W is accepted until the B handshake, so every write is
//     performed exactly once.
//   - AR is accepted when no read response is pending; RDATA/RRESP are
//     registered at the AR handshake (a CYCLE_LO read latches CYCLE_HI then),
//     RVALID until RREADY.
//   - All outputs are registers or decoded from registers; the address uses
//     bits [11:2] (the interconnect forwards the full address).
//   - aresetn: synchronous, active low; VALIDs and registers cleared.

module soc_sysctrl (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    input  wire        aresetn,

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
    input  wire        s_axi_rready,

    output wire [7:0]  led,
    output reg         tohost_valid,     // a TOHOST write happened since reset
    output reg  [31:0] tohost_value,     // value of the last TOHOST write
    output reg         tohost_wr         // one-cycle pulse per TOHOST write
);

    localparam [31:0] ID_VALUE      = 32'h5359_5331;
    localparam [31:0] VERSION_VALUE = 32'h0001_0000;

    localparam [9:0] R_ID = 10'h000, R_VERSION = 10'h001, R_TOHOST = 10'h002, R_SCRATCH = 10'h003,
                     R_LED = 10'h004, R_STATUS = 10'h005, R_CYCLE_LO = 10'h006, R_CYCLE_HI = 10'h007;

    reg        aw_held, w_held;
    reg [9:0]  aw_word;
    reg [31:0] w_data;
    reg [3:0]  w_strb;
    reg [31:0] scratch, led_r, cycle_hi_latch;
    reg [63:0] cycles;

    assign s_axi_awready = ~aw_held & ~s_axi_bvalid & aresetn;
    assign s_axi_wready  = ~w_held  & ~s_axi_bvalid & aresetn;
    assign s_axi_arready = ~s_axi_rvalid & aresetn;
    assign s_axi_bresp   = 2'b00;
    assign s_axi_rresp   = 2'b00;
    assign led           = led_r[7:0];

    function [31:0] apply_strb;
        input [31:0] old; input [31:0] data; input [3:0] strb;
        integer k;
        begin
            apply_strb = old;
            for (k = 0; k < 4; k = k + 1)
                if (strb[k]) apply_strb[8*k +: 8] = data[8*k +: 8];
        end
    endfunction

    // ---------------- write path ----------------
    always @(posedge aclk) begin
        if (!aresetn) begin
            aw_held      <= 1'b0;
            w_held       <= 1'b0;
            s_axi_bvalid <= 1'b0;
            scratch      <= 32'd0;
            led_r        <= 32'd0;
            tohost_valid <= 1'b0;
            tohost_value <= 32'd0;
            tohost_wr    <= 1'b0;
        end else begin
            tohost_wr <= 1'b0;
            if (s_axi_awvalid && s_axi_awready) begin
                aw_held <= 1'b1;
                aw_word <= s_axi_awaddr[11:2];
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_held <= 1'b1;
                w_data <= s_axi_wdata;
                w_strb <= s_axi_wstrb;
            end
            if (aw_held && w_held) begin                // perform the write exactly once
                case (aw_word)
                    R_TOHOST: begin
                        tohost_value <= apply_strb(32'd0, w_data, w_strb);
                        tohost_valid <= 1'b1;
                        tohost_wr    <= 1'b1;
                    end
                    R_SCRATCH: scratch <= apply_strb(scratch, w_data, w_strb);
                    R_LED:     led_r   <= apply_strb(led_r, w_data, w_strb);
                    default: ;                              // RO or reserved: ignored
                endcase
                aw_held      <= 1'b0;
                w_held       <= 1'b0;
                s_axi_bvalid <= 1'b1;
            end
            if (s_axi_bvalid && s_axi_bready)
                s_axi_bvalid <= 1'b0;
        end
    end

    // ---------------- read path and cycle counter ----------------
    always @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_rvalid   <= 1'b0;
            s_axi_rdata    <= 32'd0;
            cycles         <= 64'd0;
            cycle_hi_latch <= 32'd0;
        end else begin
            cycles <= cycles + 64'd1;
            if (s_axi_arvalid && s_axi_arready) begin
                s_axi_rvalid <= 1'b1;
                case (s_axi_araddr[11:2])
                    R_ID:       s_axi_rdata <= ID_VALUE;
                    R_VERSION:  s_axi_rdata <= VERSION_VALUE;
                    R_SCRATCH:  s_axi_rdata <= scratch;
                    R_LED:      s_axi_rdata <= led_r;
                    R_STATUS:   s_axi_rdata <= {30'd0, tohost_valid && tohost_value == 32'd1, tohost_valid};
                    R_CYCLE_LO: begin
                        s_axi_rdata    <= cycles[31:0];
                        cycle_hi_latch <= cycles[63:32];
                    end
                    R_CYCLE_HI: s_axi_rdata <= cycle_hi_latch;
                    default:    s_axi_rdata <= 32'd0;       // TOHOST and reserved read 0
                endcase
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
