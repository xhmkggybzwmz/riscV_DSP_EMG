"""Gesture WebSocket server: one source (one mailbox consumer), many subscribers.

    source worker thread --put()--> Handoff --dispatcher task--> Broadcaster --> client queues
                                                     |                       --> sender task / client
                                                     +--> JSON Lines log (optional)

Clients subscribe only: nothing a client sends reaches the source or the hardware. A client
connecting, disconnecting or stalling never restarts or blocks the source (PYNQ / Overlay
metadata / MMIO are set up once, in source.open(), before the port is opened).

HTTP on the same port: GET of the static web UI (--web) and /status.json (read-only);
WebSocket on /ws. Shutdown order: the source stops reading and its worker (the only
consumer) exits and releases the MMIO mapping; then the dispatcher stops, clients get a
final status and are closed, the listening socket is closed.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from http import HTTPStatus
from pathlib import Path

from websockets.legacy.server import serve as ws_serve       # websockets 10.3 (KV260 venv) .. 13.x

from . import protocol as GP
from .broadcaster import Broadcaster, Handoff

VERSION = "e4b-a"
LOCAL_ONLY = ("127.0.0.1", "localhost", "::1")


def check_host(host: str, lan: bool) -> None:
    """localhost by default; a LAN address only when given explicitly; never all interfaces"""
    if host in ("", "0.0.0.0", "::"):
        raise ValueError("refusing to listen on all interfaces: give the board's LAN address explicitly")
    if host not in LOCAL_ONLY and not lan:
        raise ValueError(f"{host} is not localhost: pass --lan to listen on a LAN address")


class GestureServer:
    def __init__(self, source, host: str = "127.0.0.1", port: int = 8765, web_root: str | None = None,
                 client_queue: int = 64, send_timeout: float = 2.0, history: int = 20, handoff: int = 256,
                 heartbeat_s: float = 1.0, jsonl: str | None = None, validate: bool = True):
        self.source, self.host, self.port = source, host, port
        self.web_root = Path(web_root).resolve() if web_root else None
        self.bc = Broadcaster(client_queue, send_timeout, history)
        self.handoff_len, self.heartbeat_s = handoff, heartbeat_s
        self.jsonl_path, self.validate = jsonl, validate
        self.server_seq = 0
        self.invalid = 0
        self.last_result_ns = None
        self.started_ns = time.monotonic_ns()
        self.handoff = None
        self.ws_server = None
        self.ready = None
        self.shutdown_report = {}

    # ------------------------------------------------------------ source -> clients
    def _sink(self, rec: dict, meta: dict) -> None:          # source thread
        self.handoff.put((rec, meta))

    async def _dispatch(self) -> None:
        log = open(self.jsonl_path, "a", encoding="utf-8") if self.jsonl_path else None
        try:
            while True:
                for rec, meta in await self.handoff.get_all():
                    if meta.get("level"):        # activity of the live source: not a result, no history, no log
                        self.bc.publish(rec)
                        continue
                    self.server_seq += 1
                    m = GP.result_message(rec, self.source.label, self.server_seq, meta["ps_ns"],
                                          meta.get("mailbox_drop_cnt") or 0, meta.get("valid", True),
                                          meta.get("fixture_origin"))
                    if self.validate and GP.validate_result(m):
                        self.invalid += 1
                        m["valid"] = False
                    self.last_result_ns = meta["ps_ns"]
                    if log:
                        log.write(json.dumps(m) + "\n")
                        log.flush()
                    self.bc.publish(m)
        finally:
            if log:
                log.close()

    def counters(self) -> dict:
        now = time.monotonic_ns()
        return {"server_seq": self.server_seq, "results_published": self.bc.stats["published"],
                "handoff_drop_cnt": self.handoff.dropped if self.handoff else 0,
                "network_drop_cnt_total": self.bc.network_drop_total(), "invalid_messages": self.invalid,
                "clients": len(self.bc.clients), "connects": self.bc.stats["connects"],
                "disconnects": self.bc.stats["disconnects"], "slow_disconnects": self.bc.stats["slow_disconnects"],
                "last_result_age_ms": None if self.last_result_ns is None else round((now - self.last_result_ns) / 1e6, 1),
                "uptime_s": round((now - self.started_ns) / 1e9, 1), "source": self.source.counters(),
                "source_error": self.source.error}

    def status(self) -> dict:
        return GP.status_message(self.source.label, self.source.state, self.counters(), {})

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            self.bc.publish(self.status())

    # ------------------------------------------------------------ connections
    async def _handler(self, ws, path=None) -> None:
        c = self.bc.add(ws)
        hello = GP.hello_message(self.source.label, {"version": VERSION, "started_monotonic_ns": self.started_ns,
                                                     "now_monotonic_ns": time.monotonic_ns(),
                                                     "server_seq": self.server_seq, "client_id": c.id},
                                 list(self.bc.history))
        c.enqueue(hello)
        c.enqueue(self.status())
        sender = asyncio.ensure_future(c.sender(self.bc.on_slow))
        try:
            async for raw in ws:                 # read-only protocol: only "ping" is answered
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    c.enqueue({"schema_version": GP.SCHEMA_VERSION, "type": "pong", "t": msg.get("t")})
        except Exception:                        # noqa: BLE001 - connection errors end this client only
            pass
        finally:
            sender.cancel()
            self.bc.remove(c)

    async def _http(self, path: str, headers):
        if headers.get("Upgrade", "").lower() == "websocket":
            return None if path.split("?")[0] == "/ws" else (HTTPStatus.NOT_FOUND, [], b"websocket endpoint is /ws\n")
        p = path.split("?")[0]
        if p == "/status.json":
            body = json.dumps(self.status()).encode()
            return HTTPStatus.OK, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], body
        if self.web_root is None:
            return HTTPStatus.NOT_FOUND, [], b"no web UI (--web)\n"
        f = (self.web_root / (p.lstrip("/") or "index.html")).resolve()
        if self.web_root not in f.parents and f != self.web_root or not f.is_file():
            return HTTPStatus.NOT_FOUND, [], b"not found\n"
        ctype = {".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html; charset=utf-8",
                 ".css": "text/css", ".json": "application/json"}.get(f.suffix) or mimetypes.guess_type(f.name)[0] \
            or "application/octet-stream"
        return HTTPStatus.OK, [("Content-Type", ctype), ("Cache-Control", "no-store")], f.read_bytes()

    # ------------------------------------------------------------ lifetime
    async def run(self, stop: asyncio.Event) -> dict:
        """source.open() must have succeeded; serves until `stop` is set, then shuts down"""
        loop = asyncio.get_running_loop()
        self.handoff = Handoff(loop, self.handoff_len)
        self.source.start(self._sink)
        tasks = [asyncio.ensure_future(self._dispatch()), asyncio.ensure_future(self._heartbeat())]
        self.ws_server = await ws_serve(self._handler, self.host, self.port, process_request=self._http,
                                        max_size=4096, ping_interval=20, ping_timeout=20)
        self.port = self.ws_server.sockets[0].getsockname()[1]
        if self.ready is not None:
            self.ready.set()
        await stop.wait()
        # 1. stop reading; the worker (the only consumer) exits and unmaps
        t0 = time.monotonic()
        exited = await loop.run_in_executor(None, self.source.stop, 5.0)
        self.shutdown_report = {"consumer_exited": exited, "source_state": self.source.state,
                                "stop_s": round(time.monotonic() - t0, 3)}
        rep = getattr(self.source, "report", None)
        if rep is not None:
            self.shutdown_report["devmem_mappings_after_close"] = rep.get("devmem_mappings_after_close")
        # 2. dispatch what was handed over, then stop distributing
        await asyncio.sleep(0.05)
        for t in tasks:
            t.cancel()
        # 3. final status to the clients, close them and the listening socket
        final = self.status()
        for c in list(self.bc.clients.values()):
            try:
                await asyncio.wait_for(c.ws.send(json.dumps({**final, "client": c.info()})), 1.0)
                await asyncio.wait_for(c.ws.close(1001, "server shutdown"), 2.0)
            except Exception:                    # noqa: BLE001
                pass
        self.ws_server.close()
        await self.ws_server.wait_closed()
        self.shutdown_report["counters"] = self.counters()
        return self.shutdown_report
