#!/usr/bin/env python3
"""E4b-B: reconcile the live KV260 -> PS -> WebSocket -> PC run.

    python scripts/fpga/kv260_e4b_live.py [--dir build/vivado/kv260_e4a/e4b_b] [--browser-hash 2=... 3=...]

Three independent pieces of evidence per run (epoch):
  RV32I   debug registers after each controlled soft reset (reset<tag>.json): TOHOST, trap, cycles
  PS      the gesture server's JSON Lines (logs/server.jsonl): every result against the E2 golden
          decisions (build/vivado/kv260_e4a/e4b/golden.json), indices, flags, mailbox counters
  PC      the recorder clients (gesture_server.record) and the browser: every received result equal
          to the PS-side message, order, losses; browser rows compared by hash (same canonical text
          as the page: [seq, epoch, index, gesture_id, argmax, scores, window_end_seq], compact JSON)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "host"))
from gesture_server import protocol as GP          # noqa: E402
from ps_mailbox import golden as G                  # noqa: E402

CORE = ("schema_version", "type", "source", "server_seq", "result_index", "reset_epoch", "epoch_seq", "window_end_seq",
        "esi_timestamp", "gesture_id", "gesture", "argmax", "scores", "flags", "drop_before", "cnn_lat_cycles",
        "detect_cycle", "publish_cycle", "ps_read_monotonic_ns", "mailbox_drop_cnt", "valid")


def load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def page_hash(rows) -> str:
    """FNV-1a 32 + djb2 32 of the compact JSON text (visualization page: window.__hashes)"""
    txt = json.dumps(rows, separators=(",", ":"))
    f, d = 0x811C9DC5, 5381
    for ch in txt:
        c = ord(ch)
        f = ((f ^ c) * 0x01000193) & 0xFFFFFFFF
        d = (d * 33 + c) & 0xFFFFFFFF
    return f"{f:08x}{d:08x}"


def ms(v):
    v = sorted(v)
    return {"n": len(v), "mean": round(statistics.mean(v), 3), "min": round(v[0], 3), "max": round(v[-1], 3)} if v else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(REPO / "build" / "vivado" / "kv260_e4a" / "e4b_b"))
    ap.add_argument("--browser-hash", nargs="*", default=[], help="epoch=hash from the page")
    ns = ap.parse_args()
    D = Path(ns.dir)
    g = G.load_golden(REPO / "build" / "vivado" / "kv260_e4a" / "e4b" / "golden.json")
    srv = load(D / "server.jsonl")
    out, fails = {"checks": []}, 0

    def check(name, ok, detail=""):
        nonlocal fails
        out["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}  {str(detail)[:200]}")

    # ------------------------------------------------ PS side, per epoch
    check("server log: every message valid v1, source hardware_replay, server_seq 1..N",
          all(GP.validate_result(m) == [] and m["source"] == "hardware_replay" for m in srv)
          and [m["server_seq"] for m in srv] == list(range(1, len(srv) + 1)), f"{len(srv)} messages")
    epochs = sorted({m["reset_epoch"] for m in srv})
    by = {e: [m for m in srv if m["reset_epoch"] == e] for e in epochs}
    out["epochs"] = {e: len(v) for e, v in by.items()}
    for e, ms_ in by.items():
        gold_bad = []
        for m in ms_:
            rec = {"result_index": m["result_index"], "scores": m["scores"], "argmax": m["argmax"],
                   "gesture": m["gesture_id"], "esi_timestamp": m["esi_timestamp"], "window_end_seq": m["window_end_seq"]}
            exp = G.expected(g, m["result_index"] - 1)
            bad = [k for k in ("scores", "argmax", "gesture", "esi_timestamp", "window_end_seq", "result_index")
                   if rec[k] != exp[k]]
            if bad:
                gold_bad.append((m["result_index"], bad))
        n = len(ms_)
        idx = [m["result_index"] for m in ms_]
        seqs = [m["epoch_seq"] for m in ms_]
        label = "power-on run (no consumer: 16 kept, 72 dropped)" if e == 1 else f"run epoch {e}"
        check(f"PS epoch {e} [{label}]: RESULT_INDEX 1..{n}, epoch_seq 0..{n - 1}, golden scores/argmax/gesture/ts/seq",
              idx == list(range(1, n + 1)) and seqs == list(range(n)) and not gold_bad, f"{n} records, golden mismatches {gold_bad[:3]}")
        check(f"PS epoch {e}: flags POSTPROC|ARGMAX_OK|GOLDEN_OK, no DROP_BEFORE, valid, CNN latency 11152",
              all(m["flags"] & 0x19 == 0x19 and not m["drop_before"] and m["valid"] and m["cnn_lat_cycles"] == 11152
                  for m in ms_) and (n < 88 or ms_[-1]["flags"] & 0x80),
              sorted({hex(m['flags']) for m in ms_}))
        if e > 1:
            check(f"PS epoch {e}: 88 records, mailbox_drop_cnt 0 in every message", n == 88 and all(m["mailbox_drop_cnt"] == 0 for m in ms_))
    # timing per run (PL domain and PS domain kept apart)
    out["timing"] = {}
    for e in epochs[1:]:
        ms_ = by[e]
        det = [((ms_[i]["detect_cycle"] - ms_[i - 1]["detect_cycle"]) & 0xFFFFFFFF) for i in range(1, len(ms_))]
        inside = [d for i, d in enumerate(det) if (i + 1) % 11 != 0]
        bound = [d for i, d in enumerate(det) if (i + 1) % 11 == 0]
        ps = [(ms_[i]["ps_read_monotonic_ns"] - ms_[i - 1]["ps_read_monotonic_ns"]) / 1e6 for i in range(1, len(ms_))]
        out["timing"][e] = {
            "pl_detect_step_cycles_in_pass": [min(inside), max(inside)], "pl_detect_step_cycles_pass_boundary": [min(bound), max(bound)],
            "publish_minus_detect_cycles": [min((m["publish_cycle"] - m["detect_cycle"]) & 0xFFFFFFFF for m in ms_),
                                            max((m["publish_cycle"] - m["detect_cycle"]) & 0xFFFFFFFF for m in ms_)],
            "ps_read_interval_ms_in_pass": ms([p for i, p in enumerate(ps) if (i + 1) % 11 != 0]),
            "ps_read_interval_ms_pass_boundary": ms([p for i, p in enumerate(ps) if (i + 1) % 11 == 0]),
            "run_span_s": round((ms_[-1]["ps_read_monotonic_ns"] - ms_[0]["ps_read_monotonic_ns"]) / 1e9, 3)}
        check(f"PL epoch {e}: classification step 5,000,000 +- 1000 cycles in a replay pass (50 ms @ 100 MHz)",
              all(abs(d - 5_000_000) <= 1000 for d in inside), f"{min(inside)}..{max(inside)}, boundary {min(bound)}..{max(bound)}")

    # ------------------------------------------------ RV32I
    for tag, e in (("1", 2), ("2", 3), ("3_visual", 4)):
        r = json.loads((D / f"reset{tag}.json").read_text(encoding="utf-8"))
        a = {k: int(v, 16) for k, v in r["after"].items()}
        check(f"RV32I run {tag} (epoch {e}): TOHOST = 1 once, no trap, reset release {e}",
              "error" not in r and a["FIRST_TOHOST"] == 1 and a["TOHOST_WRITES"] == 1 and (a["STATUS"] >> 1) & 1 == 0
              and a["TRAP_CAUSE"] == 0 and a["RESET_RELEASES"] == e, f"RUN_CYCLES {a['RUN_CYCLES']}")
        out.setdefault("run_cycles", {})[e] = a["RUN_CYCLES"]
    pr = json.loads((D / "probe.json").read_text(encoding="utf-8"))
    a = {k: int(v, 16) for k, v in pr["regs"].items()}
    out["run_cycles"][1] = a["RUN_CYCLES"]
    check("RV32I power-on run (epoch 1, no consumer): TOHOST = 1 once, no trap; header 16 published / 72 dropped / 0 errors",
          a["FIRST_TOHOST"] == 1 and a["TOHOST_WRITES"] == 1 and (a["STATUS"] >> 1) & 1 == 0 and
          pr["header"]["published_cnt"] == 16 and pr["header"]["drop_cnt"] == 72 and pr["header"]["error_cnt"] == 0,
          f"RUN_CYCLES {a['RUN_CYCLES']}")

    # ------------------------------------------------ PC recorders
    srv_by_seq = {m["server_seq"]: m for m in srv}
    out["clients"] = {}
    for name, ep in (("run1_clientA", 2), ("run1_clientB", 2), ("run2_clientA", 3), ("run2_clientB", 3),
                     ("run2_clientC", 3), ("run2_clientD", 3)):
        msgs = load(D / f"{name}.jsonl")
        live = [m for m in msgs if m["type"] == "result"]
        hist = [h for m in msgs if m["type"] == "hello" and m["_conn"] > 1 for h in m["history"]]
        recv = {m["server_seq"]: m for m in live if m["reset_epoch"] == ep}
        restored = {h["server_seq"]: h for h in hist if h["reset_epoch"] == ep and h["server_seq"] not in recv}
        want = [m["server_seq"] for m in by[ep]]
        same = all({k: m[k] for k in CORE} == {k: srv_by_seq[s][k] for k in CORE} for s, m in {**recv, **restored}.items())
        order = [m["server_seq"] for m in live]
        rx = [m["_rx_ns"] for m in live if m["reset_epoch"] == ep]
        rxi = [(rx[i] - rx[i - 1]) / 1e6 for i in range(1, len(rx))]
        missing = sorted(set(want) - set(recv) - set(restored))
        conns = max(m["_conn"] for m in msgs)
        out["clients"][name] = {"epoch": ep, "live": len(recv), "restored_from_history": len(restored), "missing": len(missing),
                                "connections": conns, "network_drop_cnt_max": max((m["network_drop_cnt"] for m in live), default=0),
                                "rx_interval_ms": ms(rxi)}
        check(f"PC {name} (epoch {ep}): every received result identical to the PS message, order increasing",
              same and order == sorted(order) and len(set(order)) == len(order),
              f"live {len(recv)} + history {len(restored)}, missing {len(missing)}, connections {conns}")
        rows = [[s, ep, m["result_index"], m["gesture_id"], m["argmax"], m["scores"], m["window_end_seq"]]
                for s, m in sorted({**recv, **restored}.items())]
        out["clients"][name]["page_hash"] = page_hash(rows) if not missing else None
    c1 = out["clients"]
    check("multi-client run 1: A and B got all 88, no reconnect, no network drop",
          c1["run1_clientA"]["live"] == 88 == c1["run1_clientB"]["live"] and c1["run1_clientA"]["network_drop_cnt_max"] == 0
          and c1["run1_clientB"]["network_drop_cnt_max"] == 0)
    check("run 2: continuous clients A / B got all 88 while C / D / the browser reconnected",
          c1["run2_clientA"]["live"] == 88 == c1["run2_clientB"]["live"])
    check("run 2: client C (0.5 s away) complete with the reconnect history", c1["run2_clientC"]["missing"] == 0,
          str(c1["run2_clientC"]))
    check("run 2: client D (2.5 s away, deeper than the 20-result history): unrecoverable gap reported, rest identical",
          c1["run2_clientD"]["missing"] > 0, str(c1["run2_clientD"]))
    # browser (page hashes)
    for kv in ns.browser_hash:
        e, h = kv.split("=")
        e = int(e)
        rows = [[m["server_seq"], e, m["result_index"], m["gesture_id"], m["argmax"], m["scores"], m["window_end_seq"]] for m in by[e]]
        check(f"browser epoch {e}: 88 rows identical to the PS log (hash of the canonical rows)", page_hash(rows) == h,
              f"page {h}, PS {page_hash(rows)}")
    (D / "analysis.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nE4b-B SUMMARY: {len(out['checks']) - fails}/{len(out['checks'])} checks")
    print(json.dumps({"epochs": out["epochs"], "run_cycles": out["run_cycles"], "timing": out["timing"]}, indent=1))
    print(json.dumps({k: {x: v[x] for x in ("live", "restored_from_history", "missing", "connections", "rx_interval_ms")}
                      for k, v in out["clients"].items()}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
