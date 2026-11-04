# KV260 J2 Pmod, from the official Xilinx kv260_bist pin constraints.
# J2.1 SCLK, J2.3 MOSI, J2.5 CS_N, J2.7 READY; J2.9 is GND.
set_property PACKAGE_PIN H12 [get_ports spi_sclk]
set_property PACKAGE_PIN E10 [get_ports spi_mosi]
set_property PACKAGE_PIN D10 [get_ports spi_cs_n]
set_property PACKAGE_PIN C11 [get_ports spi_ready]
set_property IOSTANDARD LVCMOS33 [get_ports {spi_sclk spi_mosi spi_cs_n spi_ready}]
set_property PULLUP true [get_ports spi_cs_n]
set_property SLEW SLOW [get_ports spi_ready]
set_property DRIVE 4 [get_ports spi_ready]

# SPI is deliberately treated as asynchronous data and oversampled by PL_CLK0.
# ASYNC_REG attributes on the first synchronizers cover the CDC path.
set_false_path -from [get_ports {spi_sclk spi_mosi spi_cs_n}]
