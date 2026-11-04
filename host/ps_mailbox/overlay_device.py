"""Overlay-aware mailbox device: PYNQ Overlay(download=False) for the hardware metadata,
pynq.MMIO for the shared BRAM, MailboxReader (pynq_reader.py) for the protocol.

    dev = PynqMailboxDevice("mbx_rt2k.bit", loaded_bin="fw/rv32_6a.bit.bin")
    dev.verify()                      # artifacts, board state, Overlay metadata, debug ID
    rd = MailboxReader(dev.open_mailbox(), guard=dev.guard)
    ...
    dev.close()                       # MMIO mappings released (checked in /proc/self/maps)

The PL must already be configured by the board tools (scripts/fpga/kv260_ps_board.py load:
FPGA manager + device tree overlay rv32_6a). Nothing here loads or reconfigures the PL:
pynq.Overlay is only ever constructed with download=False, which in PYNQ 3.0.1 parses the
.hwh next to the .bit (and converts the .bit in memory) without touching the FPGA manager,
/lib/firmware, device tree overlays or the PYNQ global state; verify_overlay() checks that
by comparing those items before and after the constructor.

PYNQ 3.0.1 Overlay.is_loaded() with download=False compares file names only (the Overlay
timestamp stays "" and __init__ has just set device.bitfile_name to this bitstream), so it
is recorded but is no evidence of the PL contents. Evidence used instead, all checked
before the first PL access:
  1. artifacts: sha256 of .bit / .hwh == manifest (one build), every manifest check passed,
     clean commit; the board copy of protocol.py / the config is the one the manifest names
  2. the bitstream PYNQ parsed is the one the FPGA manager loaded: sha256(bit2bin(.bit))
     (PYNQ's parser.bin_data) == sha256 of the firmware file of overlay rv32_6a ==
     manifest bit_bin_sha256
  3. board guard (sysfs): overlay rv32_6a applied, FPGA manager operating
  4. HWH (own XML parse and PYNQ's ip_dict / mem_dict): exactly one axi_bram_ctrl behind
     M_AXI_HPM0_FPD whose base / range equal the config and the manifest; debug window
     kv260_dbg_axil likewise
  5. debug ID register (read once) == config dbg_id
Only the class labels and record layout come from protocol.py; nothing here depends on
Vivado instance names (the mailbox controller is found by its IP type).
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import protocol as P
from .pynq_reader import CONFIG, board_guard, load_config
from .registers import MmioError, MmioWindow, PynqMmio

PKG = Path(__file__).resolve().parent
BRAM_CTRL = "axi_bram_ctrl"
DBG_MODTYPE = "kv260_dbg_axil"
HPM0 = "M_AXI_HPM0_FPD"
PYNQ_STATE = ("global_pl_state.json", "_current_metadata.pkl")   # in pynq/pl_server
DBG_ID_OFF = 0x00
DBG_SIZE = 0x40000                     # kv260_dbg_axil window (256 KB, kv260_ps_board.py)


class DeviceError(Exception):
    """hardware / metadata not verified: do not access the PL"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def bit_header(bit: bytes) -> tuple[dict, bytes]:
    """.bit header fields (a design, b part, c date, d time) and the configuration data
    (as pynq.pl_server.embedded_device.parse_bit_header)"""
    off = 2 + struct.unpack(">h", bit[0:2])[0] + 2
    hdr = {}
    while True:
        d = bit[off]
        off += 1
        if d == 0x65:
            n = struct.unpack(">i", bit[off:off + 4])[0]
            return hdr, bit[off + 4:off + 4 + n]
        n = struct.unpack(">h", bit[off:off + 2])[0]
        hdr[chr(d)] = bit[off + 2:off + 2 + n].decode("ascii")[:-1]
        off += 2 + n


def bit2bin(bit: bytes) -> bytes:
    """.bit -> FPGA manager .bin: data with every 32-bit word byte-swapped (as pynq's
    bit2bin; equal to bootgen -process_bitstream bin)"""
    data = bit_header(bit)[1]
    out = bytearray(data)
    out[0::4], out[1::4], out[2::4], out[3::4] = data[3::4], data[2::4], data[1::4], data[0::4]
    return bytes(out)


def hwh_ranges(hwh: Path) -> list[dict]:
    """address ranges of the PS master M_AXI_HPM0_FPD from the HWH (module type per instance)"""
    root = ET.parse(hwh).getroot()
    mods = {m.get("INSTANCE"): m for m in root.iter("MODULE")}
    out = []
    for m in mods.values():
        for r in m.iter("MEMRANGE"):
            if r.get("MASTERBUSINTERFACE") != HPM0:
                continue
            inst = r.get("INSTANCE")
            base, high = int(r.get("BASEVALUE"), 16), int(r.get("HIGHVALUE"), 16)
            tgt = mods.get(inst)
            out.append({"instance": inst, "modtype": tgt.get("MODTYPE") if tgt is not None else None,
                        "vlnv": tgt.get("VLNV") if tgt is not None else None, "base": base,
                        "size": high - base + 1, "memtype": r.get("MEMTYPE"), "slave": r.get("SLAVEBUSINTERFACE")})
    return out


