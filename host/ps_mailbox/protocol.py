"""Mailbox v1 protocol: RV32I (producer) -> shared BRAM -> ARM PS (consumer).

Single source of the layout (docs/soc/e4a_mailbox.md). scripts/gen/gen_mailbox.py
generates from this module:
    firmware/rv32/mailbox/mbx_layout.h        C constants for the RV32I firmware
    dv/mbx/mbx_layout.vh                      Verilog constants for the test benches
    host/ps_mailbox/configs/kv260_mailbox.json addresses + layout for the PS reader
No hardware access and no pynq import here: the module runs on the PC and on the board.

Window (4 KB, 32-bit little-endian words, 4-byte aligned accesses only):
    0x000-0x03F  header, written by the RV32I only
    0x040-0x07F  consumer words, written by the PS only
    0x080-0x0FF  reserved (0)
    0x100-0x4FF  ring: RING_SIZE = 16 records of RECORD_SIZE = 64 bytes
    0x500-0xFFF  reserved (0)
Single producer (RV32I), single consumer (PS). Each word has exactly one writer.

Indices are free-running unsigned 32-bit counters (wrap at 2**32): record n of the
stream lives in slot n % RING_SIZE; used = (PRODUCER_INDEX - CONSUMER_INDEX) mod 2**32
must be 0..RING_SIZE. RING_SIZE divides 2**32, so the slot mapping survives the wrap.

Producer (RV32I) publish of one result (stores reach the BRAM in program order:
one outstanding CMI access, the store is in the BRAM when it is answered):
    1. consumer = CONSUMER_INDEX if CONSUMER_EPOCH == RESET_EPOCH (read epoch, index,
       epoch again until both epoch reads agree), else EPOCH_START_INDEX
       (no consumer attached to this epoch yet: the first RING_SIZE results are kept)
    2. used = PRODUCER_INDEX - consumer; used > RING_SIZE -> ERROR_CNT, LAST_ERROR =
       ERR_BAD_CONSUMER and the result is dropped; used == RING_SIZE -> DROP_CNT += 1,
       result dropped (never overwrite an unconsumed record, never block the CNN)
    3. slot word REC_HDR = 0 (invalidate), words 1..15, REC_HDR = magic|version|flags
    4. PRODUCER_INDEX += 1, then PUBLISHED_CNT += 1
Consumer (PS):
    1. MAGIC / VERSION major must match; STATE must be RUNNING or DONE
    2. RESET_EPOCH changed (or first attach): CONSUMER_INDEX = EPOCH_START_INDEX (backlog)
       or PRODUCER_INDEX (live), then CONSUMER_EPOCH = RESET_EPOCH, CONSUMED_CNT = 0
    3. PRODUCER_INDEX read until two consecutive reads agree; avail = prod - cons
    4. per record: words read, then REC_HDR and PUBLISH_INDEX read again; valid when
       REC_HDR magic/version ok, PUBLISH_INDEX == cons, RESET_EPOCH == epoch and both
       re-reads unchanged; then CONSUMER_INDEX = cons + 1, CONSUMED_CNT += 1
Reset: BRAM contents survive CPU / PL resets. The producer starts every run with a new
epoch (RESET_EPOCH + 1 when the header is valid, else 1), STATE = INIT while it clears
the ring headers and rewrites the header, then STATE = RUNNING.
"""

from __future__ import annotations

MAGIC = 0x4D425831                 # "MBX1"
VERSION = 0x00010000               # major 1, minor 0
VERSION_MAJOR = VERSION >> 16
WINDOW_SIZE = 0x1000
RING_SIZE = 16
RECORD_SIZE = 64
RING_OFFSET = 0x100
RECORD_WORDS = RECORD_SIZE // 4

