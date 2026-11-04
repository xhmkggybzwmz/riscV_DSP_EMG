"""Record what a WebSocket subscriber receives (for reconciliation with the PS-side log).

    python -m gesture_server.record --url ws://192.168.137.25:8765/ws --out client_a.jsonl --duration 60
    python -m gesture_server.record ... --drop-after 30 --drop-for 1.0   (disconnect once, then reconnect)

Every received message is written as one JSON line with "_rx_ns" (this host's monotonic clock)
and "_conn" (connection number). Read-only: sends nothing but WebSocket control frames.
Stops after --duration s, on Ctrl-C / SIGTERM, or after --until-results result messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time

from websockets.legacy.client import connect


async def record(ns) -> dict:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, ValueError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    t0 = time.monotonic()
    stats = {"messages": 0, "results": 0, "connections": 0, "errors": []}
    dropped_once = False
    with open(ns.out, "w", encoding="utf-8") as out:
        while not stop.is_set() and time.monotonic() - t0 < ns.duration:
            try:
                async with connect(ns.url, max_size=2**20) as ws:
                    stats["connections"] += 1
                    conn = stats["connections"]
                    while not stop.is_set() and time.monotonic() - t0 < ns.duration:
                        if ns.drop_after and not dropped_once and stats["results"] >= ns.drop_after:
                            dropped_once = True
                            stats["dropped_at_ns"] = time.monotonic_ns()
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 0.2)
                        except asyncio.TimeoutError:
                            continue
                        m = json.loads(raw)
                        m["_rx_ns"] = time.monotonic_ns()
                        m["_conn"] = conn
                        out.write(json.dumps(m) + "\n")
                        out.flush()
                        stats["messages"] += 1
                        if m.get("type") == "result":
                            stats["results"] += 1
                            if ns.until_results and stats["results"] >= ns.until_results:
                                stop.set()
                if dropped_once and conn == 1:
                    await asyncio.sleep(ns.drop_for)          # stay disconnected, then reconnect
                    stats["reconnect_at_ns"] = time.monotonic_ns()
            except (OSError, Exception) as e:                  # noqa: BLE001 - recorded, retried
                stats["errors"].append(f"{type(e).__name__}: {e}")
                await asyncio.sleep(0.5)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--until-results", type=int, default=0)
    ap.add_argument("--drop-after", type=int, default=0, help="disconnect once after this many result messages")
    ap.add_argument("--drop-for", type=float, default=1.0, help="seconds to stay disconnected")
    ns = ap.parse_args(argv)
    st = asyncio.run(record(ns))
    print("RECORD " + json.dumps(st), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
