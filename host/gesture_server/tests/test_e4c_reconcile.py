"""scripts/fpga/kv260_e4c_live.py on a SIMULATED evidence set (mailbox model, not hardware): power-on
run without consumer (16 kept / 56 dropped), gesture server started, two recorder clients, controlled
run of 72 records with 50 ms detection steps; then the same set with one altered score must fail."""

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from websockets.legacy.client import connect

from gesture_server.server import GestureServer
from gesture_server.source import MailboxSource
from gesture_server.tests.test_mg6_compat import program_records
from ps_mailbox.fake_mmio import FakeMailbox, FakeProducer
from ps_mailbox.pynq_reader import MailboxReader

REPO = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parent / "fixtures" / "e4c_mg6_expected.json"
SCRIPT = REPO / "scripts" / "fpga" / "kv260_e4c_live.py"


def publish_program(prod: FakeProducer, recs, pace: float = 0.0) -> None:
    """one producer epoch; detection times like the 2 kHz hardware (decisions 5 M cycles apart)"""
    prod.init(0)
    t = 0
    for r in recs:
        d = (r["index"] - 1) % 3
        t += 20_000_000 if d == 0 else 5_000_000
        prod.cycle = t - 1000                 # FakeProducer adds 1000 before stamping detect_cycle
        prod.publish(r, flags=r["flags"])
        if pace:
            time.sleep(pace)
    prod.done()


class FakeHw(MailboxSource):
    label = "hardware_replay"

    def __init__(self, box):
        super().__init__(policy="backlog", poll_ms=2)
        self.box = box

    def open(self):
        self.reader = MailboxReader(self.box.ps, policy=self.policy)


def make_evidence(d: Path) -> None:
    fx = json.loads(FIX.read_text(encoding="utf-8"))
    recs = program_records(fx, 4)
    box = FakeMailbox()
    prod = FakeProducer(box)
    publish_program(prod, recs)                                   # power-on run, nobody reading
    hdr = MailboxReader(box.ps).read_header()
    regs = {"FIRST_TOHOST": 1, "TOHOST_WRITES": 1, "STATUS": 0x3D, "TRAP_CAUSE": 0, "RUN_CYCLES": 123, "RESET_RELEASES": 1}
    (d / "probe.json").write_text(json.dumps({"simulated": True, "header": hdr,
                                              "regs": {k: f"0x{v:08X}" for k, v in regs.items()}}))

    async def run():
        src = FakeHw(box)
        src.open()
        srv = GestureServer(src, port=0, jsonl=str(d / "server.jsonl"))
        srv.ready, stop = asyncio.Event(), asyncio.Event()
        task = asyncio.ensure_future(srv.run(stop))
        await srv.ready.wait()

        async def client(name):
            with open(d / f"{name}.jsonl", "w", encoding="utf-8") as out:
                async with connect(f"ws://127.0.0.1:{srv.port}/ws") as ws:
                    t_end = time.monotonic() + 15
                    n = 0
                    while time.monotonic() < t_end and n < 72:
                        try:
                            m = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
                        except asyncio.TimeoutError:
                            continue
                        m["_rx_ns"], m["_conn"] = time.monotonic_ns(), 1
                        out.write(json.dumps(m) + "\n")
                        n += m["type"] == "result" and m["reset_epoch"] == 2
        clients = [asyncio.ensure_future(client(n)) for n in ("clientA", "clientB")]
        await asyncio.sleep(0.5)
        th = threading.Thread(target=publish_program, args=(prod, recs, 0.004))   # the controlled run
        th.start()
        await asyncio.gather(*clients)
        th.join()
        await asyncio.sleep(0.2)
        (d / "status_after_run.json").write_text(json.dumps(srv.status()))
        stop.set()
        await task
    asyncio.run(run())
    regs.update({"RESET_RELEASES": 2, "RUN_CYCLES": 456})
    (d / "reset1.json").write_text(json.dumps({"simulated": True, "after": {k: f"0x{v:08X}" for k, v in regs.items()}}))


class TestE4cReconcile(unittest.TestCase):
    def test_simulated_run_passes_and_a_changed_score_fails(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            make_evidence(d)
            r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(d), "--decisions", str(FIX)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
            self.assertIn("E4c-B SUMMARY", r.stdout)
            lines = (d / "server.jsonl").read_text(encoding="utf-8").splitlines()
            k = next(i for i, l in enumerate(lines) if json.loads(l)["reset_epoch"] == 2) + 40
            m = json.loads(lines[k])
            m["scores"][2] += 1
            lines[k] = json.dumps(m)
            (d / "server.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(d), "--decisions", str(FIX)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
