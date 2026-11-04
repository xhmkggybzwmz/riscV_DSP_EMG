"""Minimal, strict WFDB reader (header + format-16 signal file), numpy only.

Implements the parts of the WFDB header format (PhysioNet "header(5)") that the
GRABMyo records use, and refuses everything else instead of guessing:

    record line : <name> <nsig> <fs>[/<counter freq>[(<base counter>)]] <nsamp> [<time> <date>]
    signal line : <file> <fmt>[x<spf>][:<skew>][+<offset>] <gain>[(<baseline>)][/<units>]
                  <adc res> <adc zero> <init value> <checksum> <block size> <description...>

Supported: format 16 (16-bit two's complement, little endian), all signals in
one file, interleaved sample by sample, no skew, no byte offset, one sample per
frame. Physical value (WFDB semantics): physical = (digital - baseline) / gain,
per channel; when the baseline is omitted it equals the ADC zero; a gain of 0
means "uncalibrated" and is rejected here.

Every load checks: file size == nsamp * nsig * 2, and for every channel the
header's initial value (first sample) and 16-bit checksum (sum of all samples
mod 2^16) against the decoded data. Any mismatch raises WFDBError.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_FMT_RE = re.compile(r"^(\d+)(?:x(\d+))?(?::(\d+))?(?:\+(\d+))?$")
_GAIN_RE = re.compile(r"^([-+]?[0-9.eE+-]+)(?:\(([-+]?\d+)\))?(?:/(.+))?$")


class WFDBError(ValueError):
    pass


@dataclass(frozen=True)
class SignalSpec:
    file_name: str
    fmt: int
    gain: float
    baseline: int
    units: str
    adc_res: int
    adc_zero: int
    init_value: int
    checksum: int
    block_size: int
    description: str


@dataclass(frozen=True)
class WFDBHeader:
    record_name: str
    n_sig: int
    fs: float
    n_samples: int
    signals: tuple[SignalSpec, ...]
    comments: tuple[str, ...] = field(default_factory=tuple)


def _int_field(tok: list[str], i: int, default: int) -> int:
    return int(tok[i]) if len(tok) > i else default


def parse_header(path: Path) -> WFDBHeader:
    lines = Path(path).read_text(encoding="ascii", errors="strict").splitlines()
    comments = tuple(l[1:].strip() for l in lines if l.startswith("#"))
    body = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    if not body:
        raise WFDBError(f"{path}: empty header")
    rec = body[0].split()
    if "/" in rec[0]:
        raise WFDBError(f"{path}: multi-segment records are not supported")
    if len(rec) < 4:
        raise WFDBError(f"{path}: record line needs name, nsig, fs, nsamp")
    name, n_sig = rec[0], int(rec[1])
    fs = float(rec[2].split("/")[0])
    n_samples = int(rec[3])
    if len(body) - 1 != n_sig:
        raise WFDBError(f"{path}: {n_sig} signals declared, {len(body) - 1} signal lines")
    sigs = []
    for line in body[1:]:
        tok = line.split()
        if len(tok) < 3:
            raise WFDBError(f"{path}: short signal line {line!r}")
        m = _FMT_RE.match(tok[1])
        if not m:
            raise WFDBError(f"{path}: cannot parse format {tok[1]!r}")
        fmt, spf, skew, offset = m.groups()
        if fmt != "16" or (spf not in (None, "1")) or (skew not in (None, "0")) or (offset not in (None, "0")):
            raise WFDBError(f"{path}: only format 16, 1 sample/frame, no skew/offset is supported ({tok[1]})")
        g = _GAIN_RE.match(tok[2])
        if not g:
            raise WFDBError(f"{path}: cannot parse gain field {tok[2]!r}")
        gain = float(g.group(1))
        if gain == 0.0:
            raise WFDBError(f"{path}: gain 0 (uncalibrated) is not supported")
        adc_res = _int_field(tok, 3, 0)
        adc_zero = _int_field(tok, 4, 0)
        baseline = int(g.group(2)) if g.group(2) is not None else adc_zero
        units = g.group(3) or "mV"            # WFDB default physical unit
        sigs.append(SignalSpec(file_name=tok[0], fmt=16, gain=gain, baseline=baseline, units=units,
                               adc_res=adc_res, adc_zero=adc_zero, init_value=_int_field(tok, 5, 0),
                               checksum=_int_field(tok, 6, 0), block_size=_int_field(tok, 7, 0),
                               description=" ".join(tok[8:])))
    files = {s.file_name for s in sigs}
    if len(files) != 1:
        raise WFDBError(f"{path}: signals spread over several files {sorted(files)} (not supported)")
    return WFDBHeader(record_name=name, n_sig=n_sig, fs=fs, n_samples=n_samples,
                      signals=tuple(sigs), comments=comments)


def read_digital(header_path: Path, header: WFDBHeader | None = None) -> tuple[WFDBHeader, np.ndarray]:
    """Return (header, digital samples [n_samples, n_sig] as int16), fully checked."""
    header_path = Path(header_path)
    h = header or parse_header(header_path)
    dat = header_path.with_name(h.signals[0].file_name)
    raw = dat.read_bytes()
    expected = h.n_samples * h.n_sig * 2
    if len(raw) != expected:
        raise WFDBError(f"{dat}: {len(raw)} bytes, header implies {expected}")
    d = np.frombuffer(raw, dtype="<i2").reshape(h.n_samples, h.n_sig)
    for c, s in enumerate(h.signals):
        if int(d[0, c]) != s.init_value:
            raise WFDBError(f"{dat}: channel {c} initial value {int(d[0, c])} != header {s.init_value}")
        csum = int(d[:, c].astype(np.int64).sum()) & 0xFFFF
        if csum >= 0x8000:
            csum -= 0x10000
        hsum = s.checksum if s.checksum < 0x8000 else s.checksum - 0x10000
        if csum != hsum:
            raise WFDBError(f"{dat}: channel {c} checksum {csum} != header {s.checksum}")
    return h, d


def to_physical(h: WFDBHeader, digital: np.ndarray) -> np.ndarray:
    """physical = (digital - baseline) / gain, per channel (float64)."""
    gain = np.array([s.gain for s in h.signals], dtype=np.float64)
    base = np.array([s.baseline for s in h.signals], dtype=np.float64)
    return (digital.astype(np.float64) - base) / gain
