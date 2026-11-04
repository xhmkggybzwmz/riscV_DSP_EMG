"""E4c-A offline compatibility: the six-class records of the multi-gesture firmware (mg6_common.h:
order, flags, timestamps, segments; values = the E2 decisions of the selected real segments,
fixtures/e4c_mg6_expected.json from scripts/sim/gen_e4c.py) through the mailbox model,
MailboxReader, the gesture server and a WebSocket client. No hardware, not an FPGA result."""

import asyncio
import json
import time
import unittest
from pathlib import Path

from websockets.legacy.client import connect

from gesture_server import protocol as GP
from gesture_server.server import GestureServer
from gesture_server.source import SyntheticSource
from ps_mailbox import golden_mg6
from ps_mailbox import protocol as MP

FIX = Path(__file__).resolve().parent / "fixtures" / "e4c_mg6_expected.json"
PASSES = 4                                  # mbx_mg6_rt / mbx_mg6_p4: 72 results


def program_records(fx: dict, passes: int = PASSES) -> list[dict]:
    """records in the order / with the fields mbx_mg6 publishes them"""
    L, out, n = fx["segment_length"], [], 0
    nseg = len(fx["segments"])
    for s, seg in enumerate(fx["segments"]):
        for p in range(passes):
            run = s * passes + p
            for d, dec in enumerate(seg["decisions"]):
                n += 1
                flags = MP.F_POSTPROC | MP.F_ARGMAX_OK | MP.F_GOLDEN_OK
                if d == 0:
                    flags |= MP.F_NEW_SEGMENT
                if run == nseg * passes - 1 and d == len(seg["decisions"]) - 1:
                    flags |= MP.F_LAST
                out.append({"index": n, "seq": fx["seq0"] + run * L + dec["end"], "ts": dec["end"], "seg": run & 0xFF,
                            "argmax": dec["argmax"], "lat": 11152, "scores": dec["scores"], "flags": flags,
                            "class": seg["class"]})
    return out


class Mg6Source(SyntheticSource):
    """the firmware's record sequence through the mailbox model (FakeProducer) and MailboxReader"""
    label = "test_fixture"

    def __init__(self, recs):
        super().__init__(rate_hz=200, count=len(recs))
        self.recs = recs

    def _produce(self):
        self.prod.init(0)
        for r in self.recs:
            if self._stop.wait(0.005):
                break
            self.prod.publish(r, flags=r["flags"])
        self.prod.done()
        self._produced.set()


class TestMg6Compat(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads(FIX.read_text(encoding="utf-8"))

    def test_fixture_is_six_classes_in_order(self):
        segs = self.fx["segments"]
        self.assertEqual([s["class"] for s in segs], list(range(6)))
        self.assertEqual([s["label"] for s in segs], list(GP.LABELS))
        for s in segs:
            for d in s["decisions"]:
                self.assertEqual(d["argmax"], MP.argmax_lowest(d["scores"]))
                self.assertEqual(d["argmax"], s["class"])

    def test_reader_server_websocket_keep_classes_and_values(self):
        recs = program_records(self.fx)

        async def t():
            src = Mg6Source(recs)
            src.open()
            srv = GestureServer(src, port=0)
            srv.ready, stop = asyncio.Event(), asyncio.Event()
            task = asyncio.ensure_future(srv.run(stop))
            await srv.ready.wait()
            got = []
            async with connect(f"ws://127.0.0.1:{srv.port}/ws") as ws:
                t_end = time.monotonic() + 10
                while time.monotonic() < t_end and len([m for m in got if m["type"] == "result"]) < len(recs):
                    try:
                        got.append(json.loads(await asyncio.wait_for(ws.recv(), 1.0)))
                    except asyncio.TimeoutError:
                        pass
            stop.set()
            await task
            hist = got[0]["history"]
            res = {m["server_seq"]: m for m in hist + [m for m in got if m["type"] == "result"]}
            return [res[k] for k in sorted(res)], src

        msgs, src = asyncio.run(t())
        self.assertEqual(len(msgs), len(recs))
        self.assertEqual(len(msgs), 72)
        self.assertEqual(src.reader.stats["errors"] + src.reader.stats["gaps"] + src.reader.stats["dup"], 0)
        # every message against the per-record golden table (golden_mg6): scores, argmax, gesture, ts,
        # seq, flags, segment; one producer epoch for the whole program
        tab = golden_mg6.table(golden_mg6.load(FIX), PASSES)
        self.assertEqual(len(tab), 72)
        for m in msgs:
            self.assertEqual(golden_mg6.check(m, tab[m["result_index"] - 1]), [], m["result_index"])
        self.assertEqual({m["reset_epoch"] for m in msgs}, {1})
        self.assertEqual([m["segment"] for m in msgs], [r["segment"] for r in tab])
        self.assertEqual([sum(m["gesture_id"] == c for m in msgs) for c in range(6)], [12] * 6)
        for m, r in zip(msgs, recs):
            self.assertEqual(GP.validate_result(m), [])
            self.assertEqual((m["result_index"], m["gesture_id"], m["gesture"], m["argmax"], m["scores"]),
                             (r["index"], r["class"], GP.LABELS[r["class"]], r["class"], r["scores"]))
            self.assertEqual((m["window_end_seq"], m["esi_timestamp"]), (r["seq"], r["ts"]))
        # the class sequence the browser receives: each class for PASSES runs, in E2/E3 order
        seq = [m["gesture"] for m in msgs]
        blocks = [seq[i:i + 3 * PASSES] for i in range(0, len(seq), 3 * PASSES)]
        self.assertEqual([set(b) for b in blocks], [{c} for c in GP.LABELS])


if __name__ == "__main__":
    unittest.main()
