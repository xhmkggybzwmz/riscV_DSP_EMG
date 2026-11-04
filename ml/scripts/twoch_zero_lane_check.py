"""Two sensors on the deployed 4-lane hardware: is a 2-channel model the same as the 4-lane model with
lanes 2 and 3 held at zero, on the bit-exact fixed-point reference (fixed DSP + INT8 CNN, E2/E3)?

    python ml/scripts/twoch_zero_lane_check.py [--channels bip2_15] [--config ml/configs/twoch_g1.toml]

Reference fold of E2 (subject 1, P2, test session 3, seed 0), retrained on the two-channel input:
  - ESI LSB from the fold's training raw samples of the two active lanes only
  - fixed DSP on [lane0, lane1, 0, 0]: the zero lanes must stay exactly zero
  - the folded float model padded with zero conv1 weights for lanes 2-3, calibrated and quantised exactly
    as E2 does; INT8 weights, requant parameters and every test logit compared with the 2-lane model
  - test accuracy of float (PyTorch) vs fixed DSP + INT8 CNN for this fold (hardware schedule, settle 0)
Writes analysis/ml/twoch/zero_lane_<channels>.json (git-ignored). Exit 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tomllib
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path[:0] = [str(ML), str(REPO / "reference")]

from emg_fixed.cnn.int_model import run_record_int  # noqa: E402
from emg_fixed.cnn.quantize import calibrate, quantise, to_arrays  # noqa: E402
from emg_fixed.dsp.fixed_dsp import FixedDSP, sections_from_config  # noqa: E402
from emg_fixed.esi import SourceCalibration, grabmyo_lsb, to_esi  # noqa: E402
from emgml import labels  # noqa: E402
from emgml.datasets import grabmyo  # noqa: E402
from emgml.dsp import DSPConfig  # noqa: E402
from emgml.experiment import _mask, build_subject, make_folds, record_keys, run_cnn  # noqa: E402
from emgml.metrics import summarize  # noqa: E402
from emgml.models.cnn import CNNSpec, fold_cnn  # noqa: E402

LABELS = sorted(labels.UNIFIED)
N_HW = 4                                           # lanes of the built DSP / CNN (rtl N_CH = 4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channels", default="bip2_15")
    ap.add_argument("--config", default=str(ML / "configs" / "twoch_g1.toml"))
    ap.add_argument("--subject", type=int, default=1)
    ap.add_argument("--fold", default="test_s3")
    ap.add_argument("--seed", type=int, default=0)
    ns = ap.parse_args()
    with open(ns.config, "rb") as f:
        cfg = tomllib.load(f)
    d = cfg["data"]
    keys = record_keys(cfg)
    fold = next(f for f in make_folds(keys)["P2"] if f.subject == ns.subject and f.name == ns.fold)
    gidx = [i for i, k in enumerate(keys) if k[1] == ns.subject]
    inv = {v: k for k, v in grabmyo.GRABMYO_TO_UNIFIED.items()}
    recs = [grabmyo.load_trial(REPO / d["raw_root"], keys[i][2], ns.subject, inv[keys[i][4]], keys[i][3], ns.channels,
                               d["fs_out"]) for i in gidx]
    n_ch = recs[0].n_ch
    dsp = DSPConfig.load(ML / "configs" / "dsp" / "dc_bp20_450.toml")
    dsp.n_ch, dsp.fs = n_ch, recs[0].fs
    spec = CNNSpec(in_ch=n_ch, layers=[tuple(l) for l in cfg["cnn"]["layers"]], n_classes=len(LABELS), window=400,
                   step=100, settle=200, fs=dsp.fs)
    sd = build_subject(recs, gidx, dsp)
    pos_of = {g: p for p, g in enumerate(gidx)}
    tr_pos, te_pos = [pos_of[i] for i in fold.train], [pos_of[i] for i in fold.test]
    checks, fails = [], 0

    def check(name, ok, detail=""):
        nonlocal fails
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}  {detail}")

    # ESI input: LSB from the training raw samples of the active lanes only
    lsb = grabmyo_lsb(np.concatenate([recs[p].x for p in tr_pos]))
    cal = SourceCalibration(f"GRABMyo-1.1.0 replay ({ns.channels})", "PRE_DSP", "mV", lsb, n_ch, recs[0].channel_names,
                            rule="P99.99(|x|) of training raw samples -> code 16384",
                            calibration_records=[list(keys[i]) for i in fold.train])
    codes = [to_esi(r.x, cal)[0] for r in recs]
    secs = sections_from_config(dsp)
    pad = lambda c: np.concatenate([c, np.zeros((c.shape[0], N_HW - n_ch), dtype=c.dtype)], axis=1)
    post2, post4 = [], []
    for c in codes:
        post2.append(FixedDSP(secs, n_ch).process(c, sos=True))
        post4.append(FixedDSP(secs, N_HW).process(pad(c), sos=True))
    check(f"fixed DSP: lanes 0..{n_ch - 1} of the {N_HW}-lane run == the {n_ch}-lane run, every record",
          all(np.array_equal(a, b[:, :n_ch]) for a, b in zip(post2, post4)), f"{len(recs)} records")
    check(f"fixed DSP: zero input lanes {n_ch}..{N_HW - 1} stay exactly zero",
          all(not np.any(b[:, n_ch:]) for b in post4))

    # train the two-channel CNN (E1b settings), fold, pad conv1 with zero weights
    r_cnn, res = run_cnn(sd, keys, fold, ns.seed, cfg["cnn"], spec, cfg["run"]["vote_k"], dsp)
    f2 = fold_cnn(res.model, res.scale)
    f4 = copy.deepcopy(f2)
    W = f4["convs"][0]["W"]
    f4["convs"][0]["W"] = np.concatenate([W, np.zeros((W.shape[0], N_HW - n_ch, W.shape[2]))], axis=1)
    cal2 = calibrate(f2, [post2[p] for p in tr_pos], lsb)
    cal4 = calibrate(f4, [post4[p] for p in tr_pos], lsb)
    check("calibration ranges identical", [l["p"] for l in cal2["layers"]] == [l["p"] for l in cal4["layers"]],
          [round(l["p"], 6) for l in cal2["layers"]])
    q2, q4 = quantise(f2, lsb, cal2, settle=0), quantise(f4, lsb, cal4, settle=0)
    a2, a4 = to_arrays(q2), to_arrays(q4)
    w1_2, w1_4 = q2.layers[0].wq, q4.layers[0].wq
    same_rest = all(np.array_equal(a2[k], a4[k]) for k in a2 if not k.startswith("conv1_w") and k in a4)
    check(f"INT8 conv1 weights: lanes 0..{n_ch - 1} identical, lanes {n_ch}..{N_HW - 1} all zero; all other "
          "arrays (biases, M, s, conv2/3, FC) identical",
          np.array_equal(w1_4[:, :n_ch], w1_2) and not np.any(w1_4[:, n_ch:]) and same_rest
          and sorted(a2) == sorted(a4), f"{len(a2)} arrays")
    r2 = {p: run_record_int(q2, post2[p]) for p in te_pos}
    r4 = {p: run_record_int(q4, post4[p]) for p in te_pos}
    check("INT8 CNN: every test window logit and decision bit-identical (2-lane model vs 4-lane zero-padded)",
          all(np.array_equal(r2[p]["logits"], r4[p]["logits"]) and np.array_equal(r2[p]["ends"], r4[p]["ends"])
              for p in te_pos), f"{sum(len(r2[p]['ends']) for p in te_pos)} windows, {len(te_pos)} records")

    # accuracy of this fold: float (E1b pipeline, settle 200) and fixed + INT8 (hardware schedule, settle 0)
    me = _mask(sd, fold.test)
    y_int = np.concatenate([np.full(len(r4[p]["argmax"]), recs[p].label) for p in te_pos])
    p_int = np.concatenate([r4[p]["argmax"] for p in te_pos])
    out = {"channels": ns.channels, "lanes": recs[0].channel_names, "subject": ns.subject, "fold": ns.fold,
           "seed": ns.seed, "esi_lsb_mV": lsb, "checks": checks,
           "float_balanced_accuracy": r_cnn["window"]["balanced_accuracy"], "float_windows": int(me.sum()),
           "int8_hw_schedule": summarize(y_int, p_int, LABELS)}
    print(f"fold accuracy: float {out['float_balanced_accuracy']:.4f} ({out['float_windows']} windows, settle 200); "
          f"fixed DSP + INT8 {out['int8_hw_schedule']['balanced_accuracy']:.4f} ({len(y_int)} windows, settle 0)")
    dst = REPO / "analysis" / "ml" / "twoch" / f"zero_lane_{ns.channels}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, indent=1, default=float), encoding="utf-8")
    print(f"{len(checks) - fails}/{len(checks)} checks -> {dst.relative_to(REPO)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
