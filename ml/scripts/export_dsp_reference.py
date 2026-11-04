"""Export the DSP-R0 float reference for later E2 (fixed point) / E3 (RTL) work.

    python ml/scripts/export_dsp_reference.py [--out analysis/ml/dsp_reference]

Writes (git-ignored, reproducible from this script and the tracked configs):
  <variant>/description.json   stage order, coefficients (SOS / FIR taps), poles, responses, group delay
  <variant>/vectors.npz        deterministic test signals and the pipeline's outputs
                               (waveform, window ends, features) - stream starts with SOS, a GAP in the middle
  preprocessing_g0.json        before/after statistics on real GRABMyo G0 records (if downloaded)
Test signals: DC + steps, 50/60 Hz lines, sweep 5-900 Hz, Gaussian noise, a burst, all 4 channels.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy import signal

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path.insert(0, str(ML))

from emgml.dsp import DSPConfig, EMGPreprocessPipeline  # noqa: E402

VARIANTS = ["dc_bp20_450", "dc_notch60_bp20_450", "dc_fir20_450", "none"]


def test_signals(fs=2000.0, n=8000, seed=1234) -> np.ndarray:
    t = np.arange(n) / fs
    rng = np.random.default_rng(seed)
    ch0 = 0.5 + 0.2 * (t > 1.0) - 0.4 * (t > 2.5) + 0.05 * np.sin(2 * np.pi * 150 * t)       # DC, steps
    ch1 = 0.1 * np.sin(2 * np.pi * 50 * t) + 0.1 * np.sin(2 * np.pi * 60 * t) + 0.02 * rng.normal(size=n)
    ch2 = 0.1 * signal.chirp(t, 5, t[-1], 900, method="logarithmic")                          # sweep
    ch3 = 0.03 * rng.normal(size=n) * (1 + 4 * ((t > 1.5) & (t < 2.5)))                       # burst
    return np.stack([ch0, ch1, ch2, ch3], axis=1)


def export_variant(name: str, out: Path) -> dict:
    cfg = DSPConfig.load(ML / "configs" / "dsp" / f"{name}.toml")
    cfg.n_ch = 4
    p = EMGPreprocessPipeline(cfg)
    x = test_signals(cfg.fs)
    a = p.process(x[:5000], sos=True)
    b = p.process(x[5000:], gap=True)                  # GAP: state reset per ESI v0 (gap_policy = reset)
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    desc = p.describe()
    (d / "description.json").write_text(json.dumps(desc, indent=1), encoding="utf-8")
    np.savez_compressed(d / "vectors.npz", x=x, gap_at=5000,
                        y_a=a.waveform, end_a=a.window_end, feat_a=a.features,
                        y_b=b.waveform, end_b=b.window_end, feat_b=b.features,
                        feature_names=np.array(a.feature_names))
    return desc


def preprocessing_stats(out: Path) -> dict | None:
    raw = REPO / "data" / "public" / "grabmyo" / "raw"
    if not (raw / "Session1").exists():
        return None
    from emgml.datasets import grabmyo
    recs = grabmyo.load_subset(raw, [1, 2, 3], [1, 2, 3], [16, 17], [1, 4, 7], "bip4", 2000.0)
    res = {}
    for name in ("none", "dc_bp20_450", "dc_notch60_bp20_450", "dc_bp40_450"):
        cfg = DSPConfig.load(ML / "configs" / "dsp" / f"{name}.toml")
        cfg.n_ch = 4
        rms, p60, mdf = [], [], []
        for r in recs:
            y = EMGPreprocessPipeline(cfg).run_record(r.x).waveform[200:]          # skip settle
            rms.append(np.sqrt(np.mean(y ** 2, axis=0)))
            f, pxx = signal.welch(y, fs=2000.0, nperseg=2000, axis=0)
            band = (f >= 55) & (f <= 65)
            p60.append(np.sum(pxx[band], axis=0) / np.sum(pxx, axis=0))
            c = np.cumsum(pxx, axis=0)
            mdf.append(f[np.argmax(c >= c[-1] / 2, axis=0)])
        rms, p60, mdf = map(np.array, (rms, p60, mdf))
        lab = np.array([r.label for r in recs])
        res[name] = {"rms_mV_median_rest": float(np.median(rms[lab == 0])),
                     "rms_mV_median_hand_close": float(np.median(rms[lab == 1])),
                     "power_frac_55_65Hz_median": float(np.median(p60)),
                     "median_freq_hz_median": float(np.median(mdf))}
    base = res["none"]
    for k, v in res.items():
        v["rms_ratio_vs_none_hand_close"] = v["rms_mV_median_hand_close"] / base["rms_mV_median_hand_close"]
    res["_records"] = len(recs)
    (out / "preprocessing_g0.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "analysis" / "ml" / "dsp_reference"))
    out = Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    for v in VARIANTS:
        desc = export_variant(v, out)
        print(f"{v}: stages {list(desc['stages'])}")
        for st, info in desc["stages"].items():
            coef = info.get("sos", info.get("taps"))
            print(f"   {st}: {info['type']}; coefficients {np.array(coef).shape}; "
                  f"max pole radius {info.get('max_pole_radius', '-')}")
    s = preprocessing_stats(out)
    if s:
        print(json.dumps(s, indent=1))
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
