"""registers.py against the RTL and the address map; the live-image record flags through the reader and the server."""

import re
import unittest
from pathlib import Path

from gesture_server import protocol as GP
from ps_dma import registers as R
from ps_mailbox import protocol as MP
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer, make_result
from ps_mailbox.pynq_reader import MailboxReader, to_json

REPO = Path(__file__).resolve().parents[3]


class TestRegisters(unittest.TestCase):
    def test_addresses_equal_soc_addr_map(self):
        vh = (REPO / "rtl" / "soc" / "soc_addr_map.vh").read_text(encoding="utf-8")
        m = {n: int(v.replace("_", ""), 16) for n, v in re.findall(r"`define (\w+) 32'h([0-9A-Fa-f_]+)", vh)}
        self.assertEqual((R.DMA_BASE, R.DMA_SIZE), (m["KV260_DMA_PS_BASE"], m["KV260_DMA_PS_SIZE"]))
        self.assertEqual((R.SRC_BASE, R.SRC_SIZE), (m["KV260_ESRC_PS_BASE"], m["KV260_ESRC_PS_SIZE"]))
        self.assertFalse(m["KV260_MBX_PS_BASE"] <= R.DMA_BASE < m["KV260_MBX_PS_BASE"] + m["KV260_MBX_PS_SIZE"])

    def test_source_page_equals_the_rtl(self):
        v = (REPO / "rtl" / "e3c" / "esi_dma_src_axil.v").read_text(encoding="utf-8")
        table = {n: int(o, 16) for o, n in re.findall(r"^// (0x[0-9A-F]{3})\s+([A-Z_]+)\s+R[OW]", v, re.M)}
        self.assertEqual(table["ID"], R.SRC_ID)
        self.assertEqual(table["CTRL"], R.SRC_CTRL)
        self.assertEqual(table["STATUS"], R.SRC_STATUS)
        self.assertEqual(table["PERIOD"], R.SRC_PERIOD)
        self.assertEqual(table["FRAME_CNT"], R.SRC_FRAME_CNT)
        self.assertEqual(table["LATE_CNT"], R.SRC_LATE_CNT)
        self.assertEqual(table["SEQ_BASE"], R.SRC_SEQ_BASE)
        self.assertEqual(table["SEG_CNT"], R.SRC_SEG_CNT)
        self.assertEqual(table["UNDERRUN_CNT"], R.SRC_UNDERRUN_CNT)
        self.assertEqual(table["KEEP_ERR_CNT"], R.SRC_KEEP_ERR_CNT)
        self.assertIn("32'h4553_4431", v)
        self.assertEqual(R.SRC_ID_VALUE, 0x45534431)

    def test_dma_width_equals_the_block_design(self):
        tcl = (REPO / "fpga" / "kv260" / "tcl" / "dma_bd.tcl").read_text(encoding="utf-8")
        self.assertIn("CONFIG.c_m_axi_mm2s_data_width {128}", tcl)
        self.assertIn("CONFIG.c_m_axis_mm2s_tdata_width {64}", tcl)
        self.assertIn("CONFIG.c_sg_length_width {26}", tcl)
        self.assertEqual(R.ADDR_ALIGN, 128 // 8)
        self.assertEqual(R.LENGTH_MAX, (1 << 26) - 1)


class TestLiveFlags(unittest.TestCase):
    def test_records_without_golden_flag_pass_reader_and_server(self):
        """firmware/rv32/dma cannot set F_GOLDEN_OK (unknown data), and STATE stays RUNNING"""
        box = FakeMailbox()
        prod = FakeProducer(box)
        prod.init(0)
        flags = MP.F_POSTPROC | MP.F_ARGMAX_OK
        for n in range(1, 6):
            prod.publish(make_result(n), flags=flags | (MP.F_NEW_SEGMENT if n == 1 else 0))
        rd = MailboxReader(box.ps)
        recs = [e for e in rd.poll() if "result_index" in e]
        self.assertEqual(len(recs), 5)
        self.assertEqual(rd.stats["errors"] + rd.stats["gaps"], 0)
        self.assertEqual(rd.read_header()["state"], MP.STATE_RUNNING if hasattr(MP, "STATE_RUNNING") else 1)
        for i, r in enumerate(recs):
            self.assertFalse(r["flags"] & MP.F_GOLDEN_OK)
            m = GP.result_message(to_json(r, "hardware_replay"), "hardware_replay", i + 1, 1000 + i)
            self.assertEqual(GP.validate_result(m), [])


if __name__ == "__main__":
    unittest.main()