# header words: name -> (byte offset, writer, description)
HEADER = {
    "MAGIC":              (0x00, "cpu", "0x4D425831 'MBX1' once the header is valid"),
    "VERSION":            (0x04, "cpu", "major << 16 | minor"),
    "RING_SIZE":          (0x08, "cpu", "number of record slots"),
    "RECORD_SIZE":        (0x0C, "cpu", "bytes per record"),
    "RING_OFFSET":        (0x10, "cpu", "byte offset of slot 0"),
    "RESET_EPOCH":        (0x14, "cpu", "+1 on every producer start (>= 1)"),
    "STATE":              (0x18, "cpu", "producer state, STATE_*"),
    "PRODUCER_INDEX":     (0x1C, "cpu", "records published (free-running u32)"),
    "EPOCH_START_INDEX":  (0x20, "cpu", "PRODUCER_INDEX at the start of this epoch"),
    "PUBLISHED_CNT":      (0x24, "cpu", "records published in this epoch"),
    "RESULT_CNT":         (0x28, "cpu", "CNN results taken from the result FIFO in this epoch"),
    "DROP_CNT":           (0x2C, "cpu", "results not published: ring full"),
    "ERROR_CNT":          (0x30, "cpu", "producer-detected errors"),
    "LAST_ERROR":         (0x34, "cpu", "ERR_* code of the last error"),
    "HEARTBEAT":          (0x38, "cpu", "SYSCTRL CYCLE_LO, refreshed while the producer waits"),
    "CONSUMER_INDEX":     (0x40, "ps", "records consumed (free-running u32)"),
    "CONSUMER_EPOCH":     (0x44, "ps", "RESET_EPOCH the consumer is attached to"),
    "CONSUMED_CNT":       (0x48, "ps", "records consumed in this epoch"),
    "CONSUMER_HEARTBEAT": (0x4C, "ps", "free for the consumer (e.g. poll count)"),
}

STATE_INIT, STATE_RUNNING, STATE_DONE, STATE_ERROR = 0, 1, 2, 3
STATE_NAMES = {STATE_INIT: "INIT", STATE_RUNNING: "RUNNING", STATE_DONE: "DONE", STATE_ERROR: "ERROR"}

ERR_NONE, ERR_BAD_CONSUMER, ERR_CNN_CHECK, ERR_TIMEOUT, ERR_ARGMAX = 0, 1, 2, 3, 4

# record words: name -> (word index, description)
RECORD = {
    "REC_HDR":        (0, "[31:16] REC_MAGIC, [15:8] REC_VERSION, [7:0] FLAGS; written last (commit)"),
    "PUBLISH_INDEX":  (1, "ring sequence number of this record (= PRODUCER_INDEX before publishing)"),
    "RESET_EPOCH":    (2, "producer epoch"),
    "RESULT_INDEX":   (3, "CNN result number (CNN RES_INDEX, from 1 after a CNN reset)"),
    "WINDOW_END_SEQ": (4, "ESI sequence number of the last sample of the window (CNN RES_SEQ)"),
    "ESI_TIMESTAMP":  (5, "ESI timestamp of that sample (CNN RES_TS; replay: frame number)"),
    "CLASS":          (6, "[7:0] GESTURE (int8, post-processed), [15:8] CNN ARGMAX, [31:16] SEGMENT"),
    "SCORE0":         (7, "int32 class score REST"),
    "SCORE1":         (8, "int32 class score HAND_CLOSE"),
    "SCORE2":         (9, "int32 class score HAND_OPEN"),
    "SCORE3":         (10, "int32 class score WRIST_FLEX"),
    "SCORE4":         (11, "int32 class score WRIST_EXT"),
    "SCORE5":         (12, "int32 class score PINCH"),
    "CNN_LAT_CYCLES": (13, "CNN result latency measured by the CNN page (PL cycles)"),
    "DETECT_CYCLE":   (14, "SYSCTRL CYCLE_LO when the RV32I saw STATUS.PENDING"),
    "PUBLISH_CYCLE":  (15, "SYSCTRL CYCLE_LO before the commit write"),
}
REC_MAGIC = 0x5243                 # "RC"
REC_VERSION = 1

# REC_HDR[7:0]
F_POSTPROC = 0x01                  # GESTURE from the post-processing (K, T) of the firmware
F_DROP_BEFORE = 0x02               # results were dropped (ring full) since the previous record
F_NEW_SEGMENT = 0x04               # first published result of a new ESI segment / epoch
F_ARGMAX_OK = 0x08                 # RV32I: CNN ARGMAX == argmax(scores), ties -> lowest index
F_GOLDEN_OK = 0x10                 # self-test firmware: scores equal the E2 golden decision
F_LAST = 0x80                      # last result of a finite run

