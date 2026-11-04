"""Generate E3 RTL test vectors (git-ignored, build/e3/vec/) from the frozen E2 reference.

    python scripts/sim/gen_e3_vectors.py [--out build/e3/vec]

Model, quantisation and DSP coefficients come only from reference/emg_fixed/vectors/ref_s1_test_s3_seed0
(E2 reference fold: subject 1, P2 test day 3, seed 0); nothing is re-trained or re-quantised.
Decision schedule settle = 0 (first decision at sample index 399).

Each case directory has meta.json (consumed by scripts/sim/run_e3.py) and hex files:
    DSP cases  : frames.hex (176-bit ESI frames), expect.hex (176-bit expected DSP output frames)
    CNN/chain  : the files written by emg_fixed.vectors.export_scenario + cnn_in.hex (POST_DSP frames)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "ml"), str(REPO / "reference")]

from emg_fixed import vectors  # noqa: E402
from emg_fixed.cnn.quantize import from_saved  # noqa: E402
from emg_fixed.dsp.fixed_dsp import FixedDSP, quantise_sos  # noqa: E402
from emg_fixed.esi import (FLAG_GAP, FLAG_OVR, FLAG_POST_DSP, FLAG_SOS, SRC_PATTERN, SRC_REPLAY,  # noqa: E402
                           SourceCalibration, make_frames, reset_points, to_esi)
from emgml.datasets import grabmyo  # noqa: E402
from emgml.dsp import DSPConfig  # noqa: E402
from emgml.dsp.bandpass import Bandpass  # noqa: E402
from emgml.dsp.dc_remove import dc_blocker_sos  # noqa: E402
from emgml.dsp.notch import notch_sos  # noqa: E402

TRACKED = REPO / "reference" / "emg_fixed" / "vectors" / "ref_s1_test_s3_seed0"
RAW = REPO / "data" / "public" / "grabmyo" / "raw"
FS = 2000.0
DEFAULT_MASK = 0b1101          # slots: bit0 DC, bit1 notch, bit2 BP0, bit3 BP1


def slots() -> list:
    dsp = DSPConfig.load(REPO / "ml" / "configs" / "dsp" / "dc_bp20_450.toml")
    bp = Bandpass("iir_butter", dsp.bandpass.low_hz, dsp.bandpass.high_hz, FS, 1, dsp.bandpass.order)
    s = quantise_sos("dc", dc_blocker_sos(dsp.dc.fc_hz, FS)) + quantise_sos("notch", notch_sos(60.0, 30.0, FS)) \
        + quantise_sos("bp", bp.sos)
    assert len(s) == 4
    return s


SLOTS = slots()


def sections(mask: int):
    return [SLOTS[i] for i in range(4) if mask >> i & 1]


def write_coef(path: Path):
    words = []
    for sl in SLOTS:
        words += list(sl.b) + list(sl.a)
    vectors.write_words(path, words, 18)


def model():
    q = from_saved(*_model_files())
    q.settle = 0
    return q


def _model_files():
    run_dir = REPO / "analysis" / "ml" / "e2" / "e2_20261022_023953" / "models"
    return run_dir / "s1_test_s3_seed0_int8.npz", run_dir / "s1_test_s3_seed0_int8.json"


def calibration() -> SourceCalibration:
    meta = json.loads(_model_files()[1].read_text(encoding="utf-8"))
    return SourceCalibration(**meta["esi"])


def i16(a):
    return np.clip(np.round(a), -32768, 32767).astype(np.int16)


def dsp_expected(fr, mask_schedule: list[tuple[int, int]]):
    """Expected DSP output frames: resets at SOS/GAP and at mask changes (forced SOS on output)."""
    codes = fr.lanes[:, :4]
    T = codes.shape[0]
    cuts = sorted(set(list(reset_points(fr)) + [s for s, _ in mask_schedule]) | {T})
    forced = {s for s, _ in mask_schedule[1:]}
    out = np.zeros((T, 4), dtype=np.int16)
    sat = np.zeros(T, dtype=bool)
    stats = {"int_sat": 0, "out_sat": 0}
    for a, b in zip(cuts[:-1], cuts[1:]):
        mask = [m for s, m in mask_schedule if s <= a][-1]
        d = FixedDSP(sections(mask), 4)
        out[a:b] = d.process(codes[a:b], sos=True)
        sat[a:b] = d.last_out_sat.any(axis=1)
        stats["int_sat"] += d.stats["internal_saturations"]
        stats["out_sat"] += d.stats["output_saturations"]
    flags = fr.flags.copy() | FLAG_POST_DSP | np.where(sat, FLAG_OVR, 0)
    for s in forced:
        flags[s] |= FLAG_SOS
    lanes = np.zeros((T, 8), dtype=np.int16)
    lanes[:, :4] = out
    return vectors.Frames(fr.seq, flags, lanes), stats


def dsp_case(root: Path, name: str, x: np.ndarray, mask_schedule, desc: str, coef_via_port=False, **frame_kw):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fr = make_frames(i16(x) if x.dtype != np.int16 else x, frame_kw.pop("src", SRC_REPLAY), **frame_kw)
    exp, stats = dsp_expected(fr, mask_schedule)
    vectors.write_words(d / "frames.hex", vectors.frame_words(fr), 176)
    vectors.write_words(d / "expect.hex", vectors.frame_words(exp), 176)
    meta = {"tb": "dsp", "name": name, "description": desc, "frames": int(len(fr.seq)),
            "mask": mask_schedule[0][1], "cfgchg_at": mask_schedule[1][0] if len(mask_schedule) > 1 else -1,
            "cfgchg_mask": mask_schedule[1][1] if len(mask_schedule) > 1 else 0,
            "coef_via_port": coef_via_port, "expect_int_sat": stats["int_sat"], "expect_out_sat": stats["out_sat"]}
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def cnn_meta(d: Path, tb: str, scen: dict, frames_file: str, with_acc: bool, desc: str):
    meta = {"tb": tb, "name": d.name, "description": desc, "frames": scen["frames"], "input": frames_file,
            "with_acc": with_acc, "mask": DEFAULT_MASK, "decisions": scen["decisions"],
            "counts": {k: v["words"] for k, v in scen["files"].items()}}
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def post_frames(d: Path):
    """cnn_in.hex: dsp_out lanes + input flags/seq (+ POST_DSP) for CNN-only runs of a PRE_DSP scenario."""
    fin = [int(l, 16) for l in (d / "input_frames.hex").read_text().split()]
    dout = [int(l, 16) for l in (d / "dsp_out.hex").read_text().split()]
    words = [(lanes & ((1 << 128) - 1)) | ((((f >> 128) & 0xFFFF) | FLAG_POST_DSP) << 128) | ((f >> 144) << 144)
             for f, lanes in zip(fin, dout)]
    vectors.write_words(d / "cnn_in.hex", words, 176)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "build" / "e3" / "vec"))
    ns = ap.parse_args()
    out = Path(ns.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    q, cal = model(), calibration()
    rng = np.random.default_rng(20261022)
    t = np.arange(40000) / FS
    metas = []
    # ---------------- model files for the RTL ($readmemh) ----------------
    mdir = out / "model"
    shutil.copytree(TRACKED / "model", mdir)
    write_coef(mdir / "dsp_coef4.hex")
    # ---------------- DSP cases ----------------
    x = np.zeros((2000, 4))
    x[100, 0], x[300, 1], x[500, 2], x[700, 3], x[701, 3], x[1500, 0] = 32767, -32768, 10000, -5000, -5000, -32768
    metas.append(dsp_case(out, "dsp_impulse", x, [(0, DEFAULT_MASK)], "impulses, full scale and small"))
    x = np.zeros((3000, 4))
    x[200:, :] = [20000, -20000, 5000, 32767]
    x[1500:, :] -= [30000, -30000, 10000, 65535]
    metas.append(dsp_case(out, "dsp_step", x, [(0, DEFAULT_MASK)], "steps up and down per lane"))
    ph = np.array([0, 0.7, 1.9, 3.1])
    x = 15000 * np.sin(2 * np.pi * 50 * t[:4000, None] + ph) + rng.normal(scale=300, size=(4000, 4))
    metas.append(dsp_case(out, "dsp_sine50", x, [(0, DEFAULT_MASK)], "50 Hz sine + noise, notch off"))
    x = 12000 * np.sin(2 * np.pi * 60 * t[:4000, None] + ph) + 6000 * np.sin(2 * np.pi * 150 * t[:4000, None])
    metas.append(dsp_case(out, "dsp_sine60_notch", x, [(0, 0b1111)], "60 Hz + 150 Hz, notch on"))
    n = np.arange(2000)
    x = np.stack([np.where((n // 50) % 2 == 0, 32767, -32768), np.where(n % 2 == 0, 32767, -32768),
                  np.clip(rng.normal(scale=40000, size=2000), -32768, 32767), np.full(2000, 32767)], axis=1)
    metas.append(dsp_case(out, "dsp_extremes", x.astype(np.int16), [(0, DEFAULT_MASK)],
                          "full-scale square, fs/2 alternation, clipped noise, constant max", src=SRC_PATTERN))
    x = rng.normal(size=(3000, 4)) * [2000, 8000, 15000, 30000]
    metas.append(dsp_case(out, "dsp_multich", x, [(0, DEFAULT_MASK)], "independent lanes, different amplitudes"))
    metas.append(dsp_case(out, "dsp_coef_port", x, [(0, DEFAULT_MASK)], "as dsp_multich, coefficients via write port",
                          coef_via_port=True))
    x = rng.normal(scale=6000, size=(3000, 4))
    metas.append(dsp_case(out, "dsp_sos_gap", x, [(0, DEFAULT_MASK)], "SOS at 0, GAP at 1100 (seq +50), SOS at 2200; seq wraps",
                          seq0=0xFFFFFC00, sos_at=(0, 2200), gap_at=(1100,), seq_jump_at={1100: 50}))
    metas.append(dsp_case(out, "dsp_cfg_change", x, [(0, DEFAULT_MASK), (1500, 0b1111)],
                          "notch enabled at frame 1500 (state cleared, SOS forced on output)"))
    real = [grabmyo.load_trial(RAW, 3, 1, g, tr, "bip4", FS) for g, tr in ((15, 1), (4, 2), (12, 3))]
    codes = np.concatenate([to_esi(r.x, cal)[0] for r in real])
    metas.append(dsp_case(out, "dsp_bypass", codes[:2000], [(0, 0)], "all slots disabled: output = input"))
    metas.append(dsp_case(out, "dsp_long_real", codes, [(0, DEFAULT_MASK)],
                          "30000 frames: three real G1 test records as one continuous stream"))
    # ---------------- CNN-only cases (POST_DSP input) ----------------
    secs = sections(DEFAULT_MASK)
    for sc in ("s1_normal", "s2_sos_restart", "s3_gap", "s4_saturation", "s5_post_dsp"):
        d = out / f"cnn_{sc}"
        shutil.copytree(TRACKED / sc, d)
        man = json.loads((TRACKED / "manifest.json").read_text(encoding="utf-8"))
        scen = next(s for s in man["scenarios"] if s["name"] == sc)
        if sc == "s5_post_dsp":
            shutil.copy(d / "input_frames.hex", d / "cnn_in.hex")
        else:
            post_frames(d)
        metas.append(cnn_meta(d, "cnn", scen, "cnn_in.hex", (d / "conv1_acc.hex").exists(),
                              f"E2 tracked scenario {sc}, POST_DSP input to the CNN"))
        if sc != "s5_post_dsp":
            dc = out / f"chain_{sc}"
            shutil.copytree(d, dc)
            metas.append(cnn_meta(dc, "chain", scen, "input_frames.hex", (dc / "conv1_acc.hex").exists(),
                                  f"E2 tracked scenario {sc}, PRE_DSP input through DSP + CNN"))
    for g, tr in ((17, 1), (16, 1), (15, 2), (12, 3), (11, 4), (4, 5)):
        r = grabmyo.load_trial(RAW, 3, 1, g, tr, "bip4", FS)
        c, _ = to_esi(r.x, cal)
        y = FixedDSP(secs, 4).process(c, sos=True)
        fr = make_frames(y, SRC_REPLAY, seq0=1000 * g, post_dsp=True)
        d = out / f"cnn_real_g{g}_t{tr}"
        scen = vectors.export_scenario(d, d.name, fr, q, secs, 4, "POST_DSP", True,
                                       f"G1 test record session3/p1 gesture {g} trial {tr}, full 10000 frames")
        shutil.copy(d / "input_frames.hex", d / "cnn_in.hex")
        metas.append(cnn_meta(d, "cnn", scen, "cnn_in.hex", True, scen["description"]))
    y = np.clip(rng.normal(scale=5000, size=(6000, 4)), -32768, 32767).astype(np.int16)
    fr = make_frames(y, SRC_REPLAY, seq0=0xFFFFF000, sos_at=(0, 4000), gap_at=(2345,), seq_jump_at={2345: 99},
                     post_dsp=True)
    d = out / "cnn_random_resets"
    scen = vectors.export_scenario(d, d.name, fr, q, secs, 4, "POST_DSP", True,
                                   "noise, SOS at 0 and 4000, GAP at 2345 (seq +99), seq wraps")
    shutil.copy(d / "input_frames.hex", d / "cnn_in.hex")
    metas.append(cnn_meta(d, "cnn", scen, "cnn_in.hex", True, scen["description"]))
    # ---------------- chain: long real stream ----------------
    fr = make_frames(codes[:20000], SRC_REPLAY, seq0=7)
    d = out / "chain_real_long"
    scen = vectors.export_scenario(d, d.name, fr, q, secs, 4, "PRE_DSP", True,
                                   "20000 frames: two real G1 test records, one continuous stream")
    metas.append(cnn_meta(d, "chain", scen, "input_frames.hex", True, scen["description"]))
    model_sha = hashlib.sha256(_model_files()[0].read_bytes()).hexdigest()
    (out / "index.json").write_text(json.dumps({"model_npz_sha256": model_sha, "settle": q.settle,
                                                "cases": metas}, indent=1), encoding="utf-8")
    print(f"{len(metas)} cases -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
