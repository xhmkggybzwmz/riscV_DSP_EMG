"""E5a: load GRABMyo records on the PS and send them to the PL through the AXI DMA.

  sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:$PATH /usr/local/share/pynq-venv/bin/python3 -m ps_dma.cli
      --bit overlays/mbx_dma_live/mbx_dma_live.bit --loaded-bin fw/rv32_6a.bit.bin
      --data-root ~/grabmyo/raw --plan mg6 --passes 4 --period 50000

The PL must already be loaded (scripts/fpga/kv260_ps_board.py load) and the mailbox consumer (gesture_server or
ps_mailbox.cli) started separately: this process only feeds data. It maps the DMA and source pages, never the mailbox.

  --plan mg6                     the six E4c segments, each --passes times in a row (frames hash checked against the PC)
  --plan records --record S:P:G:T[:START:LEN]   records in the order given (repeatable), --passes times each
  --dry-run                      load and convert only, no PL access (any machine with numpy)
  --report FILE                  JSON: frames hash, sources, per-segment times, source counters
Exit 0 only if every transfer finished without error and no frame went missing.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import dataset as D
from . import registers as R
from .feeder import DmaFeeder, FeederError, layout

FIXTURE = Path(__file__).resolve().parents[1] / "gesture_server" / "tests" / "fixtures" / "e4c_mg6_expected.json"


def parse_record(text: str) -> dict:
    v = [int(x) for x in text.split(":")]
    if len(v) not in (4, 6):
        raise argparse.ArgumentTypeError(f"{text!r}: S:P:G:T or S:P:G:T:START:LEN")
    d = dict(zip(("session", "participant", "gesture", "trial"), v))
    if len(v) == 6:
        d.update(start=v[4], length=v[5])
    return d


def build_segments(ns, cfg: dict) -> list[D.Segment]:
    if ns.plan == "mg6":
        return D.mg6_segments(ns.data_root, ns.fixture, cfg)
    if not ns.record:
        raise SystemExit("--plan records needs at least one --record")
    return [D.load_segment(ns.data_root, r["session"], r["participant"], r["gesture"], r["trial"], cfg,
                           r.get("start", 0), r.get("length")) for r in ns.record]


class PynqMem:
    """pynq.allocate buffer as feeder.mem"""

    def __init__(self, buf):
        self.buf = buf
        self.physical_address = int(buf.physical_address)
        self.data = buf

    def flush(self) -> None:
        self.buf.flush()


def feed(ns, segments, rep) -> int:
    from ps_mailbox.overlay_device import DeviceError           # noqa: PLC0415

    from .device import PynqDmaDevice                           # noqa: PLC0415
    if not ns.bit:
        raise SystemExit("--bit is required (or --dry-run)")
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    dev = PynqDmaDevice(ns.bit, loaded_bin=ns.loaded_bin)
    fd = None
    buf = None
    try:
        dev.verify()
        rep["device"] = dev.report
        from pynq import allocate                               # noqa: PLC0415  (board only)
        _, total = layout(segments)
        buf = allocate(shape=(total,), dtype="u1")
        rep["buffer"] = {"bytes": total, "physical_address": f"0x{int(buf.physical_address):X}"}
        fd = DmaFeeder(dev.open_dma(), dev.open_source(), PynqMem(buf))
        fd.load(segments)
        fd.start(ns.period, ns.seq_base)
        order = [i for i in range(len(segments)) for _ in range(ns.passes)]
        want = sum(segments[i].frames.shape[0] for i in order)
        fd.run(order, lambda: stop["now"])
        n = sum(s.frames for s in fd.sent)
        fd.wait_drained(n)
        c = fd.counters()
        rep["sent"] = [vars(s) for s in fd.sent]
        rep["counters"] = c
        print(f"SENT {len(fd.sent)} transfers, {n} frames: {c}", flush=True)
        return 0 if n == want and c["frames"] == want and c["segments"] == len(order) and c["keep_err"] == 0 else 1
    except (DeviceError, FeederError) as e:
        rep["error"] = str(e)
        print(f"ERROR {e}")
        return 1
    finally:
        if fd is not None and fd.started:
            fd.stop()
        if buf is not None:
            buf.freebuffer()
        dev.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bit")
    ap.add_argument("--loaded-bin")
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--plan", choices=["mg6", "records"], default="mg6")
    ap.add_argument("--record", action="append", type=parse_record)
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--period", type=int, default=R.PERIOD_2KHZ, help="cycles per frame (50000 = 2 kHz, 0 = flow)")
    ap.add_argument("--seq-base", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report")
    ns = ap.parse_args(argv)
    cfg = D.load_config()
    t0 = time.monotonic()
    segments = build_segments(ns, cfg)
    sha = D.frames_sha256(segments)
    rep = {"plan": ns.plan, "passes": ns.passes, "period": ns.period, "load_s": round(time.monotonic() - t0, 3),
           "frames_le_sha256": sha, "segments": [{"label": s.label, **s.source} for s in segments]}
    print(f"LOADED {len(segments)} segments, {sum(s.frames.shape[0] for s in segments)} frames in {rep['load_s']} s, "
          f"sha256 {sha[:16]}", flush=True)
    rc = 0
    if ns.plan == "mg6" and sha != cfg["mg6_frames_le_sha256"]:
        print(f"ERROR frames differ from the PC reference ({cfg['mg6_frames_le_sha256'][:16]}): results would not "
              "compare with the E2 tables")
        rc = 1
    elif not ns.dry_run:
        rc = feed(ns, segments, rep)
    if ns.report:
        Path(ns.report).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
