"""Layout invariants, encoding, generated files (Python model only, no hardware)."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from ps_mailbox import protocol as P
from ps_mailbox.pynq_reader import load_config

REPO = Path(__file__).resolve().parents[3]


class TestLayout(unittest.TestCase):
    def test_header_words(self):
        offs = [o for o, _, _ in P.HEADER.values()]
        self.assertEqual(len(offs), len(set(offs)))
        for name, (off, who, _) in P.HEADER.items():
            self.assertEqual(off % 4, 0, name)
            self.assertIn(who, ("cpu", "ps"))
            self.assertTrue(off < 0x40 if who == "cpu" else 0x40 <= off < 0x80, name)

    def test_ring_fits(self):
        self.assertGreaterEqual(P.RING_OFFSET, 0x80)
        self.assertLessEqual(P.RING_OFFSET + P.RING_SIZE * P.RECORD_SIZE, P.WINDOW_SIZE)
        self.assertEqual((1 << 32) % P.RING_SIZE, 0, "slot mapping must survive the 32-bit wrap")
        self.assertEqual(sorted(w for w, _ in P.RECORD.values()), list(range(P.RECORD_WORDS)))

    def test_wrap_arithmetic(self):
        self.assertEqual(P.used(3, 0xFFFFFFFE), 5)
        self.assertEqual(P.slot_offset(0xFFFFFFFF), P.RING_OFFSET + 15 * P.RECORD_SIZE)
        self.assertEqual(P.slot_offset(0x100000000 & P.MASK32), P.RING_OFFSET)

    def test_labels_frozen(self):
        spec = importlib.util.spec_from_file_location("labels", REPO / "ml" / "emgml" / "labels.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(list(P.LABELS), [mod.UNIFIED[i] for i in range(6)])

    def test_encode_decode(self):
        rec = {"flags": P.F_POSTPROC | P.F_DROP_BEFORE, "publish_index": 0xFFFFFFFF, "epoch": 7,
               "result_index": 12, "window_end_seq": 0x00A0_0563, "esi_timestamp": 1379, "gesture": -1,
               "argmax": 2, "segment": 0x1234, "scores": [-673553, 664745, -2**31, 2**31 - 1, 0, -1],
               "cnn_lat_cycles": 11152, "detect_cycle": 0xFFFFFF00, "publish_cycle": 0x00000100}
        d = P.decode_record(P.encode_record(rec))
        for k, v in rec.items():
            self.assertEqual(d[k], v, k)
        self.assertEqual(d["rec_magic"], P.REC_MAGIC)
        self.assertEqual(d["rec_version"], P.REC_VERSION)

    def test_record_problems(self):
        scores = [1, 9, 9, 3, 4, 5]
        rec = P.decode_record(P.encode_record({
            "flags": P.F_ARGMAX_OK, "publish_index": 5, "epoch": 2, "result_index": 1, "window_end_seq": 0,
            "esi_timestamp": 0, "gesture": 1, "argmax": 1, "segment": 0, "scores": scores,
            "cnn_lat_cycles": 1, "detect_cycle": 0, "publish_cycle": 0}))
        self.assertEqual(P.record_problems(rec, 5, 2), [])      # tie 9/9 -> lowest index 1
        self.assertTrue(P.record_problems(rec, 6, 2))
        self.assertTrue(P.record_problems(rec, 5, 3))
        rec["argmax"] = 2
        self.assertTrue(P.record_problems(rec, 5, 2))


class TestGenerated(unittest.TestCase):
    def test_generated_files_up_to_date(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "gen" / "gen_mailbox.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_config(self):
        cfg = load_config()
        self.assertEqual(cfg["header"], {n: o for n, (o, _, _) in P.HEADER.items()})
        self.assertEqual(cfg["labels"], list(P.LABELS))
        text = (REPO / "rtl" / "soc" / "soc_addr_map.vh").read_text(encoding="utf-8")
        self.assertIn("`define KV260_MBX_PS_BASE 32'h" + cfg["ps_base"][2:6] + "_" + cfg["ps_base"][6:], text)
        self.assertNotEqual(int(cfg["ps_base"], 16) >> 18, int(cfg["board"]["dbg_base"], 16) >> 18,
                            "mailbox must not overlap the 256 KB debug window")
        json.dumps(cfg)


if __name__ == "__main__":
    unittest.main()
