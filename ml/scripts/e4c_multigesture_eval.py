"""E4c-A: what the deployed E2 model does on the six classes, and which real segments can drive a
multi-gesture hardware replay.

    python ml/scripts/e4c_multigesture_eval.py [--out build/e4c] [--seg-len 600]

Model: the frozen E2 reference fold (subject 1, P2: trained on sessions 1+2, tested on session 3,
seed 0), INT8 arrays + the fold's training-derived ESI LSB, settle = 0 (first decision at sample
399 after SOS, then every 100). Nothing is retrained or requantised.

Data: the fold's independent test records, GRABMyo 1.1.0 session 3, participant 1, the six mapped
gestures (17 REST, 16 HAND_CLOSE, 15 HAND_OPEN, 12 WRIST_FLEX, 11 WRIST_EXT, 4 PINCH), trials 1..7,
bip4 (F1-F9, F3-F11, F5-F13, F7-F15), resampled 2048 -> 2000 Hz.

1. Whole-record evaluation (the honest number): every window of every test record, E2 fixed-point
   (fixed DSP + INT8 CNN) and the folded float model on the float DSP, per class and balanced.
2. Segment candidates for the replay: a segment = L frames of one record starting with SOS (the DSP
   and CNN start from zero state, as after every SOS on the hardware), decisions at 399, 499, ...
   For every start offset (step 100) the fixed-point decisions are computed; a candidate is "usable"
   when all its decisions are the record's true class. Per class one segment is SELECTED by a fixed
   rule (largest minimum top1-top2 margin; ties: lowest trial, then lowest offset). These are curated
   demonstration samples, not an accuracy claim.
Writes <out>/e4c_eval.json and <out>/mg6_selection.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path[:0] = [str(ML), str(REPO / "reference")]

from emg_fixed.cnn.int_model import run_record_int  # noqa: E402
from emg_fixed.cnn.quantize import float_record, from_saved  # noqa: E402
from emg_fixed.dsp.fixed_dsp import FixedDSP, sections_from_config  # noqa: E402
from emg_fixed.esi import SourceCalibration, to_esi  # noqa: E402
from emgml.datasets import grabmyo  # noqa: E402
from emgml.dsp import DSPConfig, EMGPreprocessPipeline  # noqa: E402
from emgml.labels import GRABMYO_TO_UNIFIED, UNIFIED  # noqa: E402

RAW = REPO / "data" / "public" / "grabmyo" / "raw"
RUN = REPO / "analysis" / "ml" / "e2" / "e2_20261022_023953" / "models"
STEM = "s1_test_s3_seed0"
CLASS_GESTURE = {c: g for g, c in GRABMYO_TO_UNIFIED.items()}          # unified class -> GRABMyo gesture
SESSION, PARTICIPANT, TRIALS = 3, 1, range(1, 8)


def load_model():
    q = from_saved(RUN / f"{STEM}_int8.npz", RUN / f"{STEM}_int8.json")
    q.settle = 0
    meta = json.loads((RUN / f"{STEM}_int8.json").read_text(encoding="utf-8"))
    return q, SourceCalibration(**meta["esi"]), meta


def load_float():
    """folded float model of the same fold / seed (float64, E2 variant v2)"""
    import torch
    from emgml.models.cnn import CausalCNN, CNNSpec, fold_cnn
    z = np.load(RUN / f"{STEM}_float.npz")
    spec = CNNSpec(in_ch=4, layers=[(16, 8, 2), (32, 7, 2), (32, 6, 5)], n_classes=6, window=400, step=100,
                   settle=0, fs=2000.0)
    m = CausalCNN(spec)
    m.load_state_dict({k: torch.from_numpy(z[k]) for k in z.files if k != "channel_scale"})
    m.eval()
    return fold_cnn(m, z["channel_scale"])


def margin_top2(l: np.ndarray) -> np.ndarray:
    s = np.sort(l, axis=1)
    return (s[:, -1] - s[:, -2]).astype(np.int64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "build" / "e4c"))
    ap.add_argument("--seg-len", type=int, default=600)
    ns = ap.parse_args()
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    q, cal, meta = load_model()
    f = load_float()
    dsp_cfg = DSPConfig.load(ML / "configs" / "dsp" / "dc_bp20_450.toml")
    dsp_cfg.n_ch, dsp_cfg.fs = 4, 2000.0
    secs = sections_from_config(dsp_cfg)
    L = ns.seg_len
    starts = list(range(0, 10000 - L + 1, 100))
    rep = {"model": {"fold": meta["meta"]["fold"], "subject": meta["meta"]["subject"], "seed": meta["meta"]["seed"],
                     "int8_npz_sha256": hashlib.sha256((RUN / f"{STEM}_int8.npz").read_bytes()).hexdigest(),
                     "esi_lsb_mV": cal.lsb, "settle": 0, "training_sessions": [1, 2], "test_session": SESSION},
           "records": [], "classes": {}, "segments": {"length": L, "starts": starts}}
    all_y, all_fx, all_fl = [], [], []
    cand = {}
    for c in range(6):
        g = CLASS_GESTURE[c]
        cand[c] = []
        for t in TRIALS:
            r = grabmyo.load_trial(RAW, SESSION, PARTICIPANT, g, t, "bip4", 2000.0)
            codes, nsat = to_esi(r.x, cal)
            # whole record, SOS at sample 0: E2 fixed point and folded float
            y = FixedDSP(secs, 4).process(codes, sos=True)
            rr = run_record_int(q, y)
            wave = EMGPreprocessPipeline(dsp_cfg).run_record(r.x).waveform
            fr = float_record(f, wave, settle=0)
            n = min(len(rr["argmax"]), len(fr["logits"]))
            fx, fl = rr["argmax"][:n], np.argmax(fr["logits"][:n], axis=1)
            all_y.append(np.full(n, c))
            all_fx.append(fx)
            all_fl.append(fl)
            rep["records"].append({"class": UNIFIED[c], "gesture": g, "trial": t, "source": r.source_path,
                                   "sha256": r.sha256, "samples": int(codes.shape[0]), "input_saturations": int(nsat),
                                   "windows": int(n), "fixed_correct": int((fx == c).sum()),
                                   "float_correct": int((fl == c).sum()), "fixed_float_agree": int((fx == fl).sum()),
                                   "fixed_pred_hist": np.bincount(fx, minlength=6).tolist()})
            # segment candidates: SOS at each start, all segments of the record as DSP lanes
            X = np.concatenate([codes[s:s + L] for s in starts], axis=1)
            Y = FixedDSP(secs, X.shape[1]).process(X, sos=True)
            for i, s in enumerate(starts):
                sr = run_record_int(q, Y[:, 4 * i:4 * i + 4])
                lg = np.asarray(sr["logits"])
                am = np.asarray(sr["argmax"])
                cand[c].append({"trial": t, "start": s, "argmax": am.tolist(), "ends": np.asarray(sr["ends"]).tolist(),
                                "correct": int((am == c).sum()), "n": int(len(am)),
                                "min_margin_if_correct": int(margin_top2(lg).min()) if (am == c).all() else None})
        recs = [x for x in rep["records"] if x["class"] == UNIFIED[c]]
        nw = sum(x["windows"] for x in recs)
        cs = cand[c]
        usable = [x for x in cs if x["correct"] == x["n"]]
        first_ok = sum(x["argmax"][0] == c for x in cs)
        rep["classes"][UNIFIED[c]] = {
            "gesture": g, "records": len(recs), "windows": nw,
            "fixed_recall": round(sum(x["fixed_correct"] for x in recs) / nw, 4),
            "float_recall": round(sum(x["float_correct"] for x in recs) / nw, 4),
            "fixed_float_agree": round(sum(x["fixed_float_agree"] for x in recs) / nw, 4),
            "fixed_pred_hist": np.sum([x["fixed_pred_hist"] for x in recs], axis=0).tolist(),
            "segment_candidates": len(cs), "segments_all_correct": len(usable),
            "segments_first_decision_correct": int(first_ok),
            "trials_with_usable_segment": sorted({x["trial"] for x in usable})}
        print(f"{UNIFIED[c]:11s} g{g:2d}: windows {nw}, fixed recall {rep['classes'][UNIFIED[c]]['fixed_recall']:.3f}, "
              f"float {rep['classes'][UNIFIED[c]]['float_recall']:.3f}; segments all-correct {len(usable)}/{len(cs)} "
              f"(first decision ok {first_ok}), trials {rep['classes'][UNIFIED[c]]['trials_with_usable_segment']}",
              flush=True)
    yy, fx, fl = np.concatenate(all_y), np.concatenate(all_fx), np.concatenate(all_fl)
    ba = lambda p: float(np.mean([np.mean(p[yy == c] == c) for c in range(6)]))   # noqa: E731
    conf = np.zeros((6, 6), dtype=int)
    for a, b in zip(yy, fx):
        conf[a, b] += 1
    rep["overall"] = {"windows": int(len(yy)), "fixed_balanced_accuracy": round(ba(fx), 4),
                      "float_balanced_accuracy": round(ba(fl), 4), "fixed_float_agree": round(float(np.mean(fx == fl)), 4),
                      "fixed_confusion_rows_true_cols_pred": conf.tolist()}
    print(f"overall: {len(yy)} windows, balanced accuracy fixed {ba(fx):.4f} float {ba(fl):.4f}, agree {np.mean(fx == fl):.4f}")
    # selection: largest minimum margin among all-correct candidates (ties: trial, start)
    sel = []
    for c in range(6):
        usable = [x for x in cand[c] if x["correct"] == x["n"]]
        if not usable:
            sel.append({"class": c, "label": UNIFIED[c], "gesture": CLASS_GESTURE[c], "selected": None})
            continue
        best = sorted(usable, key=lambda x: (-x["min_margin_if_correct"], x["trial"], x["start"]))[0]
        sel.append({"class": c, "label": UNIFIED[c], "gesture": CLASS_GESTURE[c], "session": SESSION,
                    "participant": PARTICIPANT, "trial": best["trial"], "start": best["start"], "length": L,
                    "decisions": best["n"], "min_margin": best["min_margin_if_correct"],
                    "rule": "all decisions correct; max of min(top1 - top2); ties: lowest trial, lowest start",
                    "curated": True})
    rep["selection"] = sel
    rep["candidates"] = {UNIFIED[c]: cand[c] for c in range(6)}
    (out / "e4c_eval.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    (out / "mg6_selection.json").write_text(json.dumps({"model": rep["model"], "segment_length": L, "selection": sel},
                                                       indent=1), encoding="utf-8")
    for s in sel:
        print("SELECT", json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
