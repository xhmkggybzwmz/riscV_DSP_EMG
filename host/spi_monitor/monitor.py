from dataclasses import dataclass

BASE = 0xA013_0000
SIZE = 0x1000
ID = 0x454D4731


@dataclass(frozen=True)
class Snapshot:
    status: int
    good_frames: int
    crc_errors: int
    seq_errors: int
    frame_errors: int
    overflow_errors: int
    last_index: int
    expected_index: int
    sample_rate: int


class SpiMonitor:
    def __init__(self, mmio):
        self.mmio = mmio

    def verify(self):
        got = self.mmio.read(0x00)
        if got != ID:
            raise RuntimeError(f"SPI monitor ID 0x{got:08x}, expected 0x{ID:08x}")

    def clear(self):
        self.mmio.write(0x04, 1)

    def snapshot(self):
        return Snapshot(
            status=self.mmio.read(0x08),
            good_frames=self.mmio.read(0x0C),
            crc_errors=self.mmio.read(0x10),
            seq_errors=self.mmio.read(0x14),
            frame_errors=self.mmio.read(0x18),
            overflow_errors=self.mmio.read(0x1C),
            last_index=self.mmio.read(0x20),
            expected_index=self.mmio.read(0x24),
            sample_rate=self.mmio.read(0x28),
        )

    def samples(self):
        return [self.mmio.read(0x100 + 4 * i) & 0xFFFF for i in range(128)]

    def frame(self, retries=5):
        """Return one coherent latest frame while the PL keeps receiving."""
        for _ in range(retries):
            before = self.mmio.read(0x0C)
            samples = self.samples()
            after = self.mmio.read(0x0C)
            if before == after:
                return self.mmio.read(0x20), samples
        raise RuntimeError("SPI sample bank changed during every read attempt")