def _pynq_entries(ol, instance: str) -> list[dict]:
    """ip_dict / mem_dict entries of `instance` as {dict, key, base, size, type}"""
    out = []
    for dname in ("ip_dict", "mem_dict"):
        d = getattr(ol, dname, None) or {}
        for k, v in d.items():
            if not isinstance(v, dict):
                continue
            path = str(v.get("fullpath", k))
            if k.split("/")[0] != instance and path.split("/")[0] != instance:
                continue
            base = v.get("phys_addr", v.get("base_address"))
            size = v.get("addr_range", v.get("size"))
            out.append({"dict": dname, "key": k, "base": base, "size": size,
                        "type": v.get("type"), "used": v.get("used")})
    return out


def _maps_devmem() -> int:
    """number of /dev/mem mappings of this process"""
    try:
        return sum(1 for line in Path("/proc/self/maps").read_text().splitlines() if line.endswith(" /dev/mem"))
    except OSError:
        return -1


class PynqMailboxDevice:
    def __init__(self, bitfile, manifest=None, config=None, loaded_bin=None, sysfs_root: str = "/",
                 require_clean: bool = True, overlay_factory=None, mmio_factory=None, pynq_state_dir=None):
        self.bitfile = Path(bitfile).resolve()
        self.hwh = self.bitfile.with_suffix(".hwh")
        self.manifest_path = Path(manifest) if manifest else self.bitfile.with_suffix(".manifest.json")
        self.config_path = Path(config) if config else CONFIG
        self.cfg = load_config(self.config_path)
        self.loaded_bin = Path(loaded_bin) if loaded_bin else None
        self.sysfs_root = Path(sysfs_root)
        self.guard = board_guard(self.cfg, sysfs_root)
        self.require_clean = require_clean
        self._overlay_factory = overlay_factory
        self._mmio_factory = mmio_factory or PynqMmio
        self._pynq_state_dir = pynq_state_dir
        self.ol = None
        self.windows: list[MmioWindow] = []
        self.mbx = None                         # (base, size) once verified
        self.dbg = None
        self.report: dict = {"bitfile": str(self.bitfile), "hwh": str(self.hwh), "manifest": str(self.manifest_path)}
        self.verified = False

    # ------------------------------------------------------------ artifacts
    def verify_artifacts(self) -> None:
        if not self.bitfile.is_file() or not self.hwh.is_file() or not self.manifest_path.is_file():
            raise DeviceError(f"missing {[str(p) for p in (self.bitfile, self.hwh, self.manifest_path) if not p.is_file()]}")
        man = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.manifest = man
        bad = [c["name"] for c in man.get("checks", []) if not c["ok"]]
        if bad or not man.get("checks"):
            raise DeviceError(f"manifest checks failed: {bad or 'none present'}")
        git = man.get("git", {})
        if self.require_clean and git.get("dirty", True):
            raise DeviceError("manifest: bitstream built from a dirty work tree")
        bit = self.bitfile.read_bytes()
        hdr = bit_header(bit)[0]
        got = {"bit_sha256": hashlib.sha256(bit).hexdigest(), "hwh_sha256": sha256_file(self.hwh),
               "bit2bin_sha256": hashlib.sha256(bit2bin(bit)).hexdigest(),
               "protocol.py_sha256": sha256_file(PKG / "protocol.py"),
               "config_sha256": sha256_file(self.config_path)}
        want = {"bit_sha256": man["bitstream"]["bit_sha256"], "hwh_sha256": man["mailbox"]["hwh_sha256"],
                "bit2bin_sha256": man["bitstream"]["bit_bin_sha256"],
                "protocol.py_sha256": man["mailbox"]["protocol.py_sha256"],
                "config_sha256": man["mailbox"]["kv260_mailbox.json_sha256"]}
        diff = [k for k in want if got[k] != want[k]]
        if diff:
            raise DeviceError(f"artifacts differ from the manifest: {diff}")
        if int(man["mailbox"]["ps_base"], 16) != int(self.cfg["ps_base"], 16):
            raise DeviceError(f"manifest ps_base {man['mailbox']['ps_base']} != config {self.cfg['ps_base']}")
        hwh_ts = ET.parse(self.hwh).getroot().get("TIMESTAMP")
        self.report["artifacts"] = {"commit": git.get("commit"), "dirty": git.get("dirty"),
                                    "manifest_checks": len(man["checks"]), "bit_header": hdr,
                                    "hwh_timestamp": hwh_ts, **got}

    # ------------------------------------------------------------ board state
    def verify_board(self) -> None:
        bad = self.guard()
        if bad:
            raise DeviceError(f"board: {bad}")
        st = {}
        if self.loaded_bin is not None:
            st["loaded_bin"] = str(self.loaded_bin)
            st["loaded_bin_sha256"] = sha256_file(self.loaded_bin)
            if st["loaded_bin_sha256"] != self.manifest["bitstream"]["bit_bin_sha256"]:
                raise DeviceError("the FPGA manager firmware file is not this bitstream")
        st["fpga_state"] = self._sysfs(self.cfg["board"]["fpga_manager"] + "/state")
        st["fpga_flags"] = self._sysfs(self.cfg["board"]["fpga_manager"] + "/flags")
        st["overlay_status"] = self._sysfs("sys/kernel/config/device-tree/overlays/" + self.cfg["board"]["overlay"] + "/status")
        self.report["board"] = st

    def _sysfs(self, rel: str) -> str:
        try:
            return (self.sysfs_root / rel.lstrip("/")).read_text().strip()
        except OSError as e:
            return f"<{e.strerror}>"

    # ------------------------------------------------------------ PYNQ overlay metadata
    def _witness(self) -> dict:
        """items a PL download would change: FPGA manager, overlays, /lib/firmware, PYNQ state"""
        ovl = self.sysfs_root / "sys/kernel/config/device-tree/overlays"
        fw = Path("/lib/firmware")
        w = {"fpga_state": self._sysfs(self.cfg["board"]["fpga_manager"] + "/state"),
             "fpga_flags": self._sysfs(self.cfg["board"]["fpga_manager"] + "/flags"),
             "overlays": sorted(p.name for p in ovl.iterdir()) if ovl.is_dir() else [],
             "lib_firmware": sorted((p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in fw.iterdir()
                                    if p.is_file()) if fw.is_dir() else []}
        sd = Path(self._pynq_state_dir) if self._pynq_state_dir else None
        if sd is not None:
            w["pynq_state"] = {n: sha256_file(sd / n) if (sd / n).is_file() else None for n in PYNQ_STATE}
        return w

    def verify_overlay(self) -> None:
        if self._overlay_factory is None:
            os.environ.setdefault("XILINX_XRT", "/usr")  # as /etc/profile.d/pynq_venv.sh
            import pynq                                  # noqa: PLC0415 (board only)
            self._pynq_state_dir = Path(pynq.__file__).parent / "pl_server"
            self.report["pynq_version"] = pynq.__version__
            factory = lambda path: pynq.Overlay(path, download=False)   # noqa: E731
        else:
            factory = self._overlay_factory
        before = self._witness()
        t0 = time.monotonic()
        self.ol = factory(str(self.bitfile))
        t1 = time.monotonic()
        after = self._witness()
        if before != after:
            raise DeviceError(f"Overlay(download=False) changed the system: {before} -> {after}")
        ol = self.ol
        rep = {"construct_s": round(t1 - t0, 3), "download": False, "unchanged": sorted(before),
               "is_loaded": bool(ol.is_loaded()), "overlay_timestamp": getattr(ol, "timestamp", None),
               "device_bitfile_name": getattr(getattr(ol, "device", None), "bitfile_name", None),
               "ip_dict_keys": sorted(getattr(ol, "ip_dict", {}) or {}),
               "mem_dict_keys": sorted(getattr(ol, "mem_dict", {}) or {})}
        parser = getattr(ol, "parser", None)
        bin_data = getattr(parser, "bin_data", None)
        rep["pynq_bin_data_sha256"] = hashlib.sha256(bin_data).hexdigest() if bin_data else None
        if bin_data and rep["pynq_bin_data_sha256"] != self.manifest["bitstream"]["bit_bin_sha256"]:
            raise DeviceError("PYNQ's bin data of the .bit differs from the loaded bitstream")
        # HWH (own parse): the mailbox controller by IP type, the debug window by module type
        rngs = hwh_ranges(self.hwh)
        rep["hwh_hpm0_ranges"] = [{**r, "base": f"0x{r['base']:08X}", "size": f"0x{r['size']:X}"} for r in rngs]
        mb = [r for r in rngs if r["modtype"] == BRAM_CTRL]
        db = [r for r in rngs if r["modtype"] == DBG_MODTYPE]
        if len(mb) != 1 or len(db) != 1:
            raise DeviceError(f"HWH: {len(mb)} {BRAM_CTRL} / {len(db)} {DBG_MODTYPE} ranges on {HPM0} (want 1 / 1)")
        want_m = (int(self.cfg["ps_base"], 16), self.cfg["window_size"])
        want_d = (int(self.cfg["board"]["dbg_base"], 16), DBG_SIZE)
        if (mb[0]["base"], mb[0]["size"]) != want_m or mb[0]["memtype"] != "MEMORY":
            raise DeviceError(f"HWH mailbox range {mb[0]} != config {want_m}")
        if (db[0]["base"], db[0]["size"]) != want_d:
            raise DeviceError(f"HWH debug range {db[0]} != 0x{want_d[0]:08X} + 0x{want_d[1]:X}")
        # PYNQ's view of the same instances
        for tag, r in (("mailbox", mb[0]), ("debug", db[0])):
            ents = _pynq_entries(ol, r["instance"])
            rep[f"pynq_{tag}_entries"] = ents
            if not any(e["base"] is not None for e in ents):
                raise DeviceError(f"PYNQ ip_dict / mem_dict have no address for {r['instance']}")
            for e in ents:
                if e["base"] is not None and int(e["base"]) != r["base"] or \
                        e["size"] is not None and int(e["size"]) != r["size"]:
                    raise DeviceError(f"PYNQ entry {e} != HWH {r}")
        self.mbx = (mb[0]["base"], mb[0]["size"])
        self.dbg = (db[0]["base"], db[0]["size"])
        rep["mailbox"] = {"instance": mb[0]["instance"], "vlnv": mb[0]["vlnv"], "base": f"0x{self.mbx[0]:08X}",
                          "size": self.mbx[1]}
        rep["debug"] = {"instance": db[0]["instance"], "base": f"0x{self.dbg[0]:08X}", "size": self.dbg[1]}
        self.report["overlay"] = rep

    # ------------------------------------------------------------ windows
    def _open(self, base: int, size: int) -> MmioWindow:
        bad = self.guard()
        if bad:
            raise DeviceError(f"board: {bad}")
        w = self._mmio_factory(base, size)
        self.windows.append(w)
        return w

    def check_debug_id(self) -> int:
        """one read of the kv260_dbg_axil ID register (the design in the PL is ours)"""
        w = self._open(*self.dbg)
        try:
            v = w.read32(DBG_ID_OFF)
        finally:
            self._close_window(w)
        self.report["debug_id"] = f"0x{v:08X}"
        if v != int(self.cfg["board"]["dbg_id"], 16):
            raise DeviceError(f"debug ID 0x{v:08X} != {self.cfg['board']['dbg_id']}")
        return v

    def verify(self) -> dict:
        self.verify_artifacts()
        self.verify_board()
        self.verify_overlay()
        self.check_debug_id()
        self.verified = True
        return self.report

    def open_mailbox(self) -> MmioWindow:
        if not self.verified:
            raise DeviceError("verify() first")
        w = self._open(*self.mbx)
        self.report["backend"] = f"{type(w).__module__}.{type(w).__name__} ({type(getattr(w, '_m', w)).__module__})"
        return w

    def open_debug(self) -> MmioWindow:
        if not self.verified:
            raise DeviceError("verify() first")
        return self._open(*self.dbg)

    def probe_header(self, w: MmioWindow) -> dict:
        """first contact: each identification word read once, no retries; DeviceError unless
        they describe mailbox v1"""
        names = ("MAGIC", "VERSION", "RING_SIZE", "RECORD_SIZE", "RING_OFFSET", "STATE", "PRODUCER_INDEX",
                 "RESET_EPOCH")
        try:
            v = {n: w.read32(P.hoff(n)) for n in names}
        except MmioError as e:
            raise DeviceError(f"first MMIO read failed: {e}") from e
        self.report["probe"] = {n: f"0x{x:08X}" for n, x in v.items()}
        if v["MAGIC"] != P.MAGIC or v["VERSION"] >> 16 != P.VERSION_MAJOR or \
                (v["RING_SIZE"], v["RECORD_SIZE"], v["RING_OFFSET"]) != (P.RING_SIZE, P.RECORD_SIZE, P.RING_OFFSET) or \
                v["STATE"] not in P.STATE_NAMES:
            raise DeviceError(f"not a mailbox v1 header: {self.report['probe']}")
        return v

    def _close_window(self, w: MmioWindow) -> None:
        w.close()
        if w in self.windows:
            self.windows.remove(w)

    def close(self) -> dict:
        for w in list(self.windows):
            self._close_window(w)
        self.ol = None
        gc.collect()          # pynq.MMIO / Overlay objects hold reference cycles
        self.report["devmem_mappings_after_close"] = _maps_devmem()
        return self.report
