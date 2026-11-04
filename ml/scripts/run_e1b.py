"""E1b: LDA vs feature-MLP vs causal 1D-CNN on GRABMyo, identical record-level folds.

    python ml/scripts/run_e1b.py --config ml/configs/e1b_g1.toml [--workers 10] [--only CASE ...]

Writes analysis/ml/e1b/<run id>/ (git-ignored):
    splits.json        the fold manifest used by every model (+ its SHA-256 in results.json)
    results.json       every fold x model x seed result (metrics, confusion, training history)
    summary.json       aggregates (per subject, per test session, seed spread, paired comparisons, costs)
    report.md          human-readable tables
    contracts/         candidate deployment contracts of the reference fold (not frozen)
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
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path.insert(0, str(ML))

from emgml import labels  # noqa: E402
from emgml.datasets import grabmyo  # noqa: E402
from emgml.experiment import make_folds, record_keys, subject_task, val_split  # noqa: E402
from emgml.models.cnn import CNNSpec, cnn_cost  # noqa: E402
from emgml.models.mlp import mlp_cost  # noqa: E402

NAMES = labels.class_names()
N_CH = {k: len(v) for k, v in grabmyo.CHANNEL_CONFIGS.items()}


def environment() -> dict:
    import scipy
    import sklearn
    import torch
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    return {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
            "sklearn": sklearn.__version__, "torch": torch.__version__, "platform": platform.platform(),
            "git_commit": commit, "note": "ml/ is not committed yet; results tie to the files in the working tree"}


def per_class(conf) -> dict:
    c = np.asarray(conf, dtype=float)
    tp = np.diag(c)
    prec = np.divide(tp, c.sum(0), out=np.zeros_like(tp), where=c.sum(0) > 0)
    rec = np.divide(tp, c.sum(1), out=np.zeros_like(tp), where=c.sum(1) > 0)
    return {NAMES[i]: {"precision": float(prec[i]), "recall": float(rec[i])} for i in range(len(NAMES))}


def aggregate(rows: list[dict]) -> dict:
    """rows: results of one (case, model, protocol)."""
    seeds = sorted({r["seed"] for r in rows})
    folds = sorted({(r["subject"], r["fold"]) for r in rows})
    fold_ba = {f: float(np.mean([r["window"]["balanced_accuracy"] for r in rows if (r["subject"], r["fold"]) == f]))
               for f in folds}
    fold_f1 = {f: float(np.mean([r["window"]["macro_f1"] for r in rows if (r["subject"], r["fold"]) == f]))
               for f in folds}
    subjects = sorted({f[0] for f in folds})
    subj_ba = {s: float(np.mean([v for f, v in fold_ba.items() if f[0] == s])) for s in subjects}
    subj_f1 = {s: float(np.mean([v for f, v in fold_f1.items() if f[0] == s])) for s in subjects}
    sessions = sorted({r["test_session"] for r in rows})
    sess_ba = {}
    for se in sessions:
        v = [np.mean([r["window"]["balanced_accuracy"] for r in rows if r["subject"] == s and r["test_session"] == se])
             for s in subjects if any(r["subject"] == s and r["test_session"] == se for r in rows)]
        sess_ba[se] = {"mean": float(np.mean(v)), "std": float(np.std(v))}
    seed_means = []
    for sd in seeds:
        rs = [r for r in rows if r["seed"] == sd]
        sm = [np.mean([r["window"]["balanced_accuracy"] for r in rs if r["subject"] == s]) for s in subjects]
        seed_means.append(float(np.mean(sm)))
    first = [r for r in rows if r["seed"] == seeds[0]]
    conf = np.sum([r["window"]["confusion"] for r in first], axis=0)
    out = {"n_results": len(rows), "seeds": seeds, "n_folds": len(folds),
           "balanced_accuracy_mean": float(np.mean(list(subj_ba.values()))),
           "balanced_accuracy_std_subjects": float(np.std(list(subj_ba.values()))),
           "macro_f1_mean": float(np.mean(list(subj_f1.values()))),
           "macro_f1_std_subjects": float(np.std(list(subj_f1.values()))),
           "per_subject_balanced_accuracy": subj_ba, "per_test_session_balanced_accuracy": sess_ba,
           "seed_means": seed_means, "seed_std": float(np.std(seed_means)) if len(seed_means) > 1 else None,
           "confusion_seed0": conf.tolist(), "per_class_seed0": per_class(conf),
           "fold_balanced_accuracy": {f"{s}:{n}": v for (s, n), v in fold_ba.items()}}
    if "vote5" in rows[0]:
        out["vote5_balanced_accuracy_mean"] = float(np.mean(
            [np.mean([r["vote5"]["balanced_accuracy"] for r in rows if r["subject"] == s]) for s in subjects]))
    if "best_epoch" in rows[0]:
        out["best_epoch_mean"] = float(np.mean([r["best_epoch"] for r in rows]))
        out["epochs_run_mean"] = float(np.mean([r["epochs_run"] for r in rows]))
    if "folded_argmax_mismatch" in rows[0]:
        out["folded_argmax_mismatch_total"] = int(sum(r["folded_argmax_mismatch"] for r in rows))
    if "test_clipped_samples" in rows[0]:
        out["int16_lsb_mV_range"] = [float(min(r["int16_lsb_mV"] for r in rows)),
                                     float(max(r["int16_lsb_mV"] for r in rows))]
        out["test_clipped_samples_total"] = int(sum(r["test_clipped_samples"] for r in rows if r["seed"] == seeds[0]))
    return out


def paired(rows_a: list[dict], rows_b: list[dict]) -> dict | None:
    """b - a on identical (subject, fold), each averaged over seeds."""
    def fm(rows):
        d = {}
        for r in rows:
            d.setdefault((r["subject"], r["fold"]), []).append(r["window"]["balanced_accuracy"])
        return {k: float(np.mean(v)) for k, v in d.items()}
    a, b = fm(rows_a), fm(rows_b)
    common = sorted(set(a) & set(b))
    if not common:
        return None
    diff = np.array([b[k] - a[k] for k in common])
    subj = sorted({k[0] for k in common})
    sd = {s: float(np.mean([b[k] - a[k] for k in common if k[0] == s])) for s in subj}
    return {"n_pairs": len(common), "mean_diff": float(diff.mean()), "std_diff": float(diff.std()),
            "n_b_better": int(np.sum(diff > 0)), "n_a_better": int(np.sum(diff < 0)),
            "per_subject_mean_diff": sd, "subjects_b_better": int(sum(v > 0 for v in sd.values()))}


def costs(cfg: dict, case: dict) -> dict:
    n_ch = N_CH[case["channels"]]
    n_cls = len(NAMES)
    out = {"lda": {"note": "PC baseline only", "macs_per_window": n_ch * 5 * n_cls}}
    d_in = n_ch * 5
    mc = mlp_cost(d_in, tuple(cfg["mlp"]["hidden"]), n_cls)
    mc["stream_macs_per_s"] = mc["macs_per_window"] * 2000.0 / 100
    mc["weight_bytes_int8"] = mc["params"]
    mc["feature_ops_note"] = "plus the DSP feature extractor (per sample: |x|, x^2, |dx|, ZC, SSC per channel)"
    cc = cnn_cost(CNNSpec(in_ch=n_ch, layers=[tuple(l) for l in cfg["cnn"]["layers"]]))
    cc["weight_bytes_int8"] = cc["params_deployed"]
    out["mlp"], out["cnn"] = mc, cc
    return out


def fmt(v, s=None):
    return f"{v:.3f}" + (f" ± {s:.3f}" if s is not None else "")


def report(res: dict) -> str:
    L = [f"# E1b - {res['run_id']}", "",
         f"Data: {res['data']}; folds: {res['n_folds']}; splits sha256 `{res['splits_sha256'][:16]}`.", "",
         "All metrics: window-level balanced accuracy (bal.acc) unless noted; mean ± std over subjects "
         "(each fold first averaged over seeds).", "",
         "| case | model | protocol | bal.acc | macro-F1 | vote5 bal.acc | seed spread (std of seed means) | folds × seeds |",
         "|---|---|---|---|---|---|---|---|"]
    for key, a in res["summary"]["aggregates"].items():
        case, model, proto = key.split("|")
        L.append(f"| {case} | {model} | {proto} | {fmt(a['balanced_accuracy_mean'], a['balanced_accuracy_std_subjects'])} | "
                 f"{fmt(a['macro_f1_mean'], a['macro_f1_std_subjects'])} | "
                 f"{fmt(a['vote5_balanced_accuracy_mean']) if 'vote5_balanced_accuracy_mean' in a else '-'} | "
                 f"{fmt(a['seed_std']) if a['seed_std'] is not None else '-'} | {a['n_folds']} × {len(a['seeds'])} |")
    L += ["", "## Paired comparisons (same subject and test fold; b - a, averaged over seeds)", "",
          "| case | protocol | a | b | pairs | mean diff | std | b better / a better | subjects b better |",
          "|---|---|---|---|---|---|---|---|---|"]
    for key, p in res["summary"]["paired"].items():
        case, proto, a, b = key.split("|")
        L.append(f"| {case} | {proto} | {a} | {b} | {p['n_pairs']} | {p['mean_diff']:+.3f} | {p['std_diff']:.3f} | "
                 f"{p['n_b_better']} / {p['n_a_better']} | {p['subjects_b_better']}/{len(p['per_subject_mean_diff'])} |")
    for key, a in res["summary"]["aggregates"].items():
        case, model, proto = key.split("|")
        if case != res["primary_case"]:
            continue
        L += ["", f"## {case} {model} {proto}", "", "Per subject: " + ", ".join(
            f"{s}: {v:.3f}" for s, v in a["per_subject_balanced_accuracy"].items()),
              "", "Per test session: " + ", ".join(f"s{s}: {v['mean']:.3f} ± {v['std']:.3f}"
                                                   for s, v in a["per_test_session_balanced_accuracy"].items()), "",
              "| class | precision | recall |", "|---|---|---|"]
        for n, v in a["per_class_seed0"].items():
            L.append(f"| {n} | {v['precision']:.3f} | {v['recall']:.3f} |")
        L += ["", "Confusion (seed 0, rows true, columns predicted, order " + ", ".join(NAMES) + "):", ""]
        L += ["    " + " ".join(f"{x:6d}" for x in row) for row in a["confusion_seed0"]]
    L += ["", "## Model cost", "", "```", json.dumps(res["summary"]["costs"], indent=1)[:6000], "```"]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--only", action="append", default=None)
    ap.add_argument("--out", default=None)
    ns = ap.parse_args()
    cfg_path = Path(ns.config)
    with cfg_path.open("rb") as f:
        cfg = tomllib.load(f)
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(ns.out) if ns.out else REPO / "analysis" / "ml" / "e1b" / f"{cfg_path.stem}_{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    keys = record_keys(cfg)
    folds = make_folds(keys)
    split_doc = {p: [{"subject": f.subject, "name": f.name, "train": [list(keys[i]) for i in f.train],
                      "validation_for_nn": [list(keys[i]) for i in val_split(keys, f.train)[1]],
                      "test": [list(keys[i]) for i in f.test]} for f in fl] for p, fl in folds.items()}
    sj = json.dumps(split_doc, sort_keys=True)
    (out / "splits.json").write_text(sj, encoding="utf-8")
    splits_sha = hashlib.sha256(sj.encode()).hexdigest()
    cases = [c for c in cfg["cases"] if not ns.only or c["name"] in ns.only]
    workers = ns.workers or cfg["run"]["workers"]
    tasks = []
    for case in cases:
        for subj in cfg["data"]["participants"]:
            tasks.append({"cfg": cfg, "case": case, "subject": subj, "keys": keys, "threads": cfg["run"]["threads"],
                          "folds": {p: [f for f in folds[p] if f.subject == subj] for p in case["protocols"]},
                          "raw": REPO / cfg["data"]["raw_root"], "out_dir": str(out),
                          "dsp_path": ML / "configs" / "dsp" / f"{case['dsp']}.toml",
                          "reference_fold": cfg["run"].get("reference_fold") if case.get("export") else None})
    t0 = time.time()
    results, contracts, task_secs = [], {}, []
    print(f"{len(tasks)} tasks, {workers} workers x {cfg['run']['threads']} threads -> {out.relative_to(REPO)}",
          flush=True)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(subject_task, t): (t["case"]["name"], t["subject"]) for t in tasks}
        for fut in as_completed(futs):
            r = fut.result()
            results += r["results"]
            task_secs.append(r["seconds"])
            if r["contracts"]:
                contracts[r["case"]] = r["contracts"]
            print(f"  done {r['case']} subject {r['subject']} ({r['seconds']} s, {time.time() - t0:.0f} s total)",
                  flush=True)
    agg, pair = {}, {}
    for case in cases:
        for model in case["models"]:
            for proto in case["protocols"]:
                rows = [r for r in results if r["case"] == case["name"] and r["model"] == model and r["protocol"] == proto]
                if rows:
                    agg[f"{case['name']}|{model}|{proto}"] = aggregate(rows)
        for proto in case["protocols"]:
            for a, b in (("lda", "mlp"), ("lda", "cnn"), ("mlp", "cnn")):
                ra = [r for r in results if r["case"] == case["name"] and r["model"] == a and r["protocol"] == proto]
                rb = [r for r in results if r["case"] == case["name"] and r["model"] == b and r["protocol"] == proto]
                p = paired(ra, rb) if ra and rb else None
                if p:
                    pair[f"{case['name']}|{proto}|{a}|{b}"] = p
    # cross-case paired comparisons (same model, same folds)
    for a_case, b_case in cfg["run"].get("case_pairs", []):
        for model in ("lda", "mlp", "cnn"):
            for proto in ("P1", "P2"):
                ra = [r for r in results if r["case"] == a_case and r["model"] == model and r["protocol"] == proto]
                rb = [r for r in results if r["case"] == b_case and r["model"] == model and r["protocol"] == proto]
                p = paired(ra, rb) if ra and rb else None
                if p:
                    pair[f"{a_case}->{b_case}|{proto}|{model}|{model}"] = p
    summary = {"aggregates": agg, "paired": pair, "costs": {c["name"]: costs(cfg, c) for c in cases},
               "contract_equivalence": contracts}
    d = cfg["data"]
    res = {"run_id": run_id, "config_file": cfg_path.as_posix(), "config": cfg, "environment": environment(),
           "data": f"GRABMyo participants {d['participants']}, sessions {d['sessions']}, gestures {d['gestures']}, "
                   f"trials {d['trials']} ({len(keys)} recordings)",
           "n_folds": {p: len(v) for p, v in folds.items()}, "splits_sha256": splits_sha,
           "primary_case": cfg["run"]["primary_case"], "wall_seconds": round(time.time() - t0, 1),
           "task_seconds_sum": round(sum(task_secs), 1), "summary": summary}
    (out / "summary.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"run_id": run_id, "results": results}), encoding="utf-8")
    (out / "config.toml").write_bytes(cfg_path.read_bytes())
    (out / "report.md").write_text(report(res), encoding="utf-8")
    print(f"wall {res['wall_seconds']} s; report {(out / 'report.md').relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
