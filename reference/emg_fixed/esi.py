"""ESI INT16 input contract (docs/system_design_extend.md section 6, docs/ml/e2_fixed_point_spec.md).

A lane sample is a 16-bit two's-complement integer q in [-32768, 32767]. Its physical
meaning comes from the source calibration record:

    physical = (q - offset[lane]) * lsb           (offset in LSB, lsb in source units / LSB)
    q        = sat16(round_half_even(physical / lsb) + offset[lane])   (conversion done by the source adapter)

Saturation at the source is counted (ESI OVR flag). Lanes >= n_ch carry 0.

GRABMyo (replay, PRE_DSP): units mV, offset 0, lsb = P99.99(|x|) / 16384 over the raw (pre-DSP)
samples of the TRAINING records of the fold only (never test records).
STM32 MyoWare (SPI): 12-bit ADC codes; per-lane OFFSET (static ADC offset, measured) and SCALE are
configuration registers of the EMG1 parser; values are NOT defined yet (RAW vs ENV undecided).
GRABMyo weights must not be assumed valid for MyoWare data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

FLAG_SOS, FLAG_GAP, FLAG_OVR, FLAG_UNDERRUN = 1 << 0, 1 << 1, 1 << 2, 1 << 3
SRC_REPLAY, SRC_SPI, SRC_PATTERN = 0, 1, 2
FLAG_POST_DSP = 1 << 6
NCH_MAX = 8


def src_bits(src: int) -> int:
    return (src & 3) << 4


@dataclass
class SourceCalibration:
    source: str                       # "GRABMyo-1.1.0 replay" / "STM32 MyoWare SPI"
    stage: str                        # "PRE_DSP" or "POST_DSP"
    units: str                        # physical unit of (q - offset) * lsb
    lsb: float
    n_ch: int
    lane_names: list[str]
    offset: list[int] = field(default_factory=list)      # per lane, in LSB
    rule: str = ""
    calibration_records: list = field(default_factory=list)

    def __post_init__(self):
        if not self.offset:
            self.offset = [0] * self.n_ch
        if self.stage not in ("PRE_DSP", "POST_DSP"):
            raise ValueError(self.stage)

    def to_dict(self) -> dict:
        return asdict(self)


def grabmyo_lsb(train_raw: np.ndarray, headroom_code: int = 16384) -> float:
    """LSB from TRAINING raw samples only: P99.99 of |x| maps to code 16384 (half scale)."""
    return float(np.percentile(np.abs(train_raw), 99.99) / headroom_code)


def to_esi(x_phys: np.ndarray, cal: SourceCalibration) -> tuple[np.ndarray, int]:
    """Physical [T, C] -> int16 codes [T, C]; returns (codes, number of saturated samples)."""
    q = np.round(np.asarray(x_phys, dtype=np.float64) / cal.lsb).astype(np.int64) + np.asarray(cal.offset)
    sat = int(np.sum((q > 32767) | (q < -32768)))
    return np.clip(q, -32768, 32767).astype(np.int16), sat


def from_esi(q: np.ndarray, cal: SourceCalibration) -> np.ndarray:
    return (np.asarray(q, dtype=np.int64) - np.asarray(cal.offset)) * cal.lsb


@dataclass
class Frames:
    """A run of ESI frames: seq[T] (uint32), flags[T] (uint16), lanes[T, 8] (int16)."""
    seq: np.ndarray
    flags: np.ndarray
    lanes: np.ndarray


def make_frames(codes: np.ndarray, src: int, seq0: int = 0, sos_at=(0,), gap_at=(), post_dsp=False,
                seq_jump_at: dict | None = None) -> Frames:
    T, C = codes.shape
    lanes = np.zeros((T, NCH_MAX), dtype=np.int16)
    lanes[:, :C] = codes
    flags = np.full(T, src_bits(src) | (FLAG_POST_DSP if post_dsp else 0), dtype=np.int64)
    for i in sos_at:
        flags[i] |= FLAG_SOS
    for i in gap_at:
        flags[i] |= FLAG_GAP
    seq = (seq0 + np.arange(T, dtype=np.int64))
    for i, jump in (seq_jump_at or {}).items():
        seq[i:] += jump
    return Frames(seq & 0xFFFFFFFF, flags & 0xFFFF, lanes)


def reset_points(fr: Frames) -> np.ndarray:
    """Frame indices where the stream state must be cleared (SOS or GAP; gap_policy = reset)."""
    return np.flatnonzero(fr.flags & (FLAG_SOS | FLAG_GAP))
