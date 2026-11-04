#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC = b"EMG1"
PROTOCOL_VERSION = 1
HEADER_SIZE = 20
CRC_SIZE = 2

FORMAT_U16_LE = 0x01
FORMAT_S16_LE = 0x02
FORMAT_S24_LE = 0x03
FORMAT_S32_LE = 0x04

FORMAT_NAMES = {
    FORMAT_U16_LE: "U16_LE",
    FORMAT_S16_LE: "S16_LE",
    FORMAT_S24_LE: "S24_LE",
    FORMAT_S32_LE: "S32_LE",
}

BYTES_PER_SAMPLE = {
    FORMAT_U16_LE: 2,
    FORMAT_S16_LE: 2,
    FORMAT_S24_LE: 3,
    FORMAT_S32_LE: 4,
}

HEADER_REST_STRUCT = struct.Struct("<BBBBIIHH")


@dataclass
class ParsedPacket:
    version: int
    sample_format: int
    channel_count: int
    reserved: int
    sample_rate_hz: int
    sample_index: int
    samples_per_channel: int
    payload_bytes: int
    payload: bytes
    crc16: int
    file_offset: int


@dataclass
class ParseResult:
    packets: list[ParsedPacket]
    bad_crc_packets: int
    bad_headers: int
    resync_bytes: int
    trailing_bytes: int
    discontinuities: list[tuple[int, int, int]]
    total_bytes: int


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def header_is_sane(version, sample_format, channel_count, sample_rate_hz,
                   samples_per_channel, payload_bytes) -> bool:
    if version != PROTOCOL_VERSION:
        return False
    if sample_format not in BYTES_PER_SAMPLE:
        return False
    if channel_count <= 0 or channel_count > 64:
        return False
    if sample_rate_hz <= 0 or sample_rate_hz > 1_000_000:
        return False
    if samples_per_channel <= 0:
        return False

    expected = (
        samples_per_channel
        * channel_count
        * BYTES_PER_SAMPLE[sample_format]
    )
    return payload_bytes == expected


def parse_file(path: Path) -> ParseResult:
    data = path.read_bytes()
    total_bytes = len(data)

    packets = []
    bad_crc_packets = 0
    bad_headers = 0
    resync_bytes = 0
    discontinuities = []

    pos = 0
    expected_index = None

    while pos + HEADER_SIZE + CRC_SIZE <= total_bytes:
        if data[pos:pos + 4] != MAGIC:
            next_pos = data.find(MAGIC, pos + 1)
            if next_pos < 0:
                break
            resync_bytes += next_pos - pos
            pos = next_pos
            continue

        if pos + HEADER_SIZE > total_bytes:
            break

        rest = data[pos + 4:pos + HEADER_SIZE]
        (
            version,
            sample_format,
            channel_count,
            reserved,
            sample_rate_hz,
            sample_index,
            samples_per_channel,
            payload_bytes,
        ) = HEADER_REST_STRUCT.unpack(rest)

        if not header_is_sane(
            version,
            sample_format,
            channel_count,
            sample_rate_hz,
            samples_per_channel,
            payload_bytes,
        ):
            bad_headers += 1
            pos += 1
            continue

        packet_size = HEADER_SIZE + payload_bytes + CRC_SIZE
        if pos + packet_size > total_bytes:
            break

        header = data[pos:pos + HEADER_SIZE]
        payload = data[pos + HEADER_SIZE:pos + HEADER_SIZE + payload_bytes]
        crc_bytes = data[
            pos + HEADER_SIZE + payload_bytes:
            pos + packet_size
        ]

        received_crc = int.from_bytes(crc_bytes, "little")
        calculated_crc = crc16_ccitt_false(header + payload)

        if received_crc != calculated_crc:
            bad_crc_packets += 1
            pos += 1
            continue

        packet = ParsedPacket(
            version=version,
            sample_format=sample_format,
            channel_count=channel_count,
            reserved=reserved,
            sample_rate_hz=sample_rate_hz,
            sample_index=sample_index,
            samples_per_channel=samples_per_channel,
            payload_bytes=payload_bytes,
            payload=payload,
            crc16=received_crc,
            file_offset=pos,
        )

        if expected_index is not None and sample_index != expected_index:
            discontinuities.append(
                (len(packets), expected_index, sample_index)
            )

        expected_index = (
            sample_index + samples_per_channel
        ) & 0xFFFFFFFF

        packets.append(packet)
        pos += packet_size

    trailing_bytes = total_bytes - pos

    return ParseResult(
        packets=packets,
        bad_crc_packets=bad_crc_packets,
        bad_headers=bad_headers,
        resync_bytes=resync_bytes,
        trailing_bytes=trailing_bytes,
        discontinuities=discontinuities,
        total_bytes=total_bytes,
    )


