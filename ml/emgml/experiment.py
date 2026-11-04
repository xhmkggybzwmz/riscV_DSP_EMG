"""E1b experiment engine: LDA / MLP / CNN on identical record-level folds.

One task = (case, subject): loads that subject's recordings, runs the DSP per
recording (SOS at each recording), and trains/evaluates every requested model,
protocol, fold and seed. Folds come from the main process (splits manifest), so
all models see exactly the same train / validation / test records.

Rules enforced here (see docs/system_design_extend.md 9.2):
  - folds are record-level; windows inherit their record's side (check_fold on every fold)
  - validation records (early stopping, LDA feature-set choice) come from the training records only:
      MLP/CNN: the training records with the highest trial number present in training
      LDA    : inner folds over the training records (as in E1a)
  - standardisation (MLP) / channel scale (CNN) / int16 LSB come from training records only
  - test records are used once, for the final metrics
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import numpy as np

from . import labels
from .datasets import grabmyo
from .dsp import DSPConfig, EMGPreprocessPipeline
from .metrics import majority_vote, summarize
from .models.lda import FEATURE_SETS, make_lda
from .splits import Fold, check_fold, inner_folds, p1_folds, p2_folds
from .windowing import feature_columns

LABELS = sorted(labels.UNIFIED)


def record_keys(cfg: dict) -> list[tuple]:
    d = cfg["data"]
    return [(grabmyo.DATASET, p, s, t, grabmyo.GRABMYO_TO_UNIFIED[g])
            for p in d["participants"] for s in d["sessions"] for g in d["gestures"] for t in d["trials"]]


def make_folds(keys: list[tuple]) -> dict[str, list[Fold]]:
    return {"P1": p1_folds(keys), "P2": p2_folds(keys)}


def val_split(keys, train: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(fit, val) inside the training records: val = highest trial number present in training."""
    vt = max(keys[i][3] for i in train)
    val = tuple(i for i in train if keys[i][3] == vt)
    fit = tuple(i for i in train if keys[i][3] != vt)
    return fit, val


@dataclass
class SubjectData:
    keys: list[tuple]             # global keys of this subject's records (global index order)
    gidx: np.ndarray              # global record indices
    feats: np.ndarray             # [K, C*F]
    y: np.ndarray
    rec: np.ndarray               # window -> position in gidx
    kidx: np.ndarray              # window -> unfold index (start / step)
    wave: np.ndarray              # [R, C, T] float32 DSP output
    feat_names: list[str]
    clipped: dict | None = None


def quantise(x: np.ndarray, lsb: float) -> tuple[np.ndarray, int]:
    q = np.round(x / lsb)
    clipped = int(np.sum((q > 32767) | (q < -32768)))
    return np.clip(q, -32768, 32767) * lsb, clipped


