"""Unified recording structure shared by all dataset loaders.

A Recording is one continuous, gap-free acquisition segment (GRABMyo: one
trial file; Ninapro: one repetition cut from an exercise file; EMG1 capture:
one run between discontinuities). Each Recording starts a new stream (SOS) for
the DSP pipeline: filter state never carries over from one Recording to the next.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np


@dataclass
class Recording:
    x: np.ndarray                      # [T, N_CH], float64 physical units (or int16 when quantised)
    fs: float                          # sampling rate of x (Hz)
    dataset: str
    subject: int
    session: int
    trial: int                         # trial (GRABMyo) or repetition (Ninapro)
    label: int                         # unified class id (labels.py); -1 = unlabelled
    channel_names: list[str]           # lane order, e.g. ["F1-F9", ...] or ["F1", ...]
    units: str                         # "mV", or "LSB" when quantised
    source_path: str                   # path of the raw file (repo-relative when possible)
    sha256: str                        # SHA-256 of the raw signal file
    fs_original: float | None = None   # before resampling (None: not resampled)
    lsb: float | None = None           # physical value of one LSB when x is integer
    dataset_label: int | None = None   # label id in the dataset's own numbering
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.x.ndim != 2:
            raise ValueError(f"x must be [T, N_CH], got shape {self.x.shape}")
        if len(self.channel_names) != self.x.shape[1]:
            raise ValueError("channel_names does not match the number of columns")

    @property
    def n_samples(self) -> int:
        return int(self.x.shape[0])

    @property
    def n_ch(self) -> int:
        return int(self.x.shape[1])

    @property
    def key(self) -> tuple[str, int, int, int, int]:
        """Identity of the recording for splits and leakage checks:
        (dataset, subject, session, trial, label). The label is part of the key
        because GRABMyo numbers trials 1..7 separately for every gesture."""
        return (self.dataset, self.subject, self.session, self.trial, self.label)

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("x")
        d["n_samples"] = self.n_samples
        return d
