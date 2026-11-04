"""EMGPreprocessPipeline: DSP-R0 float reference, stream semantics.

    input chunk x[T, C] ──> [DC blocker] ──> [notch] ──> [band-pass] ──> waveform y[T, C]
                                                                    └──> window features

Order is fixed (config.STAGE_ORDER); disabled stages are pass-through.

State contract (mirrors ESI, docs/system_design_extend.md section 6):
    process(x, sos=True)   clear all state (every filter and the feature history)
                           before x[0]; x[0] is stream sample 0
    process(x, gap=True)   GAP with gap_policy "reset": identical to SOS
    process(x)             continue: the chunk boundary changes nothing
    reconfigure(cfg)       new parameters always imply a reset (SOS)
    reset()                explicit reset (stream restarts with the next chunk)

Output sample n of the waveform corresponds to input sample n (causal; the
filters' own delay is part of the signal, as in hardware). Feature windows
are reported with their end index counted from the last SOS.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bandpass import Bandpass
from .config import DSPConfig
from .dc_remove import DCBlocker
from .features import StreamingFeatures
from .notch import Notch


@dataclass
class PipelineOutput:
    waveform: np.ndarray        # [T, C]
    window_end: np.ndarray      # [K] sample index since SOS of each window's last sample
    features: np.ndarray        # [K, C, F]
    feature_names: tuple[str, ...]


class EMGPreprocessPipeline:
    def __init__(self, cfg: DSPConfig):
        self.reconfigure(cfg)

    def reconfigure(self, cfg: DSPConfig) -> None:
        cfg.validate()
        self.cfg = cfg
        c = cfg
        self.stages = []
        if c.dc.enabled:
            self.stages.append(("dc", DCBlocker(c.dc.fc_hz, c.fs, c.n_ch)))
        if c.notch.enabled:
            self.stages.append(("notch", Notch(c.notch.f0_hz, c.notch.q, c.fs, c.n_ch)))
        if c.bandpass.enabled:
            b = c.bandpass
            self.stages.append(("bandpass", Bandpass(b.kind, b.low_hz, b.high_hz, c.fs, c.n_ch,
                                                     b.order, b.numtaps, b.window)))
        f = c.features
        self.feat = StreamingFeatures(c.n_ch, f.window_samples, f.step_samples, f.settle_samples,
                                      f.boundary, f.zc_threshold, f.ssc_threshold, f.features)
        self.reset()

    def reset(self) -> None:
        for _, st in self.stages:
            st.reset()
        self.feat.reset()

    def filter(self, x: np.ndarray) -> np.ndarray:
        y = np.asarray(x, dtype=np.float64)
        for _, st in self.stages:
            y = st.process(y)
        return y

    def process(self, x: np.ndarray, sos: bool = False, gap: bool = False) -> PipelineOutput:
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.cfg.n_ch:
            raise ValueError(f"expected [T, {self.cfg.n_ch}], got {x.shape}")
        if sos or gap:                     # gap_policy "reset" (the only one defined in ESI v0)
            self.reset()
        y = self.filter(x)
        ends, feats = self.feat.process(y)
        return PipelineOutput(y, ends, feats, tuple(self.cfg.features.features))

    def run_record(self, x: np.ndarray) -> PipelineOutput:
        """One independent recording = one stream starting with SOS."""
        return self.process(x, sos=True)

    def describe(self) -> dict:
        return {"config": self.cfg.to_dict(),
                "stages": {name: st.describe() for name, st in self.stages}}
