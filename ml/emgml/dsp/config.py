"""DSP-R0 configuration: one dataclass tree, saved/loaded as JSON or TOML.

The processing order is fixed (STAGE_ORDER) and is part of the saved config,
so training and (future) hardware cannot silently use a different order.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

STAGE_ORDER = ("dc", "notch", "bandpass")
FEATURES_ALL = ("MAV", "RMS", "WL", "ZC", "SSC")


@dataclass
class DCConfig:
    enabled: bool = True
    fc_hz: float = 5.0             # -3 dB corner of the first-order DC blocker


@dataclass
class NotchConfig:
    enabled: bool = False
    f0_hz: float = 50.0
    q: float = 30.0


@dataclass
class BandpassConfig:
    enabled: bool = True
    kind: str = "iir_butter"       # "iir_butter" (SOS) or "fir_window"
    low_hz: float = 20.0
    high_hz: float = 450.0
    order: int = 2                 # Butterworth prototype order N (band-pass filter order is 2N)
    numtaps: int = 101             # FIR only (odd: linear phase, integer group delay)
    window: str = "hamming"        # FIR only


@dataclass
class FeatureConfig:
    window_samples: int = 400      # 200 ms at 2 kHz
    step_samples: int = 100        # 50 ms at 2 kHz
    settle_samples: int = 200      # samples after SOS before the first window may start
    boundary: str = "continuous"   # "continuous" (stream semantics) or "independent" (textbook window)
    features: tuple[str, ...] = FEATURES_ALL
    zc_threshold: float = 0.0      # signal units (mV in the float path)
    ssc_threshold: float = 0.0     # signal units squared


@dataclass
class DSPConfig:
    fs: float = 2000.0
    n_ch: int = 4
    dc: DCConfig = field(default_factory=DCConfig)
    notch: NotchConfig = field(default_factory=NotchConfig)
    bandpass: BandpassConfig = field(default_factory=BandpassConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    gap_policy: str = "reset"      # ESI GAP handling: "reset" = same as SOS (docs/system_design_extend.md 6)
    stage_order: tuple[str, ...] = STAGE_ORDER

    def validate(self) -> None:
        if tuple(self.stage_order) != STAGE_ORDER:
            raise ValueError(f"stage order is fixed to {STAGE_ORDER}, got {self.stage_order}")
        if self.fs <= 0 or self.n_ch < 1:
            raise ValueError("fs and n_ch must be positive")
        nyq = self.fs / 2
        if self.dc.enabled and not 0 < self.dc.fc_hz < nyq:
            raise ValueError("dc.fc_hz out of range")
        if self.notch.enabled and not (0 < self.notch.f0_hz < nyq and self.notch.q > 0):
            raise ValueError("notch parameters out of range")
        b = self.bandpass
        if b.enabled:
            if b.kind not in ("iir_butter", "fir_window"):
                raise ValueError(f"unknown band-pass kind {b.kind}")
            if not 0 < b.low_hz < b.high_hz < nyq:
                raise ValueError("band-pass edges must satisfy 0 < low < high < fs/2")
            if b.kind == "fir_window" and b.numtaps % 2 == 0:
                raise ValueError("FIR numtaps must be odd")
        f = self.features
        if f.boundary not in ("continuous", "independent"):
            raise ValueError(f"unknown feature boundary {f.boundary}")
        if f.window_samples < 3 or f.step_samples < 1:
            raise ValueError("window must be >= 3 samples, step >= 1")
        if f.boundary == "continuous" and f.settle_samples < 2:
            raise ValueError("continuous features need settle_samples >= 2 (history samples)")
        unknown = set(f.features) - set(FEATURES_ALL)
        if unknown:
            raise ValueError(f"unknown features {sorted(unknown)}")
        if self.gap_policy != "reset":
            raise ValueError("only gap_policy = 'reset' is defined in ESI v0")

    # ---- serialisation ----
    def to_dict(self) -> dict:
        d = asdict(self)
        d["stage_order"] = list(self.stage_order)
        d["features"]["features"] = list(self.features.features)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DSPConfig":
        d = dict(d)
        feat = dict(d.get("features", {}))
        feat["features"] = tuple(feat.get("features", FEATURES_ALL))
        cfg = cls(
            fs=float(d.get("fs", 2000.0)), n_ch=int(d.get("n_ch", 4)),
            dc=DCConfig(**d.get("dc", {})), notch=NotchConfig(**d.get("notch", {})),
            bandpass=BandpassConfig(**d.get("bandpass", {})),
            features=FeatureConfig(**feat),
            gap_policy=d.get("gap_policy", "reset"),
            stage_order=tuple(d.get("stage_order", STAGE_ORDER)))
        cfg.validate()
        return cfg

    def save(self, path: Path) -> None:
        path = Path(path)
        if path.suffix == ".json":
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        elif path.suffix == ".toml":
            path.write_text(dict_to_toml(self.to_dict()), encoding="utf-8")
        else:
            raise ValueError("use .json or .toml")

    @classmethod
    def load(cls, path: Path) -> "DSPConfig":
        path = Path(path)
        if path.suffix == ".json":
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if path.suffix == ".toml":
            with path.open("rb") as f:
                d = tomllib.load(f)
            return cls.from_dict(d.get("dsp", d))
        raise ValueError("use .json or .toml")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v)} to TOML")


def dict_to_toml(d: dict, prefix: str = "") -> str:
    """Minimal TOML writer for nested dicts of scalars and lists (enough for configs)."""
    scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in d.items() if isinstance(v, dict)]
    out = []
    if prefix and scalars:
        out.append(f"[{prefix}]")
    out += [f"{k} = {_toml_value(v)}" for k, v in scalars]
    for k, v in tables:
        if out:
            out.append("")
        out.append(dict_to_toml(v, f"{prefix}.{k}" if prefix else k))
    return "\n".join(out) + ("\n" if not prefix else "")
