`timescale 1ns / 1ps
`default_nettype none

// rv32i_regfile - RV32I integer register file (x0..x31, 32 bits each).
//
// - Two asynchronous (combinational) read ports, one synchronous write port.
// - x0 always reads as zero; writes to x0 are discarded.
// - No reset and no initial value. Software must write a register before
//   relying on its contents; simulation returns X for never-written registers.
// - No write-through: when a port reads the register that is being written in
//   the same cycle, it returns the old value until the rising edge, and the new
//   value afterwards.
// - Written for FPGA distributed RAM (LUTRAM) inference: one write port and
//   asynchronous reads. Actual inference must be confirmed in Vivado reports.
//
// Spec: docs/cpu/rv32i_core_spec.md

module rv32i_regfile (
    input  wire        clk,

    input  wire        we,        // write enable
    input  wire [4:0]  waddr,     // write register index
    input  wire [31:0] wdata,     // write data

    input  wire [4:0]  raddr1,    // read port 1 register index
    output wire [31:0] rdata1,    // read port 1 data (combinational)

    input  wire [4:0]  raddr2,    // read port 2 register index
    output wire [31:0] rdata2     // read port 2 data (combinational)
);

    reg [31:0] regs [0:31];       // entry 0 is never written and never read

    always @(posedge clk) begin
        if (we && (waddr != 5'd0))
            regs[waddr] <= wdata;
    end

    assign rdata1 = (raddr1 == 5'd0) ? 32'd0 : regs[raddr1];
    assign rdata2 = (raddr2 == 5'd0) ? 32'd0 : regs[raddr2];

endmodule

`default_nettype wire