def decode_payload(packet: ParsedPacket) -> np.ndarray:
    scalar_count = packet.samples_per_channel * packet.channel_count

    if packet.sample_format == FORMAT_U16_LE:
        values = np.frombuffer(packet.payload, dtype="<u2").astype(np.int64)

    elif packet.sample_format == FORMAT_S16_LE:
        values = np.frombuffer(packet.payload, dtype="<i2").astype(np.int64)

    elif packet.sample_format == FORMAT_S32_LE:
        values = np.frombuffer(packet.payload, dtype="<i4").astype(np.int64)

    elif packet.sample_format == FORMAT_S24_LE:
        raw = np.frombuffer(packet.payload, dtype=np.uint8).reshape(-1, 3)
        values = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        sign_bit = 1 << 23
        values = ((values ^ sign_bit) - sign_bit).astype(np.int64)

    else:
        raise ValueError(
            f"Unsupported sample format 0x{packet.sample_format:02X}"
        )

    if values.size != scalar_count:
        raise ValueError(
            f"Decoded scalar count {values.size} != expected {scalar_count}"
        )

    return values.reshape(
        packet.samples_per_channel,
        packet.channel_count,
    )


def decode_recording(result: ParseResult):
    if not result.packets:
        raise ValueError("No valid EMG1 packets found")

    first = result.packets[0]
    fs = first.sample_rate_hz
    ch = first.channel_count
    fmt = first.sample_format

    blocks = []
    for i, packet in enumerate(result.packets):
        if (
            packet.sample_rate_hz != fs
            or packet.channel_count != ch
            or packet.sample_format != fmt
        ):
            raise ValueError(
                f"Stream configuration changed at packet {i}"
            )
        blocks.append(decode_payload(packet))

    samples = np.vstack(blocks)

    metadata = {
        "version": first.version,
        "sample_format": first.sample_format,
        "sample_format_name": FORMAT_NAMES[first.sample_format],
        "channel_count": ch,
        "sample_rate_hz": fs,
        "first_sample_index": first.sample_index,
        "samples_per_channel": samples.shape[0],
        "duration_s": samples.shape[0] / fs,
    }
    return samples, metadata


