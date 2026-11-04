`timescale 1ns / 1ps
`default_nettype none

// kv260_dbg_axil - PS-accessible debug/control slave of the KV260 bring-up design
// (stage 6A, PS variant): replaces the JTAG VIO and ILA so that the board test
// needs no JTAG connection (a JTAG session hangs the KV260 Linux, see
// docs/fpga/kv260_stage6a.md section 9). Observe-only towards the CPU
// subsystem except for the CPU reset request.
//
// AXI4-Lite slave, 256 KB window (PS M_AXI_HPM0_FPD at 0xA000_0000 in the BD):
//   0x00000 ID             RO  32'h5256_3641 ("RV6A")
//   0x00004 VERSION        RO  32'h0001_0000
//   0x00008 CTRL           RW  [0] cpu_reset (1 = hold the CPU subsystem in reset,
//                              drives proc_sys_reset aux_reset_in), [1] ps_rst_override
//                              (diagnosis only); reset 0 -> the CPU runs after configuration
//   0x0000C STATUS         RO  [0] tohost_valid [1] halted [2] soc_aresetn [3] pl_resetn0
//                              [4] trace_active [5] trace_full
//   0x00010 FIRST_TOHOST   RO  0x00014 TOHOST_WRITES  0x00018 TOHOST_VALUE (last)
//   0x0001C TRAP_CAUSE     RO  0x00020 TRAP_PC        0x00024 TRAP_TVAL
//   0x00028 RUN_CYCLES     RO  0x0002C RETIRED        0x00030 TOHOST_RETIRED
//   0x00034 RESET_RELEASES RO  0x00038 FREE_CYCLES    0x0003C FETCH_PC
//   0x00040 LED            RO  0x00044 SCRATCH (RW, PS bus test)
//   0x00080 TRACE_START    RW  instruction number that starts the trace (reset 0)
//   0x00084 TRACE_COUNT    RO  records written in this run
//   0x00088 TRACE_DEPTH    RO  DEPTH
//   0x20000 + 32*i + 4*w   RO  trace record i, word w (i < DEPTH)
//   other offsets read 0; writes to RO/reserved offsets are ignored; responses OKAY
//
// Trace: cleared while the CPU subsystem is in reset (soc_aresetn = 0). It starts
// with the retire of instruction number TRACE_START (retire_valid && retired ==
// TRACE_START) and then stores one 8-word record per cycle with trace_event
// (ctrl[12]: retire, data request handshake, data response, trap, TOHOST write,
// AXI handshake) until DEPTH records. Record words:
//   w0 ctrl[31:0] (bit layout of kv260_cpu_dbg ila_ctrl)  w1 retired
//   w2 retire_pc  w3 retire_insn  w4 retire_rd_wdata  w5 dmem_req_addr
//   w6 dmem_req_wdata  w7 {2'b0, ctrl[45:32] (AXI VALID/READY/BRESP/RRESP), cycle[15:0]}
// cycle = cycles since the CPU subsystem left reset (16 bits, wraps).
//
// aresetn is the PS-side reset (pl_resetn0 only), NOT the CPU subsystem reset,
// so CTRL survives the CPU resets it requests.

