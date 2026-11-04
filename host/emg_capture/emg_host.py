#!/usr/bin/env python3
"""EMG Packet Protocol V1 host utility."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import serial

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

# Header bytes after the 4-byte magic:
# version, sample_format, channel_count, reserved,
# sample_rate_hz, sample_index, samples_per_channel, payload_bytes
HEADER_REST_STRUCT = struct.Struct("<BBBBIIHH")


@dataclass(frozen=True)
class EMGPacket:
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
    raw_bytes: bytes

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(
            self.sample_format,
            f"UNKNOWN(0x{self.sample_format:02X})",
        )


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly=0x1021, init=0xFFFF, xorout=0."""
    crc = 0xFFFF

    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return crc


class EMGReader:
    def __init__(self, port: str, baudrate: int, timeout: float = 0.5):
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
        )
        self.ser.reset_input_buffer()

        self.expected_index: int | None = None
        self.good_packets = 0
        self.bad_crc_packets = 0
        self.bad_header_packets = 0
        self.discontinuities = 0

    def close(self) -> None:
        if self.ser.is_open:
            self.ser.close()

    def read_exact(self, count: int) -> bytes:
        data = bytearray()

        while len(data) < count:
            chunk = self.ser.read(count - len(data))
            if not chunk:
                raise TimeoutError("serial receive timeout")
            data.extend(chunk)

        return bytes(data)

    def sync_to_magic(self) -> bytes:
        """Find b'EMG1' anywhere in the serial byte stream."""
        window = bytearray()

        while True:
            window.extend(self.read_exact(1))

            if len(window) > len(MAGIC):
                del window[0]

            if bytes(window) == MAGIC:
                return MAGIC

    def _header_is_sane(
        self,
        version: int,
        sample_format: int,
        channel_count: int,
        sample_rate_hz: int,
        samples_per_channel: int,
        payload_bytes: int,
    ) -> bool:
        if version != PROTOCOL_VERSION:
            return False
        if sample_format not in BYTES_PER_SAMPLE:
            return False
        if channel_count == 0 or channel_count > 64:
            return False
        if sample_rate_hz == 0 or sample_rate_hz > 1_000_000:
            return False
        if samples_per_channel == 0:
            return False

        expected_payload_bytes = (
            samples_per_channel
            * channel_count
            * BYTES_PER_SAMPLE[sample_format]
        )

        return payload_bytes == expected_payload_bytes

    def read_packet(self) -> EMGPacket:
        """Return the next valid packet, re-synchronizing after errors."""
        while True:
            magic = self.sync_to_magic()
            rest = self.read_exact(HEADER_SIZE - len(MAGIC))

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

            if not self._header_is_sane(
                version,
                sample_format,
                channel_count,
                sample_rate_hz,
                samples_per_channel,
                payload_bytes,
            ):
                self.bad_header_packets += 1
                continue

            header = magic + rest
            payload = self.read_exact(payload_bytes)
            crc_bytes = self.read_exact(CRC_SIZE)

            received_crc = int.from_bytes(crc_bytes, "little")
            calculated_crc = crc16_ccitt_false(header + payload)

            if received_crc != calculated_crc:
                self.bad_crc_packets += 1
                continue

            if (
                self.expected_index is not None
                and sample_index != self.expected_index
            ):
                self.discontinuities += 1
                print(
                    "[WARN] sample discontinuity: "
                    f"expected {self.expected_index}, got {sample_index}",
                    file=sys.stderr,
                )

            self.expected_index = (
                sample_index + samples_per_channel
            ) & 0xFFFFFFFF

            self.good_packets += 1

            return EMGPacket(
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
                raw_bytes=header + payload + crc_bytes,
            )


