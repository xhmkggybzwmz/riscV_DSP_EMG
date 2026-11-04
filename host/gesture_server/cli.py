"""Gesture WebSocket server command line (docs/system/e4b_visualization.md).

PC, no hardware (run from host/):
    python -m gesture_server.cli --source synthetic --web ../visualization/web
    python -m gesture_server.cli --source replay --replay ../build/vivado/kv260_e4a/e4b/results/mbx_rt2k/rt2k.jsonl \\
        --web ../visualization/web
    -> open http://127.0.0.1:8765/ (WebSocket ws://127.0.0.1:8765/ws)
KV260 (root, PYNQ venv; the E4a mailbox bitstream already loaded by the board tools):
    cd ~/riscv-emg-soc/<dir with ps_mailbox/ gesture_server/ web/> && sudo env XILINX_XRT=/usr \\
        PATH=/usr/local/share/pynq-venv/bin:$PATH /usr/local/share/pynq-venv/bin/python3 -m gesture_server.cli \\
        --source hardware --bit overlays/mbx_rt2k_long/mbx_rt2k_long.bit --loaded-bin fw/rv32_6a.bit.bin --web web

Listens on 127.0.0.1 unless --host <address> --lan is given (never all interfaces). Stop with
Ctrl-C / SIGTERM: the reader stops and unmaps before the port closes; only then may the board
tools unload the PL. Exit 2: the source could not start (hardware unavailable, mailbox in use),
nothing was mapped beyond the verification reads and no port was opened.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from .server import GestureServer, check_host
from .source import HardwareSource, ReplaySource, SourceError, SyntheticSource


def make_source(ns):
    if ns.source == "hardware":
        if not ns.bit:
            raise SourceError("--source hardware needs --bit")
        return HardwareSource(ns.bit, loaded_bin=ns.loaded_bin, manifest=ns.manifest, policy=ns.policy,
                              poll_ms=ns.poll_ms)
    if ns.source == "spi_live":
        if not ns.bit:
            raise SourceError("--source spi_live needs --bit")
        from spi_live.level import ActivityMeter
        from spi_live.source import SpiLiveSource
        meter = ActivityMeter.from_json(ns.levels) if ns.levels else ActivityMeter()
        return SpiLiveSource(ns.bit, meter=meter, loaded_bin=ns.loaded_bin, manifest=ns.manifest, policy=ns.policy,
                             poll_ms=ns.poll_ms)
    if ns.source == "synthetic":
        return SyntheticSource(rate_hz=ns.rate, hold=ns.hold, count=ns.count, poll_ms=ns.poll_ms)
    if not ns.replay:
        raise SourceError("--source replay needs --replay FILE")
    return ReplaySource(ns.replay, speed=ns.speed, rate_hz=ns.replay_rate, loop=ns.loop, poll_ms=ns.poll_ms)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["hardware", "spi_live", "synthetic", "replay"], required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--lan", action="store_true", help="allow a non-localhost --host (the board's LAN address)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--web", default=None, help="directory of the web UI (visualization/web)")
    ap.add_argument("--jsonl", default=None, help="append every result message here")
    ap.add_argument("--client-queue", type=int, default=64)
    ap.add_argument("--send-timeout", type=float, default=2.0)
    ap.add_argument("--history", type=int, default=20)
    ap.add_argument("--poll-ms", type=float, default=5.0)
    ap.add_argument("--bit", default=None)
    ap.add_argument("--loaded-bin", default=None)
    ap.add_argument("--levels", default=None, help="spi_live: levels.json of ml/scripts/train_spi_levels.py")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--policy", choices=["live", "backlog"], default="live",
                    help="hardware: where to start in an epoch the server attaches to (live = newest)")
    ap.add_argument("--rate", type=float, default=20.0, help="synthetic: results per second")
    ap.add_argument("--hold", type=int, default=30, help="synthetic: results per gesture")
    ap.add_argument("--count", type=int, default=0, help="synthetic: results in total (0 = endless)")
    ap.add_argument("--replay", nargs="*", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--replay-rate", type=float, default=0.0, help="replay at a fixed rate instead of the saved spacing")
    ap.add_argument("--loop", action="store_true")
    ns = ap.parse_args(argv)
    try:
        check_host(ns.host, ns.lan)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    try:
        src = make_source(ns)
        src.open()                     # hardware: Overlay(download=False) + checks + MMIO, once
    except SourceError as e:
        print(f"ERROR: source not available: {e}", file=sys.stderr)
        return 2
    srv = GestureServer(src, ns.host, ns.port, ns.web, ns.client_queue, ns.send_timeout, ns.history,
                        jsonl=ns.jsonl)

    async def amain():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, AttributeError, ValueError):     # Windows: Ctrl-C via KeyboardInterrupt
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        srv.ready = asyncio.Event()
        task = asyncio.ensure_future(srv.run(stop))
        await srv.ready.wait()
        print(f"READY source={src.label} http://{ns.host}:{srv.port}/  ws://{ns.host}:{srv.port}/ws", file=sys.stderr,
              flush=True)
        return await task

    rep = asyncio.run(amain())
    print("SHUTDOWN " + json.dumps(rep, default=str), file=sys.stderr)
    return 0 if rep.get("consumer_exited") else 1


if __name__ == "__main__":
    sys.exit(main())
