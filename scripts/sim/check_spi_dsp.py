#!/usr/bin/env python3
"""Bit-exact check of the live DSP configuration (gen_spi_live.SEC_MASK and its coefficients) on a real recording:
RTL emg_dsp against reference/emg_fixed FixedDSP, with dv/e3/tb_emg_dsp.v, frame by frame.

    python scripts/sim/gen_spi_live.py
    python scripts/sim/check_spi_dsp.py analysis/spi/rec1.npz [--frames 39000]

The vectors hold real samples, so they stay in the ignored build/spi/dsp_check.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "ml"), str(REPO / "scripts" / "sim")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rec")
    ap.add_argument("--frames", type=int, default=39000)
    ns = ap.parse_args()

    from gen_spi_live import SEC_MASK, dsp_slots
    from reference.emg_fixed import vectors
    from reference.emg_fixed.dsp.fixed_dsp import FixedDSP
    from reference.emg_fixed.esi import FLAG_POST_DSP
    from run_e3 import VVP, compile_target, win

    u = np.load(ns.rec)["u16"][:ns.frames]
    q = np.clip((u.astype(np.int64) - 2048) * 8, -32768, 32767).astype(np.int16)
    x = np.zeros((len(q), 4), dtype=np.int16)
    x[:, 0] = q
    slots = dsp_slots()
    y = FixedDSP([slots[i] for i in range(4) if SEC_MASK >> i & 1], 4).process(x, sos=True)

    def words(a, post=0):
        out = []
        for t, row in enumerate(a):
            lanes = list(int(v) for v in row) + [0] * 4
            flags = (vectors.FLAG_SOS if t == 0 else 0) | post
            out.append(vectors.pack(lanes, 16) | (flags << 128) | (t << 144))
        return out

    d = REPO / "build" / "spi" / "dsp_check"
    d.mkdir(parents=True, exist_ok=True)
    vectors.write_words(d / "frames.hex", words(x), 176)
    vectors.write_words(d / "expect.hex", words(y, FLAG_POST_DSP), 176)
    vvp = compile_target("dsp", d, inc=REPO / "build" / "e3")
    r = subprocess.run([str(VVP), str(vvp), f"+vec={win(d)}", f"+frames={len(u)}", f"+mask={SEC_MASK}", "+seed=1"],
                       capture_output=True, text=True, cwd=d)
    (d / "run.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    tail = "\n".join(r.stdout.splitlines()[-6:])
    ok = r.returncode == 0 and "PASS" in r.stdout and "FAIL" not in r.stdout
    print(f"frames {len(u)}, mask 0x{SEC_MASK:X}, max|y| {int(np.abs(y).max())}\n{tail}\n[{'PASS' if ok else 'FAIL'}] log {d / 'run.log'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
