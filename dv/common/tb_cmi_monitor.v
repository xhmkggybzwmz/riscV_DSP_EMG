`timescale 1ns / 1ps
`default_nettype none

// tb_cmi_monitor - passive checker for one CMI port (docs/cpu/cpu_mem_interface.md).
//
// Checks (each violation prints a line starting with "ERROR:" and increments
// `errors`):
//   C1  req_valid is 0 while rst is high, and in the first cycle after rst is
//       released (a request may start only after the first rising edge with rst low)
//   C2  a pending request (valid && !ready) keeps valid=1 and all request
//       fields unchanged in the next cycle
//   C3  at most one outstanding request: no handshake while one is outstanding
//   C4  every response has an outstanding request (no unsolicited response, no
//       response in the handshake cycle, no stale response after rst)
//   C5  no new request in the cycle in which a response arrives (rule R6)
//   C6  no request while `halted` is high
//   C7  read requests have wstrb = 0; write requests have wstrb != 0
//   C8  instruction-port addresses are 4-byte aligned (CHECK_ALIGN = 1)
// Counters (cleared by rst): handshakes, write handshakes, responses, errors.

module tb_cmi_monitor #(
    parameter NAME        = "cmi",
    parameter CHECK_ALIGN = 0
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        halted,

    input  wire        req_valid,
    input  wire        req_ready,
    input  wire [31:0] req_addr,
    input  wire        req_we,
    input  wire [3:0]  req_wstrb,
    input  wire [31:0] req_wdata,
    input  wire        rsp_valid,
    input  wire        rsp_err,

    output reg  [31:0] errors,
    output reg  [31:0] n_handshakes,
    output reg  [31:0] n_write_handshakes,
    output reg  [31:0] n_responses,
    output reg  [31:0] n_error_responses,
    output reg         outstanding
);

    reg        pend;              // last cycle had valid && !ready (outside reset)
    reg        prev_rst = 1'b0;   // rst at the previous rising edge
    reg [31:0] p_addr, p_wdata;
    reg        p_we;
    reg [3:0]  p_wstrb;

    initial errors = 0;

    task violation;
        input [8*72-1:0] what;
        begin
            errors = errors + 1;
            $display("ERROR: [%0s monitor] %0s (t=%0t addr=%h)", NAME, what, $time, req_addr);
        end
    endtask

    wire hs = req_valid && req_ready;

    always @(posedge clk) begin
        prev_rst <= rst;
        if (!rst && prev_rst && req_valid)
            violation("C1 req_valid asserted in the first cycle after reset release");
        if (rst) begin
            if (req_valid) violation("C1 req_valid asserted during reset");
            pend               <= 1'b0;
            outstanding        <= 1'b0;
            n_handshakes       <= 0;
            n_write_handshakes <= 0;
            n_responses        <= 0;
            n_error_responses  <= 0;
        end else begin
            // C2
            if (pend) begin
                if (!req_valid)
                    violation("C2 req_valid withdrawn before handshake");
                else if (req_addr !== p_addr || req_we !== p_we ||
                         req_wstrb !== p_wstrb || (p_we && req_wdata !== p_wdata))
                    violation("C2 request fields changed while waiting for ready");
            end
            // C3
            if (hs && outstanding)
                violation("C3 second request accepted while one is outstanding");
            // C4
            if (rsp_valid && !outstanding)
                violation("C4 response without an outstanding request (early/stale/unsolicited)");
            // C5
            if (rsp_valid && req_valid)
                violation("C5 new request in the response cycle");
            // C6
            if (halted && req_valid)
                violation("C6 request issued while halted");
            // C7
            if (req_valid && !req_we && req_wstrb != 4'b0000)
                violation("C7 read request with non-zero wstrb");
            if (req_valid && req_we && req_wstrb == 4'b0000)
                violation("C7 write request with zero wstrb");
            // C8
            if (CHECK_ALIGN && req_valid && req_addr[1:0] != 2'b00)
                violation("C8 misaligned instruction fetch address");

            pend    <= req_valid && !req_ready;
            p_addr  <= req_addr;
            p_we    <= req_we;
            p_wstrb <= req_wstrb;
            p_wdata <= req_wdata;

            if (hs) begin
                n_handshakes <= n_handshakes + 1;
                if (req_we) n_write_handshakes <= n_write_handshakes + 1;
            end
            if (rsp_valid) begin
                n_responses <= n_responses + 1;
                if (rsp_err) n_error_responses <= n_error_responses + 1;
            end
            outstanding <= (outstanding | hs) & ~rsp_valid;
        end
    end

endmodule

`default_nettype wire
