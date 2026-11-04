"""Record-level split protocols (split first, window afterwards) and leakage checks.

Record key = (dataset, subject, session, trial, label). Splits are lists of
record indices; windows inherit their record's side, so overlapping windows of
one trial can never be on both sides.

P1 within-session : for each subject and session, leave one trial number out
                    (trial t of every gesture is the test set; the other trials train)
P2 cross-day      : for each subject, train on two sessions, test on the third

inner_folds() builds the validation folds used for model selection *inside*
the training records only (same idea as the outer protocol).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Fold:
    protocol: str
    subject: int
    name: str                  # e.g. "s1_t3" or "test_s2"
    train: tuple[int, ...]     # record indices
    test: tuple[int, ...]


def _keys(records):
    return [dict(dataset=k[0], subject=k[1], session=k[2], trial=k[3], label=k[4]) for k in records]


def p1_folds(records) -> list[Fold]:
    ks = _keys(records)
    folds = []
    for subj in sorted({k["subject"] for k in ks}):
        for sess in sorted({k["session"] for k in ks if k["subject"] == subj}):
            idx = [i for i, k in enumerate(ks) if k["subject"] == subj and k["session"] == sess]
            for t in sorted({ks[i]["trial"] for i in idx}):
                test = tuple(i for i in idx if ks[i]["trial"] == t)
                train = tuple(i for i in idx if ks[i]["trial"] != t)
                folds.append(Fold("P1", subj, f"s{sess}_t{t}", train, test))
    return folds


def p2_folds(records) -> list[Fold]:
    ks = _keys(records)
    folds = []
    for subj in sorted({k["subject"] for k in ks}):
        idx = [i for i, k in enumerate(ks) if k["subject"] == subj]
        sessions = sorted({ks[i]["session"] for i in idx})
        if len(sessions) < 2:
            continue
        for s in sessions:
            test = tuple(i for i in idx if ks[i]["session"] == s)
            train = tuple(i for i in idx if ks[i]["session"] != s)
            folds.append(Fold("P2", subj, f"test_s{s}", train, test))
    return folds


def inner_folds(records, train: tuple[int, ...], protocol: str) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Validation folds inside the training records: leave one trial number out (P1)
    or leave one training session out (P2)."""
    ks = _keys(records)
    field = "trial" if protocol == "P1" else "session"
    values = sorted({ks[i][field] for i in train})
    out = []
    for v in values:
        va = tuple(i for i in train if ks[i][field] == v)
        tr = tuple(i for i in train if ks[i][field] != v)
        if tr and va:
            out.append((tr, va))
    return out


def check_fold(records, fold: Fold) -> None:
    """Raise if the fold leaks: shared records, shared (subject, session, trial, label) keys,
    a test trial (P1) or test session (P2) present in training, or mixed subjects."""
    ks = _keys(records)
    tr, te = set(fold.train), set(fold.test)
    if tr & te:
        raise AssertionError(f"{fold.name}: records on both sides")
    if not tr or not te:
        raise AssertionError(f"{fold.name}: empty side")
    ktr = {tuple(records[i]) for i in tr}
    kte = {tuple(records[i]) for i in te}
    if ktr & kte:
        raise AssertionError(f"{fold.name}: identical record keys on both sides")
    subj = {ks[i]["subject"] for i in tr | te}
    if subj != {fold.subject}:
        raise AssertionError(f"{fold.name}: subjects {subj}")
    if fold.protocol == "P1":
        sess = {ks[i]["session"] for i in tr | te}
        t_test = {ks[i]["trial"] for i in te}
        if len(sess) != 1 or len(t_test) != 1 or t_test & {ks[i]["trial"] for i in tr}:
            raise AssertionError(f"{fold.name}: P1 trial isolation violated")
    elif fold.protocol == "P2":
        s_test = {ks[i]["session"] for i in te}
        if len(s_test) != 1 or s_test & {ks[i]["session"] for i in tr}:
            raise AssertionError(f"{fold.name}: P2 session isolation violated")


def window_mask(record_of_window: np.ndarray, rec_idx) -> np.ndarray:
    return np.isin(record_of_window, np.asarray(list(rec_idx)))
