#!/usr/bin/env python3
"""INT8 accuracy of a train_spi_levels.py model on a recording it has never seen (another electrode placement).

    python ml/scripts/eval_spi_levels.py --model analysis/spi/model --rec analysis/spi/rec3.npz

Same path as the hardware. Windows inside one label piece, after SETTLE samples, as in training. Per round, so one bad
round stays visible. Also reports where each result falls when RMS thresholds alone are used, as a baseline for the CNN.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "ml"), str(REPO / "ml" / "scripts"), str(REPO / "scripts" / "sim")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--rec", nargs="+", required=True)
    ns = ap.parse_args()

    from gen_spi_live import SEC_MASK, dsp_slots
    from reference.emg_fixed.cnn.int_model import run_record_int
    from reference.emg_fixed.cnn.quantize import QLayer, QuantCNN
    from reference.emg_fixed.dsp.fixed_dsp import FixedDSP
    from train_spi_levels import NAMES, SETTLE, STEP, load, pieces, to_codes

    model = Path(ns.model)
    a = np.load(model / "levels_int8.npz")
    meta = json.loads((model / "levels.json").read_text(encoding="utf-8"))
    d = meta["describe"]
    layers = [QLayer(wq=a[f"conv{i + 1}_wq"].astype(np.int64), bq=a[f"conv{i + 1}_bq"].astype(np.int64),
                     M=a[f"conv{i + 1}_M"].astype(np.int64), s=a[f"conv{i + 1}_s"].astype(np.int64),
                     stride=int(a[f"conv{i + 1}_stride"]), s_in=ld["s_in"], s_out=ld["s_out"])
              for i, ld in enumerate(d["layers"])]
    q = QuantCNN(layers=layers, fc_wq=a["fc_wq"].astype(np.int64), fc_bq=a["fc_bq"].astype(np.int64), s_fc=d["fc"]["s_fc"],
                 M_gap=d["M_gap"], D=d["D"], RF=d["RF"], window=d["window"], step=d["step"], settle=d["settle"], meta={})
    med = [meta["level"]["median_rms"][n] for n in NAMES]
    t01, t12 = (med[0] + med[1]) / 2, (med[1] + med[2]) / 2

    slots = dsp_slots()
    secs = [slots[i] for i in range(4) if SEC_MASK >> i & 1]
    for path, rec in zip(ns.rec, load(ns.rec)):
        post = FixedDSP(secs, 4).process(to_codes(rec["u16"]), sos=True)
        conf, thr = np.zeros((3, 6), dtype=int), np.zeros((3, 3), dtype=int)
        by_round: dict[int, np.ndarray] = {}
        for p in pieces(rec, 0, post):
            am = run_record_int(q, p["x"], keep=False)["argmax"][SETTLE // STEP:]
            r = p["round"][1]
            br = by_round.setdefault(r, np.zeros(3))
            for v in am:
                conf[p["label"], v] += 1
            br[p["label"]] = float(np.mean(am == p["label"])) if len(am) else float("nan")
            y = p["x"][SETTLE:, 0].astype(np.float64)
            for k in range(0, len(y) - 399, 100):
                rms = float(np.sqrt(np.mean(y[k:k + 400] ** 2)))
                thr[p["label"], int(rms >= t01) + int(rms >= t12)] += 1
        recall = [float(conf[c, c] / max(1, conf[c].sum())) for c in range(3)]
        trec = [float(thr[c, c] / max(1, thr[c].sum())) for c in range(3)]
        print(json.dumps({"rec": path, "windows": int(conf.sum()), "cnn_recall": dict(zip(NAMES, map(lambda v: round(v, 4), recall))),
                          "cnn_balanced": round(float(np.mean(recall)), 4), "cnn_confusion": conf.tolist(),
                          "rms_threshold_balanced": round(float(np.mean(trec)), 4)}))
        for r, br in sorted(by_round.items()):
            print(f"  round {r}: " + " ".join(f"{n} {v:.3f}" for n, v in zip(NAMES, br)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
