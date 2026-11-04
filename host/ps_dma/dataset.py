"""GRABMyo raw records -> int16 ESI frames on the PS, numpy only (the board needs no scipy).

The same conversion as the PC pipeline (ml/emgml/datasets/grabmyo.py, resample.py, reference/emg_fixed/esi.py):
channels by header name, bip4 differences, 2048 -> 2000 Hz polyphase FIR (the filter of
scipy.signal.resample_poly: Kaiser beta 5, 2 * 10 * max(up, down) + 1 taps, zero phase), code =
round(x / LSB) clipped to int16. The LSB is a property of the deployed model, not of the data
(configs/e2_ref_fold.json). A frame = [lane0, lane1, lane2, lane3], little-endian int16.

The result is checked against the PC: mg6 frames hash in the configuration (tests/test_dataset.py; on the
board: cli.py). Floating-point rounding can differ between machines; a code near x.5 could change, which is
why the hash is compared before any result is compared with the reference.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import ceil, gcd
from pathlib import Path

import numpy as np

from .wfdb_reader import parse_header, read_digital, to_physical

CONFIG = Path(__file__).resolve().parent / "configs" / "e2_ref_fold.json"
FS_NATIVE = 2048.0
KAISER_BETA = 5.0


class DatasetError(Exception):
    pass


def load_config(path=CONFIG) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def relpath(session: int, participant: int, gesture: int, trial: int) -> str:
    return (f"Session{session}/session{session}_participant{participant}/"
            f"session{session}_participant{participant}_gesture{gesture}_trial{trial}")


def _kaiser_fir(numtaps: int, cutoff: float, beta: float) -> np.ndarray:
    """scipy.signal.firwin(numtaps, cutoff, window=("kaiser", beta)): low-pass, unit gain at DC"""
    m = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = cutoff * np.sinc(cutoff * m) * np.kaiser(numtaps, beta)
    return h / h.sum()


def resample_poly(x: np.ndarray, up: int, down: int, beta: float = KAISER_BETA) -> np.ndarray:
    """x [T, C] -> [ceil(T * up / down), C]: scipy.signal.resample_poly(x, up, down, axis=0, window=("kaiser", beta))"""
    g = gcd(up, down)
    up, down = up // g, down // g
    n_in = x.shape[0]
    n_out = ceil(n_in * up / down)
    max_rate = max(up, down)
    half_len = 10 * max_rate
    h = _kaiser_fir(2 * half_len + 1, 1.0 / max_rate, beta) * up
    # output sample j sits at position t = j * down on the up-sampled time axis; the filter (centre at
    # half_len) weights input sample i, at position i * up, with h[t - i * up + half_len]
    t = np.arange(n_out, dtype=np.int64) * down
    i_lo = np.maximum(np.ceil((t - half_len) / up).astype(np.int64), 0)
    width = (2 * half_len) // up + 2
    idx = i_lo[:, None] + np.arange(width, dtype=np.int64)[None, :]
    k = t[:, None] - idx * up + half_len
    ok = (k >= 0) & (k < h.size) & (idx < n_in)
    w = np.where(ok, h[np.clip(k, 0, h.size - 1)], 0.0)
    return np.einsum("jw,jwc->jc", w, x[np.clip(idx, 0, n_in - 1)])


def load_record(root, session: int, participant: int, gesture: int, trial: int, cfg: dict) -> np.ndarray:
    """raw record -> physical bipolar channels at fs_out, mV, float64 [T, C]"""
    hea = Path(root) / (relpath(session, participant, gesture, trial) + ".hea")
    h = parse_header(hea)
    if h.fs != FS_NATIVE or {s.units for s in h.signals} != {"mV"}:
        raise DatasetError(f"{hea}: fs {h.fs}, units {sorted({s.units for s in h.signals})}")
    _, d = read_digital(hea, h)
    phys = to_physical(h, d)
    names = {s.description: i for i, s in enumerate(h.signals)}
    x = np.stack([phys[:, names[a]] - phys[:, names[b]] for a, b in cfg["channels"]], axis=1)
    return resample_poly(x, int(cfg["fs_out"]), int(FS_NATIVE))


def to_codes(x: np.ndarray, lsb: float) -> tuple[np.ndarray, int]:
    """physical [T, C] -> int16 codes, number of saturated samples"""
    q = np.round(np.asarray(x, dtype=np.float64) / lsb).astype(np.int64)
    sat = int(np.sum((q > 32767) | (q < -32768)))
    return np.clip(q, -32768, 32767).astype(np.int16), sat


@dataclass
class Segment:
    """one DMA transfer = one ESI segment (first frame SOS)"""
    label: str
    frames: np.ndarray                  # int16 [T, 4]
    source: dict

    @property
    def nbytes(self) -> int:
        return self.frames.shape[0] * 8


def load_segment(root, session: int, participant: int, gesture: int, trial: int, cfg: dict, start: int = 0,
                 length: int | None = None, label: str = "") -> Segment:
    codes, sat = to_codes(load_record(root, session, participant, gesture, trial, cfg), cfg["esi_lsb_mV"])
    end = codes.shape[0] if length is None else start + length
    if start < 0 or end > codes.shape[0] or end <= start:
        raise DatasetError(f"frames {start}..{end} outside the record ({codes.shape[0]} frames)")
    return Segment(label or f"s{session}p{participant}g{gesture}t{trial}", codes[start:end],
                   {"session": session, "participant": participant, "gesture": gesture, "trial": trial,
                    "start": start, "length": end - start, "saturated_in_record": sat})


def mg6_segments(root, fixture, cfg: dict) -> list[Segment]:
    """the six E4c segments, in class order (fixture: decisions only, no samples)"""
    fx = json.loads(Path(fixture).read_text(encoding="utf-8"))
    return [load_segment(root, s["session"], s["participant"], s["gesture"], s["trial"], cfg, s["start"], s["length"],
                         s["label"]) for s in fx["segments"]]


def frames_sha256(segments: list[Segment]) -> str:
    h = hashlib.sha256()
    for s in segments:
        h.update(s.frames.astype("<i2").tobytes())
    return h.hexdigest()
