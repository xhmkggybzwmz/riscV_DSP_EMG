#!/usr/bin/env python3
"""E4c-A: multi-gesture replay inputs from six real GRABMyo test segments (no new model, no retraining).

    python ml/scripts/e4c_multigesture_eval.py     # -> build/e4c/mg6_selection.json (which segments)
    python scripts/sim/gen_e4c.py                  # -> vectors, firmware header, expectations

The RV32I firmware (firmware/rv32/mailbox/mg6_common.h) plays the six selected segments one after the
other: it writes segment s into the replay frame memory through WADDR / WDATA_LO / WDATA_HI /
WFLAGS while RUN = 0 (esi_replay_axil), then runs it PASSES times; each run = replay SOFT_RESET
(replay only: DONE, k, counters), SEQ_BASE = MG6_SEQ0 + run * LEN, RUN without LOOP. Frame 0 of the
segment carries SOS, so DSP and CNN clear their stream state at every run while the CNN result
numbering continues. The ESI stream the pipeline sees is therefore: for s in 0..5, for p in 0..P-1:
the L frames of segment s, SOS on the first, seq continuous from MG6_SEQ0; per run the replay
timestamp restarts at 0.

Writes
  build/e4c/vec/{model/, chain_mg6_p1/, chain_mg6_p2/, chain_mg6_p4/, index.json}
        E3 chain cases (scripts/sim/run_e3.py --vec build/e4c/vec): that exact stream for P = 1, 2, 4,
        every DSP frame, layer, GAP sum and score from the E2 reference (emg_fixed.vectors), plus
        rd_expect.hex: per decision {ts[63:32], seq[31:0]} for the SoC test bench reader
  build/e4c/fw/mg6_data.h               frames of the six segments + expected results; included by
                                        firmware/rv32/mg6 (build_asm_tests.py --inc build/e4c/fw). Kept
                                        out of git: it holds real GRABMyo sample values (CC BY 4.0 data;
                                        regenerate it from the local dataset with this script)
  build/e4c/fw/mg6_data.json            provenance: model, ESI LSB, DSP mask, records (sha256), frame
                                        ranges, packing, header / frame-table sha256
  host/gesture_server/tests/fixtures/e4c_mg6_expected.json   expected decisions only (test fixture)
  build/e4c/mg6_golden.json             per segment: class, source record, decisions with scores
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "ml"), str(REPO / "reference"), str(REPO / "scripts" / "sim")]

import gen_e3_vectors as G3  # noqa: E402  (same model, calibration and DSP sections as E3)
from emg_fixed import vectors  # noqa: E402
from emg_fixed.esi import FLAG_SOS, SRC_REPLAY, make_frames, to_esi  # noqa: E402
from emgml.datasets import grabmyo  # noqa: E402

OUT = REPO / "build" / "e4c"
HDR = OUT / "fw" / "mg6_data.h"            # git-ignored: contains real GRABMyo samples (ESI codes)
FIXTURE = REPO / "host" / "gesture_server" / "tests" / "fixtures" / "e4c_mg6_expected.json"
MG6_SEQ0 = 0
PASSES = (1, 2, 4)


def s32(v: int) -> int:
    v &= 0xFFFF_FFFF
    return v - (1 << 32) if v & 0x8000_0000 else v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selection", default=str(OUT / "mg6_selection.json"))
    ns = ap.parse_args()
    sel = json.loads(Path(ns.selection).read_text(encoding="utf-8"))
    q, cal = G3.model(), G3.calibration()
    import hashlib
    if hashlib.sha256(G3._model_files()[0].read_bytes()).hexdigest() != sel["model"]["int8_npz_sha256"]:
        sys.exit("selection was made with another model file")
    secs = G3.sections(G3.DEFAULT_MASK)
    L = sel["segment_length"]
    segs, info = [], []
    for s in sel["selection"]:
        if s.get("selected", True) is None:
            sys.exit(f"no usable segment for {s['label']}")
        r = grabmyo.load_trial(G3.RAW, s["session"], s["participant"], s["gesture"], s["trial"], "bip4", G3.FS)
        codes, nsat = to_esi(r.x, cal)
        seg = codes[s["start"]:s["start"] + L]
        segs.append(seg)
        info.append({"class": s["class"], "label": s["label"], "gesture": s["gesture"], "session": s["session"],
                     "participant": s["participant"], "trial": s["trial"], "start": s["start"], "length": L,
                     "source": r.source_path, "sha256": r.sha256, "input_saturations_in_record": int(nsat)})
    vec = OUT / "vec"
    if vec.exists():
        shutil.rmtree(vec)
    vec.mkdir(parents=True)
    if not (REPO / "build" / "e3" / "vec" / "model").is_dir():      # first run on a machine
        subprocess.run([sys.executable, str(REPO / "scripts" / "sim" / "gen_e3_vectors.py")], check=True)
    shutil.copytree(REPO / "build" / "e3" / "vec" / "model", vec / "model")
    metas, per_seg = [], None
    for P in PASSES:
        runs = [(si, p) for si in range(len(segs)) for p in range(P)]
        x = np.concatenate([segs[si] for si, _ in runs])
        fr = make_frames(x, SRC_REPLAY, seq0=MG6_SEQ0, sos_at=tuple(i * L for i in range(len(runs))))
        d = vec / f"chain_mg6_p{P}"
        scen = vectors.export_scenario(d, d.name, fr, q, secs, 4, "PRE_DSP", True,
                                       f"six real GRABMyo test segments (E4c-A selection), {L} frames each, "
                                       f"{P} run(s) per segment, SOS at every run, seq continuous")
        metas.append(G3.cnn_meta(d, "chain", scen, "input_frames.hex", True, scen["description"]))
        logits = [int(w, 16) for w in (d / "logits.hex").read_text().split()]
        decs = [int(w, 16) for w in (d / "decisions.hex").read_text().split()]
        nd = len(decs) // len(runs)
        rows = []
        for k, (lw, dw) in enumerate(zip(logits, decs)):
            run = k // nd
            end = (dw >> 40) & 0xFFFFFF
            rows.append({"run": run, "segment": run // P, "scores": [s32(lw >> (32 * c)) for c in range(6)],
                         "argmax": dw & 0xF, "end": end, "seg": (dw >> 64) & 0xFF})
        # every run of a segment must give the same decisions (SOS clears all state)
        for si in range(len(segs)):
            rr = [[(r["scores"], r["argmax"], r["end"]) for r in rows if r["run"] == si * P + p] for p in range(P)]
            if any(x != rr[0] for x in rr):
                sys.exit(f"P={P}: runs of segment {si} differ")
        # SoC test bench reader expectation per decision: {ts, seq}
        exp = [((r["end"]) << 32) | ((MG6_SEQ0 + r["run"] * L + r["end"]) & 0xFFFF_FFFF) for r in rows]
        vectors.write_words(d / "rd_expect.hex", exp, 64)
        if P == 1:
            per_seg = [[r for r in rows if r["segment"] == si] for si in range(len(segs))]
    (vec / "index.json").write_text(json.dumps({"model_npz_sha256": sel["model"]["int8_npz_sha256"], "settle": q.settle,
                                                "cases": metas}, indent=1), encoding="utf-8")
    nd = len(per_seg[0])
    # check against the selection (computed independently in e4c_multigesture_eval.py)
    for si, s in enumerate(sel["selection"]):
        got = [r["argmax"] for r in per_seg[si]]
        if got != [s["class"]] * s["decisions"]:
            sys.exit(f"segment {si}: decisions {got} != class {s['class']}")
    # firmware header
    lines = ["/* mg6_data.h - GENERATED by scripts/sim/gen_e4c.py from build/e4c/mg6_selection.json. Do not edit.",
             " * Six real GRABMyo test segments (session 3, participant 1 = the frozen E2 reference fold's test day),",
             " * one per class, curated by ml/scripts/e4c_multigesture_eval.py; expected results from the E2",
             " * fixed-point reference (fixed DSP + INT8 CNN, settle 0) for a run that starts with SOS. */",
             "#ifndef MG6_DATA_H", "#define MG6_DATA_H", "", "#include <stdint.h>", "",
             f"#define MG6_NSEG {len(segs)}", f"#define MG6_LEN {L}", f"#define MG6_NDEC {nd}",
             f"#define MG6_SEQ0 {MG6_SEQ0}u", "#define MG6_FLAG_SOS 0x01u", "",
             "typedef struct { int32_t score[6]; uint32_t argmax, end; } mg6_result_t;", "",
             "/* class of each segment (E2/E3 order: REST, HAND_CLOSE, HAND_OPEN, WRIST_FLEX, WRIST_EXT, PINCH) */",
             "static const uint32_t mg6_class[MG6_NSEG] = {" + ", ".join(str(i["class"]) for i in info) + "};", "",
             "static const mg6_result_t mg6_expected[MG6_NSEG][MG6_NDEC] = {"]
    for si in range(len(segs)):
        lines.append("    {" + ", ".join("{{" + ", ".join(str(v) for v in r["scores"]) + f"}}, {r['argmax']}, {r['end']}}}"
                                           for r in per_seg[si]) + "},")
    lines += ["};", "", "/* frames: {lane1 << 16 | lane0, lane3 << 16 | lane2} (int16 ESI codes, PRE_DSP) */",
              "static const uint32_t mg6_frames[MG6_NSEG][MG6_LEN][2] = {"]
    for si, seg in enumerate(segs):
        lines.append(f"    {{ /* {info[si]['label']}: {info[si]['source']} frames {info[si]['start']}.."
                     f"{info[si]['start'] + L - 1} */")
        u = seg.astype(np.int64) & 0xFFFF
        lo = (u[:, 1] << 16) | u[:, 0]
        hi = (u[:, 3] << 16) | u[:, 2]
        for i in range(0, L, 4):
            lines.append("        " + " ".join(f"{{0x{a:08X}u, 0x{b:08X}u}}," for a, b in zip(lo[i:i + 4], hi[i:i + 4])))
        lines.append("    },")
    lines += ["};", "", "#endif", ""]
    HDR.parent.mkdir(parents=True, exist_ok=True)
    HDR.write_text("\n".join(lines), encoding="ascii", newline="\n")
    words = b"".join(int(w).to_bytes(4, "little") for seg in segs for v in (seg.astype(np.int64) & 0xFFFF)
                     for w in ((v[1] << 16) | v[0], (v[3] << 16) | v[2]))
    prov = {"generator": "scripts/sim/gen_e4c.py",
            "selection_sha256": hashlib.sha256(Path(ns.selection).read_bytes()).hexdigest(),
            "model": sel["model"], "esi_lsb_mV": cal.lsb, "esi_rule": cal.rule, "dsp_mask": G3.DEFAULT_MASK,
            "segment_length": L, "decisions_per_run": nd, "seq0": MG6_SEQ0,
            "flags": {"frame0": "SOS (0x01)", "others": 0},
            "packing": "per frame two little-endian words {lane1<<16|lane0, lane3<<16|lane2} (int16 lanes); "
                       "replay word {flags[7:0], lane3, lane2, lane1, lane0}",
            "segments": info, "frame_words_le_sha256": hashlib.sha256(words).hexdigest(),
            "frame_bytes": len(words), "header_sha256": hashlib.sha256(HDR.read_bytes()).hexdigest()}
    (HDR.parent / "mg6_data.json").write_text(json.dumps(prov, indent=1), encoding="utf-8")
    gold = {"model": sel["model"], "segment_length": L, "decisions_per_run": nd, "seq0": MG6_SEQ0,
            "flags_frame0": FLAG_SOS, "segments": [{**info[si], "decisions": [
                {k: r[k] for k in ("scores", "argmax", "end")} for r in per_seg[si]]} for si in range(len(segs))]}
    (OUT / "mg6_golden.json").write_text(json.dumps(gold, indent=1), encoding="utf-8")
    # small tracked fixture for the PS / WebSocket / browser compatibility tests (no frames)
    fix = {"generated_by": "scripts/sim/gen_e4c.py", "segment_length": L, "seq0": MG6_SEQ0,
           "segments": [{k: v for k, v in g.items() if k not in ("sha256", "input_saturations_in_record")}
                        for g in gold["segments"]]}
    FIXTURE.write_text(json.dumps(fix, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(metas)} chain cases -> {vec}; {HDR.relative_to(REPO)} ({len(segs)} x {L} frames, {nd} decisions each); "
          f"build/e4c/mg6_golden.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
