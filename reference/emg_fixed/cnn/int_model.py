"""INT8 streaming 1D-CNN - integer golden reference (E2).

Integer semantics (every layer; widths are checked, see docs/ml/e2_fixed_point_spec.md):

    conv l   acc[o, j] = bq[o] + sum_{c,k} wq[o, c, k] * x[c, j*s_l + k]          int32 accumulator
             x : conv1 -> int16 POST_DSP samples (signed), conv2/conv3 -> uint8 activations
             wq: int8 in [-127, 127] (per-output-channel scale), bq: int32
    requant  a[o, j] = 0                                         if acc <= 0      (ReLU)
                     = min(255, round_shift(acc * M[o], s[o]))  otherwise         M: uint16 in [2^15, 2^16)
             acc * M < 2^48 (checked); round_shift = round half up; a is uint8 (0..255)
    GAP      S[c] = sum of the last 19 conv3 outputs a3[c, j]   (uint8 x 19 <= 4845 -> 13-bit unsigned)
    FC       logit[k] = bfq[k] + sum_c wfq[k, c] * S[c]          int8 x 13 bit, 32 terms -> int32
             one scale for all classes, so int32 logits are directly comparable
    decision argmax(logit), ties -> lowest class index

Window/stream geometry: D = 20, RF = 40, M = 19. Decisions at sample index settle + 399 + 100*n after
SOS; E3 hardware: settle = 0 -> first decision at index 399 (400th sample), then every 100 samples
(E2's recorded evaluation used settle = 200). Three independent paths:
    forward_window_int  one 400-sample window (direct sums)
    StreamingIntCNN     chunked stream, per-layer state, ring buffer + running GAP sum (add/subtract)
    run_record_int      whole record vectorised (used for large evaluations)
They must agree bit for bit at every decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..fixed import check_width

ACC_BITS = 32
PROD_BITS = 48
ACT_MAX = 255


@dataclass
class QLayer:
    wq: np.ndarray          # int64 [o, i, k], values in [-127, 127]
    bq: np.ndarray          # int64 [o]
    M: np.ndarray           # int64 [o], [2^15, 2^16)
    s: np.ndarray           # int64 [o], >= 1
    stride: int
    s_in: float = 0.0       # real scale of the input integer (documentation / dequantisation)
    s_w: np.ndarray = None  # real per-output-channel weight scale
    s_out: float = 0.0      # real scale of the uint8 output

    @property
    def k(self) -> int:
        return self.wq.shape[2]


@dataclass
class QuantCNN:
    layers: list[QLayer]
    fc_wq: np.ndarray       # int64 [n_cls, c]
    fc_bq: np.ndarray       # int64 [n_cls]
    s_fc: float             # real value of one logit LSB
    M_gap: int              # 19
    D: int
    RF: int
    window: int = 400
    step: int = 100
    settle: int = 200
    meta: dict = field(default_factory=dict)

    @property
    def first_step(self) -> int:
        return (self.settle + self.window - self.RF) // self.D

    @property
    def every(self) -> int:
        return self.step // self.D


def conv_valid_int(x: np.ndarray, wq: np.ndarray, stride: int) -> np.ndarray:
    """x int64 [C_in, L] -> int64 [C_out, n] exact integer sums (no bias)."""
    k = wq.shape[2]
    n = (x.shape[1] - k) // stride + 1
    if n <= 0:
        return np.zeros((wq.shape[0], 0), dtype=np.int64)
    idx = np.arange(n)[:, None] * stride + np.arange(k)[None, :]
    patches = x[:, idx]                                   # [C_in, n, k]
    return np.einsum("oik,ink->on", wq, patches, dtype=np.int64)


def requant_relu(acc: np.ndarray, M: np.ndarray, s: np.ndarray) -> np.ndarray:
    prod = check_width(acc * M[:, None], PROD_BITS, "acc*M")
    half = (np.int64(1) << (s - 1))[:, None]
    r = (prod + half) >> s[:, None]
    return np.where(acc > 0, np.minimum(r, ACT_MAX), 0).astype(np.int64)


def layer_int(L: QLayer, x: np.ndarray, name: str = "conv") -> tuple[np.ndarray, np.ndarray]:
    acc = check_width(conv_valid_int(x, L.wq, L.stride) + L.bq[:, None], ACC_BITS, f"{name} accumulator")
    return acc, requant_relu(acc, L.M, L.s)


def fc_int(q: QuantCNN, S: np.ndarray) -> np.ndarray:
    """S int64 [c] or [c, K] -> logits int64 [n_cls] or [n_cls, K]."""
    check_width(S, 14, "GAP sum")
    return check_width(q.fc_wq @ S + (q.fc_bq if S.ndim == 1 else q.fc_bq[:, None]), ACC_BITS, "FC accumulator")


def forward_window_int(q: QuantCNN, x: np.ndarray, keep: bool = False) -> dict:
    """x: int16 [C, window] POST_DSP samples of one window (its first sample at a multiple of D after SOS)."""
    h = np.asarray(x).astype(np.int64)
    if np.asarray(x).dtype != np.int16:
        raise TypeError("window samples must be int16")
    out = {"acc": [], "act": []}
    for i, L in enumerate(q.layers):
        acc, h = layer_int(L, h, f"conv{i + 1}")
        if keep:
            out["acc"].append(acc)
            out["act"].append(h)
    if h.shape[1] != q.M_gap:
        raise ValueError(f"window gives {h.shape[1]} conv3 steps, expected {q.M_gap}")
    S = h.sum(axis=1)
    logits = fc_int(q, S)
    out.update({"gap": S, "logits": logits, "argmax": int(np.argmax(logits))})
    return out


class StreamingIntCNN:
    """Chunked integer stream. process(y, sos, gap) -> (window_end[K], logits[K, n_cls]).
    record=True keeps every layer output (for test vectors)."""

    def __init__(self, q: QuantCNN, record: bool = False):
        self.q, self.record = q, record
        self.reset()

    def reset(self) -> None:
        n = len(self.q.layers)
        cin = [self.q.layers[0].wq.shape[1]] + [L.wq.shape[0] for L in self.q.layers[:-1]]
        self.hist = [np.zeros((c, 0), dtype=np.int64) for c in cin]
        self.n_in, self.n_out = [0] * n, [0] * n
        self.ring = []                                     # last M_gap conv3 outputs
        self.S = np.zeros(self.q.layers[-1].wq.shape[0], dtype=np.int64)
        self.n_final = 0
        self.log = {"acc": [[] for _ in range(n)], "act": [[] for _ in range(n)], "gap": [], "logits": []}

    def _layer(self, i: int, x: np.ndarray) -> np.ndarray:
        L = self.q.layers[i]
        k, s = L.k, L.stride
        buf = np.concatenate([self.hist[i], x], axis=1)
        base = self.n_in[i] - self.hist[i].shape[1]
        avail = self.n_in[i] + x.shape[1]
        j0 = self.n_out[i]
        j1 = (avail - k) // s if avail >= k else -1
        a = np.zeros((L.wq.shape[0], 0), dtype=np.int64)
        if j1 >= j0:
            acc, a = layer_int(L, buf[:, j0 * s - base: j1 * s + k - base], f"conv{i + 1}")
            if self.record:
                self.log["acc"][i].append((j0, acc))
                self.log["act"][i].append((j0, a))
            self.n_out[i] = j1 + 1
        self.n_in[i] = avail
        self.hist[i] = buf[:, self.n_out[i] * s - base:]
        return a

    def process(self, y: np.ndarray, sos: bool = False, gap: bool = False):
        if sos or gap:
            self.reset()
        y = np.asarray(y)
        if y.dtype != np.int16:
            raise TypeError("POST_DSP samples must be int16")
        h = y.T.astype(np.int64)
        for i in range(len(self.q.layers)):
            h = self._layer(i, h)
        ends, logits = [], []
        for t in range(h.shape[1]):
            v = h[:, t]
            self.ring.append(v)
            self.S = self.S + v                            # add the new step
            if len(self.ring) > self.q.M_gap:
                self.S = self.S - self.ring.pop(0)         # subtract the step leaving the window
            j = self.n_final
            if j >= self.q.first_step and (j - self.q.first_step) % self.q.every == 0:
                lg = fc_int(self.q, self.S)
                ends.append(j * self.q.D + self.q.RF - 1)
                logits.append(lg)
                if self.record:
                    self.log["gap"].append((ends[-1], self.S.copy()))
                    self.log["logits"].append((ends[-1], lg))
            self.n_final += 1
        n_cls = self.q.fc_bq.shape[0]
        return np.asarray(ends, dtype=np.int64), (np.array(logits) if logits else np.zeros((0, n_cls), np.int64))


def run_record_int(q: QuantCNN, y: np.ndarray, keep: bool = False) -> dict:
    """Whole record from SOS (vectorised). y int16 [T, C]."""
    h = np.asarray(y).T.astype(np.int64)
    accs, acts = [], []
    for i, L in enumerate(q.layers):
        acc, h = layer_int(L, h, f"conv{i + 1}")
        if keep:
            accs.append(acc)
            acts.append(h)
    n3 = h.shape[1]
    js = np.arange(q.first_step, n3, q.every)
    cs = np.concatenate([np.zeros((h.shape[0], 1), np.int64), np.cumsum(h, axis=1)], axis=1)
    S = cs[:, js + 1] - cs[:, js + 1 - q.M_gap]            # [c, K]
    logits = fc_int(q, S).T if len(js) else np.zeros((0, q.fc_bq.shape[0]), np.int64)
    return {"ends": js * q.D + q.RF - 1, "logits": logits, "gap": S.T, "acc": accs, "act": acts,
            "argmax": np.argmax(logits, axis=1) if len(js) else np.zeros(0, np.int64)}
