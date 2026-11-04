`timescale 1ns / 1ps
`default_nettype none

// tb_axil_slave_bfm - memory-backed AXI4-Lite slave for testbenches.
//
// Handshake behaviour (configure with tasks before/while running):
//   aw_pct / w_pct / ar_pct : probability (%) that the READY may be given in a cycle
//   order mode (set_mode):
//     0 RANDOM      AW and W accepted independently, in any order
//     1 AW_FIRST    W is accepted only after AW has been accepted
//     2 W_FIRST     AW is accepted only after W has been accepted
//     3 BOTH        AWREADY and WREADY only together, and only when both
//                   AWVALID and WVALID are high (the master must not wait
//                   for one READY before raising the other VALID)
//   b_lat / r_lat range : cycles from the last of AW/W (or AR) to B (R) VALID
// Responses:
//   BVALID only after both AW and W were accepted; BVALID/RVALID held until
//   the master's READY; payload stable meanwhile. One write and one read in
//   flight at most (AXI4-Lite masters here issue one transaction at a time).
//   Error windows (inclusive): SLVERR, DECERR, EXOKAY (an illegal AXI4-Lite
//   response, used to check that the master treats it as an error). A write
//   with an error response does not modify memory. Read data = memory word.
// Counters: handshakes per channel and memory writes performed.

