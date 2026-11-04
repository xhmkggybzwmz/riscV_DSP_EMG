"""PynqMailboxDevice with a stand-in for pynq.Overlay (no hardware, no pynq): artifact /
board / HWH / PYNQ-entry checks, refusal before any PL access, release of the windows."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from ps_mailbox import protocol as P
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.overlay_device import DeviceError, PynqMailboxDevice, bit2bin, bit_header, hwh_ranges, sha256_file
from ps_mailbox.pynq_reader import CONFIG, MailboxReader
from ps_mailbox.registers import MmioWindow

PKG = Path(__file__).resolve().parents[1]
HWH = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<EDKSYSTEM EDWVERSION="1.2" TIMESTAMP="Tue Oct 27 15:22:21 2026" VIVADOVERSION="2026.1">
  <MODULES>
    <MODULE FULLNAME="/zynq_ultra_ps_e_0" INSTANCE="zynq_ultra_ps_e_0" MODTYPE="zynq_ultra_ps_e" VLNV="xilinx.com:ip:zynq_ultra_ps_e:3.5">
      <MEMORYMAP>
        <MEMRANGE BASEVALUE="0xA0000000" HIGHVALUE="0xA003FFFF" INSTANCE="dbg" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="REGISTER" SLAVEBUSINTERFACE="s_axi"/>
        <MEMRANGE BASEVALUE="{mbx_base}" HIGHVALUE="{mbx_high}" INSTANCE="mbx_ctrl" MASTERBUSINTERFACE="M_AXI_HPM0_FPD" MEMTYPE="MEMORY" SLAVEBUSINTERFACE="S_AXI"/>
      </MEMORYMAP>
    </MODULE>
    <MODULE FULLNAME="/dbg" INSTANCE="dbg" MODTYPE="kv260_dbg_axil" VLNV="xilinx.com:module_ref:kv260_dbg_axil:1.0"/>
    <MODULE FULLNAME="/mbx_ctrl" INSTANCE="mbx_ctrl" MODTYPE="axi_bram_ctrl" VLNV="xilinx.com:ip:axi_bram_ctrl:4.1"/>
  </MODULES>
</EDKSYSTEM>
"""


def make_bit(data: bytes) -> bytes:
    def field(tag, text):
        b = text.encode() + b"\0"
        return tag.encode() + struct.pack(">h", len(b)) + b
    head = struct.pack(">h", 9) + bytes(9) + struct.pack(">h", 1)
    return (head + field("a", "kv260_bd_wrapper;UserID=0XFFFFFFFF;Version=2026.1") + field("b", "xck26-sfvc784-2LV-c")
            + field("c", "2026/10/27") + field("d", "15:29:26") + b"e" + struct.pack(">i", len(data)) + data)


class FakeOverlay:
    def __init__(self, path, mbx_base=0xA0100000):
        self.bitfile_name = path
        self.timestamp = ""
        self.device = type("D", (), {"bitfile_name": path})()
        self.parser = type("Parser", (), {"bin_data": bit2bin(Path(path).read_bytes())})()
        self.ip_dict = {"dbg": {"phys_addr": 0xA0000000, "addr_range": 0x40000, "type": "xilinx.com:module_ref:kv260_dbg_axil:1.0",
                                "fullpath": "dbg"}}
        self.mem_dict = {"mbx_ctrl": {"base_address": mbx_base, "size": 0x1000, "used": 1, "type": "MEMORY"}}

    def is_loaded(self):
        return self.bitfile_name == self.device.bitfile_name


class Win(MmioWindow):
    """debug window stand-in: ID register only"""
    def __init__(self, size, ident=0x52563641):
        super().__init__(size)
        self.ident = ident

    def _rd(self, off):
        return self.ident if off == 0 else 0


class TestOverlayDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.d = d
        self.bit = d / "mbx_rt2k.bit"
        self.bit.write_bytes(make_bit(bytes(range(256)) * 16))
        self.hwh = d / "mbx_rt2k.hwh"
        self.write_hwh(0xA0100000, 0x1000)
        self.bin = d / "rv32_6a.bit.bin"
        self.bin.write_bytes(bit2bin(self.bit.read_bytes()))
        self.man = {
            "bitstream": {"bit_sha256": sha256_file(self.bit), "bit_bin_sha256": sha256_file(self.bin)},
            "git": {"commit": "c0ffee", "dirty": False},
            "mailbox": {"ps_base": "0xA0100000", "hwh_sha256": sha256_file(self.hwh),
                        "protocol.py_sha256": sha256_file(PKG / "protocol.py"),
                        "kv260_mailbox.json_sha256": sha256_file(CONFIG)},
            "checks": [{"name": "x", "ok": True, "detail": ""}]}
        self.write_manifest()
        sys_ = d / "sys"
        (sys_ / "sys/kernel/config/device-tree/overlays/rv32_6a").mkdir(parents=True)
        (sys_ / "sys/kernel/config/device-tree/overlays/rv32_6a/status").write_text("applied\n")
        (sys_ / "sys/class/fpga_manager/fpga0").mkdir(parents=True)
        (sys_ / "sys/class/fpga_manager/fpga0/state").write_text("operating\n")
        (sys_ / "sys/class/fpga_manager/fpga0/flags").write_text("100\n")
        self.sys = sys_
        self.box = FakeMailbox()
        prod = FakeProducer(self.box)
        prod.init(0)
        for n in range(1, 4):
            prod.publish(make_result(n))
        self.opened = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_hwh(self, base, size):
        self.hwh.write_text(HWH.format(mbx_base=f"0x{base:08X}", mbx_high=f"0x{base + size - 1:08X}"), encoding="utf-8")

    def write_manifest(self):
        self.bit.with_suffix(".manifest.json").write_text(json.dumps(self.man), encoding="utf-8")

    def mmio(self, base, size):
        w = self.box.ps if base == 0xA0100000 else Win(size)
        self.opened.append((base, size))
        return w

    def dev(self, **kw):
        kw.setdefault("overlay_factory", FakeOverlay)
        return PynqMailboxDevice(self.bit, loaded_bin=self.bin, sysfs_root=str(self.sys), mmio_factory=self.mmio, **kw)

    def test_verify_open_read_close(self):
        dev = self.dev()
        rep = dev.verify()
        self.assertEqual(rep["overlay"]["mailbox"]["base"], "0xA0100000")
        self.assertTrue(rep["overlay"]["is_loaded"])
        self.assertEqual(rep["overlay"]["pynq_bin_data_sha256"], self.man["bitstream"]["bit_bin_sha256"])
        self.assertEqual(rep["debug_id"], "0x52563641")
        w = dev.open_mailbox()
        v = dev.probe_header(w)
        self.assertEqual(v["MAGIC"], P.MAGIC)
        recs = [e for e in MailboxReader(w).poll() if e["type"] == "record"]
        self.assertEqual([r["result_index"] for r in recs], [1, 2, 3])
        self.assertEqual([r["epoch_seq"] for r in recs], [0, 1, 2])
        dev.close()
        self.assertEqual(dev.windows, [])
        self.assertEqual(self.opened, [(0xA0000000, 0x40000), (0xA0100000, 0x1000)])

    def test_bit2bin_and_header(self):
        hdr, data = bit_header(self.bit.read_bytes())
        self.assertEqual(hdr["b"], "xck26-sfvc784-2LV-c")
        b = bit2bin(self.bit.read_bytes())
        self.assertEqual(b[:4], data[3::-1])
        self.assertEqual(len(b), len(data))

    def test_hwh_ranges(self):
        r = {x["instance"]: x for x in hwh_ranges(self.hwh)}
        self.assertEqual((r["mbx_ctrl"]["base"], r["mbx_ctrl"]["size"], r["mbx_ctrl"]["modtype"]),
                         (0xA0100000, 0x1000, "axi_bram_ctrl"))

    def refused(self, dev, what):
        with self.assertRaises(DeviceError) as cm:
            dev.verify()
        self.assertIn(what, str(cm.exception))
        self.assertEqual(self.opened, [], "no PL window may be opened")
        with self.assertRaises(DeviceError):
            dev.open_mailbox()

    def test_refuse_dirty_build(self):
        self.man["git"]["dirty"] = True
        self.write_manifest()
        self.refused(self.dev(), "dirty")

    def test_refuse_failed_manifest_check(self):
        self.man["checks"].append({"name": "timing", "ok": False, "detail": ""})
        self.write_manifest()
        self.refused(self.dev(), "manifest checks failed")

    def test_refuse_other_hwh(self):
        self.write_hwh(0xA0200000, 0x1000)      # sha differs from the manifest
        self.refused(self.dev(), "hwh_sha256")

    def test_refuse_hwh_address_mismatch(self):
        self.write_hwh(0xA0200000, 0x1000)
        self.man["mailbox"]["hwh_sha256"] = sha256_file(self.hwh)
        self.write_manifest()
        self.refused(self.dev(), "HWH mailbox range")

    def test_refuse_pynq_entry_mismatch(self):
        self.refused(self.dev(overlay_factory=lambda p: FakeOverlay(p, mbx_base=0xA0110000)), "PYNQ entry")

    def test_refuse_other_loaded_firmware(self):
        self.bin.write_bytes(b"\0" * 64)
        self.refused(self.dev(), "FPGA manager firmware")

    def test_refuse_overlay_not_applied(self):
        (self.sys / "sys/kernel/config/device-tree/overlays/rv32_6a/status").write_text("unapplied\n")
        self.refused(self.dev(), "not applied")

    def test_refuse_wrong_debug_id(self):
        self.mmio_ident = 0
        dev = PynqMailboxDevice(self.bit, loaded_bin=self.bin, sysfs_root=str(self.sys), overlay_factory=FakeOverlay,
                                mmio_factory=lambda b, s: Win(s, ident=0x12345678))
        with self.assertRaises(DeviceError):
            dev.verify()
        self.assertEqual(dev.windows, [])

    def test_refuse_side_effect(self):
        state = self.sys / "sys/class/fpga_manager/fpga0/flags"

        def loading_overlay(path):              # an Overlay that would reconfigure the PL
            state.write_text("0\n")
            return FakeOverlay(path)
        self.refused(self.dev(overlay_factory=loading_overlay), "changed the system")

    def test_probe_header_rejects_other_content(self):
        dev = self.dev()
        dev.verify()
        w = dev.open_mailbox()
        self.box.mem[0:4] = (0x12345678).to_bytes(4, "little")
        with self.assertRaises(DeviceError):
            dev.probe_header(w)
        dev.close()

    def test_window_refuses_after_close(self):
        dev = self.dev()
        dev.verify()
        w = dev.open_mailbox()
        dev.close()
        from ps_mailbox.registers import MmioError
        with self.assertRaises(MmioError):
            w.read32(0)


class TestGolden(unittest.TestCase):
    VEC = Path(__file__).resolve().parents[3] / "build" / "e3" / "vec" / "chain_s1_normal"

    @unittest.skipUnless((VEC / "logits.hex").is_file(), "E2 vectors not built (build/e3/vec)")
    def test_matches_firmware_expectation(self):
        from ps_mailbox.golden import check, expected, from_vectors
        g = from_vectors(self.VEC)
        self.assertEqual(len(g["results"]), 11)
        self.assertEqual(g["results"][0]["scores"], [-673553, 664745, -467160, -42831, -97672, -867340])
        e = expected(g, 12)                       # pass 1, result 1
        self.assertEqual((e["result_index"], e["esi_timestamp"], e["segment"]), (13, 499 + 1400, 1))
        rec = dict(e)
        self.assertEqual(check(rec, g, 12), [])
        rec2 = copy.deepcopy(rec)
        rec2["scores"][3] += 1
        self.assertEqual(len(check(rec2, g, 12)), 1)


if __name__ == "__main__":
    unittest.main()