def welch_psd(x: np.ndarray, fs: float, segment_length: int = 4096):
    x = np.asarray(x, dtype=np.float64)
    n = x.size

    if n < 8:
        raise ValueError("Not enough samples for PSD")

    nperseg = min(segment_length, n)
    step = max(1, nperseg // 2)
    window = np.hanning(nperseg)
    window_power = np.sum(window ** 2)

    acc = None
    count = 0

    for start in range(0, n - nperseg + 1, step):
        seg = x[start:start + nperseg]
        seg = seg - np.mean(seg)
        spec = np.fft.rfft(seg * window)
        psd = (np.abs(spec) ** 2) / (fs * window_power)

        if nperseg % 2 == 0:
            if psd.size > 2:
                psd[1:-1] *= 2.0
        else:
            if psd.size > 1:
                psd[1:] *= 2.0

        acc = psd if acc is None else acc + psd
        count += 1

    if count == 0:
        seg = x - np.mean(x)
        window = np.hanning(n)
        spec = np.fft.rfft(seg * window)
        acc = (np.abs(spec) ** 2) / (fs * np.sum(window ** 2))
        count = 1
        nperseg = n

    freqs = np.fft.rfftfreq(nperseg, d=1.0 / fs)
    return freqs, acc / count


def line_prominence_db(freqs, psd, target_hz: float) -> float:
    if target_hz <= 0 or target_hz >= freqs[-1]:
        return float("nan")

    target_mask = np.abs(freqs - target_hz) <= 1.0
    local_mask = (
        (freqs >= target_hz - 10.0)
        & (freqs <= target_hz + 10.0)
        & (np.abs(freqs - target_hz) >= 3.0)
    )

    if not np.any(target_mask) or not np.any(local_mask):
        return float("nan")

    target_power = float(np.mean(psd[target_mask]))
    local_floor = float(np.median(psd[local_mask]))

    eps = np.finfo(float).tiny
    return 10.0 * math.log10((target_power + eps) / (local_floor + eps))


def dominant_frequencies(freqs, psd, fmin=20.0, fmax=500.0,
                         count=5, min_separation_hz=3.0):
    mask = (freqs >= fmin) & (freqs <= min(fmax, freqs[-1]))
    idx = np.where(mask)[0]
    if idx.size == 0:
        return []

    order = idx[np.argsort(psd[idx])[::-1]]
    selected = []
    ref = np.median(psd[idx])
    eps = np.finfo(float).tiny

    for i in order:
        f = float(freqs[i])
        if any(abs(f - old_f) < min_separation_hz for old_f, _ in selected):
            continue

        prominence = 10.0 * math.log10(
            (float(psd[i]) + eps) / (float(ref) + eps)
        )
        selected.append((f, prominence))

        if len(selected) >= count:
            break

    return selected


def adc_limits(sample_format: int, adc_bits: int):
    if adc_bits <= 0 or adc_bits > 32:
        return None

    if sample_format == FORMAT_U16_LE:
        return 0, (1 << adc_bits) - 1

    if sample_format in (FORMAT_S16_LE, FORMAT_S24_LE, FORMAT_S32_LE):
        return -(1 << (adc_bits - 1)), (1 << (adc_bits - 1)) - 1

    return None


def per_channel_stats(samples, sample_format, adc_bits, clip_margin):
    limits = adc_limits(sample_format, adc_bits)
    stats = []

    for ch in range(samples.shape[1]):
        x = samples[:, ch].astype(np.float64)
        mean = float(np.mean(x))
        median = float(np.median(x))
        std = float(np.std(x))
        centered_rms = float(np.sqrt(np.mean((x - mean) ** 2)))

        item = {
            "channel": ch,
            "min": int(np.min(x)),
            "max": int(np.max(x)),
            "mean": mean,
            "median": median,
            "std": std,
            "centered_rms": centered_rms,
            "peak_to_peak": int(np.max(x) - np.min(x)),
            "p01": float(np.percentile(x, 1)),
            "p99": float(np.percentile(x, 99)),
            "clip_low_count": None,
            "clip_high_count": None,
            "clip_fraction": None,
        }

        if limits is not None:
            lo, hi = limits
            low_count = int(np.sum(x <= lo + clip_margin))
            high_count = int(np.sum(x >= hi - clip_margin))
            item["clip_low_count"] = low_count
            item["clip_high_count"] = high_count
            item["clip_fraction"] = (low_count + high_count) / x.size

        stats.append(item)

    return stats


def save_waveform_plot(path, samples, fs, title, max_points):
    import matplotlib.pyplot as plt

    n = samples.shape[0]
    stride = max(1, math.ceil(n / max_points))

    y = samples[::stride]
    t = np.arange(y.shape[0], dtype=np.float64) * stride / fs

    fig, ax = plt.subplots(figsize=(13, 5))
    for ch in range(y.shape[1]):
        ax.plot(t, y[:, ch], linewidth=0.8, label=f"CH{ch}")

    ax.set_title(title)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("ADC sample")
    ax.grid(True)

    if y.shape[1] > 1:
        ax.legend()

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def save_psd_plot(path, samples, fs, title, max_frequency):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(13, 5))

    for ch in range(samples.shape[1]):
        freqs, psd = welch_psd(samples[:, ch], fs)
        psd_db = 10.0 * np.log10(
            np.maximum(psd, np.finfo(float).tiny)
        )
        mask = freqs <= min(max_frequency, fs / 2.0)
        ax.plot(
            freqs[mask],
            psd_db[mask],
            linewidth=0.9,
            label=f"CH{ch}",
        )

    ax.set_title(title)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("PSD (dB/Hz)")
    ax.grid(True)

    if samples.shape[1] > 1:
        ax.legend()

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def export_csv(path, samples, fs, first_sample_index):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["sample_index", "time_s"]
            + [f"ch{ch}" for ch in range(samples.shape[1])]
        )

        for i in range(samples.shape[0]):
            writer.writerow(
                [first_sample_index + i, i / fs]
                + [int(v) for v in samples[i]]
            )


