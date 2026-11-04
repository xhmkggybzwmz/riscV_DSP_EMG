"""Record labelled raw EMG through the ps_spi bitstream, on the board (docs/system/stm32_spi_live.md).

    sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:$PATH /usr/local/share/pynq-venv/bin/python3 -B \\
        -m spi_live.capture --bit <kv260_soc.bit> --out rec1.npz --rounds 6 --hold 10

The prompt on the terminal drives the session: REST, LIGHT, STRONG, each --hold seconds, --rounds times. The first
--settle seconds of a hold are labelled -1 (transition). Saves u16, label, round, lost; prints how many blocks were lost.
Real EMG samples: copy the file to analysis/ on the PC, never into git.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

CLASSES = ("REST", "LIGHT", "STRONG")
BLOCK = 128


class Recorder:
    """joins the blocks of SpiMonitor.frame(); a missing block is counted and filled with mid-rail, label -1"""

    def __init__(self):
        self.blocks, self.labels, self.rounds = [], [], []
        self.expected = None
        self.lost = 0
        self.seen = 0

    def add(self, index: int, samples: list[int], label: int, rnd: int) -> None:
        self.seen += 1
        if self.expected is not None:
            missing = ((index - self.expected) & 0xFFFF_FFFF) // BLOCK
            if missing:
                if missing > 1000:
                    raise RuntimeError(f"index jumped by {missing} blocks: did the STM32 restart?")
                self.lost += missing
                self.blocks += [[2048] * BLOCK] * missing
                self.labels += [-1] * missing
                self.rounds += [rnd] * missing
        self.expected = (index + BLOCK) & 0xFFFF_FFFF
        self.blocks.append(samples)
        self.labels.append(label)
        self.rounds.append(rnd)

    def arrays(self):
        import numpy as np
        u = np.array(self.blocks, dtype=np.uint16).ravel()
        return {"u16": u, "label": np.repeat(np.array(self.labels, dtype=np.int8), BLOCK),
                "round": np.repeat(np.array(self.rounds, dtype=np.int16), BLOCK), "lost": np.array(self.lost)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--hold", type=float, default=10.0)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--lead", type=float, default=5.0, help="seconds before the first hold")
    ap.add_argument("--allow-test-pattern", action="store_true")
    ns = ap.parse_args(argv)

    import numpy as np
    from pynq import MMIO, Overlay

    from spi_monitor.monitor import BASE, SIZE, SpiMonitor
    Overlay(ns.bit, download=False)
    mon = SpiMonitor(MMIO(BASE, SIZE))
    mon.verify()
    mon.clear()
    t0 = time.monotonic()
    while mon.snapshot().good_frames == 0:
        if time.monotonic() - t0 > 3.0:
            print("ERROR: no good EMG1 packet in 3 s: check power, wiring and the STM32", file=sys.stderr)
            return 2
        time.sleep(0.02)
    s = mon.snapshot()
    if s.sample_rate != 2000:
        print(f"ERROR: the STM32 reports {s.sample_rate} Hz, the pipeline needs 2000", file=sys.stderr)
        return 2
    _, first = mon.frame()
    if not ns.allow_test_pattern and all(((b - a) & 0xFFF) == 1 for a, b in zip(first, first[1:])):
        print("ERROR: this is the TEST pattern. Flash the STM32 with EMG_SOURCE_MODE=0, or give --allow-test-pattern",
              file=sys.stderr)
        return 2

    rec = Recorder()
    last = None

    def take(label: int, rnd: int) -> None:
        nonlocal last
        idx, samples = mon.frame()
        if idx != last:
            rec.add(idx, samples, label, rnd)
            last = idx

    print(f"get ready: {ns.lead:.0f} s", flush=True)
    end = time.monotonic() + ns.lead
    while time.monotonic() < end:
        take(-1, 0)
        time.sleep(0.005)
    for r in range(ns.rounds):
        for c, name in enumerate(CLASSES):
            print(f"round {r + 1}/{ns.rounds}: >>> {name} <<< for {ns.hold:.0f} s", flush=True)
            t0 = time.monotonic()
            while time.monotonic() - t0 < ns.hold:
                take(-1 if time.monotonic() - t0 < ns.settle else c, r)
                time.sleep(0.005)
    arr = rec.arrays()
    np.savez(ns.out, **arr)
    snap = mon.snapshot()
    report = {"blocks": rec.seen, "lost_blocks": rec.lost, "crc": snap.crc_errors, "seq": snap.seq_errors,
              "frame": snap.frame_errors, "overflow": snap.overflow_errors, "file": ns.out}
    for c, name in enumerate(CLASSES):
        x = arr["u16"][arr["label"] == c].astype(float)
        report[name] = {"mean": round(float(x.mean()), 1), "rms_ac": round(float(x.std()), 1),
                        "min": int(x.min()), "max": int(x.max())}
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
