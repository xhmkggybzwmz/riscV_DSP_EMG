"""Ninapro DB2 loader - interface only (not downloaded, not tested on real data).

Status (docs/system_design_extend.md 3.2): data licence and the movement-number
mapping of exercise B/C are not verified; the data have NOT been downloaded
(not authorised in E1a). This module fixes the interface the real loader will
implement, and refuses to run until the label map is filled in labels.py.

Planned behaviour (from Atzori et al. 2014 and the Ninapro DB2 page):
  - one MATLAB file per subject and exercise; variables emg [T, 12], restimulus, rerepetition
  - 2 kHz; electrodes 1..8 equally spaced around the forearm (radio-humeral joint),
    9/10 on finger flexor/extensor, 11/12 on biceps/triceps (double-differential Delsys Trigno)
  - one Recording per (movement, repetition) segment cut with restimulus/rerepetition;
    rest segments between repetitions become REST recordings; segments with mixed labels are
    never emitted as a pure class
  - protocol P4: repetitions {1, 3, 4, 6} train, {2, 5} test (common in the literature,
    reference still to be verified)
"""

from __future__ import annotations

from pathlib import Path

from ..labels import NINAPRO_DB2_TO_UNIFIED
from ..recording import Recording

DATASET = "NinaproDB2"
FS = 2000.0
CHANNEL_CONFIGS = {"ring4": [1, 3, 5, 7], "ring8": list(range(1, 9))}


def load_exercise(mat_path: Path, subject: int, exercise: str, config: str = "ring4") -> list[Recording]:
    if not NINAPRO_DB2_TO_UNIFIED:
        raise NotImplementedError("Ninapro DB2 label map not verified yet (labels.NINAPRO_DB2_TO_UNIFIED is empty)")
    raise NotImplementedError("Ninapro DB2 loading is planned after licence confirmation and download approval")
