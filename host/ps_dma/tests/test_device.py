"""PynqDmaDevice with a stand-in for pynq.Overlay: the DMA path is checked before any access to its windows."""

import json
import tempfile
import unittest
from pathlib import Path

from ps_dma import registers as R
from ps_dma.device import PynqDmaDevice, hwh_dma_info
from ps_mailbox.overlay_device import DeviceError, bit2bin, sha256_file
from ps_mailbox.pynq_reader import CONFIG
from ps_mailbox.registers import MmioWindow
from ps_mailbox.tests.test_overlay_device import PKG, FakeOverlay, make_bit

REPO = Path(__file__).resolve().parents[3]
BUILT_HWH = REPO / "build" / "vivado" / "kv260_e5a" / "mbx_dma_live" / "out" / "kv260_soc.hwh"
HWH = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<EDKSYSTEM EDWVERSION="1.2" TIMESTAMP="Tue Oct 27 15:22:21 2026" VIVADOVERSION="2026.1">
  <MODULES>
    <MODULE FULLNAME="/zynq_ultra_ps_e_0" INSTANCE="zynq_ultra_ps_e_0" MODTYPE="zynq_ultra_ps_e" VLNV="xilinx.com:ip:zynq_ultra_ps_e:3.5">
      <PARAMETERS><PARAMETER NAME="C_SAXIGP2_DATA_WIDTH" VALUE="{hp0}"/></PARAMETERS>
      <MEMORYMAP>
        <MEMRANGE BASEVALUE="0xA0000000" HIGHVALUE="0xA003FFFF" INSTANCE="dbg" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="REGISTER" SLAVEBUSINTERFACE="s_axi"/>
        <MEMRANGE BASEVALUE="0xA0100000" HIGHVALUE="0xA0100FFF" INSTANCE="mbx_ctrl" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="MEMORY" SLAVEBUSINTERFACE="S_AXI"/>
        <MEMRANGE BASEVALUE="{dma}" HIGHVALUE="{dma_hi}" INSTANCE="axi_dma" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="REGISTER" SLAVEBUSINTERFACE="S_AXI_LITE"/>
        <MEMRANGE BASEVALUE="0xA0120000" HIGHVALUE="0xA0120FFF" INSTANCE="e3c" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="REGISTER" SLAVEBUSINTERFACE="s_axi_rep"/>
      </MEMORYMAP>
    </MODULE>
    <MODULE FULLNAME="/dbg" INSTANCE="dbg" MODTYPE="kv260_dbg_axil" VLNV="xilinx.com:module_ref:kv260_dbg_axil:1.0"/>
    <MODULE FULLNAME="/mbx_ctrl" INSTANCE="mbx_ctrl" MODTYPE="axi_bram_ctrl" VLNV="xilinx.com:ip:axi_bram_ctrl:4.1"/>
    <MODULE FULLNAME="/e3c" INSTANCE="e3c" MODTYPE="emg_e5_subsys" VLNV="xilinx.com:module_ref:emg_e5_subsys:1.0"/>
    <MODULE FULLNAME="/axi_dma" INSTANCE="axi_dma" MODTYPE="axi_dma" VLNV="xilinx.com:ip:axi_dma:7.1">
      <PARAMETERS>
        <PARAMETER NAME="C_M_AXI_MM2S_DATA_WIDTH" VALUE="{dma_w}"/>
        <PARAMETER NAME="C_M_AXIS_MM2S_TDATA_WIDTH" VALUE="64"/>
        <PARAMETER NAME="C_ADDR_WIDTH" VALUE="40"/>
        <PARAMETER NAME="C_INCLUDE_SG" VALUE="{sg}"/>
        <PARAMETER NAME="C_INCLUDE_S2MM" VALUE="0"/>
      </PARAMETERS>
    </MODULE>
  </MODULES>
