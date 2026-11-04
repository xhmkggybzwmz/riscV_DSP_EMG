`timescale 1ns / 1ps
`default_nettype none

// kv260_dbg_obs - board-level observation and reset gating for the KV260
// bring-up design (stage 6A, fpga/kv260/tcl/kv260_bd.tcl). Observe-only: it
// reads status/debug outputs of rv32_soc_top and soc_sysctrl and never feeds
// anything back into the CPU subsystem except through the reset generator.
//
// Reset path (proc_sys_reset in the block design):
//   ext_reset_n = pl_resetn0 | ps_rst_override
//     pl_resetn0 is the PS fabric reset (active low). ps_rst_override comes from
//     the VIO (init 0) and only exists so that a PS that holds pl_resetn0 low
//     can be diagnosed; it is never set in normal operation.
//   aux_reset_in of proc_sys_reset is driven by the VIO directly (CPU re-run).
//   dcm_locked = 1: pl_clk0 comes straight from the PS PLL, no MMCM.
//
// Counters (aclk, cleared by the synchronised rst_n = peripheral_aresetn,
// except reset_releases which counts rst_n rising edges since configuration):
//   run_cycles     cycles since reset release, frozen at the first TOHOST write
//   retired        retired instructions since reset release, never frozen
//   tohost_retired retired count when SYSCTRL performs the first TOHOST write; the
//                  TOHOST store itself retires only after the AXI B response, so
//                  this equals the ISS instruction count minus 1
//   tohost_writes  number of TOHOST writes since reset release (saturating)
//   first_tohost   value of the first TOHOST write
//   free_cycles    free-running aclk counter, never reset (read twice over JTAG
//                  some seconds apart to measure the real pl_clk0 frequency)
//
// The VIO and ILA read these values; nothing here is required for the SoC to
// work.

module kv260_dbg_obs (
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET rst_n" *)
    input  wire        aclk,
    input  wire        rst_n,

    input  wire        pl_resetn0,
    input  wire        ps_rst_override,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 ext_reset_n RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    output wire        ext_reset_n,
    output wire        dcm_locked,

    input  wire        retire_valid,
    input  wire        tohost_wr,
    input  wire [31:0] tohost_value,

    output reg  [31:0] run_cycles,
    output reg  [31:0] retired,
    output reg  [31:0] tohost_retired,
    output reg  [7:0]  tohost_writes,
    output reg  [31:0] first_tohost,
    output reg  [15:0] reset_releases,
    output reg  [31:0] free_cycles
);

    assign ext_reset_n = pl_resetn0 | ps_rst_override;
    assign dcm_locked  = 1'b1;

    reg rst_n_q = 1'b0;
    initial reset_releases = 16'd0;
    initial free_cycles = 32'd0;

    always @(posedge aclk) begin
        free_cycles <= free_cycles + 32'd1;
        rst_n_q <= rst_n;
        if (rst_n && !rst_n_q)
            reset_releases <= reset_releases + 16'd1;
    end

    always @(posedge aclk) begin
        if (!rst_n) begin
            run_cycles     <= 32'd0;
            retired        <= 32'd0;
            tohost_retired <= 32'd0;
            tohost_writes  <= 8'd0;
            first_tohost   <= 32'd0;
        end else begin
            if (tohost_writes == 8'd0)
                run_cycles <= run_cycles + 32'd1;
            if (retire_valid)
                retired <= retired + 32'd1;
            if (tohost_wr) begin
                if (tohost_writes == 8'd0) begin
                    first_tohost   <= tohost_value;
                    tohost_retired <= retired + (retire_valid ? 32'd1 : 32'd0);
                end
                if (tohost_writes != 8'hFF)
                    tohost_writes <= tohost_writes + 8'd1;
            end
        end
    end

endmodule

`default_nettype wire
