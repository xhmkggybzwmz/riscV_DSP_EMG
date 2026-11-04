# SoC (CPU subsystem) RTL file list - same format as rtl/cpu/cpu.f.
# The CPU core files come from rtl/cpu/cpu.f; this list adds the subsystem.

+incdir+rtl/cpu
+incdir+rtl/soc

rtl/soc/soc_ram_dp.v
rtl/soc/soc_dmem_router.v
rtl/soc/cmi2axil.v
rtl/soc/soc_sysctrl.v
rtl/soc/rv32_soc_top.v
