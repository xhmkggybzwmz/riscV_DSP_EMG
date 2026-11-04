"""Post-processing reference for the future RV32I firmware (integer only, no floating point).

Input per valid decision window: int32 class scores (logits) from the inference core, class order
REST, HAND_CLOSE, HAND_OPEN, WRIST_FLEX, WRIST_EXT, PINCH (index 0..5).

    top1   = argmax(logits); ties -> lowest class index
    margin = logits[top1] - max(logits[k] for k != top1)          (>= 0, in logit LSB = s_fc)
    raw    = top1 if margin >= T else UNCERTAIN (-1)              T: int threshold in logit LSB (0 = off)

    vote over the last K raw decisions (K odd; K = 1 means no vote):
        history holds raw values (UNCERTAIN included: an uncertain window is a vote for "no class")
        while fewer than K decisions exist since the last reset -> output NO_DECISION (-2)
        cls with count(cls) >= K//2 + 1 among the last K (strict majority, UNCERTAIN never wins)
            -> output = cls
        otherwise -> output = previous output (hold; may stay NO_DECISION)
    reset (SOS / GAP / reconfiguration): history cleared, output = NO_DECISION
    REST is an ordinary class (index 0); there is no special "invalid" class besides UNCERTAIN.

Decision timing: a new raw decision every 100 input samples (50 ms at 2 kHz). The first output
after a reset needs K decisions: window end at sample 599 + 100*(K-1). After a class change in the
signal, the voted output changes after at least K//2 + 1 windows showing the new class.
"""

from __future__ import annotations

from collections import deque

import numpy as np

UNCERTAIN = -1
NO_DECISION = -2


def top2(logits: np.ndarray) -> tuple[int, int]:
    lg = np.asarray(logits, dtype=np.int64)
    t1 = int(np.argmax(lg))                    # first maximum -> lowest index on ties
    rest = np.delete(lg, t1)
    return t1, int(lg[t1] - rest.max())


class PostProcessor:
    def __init__(self, K: int = 5, T: int = 0):
        if K < 1 or K % 2 == 0:
            raise ValueError("K must be odd and >= 1")
        if T < 0:
            raise ValueError("T must be >= 0")
        self.K, self.T, self.need = K, T, K // 2 + 1
        self.reset()

    def reset(self) -> None:
        self.hist = deque(maxlen=self.K)
        self.out = NO_DECISION

    def step(self, logits) -> dict:
        t1, margin = top2(logits)
        raw = t1 if margin >= self.T else UNCERTAIN
        self.hist.append(raw)
        if len(self.hist) >= self.K:
            counts = {}
            for v in self.hist:
                if v != UNCERTAIN:
                    counts[v] = counts.get(v, 0) + 1
            winners = [c for c, n in counts.items() if n >= self.need]
            if winners:                            # at most one class can have a strict majority
                self.out = winners[0]
        return {"top1": t1, "margin": margin, "raw": raw, "out": self.out}

    def run(self, logits_seq, reset_before=()) -> list[dict]:
        res = []
        for i, lg in enumerate(logits_seq):
            if i in reset_before:
                self.reset()
            res.append(self.step(lg))
        return res