module tb_axil_slave_bfm #(
    parameter WORDS = 1024                  // memory window: address bits [log2(WORDS)+1:2]
) (
    input  wire        aclk,
    input  wire        aresetn,
    input  wire [31:0] s_axi_awaddr,
    input  wire [2:0]  s_axi_awprot,
    input  wire        s_axi_awvalid,
    output reg         s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output reg         s_axi_wready,
    output reg  [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [31:0] s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output reg         s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output reg  [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready
);
    reg [31:0] mem [0:WORDS-1];

    integer aw_pct = 100, w_pct = 100, ar_pct = 100, mode = 0;
    integer b_lat_min = 0, b_lat_max = 0, r_lat_min = 0, r_lat_max = 0;
    integer seed = 1;
    reg        slv_en = 0, dec_en = 0, exo_en = 0;
    reg [31:0] slv_lo = 0, slv_hi = 0, dec_lo = 0, dec_hi = 0, exo_lo = 0, exo_hi = 0;

    integer n_aw, n_w, n_b, n_ar, n_r, n_mem_writes;

    task configure;
        input integer t_aw; input integer t_w; input integer t_ar; input integer t_mode;
        input integer t_bmin; input integer t_bmax; input integer t_rmin; input integer t_rmax;
        input integer t_seed;
        begin
            aw_pct = t_aw; w_pct = t_w; ar_pct = t_ar; mode = t_mode;
            b_lat_min = t_bmin; b_lat_max = t_bmax; r_lat_min = t_rmin; r_lat_max = t_rmax;
            seed = t_seed;
        end
    endtask

    task set_errors;            // window per response type; hi < lo disables
        input [31:0] s_lo; input [31:0] s_hi; input [31:0] d_lo; input [31:0] d_hi;
        input [31:0] e_lo; input [31:0] e_hi;
        begin
            slv_en = s_hi >= s_lo; slv_lo = s_lo; slv_hi = s_hi;
            dec_en = d_hi >= d_lo; dec_lo = d_lo; dec_hi = d_hi;
            exo_en = e_hi >= e_lo; exo_lo = e_lo; exo_hi = e_hi;
        end
    endtask

    task clear_counters;
        begin n_aw = 0; n_w = 0; n_b = 0; n_ar = 0; n_r = 0; n_mem_writes = 0; end
    endtask

    function integer urand;
        input integer m;
        integer r;
        begin r = $random(seed); if (r < 0) r = -r; urand = (m <= 0) ? 0 : r % m; end
    endfunction

    function [1:0] resp_for;
        input [31:0] a;
        begin
            if (dec_en && a >= dec_lo && a <= dec_hi)      resp_for = 2'b11;
            else if (slv_en && a >= slv_lo && a <= slv_hi) resp_for = 2'b10;
            else if (exo_en && a >= exo_lo && a <= exo_hi) resp_for = 2'b01;
            else                                            resp_for = 2'b00;
        end
    endfunction

    // write path state
    reg        aw_got, w_got;
    reg [31:0] aw_a, w_d;
    reg [3:0]  w_s;
    integer    b_cnt;
    reg        b_wait;
    // read path state
    reg [31:0] ar_a;
    integer    r_cnt;
    reg        r_wait;
    integer    k;
    reg        aw_ok, w_ok, both;

    initial clear_counters;

    always @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_awready <= 1'b0; s_axi_wready <= 1'b0; s_axi_arready <= 1'b0;
            s_axi_bvalid  <= 1'b0; s_axi_rvalid <= 1'b0;
            aw_got <= 1'b0; w_got <= 1'b0; b_wait <= 1'b0; r_wait <= 1'b0;
        end else begin
            // ---------------- AW / W acceptance ----------------
            if (s_axi_awvalid && s_axi_awready) begin
                aw_got <= 1'b1; aw_a <= s_axi_awaddr; n_aw = n_aw + 1;
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_got <= 1'b1; w_d <= s_axi_wdata; w_s <= s_axi_wstrb; n_w = n_w + 1;
            end
            // READY for the next cycle (registered); never while a write is pending
            aw_ok = !aw_got && !(s_axi_awvalid && s_axi_awready) && !b_wait && !s_axi_bvalid;
            w_ok  = !w_got  && !(s_axi_wvalid  && s_axi_wready)  && !b_wait && !s_axi_bvalid;
            case (mode)
                1: w_ok  = w_ok  && (aw_got || (s_axi_awvalid && s_axi_awready));
                2: aw_ok = aw_ok && (w_got  || (s_axi_wvalid  && s_axi_wready));
                default: ;
            endcase
            if (mode == 3) begin
                // one decision for both READYs: they are always given together
                both = aw_ok && w_ok && s_axi_awvalid && s_axi_wvalid && (urand(100) < aw_pct);
                s_axi_awready <= both;
                s_axi_wready  <= both;
            end else begin
                s_axi_awready <= aw_ok && (urand(100) < aw_pct);
                s_axi_wready  <= w_ok  && (urand(100) < w_pct);
            end

            // ---------------- B ----------------
            if (aw_got && w_got && !b_wait && !s_axi_bvalid) begin
                b_wait <= 1'b1;
                b_cnt  <= b_lat_min + urand(b_lat_max - b_lat_min + 1);
            end
            if (b_wait) begin
                if (b_cnt <= 0) begin
                    b_wait       <= 1'b0;
                    s_axi_bvalid <= 1'b1;
                    s_axi_bresp  <= resp_for(aw_a);
                    if (resp_for(aw_a) == 2'b00) begin
                        for (k = 0; k < 4; k = k + 1)
                            if (w_s[k]) mem[aw_a[31:2] % WORDS][8*k +: 8] = w_d[8*k +: 8];
                        n_mem_writes = n_mem_writes + 1;
                    end
                    aw_got <= 1'b0; w_got <= 1'b0;
                end else b_cnt <= b_cnt - 1;
            end
            if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid <= 1'b0; n_b = n_b + 1;
            end

            // ---------------- AR / R ----------------
            if (s_axi_arvalid && s_axi_arready) begin
                ar_a <= s_axi_araddr; n_ar = n_ar + 1;
                r_wait <= 1'b1;
                r_cnt  <= r_lat_min + urand(r_lat_max - r_lat_min + 1);
            end
            s_axi_arready <= !r_wait && !s_axi_rvalid && !(s_axi_arvalid && s_axi_arready) &&
                             (urand(100) < ar_pct);
            if (r_wait && !(s_axi_arvalid && s_axi_arready)) begin
                if (r_cnt <= 0) begin
                    r_wait       <= 1'b0;
                    s_axi_rvalid <= 1'b1;
                    s_axi_rresp  <= resp_for(ar_a);
                    s_axi_rdata  <= mem[ar_a[31:2] % WORDS];
                end else r_cnt <= r_cnt - 1;
            end
            if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0; n_r = n_r + 1;
            end
        end
    end
endmodule

`default_nettype wire
