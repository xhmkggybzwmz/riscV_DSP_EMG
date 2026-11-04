#!/usr/bin/env python3
"""SPI live source regression (docs/system/stm32_spi_live.md), Icarus only, no data or model needed.

  python scripts/sim/run_spi.py                 # unit tests of esi_spi_src_axil and emg_spi_monitor_axil
  python scripts/sim/run_spi.py --mutants       # negative controls: every mutant must be detected
  python scripts/sim/run_spi.py --subsys        # SPI -> DSP -> CNN with build/e3/model_paths.vh (gen_spi_live.py)

Results: build/spi/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build" / "spi"
ICARUS = Path(os.environ.get("RISCV_EMG_ICARUS_BIN", r"C:\iverilog\bin"))
IVERILOG = str(ICARUS / "iverilog.exe") if (ICARUS / "iverilog.exe").exists() else shutil.which("iverilog")
VVP = str(ICARUS / "vvp.exe") if (ICARUS / "vvp.exe").exists() else shutil.which("vvp")

RX, SRC = "rtl/spi/emg1_spi_rx.v", "rtl/spi/esi_spi_src_axil.v"
SOURCES = [RX, SRC, "rtl/e3c/e3c_axil_if.v", "dv/common/tb_axil_master.v", "dv/esi/esi_monitor.v", "dv/spi/tb_esi_spi_src.v"]
INC = ["rtl/esi", "dv/common"]
SEEDS = (1, 2, 3, 7, 11)

# name, file, text to replace, replacement
MUTANTS = [
    ("crc_ignored", RX, "else if ({rx_byte, crc_low} != crc)", "else if (1'b0)"),
    ("magic_ignored", RX, 'if (rx_byte != "E")', "if (1'b0)"),
    ("channels_ignored", RX, "9'd6:  if (rx_byte != 8'd1)", "9'd6:  if (1'b0)"),
    ("abort_not_reported", RX, "if (active && !complete) fmt_err <= 1'b1;", ";"),
    ("too_long_not_reported", RX, "byte_count == 9'd278 && complete && !frame_bad", "1'b0"),
    ("bytes_swapped", RX, "sample_data  <= {rx_byte, sample_low};", "sample_data  <= {sample_low, rx_byte};"),
    ("no_overflow_check", RX, "edge_gap < 8'd4", "1'b0"),
    ("cs_not_filtered", RX, "if (cs_cnt == 3'd7) begin", "if (1'b1) begin"),
    ("gap_never", SRC, "have_store_exp && (rx_index != store_exp)", "1'b0"),
    ("gap_on_every_frame", SRC, "meta_gap[rblk] && (rpos == 7'd0) && !next_sos", "meta_gap[rblk] && !next_sos"),
    ("sos_never", SRC, "next_sos <= 1'b1; k <= 32'd0;", "next_sos <= 1'b0; k <= 32'd0;"),
    ("bad_crc_stored", SRC, "wire               push   = rx_good && accept && rate_ok;",
     "wire               push   = (rx_good | rx_crc) && accept && rate_ok;"),
    ("rate_ignored", SRC, "(expect_rate == 32'd0) || (rx_rate == expect_rate)", "1'b1"),
    ("ready_ignores_store", SRC, "assign spi_ready = aresetn && (blocks < READY_BELOW);", "assign spi_ready = aresetn;"),
    ("ready_too_late", SRC, "READY_BELOW = 3'd3", "READY_BELOW = 3'd4"),
    ("overwrite_when_full", SRC, "accept <= run && (blocks < N_BLOCKS);", "accept <= run;"),
    ("stored_without_run", SRC, "accept <= run && (blocks < N_BLOCKS);", "accept <= (blocks < N_BLOCKS);"),
    ("ovr_never", SRC, "wire               q_ovr  = sat_hi | sat_lo;", "wire               q_ovr  = 1'b0;"),
    ("no_saturation", SRC, "sat_hi ? 16'h7FFF : sat_lo ? 16'h8000 : wide[15:0]", "wide[15:0]"),
    ("tlast_early", SRC, "m_tlast  <= (rpos == 7'd127);", "m_tlast  <= (rpos == 7'd126);"),
    ("seq_without_position", SRC, "meta_index[rblk] + {25'd0, rpos}", "meta_index[rblk]"),
    ("wrong_lane", SRC, "m_tdata  <= {112'd0, q};", "m_tdata  <= {96'd0, q, 16'd0};"),
    ("src_id_zero", SRC, "9'd0, 1'b0, 2'b01, 1'b0,", "9'd0, 1'b0, 2'b00, 1'b0,"),
    ("sink_reset_no_flush", SRC, "if (rst || sink_rst) begin\n            accept", "if (rst) begin\n            accept"),
    ("valid_drops_with_run", SRC, "assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q;",
     "assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q & run;"),
    ("valid_in_sink_reset", SRC, "assign m_tvalid = valid_r & ~rst & ~sink_rst & ~sink_rst_q;", "assign m_tvalid = valid_r & ~rst;"),
    ("offset_not_writable", SRC, "10'h013: offset ", "10'h03F: offset "),
    ("soft_reset_keeps_counts", SRC, "if (rst) begin\n            last_index", "if (!aresetn) begin\n            last_index"),
    ("clear_ignored", SRC, "10'h001: if (wr_strb[0]) clear_stats <= wr_data[0];", "10'h001: ;"),
    ("timestamp_wrong", SRC, "k        <= next_sos ? 32'd1 : k + 32'd1;", "k        <= k + 32'd2;"),
    ("drop_not_counted", SRC, "if (rx_start && run && !sink_rst && blocks >= N_BLOCKS) drop_cnt <= drop_cnt + 32'd1;", ""),
]


def compile_tb(sources: list[str], vvp: Path) -> str:
    vvp.parent.mkdir(parents=True, exist_ok=True)
    cmd = [IVERILOG, "-g2012", "-Wall", "-o", str(vvp), "-s", "tb_esi_spi_src"]
    cmd += [f"-I{REPO / i}" for i in INC] + sources
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return r.stdout + r.stderr
    return ""


def run(vvp: Path, seed: int, log: Path, timeout: int = 1800) -> bool:
    try:
        r = subprocess.run([VVP, str(vvp), f"+seed={seed}"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.write_text("TIMEOUT\n", encoding="utf-8")
        return False
    log.write_text(r.stdout + r.stderr, encoding="utf-8")
    return r.returncode == 0 and "TEST_RESULT: PASS" in r.stdout


def unit() -> int:
    vvp = BUILD / "tb_esi_spi_src.vvp"
    err = compile_tb([str(REPO / s) for s in SOURCES], vvp)
    if err:
        print(err)
        print("COMPILE FAILED")
        return 1
    results = []
    with ThreadPoolExecutor(4) as ex:
        futs = {s: ex.submit(run, vvp, s, BUILD / f"src_seed{s}.log") for s in SEEDS}
    for s, f in futs.items():
        ok = f.result()
        results.append(ok)
        checks = [l for l in (BUILD / f"src_seed{s}.log").read_text(encoding="utf-8").splitlines() if "TEST_RESULT" in l or "INFO: random" in l]
        print(f"[{'PASS' if ok else 'FAIL'}] unit/src_seed{s}  {' | '.join(checks)}")
    mon = BUILD / "tb_mon.vvp"
    r = subprocess.run([IVERILOG, "-g2012", "-o", str(mon), str(REPO / "rtl/spi/emg_spi_monitor_axil.v"),
                        str(REPO / "dv/spi/tb_emg_spi_monitor.v")], capture_output=True, text=True)
    ok = r.returncode == 0 and "PASS" in subprocess.run([VVP, str(mon)], capture_output=True, text=True).stdout
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] unit/monitor")
    print(f"SUMMARY: {sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


def one_mutant(m) -> tuple[str, str]:
    name, file, old, new = m
    d = BUILD / "mutants" / name
    d.mkdir(parents=True, exist_ok=True)
    text = (REPO / file).read_text(encoding="utf-8").replace("\r\n", "\n")
    if old not in text:
        return name, "NOT APPLIED"
    mutated = d / Path(file).name
    mutated.write_text(text.replace(old, new), encoding="utf-8")
    srcs = [str(mutated) if s == file else str(REPO / s) for s in SOURCES]
    vvp = d / "tb.vvp"
    if compile_tb(srcs, vvp):
        return name, "COMPILE ERROR"
    return name, "survived" if run(vvp, 1, d / "sim.log", 900) else "detected"


def mutants() -> int:
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one_mutant, MUTANTS))
    for name, st in res:
        print(f"[{st}] {name}")
    det = sum(1 for _, s in res if s == "detected")
    print(f"MUTANTS: {det}/{len(res)} detected")
    return 0 if det == len(res) else 1


def subsys() -> int:
    vvp = BUILD / "tb_spi_subsys.vvp"
    BUILD.mkdir(parents=True, exist_ok=True)
    srcs = ["rtl/spi/emg_spi_subsys.v", SRC, RX, "rtl/e3c/e3c_axil_if.v", "rtl/e3c/emg_dsp_axil.v", "rtl/e3c/emg_cnn_axil.v",
            "rtl/dsp/emg_dsp.v", "rtl/infer/emg_cnn.v", "dv/common/tb_axil_master.v", "dv/esi/esi_monitor.v",
            "dv/spi/tb_spi_subsys.v"]
    cmd = [IVERILOG, "-g2012", "-o", str(vvp), "-s", "tb_spi_subsys"]
    cmd += [f"-I{REPO / i}" for i in INC + ["build/e3"]] + [str(REPO / s) for s in srcs]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr)
        return 1
    log = BUILD / "subsys.log"
    ok = run(vvp, 1, log, 3000)
    text = log.read_text(encoding="utf-8").splitlines()
    print(chr(10).join(l for l in text if l.startswith(("ERROR", "INFO", "TEST_RESULT")))[:3000])
    print(f"[{'PASS' if ok else 'FAIL'}] subsys")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--subsys", action="store_true")
    ns = ap.parse_args()
    if not IVERILOG or not VVP:
        sys.exit("Icarus Verilog not found: set RISCV_EMG_ICARUS_BIN")
    return subsys() if ns.subsys else mutants() if ns.mutants else unit()


if __name__ == "__main__":
    sys.exit(main())
