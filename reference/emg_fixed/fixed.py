"""Integer arithmetic primitives of the E2 fixed-point reference (bit-exact spec).

Every value is a Python/numpy int64 that must fit a declared two's-complement
(or unsigned) width. The rules below are the hardware rules; the reference
never computes in unlimited precision and trims at the end.

    round_shift(x, s)   s >= 1: (x + 2^(s-1)) >> s      arithmetic right shift (floor) after adding half
                        = round half up (towards +inf) for positive AND negative x
                        s == 0: x unchanged
    sat_signed(x, b)    clamp to [-2^(b-1), 2^(b-1) - 1]
    sat_unsigned(x, b)  clamp to [0, 2^b - 1]
    check_width(x, b)   accumulator / product width assertion: raises Overflow if any value does not
                        fit b bits signed. Accumulators are sized so that this never fires; a firing
                        assertion means the spec (not the data) is wrong.
"""

from __future__ import annotations

import numpy as np


class Overflow(ArithmeticError):
    pass


def as_i64(x) -> np.ndarray:
    a = np.asarray(x)
    if a.dtype.kind not in "iu":
        raise TypeError(f"integer array expected, got {a.dtype}")
    return a.astype(np.int64)


def round_shift(x, s: int) -> np.ndarray:
    x = as_i64(x)
    if s < 0:
        raise ValueError("negative shift")
    if s == 0:
        return x
    return (x + (np.int64(1) << (s - 1))) >> s


def sat_signed(x, bits: int) -> np.ndarray:
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return np.clip(as_i64(x), lo, hi)


def sat_unsigned(x, bits: int) -> np.ndarray:
    return np.clip(as_i64(x), 0, (1 << bits) - 1)


def check_width(x, bits: int, what: str = "value") -> np.ndarray:
    x = as_i64(x)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if x.size and (x.min() < lo or x.max() > hi):
        raise Overflow(f"{what}: range [{x.min()}, {x.max()}] exceeds {bits}-bit signed")
    return x


def count_sat_signed(x, bits: int) -> int:
    x = as_i64(x)
    return int(np.sum((x < -(1 << (bits - 1))) | (x > (1 << (bits - 1)) - 1)))


def quantise_coef(c: float, frac: int, bits: int) -> int:
    """Round a real coefficient to `bits`-bit signed with `frac` fraction bits (round half away from 0
    is NOT used: np.round rounds half to even; coefficients are computed offline, the integer is the spec)."""
    q = int(np.round(c * (1 << frac)))
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if not lo <= q <= hi:
        raise Overflow(f"coefficient {c} does not fit Q{bits - frac}.{frac} ({bits} bits)")
    return q


def isqrt_floor(x) -> np.ndarray:
    """floor(sqrt(x)) for non-negative int64 values (exact; hardware: digit-by-digit square root)."""
    x = as_i64(x)
    if x.size and x.min() < 0:
        raise ValueError("isqrt of negative value")
    r = np.floor(np.sqrt(x.astype(np.float64))).astype(np.int64)
    # correct the float estimate (exact for x < 2^52 after the adjustment)
    r = np.where(r * r > x, r - 1, r)
    r = np.where((r + 1) * (r + 1) <= x, r + 1, r)
    return r


def to_hex(v: int, bits: int) -> str:
    """Two's-complement hex of a signed value with `bits` bits (width rounded up to nibbles)."""
    n = (bits + 3) // 4
    return format(int(v) & ((1 << (4 * n)) - 1), f"0{n}X")
