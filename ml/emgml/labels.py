"""Unified gesture set and per-dataset label maps (docs/system_design_extend.md 4.1).

The maps live here (and can be overridden from a config), never inside a loader
or a model.
"""

from __future__ import annotations

UNIFIED = {
    0: "REST",
    1: "HAND_CLOSE",
    2: "HAND_OPEN",
    3: "WRIST_FLEX",
    4: "WRIST_EXT",
    5: "PINCH",
}

# GRABMyo v1.1.0 gesture numbers (MotionSequence.txt, k = 1..17 per readme.txt)
GRABMYO_GESTURES = {
    1: "Lateral Prehension", 2: "Thumb Adduction", 3: "Thumb and Little Finger Opposition",
    4: "Thumb and Index Finger Opposition", 5: "Thumb and Index Finger Extension",
    6: "Thumb and Little Finger Extension", 7: "Index and Middle Finger Extension",
    8: "Little Finger Extension", 9: "Index Finger Extension", 10: "Thumb Finger Extension",
    11: "Wrist Extension", 12: "Wrist Flexion", 13: "Forearm Supination", 14: "Forearm Pronation",
    15: "Hand Open", 16: "Hand Close", 17: "Rest",
}

# GRABMyo gesture -> unified label
GRABMYO_TO_UNIFIED = {17: 0, 16: 1, 15: 2, 12: 3, 11: 4, 4: 5}

# Ninapro DB2: movement numbers are NOT verified yet (docs/system_design_extend.md 3.2);
# the loader refuses to map labels until this table is filled from the official README.
NINAPRO_DB2_TO_UNIFIED: dict[tuple[str, int], int] = {}


def name(label: int) -> str:
    return UNIFIED[label]


def class_names(labels: list[int] | None = None) -> list[str]:
    return [UNIFIED[i] for i in (labels if labels is not None else sorted(UNIFIED))]
