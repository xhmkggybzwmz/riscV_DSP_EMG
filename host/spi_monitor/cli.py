import argparse
import json
import time

from .monitor import BASE, SIZE, SpiMonitor


def main():
    ap = argparse.ArgumentParser(description="Observe STM32 EMG1 packets received by KV260 PL")
    ap.add_argument("--bit", required=True,
                    help="matching .bit file; parsed with Overlay(download=False)")
    ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--count", type=int, default=0, help="0 means run until Ctrl-C")
    ap.add_argument("--clear", action="store_true")
    ns = ap.parse_args()
    from pynq import MMIO, Overlay
    Overlay(ns.bit, download=False)
    mon = SpiMonitor(MMIO(BASE, SIZE))
    mon.verify()
    if ns.clear:
        mon.clear()
    n = 0
    try:
        while ns.count == 0 or n < ns.count:
            snap = mon.snapshot()
            frame_index, samples = mon.frame()
            print(json.dumps({**snap.__dict__, "frame_index": frame_index,
                              "first": samples[:8], "last": samples[-8:]}), flush=True)
            n += 1
            time.sleep(ns.period)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
