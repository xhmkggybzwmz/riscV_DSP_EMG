"""M3: small causal 1D-CNN on DSP-filtered waveforms, with an exact streaming form.

Network (training form), input window x[C, N] (N = 400):
    for each layer l: Conv1d(c_in, c_out, k_l, stride s_l, no padding = "valid") -> BatchNorm1d -> ReLU
    global average over the M time steps of the last layer -> Linear(c_last, n_classes)

Geometry (stream index n counted from SOS, all layers causal and valid):
    D  = prod(s_l)                          total stride of the last layer
    RF = k_1 + sum_{l>=2} (k_l - 1) * prod_{i<l} s_i   receptive field in input samples
    last-layer step j uses input samples [j*D, j*D + RF - 1]
A window [a, a+N-1] maps onto whole last-layer steps only if
    (N - RF) % D == 0   -> M = (N - RF)/D + 1 steps tile the window exactly (no sample dropped)
    a % D == 0 for every window start a = settle + k*step -> settle % D == 0 and step % D == 0
check_geometry() enforces this; the default spec has k = 8/7/6, s = 2/2/5: D = 20, RF = 40, M = 19.
(The earlier 7/5/3, stride 2/2/2 sketch has D = 8, which does not divide the 100-sample step.)

Streaming form (what hardware will do): each layer keeps its last k-1 inputs and
emits an output when an input with index k-1 + j*s arrives; a sliding sum keeps the
last M outputs of the final layer; at every window end (every step/D final steps,
first at the window ending at settle + N - 1) the FC layer produces the logits.
SOS/GAP clears all of it (same contract as DSP-R0).

Deployment form (fold_cnn): BatchNorm folded into the convolutions, the per-channel
input scale folded into conv 1, the 1/M of the average folded into the FC weights.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch
from torch import nn


@dataclass
class CNNSpec:
    in_ch: int = 4
    layers: list[tuple[int, int, int]] = field(default_factory=lambda: [(16, 8, 2), (32, 7, 2), (32, 6, 5)])
    n_classes: int = 6
    window: int = 400
    step: int = 100
    settle: int = 200
    fs: float = 2000.0


def check_geometry(spec: CNNSpec) -> dict:
    D, RF, prod = 1, 0, 1
    for i, (_, k, s) in enumerate(spec.layers):
        RF = k if i == 0 else RF + (k - 1) * prod
        prod *= s
    D = prod
    lengths, L = [], spec.window
    for _, k, s in spec.layers:
        L = (L - k) // s + 1
        lengths.append(L)
    problems = []
    if (spec.window - RF) % D:
        problems.append(f"(window - RF) % D = {(spec.window - RF) % D}: window not tiled by last-layer steps")
    if spec.step % D:
        problems.append(f"step {spec.step} not a multiple of D = {D}")
    if spec.settle % D:
        problems.append(f"settle {spec.settle} not a multiple of D = {D}")
    M = (spec.window - RF) // D + 1
    if lengths[-1] != M:
        problems.append(f"last-layer length {lengths[-1]} != M {M}")
    # every layer must consume its whole input inside the window (no trailing samples left over)
    L = spec.window
    for (_, k, s), Lo in zip(spec.layers, lengths):
        if (L - k) % s:
            problems.append(f"layer with k={k}, s={s} leaves {(L - k) % s} unused inputs")
        L = Lo
    return {"D": D, "RF": RF, "M": M, "lengths": lengths, "ok": not problems, "problems": problems,
            "decisions_per_s": spec.fs / spec.step, "last_layer_rate_hz": spec.fs / D,
            "first_window_end": spec.settle + spec.window - 1}


def cnn_cost(spec: CNNSpec) -> dict:
    g = check_geometry(spec)
    c_in, prod, stream, per_win, params, shapes = spec.in_ch, 1, 0.0, 0, 0, []
    for (c_out, k, s), L in zip(spec.layers, g["lengths"]):
        prod *= s
        mac_out = c_out * c_in * k
        stream += spec.fs / prod * mac_out
        per_win += L * mac_out
        params += c_out * c_in * k + c_out
        shapes.append({"W": [c_out, c_in, k], "b": [c_out], "stride": s, "out_len_per_window": L,
                       "out_rate_hz": spec.fs / prod})
        c_in = c_out
    fc = c_in * spec.n_classes
    stream += fc * spec.fs / spec.step + c_in * spec.fs / g["D"]          # FC per decision + GAP add/sub
    per_win += fc
    params += fc + spec.n_classes
    shapes.append({"W": [spec.n_classes, c_in], "b": [spec.n_classes], "op": "fc after mean over M steps"})
    return {"params_deployed": params, "macs_per_window": per_win, "stream_macs_per_s": stream,
            "tensor_shapes": shapes, "geometry": g}


class CausalCNN(nn.Module):
    def __init__(self, spec: CNNSpec):
        super().__init__()
        self.spec = spec
        mods, c = [], spec.in_ch
        for c_out, k, s in spec.layers:
            mods += [nn.Conv1d(c, c_out, k, stride=s), nn.BatchNorm1d(c_out), nn.ReLU()]
            c = c_out
        self.features = nn.Sequential(*mods)
        self.fc = nn.Linear(c, spec.n_classes)

    def forward(self, x):                     # x [B, C, N] (already scaled)
        return self.fc(self.features(x).mean(dim=2))


@dataclass
class CNNTrainResult:
    model: CausalCNN
    scale: np.ndarray                         # per input channel, x_scaled = x * scale
    history: list[dict]
    best_epoch: int


def _bal_acc(y, p, n):
    return float(np.mean([np.mean(p[y == c] == c) for c in range(n) if np.any(y == c)]))


def window_batch(U: torch.Tensor, rec: np.ndarray, kidx: np.ndarray) -> torch.Tensor:
    """U = X.unfold(2, N, step) view [R, C, K, N]; returns [B, C, N]."""
    return U[torch.as_tensor(rec), :, torch.as_tensor(kidx)]


def train_cnn(U, rec_fit, k_fit, y_fit, rec_val, k_val, y_val, scale, spec: CNNSpec, seed, max_epochs=40,
              patience=8, lr=2e-3, weight_decay=1e-4, batch=128) -> CNNTrainResult:
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    sc = torch.tensor(scale, dtype=torch.float32)[None, :, None]
    model = CausalCNN(spec)
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    loss_fn = nn.CrossEntropyLoss()
    yv = torch.tensor(y_val, dtype=torch.long)
    best, best_state, best_epoch, bad, hist = np.inf, None, -1, 0, []
    for epoch in range(max_epochs):
        model.train()
        perm = rng.permutation(len(y_fit))
        tot = 0.0
        for i in range(0, len(perm), batch):
            j = perm[i:i + batch]
            xb = window_batch(U, rec_fit[j], k_fit[j]) * sc
            opt.zero_grad()
            loss = loss_fn(model(xb), torch.as_tensor(y_fit[j], dtype=torch.long))
            loss.backward()
            opt.step()
            tot += float(loss) * len(j)
        model.eval()
        with torch.no_grad():
            logits = torch.cat([model(window_batch(U, rec_val[i:i + 512], k_val[i:i + 512]) * sc)
                                for i in range(0, len(y_val), 512)])
            vloss = float(loss_fn(logits, yv))
            vba = _bal_acc(y_val, logits.argmax(1).numpy(), spec.n_classes)
        hist.append({"epoch": epoch, "train_loss": tot / len(y_fit), "val_loss": vloss, "val_bal_acc": vba})
        if vloss < best - 1e-6:
            best, best_epoch, bad = vloss, epoch, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    return CNNTrainResult(model, np.asarray(scale, dtype=np.float64), hist, best_epoch)


def predict_cnn(res: CNNTrainResult, U, rec, kidx) -> np.ndarray:
    sc = torch.tensor(res.scale, dtype=torch.float32)[None, :, None]
    out = []
    with torch.no_grad():
        for i in range(0, len(rec), 512):
            out.append(res.model(window_batch(U, rec[i:i + 512], kidx[i:i + 512]) * sc).argmax(1).numpy())
    return np.concatenate(out) if out else np.zeros(0, dtype=np.int64)


# ---------------------------------------------------------------- deployment form
def fold_cnn(model: CausalCNN, scale: np.ndarray) -> dict:
    """BN and input scale folded; returns float64 layers for the numpy/streaming references."""
    convs = []
    mods = list(model.features)
    for i in range(0, len(mods), 3):
        conv, bn = mods[i], mods[i + 1]
        W = conv.weight.detach().double().numpy()            # [c_out, c_in, k]
        b = conv.bias.detach().double().numpy()
        g = (bn.weight.detach().double() / torch.sqrt(bn.running_var.double() + bn.eps)).numpy()
        W = W * g[:, None, None]
        b = (b - bn.running_mean.double().numpy()) * g + bn.bias.detach().double().numpy()
        if i == 0:
            W = W * np.asarray(scale, dtype=np.float64)[None, :, None]
        convs.append({"W": W, "b": b, "stride": conv.stride[0], "activation": "relu"})
    M = check_geometry(model.spec)["M"]
    Wfc = model.fc.weight.detach().double().numpy()
    return {"convs": convs, "M": M, "fc_W_over_M": Wfc / M, "fc_W": Wfc,
            "fc_b": model.fc.bias.detach().double().numpy()}


def _conv_valid(x: np.ndarray, W: np.ndarray, b: np.ndarray, s: int) -> np.ndarray:
    """x [C_in, L] -> [C_out, (L-k)//s + 1], valid, causal indexing."""
    k = W.shape[2]
    n = (x.shape[1] - k) // s + 1
    if n <= 0:
        return np.zeros((W.shape[0], 0))
    idx = np.arange(n)[:, None] * s + np.arange(k)[None, :]          # [n, k]
    patches = x[:, idx]                                              # [C_in, n, k]
    return np.einsum("oik,ink->on", W, patches) + b[:, None]


def forward_window_folded(f: dict, x: np.ndarray) -> np.ndarray:
    """Reference: one window x [C, N] (unscaled, the scale is in conv 1) -> logits."""
    h = np.asarray(x, dtype=np.float64)
    for c in f["convs"]:
        h = np.maximum(_conv_valid(h, c["W"], c["b"], c["stride"]), 0.0)
    if h.shape[1] != f["M"]:
        raise ValueError(f"window gives {h.shape[1]} final steps, expected {f['M']}")
    return f["fc_W_over_M"] @ h.sum(axis=1) + f["fc_b"]


class StreamingCNN:
    """Chunked streaming evaluation with persistent state; reset() = SOS/GAP."""

    def __init__(self, f: dict, spec: CNNSpec):
        self.f, self.spec = f, spec
        g = check_geometry(spec)
        if not g["ok"]:
            raise ValueError(g["problems"])
        self.D, self.M = g["D"], g["M"]
        self.first_step = (spec.settle + spec.window - g["RF"]) // self.D       # last step of the first window
        self.every = spec.step // self.D
        self.reset()

    def reset(self) -> None:
        n = len(self.f["convs"])
        self.hist = [np.zeros((self.spec.in_ch if i == 0 else self.f["convs"][i - 1]["W"].shape[0], 0))
                     for i in range(n)]
        self.n_in = [0] * n                     # inputs received per layer since SOS
        self.n_out = [0] * n                    # outputs produced per layer since SOS
        self.last = []                          # last M outputs of the final layer
        self.n_final = 0

    def _layer(self, i: int, x: np.ndarray) -> np.ndarray:
        """Output j needs inputs [j*s, j*s + k - 1]; it is produced as soon as they have arrived."""
        c = self.f["convs"][i]
        k, s = c["W"].shape[2], c["stride"]
        buf = np.concatenate([self.hist[i], x], axis=1)
        base = self.n_in[i] - self.hist[i].shape[1]                 # stream index of buf[:, 0]
        avail = self.n_in[i] + x.shape[1]                           # inputs available: [0, avail)
        j0 = self.n_out[i]
        j1 = (avail - k) // s if avail >= k else -1                 # last output computable now
        out = np.zeros((c["W"].shape[0], 0))
        if j1 >= j0:
            seg = buf[:, j0 * s - base: j1 * s + k - base]
            out = np.maximum(_conv_valid(seg, c["W"], c["b"], s), 0.0)
            self.n_out[i] = j1 + 1
        self.n_in[i] = avail
        self.hist[i] = buf[:, self.n_out[i] * s - base:]              # inputs still needed later
        return out

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """x [T, C] (DSP output, unscaled). Returns (window_end_sample[K], logits[K, n_classes])."""
        h = np.asarray(x, dtype=np.float64).T
        for i in range(len(self.f["convs"])):
            h = self._layer(i, h)
        ends, logits = [], []
        for t in range(h.shape[1]):
            j = self.n_final
            self.last.append(h[:, t])
            if len(self.last) > self.M:
                self.last.pop(0)
            if j >= self.first_step and (j - self.first_step) % self.every == 0:
                ends.append(j * self.D + check_geometry(self.spec)["RF"] - 1)
                logits.append(self.f["fc_W_over_M"] @ np.sum(self.last, axis=0) + self.f["fc_b"])
            self.n_final += 1
        n_cls = self.f["fc_b"].shape[0]
        return np.asarray(ends, dtype=np.int64), (np.asarray(logits) if logits else np.zeros((0, n_cls)))
