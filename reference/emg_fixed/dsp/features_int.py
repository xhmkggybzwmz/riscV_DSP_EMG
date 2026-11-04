"""Fixed-point window features on POST_DSP int16 samples (RMS for the CNN path; MAV/WL/ZC/SSC kept
for the MLP fallback). Same window schedule and "continuous" boundary as DSP-R0
(emgml/dsp/features.py): window = samples [t-N+1, t]; WL/ZC pairs n in [t-N+1, t]; SSC centres
m in [t-N, t-1]; first window ends at settle + N - 1 after SOS, then every step.

Integer definitions (y = int16, N = window length, R = round(2^24 / N)):
    SUMSQ = sum y^2                    y^2 <= 2^30, N = 400 -> < 2^39  -> 40-bit accumulator
    MS    = round_shift(SUMSQ * R, 24) mean square in LSB^2  (product < 2^57)
    RMS   = isqrt_floor(MS)            uint16 (MS < 2^31)
    SUMABS= sum |y|                    < 2^24 ; MAV = round_shift(SUMABS * R, 24)
    WL    = sum |y[n] - y[n-1]|        |dy| <= 2^16 -> < 2^25
    ZC    = count( (y[n-1] > 0 and y[n] < 0) or (y[n-1] < 0 and y[n] > 0) ) and |dy| >= T_zc   (T_zc in LSB)
    SSC   = count( f = (y[m]-y[m-1]) * (y[m]-y[m+1]) ; f > 0 and f >= T_ssc )               (T_ssc in LSB^2)
Window sums are exact integers, so a sliding implementation (add new term, subtract leaving term)
gives identical results.
"""

from __future__ import annotations

import numpy as np

from ..fixed import check_width, isqrt_floor, round_shift

NAMES = ("RMS", "MAV", "WL", "ZC", "SSC", "SUMSQ")


def recip(n: int) -> int:
    return int(round((1 << 24) / n))


def window_features_int(seg: np.ndarray, n: int, t_zc: int = 0, t_ssc: int = 0) -> np.ndarray:
    """seg: int16 [n + 2, C] (two history samples, then the window). Returns int64 [C, 6] in NAMES order."""
    s = np.asarray(seg).astype(np.int64)
    if s.shape[0] != n + 2:
        raise ValueError("need n + 2 samples")
    w = s[2:]
    prev, cur = s[1:-1], s[2:]
    a, b, c = s[0:-2], s[1:-1], s[2:]
    sumsq = check_width((w * w).sum(axis=0), 41, "SUMSQ")
    ms = round_shift(check_width(sumsq * recip(n), 58, "SUMSQ*R"), 24)
    rms = isqrt_floor(ms)
    sumabs = np.abs(w).sum(axis=0)
    mav = round_shift(sumabs * recip(n), 24)
    d = cur - prev
    wl = np.abs(d).sum(axis=0)
    zc = (((prev > 0) & (cur < 0)) | ((prev < 0) & (cur > 0))) & (np.abs(d) >= t_zc)
    f = (b - a) * (b - c)
    ssc = (f > 0) & (f >= t_ssc)
    return np.stack([rms, mav, wl, zc.sum(axis=0), ssc.sum(axis=0), sumsq], axis=1)


class StreamingFeaturesInt:
    def __init__(self, n_ch: int, window: int = 400, step: int = 100, settle: int = 200, t_zc: int = 0,
                 t_ssc: int = 0):
        self.n_ch, self.n, self.step, self.settle, self.t_zc, self.t_ssc = n_ch, window, step, settle, t_zc, t_ssc
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.hist = np.zeros((0, self.n_ch), dtype=np.int16)

    def process(self, y: np.ndarray, sos: bool = False, gap: bool = False):
        if sos or gap:
            self.reset()
        y = np.asarray(y, dtype=np.int16)
        buf = np.concatenate([self.hist, y])
        base = self.count - self.hist.shape[0]
        first = self.settle + self.n - 1
        ends = [t for t in range(max(first, self.count), self.count + y.shape[0]) if (t - first) % self.step == 0]
        feats = np.array([window_features_int(buf[t - base - self.n - 1:t - base + 1], self.n, self.t_zc, self.t_ssc)
                          for t in ends]).reshape(len(ends), self.n_ch, len(NAMES))
        self.count += y.shape[0]
        self.hist = buf[-(self.n + 2):]
        return np.asarray(ends, dtype=np.int64), feats
