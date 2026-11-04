"""E2 evaluation engine (one task per subject): retrain the E1b CNN per P2 fold and seed, then
compare float / folded float / int16-input float / fixed DSP + INT8 CNN on the same test records.

Data isolation: LSB, calibration ranges, quantisation parameters and the post-processing margin
threshold come from the fold's TRAINING records only; test records are used once for metrics.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import numpy as np

from emgml import labels
from emgml.datasets import grabmyo
from emgml.dsp import DSPConfig, EMGPreprocessPipeline
from emgml.experiment import _mask, build_subject, run_cnn, val_split
from emgml.metrics import summarize
from emgml.models.cnn import CNNSpec, fold_cnn
from emgml.splits import check_fold

from .cnn.int_model import StreamingIntCNN, forward_window_int, run_record_int
from .cnn.quantize import calibrate, describe, float_record, layer_geometry, quantise, to_arrays
from .dsp.fixed_dsp import FixedDSP, sections_from_config
from .esi import SourceCalibration, grabmyo_lsb, to_esi
from .postproc import NO_DECISION, PostProcessor, top2

LABELS = sorted(labels.UNIFIED)
REF_VERSION = "emg_fixed-e2-0.1"


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_arrays(d: dict) -> str:
    buf = io.BytesIO()
    np.savez(buf, **{k: np.asarray(v) for k, v in sorted(d.items())})
    return sha_bytes(buf.getvalue())


def ba(y, p):
    return summarize(np.asarray(y), np.asarray(p), LABELS)


def sqnr_db(ref: np.ndarray, test: np.ndarray) -> float:
    e = np.mean((ref - test) ** 2)
    s = np.mean(ref ** 2)
    return float(10 * np.log10(s / e)) if e > 0 else float("inf")


def postproc_eval(logits_by_rec: list[np.ndarray], y_by_rec: list[int], K: int, T: int) -> dict:
    outs, ys, first_ok = [], [], []
    for lg, y in zip(logits_by_rec, y_by_rec):
        p = PostProcessor(K, T)
        o = [p.step(v)["out"] for v in lg]
        outs += o
        ys += [y] * len(o)
        ok = [i for i, v in enumerate(o) if v == y]
        first_ok.append(ok[0] if ok else None)
    outs, ys = np.array(outs), np.array(ys)
    decided = outs != NO_DECISION
    mapped = np.where(outs < 0, -1, outs)
    rec = [np.mean(mapped[ys == c] == c) for c in LABELS if np.any(ys == c)]
    fo = [v for v in first_ok if v is not None]
    return {"K": K, "T": T, "balanced_accuracy_no_decision_wrong": float(np.mean(rec)),
            "accuracy_of_decided": float(np.mean(outs[decided] == ys[decided])) if decided.any() else None,
            "frac_no_decision": float(np.mean(~decided)),
            "first_correct_window_mean": float(np.mean(fo)) if fo else None,
            "records_never_correct": int(sum(v is None for v in first_ok))}


def transition_eval(q, dsp_secs, codes_rest: list, codes_gest: list, labels_gest: list, Ks=(1, 3, 5)) -> dict:
    """SYNTHETIC: REST record followed by a gesture record as one continuous stream (no reset at the
    boundary). Counts decisions after the boundary until the voted output equals the gesture."""
    res = {K: [] for K in Ks}
    for cr, cg, lab in zip(codes_rest, codes_gest, labels_gest):
        x = np.concatenate([cr, cg]).astype(np.int16)
        y = FixedDSP(dsp_secs, x.shape[1]).process(x, sos=True)
        r = run_record_int(q, y)
        boundary = cr.shape[0]
        after = r["ends"] >= boundary
        for K in Ks:
            p = PostProcessor(K, 0)
            outs = np.array([p.step(v)["out"] for v in r["logits"]])
            o = outs[after]
            hit = np.flatnonzero(o == lab)
            res[K].append(int(hit[0]) + 1 if hit.size else None)
    return {f"K{K}": {"decisions_after_boundary_mean": float(np.mean([v for v in res[K] if v is not None]))
                      if any(v is not None for v in res[K]) else None,
                      "never": int(sum(v is None for v in res[K])), "n": len(res[K])} for K in Ks}


def subject_task(args: dict) -> dict:
    import torch
    torch.set_num_threads(args["threads"])
    t0 = time.time()
    cfg, keys, subj = args["cfg"], args["keys"], args["subject"]
    d = cfg["data"]
    out_dir = Path(args["out_dir"])
    gidx = [i for i, k in enumerate(keys) if k[1] == subj]
    inv = {v: k for k, v in grabmyo.GRABMYO_TO_UNIFIED.items()}
    recs = [grabmyo.load_trial(args["raw"], keys[i][2], subj, inv[keys[i][4]], keys[i][3], "bip4", d["fs_out"])
            for i in gidx]
    assert [r.key for r in recs] == [keys[i] for i in gidx]
    dsp = DSPConfig.load(args["dsp_path"])
    dsp.n_ch, dsp.fs = 4, recs[0].fs
    spec = CNNSpec(in_ch=4, layers=[tuple(l) for l in cfg["cnn"]["layers"]], n_classes=6, window=400, step=100,
                   settle=200, fs=dsp.fs)
    sd = build_subject(recs, gidx, dsp)                                   # float input, float DSP (as E1b)
    secs = sections_from_config(dsp)
    pos_of = {g: p for p, g in enumerate(gidx)}
    results = []
    for fold in args["folds"]:
        check_fold(keys, fold)
        tr_pos = [pos_of[i] for i in fold.train]
        te_pos = [pos_of[i] for i in fold.test]
        # ---- ESI input contract: LSB from TRAINING raw samples only ----
        lsb = grabmyo_lsb(np.concatenate([recs[p].x for p in tr_pos]))
        cal = SourceCalibration("GRABMyo-1.1.0 replay (bip4)", "PRE_DSP", "mV", lsb, 4, recs[0].channel_names,
                                rule="P99.99(|x|) of training raw samples -> code 16384",
                                calibration_records=[list(keys[i]) for i in fold.train])
        codes, sat_in = [], {"train": 0, "test": 0}
        for p, r in enumerate(recs):
            c, n = to_esi(r.x, cal)
            codes.append(c)
            if p in te_pos:
                sat_in["test"] += n
            elif p in tr_pos:
                sat_in["train"] += n
        # ---- fixed DSP on all records of the subject (lanes = records x channels) ----
        X = np.concatenate(codes, axis=1)
        fd = FixedDSP(secs, X.shape[1])
        Y = fd.process(X, sos=True)
        post = [Y[:, 4 * p:4 * p + 4] for p in range(len(recs))]
        dsp_stats = {"internal_saturations": fd.stats["internal_saturations"],
                     "output_saturations": fd.stats["output_saturations"],
                     "max_abs_acc_bits": [int(m).bit_length() + 1 for m in fd.stats["max_abs_acc"]],
                     "max_abs_internal_v": fd.stats["max_abs_v"]}
        # float DSP on int16 input (variant 3)
        fl16 = [EMGPreprocessPipeline(dsp).run_record(c.astype(np.float64) * lsb).waveform for c in codes]
        # fixed vs float DSP error (same int16 input), test records
        e = np.concatenate([post[p].astype(np.float64) * lsb - fl16[p] for p in te_pos])[200:]
        sref = np.concatenate([fl16[p] for p in te_pos])[200:]
        dsp_err = {"rms_err_lsb": float(np.sqrt(np.mean(e ** 2)) / lsb), "max_err_lsb": float(np.max(np.abs(e)) / lsb),
                   "sqnr_db": sqnr_db(sref, sref + e)}
        me = _mask(sd, fold.test)
        y_test = sd.y[me]
        for seed in args["seeds"]:
            r_cnn, res = run_cnn(sd, keys, fold, seed, cfg["cnn"], spec, cfg["run"]["vote_k"], dsp)
            state = {k: v.detach().cpu().numpy() for k, v in res.model.state_dict().items()}
            mdir = out_dir / "models"
            mdir.mkdir(parents=True, exist_ok=True)
            mname = f"s{subj}_{fold.name}_seed{seed}"
            np.savez(mdir / f"{mname}_float.npz", channel_scale=res.scale, **state)
            model_sha = sha_arrays({"channel_scale": res.scale, **state})
            p1 = None
            f = fold_cnn(res.model, res.scale)
            # (1) PyTorch float on float DSP (E1b pipeline)
            from emgml.models.cnn import predict_cnn
            import torch as _t
            U = _t.from_numpy(sd.wave).unfold(2, 400, 100)
            p1 = predict_cnn(res, U, sd.rec[me], sd.kidx[me])
            # per-record helpers (window ends are 599..9999 for every record)
            def per_rec(fn):
                lg = [fn(p) for p in te_pos]
                return lg
            # (2) folded float64 on float DSP
            lg2 = per_rec(lambda p: float_record(f, sd.wave[p].T.astype(np.float64))["logits"])
            # (3) int16 input, float DSP, folded float
            lg3 = per_rec(lambda p: float_record(f, fl16[p])["logits"])
            # (4b) int16 input, fixed DSP, folded float
            fr4b = {p: float_record(f, post[p].astype(np.float64) * lsb) for p in te_pos}
            lg4b = [fr4b[p]["logits"] for p in te_pos]
            # quantisation: calibration on TRAINING records only
            calib = calibrate(f, [post[p] for p in tr_pos], lsb)
            q = quantise(f, lsb, calib, settle=200)          # E2 recorded schedule (first decision at 599)
            q.meta.update({"fold": fold.name, "subject": subj, "seed": seed, "model_sha256": model_sha,
                           "splits_sha256": args["splits_sha256"], "reference_version": REF_VERSION,
                           "calibration_records": cal.calibration_records})
            arrays = to_arrays(q)
            np.savez(mdir / f"{mname}_int8.npz", **arrays)
            (mdir / f"{mname}_int8.json").write_text(json.dumps(
                {"describe": describe(q), "meta": q.meta, "esi": cal.to_dict(), "dsp": FixedDSP(secs, 1).describe(),
                 "int8_arrays_sha256": sha_arrays(arrays)}, indent=1, default=float), encoding="utf-8")
            # (4) fixed DSP + INT8 CNN
            r4 = {p: run_record_int(q, post[p], keep=True) for p in te_pos}
            lg4 = [r4[p]["logits"] for p in te_pos]
            stack = lambda L: np.concatenate(L)
            a2, a3, a4b, a4 = (stack(L).argmax(1) for L in (lg2, lg3, lg4b, lg4))
            # per-layer error (int dequantised vs float, same fixed-DSP input) and saturation, test records
            layer_stats = []
            for l, L in enumerate(q.layers):
                fa = np.concatenate([fr4b[p]["acts"][l].ravel() for p in te_pos])
                ia = np.concatenate([r4[p]["act"][l].ravel() for p in te_pos])
                acc = np.concatenate([r4[p]["acc"][l].ravel() for p in te_pos])
                layer_stats.append({"layer": f"conv{l + 1}", "sqnr_db": sqnr_db(fa, ia * L.s_out),
                                    "frac_saturated_255": float(np.mean(ia == 255)),
                                    "frac_zero": float(np.mean(ia == 0)),
                                    "max_abs_acc_bits": int(np.max(np.abs(acc))).bit_length() + 1,
                                    "calib_p99_99": calib["layers"][l]["p"], "calib_max": calib["layers"][l]["max"]})
            lg_f = stack(lg4b)
            lg_i = stack(lg4) * q.s_fc
            gap_max = int(max(int(np.max(r4[p]["gap"])) for p in te_pos))
            layer_stats.append({"layer": "logits", "sqnr_db": sqnr_db(lg_f, lg_i),
                                "max_abs_logit_bits": int(np.max(np.abs(stack(lg4)))).bit_length() + 1,
                                "gap_sum_max": gap_max})
            # bit-exact check on real data: one test record, random chunking, vs window reference
            p_chk = te_pos[0]
            rng = np.random.default_rng(seed + 100 * subj)
            sc = StreamingIntCNN(q)
            E, Lg, pos = [], [], 0
            while pos < post[p_chk].shape[0]:
                n = int(rng.integers(1, 900))
                e_, l_ = sc.process(post[p_chk][pos:pos + n], sos=(pos == 0))
                E.append(e_)
                Lg.append(l_)
                pos += n
            Lg = np.concatenate([x for x in Lg if len(x)])
            W = np.array([forward_window_int(q, post[p_chk][t - 399:t + 1].T)["logits"] for t in r4[p_chk]["ends"]])
            exact = bool(np.array_equal(np.concatenate(E), r4[p_chk]["ends"]) and np.array_equal(Lg, r4[p_chk]["logits"])
                         and np.array_equal(W, r4[p_chk]["logits"]))
            # post-processing: margin threshold from TRAINING windows (10th percentile of correct margins)
            tr_marg = []
            for p in tr_pos[::3]:
                rr = run_record_int(q, post[p])
                for v, a in zip(rr["logits"], rr["argmax"]):
                    if a == recs[p].label:
                        tr_marg.append(top2(v)[1])
            T_train = int(np.percentile(tr_marg, 10)) if tr_marg else 0
            yb = [recs[p].label for p in te_pos]
            pp = [postproc_eval(lg4, yb, K, T) for K in (1, 3, 5) for T in (0, T_train)]
            trans = None
            if seed == args["seeds"][0]:
                rest = [p for p in te_pos if recs[p].label == 0]
                gest = [p for p in te_pos if recs[p].label != 0]
                pairs = [(r_, g_) for r_ in rest for g_ in gest if recs[r_].key[3] == recs[g_].key[3]]
                trans = transition_eval(q, secs, [codes[a] for a, _ in pairs], [codes[b] for _, b in pairs],
                                        [recs[b].label for _, b in pairs])
            results.append({
                "subject": subj, "fold": fold.name, "test_session": keys[fold.test[0]][2], "seed": seed,
                "model_file": f"models/{mname}_float.npz", "model_sha256": model_sha,
                "int8_file": f"models/{mname}_int8.npz", "int8_sha256": sha_arrays(arrays),
                "lsb_mV": lsb, "input_saturations": sat_in, "dsp_fixed": dsp_stats, "dsp_fixed_vs_float": dsp_err,
                "best_epoch": res.best_epoch,
                "v1_torch_float": ba(y_test, p1), "v2_folded_float": ba(y_test, a2),
                "v3_int16in_float_dsp": ba(y_test, a3), "v4b_fixed_dsp_float_cnn": ba(y_test, a4b),
                "v4_int8": ba(y_test, a4),
                "argmax_agree": {"v2_vs_v1": float(np.mean(a2 == p1)), "v4_vs_v1": float(np.mean(a4 == p1)),
                                 "v4_vs_v4b": float(np.mean(a4 == a4b)), "v3_vs_v2": float(np.mean(a3 == a2))},
                "layers": layer_stats, "quant": describe(q), "bit_exact_real_record": exact,
                "postproc": pp, "T_train": T_train, "transition_synthetic": trans,
                "e1b_window_ba": r_cnn["window"]["balanced_accuracy"]})
    return {"subject": subj, "results": results, "seconds": round(time.time() - t0, 1)}
