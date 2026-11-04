"""E2: float vs fixed-point (fixed DSP + INT8 CNN) on the E1b P2 folds.

    python ml/scripts/run_e2.py --config ml/configs/e2_g1.toml [--workers N] [--subjects 1 2]

Writes analysis/ml/e2/<run id>/ (git-ignored): models/ (float + int8 per fold and seed, with
hashes), results.json, summary.json, report.md.
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
sys.path[:0] = [str(ML), str(REPO / "reference")]

from emg_fixed.e2_eval import LABELS, REF_VERSION, subject_task  # noqa: E402
from emgml import labels  # noqa: E402
from emgml.experiment import make_folds, record_keys, val_split  # noqa: E402

VARIANTS = ["v1_torch_float", "v2_folded_float", "v3_int16in_float_dsp", "v4b_fixed_dsp_float_cnn", "v4_int8"]


def splits_doc_sha(keys, folds) -> str:
    split_doc = {p: [{"subject": f.subject, "name": f.name, "train": [list(keys[i]) for i in f.train],
                      "validation_for_nn": [list(keys[i]) for i in val_split(keys, f.train)[1]],
                      "test": [list(keys[i]) for i in f.test]} for f in fl] for p, fl in folds.items()}
    return hashlib.sha256(json.dumps(split_doc, sort_keys=True).encode()).hexdigest()


def agg_variant(rows, key):
    subj = sorted({r["subject"] for r in rows})
    fold_ba = {}
    for r in rows:
        fold_ba.setdefault((r["subject"], r["fold"]), []).append(r[key]["balanced_accuracy"])
    fold_ba = {k: float(np.mean(v)) for k, v in fold_ba.items()}
    ps = {s: float(np.mean([v for k, v in fold_ba.items() if k[0] == s])) for s in subj}
    f1 = {s: float(np.mean([r[key]["macro_f1"] for r in rows if r["subject"] == s])) for s in subj}
    seeds = sorted({r["seed"] for r in rows})
    seed_means = [float(np.mean([np.mean([r[key]["balanced_accuracy"] for r in rows if r["seed"] == sd and r["subject"] == s])
                                 for s in subj])) for sd in seeds]
    conf = np.sum([r[key]["confusion"] for r in rows if r["seed"] == seeds[0]], axis=0)
    recall = (np.diag(conf) / conf.sum(1)).tolist()
    sess = {}
    for se in sorted({r["test_session"] for r in rows}):
        sess[se] = float(np.mean([r[key]["balanced_accuracy"] for r in rows if r["test_session"] == se]))
    return {"balanced_accuracy_mean": float(np.mean(list(ps.values()))),
            "balanced_accuracy_std_subjects": float(np.std(list(ps.values()))),
            "macro_f1_mean": float(np.mean(list(f1.values()))), "per_subject": ps, "per_test_session": sess,
            "seed_means": seed_means, "fold_ba": {f"{k[0]}:{k[1]}": v for k, v in fold_ba.items()},
            "recall_seed0": dict(zip(labels.class_names(), recall)), "confusion_seed0": conf.tolist()}


def paired(rows, a, b):
    d = {}
    for r in rows:
        d.setdefault((r["subject"], r["fold"]), []).append(r[b]["balanced_accuracy"] - r[a]["balanced_accuracy"])
    diffs = np.array([np.mean(v) for v in d.values()])
    per_subj = {}
    for (s, _), v in d.items():
        per_subj.setdefault(s, []).append(np.mean(v))
    return {"mean": float(diffs.mean()), "std": float(diffs.std()), "min": float(diffs.min()), "max": float(diffs.max()),
            "n_folds": len(diffs), "n_b_better": int(np.sum(diffs > 0)), "n_b_worse": int(np.sum(diffs < 0)),
            "per_subject": {s: float(np.mean(v)) for s, v in sorted(per_subj.items())}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ML / "configs" / "e2_g1.toml"))
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--subjects", type=int, nargs="*", default=None)
    ap.add_argument("--seeds", type=int, nargs="*", default=None)
    ap.add_argument("--out", default=None)
    ns = ap.parse_args()
    with open(ns.config, "rb") as f:
        cfg = tomllib.load(f)
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (Path(ns.out) if ns.out else REPO / "analysis" / "ml" / "e2" / f"e2_{run_id}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    keys = record_keys(cfg)
    folds = make_folds(keys)
    sha = splits_doc_sha(keys, folds)
    e1b_split_sha = hashlib.sha256((REPO / cfg["run"]["e1b_splits"]).read_bytes()).hexdigest()
    if sha != e1b_split_sha:
        sys.exit(f"split manifest differs from E1b ({sha[:12]} vs {e1b_split_sha[:12]})")
    subjects = ns.subjects or cfg["data"]["participants"]
    seeds = ns.seeds if ns.seeds is not None else cfg["run"]["seeds"]
    tasks = [{"cfg": {**cfg, "run": {**cfg["run"]}}, "keys": keys, "subject": s, "seeds": seeds,
              "folds": [f for f in folds["P2"] if f.subject == s], "threads": cfg["run"]["threads"],
              "raw": REPO / cfg["data"]["raw_root"], "out_dir": str(out), "splits_sha256": sha,
              "dsp_path": ML / "configs" / "dsp" / f"{cfg['run']['dsp']}.toml"} for s in subjects]
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=ns.workers or cfg["run"]["workers"]) as ex:
        futs = [ex.submit(subject_task, t) for t in tasks]
        for fu in as_completed(futs):
            r = fu.result()
            rows += r["results"]
            print(f"  subject {r['subject']} done ({r['seconds']} s, {time.time() - t0:.0f} s total)", flush=True)
    # reproduction of E1b float results
    e1b = json.loads((REPO / cfg["run"]["e1b_results"]).read_text(encoding="utf-8"))["results"]
    e1b_ba = {(r["subject"], r["fold"], r["seed"]): r["window"]["balanced_accuracy"] for r in e1b
              if r["case"] == "bip4/dc_bp20_450" and r["model"] == "cnn" and r["protocol"] == "P2"}
    rep = [abs(r["v1_torch_float"]["balanced_accuracy"] - e1b_ba[(r["subject"], r["fold"], r["seed"])]) for r in rows
           if (r["subject"], r["fold"], r["seed"]) in e1b_ba]
    summary = {
        "variants": {v: agg_variant(rows, v) for v in VARIANTS},
        "paired": {"v4_int8 - v1_torch_float": paired(rows, "v1_torch_float", "v4_int8"),
                   "v4_int8 - v4b_fixed_dsp_float_cnn": paired(rows, "v4b_fixed_dsp_float_cnn", "v4_int8"),
                   "v4b - v3 (fixed vs float DSP, float CNN)": paired(rows, "v3_int16in_float_dsp", "v4b_fixed_dsp_float_cnn"),
                   "v3 - v2 (int16 input)": paired(rows, "v2_folded_float", "v3_int16in_float_dsp"),
                   "v2 - v1 (folding)": paired(rows, "v1_torch_float", "v2_folded_float")},
        "argmax_agreement_mean": {k: float(np.mean([r["argmax_agree"][k] for r in rows])) for k in rows[0]["argmax_agree"]},
        "e1b_reproduction": {"compared": len(rep), "max_abs_ba_diff": float(max(rep)) if rep else None,
                             "n_identical": int(sum(d == 0 for d in rep))},
        "bit_exact_real_records": {"checked": len(rows), "all_exact": all(r["bit_exact_real_record"] for r in rows)},
        "layers": {L["layer"]: {"sqnr_db_mean": float(np.mean([l["sqnr_db"] for r in rows for l in r["layers"] if l["layer"] == L["layer"]])),
                                "sqnr_db_min": float(np.min([l["sqnr_db"] for r in rows for l in r["layers"] if l["layer"] == L["layer"]])),
                                **({"frac_saturated_255_max": float(np.max([l["frac_saturated_255"] for r in rows for l in r["layers"] if l["layer"] == L["layer"]])),
                                    "frac_saturated_255_mean": float(np.mean([l["frac_saturated_255"] for r in rows for l in r["layers"] if l["layer"] == L["layer"]])),
                                    "max_abs_acc_bits": int(np.max([l["max_abs_acc_bits"] for r in rows for l in r["layers"] if l["layer"] == L["layer"]]))}
                                   if L["layer"] != "logits" else
                                   {"max_abs_logit_bits": int(np.max([l["max_abs_logit_bits"] for r in rows for l in r["layers"] if l["layer"] == "logits"])),
                                    "gap_sum_max": int(np.max([l["gap_sum_max"] for r in rows for l in r["layers"] if l["layer"] == "logits"]))})}
                   for L in rows[0]["layers"]},
        # the DSP runs once per fold (not per seed): count it on the first seed's rows only
        "dsp": {"internal_saturations": int(sum(r["dsp_fixed"]["internal_saturations"] for r in rows if r["seed"] == seeds[0])),
                "output_saturations": int(sum(r["dsp_fixed"]["output_saturations"] for r in rows if r["seed"] == seeds[0])),
                "max_abs_acc_bits": int(max(max(r["dsp_fixed"]["max_abs_acc_bits"]) for r in rows)),
                "fixed_vs_float_rms_err_lsb_mean": float(np.mean([r["dsp_fixed_vs_float"]["rms_err_lsb"] for r in rows])),
                "fixed_vs_float_max_err_lsb": float(np.max([r["dsp_fixed_vs_float"]["max_err_lsb"] for r in rows])),
                "fixed_vs_float_sqnr_db_min": float(np.min([r["dsp_fixed_vs_float"]["sqnr_db"] for r in rows]))},
        "input": {"lsb_mV_range": [float(min(r["lsb_mV"] for r in rows)), float(max(r["lsb_mV"] for r in rows))],
                  "test_input_saturations": int(sum(r["input_saturations"]["test"] for r in rows if r["seed"] == seeds[0])),
                  "train_input_saturations": int(sum(r["input_saturations"]["train"] for r in rows if r["seed"] == seeds[0]))},
        "postproc": {f"K{p['K']}_T{'0' if p['T'] == 0 else 'train'}": None for p in rows[0]["postproc"]},
    }
    pp = {}
    for r in rows:
        for p in r["postproc"]:
            k = f"K{p['K']}_T{'0' if p['T'] == 0 else 'train'}"
            pp.setdefault(k, []).append(p)
    summary["postproc"] = {k: {"balanced_accuracy_no_decision_wrong": float(np.mean([p["balanced_accuracy_no_decision_wrong"] for p in v])),
                               "accuracy_of_decided": float(np.mean([p["accuracy_of_decided"] for p in v if p["accuracy_of_decided"] is not None])),
                               "frac_no_decision": float(np.mean([p["frac_no_decision"] for p in v])),
                               "first_correct_window_mean": float(np.mean([p["first_correct_window_mean"] for p in v if p["first_correct_window_mean"] is not None])),
                               "records_never_correct": int(sum(p["records_never_correct"] for p in v))} for k, v in pp.items()}
    tr = [r["transition_synthetic"] for r in rows if r["transition_synthetic"]]
    summary["transition_synthetic"] = {K: {"decisions_after_boundary_mean": float(np.mean([t[K]["decisions_after_boundary_mean"] for t in tr if t[K]["decisions_after_boundary_mean"] is not None])),
                                           "never": int(sum(t[K]["never"] for t in tr)), "n": int(sum(t[K]["n"] for t in tr))}
                                       for K in ("K1", "K3", "K5")}
    summary["T_train_range"] = [int(min(r["T_train"] for r in rows)), int(max(r["T_train"] for r in rows))]
    env = {"python": platform.python_version(), "numpy": np.__version__, "reference_version": REF_VERSION,
           "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()}
    res = {"run_id": run_id, "config": cfg, "seeds": seeds, "subjects": subjects, "splits_sha256": sha,
           "environment": env, "wall_seconds": round(time.time() - t0, 1), "summary": summary}
    (out / "summary.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    (out / "results.json").write_text(json.dumps(rows, default=float), encoding="utf-8")
    (out / "config.toml").write_bytes(Path(ns.config).read_bytes())
    print(json.dumps({k: (v["balanced_accuracy_mean"], v["balanced_accuracy_std_subjects"]) for k, v in summary["variants"].items()}, indent=1))
    print(json.dumps(summary["paired"]["v4_int8 - v1_torch_float"], indent=1))
    print("e1b reproduction", summary["e1b_reproduction"], "bit exact", summary["bit_exact_real_records"])
    print(f"wall {res['wall_seconds']} s -> {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
