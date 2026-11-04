"""Gesture WebSocket protocol v1 (docs/system/e4b_visualization.md).

Server -> client JSON text messages; the client never sends commands that reach the
hardware (the only client -> server message is an optional {"type": "ping"}).

    hello   once per connection: schema, labels, source, server identity, recent history
    result  one classification (a mailbox record, a replayed record or a synthetic event)
    status  heartbeat (1 s): source state, reader / mailbox / network counters
    level   source spi_live only: activity of lane 0 after the DSP, intensity 0..1 (about 15.6 per second)

Class order is the frozen E2/E3 order (ps_mailbox.protocol.LABELS). Integer fields are JSON
numbers: scores are signed int32 (two's complement already applied), indices / cycles are
unsigned 32-bit counters that wrap at 2**32, gesture_id is a signed int8 (-1 UNCERTAIN,
-2 NO_DECISION, 0..5 a class, anything else INVALID).

Compatibility: a v1 client accepts every message with schema_version 1 and ignores unknown
fields; a field is never removed or given another meaning inside v1 (that needs v2).
"""

from __future__ import annotations

from ps_mailbox import protocol as MP          # host/ps_mailbox: labels, flags (run from host/)

SCHEMA_VERSION = 1
LABELS = MP.LABELS
SOURCES = ("hardware_replay", "synthetic_demo", "test_fixture", "spi_live")
SOURCE_STATES = ("starting", "running", "done", "stopped", "error", "unavailable")
U32 = (0, 0xFFFFFFFF)
I32 = (-0x80000000, 0x7FFFFFFF)

# result fields: name -> (type, range or None, required, meaning / clock domain)
RESULT_FIELDS = {
    "schema_version":        (int, (1, 1), True, "protocol version"),
    "type":                  (str, None, True, '"result"'),
    "source":                (str, None, True, "hardware_replay | synthetic_demo | test_fixture"),
    "server_seq":            (int, (0, 2**53), True, "server message number (all results, all clients); "
                                                     "a gap after a reconnect = missed while disconnected"),
    "result_index":          (int, U32, True, "CNN result number of the run (RESULT_INDEX, from 1)"),
    "reset_epoch":           (int, U32, True, "producer epoch (RESET_EPOCH); +1 per RV32I run"),
    "epoch_seq":             (int, U32, True, "record number in the epoch (PUBLISH_INDEX - EPOCH_START_INDEX)"),
    "window_end_seq":        (int, U32, True, "ESI sequence number of the window's last sample (ESI domain)"),
    "esi_timestamp":         (int, U32, True, "ESI timestamp of that sample (ESI domain; replay: frame number)"),
    "gesture_id":            (int, (-128, 127), True, "post-processed gesture, int8: 0..5 class, -1 / -2 no class"),
    "gesture":               (str, None, True, "label of gesture_id (UNCERTAIN, NO_DECISION, INVALID(n))"),
    "argmax":                (int, (0, 255), True, "CNN ARGMAX (0..5 when valid)"),
    "scores":                (list, I32, True, "6 int32 class scores in LABELS order"),
    "flags":                 (int, (0, 255), True, "record flags (ps_mailbox.protocol F_*)"),
    "drop_before":           (bool, None, True, "the mailbox dropped results before this one (ring full)"),
    "cnn_lat_cycles":        (int, U32, True, "CNN latency, PL pl_clk0 cycles (100 MHz)"),
    "detect_cycle":          (int, U32, True, "SYSCTRL CYCLE_LO when the RV32I saw the result (PL domain)"),
    "publish_cycle":         (int, U32, True, "SYSCTRL CYCLE_LO before the record commit (PL domain)"),
    "ps_read_monotonic_ns":  (int, (0, 2**63 - 1), True, "CLOCK_MONOTONIC of the server host when the record "
                                                        "was read / emitted (PS domain, not comparable with PL cycles)"),
    "mailbox_drop_cnt":      (int, U32, True, "hardware: DROP_CNT of the epoch at the last header read; else 0"),
    "network_drop_cnt":      (int, (0, 2**53), True, "messages the server dropped for this client (queue full)"),
    "valid":                 (bool, None, True, "record passed the mailbox / content checks"),
    "fixture_origin":        (str, None, False, "test_fixture: where the replayed record came from"),
    "segment":               (int, (0, 0xFFFF), False, "record SEGMENT (CNN segment = SOS-separated replay run "
                                                       "inside the epoch); added in v1, optional"),
}
SOURCE_NOTE = {
    "hardware_replay": "real KV260 FPGA pipeline (public EMG data replayed in the PL) via the mailbox",
    "synthetic_demo": "SIMULATED classification events for UI tests - not a CNN output",
    "test_fixture": "records replayed from a saved JSON Lines file",
    "spi_live": "real KV260 FPGA pipeline on live EMG from the STM32: one channel, classes 0..2 mean REST, LIGHT, STRONG",
}


