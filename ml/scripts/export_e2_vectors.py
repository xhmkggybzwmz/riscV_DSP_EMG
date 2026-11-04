"""Export E2 golden vectors for the future DSP / CNN RTL testbenches.

    python ml/scripts/export_e2_vectors.py --run analysis/ml/e2/<run> [--out reference/emg_fixed/vectors/ref_s1_test_s3_seed0]

Uses the reference fold model (subject 1, P2 test day 3, seed 0) saved by run_e2.py. Small scenarios go
to the tracked output directory; a full 10000-sample record set goes to <run>/vectors_full/ (ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path[:0] = [str(ML), str(REPO / "reference")]

from emg_fixed import vectors  # noqa: E402
from emg_fixed.cnn.quantize import describe, from_saved  # noqa: E402
from emg_fixed.dsp.fixed_dsp import FixedDSP, sections_from_config  # noqa: E402
from emg_fixed.esi import (FLAG_POST_DSP, SRC_PATTERN, SRC_REPLAY, SourceCalibration, make_frames,  # noqa: E402
                           to_esi)
from emgml.datasets import grabmyo  # noqa: E402
from emgml.dsp import DSPConfig  # noqa: E402

RAW = REPO / "data" / "public" / "grabmyo" / "raw"


def _json_default(o):
    """numpy scalars -> exact Python numbers (ints stay ints)."""
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default=str(REPO / "reference" / "emg_fixed" / "vectors" / "ref_s1_test_s3_seed0"))
    ns = ap.parse_args()
    run = Path(ns.run).resolve()
    name = "s1_test_s3_seed0"
    q = from_saved(run / "models" / f"{name}_int8.npz", run / "models" / f"{name}_int8.json")
    q.settle = 0          # E3 decision schedule: first decision at sample index 399 (no warm-up)
    meta = json.loads((run / "models" / f"{name}_int8.json").read_text(encoding="utf-8"))
    cal = SourceCalibration(**meta["esi"])
    dsp_cfg = DSPConfig.load(ML / "configs" / "dsp" / "dc_bp20_450.toml")
    dsp_cfg.n_ch = 4
    secs = sections_from_config(dsp_cfg)
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    # test records of the reference fold (subject 1, session 3 = test day)
    rec = lambda g, t: grabmyo.load_trial(RAW, 3, 1, g, t, "bip4", 2000.0)
    codes = lambda r, a, b: to_esi(r.x[a:b], cal)[0]
    close, rest, ext = rec(16, 1), rec(17, 1), rec(11, 2)
    scen = []
    # S1 normal: real HAND_CLOSE data from SOS
    fr = make_frames(codes(close, 0, 1400), SRC_REPLAY)
    scen.append(vectors.export_scenario(out / "s1_normal", "s1_normal", fr, q, secs, 4, "PRE_DSP", True,
                                        "1400 samples of session3/participant1 HAND_CLOSE trial 1 (P2 test day), "
                                        "SOS at frame 0; with accumulators of every layer"))
    # S2 SOS restart: REST then a new stream (SOS) of HAND_CLOSE
    x = np.concatenate([codes(rest, 0, 800), codes(close, 0, 900)])
    fr = make_frames(x, SRC_REPLAY, seq0=5000, sos_at=(0, 800))
    scen.append(vectors.export_scenario(out / "s2_sos_restart", "s2_sos_restart", fr, q, secs, 4, "PRE_DSP", False,
                                        "800 REST samples, then SOS and 900 HAND_CLOSE samples (two streams)"))
    # S3 GAP: 37 frames lost at 900 (seq jumps by 37, GAP flag), state reset
    x = np.concatenate([codes(ext, 0, 900), codes(ext, 937, 1737)])
    fr = make_frames(x, SRC_REPLAY, seq0=0xFFFFFE00, gap_at=(900,), seq_jump_at={900: 37})
    scen.append(vectors.export_scenario(out / "s3_gap", "s3_gap", fr, q, secs, 4, "PRE_DSP", False,
                                        "WRIST_EXT trial 2: frames 0-899, 37 frames dropped (seq +37, GAP flag), "
                                        "then 800 frames; seq starts at 0xFFFFFE00 and wraps"))
    # S4 saturation / extremes (pattern source)
    t = np.arange(1000)
    sq = np.where((t // 50) % 2 == 0, 32767, -32768)
    rng = np.random.default_rng(1234)
    x = np.stack([sq, -sq - 1, np.clip(rng.normal(scale=40000, size=1000), -32768, 32767), np.full(1000, 32767)],
                 axis=1).astype(np.int16)
    fr = make_frames(x, SRC_PATTERN)
    scen.append(vectors.export_scenario(out / "s4_saturation", "s4_saturation", fr, q, secs, 4, "PRE_DSP", False,
                                        "full-scale square waves, clipped noise and a +32767 constant: DSP output "
                                        "saturation and uint8 activation clipping"))
    # S5 POST_DSP: inference core alone, input = fixed DSP output of S1 (POST_DSP flag)
    y = FixedDSP(secs, 4).process(codes(close, 0, 1400), sos=True)
    fr = make_frames(y, SRC_REPLAY, post_dsp=True)
    scen.append(vectors.export_scenario(out / "s5_post_dsp", "s5_post_dsp", fr, q, secs, 4, "POST_DSP", False,
                                        "inference-core-only input: the fixed DSP output of s1_normal "
                                        f"(flag bit 6 POST_DSP = {FLAG_POST_DSP:#x}); must reproduce s1 CNN outputs; "
                                        "must NOT be filtered again"))
    model_files = vectors.export_model(out / "model", q, secs)
    manifest = {
        "vector_version": vectors.VECTOR_VERSION, "reference_version": meta["meta"]["reference_version"],
        "model": {"fold": meta["meta"]["fold"], "subject": meta["meta"]["subject"], "seed": meta["meta"]["seed"],
                  "float_model_sha256": meta["meta"]["model_sha256"], "int8_arrays_sha256": meta["int8_arrays_sha256"],
                  "splits_sha256": meta["meta"]["splits_sha256"], "describe": describe(q), "files": model_files},
        "esi_calibration": cal.to_dict() | {"calibration_records": f"{len(cal.calibration_records)} training records "
                                                                   "(see the run's int8.json)"},
        "dsp": FixedDSP(secs, 1).describe(),
        "postproc": {"K": 1, "T": 0, "rules": "reference/emg_fixed/postproc.py (E3 default K = 1, T = 0)"},
        "decision_schedule": {"settle": 0, "first_decision_sample_index": 399, "every": 100,
                              "note": "E2's recorded accuracy used settle = 200; weights are unchanged"},
        "formats": {
            "input_frames.hex": "176-bit: lane0..7 int16 [127:0] (lane0 in bits 15:0), flags uint16 [143:128], seq uint32 [175:144]",
            "dsp_out.hex": "128-bit: lane0..7 int16 POST_DSP (lanes 4..7 = 0), one word per frame in segment order",
            "convN_act.hex": "8*C bits uint8 activations (channel 0 in bits 7:0), then j [24], then segment [8]",
            "convN_acc.hex": "32*C bits int32 accumulators (before requant), then j [24], then segment [8]",
            "gap_sum.hex": "32 x 16-bit GAP sums (13 bits used), then window end sample index [24], then segment [8]",
            "logits.hex": "6 x int32 logits (class 0 in bits 31:0), then window end [24], then segment [8]",
            "decisions.hex": "72-bit: argmax [3:0], post-processed output [7:4] (two's complement 4-bit: -2 = "
                             "NO_DECISION, -1 = UNCERTAIN), margin uint32 [39:8], window end [63:40], segment [71:64]",
            "model/convN_w.hex": "int8 per line, index (o*C_in + c)*K + k", "model/convN_b.hex": "int32 per line",
            "model/convN_M.hex": "uint16 per line (per output channel)", "model/convN_s.hex": "uint8 shift per line",
            "model/fc_w.hex": "int8 per line, index k*32 + c", "model/fc_b.hex": "int32 per line",
            "model/dsp_coef.hex": "int18 per line: for each section b0 b1 b2 a1 a2 (Q2.16)"},
        "scenarios": scen}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=_json_default), encoding="utf-8")
    # full record set (not tracked)
    full = run / "vectors_full"
    fr = make_frames(codes(close, 0, close.n_samples), SRC_REPLAY)
    sf = vectors.export_scenario(full / "full_hand_close", "full_hand_close", fr, q, secs, 4, "PRE_DSP", True,
                                 "complete 10000-sample record")
    (full / "manifest.json").write_text(json.dumps(sf, indent=1, default=_json_default), encoding="utf-8")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"tracked vectors: {out} ({size / 1024:.0f} KiB), scenarios {[s['name'] for s in scen]}, "
          f"decisions {[s['decisions'] for s in scen]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
