"""Fixed-point DSP front end (E2 golden reference for the future FPGA DSP RTL).

Arithmetic (per lane, per sample; all sections share one biquad engine):

    input            x      : int16 ESI sample (PRE_DSP)
    internal data    u, v   : int27, 8 fraction bits (D_BITS = 27, D_FRAC = 8)  -> range +-2^18 LSB(int16),
                              i.e. 8x headroom over int16 full scale for filter-internal gain
    coefficients     b*, a* : int18 Q2.16 (C_BITS = 18, C_FRAC = 16), range [-2, 2); a0 = 1 implicit
    section (DF1)    acc = b0*u[n] + b1*u[n-1] + b2*u[n-2] - a1*v[n-1] - a2*v[n-2]
                     products 27x18 -> 45 bits; exact sum in a 48-bit accumulator (ACC_BITS = 48)
                     v[n] = sat27( round_shift(acc, 16) )          round half up, saturation counted
    state            u[n-1], u[n-2], v[n-1], v[n-2] per section and lane (int27), 0 after reset
    chain            u_0 = x << 8 ; section k input = section k-1 output ; order DC -> notch -> band-pass
    output           y = sat16( round_shift(v_last, 8) )        int16 POST_DSP, same LSB as the input
    bypass           all stages disabled -> y = x (no rounding)

DC blocker = first-order section (b2 = a2 = 0) on the same engine. Coefficients are the
float DSP-R0 coefficients rounded to Q2.16; the integer values are the specification.
SOS / GAP / reconfiguration clear every state register (ESI v0.1 section 6.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..fixed import Overflow, check_width, quantise_coef

D_BITS, D_FRAC = 27, 8
C_BITS, C_FRAC = 18, 16
ACC_BITS = 48
Y_BITS = 16
D_MIN, D_MAX = -(1 << (D_BITS - 1)), (1 << (D_BITS - 1)) - 1


@dataclass
class FixedSection:
    name: str
    b: tuple[int, int, int]
    a: tuple[int, int]                     # a1, a2 (a0 = 1)
    real: list = field(default_factory=list)


def quantise_sos(name: str, sos: np.ndarray) -> list[FixedSection]:
    out = []
    for i, row in enumerate(np.atleast_2d(sos)):
        if abs(row[3] - 1.0) > 1e-12:
            raise ValueError("a0 must be 1")
        b = tuple(quantise_coef(row[j], C_FRAC, C_BITS) for j in range(3))
        a = tuple(quantise_coef(row[j], C_FRAC, C_BITS) for j in (4, 5))
        out.append(FixedSection(f"{name}{i}", b, a, list(map(float, row))))
    return out


def sections_from_config(cfg) -> list[FixedSection]:
    """Fixed sections in the fixed order from a DSP-R0 DSPConfig (emgml.dsp.config)."""
    from emgml.dsp.bandpass import Bandpass
    from emgml.dsp.dc_remove import dc_blocker_sos
    from emgml.dsp.notch import notch_sos
    secs = []
    if cfg.dc.enabled:
        secs += quantise_sos("dc", dc_blocker_sos(cfg.dc.fc_hz, cfg.fs))
    if cfg.notch.enabled:
        secs += quantise_sos("notch", notch_sos(cfg.notch.f0_hz, cfg.notch.q, cfg.fs))
    if cfg.bandpass.enabled:
        if cfg.bandpass.kind != "iir_butter":
            raise NotImplementedError("fixed-point reference implements the IIR band-pass only")
        bp = Bandpass("iir_butter", cfg.bandpass.low_hz, cfg.bandpass.high_hz, cfg.fs, 1, cfg.bandpass.order)
        secs += quantise_sos("bp", bp.sos)
    return secs


class FixedDSP:
    """Stateful fixed-point DSP over any number of independent lanes ([T, L] int16 in/out)."""

    def __init__(self, sections: list[FixedSection], n_lanes: int):
        self.sections, self.n_lanes = sections, n_lanes
        self.co = [(np.int64(s.b[0]), np.int64(s.b[1]), np.int64(s.b[2]), np.int64(s.a[0]), np.int64(s.a[1]))
                   for s in sections]
        self.reset()

    def reset(self) -> None:
        z = lambda: np.zeros(self.n_lanes, dtype=np.int64)
        self.state = [[z(), z(), z(), z()] for _ in self.sections]      # u1, u2, v1, v2
        self.stats = {"internal_saturations": 0, "output_saturations": 0,
                      "max_abs_acc": [0] * len(self.sections), "max_abs_v": [0] * len(self.sections)}

    @property
    def bypass(self) -> bool:
        return not self.sections

    def process(self, x: np.ndarray, sos: bool = False, gap: bool = False) -> np.ndarray:
        x = np.asarray(x)
        if x.dtype != np.int16:
            raise TypeError("ESI samples must be int16")
        if x.ndim != 2 or x.shape[1] != self.n_lanes:
            raise ValueError(f"expected [T, {self.n_lanes}]")
        if sos or gap:
            self.reset()
        if self.bypass:
            self.last_out_sat = np.zeros(x.shape, dtype=bool)
            return x.copy()
        T = x.shape[0]
        y = np.empty((T, self.n_lanes), dtype=np.int64)
        half_c = np.int64(1 << (C_FRAC - 1))
        half_d = np.int64(1 << (D_FRAC - 1))
        xs = x.astype(np.int64) << D_FRAC
        st, co = self.state, self.co
        max_acc = [np.zeros(self.n_lanes, dtype=np.int64) for _ in self.sections]
        max_v = [np.zeros(self.n_lanes, dtype=np.int64) for _ in self.sections]
        isat = 0
        for t in range(T):
            u0 = xs[t]
            for k in range(len(co)):
                b0, b1, b2, a1, a2 = co[k]
                u1, u2, v1, v2 = st[k]
                acc = b0 * u0 + b1 * u1 + b2 * u2 - a1 * v1 - a2 * v2
                np.maximum(max_acc[k], np.abs(acc), out=max_acc[k])
                v0 = (acc + half_c) >> C_FRAC
                if v0.max() > D_MAX or v0.min() < D_MIN:
                    isat += int(np.sum((v0 > D_MAX) | (v0 < D_MIN)))
                    v0 = np.clip(v0, D_MIN, D_MAX)
                np.maximum(max_v[k], np.abs(v0), out=max_v[k])
                st[k] = [u0, u1, v0, v1]
                u0 = v0
            y[t] = u0
        out = (y + half_d) >> D_FRAC
        self.last_out_sat = (out > 32767) | (out < -32768)      # per sample and lane (ESI OVR)
        osat = int(np.sum(self.last_out_sat))
        for k in range(len(co)):
            m = int(max_acc[k].max()) if T else 0
            if m >= (1 << (ACC_BITS - 1)):
                raise Overflow(f"section {self.sections[k].name}: accumulator exceeds {ACC_BITS} bits")
            self.stats["max_abs_acc"][k] = max(self.stats["max_abs_acc"][k], m)
            self.stats["max_abs_v"][k] = max(self.stats["max_abs_v"][k], int(max_v[k].max()) if T else 0)
        self.stats["internal_saturations"] += isat
        self.stats["output_saturations"] += osat
        return np.clip(out, -32768, 32767).astype(np.int16)

    def describe(self) -> dict:
        return {"D_BITS": D_BITS, "D_FRAC": D_FRAC, "C_BITS": C_BITS, "C_FRAC": C_FRAC, "ACC_BITS": ACC_BITS,
                "Y_BITS": Y_BITS, "rounding": "round half up: (x + 2^(s-1)) >> s, arithmetic shift",
                "sections": [{"name": s.name, "b_int": list(s.b), "a_int": list(s.a), "real": s.real,
                              "b_quantised": [v / (1 << C_FRAC) for v in s.b],
                              "a_quantised": [v / (1 << C_FRAC) for v in s.a]} for s in self.sections]}

    def sos_quantised(self) -> np.ndarray:
        """Quantised coefficients as a float SOS (for frequency-response comparison)."""
        return np.array([[s.b[0], s.b[1], s.b[2], 1 << C_FRAC, s.a[0], s.a[1]] for s in self.sections],
                        dtype=np.float64) / (1 << C_FRAC)
