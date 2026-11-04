"""Overlay-aware device for the feeder: the checks of ps_mailbox.overlay_device plus the DMA path.

PynqMailboxDevice.verify() (artifacts, board state, Overlay(download=False) metadata, debug ID) runs first. Then,
before any access to the new windows: the HWH has exactly one AXI DMA and one esi_dma_src_axil page at the addresses
of registers.py, the DMA is MM2S simple mode and as wide as S_AXI_HP0_FPD, and the PS-side width of that port (AFIFM2
RDCTRL, read only) equals the width in the design: a load from Linux does not set it, and a mismatch corrupts data
without any error.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ps_mailbox.overlay_device import DeviceError, PynqMailboxDevice, hwh_ranges

from . import registers as R

AXI_DMA = "axi_dma"
SRC_MODTYPE = "emg_e5_subsys"
AFI_RDCTRL = 0x00
FABRIC_WIDTH_BITS = {0: 128, 1: 64, 2: 32}         # AFIFM RDCTRL FABRIC_WIDTH (see psu_init of this design)


def hwh_dma_info(hwh) -> dict:
    """AXI DMA parameters and the S_AXI_HP0_FPD data width from the HWH"""
    root = ET.parse(hwh).getroot()
    out = {}
    for m in root.iter("MODULE"):
        params = {p.get("NAME"): p.get("VALUE") for p in m.iter("PARAMETER")}
        if m.get("MODTYPE") == AXI_DMA:
            out["dma_instance"] = m.get("INSTANCE")
            out["mm2s_data_width"] = int(params.get("C_M_AXI_MM2S_DATA_WIDTH", 0))
            out["mm2s_stream_width"] = int(params.get("C_M_AXIS_MM2S_TDATA_WIDTH", 0))
            out["addr_width"] = int(params.get("C_ADDR_WIDTH", 0))
            out["sg"] = int(params.get("C_INCLUDE_SG", -1))
            out["s2mm"] = int(params.get("C_INCLUDE_S2MM", -1))
        if m.get("MODTYPE") == "zynq_ultra_ps_e":
            out["hp0_data_width"] = int(params.get("C_SAXIGP2_DATA_WIDTH", params.get("PSU__SAXIGP2__DATA_WIDTH", 0)))
    return out


class PynqDmaDevice(PynqMailboxDevice):
    def verify(self) -> dict:
        super().verify()
        self.verify_dma_path()
        return self.report

    def verify_dma_path(self) -> None:
        rngs = hwh_ranges(self.hwh)
        dma = [r for r in rngs if r["modtype"] == AXI_DMA]
        src = [r for r in rngs if r["modtype"] == SRC_MODTYPE]
        if len(dma) != 1 or len(src) != 1:
            raise DeviceError(f"HWH: {len(dma)} {AXI_DMA} / {len(src)} {SRC_MODTYPE} ranges (want 1 / 1): not a DMA design")
        if (dma[0]["base"], dma[0]["size"]) != (R.DMA_BASE, R.DMA_SIZE):
            raise DeviceError(f"HWH DMA range {dma[0]} != 0x{R.DMA_BASE:08X} + 0x{R.DMA_SIZE:X}")
        if (src[0]["base"], src[0]["size"]) != (R.SRC_BASE, R.SRC_SIZE):
            raise DeviceError(f"HWH source range {src[0]} != 0x{R.SRC_BASE:08X} + 0x{R.SRC_SIZE:X}")
        info = hwh_dma_info(self.hwh)
        if info.get("sg") != 0 or info.get("s2mm") != 0:
            raise DeviceError(f"AXI DMA is not MM2S simple mode: {info}")
        if info.get("mm2s_data_width") != info.get("hp0_data_width") or info.get("mm2s_stream_width") != 64:
            raise DeviceError(f"AXI DMA / HP0 widths: {info}")
        afi = self._open(R.AFI_HP0_BASE, R.AFI_SIZE)
        try:
            rd = afi.read32(AFI_RDCTRL)
        finally:
            self._close_window(afi)
        width = FABRIC_WIDTH_BITS.get(rd & 3)
        info["afi_rdctrl"] = f"0x{rd:08X}"
        info["afi_width"] = width
        self.report["dma_path"] = info
        if width != info["hp0_data_width"]:
            raise DeviceError(f"S_AXI_HP0_FPD: PS side {width} bit (AFIFM2 RDCTRL 0x{rd:08X}), design "
                              f"{info['hp0_data_width']} bit: data would be corrupted")

    def open_dma(self):
        if not self.verified:
            raise DeviceError("verify() first")
        return self._open(R.DMA_BASE, R.DMA_SIZE)

    def open_source(self):
        if not self.verified:
            raise DeviceError("verify() first")
        return self._open(R.SRC_BASE, R.SRC_SIZE)
