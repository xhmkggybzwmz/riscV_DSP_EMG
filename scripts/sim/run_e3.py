"""E3a/E3b RTL regression: DSP, CNN and DSP+CNN chain vs the E2 fixed-point reference.

    python scripts/sim/run_e3.py                 # all cases x flow/backpressure configurations (Icarus)
    python scripts/sim/run_e3.py --mutants       # negative controls: every mutant must FAIL
    python scripts/sim/run_e3.py --xsim          # Vivado 2026.1 xsim cross-check of key cases
    python scripts/sim/run_e3.py --only "cnn_*"  # subset (fnmatch on case names)
    python scripts/sim/run_e3.py --vec build/e4c/vec   # another vector root (own index.json / model;
                                                       # compile, logs and summary stay under that root)

Vectors: build/e3/vec (scripts/sim/gen_e3_vectors.py, regenerated with --regen). Results in build/e3/.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build" / "e3"
VEC = BUILD / "vec"
IVERILOG = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin")) / "iverilog.exe"
VVP = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin")) / "vvp.exe"
VIV_BIN = Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado")) / "bin"

RTL_DSP = ["rtl/dsp/emg_dsp.v"]
RTL_CNN = ["rtl/infer/emg_cnn.v"]
BFM = ["dv/esi/esi_src_bfm.v", "dv/esi/esi_sink_bfm.v", "dv/esi/esi_monitor.v"]
TARGETS = {
    "dsp":        {"top": "tb_emg_dsp", "src": RTL_DSP + BFM + ["dv/e3/tb_emg_dsp.v"], "defs": []},
    "dsp_noinit": {"top": "tb_emg_dsp", "src": RTL_DSP + BFM + ["dv/e3/tb_emg_dsp.v"], "defs": ["NO_COEF_INIT"]},
    "cnn":        {"top": "tb_emg_cnn", "src": RTL_CNN + BFM + ["dv/e3/tb_emg_cnn.v"], "defs": []},
    "chain":      {"top": "tb_emg_cnn", "src": RTL_DSP + RTL_CNN + BFM + ["dv/e3/tb_emg_cnn.v"], "defs": ["CHAIN"]},
}
CONFIGS = {"flow": (100, 100, 1), "bp1": (60, 40, 2), "bp2": (30, 75, 3)}     # (valid %, ready %, seed)
MODEL_FILES = {"W1": "conv1_w", "B1": "conv1_b", "M1": "conv1_M", "SH1": "conv1_s", "W2": "conv2_w", "B2": "conv2_b",
               "M2": "conv2_M", "SH2": "conv2_s", "W3": "conv3_w", "B3": "conv3_b", "M3": "conv3_M", "SH3": "conv3_s",
               "FCW": "fc_w", "FCB": "fc_b"}


def win(p: Path) -> str:
    return str(p).replace("\\", "/")


def write_model_paths(model_dir: Path, out: Path) -> None:
    lines = [f'`define {k}_FILE "{win(model_dir / (v + ".hex"))}"' for k, v in MODEL_FILES.items()]
    lines.append(f'`define DSP_COEF_FILE "{win(model_dir / "dsp_coef4.hex")}"')
    out.write_text("\n".join(lines) + "\n", encoding="ascii")


def compile_target(name: str, outdir: Path, src_root: Path = REPO, inc: Path = BUILD) -> Path:
    t = TARGETS[name]
    outdir.mkdir(parents=True, exist_ok=True)
    vvp = outdir / f"{name}.vvp"
    cmd = [str(IVERILOG), "-g2001", "-Wall", "-I", str(REPO / "rtl/esi"), "-I", str(REPO / "dv/common"),
           "-I", str(inc), "-s", t["top"], "-o", str(vvp)] + [f"-D{d}" for d in t["defs"]] \
        + [str(src_root / s) for s in t["src"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    (outdir / f"{name}.compile.log").write_text(" ".join(cmd) + "\n" + r.stdout + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"compile {name} failed:\n{r.stderr[-2000:]}")
    return vvp


def plusargs(meta: dict, cfg: str, vec_dir: Path) -> list[str]:
    v, r, seed = CONFIGS[cfg]
    if meta["tb"] == "dsp":
        a = [f"+frames={meta['frames']}", f"+mask={meta['mask']}", f"+cfgchg_at={meta['cfgchg_at']}",
             f"+cfgchg_mask={meta['cfgchg_mask']}", f"+exp_int_sat={meta['expect_int_sat']}",
             f"+exp_out_sat={meta['expect_out_sat']}"]
        if meta["coef_via_port"]:
            a += ["+coefport=1", f"+coef={win(VEC / 'model' / 'dsp_coef4.hex')}"]
    else:
        c = meta["counts"]
        a = [f"+input={meta['input']}", f"+frames={meta['frames']}", f"+n1={c['conv1_act']}", f"+n2={c['conv2_act']}",
             f"+n3={c['conv3_act']}", f"+ngap={c['gap_sum']}", f"+nlog={c['logits']}", f"+acc={int(meta['with_acc'])}"]
        if meta["tb"] == "chain":
            a.append(f"+ndsp={c['dsp_out']}")
    return [f"+vec={win(vec_dir)}"] + a + [f"+vpct={v}", f"+rpct={r}", f"+seed={seed}"]


def target_of(meta: dict) -> str:
    if meta["tb"] == "dsp":
        return "dsp_noinit" if meta["coef_via_port"] else "dsp"
    return meta["tb"]


def run_case(vvp: Path, meta: dict, cfg: str, logdir: Path, vec_root: Path = VEC) -> dict:
    t0 = time.time()
    args = plusargs(meta, cfg, vec_root / meta["name"])
    r = subprocess.run([str(VVP), "-n", str(vvp)] + args, capture_output=True, text=True, timeout=3600)
    log = r.stdout + r.stderr
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / f"{meta['name']}__{cfg}.log").write_text(" ".join(args) + "\n" + log, encoding="utf-8")
    m = re.search(r"TEST_RESULT: (PASS|FAIL) checks=(\d+) errors=(\d+)", log)
    perf = [l for l in log.splitlines() if l.startswith("PERF:")]
    errs = [l for l in log.splitlines() if l.startswith("ERROR")]
    ok = bool(m and m.group(1) == "PASS" and not errs)
    return {"case": meta["name"], "cfg": cfg, "ok": ok, "seconds": round(time.time() - t0, 1),
            "perf": perf[0] if perf else "", "first_error": errs[0] if errs else ""}


# ---------------------------------------------------------------- negative controls
MUTANTS = [
    ("M1_mac_off_by_one", "rtl/infer/emg_cnn.v", "if (p3) acc <= acc + {{7{prod[24]}}, prod};",
     "if (p3) acc <= acc + {{7{prod[24]}}, prod} + (wcnt == 13'd77);", "cnn_s1_normal", "flow"),
    ("M2_requant_shift", "rtl/infer/emg_cnn.v", "(64'd1 << (s_o - 1))) >> s_o;", "(64'd1 << (s_o - 1))) >> (s_o + 1'b1);",
     "cnn_s1_normal", "flow"),
    ("M3_dsp_negative_rounding", "rtl/dsp/emg_dsp.v", "wire signed [31:0] v_raw = acc_r >>> 16;",
     "wire signed [31:0] v_raw = acc[47] ? (acc >>> 16) : (acc_r >>> 16);", "dsp_multich", "flow"),
    ("M4_weight_byte", "MODEL:conv2_w.hex", 1234, None, "cnn_s1_normal", "flow"),
    ("M5a_gap_length", "rtl/infer/emg_cnn.v", "((rfill == MGAP) ? ring[rhead * C3 + o] : 8'd0)",
     "((rfill == MGAP - 1) ? ring[rhead * C3 + o] : 8'd0)", "cnn_s1_normal", "flow"),
    ("M5b_conv3_stride_phase", "rtl/infer/emg_cnn.v", "                        ph2 <= 4'd0; ph3 <= 4'd0; dph <= 4'd0;",
     "                        ph2 <= 4'd0; ph3 <= 4'd1; dph <= 4'd0;", "cnn_s2_sos_restart", "flow"),
    ("M6_dsp_accept_while_busy", "rtl/dsp/emg_dsp.v", "assign s_tready = (state == S_IDLE) && !rst;",
     "assign s_tready = !rst;", "dsp_multich", "bp1"),
    ("M7_dsp_drop_valid_before_handshake", "rtl/dsp/emg_dsp.v",
     "                    end else if (m_tready) begin\n                        m_tvalid_r <= 1'b0;",
     "                    end else if (m_tready || stat_frames_in[3]) begin\n                        m_tvalid_r <= 1'b0;",
     "dsp_multich", "bp1"),
]


def run_mutants(index: dict) -> list[dict]:
    metas = {m["name"]: m for m in index["cases"]}
    out = []
    for name, file, a, b, case, cfg in MUTANTS:
        d = BUILD / "mut" / name
        if d.exists():
            shutil.rmtree(d)
        (d / "rtl").mkdir(parents=True)
        shutil.copytree(REPO / "rtl", d / "rtl", dirs_exist_ok=True)
        shutil.copytree(REPO / "dv", d / "dv")
        inc = d / "inc"
        inc.mkdir()
        model = VEC / "model"
        if file.startswith("MODEL:"):
            model = d / "model"
            shutil.copytree(VEC / "model", model)
            f = model / file.split(":", 1)[1]
            lines = f.read_text().split()
            lines[a] = format((int(lines[a], 16) + 1) & 0xFF, "02X")
            f.write_text("\n".join(lines) + "\n")
        else:
            f = d / file
            s = f.read_text(encoding="utf-8")
            if s.count(a) != 1:
                raise RuntimeError(f"{name}: pattern not found once")
            f.write_text(s.replace(a, b), encoding="utf-8")
        write_model_paths(model, inc / "model_paths.vh")
        meta = metas[case]
        vvp = compile_target(target_of(meta), d, src_root=d, inc=inc)
        r = run_case(vvp, meta, cfg, d / "logs")
        r.update({"mutant": name, "detected": not r["ok"]})
        out.append(r)
        print(f"  {name:38s} on {case}/{cfg}: {'DETECTED' if not r['ok'] else 'NOT DETECTED'}  {r['first_error'][:110]}",
              flush=True)
    return out


# ---------------------------------------------------------------- xsim
XSIM_CASES = [("dsp_sos_gap", "bp1"), ("dsp_cfg_change", "bp2"), ("cnn_s1_normal", "bp1"), ("cnn_s4_saturation", "flow"),
              ("chain_s3_gap", "bp1"), ("cnn_real_g16_t1", "flow")]


def run_xsim(index: dict) -> list[dict]:
    metas = {m["name"]: m for m in index["cases"]}
    env = dict(os.environ)
    env["PATH"] = str(VIV_BIN) + os.pathsep + env["PATH"]
    out = []
    for case, cfg in XSIM_CASES:
        meta = metas[case]
        tgt = TARGETS[target_of(meta)]
        d = BUILD / "xsim" / f"{case}__{cfg}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        prj = ["-i", win(REPO / "rtl/esi"), "-i", win(REPO / "dv/common"), "-i", win(BUILD)]
        for df in tgt["defs"]:
            prj += ["-d", df]
        r1 = subprocess.run(["cmd", "/c", str(VIV_BIN / "xvlog.bat")] + prj + [win(REPO / s) for s in tgt["src"]],
                            cwd=d, capture_output=True, text=True, env=env)
        r2 = subprocess.run(["cmd", "/c", str(VIV_BIN / "xelab.bat"), tgt["top"], "-s", "snap", "-timescale", "1ns/1ps"],
                            cwd=d, capture_output=True, text=True, env=env)
        (d / "opts.txt").write_text("".join(f"-testplusarg {a.lstrip('+')}\n" for a in plusargs(meta, cfg, VEC / case)),
                                    encoding="ascii")
        t0 = time.time()
        r3 = subprocess.run(["cmd", "/c", str(VIV_BIN / "xsim.bat"), "snap", "-R", "-f", "opts.txt"], cwd=d,
                            capture_output=True, text=True, env=env, timeout=7200)
        log = r1.stdout + r1.stderr + r2.stdout + r2.stderr + r3.stdout + r3.stderr
        (d / "run.log").write_text(log, encoding="utf-8")
        m = re.search(r"TEST_RESULT: (PASS|FAIL) checks=(\d+) errors=(\d+)", r3.stdout)
        errs = [l for l in r3.stdout.splitlines() if l.startswith("ERROR")]
        ok = bool(m and m.group(1) == "PASS" and not errs)
        perf = [l for l in r3.stdout.splitlines() if l.startswith("PERF:")]
        out.append({"case": case, "cfg": cfg, "ok": ok, "seconds": round(time.time() - t0, 1),
                    "perf": perf[0] if perf else "", "first_error": errs[0] if errs else
                    ("" if m else (r1.stderr + r2.stderr)[-300:])})
        print(f"  xsim {case}/{cfg}: {'PASS' if ok else 'FAIL'} ({out[-1]['seconds']} s) {out[-1]['first_error'][:120]}",
              flush=True)
    return out


def run_other_root(root: Path, ns) -> int:
    """cases of another vector root; nothing under build/e3 is written"""
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    write_model_paths(root / "model", root / "model_paths.vh")
    cases = [m for m in index["cases"] if not ns.only or any(fnmatch.fnmatchcase(m["name"], p) for p in ns.only)]
    vvps = {t: compile_target(t, root / "bin", inc=root) for t in sorted({target_of(m) for m in cases})}
    jobs = [(m, c) for m in cases for c in ns.configs]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=ns.jobs) as ex:
        res = list(ex.map(lambda mc: run_case(vvps[target_of(mc[0])], mc[0], mc[1], root / "logs", root), jobs))
    for r in res:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['case']:28s} {r['cfg']:5s} {r['seconds']:7.1f}s  "
              f"{r['perf'][6:] if r['perf'] else ''}  {r['first_error'][:100]}")
    n_ok = sum(r["ok"] for r in res)
    (root / "summary.json").write_text(json.dumps({"model_npz_sha256": index["model_npz_sha256"], "results": res,
                                                   "passed": n_ok, "total": len(res)}, indent=1), encoding="utf-8")
    print(f"SUMMARY: {n_ok}/{len(res)} passed ({round(time.time() - t0, 1)} s)")
    return 0 if n_ok == len(res) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regen", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--configs", nargs="*", default=list(CONFIGS))
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--xsim", action="store_true")
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--vec", default=None, help="other vector root (index.json, model/, cases), e.g. build/e4c/vec")
    ns = ap.parse_args()
    if ns.vec:
        return run_other_root(Path(ns.vec).resolve(), ns)
    if ns.regen or not (VEC / "index.json").exists():
        subprocess.run([sys.executable, str(REPO / "scripts/sim/gen_e3_vectors.py")], check=True)
    index = json.loads((VEC / "index.json").read_text(encoding="utf-8"))
    write_model_paths(VEC / "model", BUILD / "model_paths.vh")
    summary = {"model_npz_sha256": index["model_npz_sha256"], "settle": index["settle"]}
    if ns.mutants:
        summary["mutants"] = run_mutants(index)
        ok = all(r["detected"] for r in summary["mutants"])
        (BUILD / "mutants.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"MUTANTS: {sum(r['detected'] for r in summary['mutants'])}/{len(summary['mutants'])} detected")
        return 0 if ok else 1
    if ns.xsim:
        summary["xsim"] = run_xsim(index)
        (BUILD / "xsim.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"XSIM: {sum(r['ok'] for r in summary['xsim'])}/{len(summary['xsim'])} passed")
        return 0 if all(r["ok"] for r in summary["xsim"]) else 1
    cases = [m for m in index["cases"] if not ns.only or any(fnmatch.fnmatchcase(m["name"], p) for p in ns.only)]
    vvps = {t: compile_target(t, BUILD / "bin") for t in sorted({target_of(m) for m in cases})}
    jobs = [(m, c) for m in cases for c in ns.configs]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=ns.jobs) as ex:
        res = list(ex.map(lambda mc: run_case(vvps[target_of(mc[0])], mc[0], mc[1], BUILD / "logs"), jobs))
    for r in res:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['case']:28s} {r['cfg']:5s} {r['seconds']:7.1f}s  "
              f"{r['perf'][6:] if r['perf'] else ''}  {r['first_error'][:100]}")
    n_ok = sum(r["ok"] for r in res)
    summary.update({"results": res, "passed": n_ok, "total": len(res), "wall_seconds": round(time.time() - t0, 1)})
    (BUILD / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"SUMMARY: {n_ok}/{len(res)} passed ({summary['wall_seconds']} s)")
    return 0 if n_ok == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
