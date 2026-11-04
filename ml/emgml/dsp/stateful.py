"""Stateful multi-channel filters with explicit reset (float reference).

Every filter keeps one state vector per channel. `reset()` sets the state to
zero (the same initial condition the hardware will use after SOS/GAP/reset),
`process(x)` filters a chunk x[T, C] and keeps the state, so feeding a record
in arbitrary chunks gives the same result as one call (up to float rounding).
"""

from __future__ import annotations

import warnings

import numpy as np
from scipy import signal


class SOSFilter:
    """Cascade of biquads (scipy SOS layout: rows [b0 b1 b2 a0 a1 a2], a0 = 1)."""

    def __init__(self, sos: np.ndarray, n_ch: int):
        self.sos = np.atleast_2d(np.asarray(sos, dtype=np.float64))
        if self.sos.shape[1] != 6:
            raise ValueError("SOS must have 6 columns")
        self.n_ch = n_ch
        self.reset()

    def reset(self) -> None:
        self.zi = np.zeros((self.sos.shape[0], self.n_ch, 2))

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.n_ch:
            raise ValueError(f"expected [T, {self.n_ch}], got {x.shape}")
        if x.shape[0] == 0:
            return x.copy()
        y, self.zi = signal.sosfilt(self.sos, x.T, axis=-1, zi=self.zi)
        return y.T


class FIRFilter:
    """Direct-form FIR, causal, zero initial history."""

    def __init__(self, taps: np.ndarray, n_ch: int):
        self.taps = np.asarray(taps, dtype=np.float64)
        self.n_ch = n_ch
        self.reset()

    def reset(self) -> None:
        self.zi = np.zeros((self.n_ch, len(self.taps) - 1))

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != self.n_ch:
            raise ValueError(f"expected [T, {self.n_ch}], got {x.shape}")
        if x.shape[0] == 0:
            return x.copy()
        y, self.zi = signal.lfilter(self.taps, [1.0], x.T, axis=-1, zi=self.zi)
        return y.T


def sos_response(sos: np.ndarray, fs: float, freqs_hz) -> dict:
    """Magnitude (dB) and group delay (samples) of an SOS cascade at given frequencies."""
    f = np.asarray(freqs_hz, dtype=np.float64)
    _, h = signal.sosfreqz(sos, worN=f, fs=fs)
    # group delay of the cascade = sum of the sections' group delays
    gd = np.zeros_like(f)
    with warnings.catch_warnings():
        # exactly at a zero on the unit circle (notch centre) the group delay is undefined
        warnings.simplefilter("ignore", UserWarning)
        for sec in np.atleast_2d(sos):
            _, g = signal.group_delay((sec[:3], sec[3:]), w=f, fs=fs)
            gd += g
    mag_db = 20 * np.log10(np.maximum(np.abs(h), 1e-300))
    gd = np.where(mag_db < -100.0, np.nan, gd)
    return {"freq_hz": f.tolist(), "mag_db": mag_db.tolist(),
            "group_delay_samples": [None if np.isnan(g) else float(g) for g in gd]}


def fir_response(taps: np.ndarray, fs: float, freqs_hz) -> dict:
    f = np.asarray(freqs_hz, dtype=np.float64)
    _, h = signal.freqz(taps, worN=f, fs=fs)
    return {"freq_hz": f.tolist(), "mag_db": (20 * np.log10(np.maximum(np.abs(h), 1e-300))).tolist(),
            "group_delay_samples": [(len(taps) - 1) / 2] * len(f)}


def sos_poles_max_radius(sos: np.ndarray) -> float:
    """Largest pole radius over all sections (< 1 means stable)."""
    r = 0.0
    for sec in np.atleast_2d(sos):
        p = np.roots(sec[3:])
        if p.size:
            r = max(r, float(np.max(np.abs(p))))
    return r
