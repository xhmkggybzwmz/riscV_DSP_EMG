"""Software model of AXI DMA MM2S + esi_dma_src_axil for the feeder tests on the PC (the RTL and the real IP are
tested in dv/e5). The model consumes a transfer when LENGTH is written, and records every segment it delivered.
Register behaviour as PG021 / rtl/e3c/esi_dma_src_axil.v; anything out of order is recorded in .violations."""

from __future__ import annotations

import numpy as np

from ps_dma import registers as R
from ps_mailbox.registers import MmioWindow


class FakeMem:
    def __init__(self, size: int, physical_address: int = 0x7000_0000):
        self.data = bytearray(size)
        self.physical_address = physical_address
        self.flushed = 0
        self.flushed_copy = None

    def flush(self):
        self.flushed += 1
        self.flushed_copy = bytes(self.data)          # what the DMA would see: only what was flushed


class FakeHw:
    def __init__(self, mem: FakeMem):
        self.mem = mem
        self.rs = False
        self.dmasr_err = 0
        self.ioc = False
        self.sa = 0
        self.sa_msb = 0
        self.src_run = False
        self.period = 50000
        self.seq_base = 0
        self.frames = 0
        self.segs = 0
        self.delivered: list[dict] = []
        self.violations: list[str] = []
        self.busy_polls = 0          # DMASR reads that still report busy, per transfer
        self.busy_left = 0
        self.fail_on_transfer = None
        self.hang = False
        self.resets = 0
        self.src_id = R.SRC_ID_VALUE
        self.reset_polls = 0


class DmaWindow(MmioWindow):
    def __init__(self, hw: FakeHw):
        super().__init__(R.DMA_SIZE)
        self.hw = hw

    def _rd(self, off):
        h = self.hw
        if off == R.MM2S_DMACR:
            if h.reset_polls:
                h.reset_polls -= 1
                return R.CR_RESET
            return R.CR_RS if h.rs else 0
        if off == R.MM2S_DMASR:
            if h.hang:
                return 0
            if h.busy_left:
                h.busy_left -= 1
                return 0
            return (0 if h.rs else R.SR_HALTED) | (R.SR_IDLE if h.rs else 0) | (R.SR_IOC if h.ioc else 0) | h.dmasr_err
        return 0

    def _wr(self, off, v):
        h = self.hw
        if off == R.MM2S_DMACR:
            if v & R.CR_RESET:
                h.rs = False
                h.dmasr_err = 0
                h.ioc = False
                h.resets += 1
                h.reset_polls = 2
            else:
                h.rs = bool(v & R.CR_RS)
        elif off == R.MM2S_DMASR:
            if v & R.SR_IOC:
                h.ioc = False
        elif off == R.MM2S_SA:
            h.sa = v
        elif off == R.MM2S_SA_MSB:
            h.sa_msb = v
        elif off == R.MM2S_LENGTH:
            self._transfer(v)

    def _transfer(self, n):
        h = self.hw
        if not h.rs:
            h.violations.append("LENGTH written while halted")
            return
        if not h.src_run:
            h.violations.append("transfer started with the source not running")
        addr = (h.sa_msb << 32) | h.sa
        off = addr - h.mem.physical_address
        if addr % R.ADDR_ALIGN:
            h.violations.append(f"unaligned start {addr:#x}")
        if n % 8:
            h.violations.append(f"length {n} is not whole frames")
        if off < 0 or off + n > len(h.mem.data):
            h.dmasr_err |= 0x20
            h.rs = False
            return
        if h.mem.flushed_copy is None:
            h.violations.append("transfer before any cache flush")
            return
        if h.fail_on_transfer == len(h.delivered):
            h.dmasr_err |= 0x20
            return
        raw = h.mem.flushed_copy[off:off + n]
        h.delivered.append({"addr": addr, "bytes": n, "frames": np.frombuffer(raw, dtype="<i2").reshape(-1, 4).copy()})
        h.frames += n // 8
        h.segs += 1
        h.ioc = True
        h.busy_left = h.busy_polls


class SrcWindow(MmioWindow):
    def __init__(self, hw: FakeHw):
        super().__init__(R.SRC_SIZE)
        self.hw = hw

    def _rd(self, off):
        h = self.hw
        return {R.SRC_ID: h.src_id, R.SRC_CTRL: int(h.src_run), R.SRC_PERIOD: h.period, R.SRC_FRAME_CNT: h.frames,
                R.SRC_SEG_CNT: h.segs, R.SRC_SEQ_BASE: h.seq_base}.get(off, 0)

    def _wr(self, off, v):
        h = self.hw
        if off == R.SRC_CTRL:
            if v & R.CTRL_SOFT_RESET:
                h.src_run = False
                h.frames = 0
                h.segs = 0
            else:
                h.src_run = bool(v & R.CTRL_RUN)
        elif off == R.SRC_PERIOD:
            h.period = v
        elif off == R.SRC_SEQ_BASE:
            h.seq_base = v
