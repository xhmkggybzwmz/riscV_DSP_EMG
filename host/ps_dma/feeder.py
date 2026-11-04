"""Send ESI segments from PS memory to the PL: AXI DMA MM2S (direct register mode) + esi_dma_src_axil.

Programming order, also followed by dv/e5/tb_e5_soc.v (so it has been simulated with the real IP):
  start : source SOFT_RESET -> DMA reset, wait until it clears -> source PERIOD, SEQ_BASE, RUN -> DMA RS, wait not halted
  send  : SA, SA_MSB, LENGTH (starts the transfer) -> poll DMASR until Idle, any error stops everything -> clear IOC
  stop  : source RUN = 0 -> DMA reset -> source SOFT_RESET
The DMA only reads memory (no S2MM channel): nothing is written back into PS memory. Polling only, no interrupt
is connected. A transfer that does not finish in time raises FeederError after stopping: no retry.

mem: object with .physical_address (int), .data (writable buffer, at least layout() bytes), .flush()
(pynq.allocate buffer: the HP port is not coherent, the caches must be written back).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from . import registers as R
from .dataset import Segment


class FeederError(Exception):
    pass


def align_up(n: int, a: int) -> int:
    return (n + a - 1) // a * a


def layout(segments: list[Segment], align: int = 64) -> tuple[list[int], int]:
    """byte offset of every segment in the buffer, total size"""
    if align % R.ADDR_ALIGN:
        raise FeederError(f"alignment {align} is not a multiple of {R.ADDR_ALIGN}")
    offs, n = [], 0
    for s in segments:
        if s.frames.ndim != 2 or s.frames.shape[1] != 4 or s.frames.dtype != np.int16:
            raise FeederError(f"segment {s.label}: frames must be int16 [T, 4], got {s.frames.dtype} {s.frames.shape}")
        if not 0 < s.nbytes <= R.LENGTH_MAX:
            raise FeederError(f"segment {s.label}: {s.nbytes} bytes (limit {R.LENGTH_MAX})")
        offs.append(n)
        n = align_up(n + s.nbytes, align)
    return offs, n


@dataclass
class Sent:
    label: str
    frames: int
    seconds: float


class DmaFeeder:
    def __init__(self, dma, src, mem, poll_s: float = 0.001, sleep=time.sleep, clock=time.monotonic):
        self.dma, self.src, self.mem = dma, src, mem
        self.poll_s, self.sleep, self.clock = poll_s, sleep, clock
        self.period = 0
        self.segments: list[Segment] = []
        self.offs: list[int] = []
        self.sent: list[Sent] = []
        self.started = False

    # ------------------------------------------------------------ data
    def load(self, segments: list[Segment]) -> None:
        self.offs, total = layout(segments)
        view = np.frombuffer(self.mem.data, dtype=np.uint8)
        if view.size < total:
            raise FeederError(f"buffer of {view.size} bytes, {total} needed")
        if self.mem.physical_address % R.ADDR_ALIGN:
            raise FeederError(f"buffer at {self.mem.physical_address:#x} is not {R.ADDR_ALIGN}-byte aligned")
        for s, o in zip(segments, self.offs):
            view[o:o + s.nbytes] = np.frombuffer(s.frames.astype("<i2").tobytes(), dtype=np.uint8)
        self.mem.flush()
        self.segments = list(segments)

    # ------------------------------------------------------------ control
    def _wait(self, what: str, done, timeout: float) -> None:
        t_end = self.clock() + timeout
        while not done():
            if self.clock() > t_end:
                self.stop()
                raise FeederError(f"timeout: {what}")
            self.sleep(self.poll_s)

    def start(self, period: int = R.PERIOD_2KHZ, seq_base: int = 0) -> None:
        ident = self.src.read32(R.SRC_ID)
        if ident != R.SRC_ID_VALUE:
            raise FeederError(f"source ID {ident:#x}: not esi_dma_src_axil")
        self.period = period
        self.src.write32(R.SRC_CTRL, R.CTRL_SOFT_RESET)
        self.dma.write32(R.MM2S_DMACR, R.CR_RESET)
        self._wait("DMA reset", lambda: not self.dma.read32(R.MM2S_DMACR) & R.CR_RESET, 1.0)
        self.src.write32(R.SRC_PERIOD, period)
        self.src.write32(R.SRC_SEQ_BASE, seq_base)
        self.src.write32(R.SRC_CTRL, R.CTRL_RUN)
        self.dma.write32(R.MM2S_DMACR, R.CR_RS)
        self._wait("DMA run", lambda: not self.dma.read32(R.MM2S_DMASR) & R.SR_HALTED, 1.0)
        self.started = True

    def _timeout(self, frames: int) -> float:
        # real time plus margin (the sink is slower than a short PERIOD by the CNN latency per window)
        return 5.0 + frames * max(self.period, 2000) / 100e6 * 4

    def send(self, i: int) -> Sent:
        if not self.started:
            raise FeederError("start() first")
        s, o = self.segments[i], self.offs[i]
        addr = self.mem.physical_address + o
        t0 = self.clock()
        self.dma.write32(R.MM2S_SA, addr & 0xFFFFFFFF)
        self.dma.write32(R.MM2S_SA_MSB, addr >> 32)
        self.dma.write32(R.MM2S_LENGTH, s.nbytes)

        def done() -> bool:
            sr = self.dma.read32(R.MM2S_DMASR)
            if sr & R.SR_ERRORS:
                self.stop()
                raise FeederError(f"DMASR {sr:#010x} in segment {s.label}")
            return bool(sr & R.SR_IDLE)
        self._wait(f"segment {s.label}", done, self._timeout(s.frames.shape[0]))
        self.dma.write32(R.MM2S_DMASR, R.SR_IOC)
        r = Sent(s.label, s.frames.shape[0], self.clock() - t0)
        self.sent.append(r)
        return r

    def run(self, order: list[int] | None = None, should_stop=None) -> list[Sent]:
        """send the segments in `order` (default: each once); stops early if should_stop() is true"""
        for i in (range(len(self.segments)) if order is None else order):
            if should_stop is not None and should_stop():
                break
            self.send(i)
        return self.sent

    def counters(self) -> dict:
        return {"frames": self.src.read32(R.SRC_FRAME_CNT), "segments": self.src.read32(R.SRC_SEG_CNT),
                "late": self.src.read32(R.SRC_LATE_CNT), "underrun": self.src.read32(R.SRC_UNDERRUN_CNT),
                "keep_err": self.src.read32(R.SRC_KEEP_ERR_CNT), "status": self.src.read32(R.SRC_STATUS),
                "dmasr": self.dma.read32(R.MM2S_DMASR)}

    def wait_drained(self, frames: int, timeout: float = 5.0) -> None:
        """every frame has left the source (the DMA reports Idle when the last beat entered it)"""
        self._wait("source drained", lambda: self.src.read32(R.SRC_FRAME_CNT) >= frames, timeout)

    def stop(self) -> None:
        """source stopped, DMA reset, source reset: safe in any state, also after an error"""
        self.src.write32(R.SRC_CTRL, 0)
        self.dma.write32(R.MM2S_DMACR, R.CR_RESET)
        t_end = self.clock() + 1.0
        while self.dma.read32(R.MM2S_DMACR) & R.CR_RESET and self.clock() < t_end:
            self.sleep(self.poll_s)
        self.src.write32(R.SRC_CTRL, R.CTRL_SOFT_RESET)
        self.started = False