def build_subject(recs, gidx, dsp: DSPConfig, lsb: float | None = None) -> SubjectData:
    f = dsp.features
    feats, ys, recpos, kk, waves = [], [], [], [], []
    names, clipped = None, {}
    for pos, r in enumerate(recs):
        x = r.x
        if lsb is not None:
            x, c = quantise(x, lsb)
            clipped[pos] = c
        out = EMGPreprocessPipeline(dsp).run_record(x)
        start = out.window_end - f.window_samples + 1
        if np.any(start % f.step_samples):
            raise AssertionError("window starts must be multiples of the step (unfold indexing)")
        feats.append(out.features.reshape(len(out.window_end), -1))
        ys.append(np.full(len(out.window_end), r.label))
        recpos.append(np.full(len(out.window_end), pos))
        kk.append(start // f.step_samples)
        waves.append(out.waveform.T.astype(np.float32))
        if names is None:
            names = [f"{c}:{k}" for c in r.channel_names for k in out.feature_names]
    return SubjectData([r.key for r in recs], np.asarray(gidx), np.concatenate(feats), np.concatenate(ys),
                       np.concatenate(recpos), np.concatenate(kk), np.stack(waves), names,
                       {"lsb": lsb, "clipped_samples_per_record": clipped} if lsb is not None else None)


def _mask(sd: SubjectData, rec_global: tuple[int, ...]) -> np.ndarray:
    pos = np.flatnonzero(np.isin(sd.gidx, np.asarray(rec_global)))
    return np.isin(sd.rec, pos)


def _metrics(y, pred, rec, vote_k):
    m = {"window": summarize(y, pred, LABELS)}
    for k in vote_k:
        if k > 1:
            m[f"vote{k}"] = summarize(y, majority_vote(pred, rec, k), LABELS)
    return m


def run_lda(sd: SubjectData, keys, fold: Fold, proto: str, feature_sets, vote_k) -> dict:
    scores = {}
    for fs in feature_sets:
        cols = feature_columns(sd.feat_names, FEATURE_SETS[fs])
        s = []
        for tr, va in inner_folds(keys, fold.train, proto):
            mt, mv = _mask(sd, tr), _mask(sd, va)
            m = make_lda().fit(sd.feats[mt][:, cols], sd.y[mt])
            s.append(summarize(sd.y[mv], m.predict(sd.feats[mv][:, cols]), LABELS)["balanced_accuracy"])
        scores[fs] = float(np.mean(s))
    chosen = max(feature_sets, key=lambda k: (scores[k], -feature_sets.index(k)))
    cols = feature_columns(sd.feat_names, FEATURE_SETS[chosen])
    mt, me = _mask(sd, fold.train), _mask(sd, fold.test)
    m = make_lda().fit(sd.feats[mt][:, cols], sd.y[mt])
    pred = m.predict(sd.feats[me][:, cols])
    return {"chosen_features": chosen, "inner_scores": scores, "n_train_windows": int(mt.sum()),
            **_metrics(sd.y[me], pred, sd.rec[me], vote_k)}


def run_mlp(sd: SubjectData, keys, fold: Fold, seed: int, mcfg: dict, vote_k) -> tuple[dict, object]:
    from .models.mlp import fold_standardisation, forward_folded, predict_mlp, train_mlp
    fit, val = val_split(keys, fold.train)
    cols = feature_columns(sd.feat_names, FEATURE_SETS[mcfg.get("features", "all5")])
    mf, mv, me = _mask(sd, fit), _mask(sd, val), _mask(sd, fold.test)
    res = train_mlp(sd.feats[mf][:, cols], sd.y[mf], sd.feats[mv][:, cols], sd.y[mv], tuple(mcfg["hidden"]),
                    len(LABELS), seed, mcfg["max_epochs"], mcfg["patience"], mcfg["lr"], mcfg["weight_decay"],
                    mcfg["batch"])
    pred = predict_mlp(res, sd.feats[me][:, cols])
    folded = forward_folded(fold_standardisation(res), sd.feats[me][:, cols]).argmax(1)
    return ({"n_fit_windows": int(mf.sum()), "n_val_windows": int(mv.sum()), "best_epoch": res.best_epoch,
             "epochs_run": len(res.history), "history": res.history,
             "folded_argmax_mismatch": int(np.sum(folded != pred)),
             **_metrics(sd.y[me], pred, sd.rec[me], vote_k)}, (res, cols))


def run_cnn(sd: SubjectData, keys, fold: Fold, seed: int, ccfg: dict, spec, vote_k, dsp: DSPConfig) -> tuple[dict, object]:
    import torch

    from .models.cnn import predict_cnn, train_cnn
    fit, val = val_split(keys, fold.train)
    mf, mv, me = _mask(sd, fit), _mask(sd, val), _mask(sd, fold.test)
    fit_pos = np.flatnonzero(np.isin(sd.gidx, np.asarray(fit)))
    w = sd.wave[fit_pos][:, :, dsp.features.settle_samples:]
    std = w.transpose(1, 0, 2).reshape(w.shape[1], -1).std(axis=1)
    scale = 1.0 / np.where(std > 0, std, 1.0)
    U = torch.from_numpy(sd.wave).unfold(2, dsp.features.window_samples, dsp.features.step_samples)
    res = train_cnn(U, sd.rec[mf], sd.kidx[mf], sd.y[mf], sd.rec[mv], sd.kidx[mv], sd.y[mv], scale, spec, seed,
                    ccfg["max_epochs"], ccfg["patience"], ccfg["lr"], ccfg["weight_decay"], ccfg["batch"])
    pred = predict_cnn(res, U, sd.rec[me], sd.kidx[me])
    return ({"n_fit_windows": int(mf.sum()), "n_val_windows": int(mv.sum()), "best_epoch": res.best_epoch,
             "epochs_run": len(res.history), "history": res.history, "channel_scale": scale.tolist(),
             **_metrics(sd.y[me], pred, sd.rec[me], vote_k)}, res)


def subject_task(args: dict) -> dict:
    """Runs in a worker process. args: cfg, case, subject, folds (per protocol), keys, out_dir."""
    import torch
    torch.set_num_threads(args["threads"])
    t0 = time.time()
    cfg, case, keys = args["cfg"], args["case"], args["keys"]
    d = cfg["data"]
    subj = args["subject"]
    gidx = [i for i, k in enumerate(keys) if k[1] == subj]
    recs = []
    for i in gidx:
        _, p, s, t, lab = keys[i]
        g = {v: k for k, v in grabmyo.GRABMYO_TO_UNIFIED.items()}[lab]
        recs.append(grabmyo.load_trial(args["raw"], s, p, g, t, case["channels"], d["fs_out"]))
    assert [r.key for r in recs] == [keys[i] for i in gidx]
    dsp = DSPConfig.load(args["dsp_path"])
    dsp.n_ch, dsp.fs = recs[0].n_ch, recs[0].fs
    dsp.validate()
    from .models.cnn import CNNSpec
    spec = CNNSpec(in_ch=dsp.n_ch, layers=[tuple(l) for l in cfg["cnn"]["layers"]], n_classes=len(LABELS),
                   window=dsp.features.window_samples, step=dsp.features.step_samples,
                   settle=dsp.features.settle_samples, fs=dsp.fs)
    quant = case.get("quant", "float")
    sd_float = build_subject(recs, gidx, dsp) if quant == "float" else None
    out = []
    keep = {}
    for proto in case["protocols"]:
        for fold in args["folds"][proto]:
            check_fold(keys, fold)
            fit, val = val_split(keys, fold.train)
            assert not (set(val) & set(fold.test)) and set(fit) | set(val) == set(fold.train)
            if quant == "float":
                sd = sd_float
            elif quant == "int16_train":
                x_tr = np.concatenate([recs[gidx.index(i)].x for i in fold.train])
                lsb = float(np.percentile(np.abs(x_tr), 99.99) / 16384.0)
                sd = build_subject(recs, gidx, dsp, lsb)
            else:
                raise ValueError(quant)
            base = {"case": case["name"], "protocol": proto, "subject": subj, "fold": fold.name,
                    "test_session": keys[fold.test[0]][2], "n_test_windows": int(_mask(sd, fold.test).sum())}
            if sd.clipped:
                base["int16_lsb_mV"] = sd.clipped["lsb"]
                base["test_clipped_samples"] = int(sum(sd.clipped["clipped_samples_per_record"][gidx.index(i)]
                                                       for i in fold.test))
            for model in case["models"]:
                seeds = case.get("seeds", {}).get(proto, cfg["run"]["seeds"][proto]) if model != "lda" else [0]
                for seed in seeds:
                    t1 = time.time()
                    if model == "lda":
                        r = run_lda(sd, keys, fold, proto, cfg["run"]["feature_sets"], cfg["run"]["vote_k"])
                    elif model == "mlp":
                        r, obj = run_mlp(sd, keys, fold, seed, cfg["mlp"], cfg["run"]["vote_k"])
                        keep[("mlp", proto, fold.name, seed)] = obj
                    elif model == "cnn":
                        r, obj = run_cnn(sd, keys, fold, seed, cfg["cnn"], spec, cfg["run"]["vote_k"], dsp)
                        keep[("cnn", proto, fold.name, seed)] = obj
                    else:
                        raise ValueError(model)
                    out.append({**base, "model": model, "seed": seed, "seconds": round(time.time() - t1, 2), **r})
    contracts = {}
    ref = args.get("reference_fold")
    if ref and subj == ref["subject"]:
        from .export import export_reference
        contracts = export_reference(keep, sd_float, keys, args, dsp, spec, recs, gidx)
    return {"case": case["name"], "subject": subj, "results": out, "seconds": round(time.time() - t0, 1),
            "contracts": contracts, "pid": os.getpid()}
