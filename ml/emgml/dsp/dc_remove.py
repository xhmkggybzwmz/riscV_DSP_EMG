"""Causal DC removal: first-order DC blocker (one-pole, one-zero high-pass).

    y[n] = g * (x[n] - x[n-1]) + r * y[n-1],   r = exp(-2*pi*fc/fs),  g = (1 + r) / 2

- zero at z = 1 (exact DC rejection), pole at z = r (0 < r < 1, stable);
- g normalises the gain at fs/2 to exactly 1;
- state per channel: (x[n-1], y[n-1]), both 0 after reset (so a constant input
  x = c gives y[0] = g*c, decaying as r^n; the first samples after SOS are
  therefore discarded by the feature stage's settle_samples).

Uses no future samples and no record-wide mean (which would not be causal).
"""

from __future__ import annotations

import math

import numpy as np

from .stateful import SOSFilter, sos_poles_max_radius, sos_response


def dc_blocker_sos(fc_hz: float, fs: float) -> np.ndarray:
    r = math.exp(-2.0 * math.pi * fc_hz / fs)
    g = (1.0 + r) / 2.0
    return np.array([[g, -g, 0.0, 1.0, -r, 0.0]])


class DCBlocker:
    def __init__(self, fc_hz: float, fs: float, n_ch: int):
        self.fc_hz, self.fs = fc_hz, fs
        self.sos = dc_blocker_sos(fc_hz, fs)
        self.f = SOSFilter(self.sos, n_ch)

    def reset(self) -> None:
        self.f.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.f.process(x)

    def describe(self, freqs_hz=(0.0, 1.0, 5.0, 10.0, 20.0, 50.0, 100.0, 500.0)) -> dict:
        r = -self.sos[0, 4]
        return {"type": "first-order DC blocker (IIR)", "fc_hz": self.fc_hz, "fs": self.fs,
                "pole_r": r, "gain_g": self.sos[0, 0], "sos": self.sos.tolist(),
                "max_pole_radius": sos_poles_max_radius(self.sos),
                "response": sos_response(self.sos, self.fs, [f for f in freqs_hz if f > 0])}
