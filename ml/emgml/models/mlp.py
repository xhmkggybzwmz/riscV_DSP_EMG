"""M2: time-domain features + small MLP (PyTorch, CPU).

    x (features, physical units) -> standardise (mu, sigma from the fit set only)
      -> Linear(d0, h1) -> ReLU -> ... -> Linear(h_last, n_classes) -> logits

Deployment form (fold_standardisation): the standardisation is folded into the
first Linear layer, W1' = W1 / sigma (column-wise), b1' = b1 - W1' @ mu, so the
hardware sees raw features and plain Linear/ReLU layers only. Equivalence of
the folded network is checked numerically (tests and every exported model).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
from torch import nn


class MLP(nn.Module):
    def __init__(self, d_in: int, hidden: tuple[int, ...], n_classes: int):
        super().__init__()
        layers, d = [], d_in
        for h in hidden:
            layers += [nn.Linear(d, h), nn.ReLU()]
            d = h
        layers.append(nn.Linear(d, n_classes))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)

    def linears(self) -> list[nn.Linear]:
        return [m for m in self.net if isinstance(m, nn.Linear)]


def mlp_cost(d_in: int, hidden: tuple[int, ...], n_classes: int) -> dict:
    dims = [d_in, *hidden, n_classes]
    macs = sum(a * b for a, b in zip(dims[:-1], dims[1:]))
    params = sum(a * b + b for a, b in zip(dims[:-1], dims[1:]))
    return {"layer_dims": dims, "params": params, "macs_per_window": macs,
            "tensor_shapes": [{"W": [b, a], "b": [b]} for a, b in zip(dims[:-1], dims[1:])]}


@dataclass
class TrainResult:
    model: nn.Module
    mu: np.ndarray
    sigma: np.ndarray
    history: list[dict]          # per epoch: train_loss, val_loss, val_bal_acc
    best_epoch: int


def _bal_acc(y, p, n_classes):
    rec = [np.mean(p[y == c] == c) for c in range(n_classes) if np.any(y == c)]
    return float(np.mean(rec))


def train_mlp(X_fit, y_fit, X_val, y_val, hidden, n_classes, seed, max_epochs=200, patience=15,
              lr=1e-3, weight_decay=1e-4, batch=256) -> TrainResult:
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    mu = X_fit.mean(axis=0)
    sigma = X_fit.std(axis=0)
    sigma[sigma == 0] = 1.0
    Xf = torch.tensor((X_fit - mu) / sigma, dtype=torch.float32)
    Xv = torch.tensor((X_val - mu) / sigma, dtype=torch.float32)
    yf = torch.tensor(y_fit, dtype=torch.long)
    yv = torch.tensor(y_val, dtype=torch.long)
    model = MLP(X_fit.shape[1], tuple(hidden), n_classes)
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    loss_fn = nn.CrossEntropyLoss()
    best, best_state, best_epoch, bad, hist = np.inf, None, -1, 0, []
    for epoch in range(max_epochs):
        model.train()
        perm = rng.permutation(len(Xf))
        tot = 0.0
        for i in range(0, len(perm), batch):
            idx = torch.as_tensor(perm[i:i + batch])
            opt.zero_grad()
            loss = loss_fn(model(Xf[idx]), yf[idx])
            loss.backward()
            opt.step()
            tot += float(loss) * len(idx)
        model.eval()
        with torch.no_grad():
            lv = model(Xv)
            vloss = float(loss_fn(lv, yv))
            vba = _bal_acc(y_val, lv.argmax(1).numpy(), n_classes)
        hist.append({"epoch": epoch, "train_loss": tot / len(Xf), "val_loss": vloss, "val_bal_acc": vba})
        if vloss < best - 1e-6:
            best, best_epoch, bad = vloss, epoch, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    return TrainResult(model, mu, sigma, hist, best_epoch)


def predict_mlp(res: TrainResult, X: np.ndarray) -> np.ndarray:
    with torch.no_grad():
        return res.model(torch.tensor((X - res.mu) / res.sigma, dtype=torch.float32)).argmax(1).numpy()


def fold_standardisation(res: TrainResult) -> list[dict]:
    """Return the deployment layers (float64) with the standardisation folded into layer 1."""
    lin = res.model.linears()
    out = []
    for i, l in enumerate(lin):
        W = l.weight.detach().double().numpy()
        b = l.bias.detach().double().numpy()
        if i == 0:
            W = W / res.sigma[None, :]
            b = b - W @ res.mu
        out.append({"W": W, "b": b, "activation": "relu" if i < len(lin) - 1 else "none"})
    return out


def forward_folded(layers: list[dict], X: np.ndarray) -> np.ndarray:
    h = np.asarray(X, dtype=np.float64)
    for l in layers:
        h = h @ l["W"].T + l["b"]
        if l["activation"] == "relu":
            h = np.maximum(h, 0.0)
    return h