def print_report(file_path, result, samples, meta, stats,
                 spectral, adc_bits):
    print("=" * 78)
    print(f"File: {file_path}")
    print("=" * 78)

    print("Packet / protocol")
    print(f"  file size             : {result.total_bytes} bytes")
    print(f"  valid packets         : {len(result.packets)}")
    print(f"  CRC failures          : {result.bad_crc_packets}")
    print(f"  bad headers           : {result.bad_headers}")
    print(f"  resync skipped bytes  : {result.resync_bytes}")
    print(f"  trailing bytes        : {result.trailing_bytes}")
    print(f"  discontinuities       : {len(result.discontinuities)}")
    print(f"  protocol version      : {meta['version']}")
    print(f"  sample format         : {meta['sample_format_name']}")
    print(f"  channels              : {meta['channel_count']}")
    print(f"  sample rate/channel   : {meta['sample_rate_hz']} Hz")
    print(f"  samples/channel       : {meta['samples_per_channel']}")
    print(f"  duration              : {meta['duration_s']:.6f} s")
    print(f"  first sample index    : {meta['first_sample_index']}")

    if result.discontinuities:
        print("  first discontinuities :")
        for packet_no, expected, received in result.discontinuities[:5]:
            delta = (received - expected) & 0xFFFFFFFF
            print(
                f"    packet {packet_no}: expected {expected}, "
                f"received {received}, delta {delta}"
            )

    print()
    print(f"Signal statistics (ADC bits assumption: {adc_bits})")

    for s, sp in zip(stats, spectral):
        print(f"  CH{s['channel']}")
        print(f"    min / max           : {s['min']} / {s['max']}")
        print(
            f"    mean / median       : "
            f"{s['mean']:.3f} / {s['median']:.3f}"
        )
        print(
            f"    std / centered RMS  : "
            f"{s['std']:.3f} / {s['centered_rms']:.3f}"
        )
        print(f"    peak-to-peak        : {s['peak_to_peak']}")
        print(
            f"    P01 / P99           : "
            f"{s['p01']:.3f} / {s['p99']:.3f}"
        )

        if s["clip_fraction"] is not None:
            print(
                f"    clipping near rails : "
                f"{s['clip_fraction'] * 100:.6f}% "
                f"(low={s['clip_low_count']}, "
                f"high={s['clip_high_count']})"
            )

        p50 = sp["line50_db"]
        p60 = sp["line60_db"]

        if math.isfinite(p50):
            print(f"    50 Hz prominence    : {p50:.2f} dB")
        else:
            print("    50 Hz prominence    : n/a")

        if math.isfinite(p60):
            print(f"    60 Hz prominence    : {p60:.2f} dB")
        else:
            print("    60 Hz prominence    : n/a")

        if sp["dominant"]:
            text = ", ".join(
                f"{f:.2f} Hz ({prom:.1f} dB)"
                for f, prom in sp["dominant"]
            )
            print(f"    dominant 20-500 Hz  : {text}")


