"""Loader for EMG1 captures recorded by host/emg_capture/emg_host.py (raw packet stream files).

Reuses the reference parser host/emg_capture/emg_inspect.py (magic, header checks,
CRC-16/CCITT-FALSE, sample-major interleaving), so there is a single EMG1 definition.

A capture is split into one Recording per gap-free run: a sample_index
discontinuity (lost or corrupted packets) ends a run, exactly where a real-time
ESI source would raise GAP. Samples stay in ADC counts (units "ADC_LSB"):
the conversion of the STM32 12-bit codes to ESI int16 is NOT fixed yet (it needs
the MyoWare RAW/ENV decision and a measured static ADC offset; see
docs/system_design_extend.md section 6.2), and recordings are unlabelled (-1).
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import numpy as np

from ..recording import Recording

REPO = Path(__file__).resolve().parents[3]
_INSPECT = REPO / "host" / "emg_capture" / "emg_inspect.py"


def _emg_inspect():
    name = "emg_inspect_ref"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _INSPECT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_capture(path: Path, subject: int = 0, session: int = 0) -> list[Recording]:
    ei = _emg_inspect()
    path = Path(path)
    result = ei.parse_file(path)
    if not result.packets:
        raise ValueError(f"{path}: no valid EMG1 packets")
    first = result.packets[0]
    for p in result.packets:
        if (p.sample_rate_hz, p.channel_count, p.sample_format) != (first.sample_rate_hz, first.channel_count,
                                                                    first.sample_format):
            raise ValueError(f"{path}: stream configuration changes inside the file")
    breaks = sorted({d[0] for d in result.discontinuities})          # packet index starting a new run
    bounds = [0] + breaks + [len(result.packets)]
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    recs = []
    for run, (a, b) in enumerate(zip(bounds[:-1], bounds[1:])):
        if a == b:
            continue
        x = np.vstack([ei.decode_payload(p) for p in result.packets[a:b]]).astype(np.float64)
        recs.append(Recording(x=x, fs=float(first.sample_rate_hz), dataset="EMG1-capture", subject=subject,
                              session=session, trial=run, label=-1,
                              channel_names=[f"ADC{c}" for c in range(first.channel_count)], units="ADC_LSB",
                              source_path=path.as_posix(), sha256=sha,
                              extra={"first_sample_index": result.packets[a].sample_index,
                                     "sample_format": first.sample_format, "bad_crc_packets": result.bad_crc_packets,
                                     "discontinuities": len(result.discontinuities)}))
    return recs
