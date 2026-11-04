"""Gesture server source for the ps_e6a bitstream: mailbox results of the RV32I plus the activity of lane 0 after the DSP."""

from __future__ import annotations

import time

from gesture_server.source import HardwareSource, SourceError

from .level import ActivityMeter

BASE, SIZE = 0xA013_0000, 0x1000
ID_VALUE = 0x4553_5331
O_ID, O_GOOD, O_CRC, O_SEQ, O_FRAME, O_OVF, O_CTRL = 0x00, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x30
O_BLOCKS, O_DROP, O_RERR, O_FRAMES, O_GAP, O_OVR = 0x34, 0x38, 0x3C, 0x40, 0x44, 0x48
O_ENERGY, O_LEVEL_SEQ = 0x58, 0x5C
CTRL_RUN, CTRL_SOFT_RESET = 0x1, 0x4
COUNTERS = {"spi_good": O_GOOD, "spi_crc": O_CRC, "spi_seq": O_SEQ, "spi_frame": O_FRAME, "spi_overflow": O_OVF,
            "spi_blocks": O_BLOCKS, "spi_drop": O_DROP, "spi_rate_err": O_RERR, "esi_frames": O_FRAMES, "esi_gap": O_GAP,
            "esi_ovr": O_OVR}


def level_message(seq: int, energy: int, intensity: float, rms: float, calibrated: bool) -> dict:
    return {"schema_version": 1, "type": "level", "level_seq": seq, "energy": energy, "rms": round(rms, 1),
            "intensity": round(intensity, 4), "calibrated": calibrated, "ps_read_monotonic_ns": time.monotonic_ns()}


class LevelReader:
    """new values of the activity counter, separated from the hardware for the tests"""

    def __init__(self, window, meter: ActivityMeter):
        self.w, self.meter = window, meter
        self.last_seq = None

    def poll(self) -> dict | None:
        seq = self.w.read32(O_LEVEL_SEQ)
        if seq == self.last_seq:
            return None
        energy = self.w.read32(O_ENERGY)
        self.last_seq = seq
        x = self.meter.update(energy)
        return level_message(seq, energy, x, self.meter.rms, self.meter.calibrated)


class SpiLiveSource(HardwareSource):
    label = "spi_live"

    def __init__(self, bit, meter: ActivityMeter | None = None, **kw):
        super().__init__(bit, **kw)
        self.meter = meter or ActivityMeter()
        self.src = None
        self.levels = None

    def open(self) -> None:
        super().open()
        try:
            self.src = self.dev._open(BASE, SIZE)
            got = self.src.read32(O_ID)
            if got != ID_VALUE:
                raise SourceError(f"SPI source ID 0x{got:08x}: this is not a ps_e6a bitstream")
            self.src.write32(O_CTRL, CTRL_SOFT_RESET)
            self.src.write32(O_CTRL, CTRL_RUN)
            self.levels = LevelReader(self.src, self.meter)
        except SourceError:
            self._release()
            raise

    def _after_poll(self) -> None:
        m = self.levels.poll()
        if m is not None:
            self._sink(m, {"level": True})

    def _release(self) -> None:
        if self.src is not None:
            try:
                self.src.write32(O_CTRL, 0)
            finally:
                self.src = None
        super()._release()

    def counters(self) -> dict:
        c = super().counters()
        if self.src is not None:
            c.update({k: self.src.read32(o) for k, o in COUNTERS.items()})
        return c