</EDKSYSTEM>
"""


class Win(MmioWindow):
    def __init__(self, size, regs):
        super().__init__(size)
        self.regs = regs

    def _rd(self, off):
        return self.regs.get(off, 0)


class TestDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.bit = d / "mbx_dma_live.bit"
        self.bit.write_bytes(make_bit(bytes(range(256)) * 16))
        self.hwh = d / "mbx_dma_live.hwh"
        self.bin = d / "rv32_6a.bit.bin"
        self.bin.write_bytes(bit2bin(self.bit.read_bytes()))
        sys_ = d / "sys"
        (sys_ / "sys/kernel/config/device-tree/overlays/rv32_6a").mkdir(parents=True)
        (sys_ / "sys/kernel/config/device-tree/overlays/rv32_6a/status").write_text("applied\n")
        (sys_ / "sys/class/fpga_manager/fpga0").mkdir(parents=True)
        (sys_ / "sys/class/fpga_manager/fpga0/state").write_text("operating\n")
        (sys_ / "sys/class/fpga_manager/fpga0/flags").write_text("100\n")
        self.sys = sys_
        self.opened = []
        self.afi = 0x3B0                                  # FABRIC_WIDTH 0 = 128 bit
        self.write(hp0=128)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, hp0=128, dma_w=128, sg=0, dma=0xA0110000):
        self.hwh.write_text(HWH.format(hp0=hp0, dma_w=dma_w, sg=sg, dma=f"0x{dma:08X}", dma_hi=f"0x{dma + 0xFFFF:08X}"),
                            encoding="utf-8")
        man = {"bitstream": {"bit_sha256": sha256_file(self.bit), "bit_bin_sha256": sha256_file(self.bin)},
               "git": {"commit": "c0ffee", "dirty": False},
               "mailbox": {"ps_base": "0xA0100000", "hwh_sha256": sha256_file(self.hwh),
                           "protocol.py_sha256": sha256_file(PKG / "protocol.py"),
                           "kv260_mailbox.json_sha256": sha256_file(CONFIG)},
               "checks": [{"name": "x", "ok": True, "detail": ""}]}
        self.bit.with_suffix(".manifest.json").write_text(json.dumps(man), encoding="utf-8")

    def mmio(self, base, size):
        self.opened.append(base)
        regs = {0: 0x52563641} if base == 0xA0000000 else {0: self.afi} if base == R.AFI_HP0_BASE else {}
        return Win(size, regs)

    def dev(self):
        return PynqDmaDevice(self.bit, loaded_bin=self.bin, sysfs_root=str(self.sys), mmio_factory=self.mmio,
                             overlay_factory=FakeOverlay)

    def test_good_design(self):
        d = self.dev()
        rep = d.verify()
        self.assertEqual(rep["dma_path"]["afi_width"], 128)
        self.assertEqual(rep["dma_path"]["hp0_data_width"], 128)
        d.open_dma()
        d.open_source()
        self.assertEqual(self.opened, [0xA0000000, R.AFI_HP0_BASE, R.DMA_BASE, R.SRC_BASE])
        d.close()
        self.assertEqual(d.windows, [])

    def assertRefused(self, pattern):
        d = self.dev()
        with self.assertRaisesRegex(DeviceError, pattern):
            d.verify()
        self.assertFalse(self.opened and (R.DMA_BASE in self.opened or R.SRC_BASE in self.opened))
        d.close()

    def test_afi_width_mismatch_refused(self):
        self.afi = 0x3B1                                  # 64 bit on the PS side
        self.assertRefused("data would be corrupted")

    def test_dma_address_mismatch_refused(self):
        self.write(dma=0xA0130000)
        self.assertRefused("HWH DMA range")

    def test_scatter_gather_refused(self):
        self.write(sg=1)
        self.assertRefused("not MM2S simple mode")

    def test_dma_narrower_than_port_refused(self):
        self.write(dma_w=64)
        self.assertRefused("widths")

    def test_old_design_without_dma_refused(self):
        self.hwh.write_text(HWH.format(hp0=128, dma_w=128, sg=0, dma="0xA0110000", dma_hi="0xA011FFFF")
                            .replace('MODTYPE="axi_dma"', 'MODTYPE="other"'), encoding="utf-8")
        man = json.loads(self.bit.with_suffix(".manifest.json").read_text())
        man["mailbox"]["hwh_sha256"] = sha256_file(self.hwh)
        self.bit.with_suffix(".manifest.json").write_text(json.dumps(man))
        self.assertRefused("not a DMA design")

    def test_open_needs_verify(self):
        d = self.dev()
        with self.assertRaises(DeviceError):
            d.open_dma()
        with self.assertRaises(DeviceError):
            d.open_source()

    @unittest.skipUnless(BUILT_HWH.is_file(), "mbx_dma_live not built")
    def test_built_hwh_parses(self):
        info = hwh_dma_info(BUILT_HWH)
        self.assertEqual((info["mm2s_data_width"], info["mm2s_stream_width"], info["sg"], info["s2mm"], info["hp0_data_width"]),
                         (128, 64, 0, 0, 128))


if __name__ == "__main__":
    unittest.main()
