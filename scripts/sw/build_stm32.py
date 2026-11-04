#!/usr/bin/env python3
"""Build firmware/stm32/emg_acquisition with the compiler inside STM32CubeIDE: no cmake or ninja needed.

    python scripts/sw/build_stm32.py            # build/stm32/test and build/stm32/adc, emg_acquisition.elf in each

Same flags and sources as the project's CMake files (Debug). test = EMG_SOURCE_MODE 1 (12-bit counter, no sensor),
adc = EMG_SOURCE_MODE 0 (PA5), adc_uart = PA5 to USART1 at 460800 (recording without the KV260). Set RISCV_EMG_CUBEIDE to the folder that holds stm32cubeide.exe.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROJ = REPO / "firmware" / "stm32" / "emg_acquisition"
IDE = Path(os.environ.get("RISCV_EMG_CUBEIDE", r"E:\Tools\STM32CubeIDE_1.19.0\STM32CubeIDE"))
CPU = ["-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16", "-mfloat-abi=hard"]
MODES = {"test": ["-DEMG_SOURCE_MODE=1"], "adc": ["-DEMG_SOURCE_MODE=0"],
         "adc_uart": ["-DEMG_SOURCE_MODE=0", "-DEMG_TRANSPORT_MODE=0"]}


REPLAY_DATA = REPO / "build" / "stm32" / "replay_data"
if (REPLAY_DATA / "emg_replay.h").exists():      # scripts/sw/gen_stm32_replay.py: recording in flash, SPI to the KV260
    MODES["replay"] = ["-DEMG_SOURCE_MODE=2", f"-I{REPLAY_DATA}"]


def gcc() -> Path:
    found = sorted((IDE / "plugins").glob("*gnu-tools-for-stm32*win32*/tools/bin/arm-none-eabi-gcc.exe"))
    if not found:
        sys.exit(f"arm-none-eabi-gcc not found under {IDE}: set RISCV_EMG_CUBEIDE")
    return found[-1]


def sources() -> list[Path]:
    text = (PROJ / "cmake" / "stm32cubemx" / "CMakeLists.txt").read_text(encoding="utf-8")
    rel = re.findall(r"\$\{CMAKE_CURRENT_SOURCE_DIR\}/\.\./\.\./(\S+\.(?:c|s))", text)
    return [PROJ / r for r in rel]


def includes() -> list[str]:
    text = (PROJ / "cmake" / "stm32cubemx" / "CMakeLists.txt").read_text(encoding="utf-8")
    block = text[text.index("set(MX_Include_Dirs"):text.index("# STM32CubeMX generated application sources")]
    return [f"-I{PROJ / r}" for r in re.findall(r"\$\{CMAKE_CURRENT_SOURCE_DIR\}/\.\./\.\./(\S+)", block)]


def compile_one(cc: Path, src: Path, out: Path, mode: list[str]) -> tuple[Path, str, int]:
    obj = out / (src.stem + ".o")
    common = CPU + ["-DUSE_HAL_DRIVER", "-DSTM32F407xx", "-DDEBUG", *mode] + includes()
    if src.suffix == ".s":
        cmd = [str(cc), *common, "-x", "assembler-with-cpp", "-g3", "-c", str(src), "-o", str(obj)]
    else:
        cmd = [str(cc), *common, "-std=gnu11", "-Wall", "-fdata-sections", "-ffunction-sections", "-O0", "-g3",
               "-c", str(src), "-o", str(obj)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return obj, r.stdout + r.stderr, r.returncode


def build(name: str, mode: list[str]) -> int:
    cc = gcc()
    out = REPO / "build" / "stm32" / name
    out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda s: compile_one(cc, s, out, mode), sources()))
    own = [m for _, m, _ in res if m and "Drivers" not in m]
    if own:
        print("".join(own))
    if any(rc for _, _, rc in res):
        print("".join(m for _, m, rc in res if rc))
        print(f"{name}: COMPILE FAILED")
        return 1
    elf = out / "emg_acquisition.elf"
    cmd = [str(cc), *CPU, f"-T{PROJ / 'STM32F407xx_FLASH.ld'}", "--specs=nano.specs", f"-Wl,-Map={out / 'emg.map'}",
           "-Wl,--gc-sections", "-Wl,--print-memory-usage", *[str(o) for o, _, _ in res], "-lm", "-o", str(elf)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout + r.stderr)
    if r.returncode:
        print(f"{name}: LINK FAILED")
        return 1
    print(f"{name}: {' '.join(mode)} -> {elf.relative_to(REPO).as_posix()}")
    return 0


def main() -> int:
    return max(build(n, m) for n, m in MODES.items())


if __name__ == "__main__":
    sys.exit(main())
