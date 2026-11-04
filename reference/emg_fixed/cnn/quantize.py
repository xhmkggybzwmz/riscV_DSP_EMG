"""Post-training quantisation of the folded float CNN into QuantCNN (E2).

Inputs: the deployment-form float model f = emgml.models.cnn.fold_cnn(model, channel_scale)
(BN folded, per-channel input scale folded into conv1, 1/M folded only in f["fc_W_over_M"]),
the ESI LSB (mV per input code) and calibration recordings (POST_DSP int16 from the FIXED DSP,
training records of the fold only).

Scales (all real numbers live only here; the integer model uses wq, bq, M, s):
    conv1 input scale   s_in1 = LSB (the input code is the int16 POST_DSP sample)
    weight scale        s_w[o] = max|W[o]| / 127 (per output channel, symmetric, 0 -> 1)
    activation scale    s_a_l  = P99.99(float post-ReLU activation of layer l on calibration data) / 255
    bias                bq[o]  = round(b[o] / (s_in_l * s_w[o]))          int32
    requant             m[o]   = s_in_l * s_w[o] / s_a_l  ->  M = round(m * 2^s) in [2^15, 2^16)
    FC                  W_eff = W_fc * s_a3 / 19 ; s_fc = max|W_eff| / 127 (one scale) ; bfq = round(b / s_fc)
Only positions whose receptive field starts at or after `settle` (DSP warm-up skipped) are calibrated.
"""

from __future__ import annotations

import math

import numpy as np

from .int_model import QLayer, QuantCNN

W_MAX = 127
PCT = 99.99


def m_to_fixed(m: float) -> tuple[int, int]:
    if not m > 0:
        raise ValueError(f"requant multiplier must be positive, got {m}")
    e = math.floor(math.log2(m))
    s = 15 - e
    M = int(round(m * (1 << s))) if s >= 0 else 0
    if M == 1 << 16:
        M, s = 1 << 15, s - 1
    if not (1 <= s <= 62 and (1 << 15) <= M < (1 << 16)):
        raise ValueError(f"requant multiplier {m} -> M={M}, s={s} out of range")
    return M, s


def _conv_f(x, W, b, stride):
    k = W.shape[2]
    n = (x.shape[1] - k) // stride + 1
    idx = np.arange(n)[:, None] * stride + np.arange(k)[None, :]
    return np.einsum("oik,ink->on", W, x[:, idx]) + b[:, None]