def inspect_one(file_path, out_dir, adc_bits, clip_margin,
                max_plot_points, max_psd_frequency,
                make_plots, make_csv):
    result = parse_file(file_path)

    if not result.packets:
        raise ValueError(
            f"{file_path}: no valid EMG1 packets found"
        )

    samples, meta = decode_recording(result)

    stats = per_channel_stats(
        samples,
        sample_format=meta["sample_format"],
        adc_bits=adc_bits,
        clip_margin=clip_margin,
    )

    spectral = []
    for ch in range(samples.shape[1]):
        freqs, psd = welch_psd(
            samples[:, ch],
            fs=meta["sample_rate_hz"],
        )
        spectral.append(
            {
                "line50_db": line_prominence_db(freqs, psd, 50.0),
                "line60_db": line_prominence_db(freqs, psd, 60.0),
                "dominant": dominant_frequencies(freqs, psd),
            }
        )

    print_report(
        file_path=file_path,
        result=result,
        samples=samples,
        meta=meta,
        stats=stats,
        spectral=spectral,
        adc_bits=adc_bits,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = file_path.stem

    if make_plots:
        waveform_path = out_dir / f"{stem}_waveform.png"
        psd_path = out_dir / f"{stem}_psd.png"

        save_waveform_plot(
            waveform_path,
            samples,
            meta["sample_rate_hz"],
            title=f"{file_path.name} - waveform",
            max_points=max_plot_points,
        )

        save_psd_plot(
            psd_path,
            samples,
            meta["sample_rate_hz"],
            title=f"{file_path.name} - PSD",
            max_frequency=max_psd_frequency,
        )

        print(f"  waveform plot         : {waveform_path}")
        print(f"  PSD plot              : {psd_path}")

    if make_csv:
        csv_path = out_dir / f"{stem}.csv"
        export_csv(
            csv_path,
            samples,
            meta["sample_rate_hz"],
            meta["first_sample_index"],
        )
        print(f"  CSV export            : {csv_path}")

    return {
        "file": str(file_path),
        "duration_s": meta["duration_s"],
        "sample_rate_hz": meta["sample_rate_hz"],
        "channels": meta["channel_count"],
        "sample_format": meta["sample_format_name"],
        "valid_packets": len(result.packets),
        "crc_failures": result.bad_crc_packets,
        "bad_headers": result.bad_headers,
        "discontinuities": len(result.discontinuities),
        "stats": stats,
        "spectral": spectral,
    }


def write_summary_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "file",
            "duration_s",
            "sample_rate_hz",
            "channels",
            "sample_format",
            "valid_packets",
            "crc_failures",
            "bad_headers",
            "discontinuities",
            "channel",
            "min",
            "max",
            "mean",
            "median",
            "std",
            "centered_rms",
            "peak_to_peak",
            "clip_fraction",
            "line50_prominence_db",
            "line60_prominence_db",
        ])

        for row in rows:
            for s, sp in zip(row["stats"], row["spectral"]):
                writer.writerow([
                    row["file"],
                    f"{row['duration_s']:.9f}",
                    row["sample_rate_hz"],
                    row["channels"],
                    row["sample_format"],
                    row["valid_packets"],
                    row["crc_failures"],
                    row["bad_headers"],
                    row["discontinuities"],
                    s["channel"],
                    s["min"],
                    s["max"],
                    f"{s['mean']:.9f}",
                    f"{s['median']:.9f}",
                    f"{s['std']:.9f}",
                    f"{s['centered_rms']:.9f}",
                    s["peak_to_peak"],
                    "" if s["clip_fraction"] is None
                    else f"{s['clip_fraction']:.12g}",
                    f"{sp['line50_db']:.6f}"
                    if math.isfinite(sp["line50_db"]) else "",
                    f"{sp['line60_db']:.6f}"
                    if math.isfinite(sp["line60_db"]) else "",
                ])


def build_parser():
    parser = argparse.ArgumentParser(
        description="Inspect EMG Packet Protocol V1 .emg recordings"
    )

    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="one or more .emg files",
    )

    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("analysis/emg_inspect"),
        help="output directory (default: analysis/emg_inspect)",
    )

    parser.add_argument(
        "--adc-bits",
        type=int,
        default=12,
        help="ADC resolution used for clipping checks (default: 12)",
    )

    parser.add_argument(
        "--clip-margin",
        type=int,
        default=2,
        help="counts from ADC rails treated as clipping (default: 2)",
    )

    parser.add_argument(
        "--max-plot-points",
        type=int,
        default=100_000,
        help="maximum samples drawn in waveform plot (default: 100000)",
    )

    parser.add_argument(
        "--max-psd-frequency",
        type=float,
        default=500.0,
        help="maximum PSD frequency shown in Hz (default: 500)",
    )

    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="do not generate PNG waveform/PSD plots",
    )

    parser.add_argument(
        "--csv",
        action="store_true",
        help="also export every decoded sample to CSV",
    )

    return parser


def main():
    args = build_parser().parse_args()

    rows = []
    failures = 0

    for file_path in args.files:
        try:
            if not file_path.exists():
                raise FileNotFoundError(file_path)

            row = inspect_one(
                file_path=file_path,
                out_dir=args.out_dir,
                adc_bits=args.adc_bits,
                clip_margin=args.clip_margin,
                max_plot_points=args.max_plot_points,
                max_psd_frequency=args.max_psd_frequency,
                make_plots=not args.no_plots,
                make_csv=args.csv,
            )
            rows.append(row)
            print()

        except Exception as exc:
            failures += 1
            print(
                f"[ERROR] {file_path}: {exc}",
                file=sys.stderr,
            )

    if rows:
        summary_path = args.out_dir / "summary.csv"
        write_summary_csv(summary_path, rows)
        print(f"Combined summary: {summary_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
