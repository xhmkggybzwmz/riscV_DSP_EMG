"""Causal band-pass: Butterworth IIR (SOS) or windowed-sinc FIR candidate.

IIR: scipy.signal.butter(N, [low, high], 'bandpass', output='sos'), i.e. a
     band-pass of order 2N realised as N biquads; zero state after reset.
FIR: scipy.signal.firwin(numtaps, [low, high], pass_zero=False, window=...),
     linear phase, group delay (numtaps - 1) / 2 samples; zero history after reset.

The FIR variant is a candidate for a multiplier-friendly FPGA pipeline; which
structure (if any) goes into hardware is decided after E1b.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from .stateful import FIRFilter, SOSFilter, fir_response, sos_poles_max_radius, sos_response


class Bandpass:
    def __init__(self, kind: str, low_hz: float, high_hz: float, fs: float, n_ch: int,
                 order: int = 2, numtaps: int = 101, window: str = "hamming"):
        self.kind, self.low_hz, self.high_hz, self.fs = kind, low_hz, high_hz, fs
        self.order, self.numtaps, self.window = order, numtaps, window
        if kind == "iir_butter":
            self.sos = signal.butter(order, [low_hz, high_hz], btype="bandpass", fs=fs, output="sos")
            self.f = SOSFilter(self.sos, n_ch)
        elif kind == "fir_window":
            self.taps = signal.firwin(numtaps, [low_hz, high_hz], pass_zero=False, window=window, fs=fs)
            self.f = FIRFilter(self.taps, n_ch)
        else:
            raise ValueError(f"unknown band-pass kind {kind}")

    def reset(self) -> None:
        self.f.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.f.process(x)

    def describe(self) -> dict:
        freqs = [5.0, 10.0, self.low_hz, 50.0, 100.0, 200.0, 300.0, self.high_hz, 600.0, 900.0]
        freqs = sorted({f for f in freqs if 0 < f < self.fs / 2})
        if self.kind == "iir_butter":
            return {"type": f"Butterworth band-pass IIR, prototype order {self.order} "
                            f"(filter order {2 * self.order}, {self.sos.shape[0]} biquads)",
                    "low_hz": self.low_hz, "high_hz": self.high_hz, "fs": self.fs, "sos": self.sos.tolist(),
                    "max_pole_radius": sos_poles_max_radius(self.sos),
                    "response": sos_response(self.sos, self.fs, freqs)}
        return {"type": f"FIR window-sinc band-pass, {self.numtaps} taps, {self.window} window",
                "low_hz": self.low_hz, "high_hz": self.high_hz, "fs": self.fs, "taps": self.taps.tolist(),
                "group_delay_samples": (self.numtaps - 1) / 2,
                "response": fir_response(self.taps, self.fs, freqs)}
