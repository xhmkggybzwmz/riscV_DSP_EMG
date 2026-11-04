#!/usr/bin/env python3
"""E5a simulation inputs (git-ignored, real GRABMyo samples): the "PS memory" image of the six E4c
segments for the DMA models.

    python scripts/sim/gen_e5.py        # needs build/e4c/vec (scripts/sim/gen_e4c.py)

build/e5/vec/ddr_mg6.hex: one 64-bit word per line = one frame {lane3, lane2, lane1, lane0}, segment s at
word s * 600: the int16 array [T, 4] in memory, as host/ps_dma/feeder.py lays it out. The words are the first
600 * 6 frames of build/e4c/vec/chain_mg6_p1/input_frames.hex; their little-endian bytes must hash to
frame_words_le_sha256 of build/e4c/fw/mg6_data.json (the frames compiled into mbx_mg6_rt).
The golden data of the tests stay build/e4c/vec/chain_mg6_p<P>.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VEC = REPO / "build" / "e4c" / "vec" / "chain_mg6_p1"
PROV = REPO / "build" / "e4c" / "fw" / "mg6_data.json"
OUT = REPO / "build" / "e5" / "vec"


def main() -> int:
    if not VEC.is_dir() or not PROV.is_file():
        sys.exit("missing build/e4c: run ml/scripts/e4c_multigesture_eval.py and scripts/sim/gen_e4c.py")
    lines = (VEC / "input_frames.hex").read_text().split()
    prov = json.loads(PROV.read_text(encoding="utf-8"))
    words, flags = [], []
    for w in lines:
        v = int(w, 16)
        words.append(v & ((1 << 64) - 1))
        flags.append((v >> 128) & 0xFF)
    L = prov["segment_length"]
    if len(words) != L * len(prov["segments"]):
        sys.exit(f"{len(words)} frames, expected {L} x {len(prov['segments'])}")
    if [i for i, f in enumerate(flags) if f] != list(range(0, len(words), L)):
        sys.exit("SOS is not exactly on the first frame of every segment")
    le = b"".join(struct.pack("<Q", w) for w in words)
    sha = hashlib.sha256(le).hexdigest()
    if sha != prov["frame_words_le_sha256"]:
        sys.exit(f"frames differ from the E4c frame table: {sha} != {prov['frame_words_le_sha256']}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ddr_mg6.hex").write_text("".join(f"{w:016X}\n" for w in words), encoding="ascii", newline="\n")
    print(f"{len(words)} frames -> {(OUT / 'ddr_mg6.hex').relative_to(REPO)}  sha256(le bytes) {sha[:16]} == E4c frame table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