def decode_payload(packet: EMGPacket) -> np.ndarray:
    """
    Return shape (samples_per_channel, channel_count).
    Payload layout is sample-interleaved.
    """
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
            f"unsupported sample format: 0x{packet.sample_format:02X}"
        )

    if values.size != scalar_count:
        raise ValueError(
            f"payload length mismatch: decoded={values.size}, "
            f"expected={scalar_count}"
        )

    return values.reshape(
        packet.samples_per_channel,
        packet.channel_count,
    )


def print_stream_info(packet: EMGPacket) -> None:
    print("EMG stream:")
    print(f"  protocol version      : {packet.version}")
    print(f"  sample format         : {packet.format_name}")
    print(f"  channel count         : {packet.channel_count}")
    print(f"  sample rate/channel   : {packet.sample_rate_hz} Hz")
    print(f"  samples/ch/packet     : {packet.samples_per_channel}")
    print(f"  payload bytes/packet  : {packet.payload_bytes}")


def wait_for_packet(reader: EMGReader) -> EMGPacket:
    while True:
        try:
            return reader.read_packet()
        except TimeoutError:
            print("[WAIT] no complete EMG1 packet yet...", file=sys.stderr)


def verify_stream_unchanged(reference: EMGPacket, packet: EMGPacket) -> None:
    if (
        packet.version != reference.version
        or packet.sample_format != reference.sample_format
        or packet.channel_count != reference.channel_count
        or packet.sample_rate_hz != reference.sample_rate_hz
    ):
        raise RuntimeError(
            "stream configuration changed while running"
        )


def live_view(
    reader: EMGReader,
    window_seconds: float,
    ymin: float | None,
    ymax: float | None,
) -> None:
    import matplotlib.pyplot as plt

    if window_seconds <= 0:
        raise ValueError("--window must be > 0")

    first_packet = wait_for_packet(reader)
    print_stream_info(first_packet)

    fs = first_packet.sample_rate_hz
    channel_count = first_packet.channel_count
    max_samples = max(1, int(round(window_seconds * fs)))

    buffers = [deque(maxlen=max_samples) for _ in range(channel_count)]

    plt.ion()
    fig, ax = plt.subplots()

    lines = []
    for ch in range(channel_count):
        line, = ax.plot([], [], label=f"CH{ch}")
        lines.append(line)

    if channel_count > 1:
        ax.legend(loc="upper right")

    ax.set_title("EMG live view")
    ax.set_xlabel("Time relative to newest sample (s)")
    ax.set_ylabel("ADC sample")
    ax.grid(True)
    ax.set_xlim(-window_seconds, 0.0)

    if ymin is not None or ymax is not None:
        bottom, top = ax.get_ylim()
        ax.set_ylim(
            ymin if ymin is not None else bottom,
            ymax if ymax is not None else top,
        )

    print("Live view started. Press Ctrl+C or close the window to stop.")

    packet = first_packet
    last_draw_time = 0.0

    try:
        while plt.fignum_exists(fig.number):
            verify_stream_unchanged(first_packet, packet)
            block = decode_payload(packet)

            for ch in range(channel_count):
                buffers[ch].extend(block[:, ch].tolist())

            now = time.monotonic()

            # Redrawing more slowly than packet reception keeps the GUI light,
            # while every packet is still consumed from the serial stream.
            if now - last_draw_time >= 0.03:
                current_len = len(buffers[0])

                if current_len:
                    t = (
                        np.arange(current_len, dtype=np.float64)
                        - (current_len - 1)
                    ) / fs

                    for ch, line in enumerate(lines):
                        line.set_data(
                            t,
                            np.asarray(buffers[ch], dtype=np.float64),
                        )

                    ax.set_xlim(-window_seconds, 0.0)

                    if ymin is None and ymax is None:
                        ax.relim()
                        ax.autoscale_view(scalex=False, scaley=True)

                    fig.canvas.draw_idle()
                    plt.pause(0.001)

                last_draw_time = now

            try:
                packet = reader.read_packet()
            except TimeoutError:
                plt.pause(0.01)
                continue

    except KeyboardInterrupt:
        print("\nLive view stopped.")
    finally:
        plt.ioff()
        plt.close(fig)



