`timescale 1ns / 1ps
`default_nettype none

// esi_sink_bfm - ESI v1 frame sink: random tready (ready_pct %), captures every handshake.

module esi_sink_bfm #(
    parameter MAXF = 40000
) (
    input  wire         clk,
    input  wire         rst,
    input  wire         tvalid,
    output reg          tready,
    input  wire [127:0] tdata,
    input  wire [79:0]  tuser,
    input  wire         tlast,
    output integer      count
);
    reg [127:0] cap_data [0:MAXF-1];
    reg [79:0]  cap_user [0:MAXF-1];
    reg         cap_last [0:MAXF-1];
    integer ready_pct, rnd_seed;

    initial begin
        tready = 1'b0; count = 0; ready_pct = 100; rnd_seed = 7;
    end

    task configure;
        input integer rpct;
        input integer seed;
        begin ready_pct = rpct; rnd_seed = seed; end
    endtask

    always @(posedge clk) begin
        if (rst) begin
            tready <= 1'b0;
            count <= 0;
        end else begin
            if (tvalid && tready) begin
                cap_data[count] <= tdata;
                cap_user[count] <= tuser;
                cap_last[count] <= tlast;
                count <= count + 1;
            end
            tready <= (({$random(rnd_seed)} % 100) < ready_pct);
        end
    end
endmodule

`default_nettype wire
