"""GRABMyo v1.1.0 loader (WFDB .hea/.dat, PhysioNet).

Facts used here (all from the dataset's own files, see docs/system_design_extend.md 3.1):
  - files Session{i}/session{i}_participant{j}/session{i}_participant{j}_gesture{k}_trial{l}.{hea,dat},
    k = 1..17 (17 = Rest, MotionSequence.txt / readme.txt), l = 1..7
  - 32 signals per record, named F1..F16, W1..W12, U1..U4 in the header; 2048 Hz; 10240 samples
  - Electrodelocation.pdf: forearm ring 1 = electrodes 1..8 (electrode 1 on the centreline of the
    elbow crease, numbering in the direction of the drawn arrow), ring 2 = electrodes 9..16, 2 cm
    distal, electrode k+8 longitudinally in line with electrode k; header names F1..F16 follow the
    original electrode numbering (wrist W1..W12 = original electrodes 18..23, 26..31)
  - DeviceInfo.pdf: EMG-USB2+ (OT Bioelettronica), hardware band-pass 10-450 Hz, gain 500, 12-bit ADC;
    the published WFDB files are 16-bit with per-channel gain/baseline (re-quantised by the publisher)
  - monopolar recording; the reference electrode site is not documented

Channel configurations select lanes by header *name*, never by column position:
  bip4  : F1-F9, F3-F11, F5-F13, F7-F15    longitudinal bipolar pairs, every second ring position
  bip8  : Fk-F(k+8), k = 1..8
  mono4 : F1, F3, F5, F7                    monopolar (common reference of the amplifier)
  mono8 : F1..F8
  bip2_ij : two of the bip4 pairs, ring positions i and j (e.g. bip2_15 = F1-F9, F5-F13: electrode 1 on
            the elbow-crease centreline and the one opposite it); a two-sensor set-up
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import numpy as np

from ..labels import GRABMYO_TO_UNIFIED
from ..recording import Recording
from ..resample import resample
from .wfdb_reader import parse_header, read_digital, to_physical

DATASET = "GRABMyo-1.1.0"
FS_NATIVE = 2048.0

CHANNEL_CONFIGS: dict[str, list[tuple[str, str | None]]] = {
    "bip4": [("F1", "F9"), ("F3", "F11"), ("F5", "F13"), ("F7", "F15")],
    "bip8": [(f"F{k}", f"F{k + 8}") for k in range(1, 9)],
    "mono4": [("F1", None), ("F3", None), ("F5", None), ("F7", None)],
    "mono8": [(f"F{k}", None) for k in range(1, 9)],
}
for _i, _j in ((1, 5), (3, 7), (1, 3), (3, 5), (5, 7), (1, 7)):
    CHANNEL_CONFIGS[f"bip2_{_i}{_j}"] = [(f"F{_i}", f"F{_i + 8}"), (f"F{_j}", f"F{_j + 8}")]


def record_relpath(session: int, participant: int, gesture: int, trial: int) -> str:
    return (f"Session{session}/session{session}_participant{participant}/"
            f"session{session}_participant{participant}_gesture{gesture}_trial{trial}")


def lane_names(config: str) -> list[str]:
    return [a if b is None else f"{a}-{b}" for a, b in CHANNEL_CONFIGS[config]]


def select_channels(names: list[str], phys: np.ndarray, config: str) -> np.ndarray:
    idx = {n: i for i, n in enumerate(names)}
    cols = []
    for a, b in CHANNEL_CONFIGS[config]:
        if a not in idx or (b is not None and b not in idx):
            raise KeyError(f"channel {a}/{b} not in record ({names})")
        cols.append(phys[:, idx[a]] if b is None else phys[:, idx[a]] - phys[:, idx[b]])
    return np.stack(cols, axis=1)


def load_manifest_hashes(root: Path) -> dict[str, str]:
    """relpath -> verified sha256 from data/public/grabmyo/manifest_*.csv (if present)."""
    out = {}
    for m in Path(root).parent.glob("manifest_*.csv"):
        with m.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "verified":
                    out[r["relpath"]] = r["sha256_actual"]
    return out


def load_trial(raw_root: Path, session: int, participant: int, gesture: int, trial: int,
               config: str = "bip4", fs_out: float | None = 2000.0,
               hashes: dict[str, str] | None = None) -> Recording:
    raw_root = Path(raw_root)
    rel = record_relpath(session, participant, gesture, trial)
    hea = raw_root / (rel + ".hea")
    h, d = read_digital(hea)
    if h.fs != FS_NATIVE:
        raise ValueError(f"{hea}: fs {h.fs} != {FS_NATIVE}")
    phys = to_physical(h, d)
    units = {s.units for s in h.signals}
    if units != {"mV"}:
        raise ValueError(f"{hea}: unexpected units {units}")
    x = select_channels([s.description for s in h.signals], phys, config)
    fs = h.fs
    if fs_out is not None and fs_out != fs:
        x = resample(x, fs, fs_out)
        fs = fs_out
    dat_rel = rel + ".dat"
    sha = (hashes or {}).get(dat_rel) or hashlib.sha256((raw_root / dat_rel).read_bytes()).hexdigest()
    return Recording(x=x, fs=fs, dataset=DATASET, subject=participant, session=session, trial=trial,
                     label=GRABMYO_TO_UNIFIED.get(gesture, -1), channel_names=lane_names(config), units="mV",
                     source_path=dat_rel, sha256=sha, fs_original=h.fs if fs != h.fs else None,
                     dataset_label=gesture, extra={"channel_config": config})


def load_subset(raw_root: Path, participants, sessions, gestures, trials, config: str = "bip4",
                fs_out: float | None = 2000.0) -> list[Recording]:
    hashes = load_manifest_hashes(raw_root)
    return [load_trial(raw_root, s, p, g, t, config, fs_out, hashes)
            for p in participants for s in sessions for g in gestures for t in trials]


def header_summary(hea: Path) -> dict:
    h = parse_header(hea)
    return {"record": h.record_name, "n_sig": h.n_sig, "fs": h.fs, "n_samples": h.n_samples,
            "signals": [{"name": s.description, "gain_per_mV": s.gain, "baseline": s.baseline,
                         "units": s.units, "adc_res": s.adc_res, "adc_zero": s.adc_zero} for s in h.signals]}
