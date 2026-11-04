"""Gesture server: one consumer, many WebSocket clients, bounded queues, losses kept apart,
reconnect without re-initialising the source, shutdown order. Software only (synthetic /
replay sources, the hardware source with stand-in device and mailbox model)."""

import asyncio
import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from websockets.legacy.client import connect

from gesture_server import protocol as GP
from gesture_server.broadcaster import Broadcaster, Client, Handoff
from gesture_server.server import GestureServer, check_host
from gesture_server.source import HardwareSource, ReplaySource, SyntheticSource
from ps_mailbox import protocol as MP

FIX = Path(__file__).resolve().parent / "fixtures"
WEB = Path(__file__).resolve().parents[3] / "visualization" / "web"
if not WEB.is_dir():                                   # board copy: web/ next to the packages
    WEB = Path(__file__).resolve().parents[2] / "web"


async def start(src, **kw):
    srv = GestureServer(src, port=0, **kw)
    srv.ready = asyncio.Event()
    stop = asyncio.Event()
    task = asyncio.ensure_future(srv.run(stop))
    await srv.ready.wait()
    return srv, stop, task


async def recv_until(ws, cond, timeout=5.0):
    out = []
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), max(0.01, t_end - time.monotonic())))
        except asyncio.TimeoutError:
            break
        out.append(m)
        if cond(out):
            break
    return out


def results(msgs):
    return [m for m in msgs if m["type"] == "result"]


class FakeWS:
    """WebSocket stand-in whose send() can be held"""
    remote_address = ("test", 0)

    def __init__(self, block=False):
        self.sent = []
        self.gate = asyncio.Event()
        if not block:
            self.gate.set()
        self.closed = None

    async def send(self, data):
        await self.gate.wait()
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def run(coro):
    return asyncio.run(coro)


