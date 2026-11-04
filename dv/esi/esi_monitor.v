`timescale 1ns / 1ps
`default_nettype none

// esi_monitor - ESI v1 protocol checks (docs/soc/esi_v1.md section 3):
//   E1 tvalid = 0 during reset and in the first cycle after reset
//   E2 once tvalid is high it stays high until the handshake
//   E3 tdata / tuser / tlast stable while tvalid && !tready
//   E4 no X/Z on tvalid / tready; no X/Z on the payload while tvalid
// Counts handshakes; errors are reported with the monitor name and simulation time.

module esi_monitor #(
    parameter NAME = "esi"
) (
    input  wire         clk,
    input  wire         rst,
    input  wire         tvalid,
    input  wire         tready,
    input  wire [127:0] tdata,
    input  wire [79:0]  tuser,
    input  wire         tlast,
    output integer      errors,
    output integer      handshakes
);
    reg         pv, pr, prst;
    reg [127:0] pd;
    reg [79:0]  pu;
    reg         pl;

    initial begin errors = 0; handshakes = 0; pv = 1'b0; pr = 1'b0; prst = 1'b1; end

    task err;
        input [8*64-1:0] what;
        begin
            errors = errors + 1;
            if (errors <= 20) $display("ERROR: [%0s] %0s (t=%0t)", NAME, what, $time);
        end
    endtask

    always @(posedge clk) begin
        if ((tvalid !== 1'b0 && tvalid !== 1'b1) || (!rst && tready !== 1'b0 && tready !== 1'b1))
            err("E4 X/Z on tvalid/tready");
        if (rst || prst) begin
            if (tvalid === 1'b1) err("E1 tvalid during reset or first cycle after");
        end else begin
            if (tvalid === 1'b1 && (^tdata === 1'bx || ^tuser === 1'bx || ^tlast === 1'bx))
                err("E4 X/Z on payload while tvalid");
            if (pv && !pr) begin
                if (tvalid !== 1'b1) err("E2 tvalid dropped before handshake");
                else if (tdata !== pd || tuser !== pu || tlast !== pl) err("E3 payload changed while stalled");
            end
            if (tvalid === 1'b1 && tready === 1'b1) handshakes = handshakes + 1;
        end
        pv <= (tvalid === 1'b1) && !rst;
        pr <= (tready === 1'b1);
        pd <= tdata; pu <= tuser; pl <= tlast;
        prst <= rst;
    end
endmodule

`default_nettype wire
