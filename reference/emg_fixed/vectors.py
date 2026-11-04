"""Golden test vectors for the future DSP / CNN RTL testbenches ($readmemh friendly).

Every *.hex file holds one packed word per line (no comments, no addresses), hexadecimal,
fixed width. A word packs fields from the least significant bit upwards:
    word = data[0] | data[1] << w | ... | data[n-1] << (n-1)w | meta fields above the data
Signed elements are stored as w-bit two's complement. Field layouts, counts and the stage
(PRE_DSP / POST_DSP) of every file are in manifest.json.

ESI frame word (176 bits): lane0..lane7 int16 (bits 0..127), flags uint16 (128..143), seq uint32 (144..175).
Layer words: data elements, then step index j (24 bits), then segment number (8 bits; increments at
every SOS/GAP reset, j restarts at 0).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .cnn.int_model import StreamingIntCNN, forward_window_int, run_record_int
from .dsp.fixed_dsp import FixedDSP
from .esi import FLAG_GAP, FLAG_SOS, NCH_MAX, Frames, reset_points
from .postproc import PostProcessor

VECTOR_VERSION = "e2-vectors-0.2"   # 0.2: decision schedule settle = 0 (first decision at index 399), K = 1


def pack(values, w: int) -> int:
    word = 0
    for i, v in enumerate(values):
        word |= (int(v) & ((1 << w) - 1)) << (w * i)
    return word


def write_words(path: Path, words: list[int], width_bits: int) -> dict:
    n = (width_bits + 3) // 4
    mask = (1 << width_bits) - 1
    for w in words:                     # signed values are written as width_bits two's complement
        if not -(1 << width_bits) < int(w) < (1 << width_bits):
            raise ValueError(f"value {w} does not fit {width_bits} bits")
    path.write_text("".join(format(int(w) & mask, f"0{n}X") + "\n" for w in words), encoding="ascii")
    return {"file": path.name, "words": len(words), "width_bits": width_bits,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def frame_words(fr: Frames) -> list[int]:
    return [pack(list(fr.lanes[t]), 16) | (int(fr.flags[t]) << 128) | (int(fr.seq[t]) << 144)
            for t in range(len(fr.seq))]


def segments(fr: Frames) -> list[tuple[int, int, bool]]:
    """(start, end, gap) of each stream segment between resets (frame indices)."""
    rp = list(reset_points(fr))
    if not rp or rp[0] != 0:
        raise ValueError("first frame must carry SOS")
    bounds = rp + [len(fr.seq)]
    return [(a, b, bool(fr.flags[a] & FLAG_GAP)) for a, b in zip(bounds[:-1], bounds[1:])]


def run_scenario(fr: Frames, q, dsp_sections, n_ch: int, post_dsp_input: bool, K: int = 1, chunk: int = 97) -> dict:
    """Drive the streaming references frame by frame (resets only from the frame flags),
    and cross-check every segment against the window and whole-record references."""
    codes = fr.lanes[:, :n_ch]
    dsp = FixedDSP(dsp_sections, n_ch)
    cnn = StreamingIntCNN(q, record=True)
    post = PostProcessor(K, 0)
    out = {"dsp": [], "layers": [[] for _ in q.layers], "acc": [[] for _ in q.layers], "gap": [], "logits": [],
           "decisions": []}
    for seg_no, (a, b, gap) in enumerate(segments(fr)):
        x = codes[a:b]
        y_all = []
        dsp.reset()
        cnn.reset()
        post.reset()
        for c0 in range(0, b - a, chunk):
            xc = x[c0:c0 + chunk]
            y = xc.copy() if post_dsp_input else dsp.process(xc)
            y_all.append(y)
            ends, lg = cnn.process(y)
            for t, l in zip(ends, lg):
                r = post.step(l)
                out["decisions"].append((seg_no, int(t), int(np.argmax(l)), r["out"], r["margin"]))
        y_all = np.concatenate(y_all)
        out["dsp"].append(y_all)
        # cross-check: whole-segment and window references
        rr = run_record_int(q, y_all, keep=True)
        st_l = np.array([l for _, l in cnn.log["logits"]]).reshape(-1, q.fc_bq.shape[0])
        if not np.array_equal(st_l, rr["logits"]):
            raise AssertionError(f"segment {seg_no}: stream != record")
        for t, l in zip(rr["ends"], rr["logits"]):
            if not np.array_equal(forward_window_int(q, y_all[t - q.window + 1:t + 1].T)["logits"], l):
                raise AssertionError(f"segment {seg_no}: window != record at {t}")
        for i in range(len(q.layers)):
            out["layers"][i].append((seg_no, rr["act"][i]))
            out["acc"][i].append((seg_no, rr["acc"][i]))
        out["gap"] += [(seg_no, int(t), g) for t, g in zip(rr["ends"], rr["gap"])]
        out["logits"] += [(seg_no, int(t), l) for t, l in zip(rr["ends"], rr["logits"])]
    return out


def export_scenario(d: Path, name: str, fr: Frames, q, dsp_sections, n_ch: int, stage: str, with_acc: bool,
                    description: str, K: int = 1) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    res = run_scenario(fr, q, dsp_sections, n_ch, post_dsp_input=(stage == "POST_DSP"), K=K)
    files = {"input_frames": write_words(d / "input_frames.hex", frame_words(fr), 176)}
    dsp_words = [pack(list(v) + [0] * (NCH_MAX - n_ch), 16) for seg in res["dsp"] for v in seg]
    files["dsp_out"] = write_words(d / "dsp_out.hex", dsp_words, 128)
    for i, L in enumerate(q.layers):
        c = L.wq.shape[0]
        words = [pack(a[:, j], 8) | (j << (8 * c)) | (seg << (8 * c + 24))
                 for seg, a in res["layers"][i] for j in range(a.shape[1])]
        files[f"conv{i + 1}_act"] = write_words(d / f"conv{i + 1}_act.hex", words, 8 * c + 32)
        if with_acc:
            words = [pack(a[:, j], 32) | (j << (32 * c)) | (seg << (32 * c + 24))
                     for seg, a in res["acc"][i] for j in range(a.shape[1])]
            files[f"conv{i + 1}_acc"] = write_words(d / f"conv{i + 1}_acc.hex", words, 32 * c + 32)
    c3 = q.layers[-1].wq.shape[0]
    files["gap_sum"] = write_words(d / "gap_sum.hex", [pack(g, 16) | (t << (16 * c3)) | (s << (16 * c3 + 24))
                                                      for s, t, g in res["gap"]], 16 * c3 + 32)
    n_cls = q.fc_bq.shape[0]
    files["logits"] = write_words(d / "logits.hex", [pack(l, 32) | (t << (32 * n_cls)) | (s << (32 * n_cls + 24))
                                                     for s, t, l in res["logits"]], 32 * n_cls + 32)
    files["decisions"] = write_words(d / "decisions.hex", [
        (am & 0xF) | ((out & 0xF) << 4) | ((min(mg, (1 << 32) - 1)) << 8) | (t << 40) | (s << 64)
        for s, t, am, out, mg in res["decisions"]], 72)
    return {"name": name, "stage": stage, "description": description, "frames": len(fr.seq),
            "segments": [{"start": a, "end": b, "starts_with_gap": g} for a, b, g in segments(fr)],
            "decisions": len(res["decisions"]), "files": files}


def export_model(d: Path, q, dsp_sections) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for i, L in enumerate(q.layers):
        n = i + 1
        files[f"conv{n}_w"] = write_words(d / f"conv{n}_w.hex", [int(v) for v in L.wq.ravel()], 8)
        files[f"conv{n}_b"] = write_words(d / f"conv{n}_b.hex", [int(v) for v in L.bq], 32)
        files[f"conv{n}_M"] = write_words(d / f"conv{n}_M.hex", [int(v) for v in L.M], 16)
        files[f"conv{n}_s"] = write_words(d / f"conv{n}_s.hex", [int(v) for v in L.s], 8)
    files["fc_w"] = write_words(d / "fc_w.hex", [int(v) for v in q.fc_wq.ravel()], 8)
    files["fc_b"] = write_words(d / "fc_b.hex", [int(v) for v in q.fc_bq], 32)
    coefs = []
    for s in dsp_sections:
        coefs += list(s.b) + list(s.a)
    files["dsp_coef"] = write_words(d / "dsp_coef.hex", coefs, 18)
    return files
