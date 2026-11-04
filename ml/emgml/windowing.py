"""Recordings -> window dataset through the DSP pipeline (stream semantics).

Every Recording is one stream: SOS at its first sample, so filter state and
the feature history never cross a recording boundary, and no window spans two
recordings. Window labels: GRABMyo records contain a single gesture, so every
window carries the record's label; recordings with mixed per-sample labels
(future Ninapro loader) must be split into single-label recordings before they
get here (mixed windows are never labelled as a pure class).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .dsp import DSPConfig, EMGPreprocessPipeline
from .recording import Recording


@dataclass
class WindowSet:
    X: np.ndarray              # [K, C * F] features (channel-major: ch0 F..., ch1 F...)
    y: np.ndarray              # [K] unified labels
    record: np.ndarray         # [K] index into `records`
    end: np.ndarray            # [K] window end sample (since the recording's SOS)
    feature_names: list[str]   # "F1-F9:MAV", ...
    records: list[tuple]       # Recording.key per record index

    def subset_by_records(self, rec_idx) -> np.ndarray:
        return np.isin(self.record, np.asarray(sorted(rec_idx)))


def make_windows(recs: list[Recording], cfg: DSPConfig, trim_start_s: float = 0.0) -> WindowSet:
    if not recs:
        raise ValueError("no recordings")
    Xs, ys, ri, ends = [], [], [], []
    names = None
    for i, r in enumerate(recs):
        if r.fs != cfg.fs or r.n_ch != cfg.n_ch:
            raise ValueError(f"recording {r.key}: fs/n_ch {r.fs}/{r.n_ch} != config {cfg.fs}/{cfg.n_ch}")
        if r.label < 0:
            raise ValueError(f"recording {r.key} has no unified label")
        out = EMGPreprocessPipeline(cfg).run_record(r.x)
        start = out.window_end - cfg.features.window_samples + 1
        keep = start >= int(round(trim_start_s * r.fs))
        f = out.features[keep]
        Xs.append(f.reshape(f.shape[0], -1))
        ys.append(np.full(f.shape[0], r.label))
        ri.append(np.full(f.shape[0], i))
        ends.append(out.window_end[keep])
        if names is None:
            names = [f"{c}:{k}" for c in r.channel_names for k in out.feature_names]
    return WindowSet(np.concatenate(Xs), np.concatenate(ys), np.concatenate(ri), np.concatenate(ends),
                     names, [r.key for r in recs])


def feature_columns(names: list[str], keep_features) -> np.ndarray:
    keep = set(keep_features)
    return np.array([i for i, n in enumerate(names) if n.split(":")[1] in keep])
