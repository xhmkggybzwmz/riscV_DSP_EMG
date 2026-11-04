"""MMIO access layer of the mailbox reader: 32-bit reads / writes at window offsets.

Every backend checks that the offset is inside the window and 4-byte aligned before it
touches anything: an access outside the mapped PL window could reach an unmapped HPM0
address (AXI DECERR -> SError on the PS), so it is refused here.

Backends
  PynqMmio     pynq.MMIO(base, size) (PYNQ 3.0.1 on the KV260: /dev/mem, O_SYNC, numpy
               uint32 view, needs root). Never downloads or reconfigures anything.
  DevMemMmio   the same mapping without pynq (mmap of /dev/mem with O_SYNC, numpy uint32
               view so that every access is one 32-bit load / store)
  FakeMmio     host/ps_mailbox/fake_mmio.py, a bytearray for tests on the PC
"""

from __future__ import annotations

import gc
import mmap
import os


class MmioError(Exception):
    """refused or failed register access"""


class MmioWindow:
    """offset-checked 32-bit window; subclasses implement _rd / _wr"""

    def __init__(self, size: int):
        self.size = size
        self.reads = 0
        self.writes = 0
        self.closed = False

    def _check(self, off: int) -> None:
        if self.closed:
            raise MmioError("window closed")
        if off < 0 or off + 4 > self.size:
            raise MmioError(f"offset {off:#x} outside the {self.size:#x}-byte window")
        if off & 3:
            raise MmioError(f"unaligned offset {off:#x}")

    def read32(self, off: int) -> int:
        self._check(off)
        self.reads += 1
        return self._rd(off) & 0xFFFFFFFF

    def write32(self, off: int, value: int) -> None:
        self._check(off)
        self.writes += 1
        self._wr(off, value & 0xFFFFFFFF)

    def close(self) -> None:
        self.closed = True

    def _rd(self, off: int) -> int:
        raise NotImplementedError

    def _wr(self, off: int, value: int) -> None:
        raise NotImplementedError


class PynqMmio(MmioWindow):
    """pynq.MMIO backend (imported lazily: the module itself runs without pynq)"""

    def __init__(self, base: int, size: int):
        super().__init__(size)
        from pynq import MMIO          # noqa: PLC0415  (only on the board)
        self.base = base
        self._m = MMIO(base, size)

    def _rd(self, off: int) -> int:
        return self._m.read(off)

    def _wr(self, off: int, value: int) -> None:
        self._m.write(off, value)

    def close(self) -> None:
        # pynq.MMIO keeps a bound method in its own __dict__ (reference cycle): the mapping
        # (numpy view of an mmap of /dev/mem) is only unmapped once the cycle is collected
        self.closed = True
        self._m = None
        gc.collect()


class DevMemMmio(MmioWindow):
    """/dev/mem mapping like pynq's EmbeddedDevice.mmap (root needed)"""

    def __init__(self, base: int, size: int):
        super().__init__(size)
        import numpy as np             # noqa: PLC0415
        if base % mmap.PAGESIZE:
            raise MmioError(f"base {base:#x} not page aligned")
        self.base = base
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            self._mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        finally:
            os.close(fd)
        self._a = np.frombuffer(self._mm, dtype=np.uint32, count=size // 4)

    def _rd(self, off: int) -> int:
        return int(self._a[off >> 2])

    def _wr(self, off: int, value: int) -> None:
        self._a[off >> 2] = value

    def close(self) -> None:
        self.closed = True
        del self._a
        self._mm.close()


def open_window(backend: str, base: int, size: int) -> MmioWindow:
    if backend == "pynq":
        return PynqMmio(base, size)
    if backend == "devmem":
        return DevMemMmio(base, size)
    raise MmioError(f"unknown backend {backend!r} (pynq, devmem)")
