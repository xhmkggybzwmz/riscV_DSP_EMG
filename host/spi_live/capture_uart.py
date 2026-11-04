"""Record labelled raw EMG on a PC from the STM32 over UART, without the KV260 (docs/system/stm32_spi_live.md).

    python -m spi_live.capture_uart --port COM5 --out ../analysis/spi/rec1.npz --rounds 6 --hold 10

Firmware: build/stm32/adc_uart (EMG_SOURCE_MODE 0, EMG_TRANSPORT_MODE 0). Same prompts and file as capture.py, so
ml/scripts/train_spi_levels.py reads it unchanged.

SAFETY: electrodes on the body only with the laptop on battery: charger, KV260, network cable, monitor all unplugged.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .capture import BLOCK, CLASSES, Recorder

MAGIC = b"EMG1"
FRAME = 278
HEADER = 20


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Parser:
    """EMG1 frames from a byte stream: resynchronises on the magic, counts what it had to skip or reject"""

    def __init__(self):
        self.buf = bytearray()
        self.skipped = 0
        self.bad_crc = 0
        self.bad_format = 0

    def feed(self, data: bytes) -> list[tuple[int, int, list[int]]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                keep = min(len(self.buf), len(MAGIC) - 1)
                self.skipped += len(self.buf) - keep
                del self.buf[:len(self.buf) - keep]
                return out
            if i:
                self.skipped += i
                del self.buf[:i]
            if len(self.buf) < FRAME:
                return out
            f = bytes(self.buf[:FRAME])
            if crc16(f[:FRAME - 2]) != int.from_bytes(f[FRAME - 2:], "little"):
                self.bad_crc += 1
                del self.buf[:len(MAGIC)]        # the magic may have been inside the samples
                continue
            del self.buf[:FRAME]
            if f[4:7] != b"\x01\x01\x01" or f[16:20] != b"\x80\x00\x00\x01":
                self.bad_format += 1
                continue
            rate = int.from_bytes(f[8:12], "little")
            index = int.from_bytes(f[12:16], "little")
            samples = [int.from_bytes(f[HEADER + 2 * k:HEADER + 2 * k + 2], "little") for k in range(BLOCK)]
            out.append((index, rate, samples))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=460800)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--hold", type=float, default=10.0)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--lead", type=float, default=5.0)
    ap.add_argument("--allow-test-pattern", action="store_true")
    ns = ap.parse_args(argv)

    import numpy as np
    import serial

    p = Parser()
    rec = Recorder()
    state = {"label": -1, "round": 0, "checked": False}
    with serial.Serial(ns.port, ns.baud, timeout=0.02) as s:
        s.reset_input_buffer()

        def pump(seconds: float, label: int, rnd: int, settle: float = 0.0) -> int | None:
            t0 = time.monotonic()
            while (dt := time.monotonic() - t0) < seconds:
                for index, rate, samples in p.feed(s.read(4096)):
                    if rate != 2000:
                        print(f"ERROR: the STM32 reports {rate} Hz, the pipeline needs 2000", file=sys.stderr)
                        return 2
                    if not state["checked"]:
                        state["checked"] = True
                        counting = all(((b - a) & 0xFFF) == 1 for a, b in zip(samples, samples[1:]))
                        if counting and not ns.allow_test_pattern:
                            print("ERROR: this is the TEST pattern: flash build/stm32/adc_uart", file=sys.stderr)
                            return 2
                    rec.add(index, samples, -1 if dt < settle else label, rnd)
            return None

        print(f"get ready: {ns.lead:.0f} s", flush=True)
        if (rc := pump(ns.lead, -1, 0)) is not None:
            return rc
        if rec.seen == 0:
            print(f"ERROR: no valid EMG1 packet from {ns.port}: check TX/RX, GND, the port and the firmware", file=sys.stderr)
            return 2
        for r in range(ns.rounds):
            for c, name in enumerate(CLASSES):
                print(f"round {r + 1}/{ns.rounds}: >>> {name} <<< for {ns.hold:.0f} s", flush=True)
                if (rc := pump(ns.hold, c, r, ns.settle)) is not None:
                    return rc
    arr = rec.arrays()
    np.savez(ns.out, **arr)
    report = {"file": ns.out, "blocks": rec.seen, "lost_blocks": rec.lost, "bad_crc": p.bad_crc, "bad_format": p.bad_format,
              "skipped_bytes": p.skipped}
    for c, name in enumerate(CLASSES):
        x = arr["u16"][arr["label"] == c].astype(float)
        report[name] = {"mean": round(float(x.mean()), 1), "rms_ac": round(float(x.std()), 1),
                        "min": int(x.min()), "max": int(x.max())}
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
