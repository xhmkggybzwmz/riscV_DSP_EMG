`timescale 1ns / 1ps
`default_nettype none

// esi_src_bfm - ESI v1 frame source for testbenches (docs/soc/esi_v1.md).
//
// Frames are 176-bit words (reference/emg_fixed/vectors.py): lanes [127:0], flags [143:128],
// seq [175:144]; timestamp = frame index (written into tuser[79:48]) unless a ts file is used.
// The TB loads frames into `mem` (task load) and calls `start`. Before each frame the BFM idles
// for a random number of cycles (valid_pct = probability in % of presenting the frame in a given
// cycle; 100 = back to back). tvalid then stays high until the handshake and the payload is
// stable (ESI rule 2). tlast = 1 on every tlast_period-th frame and on the last frame.
// pause_at: the BFM stops before frame index pause_at until `release` is pulsed.

module esi_src_bfm #(
    parameter MAXF = 40000
) (
    input  wire         clk,
    input  wire         rst,
    output reg          tvalid,
    input  wire         tready,
    output reg  [127:0] tdata,
    output reg  [79:0]  tuser,
    output reg          tlast,
    output reg          done,
    output integer      sent
);
    reg [175:0] mem [0:MAXF-1];
    integer nframes, valid_pct, tlast_period, pause_at, rnd_seed;
    reg running, released;

    initial begin
        tvalid = 1'b0; tdata = 128'd0; tuser = 80'd0; tlast = 1'b0; done = 1'b0; sent = 0;
        nframes = 0; valid_pct = 100; tlast_period = 100; pause_at = -1; rnd_seed = 1;
        running = 1'b0; released = 1'b0;
    end

    task load;
        input [8*512-1:0] fname;
        input integer n;
        begin
            $readmemh(fname, mem, 0, n - 1);
            nframes = n;
        end
    endtask

    task configure;
        input integer vpct;
        input integer tl_period;
        input integer seed;
        begin
            valid_pct = vpct; tlast_period = tl_period; rnd_seed = seed;
        end
    endtask

    task start;
        begin running = 1'b1; end
    endtask

    task release_pause;
        begin released = 1'b1; end
    endtask

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            tvalid <= 1'b0;
            i = 0;
            sent <= 0;
            done <= 1'b0;
        end else if (running && !done) begin
            if (tvalid && tready) begin
                tvalid <= 1'b0;
                sent <= sent + 1;
                i = i + 1;
                if (i >= nframes) done <= 1'b1;
            end else if (!tvalid && i < nframes) begin
                if (i == pause_at && !released) begin
                    // hold
                end else if (({$random(rnd_seed)} % 100) < valid_pct) begin
                    tvalid <= 1'b1;
                    tdata  <= mem[i][127:0];
                    tuser  <= {i[31:0], mem[i][175:144], mem[i][143:128]};
                    tlast  <= ((i + 1) % tlast_period == 0) || (i == nframes - 1);
                end
            end
        end
    end
endmodule

`default_nettype wire
