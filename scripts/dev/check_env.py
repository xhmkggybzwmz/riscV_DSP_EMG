#!/usr/bin/env python3
"""Read-only check of the development environment: what is ready, what is missing, what that blocks.

    python scripts/dev/check_env.py            # standard library only, changes nothing
    python scripts/dev/check_env.py --board    # also a read-only SSH check of the KV260
    python scripts/dev/check_env.py --json

Layers: 0 Python + Icarus; 1 data + E2 model + RISC-V GCC (all RTL regressions); 2 Vivado; 3 board.
Exit code 0 if layer 0 is ready. The other layers are reported, a machine can work without them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VIVADO_ROOT = Path(os.environ.get("RISCV_EMG_VIVADO_ROOT", r"E:\Xilinx2026\2026.1\Vivado"))
ICARUS_BIN = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin"))
GCC_BIN = VIVADO_ROOT.parent / "gnu" / "riscv" / "nt" / "bin"
E2_MODEL = REPO / "analysis" / "ml" / "e2" / "e2_20261022_023953" / "models" / "s1_test_s3_seed0_int8.npz"
E2_MODEL_SHA = "59ef6fb66253ecb0ee6df1337ddb4e0ee5d123e5e44726e4d292f20216ad9a54"
BOARD = os.environ.get("RISCV_EMG_KV260", "ubuntu@192.168.137.25")
PACKAGES = ("numpy", "scipy", "sklearn", "torch", "websockets")

rows: list[dict] = []


def row(layer: int, name: str, ok: bool, detail: str = "", todo: str = "") -> None:
    rows.append({"layer": layer, "name": name, "ok": bool(ok), "detail": detail, "todo": "" if ok else todo})


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def git(*a: str) -> str:
    return run(["git", "-C", str(REPO), *a])[1]


def check_python() -> None:
    v = sys.version_info
    row(0, "Python >= 3.12", v >= (3, 12), f"{v.major}.{v.minor}.{v.micro}", "install Python 3.12 (use python, not the Store python3 stub)")
    for p in PACKAGES:
        row(0, f"package {p}", importlib.util.find_spec(p) is not None, "", "pip install -r requirements.txt")


def check_tools() -> None:
    iv = ICARUS_BIN / "iverilog.exe"
    rc, out = run([str(iv), "-V"]) if iv.is_file() else (1, "")
    row(0, "Icarus Verilog", rc == 0, out.splitlines()[0] if out else str(iv), "install Icarus 12, or set RISCV_EMG_ICARUS_BIN")
    gcc = GCC_BIN / "riscv64-unknown-elf-gcc.exe"
    row(1, "RISC-V GCC (with Vivado)", gcc.is_file(), str(gcc), "comes with Vivado 2026.1; set RISCV_EMG_VIVADO_ROOT, or RISCV_EMG_GCC_BIN")
    node = shutil.which("node")
    row(0, "Node.js", bool(node), node or "", "only needed for visualization/web/test")
    viv = VIVADO_ROOT / "bin" / "vivado.bat"
    row(2, "Vivado 2026.1", viv.is_file() and "2026.1" in str(VIVADO_ROOT), str(viv),
        "install Vivado 2026.1 (by the user), or set RISCV_EMG_VIVADO_ROOT to ...\\2026.1\\Vivado")


def check_data() -> None:
    raw = REPO / "data" / "public" / "grabmyo" / "raw"
    n = len(list((raw / "Session3").glob("session3_participant*"))) if (raw / "Session3").is_dir() else 0
    row(1, "GRABMyo G1 (10 participants)", n >= 10, f"{n} participants in Session3",
        "python ml/scripts/fetch_grabmyo.py plan --subset G1, then download (800 MB, ask first), or copy data/public/grabmyo/")
    if E2_MODEL.is_file():
        sha = hashlib.sha256(E2_MODEL.read_bytes()).hexdigest()
        row(1, "E2 reference model", sha == E2_MODEL_SHA, sha[:16],
            "the model differs from the one behind the golden vectors and board reports: tell the user")
    else:
        row(1, "E2 reference model", False, "missing", f"copy {E2_MODEL.parent.parent.relative_to(REPO)} from the old machine, or retrain (see the README)")
    for rel, todo in (("build/e4c/fw/mg6_data.h", "python ml/scripts/e4c_multigesture_eval.py; python scripts/sim/gen_e4c.py"),
                      ("build/e4c/vec/chain_mg6_p4/rd_expect.hex", "python scripts/sim/gen_e4c.py")):
        row(1, rel, (REPO / rel).is_file(), "", todo)
    row(0, "tracked E2 vectors", (REPO / "reference" / "emg_fixed" / "vectors" / "ref_s1_test_s3_seed0" / "manifest.json").is_file(),
        "", "the checkout is incomplete")


def check_git() -> None:
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head = git("log", "-1", "--format=%h %s")[:70]
    dirty = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    row(0, "branch", True, f"{branch}: {head}", "")
    print(f"working tree: {'clean' if not dirty else str(len(dirty)) + ' changed / untracked files'}")


def check_board() -> None:
    rc, out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", BOARD, "hostname; uptime"], 15)
    row(3, f"KV260 SSH ({BOARD})", rc == 0, out.splitlines()[0] if rc == 0 and out else out[:80], "board off, or no SSH key; set RISCV_EMG_KV260")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", action="store_true")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()
    check_git()
    check_python()
    check_tools()
    check_data()
    if ns.board:
        check_board()
    if ns.json:
        print(json.dumps(rows, indent=1))
    else:
        names = {0: "Layer 0: Python, Icarus (Python tests, source unit test)", 1: "Layer 1: data, model, GCC (all RTL regressions)", 2: "Layer 2: Vivado (xsim, bitstreams)", 3: "Layer 3: board"}
        for layer in sorted({r["layer"] for r in rows}):
            print(f"\n{names[layer]}")
            for r in (x for x in rows if x["layer"] == layer):
                print(f"  [{'ok' if r['ok'] else '--'}] {r['name']}  {r['detail']}")
                if r["todo"]:
                    print(f"       -> {r['todo']}")
    blocking = [r for r in rows if r["layer"] == 0 and not r["ok"] and r["name"] not in ("Node.js", "branch")]
    print(f"\n{'layer 0 ready' if not blocking else 'layer 0 not ready: ' + ', '.join(r['name'] for r in blocking)}")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