class TestServer(unittest.TestCase):
    def test_no_client_reader_keeps_consuming(self):
        async def t():
            src = SyntheticSource(rate_hz=400, hold=4, count=40)
            src.open()
            srv, stop, task = await start(src)
            for _ in range(300):
                if src.state == "done":
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            stop.set()
            rep = await task
            self.assertEqual(src.state, "done")
            self.assertEqual(srv.server_seq, 40)
            self.assertEqual(rep["counters"]["handoff_drop_cnt"], 0)
            self.assertEqual(src.reader.cons, src.prod.prod)     # every record acknowledged, no client needed
            self.assertTrue(rep["consumer_exited"])
        run(t())

    def test_two_clients_get_identical_results(self):
        async def t():
            src = SyntheticSource(rate_hz=100, hold=5)
            src.open()
            srv, stop, task = await start(src)
            url = f"ws://127.0.0.1:{srv.port}/ws"
            async with connect(url) as a, connect(url) as b:
                ra, rb = await asyncio.gather(recv_until(a, lambda o: len(results(o)) >= 40),
                                              recv_until(b, lambda o: len(results(o)) >= 40))
            stop.set()
            await task
            self.assertEqual(ra[0]["type"], "hello")
            self.assertEqual(ra[0]["labels"], list(GP.LABELS))
            self.assertEqual(ra[0]["source"], "synthetic_demo")
            da = {m["server_seq"]: m for m in results(ra)}
            db = {m["server_seq"]: m for m in results(rb)}
            common = sorted(set(da) & set(db))
            self.assertGreater(len(common), 30)
            for s in common:
                self.assertEqual(da[s], db[s])
                self.assertEqual(GP.validate_result(da[s]), [])
            seqs = [m["server_seq"] for m in results(ra)]
            self.assertEqual(seqs, sorted(seqs))
        run(t())

    def test_disconnect_reconnect_does_not_restart_source(self):
        async def t():
            from ps_mailbox.fake_mmio import FakeProducer, make_result
            from ps_mailbox.tests.test_overlay_device import FakeOverlay, TestOverlayDevice
            fx = TestOverlayDevice("test_verify_open_read_close")
            fx.setUp()
            try:
                dk = {"sysfs_root": str(fx.sys), "overlay_factory": FakeOverlay, "mmio_factory": fx.mmio}
                src = HardwareSource(str(fx.bit), loaded_bin=str(fx.bin), lock_file=None, device_kwargs=dk,
                                     policy="backlog")
                src.open()
                dev = src.dev
                prod = FakeProducer(fx.box)
                prod.init(0)
                halt = threading.Event()

                def produce():
                    n = 0
                    while not halt.is_set() and n < 400:
                        n += 1
                        prod.publish(make_result(n))
                        time.sleep(0.005)
                th = threading.Thread(target=produce, daemon=True)
                srv, stop, task = await start(src)
                th.start()
                url = f"ws://127.0.0.1:{srv.port}/ws"
                async with connect(url) as a:
                    first = await recv_until(a, lambda o: len(results(o)) >= 10)
                seq_closed = srv.server_seq
                await asyncio.sleep(0.3)                     # disconnected: the reader goes on
                self.assertGreater(srv.server_seq, seq_closed + 10)
                async with connect(url) as b:
                    second = await recv_until(b, lambda o: len(results(o)) >= 5)
                halt.set()
                th.join()
                await asyncio.sleep(0.2)
                stop.set()
                rep = await task
                self.assertEqual(src.opens, 1)               # PYNQ / Overlay metadata / MMIO set up once
                self.assertIs(dev.ol, None)                  # closed at shutdown, not at a disconnect
                hello2 = second[0]
                self.assertEqual(hello2["type"], "hello")
                self.assertEqual(len(hello2["history"]), 20)   # recent results for the reconnecting client
                gap = results(second)[0]["server_seq"] - results(first)[-1]["server_seq"] - 1
                self.assertGreater(gap, 10)                  # missed while disconnected = a server_seq gap
                self.assertEqual(results(second)[0]["network_drop_cnt"], 0)   # not a network drop
                self.assertEqual(src.reader.cons, prod.prod)
                self.assertEqual(srv.server_seq, src.reader.stats["records"])  # every consumed record dispatched
                self.assertTrue(rep["consumer_exited"])
                self.assertTrue(fx.box.ps.closed)            # the mailbox window was closed
            finally:
                fx.tearDown()
        run(t())

    def test_replay_board_records_end_to_end(self):
        async def t():
            src = ReplaySource([FIX / "e4a_b_rt2k_board.jsonl"], rate_hz=200)
            src.open()
            srv, stop, task = await start(src)
            async with connect(f"ws://127.0.0.1:{srv.port}/ws") as a:
                got = await recv_until(a, lambda o: len(results(o)) >= 44 - 1)
            stop.set()
            await task
            orig = [json.loads(l) for l in (FIX / "e4a_b_rt2k_board.jsonl").read_text(encoding="utf-8").splitlines()]
            rs = {m["server_seq"]: m for m in results(got) + got[0]["history"]}
            self.assertGreaterEqual(len(rs), 43)
            for s, m in rs.items():
                o = orig[s - 1]
                self.assertEqual(m["source"], "test_fixture")
                self.assertIn("e4a_b_rt2k_board.jsonl", m["fixture_origin"])
                self.assertEqual((m["result_index"], m["reset_epoch"], m["scores"], m["gesture"], m["window_end_seq"]),
                                 (o["result_index"], o["epoch"], o["scores"], "HAND_CLOSE", o["window_end_seq"]))
                self.assertTrue(m["valid"])
        run(t())

    def test_http_static_status_and_traversal(self):
        async def t():
            src = SyntheticSource(rate_hz=50, hold=5)
            src.open()
            srv, stop, task = await start(src, web_root=str(WEB))
            base = f"http://127.0.0.1:{srv.port}"
            loop = asyncio.get_running_loop()
            get = lambda p: urllib.request.urlopen(base + p, timeout=5)        # noqa: E731
            r = await loop.run_in_executor(None, get, "/")
            self.assertIn(b"<canvas", r.read())
            st = json.loads((await loop.run_in_executor(None, get, "/status.json")).read())
            self.assertEqual((st["type"], st["source"]), ("status", "synthetic_demo"))
            for bad in ("/../host/gesture_server/server.py", "/%2e%2e/README.md", "/nope.js"):
                with self.assertRaises(Exception):
                    await loop.run_in_executor(None, get, bad)
            stop.set()
            await task
        run(t())

    def test_jsonl_log(self):
        async def t():
            import tempfile
            d = tempfile.mkdtemp()
            log = Path(d) / "out.jsonl"
            src = SyntheticSource(rate_hz=400, hold=2, count=12)
            src.open()
            srv, stop, task = await start(src, jsonl=str(log))
            for _ in range(300):
                if src.state == "done":
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            stop.set()
            await task
            ms = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([m["server_seq"] for m in ms], list(range(1, 13)))
            self.assertTrue(all(GP.validate_result(m) == [] for m in ms))
        run(t())

    def test_check_host(self):
        check_host("127.0.0.1", False)
        check_host("192.168.137.25", True)
        for h, lan in (("0.0.0.0", True), ("::", True), ("", True), ("192.168.137.25", False)):
            with self.assertRaises(ValueError):
                check_host(h, lan)