module kv260_dbg_axil #(
    parameter DEPTH_LOG2 = 12
) (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    input  wire        aresetn,

    input  wire [17:0] s_axi_awaddr,
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
    input  wire [17:0] s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,

    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 cpu_reset RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_HIGH" *)
    output wire        cpu_reset,
    output wire        ps_rst_override,

    // observed (kv260_cpu_dbg / kv260_dbg_obs / soc_sysctrl / reset)
    input  wire [63:0]  ila_ctrl,
    input  wire [159:0] ila_cpu,
    input  wire [95:0]  ila_trap,
    input  wire [31:0]  retired,
    input  wire [31:0]  run_cycles,
    input  wire [31:0]  tohost_retired,
    input  wire [7:0]   tohost_writes,
    input  wire [31:0]  first_tohost,
    input  wire [15:0]  reset_releases,
    input  wire [31:0]  free_cycles,
    input  wire         tohost_valid,
    input  wire [7:0]   led,
    input  wire [31:0]  fetch_pc,
    input  wire         soc_aresetn,
    input  wire         pl_resetn0
);

    localparam DEPTH = 1 << DEPTH_LOG2;
    localparam [31:0] ID_VALUE = 32'h5256_3641, VERSION_VALUE = 32'h0001_0000;

    // ---------------- registers ----------------
    reg [1:0]  ctrl_r;
    reg [31:0] scratch, trace_start;
    assign cpu_reset       = ctrl_r[0];
    assign ps_rst_override = ctrl_r[1];

    // ---------------- trace ----------------
    reg [DEPTH_LOG2:0] count;
    reg                active;
    reg [15:0]         cyc;
    wire full = count[DEPTH_LOG2];
    wire trig = ila_ctrl[0] && retired == trace_start;
    wire rec  = (active || trig) && ila_ctrl[12] && !full;
    wire [DEPTH_LOG2-1:0] widx = count[DEPTH_LOG2-1:0];

    reg [31:0] m0 [0:DEPTH-1];
    reg [31:0] m1 [0:DEPTH-1];
    reg [31:0] m2 [0:DEPTH-1];
    reg [31:0] m3 [0:DEPTH-1];
    reg [31:0] m4 [0:DEPTH-1];
    reg [31:0] m5 [0:DEPTH-1];
    reg [31:0] m6 [0:DEPTH-1];
    reg [31:0] m7 [0:DEPTH-1];

    always @(posedge aclk) begin
        if (rec) begin
            m0[widx] <= ila_ctrl[31:0];
            m1[widx] <= retired;
            m2[widx] <= ila_cpu[31:0];
            m3[widx] <= ila_cpu[63:32];
            m4[widx] <= ila_cpu[95:64];
            m5[widx] <= ila_cpu[127:96];
            m6[widx] <= ila_cpu[159:128];
            m7[widx] <= {2'b00, ila_ctrl[45:32], cyc};
        end
    end

    always @(posedge aclk) begin
        if (!aresetn || !soc_aresetn) begin
            count  <= 0;
            active <= 1'b0;
            cyc    <= 16'd0;
        end else begin
            cyc <= cyc + 16'd1;
            if (trig) active <= 1'b1;
            if (rec)  count  <= count + 1'b1;
        end
    end

    // ---------------- AXI4-Lite write ----------------
    reg        aw_held, w_held;
    reg [15:0] aw_word;
    reg [31:0] w_data;
    reg [3:0]  w_strb;
    assign s_axi_awready = ~aw_held & ~s_axi_bvalid & aresetn;
    assign s_axi_wready  = ~w_held  & ~s_axi_bvalid & aresetn;
    assign s_axi_bresp   = 2'b00;
    assign s_axi_rresp   = 2'b00;

    function [31:0] apply_strb;
        input [31:0] old; input [31:0] data; input [3:0] strb;
        integer k;
        begin
            apply_strb = old;
            for (k = 0; k < 4; k = k + 1)
                if (strb[k]) apply_strb[8*k +: 8] = data[8*k +: 8];
        end
    endfunction

    always @(posedge aclk) begin
        if (!aresetn) begin
            aw_held      <= 1'b0;
            w_held       <= 1'b0;
            s_axi_bvalid <= 1'b0;
            ctrl_r       <= 2'b00;
            scratch      <= 32'd0;
            trace_start  <= 32'd0;
        end else begin
            if (s_axi_awvalid && s_axi_awready) begin
                aw_held <= 1'b1;
                aw_word <= s_axi_awaddr[17:2];
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_held <= 1'b1;
                w_data <= s_axi_wdata;
                w_strb <= s_axi_wstrb;
            end
            if (aw_held && w_held) begin
                case (aw_word)
                    16'h0002: ctrl_r      <= apply_strb({30'd0, ctrl_r}, w_data, w_strb) & 32'h3;
                    16'h0011: scratch     <= apply_strb(scratch, w_data, w_strb);
                    16'h0020: trace_start <= apply_strb(trace_start, w_data, w_strb);
                    default: ;
                endcase
                aw_held      <= 1'b0;
                w_held       <= 1'b0;
                s_axi_bvalid <= 1'b1;
            end
            if (s_axi_bvalid && s_axi_bready)
                s_axi_bvalid <= 1'b0;
        end
    end

    // ---------------- AXI4-Lite read (3 cycles: address, RAM, data) ----------------
    reg [1:0]  rstate;
    reg [15:0] ar_word;
    reg [31:0] q0, q1, q2, q3, q4, q5, q6, q7;
    assign s_axi_arready = (rstate == 2'd0) & ~s_axi_rvalid & aresetn;
    wire [DEPTH_LOG2-1:0] ridx = ar_word[DEPTH_LOG2+2:3];

    always @(posedge aclk) begin                      // block RAM read ports
        q0 <= m0[ridx]; q1 <= m1[ridx]; q2 <= m2[ridx]; q3 <= m3[ridx];
        q4 <= m4[ridx]; q5 <= m5[ridx]; q6 <= m6[ridx]; q7 <= m7[ridx];
    end

    function [31:0] reg_value;
        input [15:0] w;
        begin
            case (w)
                16'h0000: reg_value = ID_VALUE;
                16'h0001: reg_value = VERSION_VALUE;
                16'h0002: reg_value = {30'd0, ctrl_r};
                16'h0003: reg_value = {26'd0, full, active, pl_resetn0, soc_aresetn, ila_ctrl[8], tohost_valid};
                16'h0004: reg_value = first_tohost;
                16'h0005: reg_value = {24'd0, tohost_writes};
                16'h0006: reg_value = ila_trap[95:64];
                16'h0007: reg_value = {28'd0, ila_ctrl[23:20]};
                16'h0008: reg_value = ila_trap[31:0];
                16'h0009: reg_value = ila_trap[63:32];
                16'h000A: reg_value = run_cycles;
                16'h000B: reg_value = retired;
                16'h000C: reg_value = tohost_retired;
                16'h000D: reg_value = {16'd0, reset_releases};
                16'h000E: reg_value = free_cycles;
                16'h000F: reg_value = fetch_pc;
                16'h0010: reg_value = {24'd0, led};
                16'h0011: reg_value = scratch;
                16'h0020: reg_value = trace_start;
                16'h0021: reg_value = {{(31 - DEPTH_LOG2){1'b0}}, count};
                16'h0022: reg_value = DEPTH;
                default:  reg_value = 32'd0;
            endcase
        end
    endfunction

    always @(posedge aclk) begin
        if (!aresetn) begin
            rstate       <= 2'd0;
            s_axi_rvalid <= 1'b0;
            s_axi_rdata  <= 32'd0;
        end else begin
            case (rstate)
                2'd0: if (s_axi_arvalid && s_axi_arready) begin
                          ar_word <= s_axi_araddr[17:2];
                          rstate  <= 2'd1;
                      end
                2'd1: rstate <= 2'd2;                 // RAM output registers load ridx
                2'd2: begin
                          if (ar_word[15]) begin       // 0x20000 and above: trace records
                              case (ar_word[2:0])
                                  3'd0: s_axi_rdata <= q0;  3'd1: s_axi_rdata <= q1;
                                  3'd2: s_axi_rdata <= q2;  3'd3: s_axi_rdata <= q3;
                                  3'd4: s_axi_rdata <= q4;  3'd5: s_axi_rdata <= q5;
                                  3'd6: s_axi_rdata <= q6;  default: s_axi_rdata <= q7;
                              endcase
                          end else begin
                              s_axi_rdata <= reg_value(ar_word);
                          end
                          s_axi_rvalid <= 1'b1;
                          rstate       <= 2'd3;
                      end
                default: if (s_axi_rvalid && s_axi_rready) begin
                          s_axi_rvalid <= 1'b0;
                          rstate       <= 2'd0;
                      end
            endcase
        end
    end

endmodule

`default_nettype wire
