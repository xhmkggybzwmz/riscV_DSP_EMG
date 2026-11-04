"""E4c per-record golden table (ps_mailbox.golden_mg6) from the tracked decisions fixture."""

import unittest
from pathlib import Path

from ps_mailbox import golden_mg6 as GM
from ps_mailbox import protocol as P

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "host" / "gesture_server" / "tests" / "fixtures" / "e4c_mg6_expected.json"
RD_EXPECT = REPO / "build" / "e4c" / "vec" / "chain_mg6_p4" / "rd_expect.hex"


class TestGoldenMg6(unittest.TestCase):
    def setUp(self):
        self.tab = GM.table(GM.load(FIX), 4)

    def test_layout(self):
        t = self.tab
        self.assertEqual(len(t), 72)
        self.assertEqual([r["result_index"] for r in t], list(range(1, 73)))
        self.assertEqual([r["class"] for r in t], [c for c in range(6) for _ in range(12)])
        self.assertEqual([r["segment"] for r in t], [r for r in range(24) for _ in range(3)])
        self.assertEqual([r["esi_timestamp"] for r in t[:3]], [399, 499, 599])
        self.assertEqual(t[3]["window_end_seq"], 600 + 399)                 # run 1 starts at seq 600
        self.assertEqual(t[-1]["window_end_seq"], 23 * 600 + 599)
        base = P.F_POSTPROC | P.F_ARGMAX_OK | P.F_GOLDEN_OK
        self.assertEqual([r["flags"] for r in t[:4]], [base | P.F_NEW_SEGMENT, base, base, base | P.F_NEW_SEGMENT])
        self.assertEqual(t[-1]["flags"], base | P.F_LAST)
        for r in t:
            self.assertEqual(r["argmax"], P.argmax_lowest(r["scores"]))
            self.assertEqual(r["gesture"], r["class"])
        # no window sequence number is used twice
        self.assertEqual(len({r["window_end_seq"] for r in t}), 72)

    def test_check_finds_differences(self):
        e = self.tab[10]
        rec = {"result_index": e["result_index"], "argmax": e["argmax"], "scores": list(e["scores"]),
               "esi_timestamp": e["esi_timestamp"], "window_end_seq": e["window_end_seq"], "flags": e["flags"],
               "gesture_id": e["gesture"], "segment": e["segment"]}
        self.assertEqual(GM.check(rec, e), [])
        other = (e["argmax"] + 1) % 6
        for k, v in (("scores", [0] * 6), ("argmax", other), ("window_end_seq", 1), ("esi_timestamp", 0),
                     ("segment", 99), ("flags", 0), ("gesture_id", other)):
            bad = dict(rec)
            bad[k] = v
            self.assertTrue(GM.check(bad, e), k)

    @unittest.skipUnless(RD_EXPECT.is_file(), "build/e4c/vec not generated")
    def test_matches_testbench_expectation(self):
        words = [int(w, 16) for w in RD_EXPECT.read_text().split()]
        self.assertEqual(len(words), 72)
        for r, w in zip(self.tab, words):
            self.assertEqual((r["esi_timestamp"], r["window_end_seq"]), (w >> 32, w & 0xFFFFFFFF))


if __name__ == "__main__":
    unittest.main()
