"""E2 golden decisions of the replayed stream, for checking mailbox records on the PS.

The reference comes from the E2 golden vectors (build/e3/vec/<case>/logits.hex and
decisions.hex, the same files scripts/sim/gen_e3c.py turns into the firmware header), not
from the firmware: the board check is independent of the RV32I self-test.

    python -m ps_mailbox.golden --vec build/e3/vec/chain_s1_normal --out golden.json   (PC)
    g = load_golden("golden.json"); problems = check(rec, g, n)                      (board)

Record n (0-based, counted from the start of a run = one producer epoch) is result k = n %
NRES of replay pass p = n // NRES. Expected (firmware/rv32/e3c/e3c_common.h e3c_check):
scores / argmax of decision k, RESULT_INDEX n + 1, ESI_TIMESTAMP = fidx + p * NFRAMES,
WINDOW_END_SEQ = seq_base + that, SEGMENT = (seg + p * NSEG) & 0xFF, GESTURE = ARGMAX
(post-processing K = 1, T = 0).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import protocol as P


def _words(path: Path) -> list[int]:
    return [int(x, 16) for x in path.read_text(encoding="ascii").split()]


def from_vectors(vec: Path) -> dict:
    """golden table from the E2 vector directory (as scripts/sim/gen_e3c.py)"""
    frames = _words(vec / "input_frames.hex")
    dec, log = _words(vec / "decisions.hex"), _words(vec / "logits.hex")
    seg_start = [i for i, f in enumerate(frames) if (f >> 128) & 0x3]
    rows = []
    for k, (dw, lw) in enumerate(zip(dec, log)):
        am, end, seg = dw & 0xF, (dw >> 40) & 0xFFFFFF, (dw >> 64) & 0xFF
        sc = [P.s32(lw >> (32 * c)) for c in range(6)]
        if ((lw >> 192) & 0xFFFFFF) != end or ((lw >> 216) & 0xFF) != seg:
            raise ValueError(f"decision {k}: logits / decisions disagree")
        if P.argmax_lowest(sc) != am:
            raise ValueError(f"decision {k}: argmax {am} does not belong to the logits")
        rows.append({"scores": sc, "argmax": am, "end": end, "seg": seg, "fidx": seg_start[seg] + end})
    return {"source": vec.as_posix(), "nframes": len(frames), "nseg": len(seg_start), "results": rows,
            "labels": list(P.LABELS)}


def load_golden(path: str | Path) -> dict:
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    if tuple(g["labels"]) != P.LABELS:
        raise ValueError(f"{path}: class order differs from protocol.LABELS")
    return g


def expected(g: dict, n: int, seq_base: int = 0) -> dict:
    nres = len(g["results"])
    p, k = divmod(n, nres)
    r = g["results"][k]
    ts = r["fidx"] + p * g["nframes"]
    return {"result_index": n + 1, "scores": r["scores"], "argmax": r["argmax"], "gesture": r["argmax"],
            "esi_timestamp": ts, "window_end_seq": (seq_base + ts) & P.MASK32,
            "segment": (r["seg"] + p * g["nseg"]) & 0xFF}


def check(rec: dict, g: dict, n: int, seq_base: int = 0) -> list[str]:
    """differences of record `rec` (decode_record or to_json keys) from golden result n"""
    e = expected(g, n, seq_base)
    got = dict(rec)
    if "gesture_id" in rec:                     # to_json: "gesture" is the label name
        got["gesture"] = rec["gesture_id"]
    return [f"{k} {got[k]} != golden {v}" for k, v in e.items() if got[k] != v]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vec", required=True, help="E2 vector directory (build/e3/vec/chain_s1_normal)")
    ap.add_argument("--out", required=True)
    ns = ap.parse_args(argv)
    g = from_vectors(Path(ns.vec))
    Path(ns.out).write_text(json.dumps(g, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(g['results'])} golden results, {g['nframes']} frames -> {ns.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
