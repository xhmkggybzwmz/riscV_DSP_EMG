# RV32I CPU RTL file list.
#
# Format (parsed by scripts/sim/run_sim.py, not passed to tools verbatim):
#   - one path per line, relative to the repository root, forward slashes
#   - "+incdir+<dir>" adds an include directory
#   - "#" or "//" starts a comment
#
# Header files (*.vh) are pulled in with `include and are not listed as sources.

+incdir+rtl/cpu

rtl/cpu/rv32i_regfile.v
rtl/cpu/rv32i_alu.v
rtl/cpu/rv32i_decoder.v
rtl/cpu/rv32i_lsu.v
rtl/cpu/rv32i_core.v
