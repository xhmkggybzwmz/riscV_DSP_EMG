#!/usr/bin/env python3
"""Compare the results of a ps_e6a board run on the STM32 replay with the fixed-point reference, bit by bit.

    python scripts/fpga/check_spi_replay.py <results.jsonl> --model analysis/spi/model

The STM32 loops build/stm32/replay_data/replay.npz, and the ESI sequence number is the STM32 sample index, so every
frame the board processed is known. Reference: code -> int16 -> FixedDSP -> run_record_int from the first frame after
SOS. Each result must have the same scores. Accuracy is counted on samples with one label in the whole 400-frame
window; the replay is held-out data of one person and one session.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl")
    ap.add_argument("--model", required=True)
    ap.add_argument("--replay", default=str(REPO / "build" / "stm32" / "replay_data" / "replay.npz"))
    ns = ap.parse_args()

    from gen_spi_live import SEC_MASK, dsp_slots
    from reference.emg_fixed.cnn.int_model import run_record_int
    from reference.emg_fixed.cnn.quantize import QLayer, QuantCNN
    from reference.emg_fixed.dsp.fixed_dsp import FixedDSP

    res = [json.loads(line) for line in open(ns.jsonl, encoding="utf-8")]
    res = [r for r in res if r.get("type") == "result"]
    if not res:
        sys.exit("no results")
    z = np.load(ns.replay)
    u, label = z["u16"], z["label"]
    n = len(u)

    model = Path(ns.model)
    a = np.load(model / "levels_int8.npz")
    d = json.loads((model / "levels.json").read_text(encoding="utf-8"))["describe"]
    layers = [QLayer(wq=a[f"conv{i + 1}_wq"].astype(np.int64), bq=a[f"conv{i + 1}_bq"].astype(np.int64),
                     M=a[f"conv{i + 1}_M"].astype(np.int64), s=a[f"conv{i + 1}_s"].astype(np.int64),
                     stride=int(a[f"conv{i + 1}_stride"]), s_in=ld["s_in"], s_out=ld["s_out"])
              for i, ld in enumerate(d["layers"])]
    q = QuantCNN(layers=layers, fc_wq=a["fc_wq"].astype(np.int64), fc_bq=a["fc_bq"].astype(np.int64), s_fc=d["fc"]["s_fc"],
                 M_gap=d["M_gap"], D=d["D"], RF=d["RF"], window=d["window"], step=d["step"], settle=d["settle"], meta={})

    # one segment per reset_epoch: the sequence number of its first frame gives the place in the loop
    slots = dsp_slots()
    secs = [slots[i] for i in range(4) if SEC_MASK >> i & 1]
    bad = compared = 0
    conf = np.zeros((3, 6), dtype=int)
    for epoch in sorted({r["reset_epoch"] for r in res}):
        rs = [r for r in res if r["reset_epoch"] == epoch]
        s0 = rs[0]["window_end_seq"] - rs[0]["esi_timestamp"]
        last = rs[-1]["esi_timestamp"] + 1
        idx = (s0 + np.arange(last)) % n
        c = np.clip((u[idx].astype(np.int64) - 2048) * 8, -32768, 32767).astype(np.int16)
        x = np.zeros((last, 4), dtype=np.int16)
        x[:, 0] = c
        ref = run_record_int(q, FixedDSP(secs, 4).process(x, sos=True), keep=False)
        by_end = {int(e): list(map(int, l)) for e, l in zip(ref["ends"], ref["logits"])}
        for r in rs:
            e = r["esi_timestamp"]
            compared += 1
            if by_end.get(e) != list(map(int, r["scores"])):
                bad += 1
                if bad <= 3:
                    print(f"MISMATCH epoch {epoch} end {e}: board {r['scores']} reference {by_end.get(e)}")
            win = label[(s0 + np.arange(e - 399, e + 1)) % n]
            if e >= 799 and win.min() == win.max() and win[0] >= 0:
                conf[int(win[0]), int(r["argmax"])] += 1
    recall = {NAMES[c]: round(float(conf[c, c] / max(1, conf[c].sum())), 4) for c in range(3)}
    print(json.dumps({"results": len(res), "compared": compared, "mismatch": bad, "epochs": len({r["reset_epoch"] for r in res}),
                      "loop_s": n / 2000, "board_confusion": conf.tolist(), "board_recall": recall,
                      "board_balanced": round(float(np.mean(list(recall.values()))), 4)}))
    print(f"[{'PASS' if bad == 0 else 'FAIL'}] board scores against the reference")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
