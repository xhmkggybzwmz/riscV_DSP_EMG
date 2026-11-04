"""Source thread -> asyncio handoff and per-client fan-out.

Three different losses, counted separately (never one DROP_CNT for all):
    mailbox drop      the RV32I producer found the ring full (DROP_CNT / DROP_BEFORE, hardware)
    handoff drop      the source thread found the server's handoff full (event loop stalled)
    network drop      a client's send queue was full: its oldest queued message was discarded
                      (network_drop_cnt of that client, in every result it receives)
Messages a client misses while it is disconnected are not dropped by the server: the client
sees them as a server_seq gap after it reconnects.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque


class Handoff:
    """bounded, non-blocking thread -> event loop queue (put() never waits)"""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxlen: int = 256):
        self.loop, self.maxlen = loop, maxlen
        self.q = deque()
        self.lock = threading.Lock()
        self.event = asyncio.Event()
        self.dropped = 0
        self.put_count = 0

    def put(self, item) -> None:
        with self.lock:
            if len(self.q) >= self.maxlen:
                self.q.popleft()
                self.dropped += 1
            self.q.append(item)
            self.put_count += 1
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:              # loop closed during shutdown: nothing to wake
            pass

    async def get_all(self) -> list:
        await self.event.wait()
        self.event.clear()
        with self.lock:
            items = list(self.q)
            self.q.clear()
        return items


class Client:
    """one subscriber: bounded send queue, its own sender task, send timeout"""

    def __init__(self, ws, cid: int, maxlen: int, send_timeout: float):
        self.ws, self.id, self.maxlen, self.send_timeout = ws, cid, maxlen, send_timeout
        self.q = deque()
        self.event = asyncio.Event()
        self.network_drop_cnt = 0
        self.sent = 0
        self.closed_reason = None
        self.connected_ns = time.monotonic_ns()
        try:
            self.peer = "%s:%s" % ws.remote_address[:2]
        except Exception:                 # noqa: BLE001
            self.peer = "?"

    def enqueue(self, msg: dict) -> None:
        if len(self.q) >= self.maxlen:
            self.q.popleft()                     # keep the newest: a live display wants the latest state
            self.network_drop_cnt += 1
        self.q.append(msg)
        self.event.set()

    def info(self) -> dict:
        return {"id": self.id, "peer": self.peer, "sent": self.sent, "queued": len(self.q),
                "network_drop_cnt": self.network_drop_cnt}

    async def sender(self, on_slow) -> None:
        while True:
            await self.event.wait()
            self.event.clear()
            while self.q:
                msg = self.q.popleft()
                if msg.get("type") == "result":
                    msg = {**msg, "network_drop_cnt": self.network_drop_cnt}
                elif msg.get("type") == "status":
                    msg = {**msg, "client": self.info()}
                try:
                    await asyncio.wait_for(self.ws.send(json.dumps(msg)), self.send_timeout)
                except asyncio.TimeoutError:
                    self.closed_reason = "slow (send timeout)"
                    on_slow(self)
                    await self.ws.close(code=1008, reason="slow consumer")
                    return
                self.sent += 1


class Broadcaster:
    def __init__(self, client_queue: int = 64, send_timeout: float = 2.0, history: int = 20):
        self.client_queue, self.send_timeout = client_queue, send_timeout
        self.clients: dict[int, Client] = {}
        self.history = deque(maxlen=history)    # recent results, sent in "hello" to (re)connecting clients
        self.next_id = 0
        self.stats = {"published": 0, "connects": 0, "disconnects": 0, "slow_disconnects": 0,
                      "network_drop_cnt_closed_clients": 0}

    def add(self, ws) -> Client:
        self.next_id += 1
        c = Client(ws, self.next_id, self.client_queue, self.send_timeout)
        self.clients[c.id] = c
        self.stats["connects"] += 1
        return c

    def remove(self, c: Client) -> None:
        if self.clients.pop(c.id, None) is not None:
            self.stats["disconnects"] += 1
            self.stats["network_drop_cnt_closed_clients"] += c.network_drop_cnt

    def on_slow(self, c: Client) -> None:
        self.stats["slow_disconnects"] += 1

    def publish(self, msg: dict) -> None:
        """never awaits: every client gets the message in its own bounded queue"""
        if msg.get("type") == "result":
            self.history.append(msg)
            self.stats["published"] += 1
        for c in list(self.clients.values()):
            c.enqueue(msg)

    def network_drop_total(self) -> int:
        return self.stats["network_drop_cnt_closed_clients"] + sum(c.network_drop_cnt for c in self.clients.values())
