"""Protocol v1 messages, label order, sources (synthetic via the mailbox model, JSON Lines replay
of the saved E4a-B board records, hardware source with a stand-in device). No hardware."""

import json
import time
import unittest
from pathlib import Path

from gesture_server import protocol as GP
from gesture_server.source import HardwareSource, ReplaySource, SourceError, SyntheticSource
from ps_mailbox import protocol as MP

FIX = Path(__file__).resolve().parent / "fixtures"
RT2K = FIX / "e4a_b_rt2k_board.jsonl"
LONG_A = FIX / "e4a_b_long_runA_board.jsonl"


def collect(src, timeout=10.0, n=None):
    got = []
    src.open()
    src.start(lambda r, m: got.append((r, m)))
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end and src.state == "running" and (n is None or len(got) < n):
        time.sleep(0.01)
    src.stop()
    return got


class TestProtocol(unittest.TestCase):
    def test_labels_frozen(self):
        self.assertEqual(GP.LABELS, ("REST", "HAND_CLOSE", "HAND_OPEN", "WRIST_FLEX", "WRIST_EXT", "PINCH"))
        self.assertEqual(GP.LABELS, MP.LABELS)

    def test_board_record_to_message(self):
        rec = json.loads(RT2K.read_text(encoding="utf-8").splitlines()[0])
        m = GP.result_message(rec, "test_fixture", 7, 123, fixture_origin="x")
        self.assertEqual(GP.validate_result(m), [])
        self.assertEqual((m["result_index"], m["reset_epoch"], m["gesture"], m["gesture_id"], m["argmax"]),
                         (1, 1, "HAND_CLOSE", 1, 1))
        self.assertEqual(m["scores"], [-673553, 664745, -467160, -42831, -97672, -867340])
        self.assertEqual(m["window_end_seq"], 399)
        json.dumps(m)                                     # serialisable

    def test_rejects_bad_messages(self):
        rec = json.loads(RT2K.read_text(encoding="utf-8").splitlines()[0])
        good = GP.result_message(rec, "hardware_replay", 1, 1)
        for mut, what in ((lambda m: m.update(scores=[1, 2, 3]), "scores"),
                          (lambda m: m.update(scores=[2**31] + [0] * 5), "scores"),
                          (lambda m: m.update(gesture="REST"), "gesture label"),
                          (lambda m: m.update(source="fpga"), "source"),
                          (lambda m: m.update(result_index=-1), "result_index"),
                          (lambda m: m.update(flags=m["flags"] | MP.F_DROP_BEFORE), "drop_before"),
                          (lambda m: m.pop("reset_epoch"), "missing reset_epoch"),
                          (lambda m: m.update(valid=1), "valid")):
            m = dict(good)
            mut(m)
            self.assertTrue(any(what in p for p in GP.validate_result(m)), (what, GP.validate_result(m)))
        with self.assertRaises(ValueError):
            GP.result_message(rec, "fpga_cnn", 1, 1)

    def test_special_gestures(self):
        self.assertEqual(GP.gesture_label(-1), "UNCERTAIN")
        self.assertEqual(GP.gesture_label(-2), "NO_DECISION")
        self.assertEqual(GP.gesture_label(9), "INVALID(9)")


class TestSources(unittest.TestCase):
    def test_synthetic_six_gestures_through_mailbox_reader(self):
        src = SyntheticSource(rate_hz=400, hold=3, count=36)
        got = collect(src)
        self.assertEqual(src.state, "done")
        self.assertEqual(len(got), 36)
        labels = [r["gesture"] for r, _ in got]
        self.assertEqual(labels[::3], list(MP.LABELS) * 2)
        for r, _ in got:
            self.assertEqual(r["argmax"], MP.argmax_lowest(r["scores"]))
            self.assertEqual(r["source"], "synthetic_demo")
        c = src.counters()
        self.assertEqual((c["records"], c["errors"], c["gaps"], c["dup"]), (36, 0, 0, 0))
        self.assertEqual(c["consumer_index"], src.prod.prod)        # every published record acknowledged

    def test_replay_board_records(self):
        src = ReplaySource([RT2K], rate_hz=500)
        got = collect(src)
        recs = [json.loads(l) for l in RT2K.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(got), 44)
        for (r, meta), orig in zip(got, recs):
            self.assertEqual((r["result_index"], r["epoch"], r["scores"], r["argmax"]),
                             (orig["result_index"], orig["epoch"], orig["scores"], orig["argmax"]))
            self.assertIn("e4a_b_rt2k_board.jsonl", meta["fixture_origin"])
            m = GP.result_message(r, "test_fixture", 1, meta["ps_ns"], fixture_origin=meta["fixture_origin"])
            self.assertEqual(GP.validate_result(m), [])
        self.assertEqual(sorted({r["epoch"] for r, _ in got}), [1, 2, 3, 4])

    def test_replay_original_spacing(self):
        src = ReplaySource([LONG_A], speed=4.0)
        t0 = time.monotonic()
        got = collect(src, timeout=30)
        dt = time.monotonic() - t0
        self.assertEqual(len(got), 104)
        # run A spans ~5.4 s on the board after the 16 kept records: about 1.4 s at 4x
        self.assertGreater(dt, 1.0)

    def test_replay_missing_file(self):
        with self.assertRaises(SourceError):
            ReplaySource([FIX / "nope.jsonl"]).open()


class TestHardwareSource(unittest.TestCase):
    """the real PynqMailboxDevice checks with the stand-ins of ps_mailbox/tests/test_overlay_device.py"""

    def setUp(self):
        from ps_mailbox.tests.test_overlay_device import TestOverlayDevice
        self.t = TestOverlayDevice("test_verify_open_read_close")
        self.t.setUp()

    def tearDown(self):
        self.t.tearDown()

    def source(self, **kw):
        from ps_mailbox.tests.test_overlay_device import FakeOverlay
        dk = {"sysfs_root": str(self.t.sys), "overlay_factory": FakeOverlay, "mmio_factory": self.t.mmio}
        dk.update(kw)
        return HardwareSource(str(self.t.bit), loaded_bin=str(self.t.bin), lock_file=None, device_kwargs=dk,
                              policy="backlog")

    def test_reads_records_once_verified_and_releases(self):
        src = self.source()
        got = collect(src, timeout=2, n=3)
        self.assertEqual([r["result_index"] for r, _ in got], [1, 2, 3])
        self.assertTrue(all(r["source"] == "hardware_replay" for r, _ in got))
        self.assertEqual(src.opens, 1)
        self.assertIsNone(src.dev)                       # released when the worker ended
        self.assertEqual(src.state, "stopped")

    def test_unavailable_hardware_opens_nothing(self):
        (self.t.sys / "sys/kernel/config/device-tree/overlays/rv32_6a/status").write_text("unapplied\n")
        src = self.source()
        with self.assertRaises(SourceError) as cm:
            src.open()
        self.assertIn("hardware unavailable", str(cm.exception))
        self.assertEqual(self.t.opened, [])              # no PL window was mapped
        self.assertIsNone(src.dev)


if __name__ == "__main__":
    unittest.main()
