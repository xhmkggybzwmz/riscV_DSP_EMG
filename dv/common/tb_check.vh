// tb_check.vh - shared self-checking helpers for Verilog-2001 testbenches.
//
// Usage (inside a testbench module body):
//   localparam TB_TIMEOUT_NS = 1000000;   // must be declared before the include
//   `include "tb_check.vh"
//   initial begin
//     tb_init;                            // reads +seed=N (default 1)
//     ... tb_check("name", got, expected); ...
//     tb_finish;                          // prints TEST_RESULT and ends simulation
//   end
//
// Result protocol (parsed by scripts/sim/run_sim.py):
//   "TEST_RESULT: PASS checks=<n> errors=0"
//   "TEST_RESULT: FAIL checks=<n> errors=<m>"
// Each mismatch prints a line starting with "ERROR:".
// Under Icarus Verilog the simulator exit code is 0 on PASS and 1 on FAIL.

integer tb_checks = 0;
integer tb_errors = 0;
integer tb_seed   = 1;
integer tb_max_err_print = 20;

task tb_init;
    begin
        if (!$value$plusargs("seed=%d", tb_seed))
            tb_seed = 1;
        $display("INFO: seed=%0d", tb_seed);
        // Harness self-test: +tb_selftest_fail must turn any test into a FAIL.
        if ($test$plusargs("tb_selftest_fail"))
            tb_fail("tb_selftest_fail: deliberate failure requested");
    end
endtask

// Compare up to 32 bits with 4-state equality (X/Z never match a 0/1 value).
task tb_check;
    input [8*64-1:0] what;
    input [31:0]     got;
    input [31:0]     exp;
    begin
        tb_checks = tb_checks + 1;
        if (got !== exp) begin
            tb_errors = tb_errors + 1;
            if (tb_errors <= tb_max_err_print)
                $display("ERROR: %0s: got=%h exp=%h (t=%0t)", what, got, exp, $time);
            else if (tb_errors == tb_max_err_print + 1)
                $display("ERROR: further mismatches are counted but not printed");
        end
    end
endtask

task tb_fail;
    input [8*64-1:0] what;
    begin
        tb_checks = tb_checks + 1;
        tb_errors = tb_errors + 1;
        $display("ERROR: %0s (t=%0t)", what, $time);
    end
endtask

task tb_finish;
    begin
        if (tb_errors == 0 && tb_checks > 0)
            $display("TEST_RESULT: PASS checks=%0d errors=0", tb_checks);
        else
            $display("TEST_RESULT: FAIL checks=%0d errors=%0d", tb_checks, tb_errors);
`ifdef __ICARUS__
        $finish_and_return((tb_errors == 0 && tb_checks > 0) ? 0 : 1);
`else
        $finish;
`endif
    end
endtask

// Watchdog: a testbench that never reaches tb_finish fails instead of hanging.
initial begin
    #(TB_TIMEOUT_NS);
    $display("ERROR: watchdog timeout after %0d ns", TB_TIMEOUT_NS);
    tb_errors = tb_errors + 1;
    tb_finish;
end
