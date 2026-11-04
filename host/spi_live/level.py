"""Proportional control from LEVEL_ENERGY of esi_spi_src_axil: RMS of lane 0 after the DSP over 128 frames (64 ms)."""

from __future__ import annotations

import json
import math


def rms_from_energy(energy: int) -> float:
    """LEVEL_ENERGY = sum(y^2) >> 6 over 128 frames"""
    return math.sqrt(energy * 64.0 / 128.0)


class ActivityMeter:
    """intensity 0..1 between the rest and the maximum activity of this person, with a light first-order smoothing.
    Without a calibration file it runs uncalibrated: rest = lowest level seen, max = highest level seen (never shrinks)."""

    def __init__(self, rest_rms: float | None = None, max_rms: float | None = None, alpha: float = 0.4):
        if (rest_rms is None) != (max_rms is None):
            raise ValueError("give both rest_rms and max_rms, or neither")
        if rest_rms is not None and max_rms <= rest_rms:
            raise ValueError("max_rms must exceed rest_rms")
        self.calibrated = rest_rms is not None
        self.rest, self.max = rest_rms, max_rms
        self.alpha = alpha
        self.rms = 0.0
        self.smooth = None

    @classmethod
    def from_json(cls, path: str) -> "ActivityMeter":
        with open(path, encoding="utf-8") as f:
            lv = json.load(f)["level"]
        return cls(lv["rest_rms"], lv["max_rms"])

    def update(self, energy: int) -> float:
        self.rms = rms_from_energy(energy)
        if not self.calibrated:
            self.rest = self.rms if self.rest is None else min(self.rest, self.rms)
            self.max = self.rms if self.max is None else max(self.max, self.rms)
        span = self.max - self.rest
        x = 0.0 if span <= 0 else min(1.0, max(0.0, (self.rms - self.rest) / span))
        self.smooth = x if self.smooth is None else self.smooth + self.alpha * (x - self.smooth)
        return self.smooth