def gesture_label(g: int) -> str:
    return MP.gesture_name(g)


def result_message(rec: dict, source: str, server_seq: int, ps_ns: int, mailbox_drop_cnt: int = 0,
                   valid: bool = True, fixture_origin: str | None = None) -> dict:
    """result message from a ps_mailbox.pynq_reader.to_json() record (or a dict with its keys)"""
    if source not in SOURCES:
        raise ValueError(f"source {source!r}")
    g = rec["gesture_id"]
    m = {"schema_version": SCHEMA_VERSION, "type": "result", "source": source, "server_seq": server_seq,
         "result_index": rec["result_index"], "reset_epoch": rec["epoch"], "epoch_seq": rec.get("epoch_seq") or 0,
         "window_end_seq": rec["window_end_seq"], "esi_timestamp": rec["esi_timestamp"],
         "gesture_id": g, "gesture": gesture_label(g), "argmax": rec["argmax"], "scores": list(rec["scores"]),
         "flags": rec["flags"], "drop_before": bool(rec["flags"] & MP.F_DROP_BEFORE),
         "cnn_lat_cycles": rec["cnn_lat_cycles"], "detect_cycle": rec["detect_cycle"],
         "publish_cycle": rec["publish_cycle"], "ps_read_monotonic_ns": ps_ns,
         "mailbox_drop_cnt": mailbox_drop_cnt, "network_drop_cnt": 0, "valid": bool(valid)}
    if fixture_origin is not None:
        m["fixture_origin"] = fixture_origin
    if rec.get("segment") is not None:
        m["segment"] = rec["segment"]
    return m


def validate_result(m: dict) -> list[str]:
    """problems of a result message (empty = valid v1)"""
    p = []
    for name, (typ, rng, req, _) in RESULT_FIELDS.items():
        if name not in m:
            if req:
                p.append(f"missing {name}")
            continue
        v = m[name]
        if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
            p.append(f"{name}: not an integer")
        elif typ is not int and not isinstance(v, typ):
            p.append(f"{name}: not {typ.__name__}")
        elif typ is int and rng and not rng[0] <= v <= rng[1]:
            p.append(f"{name}: {v} outside {rng}")
    if not p:
        if m["type"] != "result":
            p.append("type")
        if m["source"] not in SOURCES:
            p.append(f"source {m['source']!r}")
        if len(m["scores"]) != len(LABELS) or any(isinstance(s, bool) or not isinstance(s, int)
                                                   or not I32[0] <= s <= I32[1] for s in m["scores"]):
            p.append("scores: need 6 int32")
        if m["gesture"] != gesture_label(m["gesture_id"]):
            p.append("gesture label does not match gesture_id")
        if m["drop_before"] != bool(m["flags"] & MP.F_DROP_BEFORE):
            p.append("drop_before does not match flags")
    return p


def hello_message(source: str, server: dict, history: list) -> dict:
    return {"schema_version": SCHEMA_VERSION, "type": "hello", "source": source, "source_note": SOURCE_NOTE[source],
            "labels": list(LABELS), "server": server, "history": history}


def status_message(source: str, state: str, counters: dict, client: dict) -> dict:
    if state not in SOURCE_STATES:
        raise ValueError(state)
    return {"schema_version": SCHEMA_VERSION, "type": "status", "source": source, "source_state": state,
            "counters": counters, "client": client}