# frozen E2/E3 class order (ml/emgml/labels.py UNIFIED, reference/emg_fixed/postproc.py)
LABELS = ("REST", "HAND_CLOSE", "HAND_OPEN", "WRIST_FLEX", "WRIST_EXT", "PINCH")
GESTURE_UNCERTAIN = -1
GESTURE_NO_DECISION = -2

MASK32 = 0xFFFFFFFF


def hoff(name: str) -> int:
    return HEADER[name][0]


def slot_offset(index: int) -> int:
    """byte offset of the record slot of stream index `index`"""
    return RING_OFFSET + (index % RING_SIZE) * RECORD_SIZE


def used(prod: int, cons: int) -> int:
    return (prod - cons) & MASK32


def s32(v: int) -> int:
    v &= MASK32
    return v - (1 << 32) if v & 0x80000000 else v


def s8(v: int) -> int:
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def rec_hdr(flags: int) -> int:
    return (REC_MAGIC << 16) | (REC_VERSION << 8) | (flags & 0xFF)


def argmax_lowest(scores) -> int:
    best = 0
    for c in range(1, len(scores)):
        if scores[c] > scores[best]:
            best = c
    return best


def gesture_name(g: int) -> str:
    if 0 <= g < len(LABELS):
        return LABELS[g]
    return {GESTURE_UNCERTAIN: "UNCERTAIN", GESTURE_NO_DECISION: "NO_DECISION"}.get(g, f"INVALID({g})")


def encode_record(r: dict) -> list[int]:
    """record dict (decode_record keys) -> 16 words; used by the Python producer model"""
    w = [0] * RECORD_WORDS
    w[0] = rec_hdr(r["flags"])
    w[1] = r["publish_index"] & MASK32
    w[2] = r["epoch"] & MASK32
    w[3] = r["result_index"] & MASK32
    w[4] = r["window_end_seq"] & MASK32
    w[5] = r["esi_timestamp"] & MASK32
    w[6] = (r["gesture"] & 0xFF) | ((r["argmax"] & 0xFF) << 8) | ((r["segment"] & 0xFFFF) << 16)
    for c in range(6):
        w[7 + c] = r["scores"][c] & MASK32
    w[13] = r["cnn_lat_cycles"] & MASK32
    w[14] = r["detect_cycle"] & MASK32
    w[15] = r["publish_cycle"] & MASK32
    return w


def decode_record(w: list[int]) -> dict:
    return {
        "rec_magic": w[0] >> 16, "rec_version": (w[0] >> 8) & 0xFF, "flags": w[0] & 0xFF,
        "publish_index": w[1], "epoch": w[2], "result_index": w[3], "window_end_seq": w[4],
        "esi_timestamp": w[5], "gesture": s8(w[6]), "argmax": (w[6] >> 8) & 0xFF, "segment": w[6] >> 16,
        "scores": [s32(x) for x in w[7:13]], "cnn_lat_cycles": w[13], "detect_cycle": w[14],
        "publish_cycle": w[15],
    }


def record_problems(rec: dict, expect_index: int, epoch: int) -> list[str]:
    """protocol / content checks of one decoded record; empty list = valid"""
    p = []
    if rec["rec_magic"] != REC_MAGIC or rec["rec_version"] != REC_VERSION:
        p.append(f"REC_HDR magic/version {rec['rec_magic']:#06x}/{rec['rec_version']}")
    if rec["publish_index"] != (expect_index & MASK32):
        p.append(f"PUBLISH_INDEX {rec['publish_index']} != expected {expect_index & MASK32}")
    if rec["epoch"] != epoch:
        p.append(f"RESET_EPOCH {rec['epoch']} != {epoch}")
    if not 0 <= rec["argmax"] < len(LABELS):
        p.append(f"ARGMAX {rec['argmax']} out of range")
    elif rec["flags"] & F_ARGMAX_OK and argmax_lowest(rec["scores"]) != rec["argmax"]:
        p.append("ARGMAX does not belong to the scores")
    if not (GESTURE_NO_DECISION <= rec["gesture"] < len(LABELS)):
        p.append(f"GESTURE {rec['gesture']} out of range")
    return p
