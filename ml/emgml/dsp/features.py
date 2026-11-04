"""Time-domain EMG features: exact definitions (reference for the future fixed-point
model and FPGA feature extractor) and a streaming extractor.

Notation, per channel: x[n] = output of the filter chain, window of N samples
ending at sample t (inclusive): n in [t-N+1, t].

    MAV = (1/N) * sum_{n=t-N+1..t} |x[n]|
    RMS = sqrt( (1/N) * sum_{n=t-N+1..t} x[n]^2 )          sum of squares, divide by N, square root;
                                                            range [0, max|x|]
    d[n] = x[n] - x[n-1]
    WL  = sum over the difference terms of |d[n]|
    ZC  = number of pairs (x[n-1], x[n]) with
              x[n-1] * x[n] < 0          (strict: a sample that is exactly 0 is never a crossing)
          and |x[n] - x[n-1]| >= T_zc   (equality counts)
    SSC = number of centre samples m with
              f[m] = (x[m] - x[m-1]) * (x[m] - x[m+1]),   f[m] > 0 and f[m] >= T_ssc
          (strict f > 0: flat segments never count; equality with T_ssc counts)

    T_zc is in signal units (mV in the float path, LSB in the future integer path);
    T_ssc is in signal units squared.

Which difference terms belong to a window (boundary rule):

    "independent" (textbook, window seen in isolation, N >= 3):
        WL, ZC : n in [t-N+2, t]      (N-1 pairs, all inside the window)
        SSC    : m in [t-N+2, t-1]    (N-2 centres, all neighbours inside the window)
    "continuous" (stream semantics, used for training and the future hardware):
        WL, ZC : n in [t-N+1, t]      (N pairs; the first pair uses x[t-N], the sample just
                                       before the window)
        SSC    : m in [t-N, t-1]      (N centres; uses x[t-N-1]; the centre m = t is not
                                       counted because x[t+1] is in the future)
      Every pair/centre therefore has a fixed "owner" sample index, so a sliding-window
      hardware implementation can add the new term and subtract the term leaving the window.

Window schedule after SOS (sample index s counted from the first sample after SOS):
    first window ends at  s0 = settle_samples + N - 1,  then every step_samples.
    "continuous" needs settle_samples >= 2 so that x[t-N-1] exists and was produced after SOS.
"""

from __future__ import annotations

import numpy as np

FEATURE_ORDER = ("MAV", "RMS", "WL", "ZC", "SSC")


def window_features(seg: np.ndarray, n: int, boundary: str, zc_thr: float, ssc_thr: float,
                    features=FEATURE_ORDER) -> np.ndarray:
    """Reference computation for one window.

    seg: [L, C]; the last n rows are the window. "continuous" needs L >= n + 2
    (two history samples); "independent" uses only the last n rows.
    Returns [C, len(features)] (float64; ZC and SSC are integer counts).
    """
    seg = np.asarray(seg, dtype=np.float64)
    if boundary == "continuous":
        if seg.shape[0] < n + 2:
            raise ValueError("continuous window needs n + 2 samples")
        s = seg[-(n + 2):]                 # s[0] = x[t-N-1], s[1] = x[t-N], s[2..] = window
        w = s[2:]
        prev = s[1:-1]                     # pairs (x[n-1], x[n]) for n in [t-N+1, t]
        cur = s[2:]
        a, b, c = s[0:-2], s[1:-1], s[2:]  # centres m in [t-N, t-1]
    elif boundary == "independent":
        w = seg[-n:]
        prev, cur = w[:-1], w[1:]
        a, b, c = w[:-2], w[1:-1], w[2:]
    else:
        raise ValueError(boundary)
    d = cur - prev
    f = (b - a) * (b - c)
    vals = {
        "MAV": np.mean(np.abs(w), axis=0),
        "RMS": np.sqrt(np.mean(w * w, axis=0)),
        "WL": np.sum(np.abs(d), axis=0),
        "ZC": np.sum((prev * cur < 0) & (np.abs(d) >= zc_thr), axis=0).astype(np.float64),
        "SSC": np.sum((f > 0) & (f >= ssc_thr), axis=0).astype(np.float64),
    }
    return np.stack([vals[k] for k in features], axis=1)


class StreamingFeatures:
    """Window features on a continuous stream, chunk by chunk.

    Keeps the last N + 1 samples and the sample count since SOS; emits the
    features of every scheduled window that ends inside the current chunk.
    """

    def __init__(self, n_ch: int, window: int, step: int, settle: int, boundary: str,
                 zc_thr: float, ssc_thr: float, features=FEATURE_ORDER):
        self.n_ch, self.n, self.step, self.settle = n_ch, window, step, settle
        self.boundary, self.zc_thr, self.ssc_thr, self.features = boundary, zc_thr, ssc_thr, tuple(features)
        self.hist_len = window + 2
        self.reset()

    def reset(self) -> None:
        self.count = 0                                  # samples seen since SOS
        self.hist = np.zeros((0, self.n_ch))

    @property
    def first_end(self) -> int:
        return self.settle + self.n - 1

    def process(self, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """y: [T, C] filtered chunk. Returns (end_index[K], feats[K, C, F]); end_index is
        counted from the first sample after SOS."""
        y = np.asarray(y, dtype=np.float64)
        buf = np.concatenate([self.hist, y], axis=0)
        base = self.count - self.hist.shape[0]          # stream index of buf[0]
        t_lo, t_hi = self.count, self.count + y.shape[0] - 1
        ends = []
        if t_hi >= self.first_end:
            k0 = max(0, -(-(t_lo - self.first_end) // self.step))
            t = self.first_end + k0 * self.step
            while t <= t_hi:
                ends.append(t)
                t += self.step
        feats = np.empty((len(ends), self.n_ch, len(self.features)))
        for i, t in enumerate(ends):
            j = t - base
            feats[i] = window_features(buf[max(0, j + 1 - self.hist_len):j + 1], self.n, self.boundary,
                                       self.zc_thr, self.ssc_thr, self.features)
        self.count += y.shape[0]
        self.hist = buf[-self.hist_len:]
        return np.asarray(ends, dtype=np.int64), feats
