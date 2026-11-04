"""Sampling-rate adaptation for public datasets (PC only, not part of the FPGA DSP path).

GRABMyo: 2048 Hz -> 2000 Hz = 125/128, scipy.signal.resample_poly with its
default anti-aliasing FIR (Kaiser window, beta = 5.0, cutoff at the lower of
the two Nyquist rates, length 2 * 10 * max(up, down) + 1 taps at the
up-sampled rate). resample_poly compensates the filter delay (zero-phase), so
it looks at future samples *inside one recording*; this is acceptable only
because it is an offline input adaptation applied per recording (no data of
another recording, split or subject is involved). Real 2 kHz SPI data needs no
resampling. Output length = ceil(T * up / down).
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

KAISER_BETA = 5.0


def resample_ratio(fs_in: float, fs_out: float) -> tuple[int, int]:
    frac = math_fraction(fs_out / fs_in)
    return frac


def math_fraction(r: float, max_den: int = 4096) -> tuple[int, int]:
    from fractions import Fraction
    f = Fraction(r).limit_denominator(max_den)
    return f.numerator, f.denominator


def resample(x: np.ndarray, fs_in: float, fs_out: float) -> np.ndarray:
    """x: [T, C] -> [ceil(T*up/down), C]."""
    if fs_in == fs_out:
        return np.asarray(x, dtype=np.float64).copy()
    up, down = resample_ratio(fs_in, fs_out)
    if abs(fs_in * up / down - fs_out) > 1e-9:
        raise ValueError(f"cannot express {fs_in} -> {fs_out} as a small rational ratio")
    y = signal.resample_poly(np.asarray(x, dtype=np.float64), up, down, axis=0,
                             window=("kaiser", KAISER_BETA))
    expected = math.ceil(x.shape[0] * up / down)
    if y.shape[0] != expected:
        raise AssertionError(f"resample length {y.shape[0]} != {expected}")
    return y


def describe(fs_in: float, fs_out: float) -> dict:
    up, down = resample_ratio(fs_in, fs_out)
    return {"method": "scipy.signal.resample_poly", "up": up, "down": down,
            "window": ["kaiser", KAISER_BETA], "filter_taps": 2 * 10 * max(up, down) + 1,
            "phase": "zero-phase within one recording (offline input adaptation only)"}
