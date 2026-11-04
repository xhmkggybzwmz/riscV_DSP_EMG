"""Tests of the E2 fixed-point reference.

    python -m unittest discover -s reference/emg_fixed/tests -t reference
"""

import sys
from pathlib import Path

_ML = Path(__file__).resolve().parents[3] / "ml"
if str(_ML) not in sys.path:
    sys.path.insert(0, str(_ML))