class TestQueues(unittest.TestCase):
    def test_network_drop_counted_per_client(self):
        async def t():
            ws = FakeWS(block=True)
            c = Client(ws, 1, maxlen=4, send_timeout=10)
            task = asyncio.ensure_future(c.sender(lambda _: None))
            c.enqueue({"type": "result", "server_seq": 1, "network_drop_cnt": 0})
            await asyncio.sleep(0.01)                  # the sender takes message 1 and waits in send()
            for i in range(2, 11):
                c.enqueue({"type": "result", "server_seq": i, "network_drop_cnt": 0})
            await asyncio.sleep(0.01)
            # the sender took message 1 and waits in send(); 9 more, room for 4 -> 5 dropped
            self.assertEqual(c.network_drop_cnt, 5)
            ws.gate.set()
            await asyncio.sleep(0.05)
            task.cancel()
            self.assertEqual([m["server_seq"] for m in ws.sent], [1, 7, 8, 9, 10])
            self.assertEqual(ws.sent[-1]["network_drop_cnt"], 5)
        run(t())

    def test_slow_client_disconnected_others_unaffected(self):
        async def t():
            bc = Broadcaster(client_queue=8, send_timeout=0.1)
            fast, slow = FakeWS(), FakeWS(block=True)
            cf, cs = bc.add(fast), bc.add(slow)
            tf = asyncio.ensure_future(cf.sender(bc.on_slow))
            ts = asyncio.ensure_future(cs.sender(bc.on_slow))
            t0 = time.monotonic()
            for i in range(1, 201):
                bc.publish({"type": "result", "server_seq": i})     # never awaits
                if i % 4 == 0:                         # bursts smaller than the queue: the fast client keeps up
                    await asyncio.sleep(0.005)
            publish_s = time.monotonic() - t0
            await asyncio.sleep(0.2)
            self.assertEqual([m["server_seq"] for m in fast.sent], list(range(1, 201)))
            self.assertEqual(slow.closed[0], 1008)
            self.assertEqual(bc.stats["slow_disconnects"], 1)
            self.assertLess(publish_s, 1.0)
            tf.cancel()
            ts.cancel()
        run(t())

    def test_handoff_never_blocks_and_counts(self):
        async def t():
            h = Handoff(asyncio.get_running_loop(), maxlen=16)
            th = threading.Thread(target=lambda: [h.put(i) for i in range(1000)])
            t0 = time.monotonic()
            th.start()
            th.join()                                  # the loop is not draining: put() must not wait
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(h.dropped, 1000 - 16)
            self.assertEqual(await h.get_all(), list(range(984, 1000)))
        run(t())

    def test_memory_bounded(self):
        async def t():
            bc = Broadcaster(client_queue=32, history=20)
            ws = FakeWS(block=True)
            c = bc.add(ws)
            for i in range(50000):
                bc.publish({"type": "result", "server_seq": i})
            self.assertEqual(len(c.q), 32)
            self.assertEqual(len(bc.history), 20)
            self.assertEqual(c.network_drop_cnt, 50000 - 32)
        run(t())


if __name__ == "__main__":
    unittest.main()
