"""Causal IIR notch (second order), scipy.signal.iirnotch design, SOS form.

H(z) = b(z)/a(z) with zeros on the unit circle at +-w0 (w0 = 2*pi*f0/fs) and
poles at the same angle with radius set by Q (-3 dB bandwidth = f0 / Q).
One biquad; state per channel; zero state after reset.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from .stateful import SOSFilter, sos_poles_max_radius, sos_response


def notch_sos(f0_hz: float, q: float, fs: float) -> np.ndarray:
    b, a = signal.iirnotch(f0_hz, q, fs=fs)
    return np.concatenate([b, a]).reshape(1, 6) / a[0]


class Notch:
    def __init__(self, f0_hz: float, q: float, fs: float, n_ch: int):
        self.f0_hz, self.q, self.fs = f0_hz, q, fs
        self.sos = notch_sos(f0_hz, q, fs)
        self.f = SOSFilter(self.sos, n_ch)

    def reset(self) -> None:
        self.f.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.f.process(x)

    def describe(self) -> dict:
        f0 = self.f0_hz
        freqs = [f0 - 10, f0 - 5, f0 - 2, f0, f0 + 2, f0 + 5, f0 + 10, 2 * f0, 100.0, 300.0]
        return {"type": "IIR notch (iirnotch, 1 biquad)", "f0_hz": f0, "q": self.q, "fs": self.fs,
                "bandwidth_hz": f0 / self.q, "sos": self.sos.tolist(),
                "max_pole_radius": sos_poles_max_radius(self.sos),
                "response": sos_response(self.sos, self.fs, [f for f in freqs if 0 < f < self.fs / 2])}
