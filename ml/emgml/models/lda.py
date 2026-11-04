"""Time-domain features + LDA (M1 baseline, PC only - never goes into hardware).

Pipeline: StandardScaler -> LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto").
Both steps are fitted on the training windows only (sklearn Pipeline), so the
scaling statistics and the Ledoit-Wolf shrinkage never see validation or test data.
The model has no tuned hyper-parameters; the only choice made per fold is the
feature subset, selected on inner validation folds inside the training records.
"""

from __future__ import annotations

from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURE_SETS = {
    "all5": ("MAV", "RMS", "WL", "ZC", "SSC"),
    "td4": ("MAV", "WL", "ZC", "SSC"),          # Hudgins set
    "rms": ("RMS",),
}


def make_lda() -> Pipeline:
    return Pipeline([("scale", StandardScaler()),
                     ("lda", LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto"))])
