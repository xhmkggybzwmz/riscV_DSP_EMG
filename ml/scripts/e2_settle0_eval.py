"""E3 P0: re-evaluate the saved E2 INT8 models with the settle-0 decision schedule.

    python ml/scripts/e2_settle0_eval.py --run analysis/ml/e2/<run>

Uses the frozen int8 models and each fold's training-derived LSB (from the saved json); no retraining,
no requantisation. Reports P2 balanced accuracy over all settle-0 windows (97 per record: ends
399..9999) and over the windows ending >= 599 only, which must reproduce E2's recorded v4 exactly.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path[:0] = [str(ML), str(REPO / "reference")]

RAW = REPO / "data" / "public" / "grabmyo" / "raw"


def eval_subject(args):
    sys.path[:0] = [str(ML), str(REPO / "reference")]
    from emg_fixed.cnn.int_model import run_record_int
    from emg_fixed.cnn.quantize import from_saved
    from emg_fixed.dsp.fixed_dsp import FixedDSP, sections_from_config
    from emg_fixed.esi import SourceCalibration, to_esi
    from emgml.datasets import grabmyo
    from emgml.dsp import DSPConfig
    from emgml.metrics import summarize
    run, subj, rows = Path(args["run"]), args["subject"], args["rows"]
    dsp = DSPConfig.load(ML / "configs" / "dsp" / "dc_bp20_450.toml")
    dsp.n_ch = 4
    secs = sections_from_config(dsp)
    out = []
    cache = {}
    for r in rows:
        stem = Path(r["int8_file"]).with_suffix("")
        q = from_saved(run / f"{stem}.npz", run / f"{stem}.json")
        meta = json.loads((run / f"{stem}.json").read_text(encoding="utf-8"))
        cal = SourceCalibration(**meta["esi"])
        sess = r["test_session"]
        key = (sess, cal.lsb)
        if key not in cache:
            recs = [grabmyo.load_trial(RAW, sess, subj, g, t, "bip4", 2000.0)
                    for g in (4, 11, 12, 15, 16, 17) for t in range(1, 8)]
            codes = np.concatenate([to_esi(x.x, cal)[0] for x in recs], axis=1)
            y = FixedDSP(secs, codes.shape[1]).process(codes, sos=True)
            cache[key] = ([y[:, 4 * i:4 * i + 4] for i in range(len(recs))], [x.label for x in recs])
        posts, labs = cache[key]
        q.settle = 0
        preds, ys, ends = [], [], []
        for y_, lab in zip(posts, labs):
            rr = run_record_int(q, y_)
            preds.append(rr["argmax"])
            ys.append(np.full(len(rr["argmax"]), lab))
            ends.append(rr["ends"])
        p, yy, e = np.concatenate(preds), np.concatenate(ys), np.concatenate(ends)
        a = summarize(yy, p, list(range(6)))
        m = e >= 599
        b = summarize(yy[m], p[m], list(range(6)))
        early = e < 599
        c = summarize(yy[early], p[early], list(range(6)))
        out.append({"subject": subj, "fold": r["fold"], "seed": r["seed"], "windows_per_record": int(len(e) / len(posts)),
                    "ba_settle0_all": a["balanced_accuracy"], "ba_ends_ge_599": b["balanced_accuracy"],
                    "ba_ends_399_499": c["balanced_accuracy"], "e2_recorded_v4": r["v4_int8"]["balanced_accuracy"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--workers", type=int, default=5)
    ns = ap.parse_args()
    run = Path(ns.run).resolve()
    rows = json.loads((run / "results.json").read_text(encoding="utf-8"))
    tasks = [{"run": str(run), "subject": s, "rows": [r for r in rows if r["subject"] == s]}
             for s in sorted({r["subject"] for r in rows})]
    res = []
    with ProcessPoolExecutor(max_workers=ns.workers) as ex:
        for o in ex.map(eval_subject, tasks):
            res += o
    exact = all(abs(r["ba_ends_ge_599"] - r["e2_recorded_v4"]) == 0 for r in res)

    def subj_mean(k):
        s = sorted({r["subject"] for r in res})
        v = [np.mean([r[k] for r in res if r["subject"] == x]) for x in s]
        return float(np.mean(v)), float(np.std(v))
    summary = {"n": len(res), "windows_per_record": sorted({r["windows_per_record"] for r in res}),
               "ends_ge_599_reproduce_e2_exactly": exact,
               "ba_settle0_all": subj_mean("ba_settle0_all"), "ba_ends_ge_599": subj_mean("ba_ends_ge_599"),
               "ba_ends_399_499_only": subj_mean("ba_ends_399_499"),
               "paired_all_minus_ge599": float(np.mean([r["ba_settle0_all"] - r["ba_ends_ge_599"] for r in res]))}
    (run / "settle0_eval.json").write_text(json.dumps({"summary": summary, "rows": res}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
