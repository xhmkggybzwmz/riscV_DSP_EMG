"""E1a: time-domain features + LDA on GRABMyo (P1 within-session, P2 cross-day).

    python ml/scripts/run_lda.py --config ml/configs/e1a_lda_g0.toml [--out analysis/ml/e1a_lda/<id>]

For every (channel config, DSP variant, trim) case listed in the config:
  recordings -> DSP pipeline (SOS per recording) -> windows -> record-level folds
  -> per fold: feature subset chosen on inner folds inside the training records
  -> LDA fitted on the training records -> metrics on the test records
Writes results.json, report.md, the config and DSP variant files, environment
(versions, git commit, data hashes) to the output directory (git-ignored).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path.insert(0, str(ML))

from emgml import labels  # noqa: E402
from emgml.datasets import grabmyo  # noqa: E402
from emgml.dsp import DSPConfig  # noqa: E402
from emgml.metrics import majority_vote, summarize  # noqa: E402
from emgml.models.lda import FEATURE_SETS, make_lda  # noqa: E402
from emgml.splits import check_fold, inner_folds, p1_folds, p2_folds, window_mask  # noqa: E402
from emgml.windowing import feature_columns, make_windows  # noqa: E402

LABELS = sorted(labels.UNIFIED)


def environment() -> dict:
    import scipy
    import sklearn
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "ml"], cwd=REPO, capture_output=True,
                                    text=True).stdout.strip())
    except OSError:
        commit, dirty = "unknown", True
    return {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
            "sklearn": sklearn.__version__, "platform": platform.platform(), "git_commit": commit,
            "ml_dir_uncommitted_changes": dirty}


def bal_acc(model, X, y, tr, va):
    model.fit(X[tr], y[tr])
    return summarize(y[va], model.predict(X[va]), LABELS)["balanced_accuracy"]


def run_case(recs, dsp: DSPConfig, trim_s: float, feature_sets: list[str], vote_k: list[int], seed: int) -> dict:
    t0 = time.time()
    ws = make_windows(recs, dsp, trim_s)
    out = {"n_windows": int(len(ws.y)), "windows_per_record": float(len(ws.y) / len(recs)),
           "feature_names": ws.feature_names, "protocols": {}}
    for proto, folds in (("P1", p1_folds(ws.records)), ("P2", p2_folds(ws.records))):
        res = []
        for fold in folds:
            check_fold(ws.records, fold)                       # raises on any leakage
            # ---- model selection on inner folds (training records only) ----
            scores = {}
            for fs in feature_sets:
                cols = feature_columns(ws.feature_names, FEATURE_SETS[fs])
                X = ws.X[:, cols]
                s = [bal_acc(make_lda(), X, ws.y, window_mask(ws.record, tr), window_mask(ws.record, va))
                     for tr, va in inner_folds(ws.records, fold.train, proto)]
                scores[fs] = float(np.mean(s))
            chosen = max(feature_sets, key=lambda k: (scores[k], -feature_sets.index(k)))
            # ---- final fit on all training records, evaluation on the test records ----
            cols = feature_columns(ws.feature_names, FEATURE_SETS[chosen])
            trm, tem = window_mask(ws.record, fold.train), window_mask(ws.record, fold.test)
            assert not np.any(trm & tem)
            model = make_lda().fit(ws.X[trm][:, cols], ws.y[trm])
            pred = model.predict(ws.X[tem][:, cols])
            entry = {"fold": fold.name, "subject": fold.subject, "chosen_features": chosen, "inner_scores": scores,
                     "n_train_windows": int(trm.sum()), "n_test_windows": int(tem.sum()),
                     "n_train_records": len(fold.train), "n_test_records": len(fold.test),
                     "window": summarize(ws.y[tem], pred, LABELS)}
            for k in vote_k:
                if k > 1:
                    entry[f"vote{k}"] = summarize(ws.y[tem], majority_vote(pred, ws.record[tem], k), LABELS)
            res.append(entry)
        out["protocols"][proto] = {"folds": res, "summary": aggregate(res, vote_k)}
    out["seconds"] = round(time.time() - t0, 1)
    out["seed"] = seed
    return out


def aggregate(res: list[dict], vote_k: list[int]) -> dict:
    keys = ["window"] + [f"vote{k}" for k in vote_k if k > 1]
    agg = {}
    for key in keys:
        per_subj = {}
        for s in sorted({r["subject"] for r in res}):
            rs = [r[key] for r in res if r["subject"] == s]
            conf = np.sum([r["confusion"] for r in rs], axis=0)
            per_subj[s] = {"balanced_accuracy": float(np.mean([r["balanced_accuracy"] for r in rs])),
                           "macro_f1": float(np.mean([r["macro_f1"] for r in rs])),
                           "accuracy": float(np.mean([r["accuracy"] for r in rs])),
                           "confusion": conf.tolist()}
        ba = [v["balanced_accuracy"] for v in per_subj.values()]
        f1 = [v["macro_f1"] for v in per_subj.values()]
        agg[key] = {"per_subject": per_subj,
                    "balanced_accuracy_mean": float(np.mean(ba)), "balanced_accuracy_std": float(np.std(ba)),
                    "macro_f1_mean": float(np.mean(f1)), "macro_f1_std": float(np.std(f1)),
                    "confusion_total": np.sum([v["confusion"] for v in per_subj.values()], axis=0).tolist()}
    chosen = {}
    for r in res:
        chosen[r["chosen_features"]] = chosen.get(r["chosen_features"], 0) + 1
    agg["chosen_feature_sets"] = chosen
    return agg


def report_md(results: dict) -> str:
    lines = [f"# E1a LDA baseline - {results['run_id']}", "",
             f"Config: `{results['config_file']}`; data: {results['data']['n_records']} recordings "
             f"({results['data']['description']}); seed {results['seed']}.", "",
             "| case | windows | P1 bal.acc | P1 macro-F1 | P2 bal.acc | P2 macro-F1 | P2 vote3 bal.acc | P2 vote5 bal.acc |",
             "|---|---|---|---|---|---|---|---|"]
    for name, c in results["cases"].items():
        p1, p2 = c["protocols"]["P1"]["summary"], c["protocols"]["P2"]["summary"]
        f = lambda d, k="balanced_accuracy": f"{d[k + '_mean']:.3f} ± {d[k + '_std']:.3f}"
        lines.append(f"| {name} | {c['n_windows']} | {f(p1['window'])} | {f(p1['window'], 'macro_f1')} | "
                     f"{f(p2['window'])} | {f(p2['window'], 'macro_f1')} | "
                     f"{f(p2['vote3']) if 'vote3' in p2 else '-'} | {f(p2['vote5']) if 'vote5' in p2 else '-'} |")
    lines += ["", "Mean ± std over subjects (each subject: mean over its folds). Class order: "
              + ", ".join(labels.class_names()), ""]
    prim = results["primary_case"]
    for proto in ("P1", "P2"):
        s = results["cases"][prim]["protocols"][proto]["summary"]["window"]
        lines += [f"## {prim} - {proto} per subject", "", "| subject | bal.acc | macro-F1 | accuracy |", "|---|---|---|---|"]
        for subj, v in s["per_subject"].items():
            lines.append(f"| {subj} | {v['balanced_accuracy']:.3f} | {v['macro_f1']:.3f} | {v['accuracy']:.3f} |")
        lines += ["", f"Confusion matrix (rows = true, columns = predicted, all subjects and folds):", "",
                  "| | " + " | ".join(labels.class_names()) + " |", "|---" * (len(LABELS) + 1) + "|"]
        for i, row in enumerate(s["confusion_total"]):
            lines.append(f"| {labels.class_names()[i]} | " + " | ".join(str(x) for x in row) + " |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ML / "configs" / "e1a_lda_g0.toml"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", action="append", default=None, help="run only these case names (repeatable)")
    ns = ap.parse_args()
    cfg_path = Path(ns.config)
    with cfg_path.open("rb") as f:
        cfg = tomllib.load(f)
    d, r = cfg["data"], cfg["run"]
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(ns.out) if ns.out else REPO / "analysis" / "ml" / "e1a_lda" / run_id
    out.mkdir(parents=True, exist_ok=True)
    np.random.seed(r["seed"])
    raw = REPO / d["raw_root"]
    results = {"run_id": run_id, "config_file": cfg_path.as_posix(), "config": cfg, "seed": r["seed"],
               "environment": environment(), "cases": {}, "primary_case": r["primary_case"]}
    rec_cache = {}
    cases = [c for c in r["cases"] if not ns.only or c["name"] in ns.only]
    if ns.only and len(cases) != len(ns.only):
        sys.exit(f"unknown case in {ns.only}")
    if r["primary_case"] not in [c["name"] for c in cases]:
        results["primary_case"] = cases[0]["name"]
    for case in cases:
        chan, variant, trim = case["channels"], case["dsp"], float(case.get("trim_start_s", 0.0))
        if chan not in rec_cache:
            rec_cache[chan] = grabmyo.load_subset(raw, d["participants"], d["sessions"], d["gestures"],
                                                  d["trials"], chan, d["fs_out"])
        recs = rec_cache[chan]
        dsp = DSPConfig.load(ML / "configs" / "dsp" / f"{variant}.toml")
        dsp.n_ch = recs[0].n_ch
        dsp.fs = recs[0].fs
        dsp.validate()
        dsp.save(out / f"dsp_{variant}_{chan}.json")
        name = case.get("name", f"{chan}/{variant}" + (f"/trim{trim}" if trim else ""))
        print(f"case {name}: {len(recs)} recordings ...", flush=True)
        results["cases"][name] = {"channels": chan, "dsp": variant, "trim_start_s": trim,
                                  **run_case(recs, dsp, trim, r["feature_sets"], r["vote_k"], r["seed"])}
        s = results["cases"][name]["protocols"]
        print(f"   P1 bal.acc {s['P1']['summary']['window']['balanced_accuracy_mean']:.3f}  "
              f"P2 bal.acc {s['P2']['summary']['window']['balanced_accuracy_mean']:.3f}  "
              f"({results['cases'][name]['seconds']} s)", flush=True)
    any_recs = next(iter(rec_cache.values()))
    h = hashlib.sha256("".join(sorted(x.sha256 for x in any_recs)).encode()).hexdigest()
    results["data"] = {"n_records": len(any_recs), "dataset": any_recs[0].dataset,
                       "description": f"participants {d['participants']}, sessions {d['sessions']}, "
                                      f"gestures {d['gestures']}, trials {d['trials']}",
                       "records_sha256_digest": h, "fs": any_recs[0].fs, "fs_original": any_recs[0].fs_original}
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (out / "config.toml").write_bytes(cfg_path.read_bytes())
    (out / "report.md").write_text(report_md(results), encoding="utf-8")
    print(f"results in {out.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
