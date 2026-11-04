"""Fetch a bounded subset of GRABMyo v1.1.0 from PhysioNet (official source only).

    python ml/scripts/fetch_grabmyo.py plan      # listings + SHA256SUMS -> manifest, size check
    python ml/scripts/fetch_grabmyo.py download  # download the manifest (resumable), verify SHA-256
    python ml/scripts/fetch_grabmyo.py verify    # re-hash the files on disk against SHA256SUMS

Subset "G0" (authorised 2026-10-20): participants 1-3, sessions 1-3, gestures
4/11/12/15/16/17 (the six unified classes, docs/system_design_extend.md 4.1),
trials 1-7 -> 378 records (.dat + .hea), plus the dataset documentation files.
Hard limit: 300 MB in total; `plan` stops before any data file is fetched if
the manifest is larger. Nothing is fetched recursively: every URL is listed in
the manifest first.

Output (git-ignored): data/public/grabmyo/
    raw/<same relative path as on PhysioNet>    files, made read-only after verification
    manifest_<subset>.csv                       one row per file (url, ids, size, sha256, status)
    provenance.json                             version, base URL, licence, citation, dates
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "data" / "public" / "grabmyo"
RAW = OUT / "raw"
VERSION = "1.1.0"
BASE = f"https://physionet.org/files/grabmyo/{VERSION}/"
# Official AWS Open Data mirror, listed on https://physionet.org/content/grabmyo/1.1.0/
# ("aws s3 sync --no-sign-request s3://physionet-open/grabmyo/1.1.0/"). Used only as a
# faster transport for files of the manifest; every file is checked against the
# SHA256SUMS.txt fetched from physionet.org itself.
MIRRORS = {"physionet": BASE, "aws": f"https://physionet-open.s3.amazonaws.com/grabmyo/{VERSION}/"}
# size limits per subset (authorised by the developer): total bytes of the manifest and
# bytes that would actually be downloaded (files not yet present with a verified hash)
LIMITS = {"G0": {"total": 300 * 1000 * 1000, "new": 300 * 1000 * 1000},      # 2026-10-20
          "G1": {"total": 1000 * 1000 * 1000, "new": 700 * 1000 * 1000}}     # 2026-10-20 (E1b)
USER_AGENT = "riscv-emg-soc-fetch/0.1 (research; contact via repository owner)"

DOC_FILES = ["readme.txt", "LICENSE.txt", "MotionSequence.txt", "Electrodelocation.pdf",
             "DeviceInfo.pdf", "GestureList.JPG", "subject-info.csv"]
SUMS_FILE = "SHA256SUMS.txt"

SUBSETS = {
    "G0": {"participants": [1, 2, 3], "sessions": [1, 2, 3],
           "gestures": [4, 11, 12, 15, 16, 17], "trials": list(range(1, 8))},
    "G1": {"participants": list(range(1, 11)), "sessions": [1, 2, 3],
           "gestures": [4, 11, 12, 15, 16, 17], "trials": list(range(1, 8))},
}

LISTING_RE = re.compile(r'<a href="([^"/]+)">[^<]*</a>\s+\S+\s+\S+\s+(\d+)')
FIELDS = ["relpath", "url", "kind", "session", "participant", "gesture", "trial",
          "listed_bytes", "sha256_expected", "sha256_actual", "status", "downloaded_from"]


def http_get(url: str, headers: dict | None = None, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_text(url: str) -> str:
    with http_get(url) as r:
        return r.read().decode("utf-8", errors="replace")


def listing(url: str) -> dict[str, int]:
    """name -> size from a PhysioNet directory index page."""
    return {m.group(1): int(m.group(2)) for m in LISTING_RE.finditer(fetch_text(url))}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path, expected_size: int, retries: int = 4) -> None:
    """Resumable download to <dest>.part, then rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        if have > expected_size:
            part.unlink()
            have = 0
        try:
            hdr = {"Range": f"bytes={have}-"} if have else {}
            with http_get(url, hdr) as r:
                if have and r.status != 206:          # server ignored the range: restart
                    have = 0
                with part.open("ab" if have else "wb") as f:
                    while True:
                        block = r.read(1 << 16)
                        if not block:
                            break
                        f.write(block)
            if part.stat().st_size != expected_size:
                raise IOError(f"size {part.stat().st_size} != listed {expected_size}")
            part.replace(dest)
            return
        except (urllib.error.URLError, IOError, TimeoutError) as e:
            print(f"    retry {attempt}/{retries} for {dest.name}: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"download failed: {url}")


def manifest_path(subset: str) -> Path:
    return OUT / f"manifest_{subset}.csv"


def read_manifest(subset: str) -> list[dict]:
    with manifest_path(subset).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_manifest(subset: str, rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path(subset).with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(manifest_path(subset))


def load_sums() -> dict[str, str]:
    sums = {}
    for line in (RAW / SUMS_FILE).read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            sums[parts[1].strip().lstrip("*")] = parts[0].lower()
    return sums


def cmd_plan(subset: str) -> int:
    sel = SUBSETS[subset]
    top = listing(BASE)
    missing = [f for f in DOC_FILES + [SUMS_FILE] if f not in top]
    if missing:
        sys.exit(f"plan: not in the top-level listing: {missing}")
    # SHA256SUMS first (needed to build the manifest)
    if not (RAW / SUMS_FILE).exists():
        download(BASE + SUMS_FILE, RAW / SUMS_FILE, top[SUMS_FILE])
    sums = load_sums()
    rows = []
    for name in DOC_FILES + [SUMS_FILE]:
        rows.append({"relpath": name, "url": BASE + name, "kind": "doc", "session": "", "participant": "",
                     "gesture": "", "trial": "", "listed_bytes": top[name],
                     "sha256_expected": sums.get(name, ""), "sha256_actual": "", "status": "planned"})
    absent = []
    for s in sel["sessions"]:
        for p in sel["participants"]:
            folder = f"Session{s}/session{s}_participant{p}/"
            files = listing(BASE + folder)
            for g in sel["gestures"]:
                for t in sel["trials"]:
                    for ext in (".dat", ".hea"):
                        name = f"session{s}_participant{p}_gesture{g}_trial{t}{ext}"
                        if name not in files:
                            absent.append(folder + name)
                            continue
                        rel = folder + name
                        rows.append({"relpath": rel, "url": BASE + rel, "kind": ext[1:], "session": s,
                                     "participant": p, "gesture": g, "trial": t,
                                     "listed_bytes": files[name], "sha256_expected": sums.get(rel, ""),
                                     "sha256_actual": "", "status": "planned"})
    total = sum(int(r["listed_bytes"]) for r in rows)
    n_rec = sum(1 for r in rows if r["kind"] == "dat")
    # files already on disk whose SHA-256 matches the official list are not downloaded again
    present = 0
    for r in rows:
        f = RAW / r["relpath"]
        if f.exists() and f.stat().st_size == int(r["listed_bytes"]) and r["sha256_expected"]                 and sha256_of(f) == r["sha256_expected"]:
            present += int(r["listed_bytes"])
    new = total - present
    lim = LIMITS[subset]
    no_sum = [r["relpath"] for r in rows if not r["sha256_expected"] and r["relpath"] != SUMS_FILE]
    write_manifest(subset, rows)
    print(f"plan {subset}: {n_rec} records, {len(rows)} files, {total} bytes ({total / 1e6:.1f} MB); "
          f"already present and verified {present} bytes; to download {new} bytes ({new / 1e6:.1f} MB); "
          f"limits total {lim['total'] / 1e6:.0f} MB, new {lim['new'] / 1e6:.0f} MB")
    print(f"  missing on server: {len(absent)}; without SHA-256 entry: {len(no_sum)}")
    for a in absent[:10]:
        print("   missing", a)
    for a in no_sum[:10]:
        print("   no sha256", a)
    prov = {"dataset": "GRABMyo", "version": VERSION, "base_url": BASE, "subset": subset,
            "selection": sel, "planned_at": dt.datetime.now().isoformat(timespec="seconds"),
            "planned_bytes": total, "planned_new_bytes": new, "records": n_rec,
            "license": "CC BY 4.0 (LICENSE.txt and PhysioNet page of v1.1.0); readme.txt of the same "
                       "release says 'Open Data Commons Attribution License v1.0' - discrepancy recorded",
            "citation": ["Jiang N, Pradhan A, He J. GRABMyo (version 1.1.0). PhysioNet (2024). "
                         "https://doi.org/10.13026/89dm-f662",
                         "Pradhan A, He J, Jiang N. Multi-day dataset of forearm and wrist electromyogram for "
                         "hand gesture recognition and biometrics. Sci Data 9, 733 (2022)."]}
    old = json.loads((OUT / "provenance.json").read_text(encoding="utf-8")) if (OUT / "provenance.json").exists() else {}
    hist = old.pop("history", [])
    if old:
        hist.append(old)
    prov["history"] = hist
    (OUT / "provenance.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")
    if absent or total > lim["total"] or new > lim["new"]:
        print("plan: STOP (missing files or over the size limit); nothing else downloaded")
        return 1
    return 0


def make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _fetch_one(r: dict, source: str) -> dict:
    """Download (if needed) and hash one manifest row; returns the updated fields."""
    dest = RAW / r["relpath"]
    upd = {}
    if not dest.exists():
        url = MIRRORS[source] + r["relpath"]
        download(url, dest, int(r["listed_bytes"]))
        upd["downloaded_from"] = url
    elif not r.get("downloaded_from"):
        upd["downloaded_from"] = MIRRORS[source] + r["relpath"] + " (earlier run, same source)"
    actual = sha256_of(dest)
    upd["sha256_actual"] = actual
    if r["relpath"] == SUMS_FILE:
        # the checksum list cannot contain its own hash; it is fetched from physionet.org itself
        upd["status"] = "reference_checksums"
        make_read_only(dest)
    elif r["sha256_expected"] and actual != r["sha256_expected"]:
        upd["status"] = "sha256_mismatch"
    else:
        upd["status"] = "verified" if r["sha256_expected"] else "downloaded_no_reference_hash"
        make_read_only(dest)
    return upd


def cmd_download(subset: str, source: str, workers: int) -> int:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = read_manifest(subset)
    total = sum(int(r["listed_bytes"]) for r in rows)
    if total > LIMITS[subset]["total"]:
        sys.exit(f"download: manifest is {total} bytes > limit")
    todo = [i for i, r in enumerate(rows)
            if not (r["status"] in ("verified", "reference_checksums") and (RAW / r["relpath"]).exists()
                    and (RAW / r["relpath"]).stat().st_size == int(r["listed_bytes"]))]
    print(f"download {subset}: {len(rows) - len(todo)} already verified, {len(todo)} to do, {workers} workers")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, rows[i], source): i for i in todo}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                rows[i].update(fut.result())
            except Exception as e:                       # keep going; reported at the end
                rows[i]["status"] = f"failed: {e}"
            done += 1
            if done % 50 == 0:
                write_manifest(subset, rows)
                print(f"  {done}/{len(todo)}")
    write_manifest(subset, rows)
    ok = sum(1 for r in rows if r["status"] in ("verified", "reference_checksums"))
    bad = len(rows) - ok
    prov = json.loads((OUT / "provenance.json").read_text(encoding="utf-8"))
    prov["downloaded_at"] = dt.datetime.now().isoformat(timespec="seconds")
    prov["transport"] = {"source": source, "base": MIRRORS[source],
                         "checksums_from": BASE + SUMS_FILE}
    prov["downloaded_bytes"] = sum((RAW / r["relpath"]).stat().st_size for r in rows if (RAW / r["relpath"]).exists())
    (OUT / "provenance.json").write_text(json.dumps(prov, indent=2), encoding="utf-8")
    print(f"download {subset}: {ok} verified, {bad} not verified, {prov['downloaded_bytes']} bytes on disk")
    for r in rows:
        if r["status"] not in ("verified", "reference_checksums"):
            print("  ", r["relpath"], r["status"])
    return 1 if bad else 0


def cmd_verify(subset: str) -> int:
    rows = read_manifest(subset)
    bad = []
    for r in rows:
        p = RAW / r["relpath"]
        if not p.exists():
            bad.append((r["relpath"], "missing"))
        elif r["sha256_expected"] and sha256_of(p) != r["sha256_expected"]:
            bad.append((r["relpath"], "sha256"))
    print(f"verify {subset}: {len(rows) - len(bad)}/{len(rows)} ok")
    for b in bad[:20]:
        print("  ", *b)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "download", "verify"])
    ap.add_argument("--subset", default="G0", choices=sorted(SUBSETS))
    ap.add_argument("--source", default="physionet", choices=sorted(MIRRORS),
                    help="transport for `download` (default physionet; aws = official PhysioNet mirror)")
    ap.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    ns = ap.parse_args()
    if ns.command == "download":
        return cmd_download(ns.subset, ns.source, ns.workers)
    return {"plan": cmd_plan, "verify": cmd_verify}[ns.command](ns.subset)


if __name__ == "__main__":
    sys.exit(main())