class WebViewState:
    def __init__(
        self,
        window_seconds: float,
        ymin: float | None,
        ymax: float | None,
    ):
        if window_seconds <= 0:
            raise ValueError("--window must be > 0")

        self.window_seconds = window_seconds
        self.ymin = ymin
        self.ymax = ymax

        self.lock = threading.Lock()
        self.ready = False
        self.error: str | None = None

        self.sample_rate_hz = 0
        self.channel_count = 0
        self.sample_format = ""
        self.samples_per_packet = 0
        self.max_samples = 0

        self.buffers: list[deque[int]] = []

        self.good_packets = 0
        self.bad_crc_packets = 0
        self.bad_header_packets = 0
        self.discontinuities = 0

    def configure(self, packet: EMGPacket) -> None:
        with self.lock:
            if self.ready:
                return

            self.sample_rate_hz = packet.sample_rate_hz
            self.channel_count = packet.channel_count
            self.sample_format = packet.format_name
            self.samples_per_packet = packet.samples_per_channel
            self.max_samples = max(
                1,
                int(round(self.window_seconds * self.sample_rate_hz)),
            )
            self.buffers = [
                deque(maxlen=self.max_samples)
                for _ in range(self.channel_count)
            ]
            self.ready = True

    def append_packet(self, packet: EMGPacket, reader: EMGReader) -> None:
        block = decode_payload(packet)

        with self.lock:
            for ch in range(packet.channel_count):
                self.buffers[ch].extend(
                    int(v) for v in block[:, ch]
                )

            self.good_packets = reader.good_packets
            self.bad_crc_packets = reader.bad_crc_packets
            self.bad_header_packets = reader.bad_header_packets
            self.discontinuities = reader.discontinuities

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error = message

    def snapshot(self, max_points: int = 2000) -> dict:
        with self.lock:
            if not self.ready:
                return {
                    "ready": False,
                    "error": self.error,
                    "stats": {
                        "good_packets": self.good_packets,
                        "bad_crc_packets": self.bad_crc_packets,
                        "bad_header_packets": self.bad_header_packets,
                        "discontinuities": self.discontinuities,
                    },
                }

            if not self.buffers or not self.buffers[0]:
                count = 0
            else:
                count = len(self.buffers[0])

            stride = 1
            if count > max_points:
                stride = math.ceil(count / max_points)

            channels = [
                list(buf)[::stride]
                for buf in self.buffers
            ]

            returned_count = len(channels[0]) if channels else 0

            if returned_count:
                dt = stride / self.sample_rate_hz
                start = -(returned_count - 1) * dt
                times = [
                    start + i * dt
                    for i in range(returned_count)
                ]
            else:
                times = []

            return {
                "ready": True,
                "error": self.error,
                "sample_rate_hz": self.sample_rate_hz,
                "channel_count": self.channel_count,
                "sample_format": self.sample_format,
                "samples_per_packet": self.samples_per_packet,
                "window_seconds": self.window_seconds,
                "ymin": self.ymin,
                "ymax": self.ymax,
                "stride": stride,
                "time": times,
                "channels": channels,
                "stats": {
                    "good_packets": self.good_packets,
                    "bad_crc_packets": self.bad_crc_packets,
                    "bad_header_packets": self.bad_header_packets,
                    "discontinuities": self.discontinuities,
                },
            }


