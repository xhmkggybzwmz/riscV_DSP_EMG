#!/usr/bin/env python3
"""E4c-B: reconcile the six-class KV260 run (mbx_mg6_rt) record by record.

    python scripts/fpga/kv260_e4c_live.py [--dir build/vivado/kv260_e4a/e4c_b] [--runs 4] [--browser-hash <hash>]

Golden: ps_mailbox.golden_mg6 table built from build/e4c/mg6_golden.json (E2 decisions of the six
selected real GRABMyo segments). Evidence, kept apart:
  RV32I   probe.json (power-on run, no consumer), reset1.json (controlled run): TOHOST, trap,
          RUN_CYCLES, reset releases; mailbox header counters
  PS      logs/server.jsonl: the power-on epoch (kept records) and the controlled epoch, every record
          against the table (RESULT_INDEX, scores, argmax, gesture, ts, seq, segment, flags)
  PC      recorder clients (gesture_server.record): every received result == the PS message;
          browser: hash of [seq, epoch, index, gesture_id, argmax, scores, window_end_seq, segment]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "host"), str(REPO / "scripts" / "fpga")]
from gesture_server import protocol as GP           # noqa: E402
from kv260_e4b_live import CORE, load, ms, page_hash  # noqa: E402
from ps_mailbox import golden_mg6 as GM              # noqa: E402

CORE_E4C = CORE + ("segment",)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(REPO / "build" / "vivado" / "kv260_e4a" / "e4c_b"))
    ap.add_argument("--decisions", default=str(REPO / "build" / "e4c" / "mg6_golden.json"))
    ap.add_argument("--runs", type=int, default=4)
    ap.add_argument("--browser-hash", default=None)
    ns = ap.parse_args()
    D = Path(ns.dir)
    tab = GM.table(GM.load(ns.decisions), ns.runs)
    N = len(tab)
    srv = load(D / "server.jsonl")
    out, fails = {"checks": []}, 0

    def check(name, ok, detail=""):
        nonlocal fails
        out["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}  {str(detail)[:220]}")

    check("server log: valid v1, source hardware_replay, server_seq 1..N",
          all(GP.validate_result(m) == [] and m["source"] == "hardware_replay" for m in srv)
          and [m["server_seq"] for m in srv] == list(range(1, len(srv) + 1)), f"{len(srv)} messages")
    epochs = sorted({m["reset_epoch"] for m in srv})
    by = {e: [m for m in srv if m["reset_epoch"] == e] for e in epochs}
    out["epochs"] = {e: len(v) for e, v in by.items()}
    run_ep = epochs[-1]
    # power-on epoch (no consumer while it ran): the kept records, a prefix of the table
    for e in epochs[:-1]:
        bad = [m["result_index"] for m in by[e] if GM.check(m, tab[m["result_index"] - 1])]
        check(f"PS epoch {e} (power-on run, no consumer): kept records == table rows 1..{len(by[e])}",
              [m["result_index"] for m in by[e]] == list(range(1, len(by[e]) + 1)) and not bad,
              f"{len(by[e])} records, mismatches {bad[:5]}")
    run = by[run_ep]
    bad = [(m["result_index"], GM.check(m, tab[m["result_index"] - 1])) for m in run
           if m["result_index"] > N or GM.check(m, tab[m["result_index"] - 1])]
    check(f"PS epoch {run_ep} (controlled run): {N} records, RESULT_INDEX 1..{N}, epoch_seq 0..{N - 1}",
          [m["result_index"] for m in run] == list(range(1, N + 1)) and [m["epoch_seq"] for m in run] == list(range(N)),
          f"{len(run)} records")
    check(f"PS epoch {run_ep}: every record == E2 golden table (scores, argmax, gesture, ts, seq, segment, flags)",
          not bad and len(run) == N, f"mismatches {bad[:3]}")
    check(f"PS epoch {run_ep}: six classes in order, 12 each; valid; mailbox_drop_cnt 0; no DROP_BEFORE",
          [m["gesture_id"] for m in run] == [r["class"] for r in tab] and
          [sum(m["gesture_id"] == c for m in run) for c in range(6)] == [N // 6] * 6 and
          all(m["valid"] and m["mailbox_drop_cnt"] == 0 and not m["drop_before"] for m in run),
          [sum(m["gesture_id"] == c for m in run) for c in range(6)])
    # timing (PL cycles and PS clock kept apart)
    det = [((run[i]["detect_cycle"] - run[i - 1]["detect_cycle"]) & 0xFFFFFFFF) for i in range(1, len(run))]
    within = [d for i, d in enumerate(det) if (i + 1) % 3 != 0]
    rr = [d for i, d in enumerate(det) if (i + 1) % 3 == 0 and (i + 1) % (3 * ns.runs) != 0]
    seg = [d for i, d in enumerate(det) if (i + 1) % (3 * ns.runs) == 0]
    ps = [(run[i]["ps_read_monotonic_ns"] - run[i - 1]["ps_read_monotonic_ns"]) / 1e6 for i in range(1, len(run))]
    out["timing"] = {"pl_detect_within_run_cycles": [min(within), max(within)],
                     "pl_detect_run_to_run_same_segment_cycles": [min(rr), max(rr)],
                     "pl_detect_segment_change_cycles": [min(seg), max(seg)],
                     "publish_minus_detect_cycles": [min((m["publish_cycle"] - m["detect_cycle"]) & 0xFFFFFFFF for m in run),
                                                     max((m["publish_cycle"] - m["detect_cycle"]) & 0xFFFFFFFF for m in run)],
                     "ps_read_ms_within_run": ms([p for i, p in enumerate(ps) if (i + 1) % 3 != 0]),
                     "ps_read_ms_run_boundary": ms([p for i, p in enumerate(ps) if (i + 1) % 3 == 0 and (i + 1) % (3 * ns.runs)]),
                     "ps_read_ms_segment_change": ms([p for i, p in enumerate(ps) if (i + 1) % (3 * ns.runs) == 0]),
                     "run_span_s": round((run[-1]["ps_read_monotonic_ns"] - run[0]["ps_read_monotonic_ns"]) / 1e9, 3)}
    check("PL: classification step within a run 5,000,000 +- 1000 cycles (50 ms @ 100 MHz)",
          all(abs(d - 5_000_000) <= 1000 for d in within), f"{min(within)}..{max(within)}")
    # RV32I
    pr = json.loads((D / "probe.json").read_text(encoding="utf-8"))
    a = {k: int(v, 16) for k, v in pr["regs"].items()}
    out["rv32i"] = {"poweron": {"RUN_CYCLES": a["RUN_CYCLES"], "header": pr["header"]}}
    check("RV32I power-on run (no consumer): TOHOST = 1 once, no trap; header DONE, published 16, "
          f"dropped {N - 16}, errors 0",
          a["FIRST_TOHOST"] == 1 and a["TOHOST_WRITES"] == 1 and (a["STATUS"] >> 1) & 1 == 0 and a["TRAP_CAUSE"] == 0
          and pr["header"]["state"] == 2 and pr["header"]["published_cnt"] == 16
          and pr["header"]["drop_cnt"] == N - 16 and pr["header"]["error_cnt"] == 0,
          f"RUN_CYCLES {a['RUN_CYCLES']} header {pr['header']}")
    r1 = json.loads((D / "reset1.json").read_text(encoding="utf-8"))
    b = {k: int(v, 16) for k, v in r1["after"].items()}
    out["rv32i"]["run"] = {"RUN_CYCLES": b["RUN_CYCLES"], "RESET_RELEASES": b["RESET_RELEASES"]}
    check("RV32I controlled run: TOHOST = 1 once, no trap, reset release 2",
          "error" not in r1 and b["FIRST_TOHOST"] == 1 and b["TOHOST_WRITES"] == 1 and (b["STATUS"] >> 1) & 1 == 0
          and b["TRAP_CAUSE"] == 0 and b["RESET_RELEASES"] == 2, f"RUN_CYCLES {b['RUN_CYCLES']}")
    hdr = json.loads((D / "status_after_run.json").read_text(encoding="utf-8"))["counters"]["source"]
    check(f"mailbox after the run: epoch {run_ep}, DONE, producer = consumer = {N}, drop 0, error 0",
          hdr["reset_epoch"] == run_ep and hdr["producer_state"] == "DONE" and hdr["producer_index"] == N
          and hdr["consumer_index"] == N and hdr["mailbox_drop_cnt"] == 0 and hdr["mailbox_error_cnt"] == 0
          and hdr["errors"] == 0 and hdr["gaps"] == 0 and hdr["dup"] == 0,
          {k: hdr[k] for k in ("reset_epoch", "producer_state", "producer_index", "consumer_index", "mailbox_drop_cnt",
                               "mailbox_error_cnt", "errors", "gaps", "stable_retries", "rereads")})
    # PC recorders
    srv_by = {m["server_seq"]: m for m in srv}
    want = [m["server_seq"] for m in run]
    out["clients"] = {}
    for f in sorted(D.glob("client*.jsonl")):
        msgs = load(f)
        live = [m for m in msgs if m["type"] == "result"]
        recv = {m["server_seq"]: m for m in live if m["reset_epoch"] == run_ep}
        same = all({k: m.get(k) for k in CORE_E4C} == {k: srv_by[s].get(k) for k in CORE_E4C} for s, m in recv.items())
        order = [m["server_seq"] for m in live]
        rx = [m["_rx_ns"] for m in live if m["reset_epoch"] == run_ep]
        out["clients"][f.stem] = {"received": len(recv), "missing": len(set(want) - set(recv)),
                                  "network_drop_cnt_max": max((m["network_drop_cnt"] for m in live), default=0),
                                  "rx_ms": ms([(rx[i] - rx[i - 1]) / 1e6 for i in range(1, len(rx))])}
        check(f"PC {f.stem}: {N} results of the run, each identical to the PS message, in order, no network drop",
              len(recv) == N and not set(want) - set(recv) and same and order == sorted(order)
              and out["clients"][f.stem]["network_drop_cnt_max"] == 0, out["clients"][f.stem])
    if ns.browser_hash:
        rows = [[m["server_seq"], m["reset_epoch"], m["result_index"], m["gesture_id"], m["argmax"], m["scores"],
                 m["window_end_seq"], m["segment"]] for m in run]
        check(f"browser: the {N} rows identical to the PS log (hash)", page_hash(rows) == ns.browser_hash,
              f"page {ns.browser_hash}, PS {page_hash(rows)}")
    (D / "analysis.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nE4c-B SUMMARY: {len(out['checks']) - fails}/{len(out['checks'])} checks")
    print(json.dumps({"epochs": out["epochs"], "timing": out["timing"], "rv32i": out["rv32i"]}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
