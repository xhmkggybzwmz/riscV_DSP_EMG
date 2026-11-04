#!/usr/bin/env python3
"""Flag SystemVerilog-only constructs in files that must be Verilog-2001.

Icarus Verilog's -g2001 mode and Vivado xvlog (Verilog mode) each accept some
SystemVerilog constructs (e.g. iverilog -g2001 accepts `logic`, `+=` and
`for (integer i = ...)`; xvlog accepts the inline loop declaration). This
token scanner complements them. Comments and string literals are stripped
before scanning.

Allowed on purpose: Verilog-2005 $clog2 (see docs/cpu/rv32i_core_spec.md).

Usage:  python scripts/lint/check_verilog2001.py [paths...]
        (default: rtl/ dv/ ; scans *.v and *.vh)
Exit status: 0 = clean, 1 = findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SV_KEYWORDS = [
    "logic", "bit", "byte", "shortint", "int", "longint", "shortreal", "string",
    "always_ff", "always_comb", "always_latch", "typedef", "enum", "struct",
    "union", "packed", "interface", "modport", "package", "import", "class",
    "unique", "unique0", "priority", "assert", "assume", "cover", "property",
    "sequence", "program", "final", "foreach", "return", "break", "continue",
    "void", "automatic_type", "inside", "let", "bind", "clocking",
]

PATTERNS = [
    (re.compile(r"\b(" + "|".join(SV_KEYWORDS) + r")\b"), "SystemVerilog keyword"),
    (re.compile(r"(\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)"), "SV assignment/increment operator"),
    (re.compile(r"\bfor\s*\(\s*(integer|int|genvar)\b"), "declaration inside for()"),
    (re.compile(r"'[01xXzZ](?![0-9a-zA-Z_])"), "SV unsized fill literal ('0, '1, 'x)"),
    (re.compile(r"\b\d+'\s*\("), "SV size cast"),
    (re.compile(r"\$(bits|size|left|right|high|low|increment|countones|onehot0?|isunknown|cast)\b"),
     "SV system function"),
]


def strip_comments_and_strings(text: str) -> str:
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            seg = text[i:(n if j < 0 else j + 2)]
            out.append("\n" * seg.count("\n"))   # keep line numbers
            i = n if j < 0 else j + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan(path: Path) -> list[str]:
    findings = []
    clean = strip_comments_and_strings(path.read_text(encoding="utf-8"))
    for lineno, line in enumerate(clean.splitlines(), 1):
        for rx, what in PATTERNS:
            m = rx.search(line)
            if m:
                try:
                    rel = path.relative_to(REPO_ROOT).as_posix()
                except ValueError:
                    rel = path.as_posix()
                findings.append(f"{rel}:{lineno}: {what}: '{m.group(0)}'")
    return findings


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:]] or [REPO_ROOT / "rtl", REPO_ROOT / "dv"]
    files: list[Path] = []
    for t in targets:
        t = t if t.is_absolute() else (Path.cwd() / t)
        if t.is_dir():
            files += sorted(p for p in t.rglob("*") if p.suffix in (".v", ".vh"))
        elif t.is_file():
            files.append(t)
    findings = [f for p in files for f in scan(p.resolve())]
    for f in findings:
        print(f)
    print(f"check_verilog2001: {len(files)} file(s) scanned, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
