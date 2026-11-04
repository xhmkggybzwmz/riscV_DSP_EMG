#!/usr/bin/env python3
"""E4a-B: KV260 board test of the shared BRAM mailbox through PYNQ Overlay(download=False) +
pynq.MMIO + MailboxReader (host/ps_mailbox).

    python scripts/fpga/kv260_e4b.py artifacts          PC: bit / bit.bin / hwh / manifest of one build,
                                                         HWH mailbox range, golden JSON
    python scripts/fpga/kv260_e4b.py stage              copy loader, bitstreams, overlays, reader -> board
    python scripts/fpga/kv260_e4b.py session --dry-run  print the board commands
    python scripts/fpga/kv260_e4b.py session --pre-only read-only board state, nothing changed
    python scripts/fpga/kv260_e4b.py session            the board test (stops at the first failed step)
    python scripts/fpga/kv260_e4b.py analyze            checks on the fetched results

The PL is configured only by the stage 6A / E3c-B1 FPGA-manager loader
(scripts/fpga/kv260_ps_board.py, configfs overlay rv32_6a, flags 0 while loading, restored
afterwards); PYNQ never downloads. No JTAG. Board directory ~/riscv-emg-soc/e4a_b. Session
order: state -> xmutil unloadapp -> load mbx_rt2k -> probe -> rt2k (power-on + 3 soft resets)
-> no /dev/mem user left -> unload -> load mbx_rt2k_long -> probe -> long (runs A, B, C) ->
no /dev/mem user left -> unload -> xmutil loadapp k26-starter-kits -> state compared with the
start. A failed step: readers stopped, overlay removed, app reloaded, state compared, stop.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "host"))
sys.path.insert(0, str(REPO / "scripts" / "fpga"))
import kv260_6a as K                                                    # noqa: E402
from ps_mailbox import golden as G                                       # noqa: E402
from ps_mailbox.overlay_device import bit2bin, bit_header, hwh_ranges, sha256_file   # noqa: E402

PROGS = ["mbx_rt2k", "mbx_rt2k_long"]
OUT = REPO / "build" / "vivado" / "kv260_e4a"
WORK = OUT / "e4b"
BDIR = "riscv-emg-soc/e4a_b"
# the PYNQ venv environment (/etc/profile.d/pynq_venv.sh without its login-time dtbo insert): the venv
# bin first, so that pynq finds its own xclbinutil (/usr/bin/xclbinutil is a wrapper that exits 0 on error)
PY = "sudo env XILINX_XRT=/usr PATH=/usr/local/share/pynq-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/local/share/pynq-venv/bin/python3"
LOADER = f"cd {BDIR} && sudo python3 kv260_ps_board.py"
CTRL = f"cd {BDIR} && {PY} -m ps_mailbox.board_e4b"
PYNQ_STATE = "/usr/local/share/pynq-venv/lib/python3.10/site-packages/pynq/pl_server"
EXTRA = ("cat /proc/sys/kernel/random/boot_id; uname -r; uptime; "
         f"sha256sum {PYNQ_STATE}/global_pl_state.json {PYNQ_STATE}/_current_metadata.pkl; "
         "ls -l --time-style=+%s /lib/firmware | sha256sum; "
         "sudo grep -i -E 'a00|a01' /proc/iomem || true; "
         "echo DMESG_LINES $(sudo dmesg | wc -l); "
         "sudo sh -c 'for p in /proc/[0-9]*; do grep -qs \" /dev/mem$\" $p/maps && echo DEVMEM $(basename $p) "
         "$(cat $p/comm); done; true'; "
         "pgrep -a -f 'ps_mailbox|jupyter' || true")
BAD_KERNEL = re.compile(r"Oops|BUG:|Internal error|SError|soft lockup|hard LOCKUP|Unable to handle|Call trace|"
                        r"rcu_sched self-detected|Kernel panic", re.I)


def ssh(cmd: str, timeout: int = 600) -> tuple[int, str]:
    return K.ssh(cmd, timeout)


# ---------------------------------------------------------------- artifacts (PC)
def cmd_artifacts(ns) -> int:
    K.set_variant("ps_e4a")
    WORK.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    res, fails = {}, 0
    hwh_body, hwh_hpm0 = set(), set()
    for prog in PROGS:
        o = OUT / prog / "out"
        man = json.loads((o / "manifest.json").read_text(encoding="utf-8"))
        bit = (o / "kv260_soc.bit").read_bytes()
        hdr = bit_header(bit)[0]
        hwh = o / "kv260_soc.hwh"
        rngs = {r["instance"]: r for r in hwh_ranges(hwh)}
        ts = re.search(r'TIMESTAMP="([^"]+)"', hwh.read_text(encoding="utf-8", errors="replace")).group(1)
        t_hwh = time.mktime(time.strptime(ts, "%a %b %d %H:%M:%S %Y"))
        t_bit = time.mktime(time.strptime(hdr["c"] + " " + hdr["d"], "%Y/%m/%d %H:%M:%S"))
        mb = [r for r in rngs.values() if r["modtype"] == "axi_bram_ctrl"]
        db = [r for r in rngs.values() if r["modtype"] == "kv260_dbg_axil"]
        checks = [
            ("manifest: every check passed", all(c["ok"] for c in man["checks"]), f"{len(man['checks'])} checks"),
            ("manifest: clean work tree at the current commit", man["git"]["dirty"] is False and man["git"]["commit"] == head,
             f"{man['git']['commit']} dirty={man['git']['dirty']}"),
            ("bit sha256 == manifest", sha256_file(o / "kv260_soc.bit") == man["bitstream"]["bit_sha256"], ""),
            ("bit.bin == bit2bin(bit) (same configuration data) == manifest",
             bit2bin(bit) == (o / "kv260_soc.bit.bin").read_bytes()
             and sha256_file(o / "kv260_soc.bit.bin") == man["bitstream"]["bit_bin_sha256"], man["bitstream"]["bit_bin_sha256"]),
            ("hwh sha256 == manifest", sha256_file(hwh) == man["mailbox"]["hwh_sha256"], man["mailbox"]["hwh_sha256"]),
            ("HWH: one axi_bram_ctrl on HPM0 at 0xA0100000 + 4 KB (MEMORY)",
             len(mb) == 1 and (mb[0]["base"], mb[0]["size"], mb[0]["memtype"]) == (0xA0100000, 0x1000, "MEMORY"),
             str([(r["instance"], hex(r["base"]), hex(r["size"])) for r in mb])),
            ("HWH: debug window kv260_dbg_axil at 0xA0000000 + 256 KB",
             len(db) == 1 and (db[0]["base"], db[0]["size"]) == (0xA0000000, 0x40000),
             str([(r["instance"], hex(r["base"]), hex(r["size"])) for r in db])),
            ("HWH written before the bitstream in the same build (< 2 h)", 0 <= t_bit - t_hwh < 7200,
             f"hwh {ts}, bit {hdr['c']} {hdr['d']}"),
            ("bit header: kv260_bd_wrapper, xck26-sfvc784-2LV-c, Vivado 2026.1",
             hdr["a"].startswith("kv260_bd_wrapper;") and "Version=2026.1" in hdr["a"] and hdr["b"] == "xck26-sfvc784-2LV-c",
             hdr["a"].split(";SW_")[0]),
        ]
        lines = hwh.read_text(encoding="utf-8").splitlines()
        init = [re.search(r'VALUE="([^"]*)"', l).group(1) for l in lines if 'NAME="INIT_FILE"' in l]
        checks.append(("HWH: CPU BRAM INIT_FILE is this program's firmware image",
                       init == [(K.SW / prog / f"{prog}.hex").as_posix()], str(init)))
        hwh_body.add(tuple(sorted(l for l in lines if "TIMESTAMP=" not in l and 'NAME="INIT_FILE"' not in l)))
        hwh_hpm0.add(tuple((r["instance"], r["base"], r["size"]) for r in sorted(rngs.values(), key=lambda r: r["base"])))
        res[prog] = {"checks": [{"name": n, "ok": bool(g), "detail": d} for n, g, d in checks],
                     "hwh_init_file": init,
                     "bit_sha256": man["bitstream"]["bit_sha256"], "bit_bin_sha256": man["bitstream"]["bit_bin_sha256"],
                     "hwh_sha256": man["mailbox"]["hwh_sha256"], "hex_sha256": man["firmware"]["hex_sha256"],
                     "commit": man["git"]["commit"], "bit_header": hdr, "hwh_timestamp": ts,
                     "xsim_cycles": man["board_test"]["xsim_cycles"]}
        nf = sum(not c["ok"] for c in res[prog]["checks"])
        fails += nf
        print(f"== {prog}: {len(checks) - nf}/{len(checks)}")
        for c in res[prog]["checks"]:
            print(f"   {'ok  ' if c['ok'] else 'FAIL'} {c['name']}  {c['detail']}")
    ok = len(hwh_body) == 1 and len(hwh_hpm0) == 1
    fails += not ok
    res["same_hwh_except_timestamp_init_file"] = ok
    print(f"   {'ok  ' if ok else 'FAIL'} both builds: same HWH lines apart from TIMESTAMP / INIT_FILE "
          f"(element order may differ), same HPM0 map {sorted(hwh_hpm0)}")
    g = G.from_vectors(REPO / "build" / "e3" / "vec" / "chain_s1_normal")
    (WORK / "golden.json").write_text(json.dumps(g, indent=1) + "\n", encoding="utf-8", newline="\n")
    res["golden"] = {"file": str(WORK / "golden.json"), "results": len(g["results"]), "sha256": sha256_file(WORK / "golden.json")}
    (WORK / "artifacts.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"golden: {len(g['results'])} results -> {WORK / 'golden.json'}")
    return 1 if fails else 0


# ---------------------------------------------------------------- stage
def cmd_stage(ns) -> int:
    dtbo = WORK / "rv32_6a.dtbo"
    subprocess.run([str(K.DTC), "-@", "-I", "dts", "-O", "dtb", "-o", str(dtbo),
                    str(REPO / "fpga" / "kv260" / "dts" / "rv32_6a.dts")], check=True, capture_output=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        def add(src: Path, dst: str, lf: bool = False):
            data = src.read_bytes()
            if lf:
                data = data.replace(b"\r\n", b"\n")
            ti = tarfile.TarInfo(dst)
            ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
            tf.addfile(ti, io.BytesIO(data))
        add(REPO / "scripts" / "fpga" / "kv260_ps_board.py", "kv260_ps_board.py", lf=True)
        add(dtbo, "fw/rv32_6a.dtbo")
        add(WORK / "golden.json", "golden.json")
        for prog in PROGS:
            o = OUT / prog / "out"
            add(o / "kv260_soc.bit.bin", f"fw/{prog}.bit.bin")
            add(o / "kv260_soc.bit", f"overlays/{prog}/{prog}.bit")
            add(o / "kv260_soc.hwh", f"overlays/{prog}/{prog}.hwh")
            add(o / "manifest.json", f"overlays/{prog}/{prog}.manifest.json")
        pkg = REPO / "host" / "ps_mailbox"
        for f in sorted(pkg.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                add(f, "ps_mailbox/" + f.relative_to(pkg).as_posix())   # bytes as in the manifest hashes
    rc, o = ssh(f"mkdir -p {BDIR} && ls -A {BDIR} | head -3")
    if o.strip():
        print(f"{BDIR} not empty on the board: {o!r} (not overwriting)")
        return 1
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", K.BOARD, f"tar -xf - -C {BDIR} && find {BDIR} -type f | wc -l"],
                       input=buf.getvalue(), capture_output=True)
    print(r.stdout.decode().strip(), r.stderr.decode().strip())
    rc, o = ssh(f"cd {BDIR} && sha256sum fw/*.bit.bin overlays/*/*.bit overlays/*/*.hwh golden.json kv260_ps_board.py")
    print(o)
    (WORK / "stage.txt").write_text(o + "\n", encoding="utf-8")
    return r.returncode


# ---------------------------------------------------------------- session
def state_of(text: str) -> dict:
    st = {}
    for line in text.splitlines():
        tok = line.split(None, 2)
        if len(tok) >= 2 and tok[0] == "STATE":
            st[tok[1] if tok[1] != "holder" else f"holder {tok[2].split()[0]}"] = tok[2] if len(tok) > 2 else ""
    return st


def extra_items(text: str) -> dict:
    """items of EXTRA that must be the same before and after the session"""
    lines = text.splitlines()
    d = {"boot_id": lines[0].strip() if lines else None, "kernel": lines[1].strip() if len(lines) > 1 else None}
    for l in lines:
        if l.endswith(("global_pl_state.json", "_current_metadata.pkl")):
            d["pynq " + l.rsplit("/", 1)[1]] = l.split()[0]
        elif l.endswith("  -") and len(l.split()[0]) == 64:
            d["/lib/firmware listing sha256"] = l.split()[0]
        elif l.startswith("DEVMEM"):
            d.setdefault("/dev/mem users", []).append(" ".join(l.split()[2:]))
    return d


def steps_for() -> list[tuple[str, str]]:
    s = [("state", f"{LOADER} check"), ("extra", EXTRA), ("unloadapp", "sudo xmutil unloadapp")]
    for prog, cmd, extra in (("mbx_rt2k", "rt2k", "--resets 3 --records 11"),
                             ("mbx_rt2k_long", "long", "--records 88 --split 30")):
        s += [(f"load {prog}", f"{LOADER} load {prog}.bit.bin"),
              (f"probe {prog}", f"{CTRL} probe --prog {prog}"),
              (f"{cmd} {prog}", f"{CTRL} {cmd} --prog {prog} {extra}"),
              (f"no reader left {prog}", "sudo sh -c 'for p in /proc/[0-9]*; do grep -qs \" /dev/mem$\" $p/maps && "
                                          "echo DEVMEM $(basename $p) $(cat $p/comm); done; true'"),
              (f"unload {prog}", f"{LOADER} unload")]
    s += [("loadapp", "sudo xmutil loadapp k26-starter-kits"), ("state after", f"{LOADER} check"), ("extra after", EXTRA)]
    return s


def new_kernel_errors(n0: int) -> list[str]:
    rc, o = ssh(f"sudo dmesg | tail -n +{n0 + 1}")
    return [l for l in o.splitlines() if BAD_KERNEL.search(l)]


def cmd_session(ns) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    steps = steps_for()
    if ns.pre_only:
        steps = steps[:2]
    if ns.dry_run:
        for name, c in steps:
            print(f"[{name}] {c}")
        return 0
    log = []
    before = None
    devmem0 = None
    dmesg0 = None
    extra0 = None

    def record(name, c, rc, o, t):
        log.append({"step": name, "cmd": c, "rc": rc, "s": round(t, 3), "out": o})
        (WORK / "session.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
        print(f"[{name}] rc={rc} {t:.1f}s\n    " + o[-1500:].replace("\n", "\n    "), flush=True)

    def restore(reason: str) -> int:
        print(f"STOP: {reason}; stopping readers, removing the overlay, reloading the app")
        for name, c in (("stop readers", "sudo pkill -TERM -f 'ps_mailbox.(cli|board_e4b)' ; sleep 1; "
                                         "pgrep -a -f ps_mailbox || true"),
                        ("unload (restore)", f"{LOADER} unload"),
                        ("loadapp (restore)", "sudo xmutil loadapp k26-starter-kits"),
                        ("state after (restore)", f"{LOADER} check"), ("extra after (restore)", EXTRA)):
            t0 = time.monotonic()
            rc, o = ssh(c)
            record(name, c, rc, o, time.monotonic() - t0)
        diff = K.compare_state(before or {}, state_of(log[-2]["out"]))
        diff += compare_extra(extra0, log[-1]["out"])
        for d in diff:
            print(f"UNRESTORED {d}")
        return 1

    def compare_extra(e0, text):
        a, b = extra_items(e0 or ""), extra_items(text)
        return [f"{k}: before '{a.get(k)}' now '{b.get(k)}'" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]

    for name, c in steps:
        t0 = time.monotonic()
        rc, o = ssh(c, timeout=900)
        record(name, c, rc, o, time.monotonic() - t0)
        if name == "state":
            before = state_of(o)
        if name == "extra":
            extra0 = o
            devmem0 = sorted(l for l in o.splitlines() if l.startswith("DEVMEM"))
            m = re.search(r"DMESG_LINES (\d+)", o)
            dmesg0 = int(m.group(1)) if m else None
        if name in ("unloadapp", "loadapp") or name.startswith(("state", "extra")):
            continue                  # as kv260_6a.py psboard: the next load / the final state decide
        if rc != 0:
            return restore(f"step '{name}' failed (rc {rc})")
        if name.startswith("no reader left"):
            left = sorted(l for l in o.splitlines() if l.startswith("DEVMEM"))
            if left != devmem0:
                return restore(f"/dev/mem still mapped: {left} (before: {devmem0})")
        if dmesg0 is not None:
            bad = new_kernel_errors(dmesg0)
            if bad:
                return restore(f"kernel errors after '{name}': {bad[:5]}")
    after = state_of(log[-2]["out"])
    diff = K.compare_state(before or {}, after) + compare_extra(extra0, log[-1]["out"])
    for d in diff:
        print(f"UNRESTORED {d}")
    print("STATE restored: all compared items as before" if not diff else f"{len(diff)} items not restored")
    rc = subprocess.run(["scp", "-o", "BatchMode=yes", "-r", f"{K.BOARD}:{BDIR}/results", str(WORK)],
                        capture_output=True, text=True).returncode
    print(f"results fetched -> {WORK / 'results'} (rc {rc})")
    return 1 if diff else 0


# ---------------------------------------------------------------- analyze
def ival(x) -> int:
    return int(x, 16) if isinstance(x, str) else int(x)


def cmd_analyze(ns) -> int:
    K.set_variant("ps_e4a")
    art = json.loads((WORK / "artifacts.json").read_text(encoding="utf-8"))
    out, fails = {}, 0
    for prog, cmd, runmap in (("mbx_rt2k", "rt2k", None), ("mbx_rt2k_long", "long", {"A": "run1", "B": "run2", "C": "run3"})):
        rdir = WORK / "results" / prog
        res = {"checks": []}

        def check(name, ok, detail=""):
            res["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        pr = json.loads((rdir / "probe.json").read_text(encoding="utf-8"))
        tr = json.loads((rdir / f"{cmd}.json").read_text(encoding="utf-8"))
        check("probe and test completed", "error" not in pr and "error" not in tr, pr.get("error") or tr.get("error"))
        dv = pr["device"]
        ov = dv.get("overlay", {})
        check("Overlay(download=False): FPGA manager, overlays, /lib/firmware, PYNQ state unchanged",
              ov.get("download") is False and "unchanged" in ov, str(ov.get("unchanged")))
        check("PYNQ parsed the loaded bitstream (bin_data sha256 == FPGA manager firmware == manifest)",
              ov.get("pynq_bin_data_sha256") == art[prog]["bit_bin_sha256"] ==
              dv["board"]["loaded_bin_sha256"], ov.get("pynq_bin_data_sha256"))
        check("mailbox from HWH / PYNQ: 0xA0100000, 4096 B", ov.get("mailbox", {}).get("base") == "0xA0100000"
              and ov.get("mailbox", {}).get("size") == 0x1000, str(ov.get("mailbox")))
        check("debug ID 0x52563641", dv.get("debug_id") == "0x52563641", dv.get("debug_id"))
        pb = dv.get("probe", {})
        check("first read: MAGIC MBX1, VERSION 1.0, 16 x 64 B at 0x100", pb.get("MAGIC") == "0x4D425831" and
              pb.get("VERSION") == "0x00010000" and pb.get("RING_SIZE") == "0x00000010" and
              pb.get("RECORD_SIZE") == "0x00000040" and pb.get("RING_OFFSET") == "0x00000100", str(pb))
        check("probe: mappings released", pr.get("devmem_mappings_after_close") == 0, str(pr.get("devmem_mappings_after_close")))
        if "error" in tr:
            out[prog] = res
            fails += sum(not c["ok"] for c in res["checks"])
            continue
        # RV32I runs: the stage 6A / E3c-B1 criteria (TOHOST once = 1, no trap, run_cycles)
        runs = {(runmap or {}).get(t, t): {k: ival(v) for k, v in r.items()} for t, r in tr["runs"].items()}
        vv = {t: K.ps_run_values(r) for t, r in runs.items()}
        nres = len([t for t in vv if t.startswith("run")])
        a = K.analyze_program(prog, vv, [], None, nres, WORK, tuple(pr["freq"]), "CTRL.cpu_reset")
        res["run_cycles"] = {t: r["RUN_CYCLES"] for t, r in runs.items()}
        if cmd == "long":
            # power-on run: no reader, results 17..88 dropped (the last one too: no record
            # write after it), so its run time is not comparable with the runs A / B / C
            a["checks"] = [c for c in a["checks"] if c["name"] != "run_cycles identical in every run"]
            rc3 = {t: c for t, c in res["run_cycles"].items() if t != "poweron"}
            check("runs A / B / C (reader attached): run_cycles identical", len(set(rc3.values())) == 1, str(rc3))
            res["run_cycles_note"] = "power-on run without reader (72 results dropped) reported separately"
        res["checks"] += a["checks"]
        res["xsim_cycles"] = art[prog]["xsim_cycles"]
        res["pl_clk0_mhz"] = a.get("pl_clk0_mhz_measured")
        if art[prog]["xsim_cycles"]:
            check("RUN_CYCLES == xsim (tb_mbx_soc rt2k_reader)",
                  all(c == art[prog]["xsim_cycles"] for c in res["run_cycles"].values()),
                  f"{res['run_cycles']} xsim {art[prog]['xsim_cycles']}")
        if cmd == "rt2k":
            rr = tr["reader"]
            recs = rr["records"]
            s = rr["summary"] or {}
            check("reader rc 0, 44 records, no errors / dup / gaps", rr["rc"] == 0 and len(recs) == 44
                  and s.get("errors") == 0 and s.get("dup") == 0 and s.get("gaps") == 0, json.dumps(s)[:400])
            ep = [r["epoch"] for r in recs]
            groups = [ep[i:i + 11] for i in range(0, 44, 11)]
            check("4 epochs (power-on + 3 resets), 11 records each, epoch + 1 per run",
                  all(len(set(g)) == 1 for g in groups) and [g[0] for g in groups] == list(range(groups[0][0], groups[0][0] + 4)),
                  str([g[0] for g in groups]))
            check("RESULT_INDEX 1..11 and epoch_seq 0..10 in every run",
                  all([r["result_index"] for r in recs[i:i + 11]] == list(range(1, 12)) and
                      [r["epoch_seq"] for r in recs[i:i + 11]] == list(range(11)) for i in range(0, 44, 11)))
            check("every record == E2 golden (scores, argmax, gesture, seq, ts, segment)",
                  s.get("golden_ok") == 44 and s.get("golden_mismatch") == 0 and not any("check" in r for r in recs),
                  f"ok {s.get('golden_ok')} mismatch {s.get('golden_mismatch')}")
            check("flags POSTPROC|ARGMAX_OK|GOLDEN_OK, NEW_SEGMENT on the first, LAST on the 11th",
                  all(r["flags"] & 0x19 == 0x19 for r in recs) and all(recs[i]["flags"] & 0x04 for i in range(0, 44, 11))
                  and all(recs[i + 10]["flags"] & 0x80 for i in range(0, 44, 11)), "")
            check("resync events: 3", s.get("resyncs") == 3, str(s.get("resyncs")))
            for t, h in tr["headers"].items():
                check(f"{t}: header DONE, published 11, consumed 11, drop 0, error 0, consumer == producer",
                      h.get("state") == 2 and h.get("published_cnt") == 11 and h.get("consumed_cnt") == 11 and
                      h.get("drop_cnt") == 0 and h.get("error_cnt") == 0 and h.get("consumer_index") == h.get("producer_index")
                      and h.get("consumer_epoch") == h.get("epoch"), json.dumps(h))
            res["reader_summary"] = s
            res["records"] = recs
        else:
            ra = tr["A"]
            recs = ra["records"]
            s = ra["summary"] or {}
            hp = tr["headers"]["poweron"]
            kept = hp.get("published_cnt")
            check("power-on run without reader: 16 kept, 72 dropped, no error, DONE",
                  hp.get("published_cnt") == 16 and hp.get("drop_cnt") == 72 and hp.get("error_cnt") == 0 and hp.get("state") == 2,
                  json.dumps(hp))
            run = [r for r in recs if r["epoch"] == recs[-1]["epoch"]]
            check("run A: reader rc 0, kept 16 + 88 records, no errors / dup / gaps", ra["rc"] == 0 and
                  len(recs) == kept + 88 and len(run) == 88 and s.get("errors") == 0 and s.get("dup") == 0 and
                  s.get("gaps") == 0, json.dumps(s)[:400])
            check("run A: RESULT_INDEX 1..88, epoch_seq 0..87, golden for all 104 records",
                  [r["result_index"] for r in run] == list(range(1, 89)) and [r["epoch_seq"] for r in run] == list(range(88))
                  and s.get("golden_ok") == kept + 88 and s.get("golden_mismatch") == 0)
            ha = tr["headers"]["A"]
            check("run A header: published 88, drop 0, error 0, consumed 88, DONE",
                  ha.get("published_cnt") == 88 and ha.get("drop_cnt") == 0 and ha.get("error_cnt") == 0 and
                  ha.get("consumed_cnt") == 88 and ha.get("state") == 2, json.dumps(ha))
            cyc = lambda key: [((run[i][key] - run[i - 1][key]) & 0xFFFFFFFF) for i in range(1, len(run))]
            det, pub = cyc("detect_cycle"), cyc("publish_cycle")
            ps = [(run[i]["ps_read_ns"] - run[i - 1]["ps_read_ns"]) / 1e6 for i in range(1, len(run))]
            res["A_timing"] = {"detect_cycles_min_max": [min(det), max(det)], "publish_cycles_min_max": [min(pub), max(pub)],
                               "ps_read_ms_mean": round(sum(ps) / len(ps), 3), "ps_read_ms_min": round(min(ps), 3),
                               "ps_read_ms_max": round(max(ps), 3),
                               "publish_minus_detect": [min(r["publish_minus_detect_cycles"] for r in run),
                                                        max(r["publish_minus_detect_cycles"] for r in run)],
                               "cnn_lat_cycles": sorted({r["cnn_lat_cycles"] for r in run})}
            same_pass = [d for i, d in enumerate(det) if (i + 1) % 11 != 0]
            check("run A: DETECT_CYCLE step 5,000,000 +- 1000 within a pass (20 Hz in PL cycles)",
                  all(abs(d - 5_000_000) <= 1000 for d in same_pass), f"{min(same_pass)}..{max(same_pass)}")
            res["A_summary"] = s
            res["A_records"] = run
            for x in ("B", "C"):
                r1, r2 = tr[f"{x}1"], tr[f"{x}2"]
                s1, s2 = r1["summary"] or {}, r2["summary"] or {}
                ep = r1["records"][-1]["epoch"] if r1["records"] else None
                got = [r for r in r1["records"] + r2["records"] if r["epoch"] == ep]
                ri = [r["result_index"] for r in got]
                hx = tr["headers"][x]
                drops = hx.get("drop_cnt")
                check(f"run {x}: reader 1 stopped after 30, unmapped; reader 2 resumed (no attach to a new epoch)",
                      r1["rc"] == 0 and len(r1["records"]) == 30 and s1.get("devmem_mappings_after_close") == 0
                      and s2.get("resumes") == 1 and s2.get("devmem_mappings_after_close") == 0 and r2["rc"] == 0,
                      f"r1 {len(r1['records'])} rc {r1['rc']}, r2 resumes {s2.get('resumes')} rc {r2['rc']}")
                # drops followed by a published record show up as its DROP_BEFORE gap; drops at the
                # end of a run (ring still full when the run ended) only in DROP_CNT: gaps + trailing
                # == DROP_CNT (the E4a-A reader BFM rule, dv/mbx/mbx_ps_reader.vh rd_report)
                trailing = hx.get("result_cnt", 0) - (ri[-1] if ri else 0)
                check(f"run {x}: no duplicate, records + DROP_CNT == 88, gaps + trailing == DROP_CNT, golden ok",
                      len(ri) == len(set(ri)) and ri == sorted(ri) and len(got) + drops == 88 and
                      s2.get("gaps", 0) + trailing == drops and s1.get("golden_mismatch") == 0
                      and s2.get("golden_mismatch") == 0 and s2.get("index_errors") == 0 and hx.get("error_cnt") == 0,
                      f"records {len(got)} drop {drops} gaps {s2.get('gaps')} trailing {trailing}")
                res[f"{x}"] = {"reader1_records": len(r1["records"]), "reader2_records": len(r2["records"]),
                               "drop_cnt": drops, "reader2_startup_s": s2.get("startup_s"),
                               "reader2_wait_s": json.loads(Path(rdir / f"{x}2.report.json").read_text()).get("waited_for_pid_s")
                               if (rdir / f"{x}2.report.json").is_file() else None,
                               "first_resumed": got[30]["result_index"] if len(got) > 30 else None}
            check("run C (warm restart): no drop", tr["headers"]["C"].get("drop_cnt") == 0, json.dumps(tr["headers"]["C"]))
        check("no process maps /dev/mem after the test", tr.get("devmem_holders_after") == [], str(tr.get("devmem_holders_after")))
        nf = sum(not c["ok"] for c in res["checks"])
        fails += nf
        out[prog] = res
        print(f"== {prog}: {len(res['checks']) - nf}/{len(res['checks'])} checks")
        for c in res["checks"]:
            if not c["ok"] or ns.verbose:
                print(f"   {'ok  ' if c['ok'] else 'FAIL'} {c['name']}  {str(c['detail'])[:300]}")
    (WORK / "analysis.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("artifacts")
    sub.add_parser("stage")
    s = sub.add_parser("session")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--pre-only", action="store_true", help="read-only board state only")
    a = sub.add_parser("analyze")
    a.add_argument("-v", "--verbose", action="store_true")
    ns = ap.parse_args()
    return {"artifacts": cmd_artifacts, "stage": cmd_stage, "session": cmd_session, "analyze": cmd_analyze}[ns.cmd](ns)


if __name__ == "__main__":
    sys.exit(main())