def float_record(f: dict, x_mv: np.ndarray, settle: int = 200, window: int = 400, step: int = 100,
                 keep: bool = True) -> dict:
    """Folded float model over a whole record from SOS (float64). x_mv [T, C] in mV."""
    h = np.asarray(x_mv, dtype=np.float64).T
    acts = []
    for c in f["convs"]:
        h = np.maximum(_conv_f(h, c["W"], c["b"], c["stride"]), 0.0)
        if keep:
            acts.append(h)
    D = int(np.prod([c["stride"] for c in f["convs"]]))
    RF = rf_of(f)
    M = f["M"]
    first = (settle + window - RF) // D
    js = np.arange(first, h.shape[1], step // D)
    cs = np.concatenate([np.zeros((h.shape[0], 1)), np.cumsum(h, axis=1)], axis=1)
    S = cs[:, js + 1] - cs[:, js + 1 - M]
    logits = (f["fc_W_over_M"] @ S + f["fc_b"][:, None]).T
    return {"ends": js * D + RF - 1, "logits": logits, "acts": acts, "gap_mean": (S / M).T}


def rf_of(f: dict) -> int:
    rf, prod = 0, 1
    for i, c in enumerate(f["convs"]):
        k = c["W"].shape[2]
        rf = k if i == 0 else rf + (k - 1) * prod
        prod *= c["stride"]
    return rf


def layer_geometry(f: dict) -> list[tuple[int, int]]:
    """(cumulative stride, receptive field) of every conv layer output."""
    out, rf, prod = [], 0, 1
    for i, c in enumerate(f["convs"]):
        k = c["W"].shape[2]
        rf = k if i == 0 else rf + (k - 1) * prod
        prod *= c["stride"]
        out.append((prod, rf))
    return out


def calibrate(f: dict, calib_codes: list[np.ndarray], lsb: float, settle: int = 200, pct: float = PCT) -> dict:
    geo = layer_geometry(f)
    per_layer = [[] for _ in f["convs"]]
    for codes in calib_codes:
        r = float_record(f, codes.astype(np.float64) * lsb, settle)
        for l, a in enumerate(r["acts"]):
            j0 = -(-settle // geo[l][0])                 # first output starting at or after settle
            per_layer[l].append(a[:, j0:].ravel())
    out = []
    for l, v in enumerate(per_layer):
        v = np.concatenate(v)
        p = float(np.percentile(v, pct))
        out.append({"percentile": pct, "p": p, "max": float(v.max()), "n_values": int(v.size),
                    "frac_above_p": float(np.mean(v > p)), "frac_zero": float(np.mean(v == 0))})
    return {"layers": out, "n_records": len(calib_codes)}


def quantise(f: dict, lsb: float, calib: dict, window: int = 400, step: int = 100, settle: int = 0) -> QuantCNN:
    """settle = decision schedule offset after SOS (E3 hardware: 0 -> first decision at sample index 399).
    E2's recorded evaluation used settle = 200 (first decision at 599); calibration has its own skip."""
    layers, s_in = [], lsb
    for l, c in enumerate(f["convs"]):
        W, b = c["W"], c["b"]
        wmax = np.max(np.abs(W.reshape(W.shape[0], -1)), axis=1)
        s_w = np.where(wmax > 0, wmax / W_MAX, 1.0)
        wq = np.clip(np.round(W / s_w[:, None, None]), -W_MAX, W_MAX).astype(np.int64)
        s_out = calib["layers"][l]["p"] / 255.0
        if not s_out > 0:
            raise ValueError(f"layer {l + 1}: zero activation range in calibration")
        bq = np.round(b / (s_in * s_w)).astype(np.int64)
        if np.any(np.abs(bq) >= 1 << 31):
            raise ValueError(f"layer {l + 1}: bias exceeds int32")
        Ms = [m_to_fixed(s_in * s_w[o] / s_out) for o in range(W.shape[0])]
        layers.append(QLayer(wq=wq, bq=bq, M=np.array([m for m, _ in Ms], dtype=np.int64),
                             s=np.array([s for _, s in Ms], dtype=np.int64), stride=int(c["stride"]),
                             s_in=float(s_in), s_w=s_w, s_out=float(s_out)))
        s_in = s_out
    M_gap = int(f["M"])
    W_eff = f["fc_W"] * s_in / M_gap
    s_fc = float(np.max(np.abs(W_eff)) / W_MAX)
    fc_wq = np.clip(np.round(W_eff / s_fc), -W_MAX, W_MAX).astype(np.int64)
    fc_bq = np.round(f["fc_b"] / s_fc).astype(np.int64)
    D = int(np.prod([L.stride for L in layers]))
    return QuantCNN(layers=layers, fc_wq=fc_wq, fc_bq=fc_bq, s_fc=s_fc, M_gap=M_gap, D=D, RF=rf_of(f),
                    window=window, step=step, settle=settle,
                    meta={"lsb_mV": lsb, "calibration": calib, "weight_quant": "per-output-channel symmetric int8 "
                          "[-127,127]", "activation_quant": "uint8 [0,255] after ReLU, per-tensor scale",
                          "fc_quant": "per-tensor int8, 1/M and s_a3 folded"})


def dequant_acts(q: QuantCNN, acts: list[np.ndarray]) -> list[np.ndarray]:
    return [a * L.s_out for a, L in zip(acts, q.layers)]


def to_arrays(q: QuantCNN) -> dict:
    """Flat dict for np.savez / vector export."""
    d = {"fc_wq": q.fc_wq, "fc_bq": q.fc_bq}
    for i, L in enumerate(q.layers):
        d.update({f"conv{i + 1}_wq": L.wq, f"conv{i + 1}_bq": L.bq, f"conv{i + 1}_M": L.M,
                  f"conv{i + 1}_s": L.s, f"conv{i + 1}_stride": np.array(L.stride)})
    return d


def describe(q: QuantCNN) -> dict:
    return {"layers": [{"op": "conv1d", "wq_shape": list(L.wq.shape), "stride": L.stride, "kernel": L.k,
                        "input": "int16 POST_DSP" if i == 0 else "uint8 activation",
                        "s_in": L.s_in, "s_out": L.s_out,
                        "s_w_min": None if L.s_w is None else float(L.s_w.min()),
                        "s_w_max": None if L.s_w is None else float(L.s_w.max()),
                        "M_range": [int(L.M.min()), int(L.M.max())], "s_range": [int(L.s.min()), int(L.s.max())],
                        "bias_range": [int(L.bq.min()), int(L.bq.max())]} for i, L in enumerate(q.layers)],
            "fc": {"wq_shape": list(q.fc_wq.shape), "s_fc": q.s_fc,
                   "bias_range": [int(q.fc_bq.min()), int(q.fc_bq.max())]},
            "M_gap": q.M_gap, "D": q.D, "RF": q.RF, "window": q.window, "step": q.step, "settle": q.settle,
            "first_decision_end": q.first_step * q.D + q.RF - 1}


def from_saved(npz_path, json_path) -> QuantCNN:
    """Rebuild a QuantCNN from the int8 .npz arrays and the .json description written by E2."""
    import json
    a = np.load(npz_path)
    d = json.loads(open(json_path, encoding="utf-8").read())
    desc = d["describe"]
    layers = []
    for i, ld in enumerate(desc["layers"]):
        n = i + 1
        layers.append(QLayer(wq=a[f"conv{n}_wq"].astype(np.int64), bq=a[f"conv{n}_bq"].astype(np.int64),
                             M=a[f"conv{n}_M"].astype(np.int64), s=a[f"conv{n}_s"].astype(np.int64),
                             stride=int(a[f"conv{n}_stride"]), s_in=ld["s_in"], s_out=ld["s_out"]))
    return QuantCNN(layers=layers, fc_wq=a["fc_wq"].astype(np.int64), fc_bq=a["fc_bq"].astype(np.int64),
                    s_fc=desc["fc"]["s_fc"], M_gap=desc["M_gap"], D=desc["D"], RF=desc["RF"],
                    window=desc["window"], step=desc["step"], settle=desc["settle"], meta=d["meta"])
