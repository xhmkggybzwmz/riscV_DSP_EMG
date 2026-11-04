// Gesture WebSocket protocol v1, client side (host/gesture_server/protocol.py is the reference;
// docs/system/e4b_visualization.md). Unknown fields are ignored; schema_version must be 1.

import { LABELS } from "./gestures.js";

export const SCHEMA_VERSION = 1;
export const SOURCES = ["hardware_replay", "synthetic_demo", "test_fixture", "spi_live"];
const U32 = [0, 0xffffffff];
const I32 = [-0x80000000, 0x7fffffff];

const INT = (lo, hi) => ["int", lo, hi];
export const RESULT_FIELDS = {
  server_seq: INT(0, Number.MAX_SAFE_INTEGER), result_index: INT(...U32), reset_epoch: INT(...U32),
  epoch_seq: INT(...U32), window_end_seq: INT(...U32), esi_timestamp: INT(...U32), gesture_id: INT(-128, 127),
  argmax: INT(0, 255), flags: INT(0, 255), cnn_lat_cycles: INT(...U32), detect_cycle: INT(...U32),
  publish_cycle: INT(...U32), ps_read_monotonic_ns: INT(0, Number.MAX_VALUE), mailbox_drop_cnt: INT(...U32),
  network_drop_cnt: INT(0, Number.MAX_SAFE_INTEGER), gesture: ["str"], source: ["str"], drop_before: ["bool"],
  valid: ["bool"], scores: ["scores"],
};

export function gestureLabel(g) {
  if (g >= 0 && g < LABELS.length) return LABELS[g];
  if (g === -1) return "UNCERTAIN";
  if (g === -2) return "NO_DECISION";
  return `INVALID(${g})`;
}

// problems of a parsed message (empty list = acceptable v1 message)
export function validate(m) {
  if (m === null || typeof m !== "object") return ["not an object"];
  if (m.schema_version !== SCHEMA_VERSION) return [`schema_version ${m.schema_version}`];
  if (!["hello", "result", "status", "pong"].includes(m.type)) return [`type ${m.type}`];
  if (m.type === "hello") {
    const p = [];
    if (!Array.isArray(m.labels) || m.labels.join() !== LABELS.join()) p.push("labels differ from the frozen class order");
    if (!SOURCES.includes(m.source)) p.push(`source ${m.source}`);
    return p;
  }
  if (m.type !== "result") return [];
  const p = [];
  for (const [k, [t, lo, hi]] of Object.entries(RESULT_FIELDS)) {
    const v = m[k];
    if (v === undefined) { p.push(`missing ${k}`); continue; }
    if (t === "int" && !(Number.isInteger(v) && v >= lo && v <= hi)) p.push(`${k}: ${v}`);
    if (t === "str" && typeof v !== "string") p.push(`${k}: not a string`);
    if (t === "bool" && typeof v !== "boolean") p.push(`${k}: not a boolean`);
    if (t === "scores" && !(Array.isArray(v) && v.length === LABELS.length &&
                            v.every((s) => Number.isInteger(s) && s >= I32[0] && s <= I32[1]))) p.push("scores: need 6 int32");
  }
  if (!p.length) {
    if (!SOURCES.includes(m.source)) p.push(`source ${m.source}`);
    if (m.gesture !== gestureLabel(m.gesture_id)) p.push("gesture label does not match gesture_id");
  }
  return p;
}
