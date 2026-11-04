"""Candidate deployment contracts (machine-readable) for MLP / CNN - NOT a frozen hardware model.

For one reference fold, writes <out>/contracts/<case>/<model>_contract.json and
<model>_weights.npz (deployment form: standardisation / channel scale and BN folded),
and records numerical equivalence checks:
  MLP: folded float64 network vs PyTorch on the test windows
  CNN: folded window reference vs PyTorch on the test windows, and the streaming
       implementation vs the window reference on complete test recordings (from SOS)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import labels

CONTRACT_VERSION = "e1b-candidate-0.1"


def _common(args, dsp, recs, model, fold_name, proto, seed):
    from .dsp import EMGPreprocessPipeline
    r0 = recs[0]
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "CANDIDATE - for review; not frozen, not a hardware specification yet",
        "model": model, "case": args["case"]["name"],
        "reference_fold": {"subject": args["subject"], "protocol": proto, "fold": fold_name, "seed": seed},
        "input": {"dataset": r0.dataset, "lanes": r0.channel_names, "n_ch": r0.n_ch, "fs_hz": r0.fs,
                  "fs_original_hz": r0.fs_original, "units": r0.units,
                  "quantisation": args["case"].get("quant", "float"),
                  "lane_order_rule": "GRABMyo bip lanes from electrode 1 (elbow-crease centreline) in the original "
                                     "numbering direction; ESI anatomical rule still open (D12)"},
        "dsp": {"config": dsp.to_dict(), "stage_order": list(dsp.stage_order),
                "coefficients": EMGPreprocessPipeline(dsp).describe()["stages"],
                "state_rule": "SOS/GAP/parameter change clear all filter and feature state (ESI v0.1, 6.7)"},
        "windowing": {"window_samples": dsp.features.window_samples, "step_samples": dsp.features.step_samples,
                      "settle_samples": dsp.features.settle_samples,
                      "first_window_end_after_sos": dsp.features.settle_samples + dsp.features.window_samples - 1},
        "outputs": {"class_order": labels.class_names(), "decision": "argmax of logits"},
    }


def export_reference(keep, sd, keys, args, dsp, spec, recs, gidx) -> dict:
    import torch

    from .models.cnn import StreamingCNN, cnn_cost, fold_cnn, forward_window_folded, window_batch
    from .models.mlp import fold_standardisation, forward_folded, mlp_cost
    ref = args["reference_fold"]
    out = Path(args["out_dir"]) / "contracts" / args["case"]["name"].replace("/", "__")
    out.mkdir(parents=True, exist_ok=True)
    done = {}
    folds = {f.name: f for f in args["folds"][ref["protocol"]]}
    fold = folds[ref["fold"]]
    test_pos = np.flatnonzero(np.isin(sd.gidx, np.asarray(fold.test)))
    me = np.isin(sd.rec, test_pos)
    key = ("mlp", ref["protocol"], ref["fold"], ref["seed"])
    if key in keep:
        res, cols = keep[key]
        layers = fold_standardisation(res)
        X = sd.feats[me][:, cols]
        with torch.no_grad():
            ref_logits = res.model(torch.tensor((X - res.mu) / res.sigma, dtype=torch.float32)).double().numpy()
        fl = forward_folded(layers, X)
        c = _common(args, dsp, recs, "mlp", ref["fold"], ref["protocol"], ref["seed"])
        cost = mlp_cost(len(cols), tuple(args["cfg"]["mlp"]["hidden"]), len(labels.UNIFIED))
        c.update({
            "features": {"names_in_order": [sd.feat_names[i] for i in cols],
                         "definitions": "ml/emgml/dsp/features.py (continuous boundary)",
                         "boundary": dsp.features.boundary, "zc_threshold": dsp.features.zc_threshold,
                         "ssc_threshold": dsp.features.ssc_threshold, "units": "mV (MAV/RMS/WL), counts (ZC/SSC)"},
            "standardisation": {"mu": res.mu.tolist(), "sigma": res.sigma.tolist(),
                                "deployment": "folded into layer 1: W1' = W1 / sigma, b1' = b1 - W1' mu"},
            "network": {"type": "MLP", "layers": [{"W": list(l["W"].shape), "b": list(l["b"].shape),
                                                   "activation": l["activation"]} for l in layers], **cost},
            "weights_file": "mlp_weights.npz",
            "equivalence": {"folded_vs_torch_max_abs_logit_diff": float(np.max(np.abs(fl - ref_logits))),
                            "argmax_mismatch": int(np.sum(fl.argmax(1) != ref_logits.argmax(1))),
                            "n_windows": int(len(X))}})
        np.savez(out / "mlp_weights.npz", **{f"W{i}": l["W"] for i, l in enumerate(layers)},
                 **{f"b{i}": l["b"] for i, l in enumerate(layers)})
        (out / "mlp_contract.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
        done["mlp"] = c["equivalence"]
    key = ("cnn", ref["protocol"], ref["fold"], ref["seed"])
    if key in keep:
        res = keep[key]
        f = fold_cnn(res.model, res.scale)
        U = torch.from_numpy(sd.wave).unfold(2, dsp.features.window_samples, dsp.features.step_samples)
        rec, kk = sd.rec[me], sd.kidx[me]
        res.model.double()
        with torch.no_grad():
            sc = torch.tensor(res.scale, dtype=torch.float64)[None, :, None]
            tl = np.concatenate([res.model(window_batch(U, rec[i:i + 256], kk[i:i + 256]).double() * sc).numpy()
                                 for i in range(0, len(rec), 256)])
        res.model.float()
        wl = np.array([forward_window_folded(f, U[r, :, k].double().numpy()) for r, k in zip(rec, kk)])
        # streaming over whole test recordings from SOS, compared with the window reference
        stream_diff, stream_n, stream_mismatch = 0.0, 0, 0
        for pos in test_pos[:6]:
            ends, logits = StreamingCNN(f, spec).process(sd.wave[pos].T.astype(np.float64))
            for t, lg in zip(ends, logits):
                w = sd.wave[pos][:, t - dsp.features.window_samples + 1:t + 1].astype(np.float64)
                ref_l = forward_window_folded(f, w)
                stream_diff = max(stream_diff, float(np.max(np.abs(lg - ref_l))))
                stream_mismatch += int(lg.argmax() != ref_l.argmax())
                stream_n += 1
        c = _common(args, dsp, recs, "cnn", ref["fold"], ref["protocol"], ref["seed"])
        cost = cnn_cost(spec)
        g = cost["geometry"]
        c.update({
            "input_scaling": {"channel_scale": res.scale.tolist(),
                              "rule": "x_scaled[c] = x[c] * scale[c], scale = 1/std of the fit records' DSP output",
                              "deployment": "folded into conv 1 weights (per input channel)"},
            "network": {"type": "causal 1D-CNN", "layers": [
                {"op": "conv1d", "W": list(cv["W"].shape), "b": list(cv["b"].shape), "stride": cv["stride"],
                 "padding": "none (valid)", "activation": "relu", "batchnorm": "folded"} for cv in f["convs"]]
                + [{"op": "global average over M final steps", "M": f["M"],
                    "deployment": "sliding sum of the last M outputs; 1/M folded into FC"},
                   {"op": "fc", "W": list(f["fc_W"].shape), "b": list(f["fc_b"].shape), "activation": "none"}],
                "geometry": g, **{k: v for k, v in cost.items() if k != "geometry"}},
            "time_boundary": f"final step j uses input samples [j*{g['D']}, j*{g['D']} + {g['RF'] - 1}] after SOS; "
                             f"a window [a, a+{spec.window - 1}] with a % {g['D']} == 0 is exactly steps "
                             f"a/{g['D']} .. a/{g['D']} + {g['M'] - 1}",
            "streaming_rule": f"layer state = inputs not yet consumed; final-layer sliding sum over {g['M']} steps; "
                              f"logits every {spec.step // g['D']} final steps starting at the window ending at "
                              f"sample {g['first_window_end']}; SOS/GAP clear everything",
            "weights_file": "cnn_weights.npz",
            "equivalence": {"window_folded_vs_torch_max_abs_logit_diff": float(np.max(np.abs(wl - tl))),
                            "window_argmax_mismatch": int(np.sum(wl.argmax(1) != tl.argmax(1))),
                            "n_windows": int(len(rec)),
                            "stream_vs_window_max_abs_logit_diff": stream_diff,
                            "stream_vs_window_argmax_mismatch": stream_mismatch,
                            "stream_windows_compared": stream_n}})
        np.savez(out / "cnn_weights.npz", **{f"conv{i}_W": cv["W"] for i, cv in enumerate(f["convs"])},
                 **{f"conv{i}_b": cv["b"] for i, cv in enumerate(f["convs"])}, fc_W=f["fc_W"], fc_b=f["fc_b"])
        (out / "cnn_contract.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
        done["cnn"] = c["equivalence"]
    return done
