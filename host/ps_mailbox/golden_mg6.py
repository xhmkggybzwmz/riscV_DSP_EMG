"""E4c multi-gesture program: per-record golden table for the PS-side checks.

The RV32I program firmware/rv32/mg6 (mg6_common.h) plays six real GRABMyo segments, one per class
in E2/E3 order, each RUNS times; every run starts with SOS and gives DECISIONS results. Within one
producer epoch result n (1-based, RESULT_INDEX) is decision d of run r:
    r = (n - 1) // DECISIONS, d = (n - 1) % DECISIONS, segment s = r // RUNS
    scores / argmax / gesture = E2 decision d of segment s (identical for every run of s)
    ESI_TIMESTAMP = window end (399, 499, 599: the replay timestamp restarts at every run)
    WINDOW_END_SEQ = seq0 + r * L + window end,  SEGMENT = r & 0xFF
    flags = POSTPROC | ARGMAX_OK | GOLDEN_OK, + NEW_SEGMENT on d = 0, + LAST on the last result
RESET_EPOCH is the producer epoch (one per RV32I run of the whole program); SEGMENT counts the
SOS-separated replay runs inside it - the two are never mixed.

Source: the decisions file written by scripts/sim/gen_e4c.py (build/e4c/mg6_golden.json, or the
tracked test fixture host/gesture_server/tests/fixtures/e4c_mg6_expected.json); no samples.

    python -m ps_mailbox.golden_mg6 --decisions build/e4c/mg6_golden.json --runs 4 --out table.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import protocol as P

FLAGS_BASE = P.F_POSTPROC | P.F_ARGMAX_OK | P.F_GOLDEN_OK


def load(path: str | Path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if [s["class"] for s in d["segments"]] != list(range(len(P.LABELS))):
        raise ValueError(f"{path}: segments are not the six classes in E2/E3 order")
    return d


def table(dec: dict, runs: int) -> list[dict]:
    """expected record per RESULT_INDEX (list index = RESULT_INDEX - 1)"""
    L, seq0 = dec["segment_length"], dec.get("seq0", 0)
    segs = dec["segments"]
    nd = len(segs[0]["decisions"])
    total = len(segs) * runs * nd
    out = []
    for n in range(1, total + 1):
        r, d = divmod(n - 1, nd)
        s = r // runs
        seg, e = segs[s], segs[s]["decisions"][d]
        flags = FLAGS_BASE | (P.F_NEW_SEGMENT if d == 0 else 0) | (P.F_LAST if n == total else 0)
        out.append({"result_index": n, "run": r, "decision": d, "class": seg["class"], "label": seg["label"],
                    "gesture": e["argmax"], "argmax": e["argmax"], "scores": list(e["scores"]),
                    "esi_timestamp": e["end"], "window_end_seq": (seq0 + r * L + e["end"]) & P.MASK32,
                    "segment": r & 0xFF, "flags": flags,
                    "source": {k: seg.get(k) for k in ("gesture", "session", "participant", "trial", "start")}})
    return out


def check(rec: dict, exp: dict) -> list[str]:
    """differences of a record (decode_record / to_json / WebSocket v1 keys) from its table row"""
    got = {"result_index": rec["result_index"], "argmax": rec["argmax"], "scores": list(rec["scores"]),
           "esi_timestamp": rec["esi_timestamp"], "window_end_seq": rec["window_end_seq"], "flags": rec["flags"],
           "gesture": rec.get("gesture_id", rec.get("gesture")), "segment": rec.get("segment")}
    bad = [f"{k} {got[k]} != {exp[k]}" for k in ("result_index", "argmax", "scores", "esi_timestamp", "window_end_seq",
                                                  "flags", "gesture") if got[k] != exp[k]]
    if got["segment"] is not None and got["segment"] != exp["segment"]:
        bad.append(f"segment {got['segment']} != {exp['segment']}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--runs", type=int, default=4)
    ap.add_argument("--out", required=True)
    ns = ap.parse_args(argv)
    t = table(load(ns.decisions), ns.runs)
    Path(ns.out).write_text(json.dumps(t, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(t)} expected records -> {ns.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
