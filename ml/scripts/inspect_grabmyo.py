"""X0: inspect real GRABMyo records (header vs data, units, ranges, RMS, PSD, saturation).

    python ml/scripts/inspect_grabmyo.py --session 1 --participant 1 --gesture 16 --trial 1
    python ml/scripts/inspect_grabmyo.py --summary          # statistics over all downloaded G0 records

Writes analysis/ml/x0/<record>/ (summary.json, waveform.png, psd.png) and prints a report.
Digital values at +-32767/-32768 are reported as possible saturation.
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

from emgml.datasets import grabmyo  # noqa: E402
from emgml.datasets.wfdb_reader import read_digital, to_physical  # noqa: E402

RAW = REPO / "data" / "public" / "grabmyo" / "raw"
OUT = REPO / "analysis" / "ml" / "x0"


def band_power(f, p, lo, hi):
    m = (f >= lo) & (f < hi)
    return float(np.trapezoid(p[m], f[m])) if m.any() else 0.0


def line_prominence_db(f, p, f0, half=1.0, side=(3.0, 8.0)):
    peak = p[(f >= f0 - half) & (f <= f0 + half)].max()
    ref = np.median(p[((f >= f0 - side[1]) & (f <= f0 - side[0])) | ((f >= f0 + side[0]) & (f <= f0 + side[1]))])
    return float(10 * np.log10(peak / ref))


def analyse(phys: np.ndarray, names: list[str], fs: float) -> dict:
    f, p = signal.welch(phys, fs=fs, nperseg=2048, axis=0)
    chans = []
    for c, n in enumerate(names):
        pc = p[:, c]
        total = band_power(f, pc, 0, fs / 2)
        chans.append({"name": n, "min_mV": float(phys[:, c].min()), "max_mV": float(phys[:, c].max()),
                      "mean_mV": float(phys[:, c].mean()), "rms_mV": float(np.sqrt(np.mean(phys[:, c] ** 2))),
                      "rms_ac_mV": float(np.std(phys[:, c])),
                      "power_frac_below_10Hz": band_power(f, pc, 0, 10) / total,
                      "power_frac_10_450Hz": band_power(f, pc, 10, 450) / total,
                      "power_frac_above_450Hz": band_power(f, pc, 450, fs / 2) / total,
                      "median_freq_hz": float(f[np.searchsorted(np.cumsum(pc), np.sum(pc) / 2)]),
                      "line50_prominence_db": line_prominence_db(f, pc, 50.0),
                      "line60_prominence_db": line_prominence_db(f, pc, 60.0)})
    return {"channels": chans, "psd_f": f, "psd": p}


def inspect_one(s, p, g, t, plots=True) -> dict:
    rel = grabmyo.record_relpath(s, p, g, t)
    hea = RAW / (rel + ".hea")
    h, d = read_digital(hea)                       # raises on any header/data inconsistency
    phys = to_physical(h, d)
    names = [x.description for x in h.signals]
    sat = {n: int(np.sum((d[:, i] >= 32767) | (d[:, i] <= -32768))) for i, n in enumerate(names)}
    a = analyse(phys, names, h.fs)
    out = {"record": h.record_name, "fs": h.fs, "n_samples": h.n_samples, "n_sig": h.n_sig,
           "dat_bytes": (hea.with_suffix(".dat")).stat().st_size,
           "bytes_expected": h.n_samples * h.n_sig * 2,
           "units": sorted({x.units for x in h.signals}),
           "gains": {x.description: x.gain for x in h.signals},
           "baselines": {x.description: x.baseline for x in h.signals},
           "saturated_samples": sat, "channels": a["channels"]}
    if plots:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        od = OUT / h.record_name
        od.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(16, 1, figsize=(10, 16), sharex=True)
        tt = np.arange(h.n_samples) / h.fs
        for i in range(16):
            ax[i].plot(tt, phys[:, i], lw=0.4)
            ax[i].set_ylabel(names[i], rotation=0, fontsize=7)
        ax[-1].set_xlabel("s")
        fig.suptitle(f"{h.record_name} forearm channels (mV)")
        fig.savefig(od / "waveform.png", dpi=110)
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(9, 5))
        for i in range(16):
            ax.semilogy(a["psd_f"], a["psd"][:, i], lw=0.6, label=names[i] if i < 4 else None)
        for x in (10, 50, 60, 450):
            ax.axvline(x, color="k", lw=0.5, ls=":")
        ax.set_xlabel("Hz"); ax.set_ylabel("mV^2/Hz"); ax.set_title(f"{h.record_name} PSD (Welch, 2048 seg)")
        ax.legend(fontsize=7)
        fig.savefig(od / "psd.png", dpi=110)
        plt.close(fig)
        (od / "summary.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=int, default=1)
    ap.add_argument("--participant", type=int, default=1)
    ap.add_argument("--gesture", type=int, default=16)
    ap.add_argument("--trial", type=int, default=1)
    ap.add_argument("--summary", action="store_true", help="statistics over all G0 forearm channels")
    ns = ap.parse_args()
    if not ns.summary:
        r = inspect_one(ns.session, ns.participant, ns.gesture, ns.trial)
        print(f"record {r['record']}: fs={r['fs']} n_samples={r['n_samples']} n_sig={r['n_sig']} "
              f"dat={r['dat_bytes']} B (expected {r['bytes_expected']}) units={r['units']}")
        print(f"{'ch':>4} {'gain/mV':>10} {'base':>6} {'min':>8} {'max':>8} {'rms_ac':>7} {'<10Hz':>6} "
              f"{'>450Hz':>6} {'mdf':>5} {'50Hz':>5} {'60Hz':>5} sat")
        for c in r["channels"][:16]:
            n = c["name"]
            print(f"{n:>4} {r['gains'][n]:10.0f} {r['baselines'][n]:6d} {c['min_mV']:8.4f} {c['max_mV']:8.4f} "
                  f"{c['rms_ac_mV']:7.4f} {c['power_frac_below_10Hz']:6.3f} {c['power_frac_above_450Hz']:6.3f} "
                  f"{c['median_freq_hz']:5.0f} {c['line50_prominence_db']:5.1f} {c['line60_prominence_db']:5.1f} "
                  f"{r['saturated_samples'][n]}")
        print(f"plots and summary.json in {(OUT / r['record']).relative_to(REPO).as_posix()}")
        return 0
    rows = []
    for hea in sorted(RAW.glob("Session*/*/*.hea")):
        parts = hea.stem.split("_")
        s, p, g, t = (int(x.lstrip("sessionparticipantgesturetrial")) for x in parts)
        r = inspect_one(s, p, g, t, plots=False)
        for c in r["channels"][:16]:
            rows.append((g, c["rms_ac_mV"], c["power_frac_below_10Hz"], c["power_frac_above_450Hz"],
                         c["line50_prominence_db"], c["line60_prominence_db"], r["saturated_samples"][c["name"]]))
    a = np.array(rows)
    summary = {"records": len(rows) // 16, "forearm_channel_records": len(rows),
               "saturated_samples_total": int(a[:, 6].sum()),
               "frac_below_10Hz_median": float(np.median(a[:, 2])),
               "frac_above_450Hz_median": float(np.median(a[:, 3])),
               "line50_prominence_db_median": float(np.median(a[:, 4])),
               "line60_prominence_db_median": float(np.median(a[:, 5])),
               "line60_prominence_db_p90": float(np.percentile(a[:, 5], 90)),
               "rms_ac_mV_by_gesture_median": {int(g): float(np.median(a[a[:, 0] == g, 1]))
                                               for g in sorted(set(a[:, 0]))}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "g0_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
