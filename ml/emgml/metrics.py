"""Classification metrics used in every report."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import balanced_accuracy_score, confusion_matrix, f1_score


def summarize(y_true: np.ndarray, y_pred: np.ndarray, labels: list[int]) -> dict:
    return {"n": int(len(y_true)),
            "accuracy": float(np.mean(y_true == y_pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
            "macro_f1": float(f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)),
            "confusion": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
            "class_counts": {int(l): int(np.sum(y_true == l)) for l in labels}}


def majority_vote(pred: np.ndarray, record: np.ndarray, k: int) -> np.ndarray:
    """Causal majority vote over the last k window decisions of the same recording
    (ties -> the most recent decision among the tied classes)."""
    out = pred.copy()
    for r in np.unique(record):
        idx = np.flatnonzero(record == r)
        p = pred[idx]
        for j in range(len(p)):
            h = p[max(0, j - k + 1):j + 1]
            vals, cnt = np.unique(h, return_counts=True)
            best = vals[cnt == cnt.max()]
            out[idx[j]] = next(x for x in h[::-1] if x in best)
    return out