WEB_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EMG Live View</title>
<style>
  :root {
    color-scheme: light dark;
    font-family: system-ui, -apple-system, Segoe UI, sans-serif;
  }
  body {
    margin: 0;
    padding: 20px;
    background: #111827;
    color: #e5e7eb;
  }
  .wrap { max-width: 1400px; margin: 0 auto; }
  h1 { margin: 0 0 14px; font-size: 24px; }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px;
    margin-bottom: 12px;
  }
  .card {
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 10px 12px;
  }
  .label { color: #9ca3af; font-size: 12px; }
  .value { font-size: 18px; margin-top: 2px; }
  .plotbox {
    background: #0b1220;
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 8px;
  }
  canvas {
    width: 100%;
    height: 520px;
    display: block;
  }
  #status {
    margin-top: 10px;
    color: #9ca3af;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  }
  .ok { color: #86efac; }
  .bad { color: #fca5a5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>EMG Live View</h1>
  <div class="cards">
    <div class="card"><div class="label">Sample rate</div><div id="fs" class="value">--</div></div>
    <div class="card"><div class="label">Channels</div><div id="chs" class="value">--</div></div>
    <div class="card"><div class="label">Format</div><div id="fmt" class="value">--</div></div>
    <div class="card"><div class="label">Good packets</div><div id="good" class="value">0</div></div>
    <div class="card"><div class="label">CRC failures</div><div id="crc" class="value">0</div></div>
    <div class="card"><div class="label">Discontinuities</div><div id="disc" class="value">0</div></div>
  </div>
  <div class="plotbox">
    <canvas id="plot"></canvas>
  </div>
  <div id="status">Connecting to server...</div>
</div>

<script>
const canvas = document.getElementById("plot");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");

const palette = [
  "#60a5fa", "#34d399", "#fbbf24", "#f87171",
  "#a78bfa", "#22d3ee", "#fb7185", "#c084fc"
];

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", resizeCanvas);
resizeCanvas();

function niceRange(minv, maxv) {
  if (!Number.isFinite(minv) || !Number.isFinite(maxv)) return [0, 1];
  if (minv === maxv) {
    const pad = Math.max(1, Math.abs(minv) * 0.05);
    return [minv - pad, maxv + pad];
  }
  const pad = (maxv - minv) * 0.08;
  return [minv - pad, maxv + pad];
}

function draw(data) {
  const rect = canvas.getBoundingClientRect();
  const W = rect.width;
  const H = rect.height;

  ctx.clearRect(0, 0, W, H);

  const left = 62, right = 18, top = 20, bottom = 42;
  const pw = Math.max(1, W - left - right);
  const ph = Math.max(1, H - top - bottom);

  const times = data.time || [];
  const channels = data.channels || [];

  let ymin = data.ymin;
  let ymax = data.ymax;

  if (ymin === null || ymax === null) {
    let amin = Infinity, amax = -Infinity;
    for (const ch of channels) {
      for (const v of ch) {
        if (v < amin) amin = v;
        if (v > amax) amax = v;
      }
    }
    const auto = niceRange(amin, amax);
    if (ymin === null) ymin = auto[0];
    if (ymax === null) ymax = auto[1];
  }

  if (!Number.isFinite(ymin) || !Number.isFinite(ymax) || ymin === ymax) {
    ymin = 0; ymax = 1;
  }

  const xmin = -data.window_seconds;
  const xmax = 0;

  const xpix = x => left + ((x - xmin) / (xmax - xmin)) * pw;
  const ypix = y => top + (1 - (y - ymin) / (ymax - ymin)) * ph;

  ctx.strokeStyle = "#374151";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#9ca3af";
  ctx.font = "12px system-ui";

  for (let i = 0; i <= 6; i++) {
    const x = left + (i / 6) * pw;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + ph);
    ctx.stroke();

    const tv = xmin + (i / 6) * (xmax - xmin);
    ctx.fillText(tv.toFixed(1) + " s", x - 16, top + ph + 24);
  }

  for (let i = 0; i <= 5; i++) {
    const y = top + (i / 5) * ph;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + pw, y);
    ctx.stroke();

    const vv = ymax - (i / 5) * (ymax - ymin);
    ctx.fillText(vv.toFixed(0), 6, y + 4);
  }

  for (let ch = 0; ch < channels.length; ch++) {
    const vals = channels[ch];
    if (!vals.length || vals.length !== times.length) continue;

    ctx.strokeStyle = palette[ch % palette.length];
    ctx.lineWidth = 1.4;
    ctx.beginPath();

    for (let i = 0; i < vals.length; i++) {
      const x = xpix(times[i]);
      const y = ypix(vals[i]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  ctx.fillStyle = "#d1d5db";
  ctx.fillText("Time relative to newest sample", left + pw / 2 - 80, H - 6);
}

async function update() {
  try {
    const response = await fetch("/api/data", {cache: "no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);

    const data = await response.json();

    if (!data.ready) {
      statusEl.textContent = data.error || "Waiting for first valid EMG1 packet...";
      statusEl.className = "";
      return;
    }

    document.getElementById("fs").textContent = data.sample_rate_hz + " Hz";
    document.getElementById("chs").textContent = data.channel_count;
    document.getElementById("fmt").textContent = data.sample_format;
    document.getElementById("good").textContent = data.stats.good_packets;
    document.getElementById("crc").textContent = data.stats.bad_crc_packets;
    document.getElementById("disc").textContent = data.stats.discontinuities;

    draw(data);

    if (data.error) {
      statusEl.textContent = data.error;
      statusEl.className = "bad";
    } else {
      statusEl.textContent =
        "Streaming | browser display decimation stride = " + data.stride;
      statusEl.className = "ok";
    }
  } catch (err) {
    statusEl.textContent = "Browser connection error: " + err;
    statusEl.className = "bad";
  }
}

setInterval(update, 75);
update();
</script>
</body>
</html>
"""


def run_web_view(
    reader: EMGReader,
    host: str,
    http_port: int,
    window_seconds: float,
    ymin: float | None,
    ymax: float | None,
) -> None:
    state = WebViewState(
        window_seconds=window_seconds,
        ymin=ymin,
        ymax=ymax,
    )

    stop_event = threading.Event()

    def serial_worker() -> None:
        reference: EMGPacket | None = None

        while not stop_event.is_set():
            try:
                packet = reader.read_packet()

                if reference is None:
                    reference = packet
                    state.configure(packet)
                    print_stream_info(packet)

                verify_stream_unchanged(reference, packet)
                state.append_packet(packet, reader)

            except TimeoutError:
                continue
            except Exception as exc:
                state.set_error(f"serial worker error: {exc}")
                break

    worker = threading.Thread(
        target=serial_worker,
        name="emg-serial-reader",
        daemon=True,
    )
    worker.start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path

            if path == "/":
                body = WEB_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/data":
                body = json.dumps(
                    state.snapshot(),
                    separators=(",", ":"),
                ).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_error(404)

        def log_message(self, format: str, *args) -> None:
            # Avoid printing one line for every browser polling request.
            return

    server = ThreadingHTTPServer((host, http_port), Handler)

    print()
    print("Browser EMG view started.")
    print(f"  bind address : {host}:{http_port}")
    print(
        f"  open in browser: http://<SERVER-IP>:{http_port}/"
    )
    print("Press Ctrl+C in this terminal to stop.")

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nWeb view stopped.")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1.0)



def record_stream(
    reader: EMGReader,
    output_path: Path,
    seconds: float | None,
) -> None:
    if seconds is not None and seconds <= 0:
        raise ValueError("--seconds must be > 0")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    first_packet = wait_for_packet(reader)
    print_stream_info(first_packet)

    target_samples = None
    if seconds is not None:
        target_samples = max(
            1,
            int(round(seconds * first_packet.sample_rate_hz)),
        )
        print(f"Recording about {seconds:.3f} s to {output_path}")
    else:
        print(f"Recording to {output_path}")
        print("Press Ctrl+C to stop.")

    samples_written_per_channel = 0
    packets_written = 0
    started_at = time.monotonic()
    last_status_time = started_at
    packet = first_packet

    try:
        with output_path.open("wb") as f:
            while True:
                verify_stream_unchanged(first_packet, packet)

                # Preserve the exact validated packet bytes.
                f.write(packet.raw_bytes)
                packets_written += 1
                samples_written_per_channel += packet.samples_per_channel

                now = time.monotonic()
                if now - last_status_time >= 1.0:
                    captured_seconds = (
                        samples_written_per_channel
                        / first_packet.sample_rate_hz
                    )
                    print(
                        f"\rRecorded {captured_seconds:8.3f} s  "
                        f"{packets_written:6d} packets",
                        end="",
                        flush=True,
                    )
                    last_status_time = now

                if (
                    target_samples is not None
                    and samples_written_per_channel >= target_samples
                ):
                    break

                while True:
                    try:
                        packet = reader.read_packet()
                        break
                    except TimeoutError:
                        continue

            f.flush()

    except KeyboardInterrupt:
        print("\nRecording stopped by user.")

    captured_seconds = (
        samples_written_per_channel
        / first_packet.sample_rate_hz
    )
    elapsed = time.monotonic() - started_at

    print()
    print("Recording complete:")
    print(f"  file                 : {output_path}")
    print(f"  packets              : {packets_written}")
    print(f"  samples/channel      : {samples_written_per_channel}")
    print(f"  captured duration    : {captured_seconds:.6f} s")
    print(f"  host elapsed time    : {elapsed:.3f} s")
    print(f"  CRC failures skipped : {reader.bad_crc_packets}")
    print(f"  bad headers skipped  : {reader.bad_header_packets}")
    print(f"  discontinuities      : {reader.discontinuities}")

    if seconds is not None:
        print(
            "  note                 : timed recording keeps whole packets, "
            "so the duration can exceed the request by less than one packet."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="EMG Packet Protocol V1 serial host"
    )

    parser.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="serial port (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=460800,
        help="serial baud rate (default: 460800)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.5,
        help="serial read timeout in seconds (default: 0.5)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    view_parser = subparsers.add_parser(
        "view",
        help="show the live EMG waveform",
    )
    view_parser.add_argument(
        "--window",
        type=float,
        default=3.0,
        help="visible history in seconds (default: 3)",
    )
    view_parser.add_argument(
        "--ymin",
        type=float,
        default=None,
        help="optional fixed lower Y limit",
    )
    view_parser.add_argument(
        "--ymax",
        type=float,
        default=None,
        help="optional fixed upper Y limit",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="show the live EMG waveform in a web browser",
    )
    web_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="HTTP bind address (default: 0.0.0.0)",
    )
    web_parser.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="HTTP port (default: 8000)",
    )
    web_parser.add_argument(
        "--window",
        type=float,
        default=3.0,
        help="visible history in seconds (default: 3)",
    )
    web_parser.add_argument(
        "--ymin",
        type=float,
        default=None,
        help="optional fixed lower Y limit",
    )
    web_parser.add_argument(
        "--ymax",
        type=float,
        default=None,
        help="optional fixed upper Y limit",
    )

    record_parser = subparsers.add_parser(
        "record",
        help="record validated raw EMG packets",
    )
    record_parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="record this many seconds; omit to stop manually with Ctrl+C",
    )
    record_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output .emg file",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()
    reader: EMGReader | None = None

    try:
        reader = EMGReader(
            port=args.port,
            baudrate=args.baud,
            timeout=args.timeout,
        )

        print(
            f"Opened {args.port} at {args.baud} baud. "
            "Searching for EMG1 packets..."
        )

        if args.command == "view":
            live_view(
                reader=reader,
                window_seconds=args.window,
                ymin=args.ymin,
                ymax=args.ymax,
            )
        elif args.command == "web":
            run_web_view(
                reader=reader,
                host=args.host,
                http_port=args.http_port,
                window_seconds=args.window,
                ymin=args.ymin,
                ymax=args.ymax,
            )
        elif args.command == "record":
            record_stream(
                reader=reader,
                output_path=args.out,
                seconds=args.seconds,
            )

        return 0

    except serial.SerialException as exc:
        print(f"[ERROR] serial port error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
