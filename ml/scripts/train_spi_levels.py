#!/usr/bin/env python3
"""Train the single-channel contraction level model for the live SPI path (docs/system/stm32_spi_live.md).

    python ml/scripts/train_spi_levels.py --rec analysis/spi/rec1.npz [more.npz] --out analysis/spi/model
    python ml/scripts/train_spi_levels.py --synthetic --out build/spi/model_synth     # exercises the flow only

Input: recordings of host/spi_live/capture.py: u16 [T] (ADC codes at 2 kHz), label [T] (0 REST, 1 LIGHT, 2 STRONG,
-1 transition), round [T]. Same path as the hardware: code -> int16 (2048, << 3) -> FixedDSP (DC + 50 Hz notch + band-pass, gen_spi_live.SEC_MASK)
-> CausalCNN on lane 0 (lanes 1..3 are 0) -> INT8 reference model.
The CNN keeps its 6 outputs so that RTL, firmware and mailbox do not change: classes 3..5 are simply never trained.

Split by round, never inside one: the last round is the test set and the one before it the validation set. Calibration
of the quantisation uses training rounds only. Accuracy is reported for the INT8 reference model on the test round and
applies to this person, this electrode placement and this session only.

Output: <out>/*.hex for scripts/sim/gen_spi_live.py --model, <out>/levels.json (activity calibration for host/spi_live,
metrics). Samples stay out of git: use analysis/ or build/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "ml"), str(REPO / "scripts" / "sim")]

NAMES = ["REST", "LIGHT", "STRONG"]
LSB = 1.0 / 16384.0            # int16 16384 = 1.0 in the float model; the scale only has to be consistent
WINDOW, STEP = 400, 100
SETTLE = 400                   # samples dropped after each contiguous piece: filters and the receptive field


def synthetic(rounds: int = 6, hold_s: int = 10, seed: int = 0) -> dict:
    """band-limited noise of three amplitudes: proves the flow, says nothing about EMG"""
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(seed)
    sos = butter(4, [20, 450], "bandpass", fs=2000, output="sos")
    amp = [12.0, 120.0, 500.0]
    xs, ls, rs = [], [], []
    for r in range(rounds):
        for c in range(3):
            n = hold_s * 2000
            x = sosfilt(sos, rng.standard_normal(n)) * amp[c] * (1 + 0.2 * rng.standard_normal())
            xs.append(np.clip(np.round(2048 + 30 * np.sin(np.arange(n) / 2000 * 2 * np.pi * 50) + x), 0, 4095))
            lab = np.full(n, c, dtype=np.int8)
            lab[:2000] = -1
            for _ in range(rng.integers(0, 3)):      # lost blocks, as capture.py marks them
                at = int(rng.integers(2000, n - 128))
                lab[at:at + 128] = -1
            ls.append(lab)
            rs.append(np.full(n, r, dtype=np.int16))
    return {"u16": np.concatenate(xs).astype(np.uint16), "label": np.concatenate(ls), "round": np.concatenate(rs)}


def load(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        z = np.load(p)
        out.append({k: z[k] for k in ("u16", "label", "round")})
    return out


def to_codes(u16: np.ndarray) -> np.ndarray:
    q = np.clip((u16.astype(np.int64) - 2048) * 8, -32768, 32767).astype(np.int16)
    x = np.zeros((len(q), 4), dtype=np.int16)
    x[:, 0] = q
    return x


def pieces(rec: dict, rec_no: int, post: np.ndarray) -> list[dict]:
    """contiguous pieces of one label (one record each for the trainer)"""
    lab, rnd = rec["label"], rec["round"]
    change = np.flatnonzero(np.diff(lab.astype(np.int32)) != 0) + 1
    res = []
    for a, b in zip(np.r_[0, change], np.r_[change, len(lab)]):
        c = int(lab[a])
        if c < 0 or b - a < SETTLE + WINDOW:
            continue
        res.append({"x": post[a:b], "label": c, "round": (rec_no, int(rnd[a]))})
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rec", nargs="*", default=[])
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--epochs", type=int, default=40)
    ns = ap.parse_args()
    if bool(ns.rec) == ns.synthetic:
        ap.error("give --rec or --synthetic")

    import torch
    from emgml.models.cnn import CNNSpec, fold_cnn, train_cnn
    from gen_spi_live import SEC_MASK, dsp_slots
    from reference.emg_fixed import vectors
    from reference.emg_fixed.cnn.int_model import run_record_int
    from reference.emg_fixed.cnn.quantize import calibrate, describe, quantise, to_arrays
    from reference.emg_fixed.dsp.fixed_dsp import FixedDSP

    slots = dsp_slots()
    secs = [slots[i] for i in range(4) if SEC_MASK >> i & 1]
    recs = [synthetic(seed=ns.seed)] if ns.synthetic else load(ns.rec)
    ps = []
    for i, r in enumerate(recs):
        post = FixedDSP(secs, 4).process(to_codes(r["u16"]), sos=True)
        ps += pieces(r, i, post)
    rounds = sorted({p["round"] for p in ps})
    if len(rounds) < 3:
        sys.exit(f"need at least 3 rounds, got {len(rounds)}")
    test_r, val_r = rounds[-1], rounds[-2]
    tr = [p for p in ps if p["round"] not in (test_r, val_r)]
    va = [p for p in ps if p["round"] == val_r]
    te = [p for p in ps if p["round"] == test_r]
    print(f"pieces: train {len(tr)}, validation {len(va)}, test {len(te)}; rounds {len(rounds)}")

    # activity of lane 0 as the PL reports it, training rounds only
    def rms128(x):
        n = (len(x) - SETTLE) // 128
        y = x[SETTLE:SETTLE + n * 128, 0].astype(np.float64)
        return np.sqrt(np.mean(y.reshape(n, 128) ** 2, axis=1))
    by = {c: np.concatenate([rms128(p["x"]) for p in tr if p["label"] == c]) for c in range(3)}
    missing = [NAMES[c] for c in range(3) if len(by[c]) == 0]
    if missing:
        sys.exit(f"no training data for {missing}")
    level = {"rest_rms": float(np.percentile(by[0], 90)), "max_rms": float(np.percentile(by[2], 90)),
             "median_rms": {NAMES[c]: float(np.median(by[c])) for c in range(3)}}

    # windows: pieces differ in length (a lost block ends one), so pad to [R, C, T] and index only the real windows
    T = max(len(p["x"]) for p in ps)
    all_p = tr + va + te
    X = np.zeros((len(all_p), 4, T), dtype=np.float32)
    for i, p in enumerate(all_p):
        X[i, :, :len(p["x"])] = p["x"].T.astype(np.float32) * LSB
    X = torch.from_numpy(X)
    U = X.unfold(2, WINDOW, STEP)
    k0 = SETTLE // STEP
    rec_l, k_l = [], []
    for i, p in enumerate(all_p):
        Ks = np.arange(k0, (len(p["x"]) - WINDOW) // STEP + 1)
        rec_l.append(np.full(len(Ks), i))
        k_l.append(Ks)
    rec_all, k_all = np.concatenate(rec_l), np.concatenate(k_l)
    y_all = np.array([all_p[r]["label"] for r in rec_all])
    nt, nv = len(tr), len(va)
    fit, val = rec_all < nt, (rec_all >= nt) & (rec_all < nt + nv)
    std = np.concatenate([p["x"].astype(np.float64) * LSB for p in tr]).std(axis=0)
    scale = 1.0 / np.where(std > 0, std, 1.0)
    spec = CNNSpec(in_ch=4, n_classes=6, window=WINDOW, step=STEP, settle=0, fs=2000.0)
    torch.set_num_threads(4)
    res = train_cnn(U, rec_all[fit], k_all[fit], y_all[fit], rec_all[val], k_all[val], y_all[val], scale, spec, ns.seed,
                    max_epochs=ns.epochs)
    print(f"best epoch {res.best_epoch}, validation balanced accuracy {res.history[res.best_epoch]['val_bal_acc']:.3f}")

    f = fold_cnn(res.model, res.scale)
    calib = calibrate(f, [p["x"] for p in tr], LSB)
    q = quantise(f, LSB, calib, settle=0)
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    vectors.export_model(out, q, secs)
    np.savez(out / "levels_int8.npz", **to_arrays(q))

    # INT8 reference on the held-out round, class by class
    conf = np.zeros((3, 6), dtype=int)
    for p in te:
        lg = run_record_int(q, p["x"], keep=False)["logits"]
        am = np.argmax(lg, axis=1)[(SETTLE // STEP):]
        for a in am:
            conf[p["label"], a] += 1
    recall = [float(conf[c, c] / max(1, conf[c].sum())) for c in range(3)]
    metrics = {"data": "SYNTHETIC NOISE, flow check only" if ns.synthetic else [str(p) for p in ns.rec],
               "rounds": len(rounds), "windows": int(len(rec_all)), "seed": ns.seed, "best_epoch": res.best_epoch,
               "val_bal_acc": res.history[res.best_epoch]["val_bal_acc"],
               "test_recall_int8": dict(zip(NAMES, recall)), "test_bal_acc_int8": float(np.mean(recall)),
               "test_confusion_int8": conf.tolist(), "level": level, "class_order": NAMES, "describe": describe(q)}
    (out / "levels.json").write_text(json.dumps(metrics, indent=1, default=float), encoding="utf-8")
    print(f"INT8 test recall {dict(zip(NAMES, [round(r, 3) for r in recall]))}; balanced {np.mean(recall):.3f}")
    print(f"activity: rest {level['rest_rms']:.1f}, max {level['max_rms']:.1f} LSB; written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
