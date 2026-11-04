// Unit tests of the browser logic without a browser (node >= 14):
//   node visualization/web/test/run_tests.mjs
// gestures.js (presets, interpolation), protocol.js (v1 validation), state.js (ordering, epochs,
// losses, stale, bounded memory), and the saved E4a-B board records as v1 messages.

import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { LABELS, POSES, JOINTS, poseVector, vectorPose, stepTowards, maxError } from "../src/gestures.js";
import { validate, gestureLabel } from "../src/protocol.js";
import { GestureState } from "../src/state.js";

const here = dirname(fileURLToPath(import.meta.url));
let pass = 0, fail = 0;
function test(name, fn) {
  try { fn(); pass += 1; console.log(`ok   ${name}`); }
  catch (e) { fail += 1; console.log(`FAIL ${name}: ${e.message}`); }
}
function eq(a, b, msg = "") {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) throw new Error(`${msg} ${sa} != ${sb}`);
}
function ok(c, msg) { if (!c) throw new Error(msg || "assertion"); }

let seq = 0;
function msg(over = {}) {
  seq += 1;
  return Object.assign({
    schema_version: 1, type: "result", source: "synthetic_demo", server_seq: seq, result_index: seq, reset_epoch: 1,
    epoch_seq: seq - 1, window_end_seq: 399, esi_timestamp: 399, gesture_id: 1, gesture: "HAND_CLOSE", argmax: 1,
    scores: [-1, 5, -2, -3, -4, -5], flags: 0x19, drop_before: false, cnn_lat_cycles: 11152, detect_cycle: 1,
    publish_cycle: 2, ps_read_monotonic_ns: 1000, mailbox_drop_cnt: 0, network_drop_cnt: 0, valid: true,
  }, over);
}
const hello = (id = 1, history = [], now = 0) => ({ schema_version: 1, type: "hello", source: "synthetic_demo", labels: LABELS,
  server: { started_monotonic_ns: id, now_monotonic_ns: now }, history });

// ---------------------------------------------------------------- gestures
test("labels: frozen E2/E3 order", () => eq(LABELS, ["REST", "HAND_CLOSE", "HAND_OPEN", "WRIST_FLEX", "WRIST_EXT", "PINCH"]));
test("six presets, every joint defined", () => {
  for (const l of LABELS) {
    const v = poseVector(POSES[l]);
    eq(v.length, JOINTS.length, l);
    ok(v.every((x) => Number.isFinite(x)), `${l} has a non-number`);
    eq(poseVector(vectorPose(v)), v, `${l} round trip`);
  }
});
test("presets are pairwise distinct (>= 20 deg somewhere)", () => {
  for (let i = 0; i < LABELS.length; i++)
    for (let j = i + 1; j < LABELS.length; j++)
      ok(maxError(poseVector(POSES[LABELS[i]]), poseVector(POSES[LABELS[j]])) >= 20, `${LABELS[i]} ~ ${LABELS[j]}`);
});
test("anatomical directions: fist flexes, open extends, wrist flex > 0 > ext", () => {
  const c = POSES.HAND_CLOSE.fingers, o = POSES.HAND_OPEN.fingers;
  for (const n of ["index", "middle", "ring", "little"]) {
    ok(c[n].mcp >= 80 && c[n].pip >= 90, `fist ${n}`);
    ok(o[n].mcp <= 0 && o[n].pip === 0, `open ${n}`);
  }
  ok(o.index.spread > 0 && o.little.spread < 0, "open hand fans out");
  ok(POSES.WRIST_FLEX.wrist.flex >= 45 && POSES.WRIST_EXT.wrist.flex <= -45, "wrist");
  ok(POSES.PINCH.thumb.cmcOpp > POSES.REST.thumb.cmcOpp && POSES.PINCH.fingers.index.mcp > POSES.PINCH.fingers.middle.mcp, "pinch");
  // angles inside a plausible range
  for (const l of LABELS) ok(poseVector(POSES[l]).every((x) => x >= -60 && x <= 110), `${l} range`);
});
test("interpolation: monotone, 95 % after 3 tau, frame-rate independent", () => {
  const a = poseVector(POSES.HAND_OPEN), b = poseVector(POSES.HAND_CLOSE);
  const e0 = maxError(a, b);
  let v = a, prev = e0;
  for (let i = 0; i < 22; i++) {                  // 60 fps for 0.36 s
    v = stepTowards(v, b, 1 / 60, 0.12);
    const e = maxError(v, b);
    ok(e <= prev + 1e-9, "not monotone");
    prev = e;
  }
  ok(prev / e0 < 0.06, `after 3 tau: ${prev / e0}`);
  const one = stepTowards(a, b, 0.2, 0.12), split = stepTowards(stepTowards(a, b, 0.1, 0.12), b, 0.1, 0.12);
  ok(maxError(one, split) < 1e-9, "dt split changes the result");
  eq(stepTowards(a, b, 0.016, 0), b, "tau 0 jumps");
});

// ---------------------------------------------------------------- protocol
test("protocol: valid message", () => eq(validate(msg()), []));
test("protocol: rejects wrong schema / fields", () => {
  ok(validate({ ...msg(), schema_version: 2 }).length, "schema 2");
  ok(validate({ ...msg(), scores: [1, 2, 3] }).length, "scores");
  ok(validate({ ...msg(), scores: [2 ** 31, 0, 0, 0, 0, 0] }).length, "int32");
  ok(validate({ ...msg(), gesture: "REST" }).length, "label");
  ok(validate({ ...msg(), source: "cnn" }).length, "source");
  const m = msg(); delete m.reset_epoch; ok(validate(m).length, "missing");
  ok(validate(hello()).length === 0, "hello");
  ok(validate({ ...hello(), labels: ["A"] }).length, "labels");
  eq(gestureLabel(-1), "UNCERTAIN"); eq(gestureLabel(-2), "NO_DECISION"); eq(gestureLabel(7), "INVALID(7)");
  ok(validate({ ...msg(), extra_field: 1 }).length === 0, "unknown fields are ignored");
});

// ---------------------------------------------------------------- state
test("state: duplicate and out-of-order are ignored", () => {
  const s = new GestureState(); s.setConnected(true);
  const a = msg(), b = msg();
  s.onMessage(a, 0); s.onMessage(b, 10);
  eq(s.onMessage(b, 20).accepted, false); eq(s.onMessage(a, 30).accepted, false);
  eq([s.c.duplicates, s.c.outOfOrder, s.c.results], [1, 1, 2]);
});
test("state: epoch change, result_index restart is not out-of-order", () => {
  const s = new GestureState(); s.setConnected(true);
  s.onMessage(msg({ reset_epoch: 3, result_index: 11 }), 0);
  const r = s.onMessage(msg({ reset_epoch: 4, result_index: 1 }), 50);
  eq([r.accepted, s.c.epochChanges, s.last.reset_epoch], [true, 1, 4]);
});
test("state: invalid / uncertain keep the pose, INVALID shown", () => {
  const s = new GestureState(); s.setConnected(true);
  s.onMessage(msg({ gesture_id: 2, gesture: "HAND_OPEN", argmax: 2 }), 0);
  eq(s.targetLabel, "HAND_OPEN");
  s.onMessage(msg({ gesture_id: 9, gesture: "INVALID(9)" }), 10);        // label ok, class out of range
  eq([s.targetLabel, s.display], ["HAND_OPEN", "INVALID(9)"]);
  s.onMessage(msg({ gesture_id: -1, gesture: "UNCERTAIN" }), 20);
  eq([s.targetLabel, s.display, s.c.uncertain, s.c.invalid], ["HAND_OPEN", "UNCERTAIN", 1, 1]);
  const bad = s.onMessage({ ...msg(), scores: [1] }, 30);
  eq([bad.accepted, s.c.invalid, s.targetLabel], [false, 2, "HAND_OPEN"]);
  s.onMessage(msg({ valid: false, gesture_id: 0, gesture: "REST", argmax: 0 }), 40);
  eq([s.targetLabel, s.c.invalid], ["HAND_OPEN", 3]);
});
test("state: losses kept apart (network / missed while disconnected / mailbox)", () => {
  const s = new GestureState(); s.setConnected(true);
  s.onMessage(hello(7), 0);
  s.onMessage(msg({ server_seq: 100 }), 0);
  s.onMessage(msg({ server_seq: 104, network_drop_cnt: 3 }), 10);      // 3 dropped by the server queue
  eq([s.c.networkDrop, s.c.unexplainedGap, s.c.missedDisconnected], [3, 0, 0]);
  s.setConnected(false); s.setConnected(true);
  s.onMessage(hello(7), 20);
  s.onMessage(msg({ server_seq: 150, network_drop_cnt: 0 }), 30);     // new connection: counter restarts
  eq(s.c.missedDisconnected, 45);
  s.onMessage(msg({ server_seq: 151, mailbox_drop_cnt: 5, drop_before: true, flags: 0x1b }), 40);
  eq([s.c.mailboxDrop, s.c.dropBeforeRecords, s.reconnects], [5, 1, 1]);
});
test("state: server restart resets the sequence", () => {
  const s = new GestureState(); s.setConnected(true);
  s.onMessage(hello(1), 0); s.onMessage(msg({ server_seq: 500 }), 0);
  s.onMessage(hello(2), 10);
  eq(s.onMessage(msg({ server_seq: 1 }), 20).accepted, true);
  eq(s.c.serverRestarts, 1);
});
test("state: stale after staleMs or when disconnected; pose kept, never REST", () => {
  const s = new GestureState({ staleMs: 1500 }); s.setConnected(true);
  s.onMessage(msg(), 1000);
  eq(s.isStale(2000), false); eq(s.isStale(2600), true);
  eq(s.targetLabel, "HAND_CLOSE");
  s.onMessage(msg(), 3000); s.setConnected(false);
  eq(s.isStale(3001), true);
});
test("state: hello history is back-dated (an old result shows as stale)", () => {
  const s = new GestureState({ staleMs: 1500 }); s.setConnected(true);
  const h = msg({ ps_read_monotonic_ns: 1e9 });
  s.onMessage(hello(9, [h], 11e9), 100000);                           // 10 s old on the server
  ok(s.isStale(100000), "history result 10 s old must be stale");
  eq([s.c.history, s.c.results], [1, 0]);
});
test("state: reconnect history overlap is skipped, not counted as duplicate", () => {
  const s = new GestureState(); s.setConnected(true);
  s.onMessage(hello(4), 0);
  for (let q = 1; q <= 30; q++) s.onMessage(msg({ server_seq: q }), q);
  s.setConnected(false); s.setConnected(true);
  const hist = []; for (let q = 21; q <= 40; q++) hist.push(msg({ server_seq: q }));
  s.onMessage(hello(4, hist, 0), 100);
  eq([s.c.historySkipped, s.c.history, s.c.duplicates, s.c.outOfOrder, s.c.missedDisconnected, s.lastSeq], [10, 10, 0, 0, 0, 40]);
  s.onMessage(msg({ server_seq: 45 }), 200);              // 41..44 neither in history nor received
  eq(s.c.unexplainedGap + s.c.missedDisconnected, 4);
});
test("state: rate over the window", () => {
  const s = new GestureState(); s.setConnected(true);
  for (let i = 0; i < 41; i++) s.onMessage(msg(), i * 50);
  ok(Math.abs(s.rateHz(2000) - 20) < 0.6, `rate ${s.rateHz(2000)}`);
});
test("state: memory bounded over 200000 messages", () => {
  const s = new GestureState({ historyMax: 120 }); s.setConnected(true);
  for (let i = 0; i < 200000; i++) s.onMessage(msg(), i * 0.05);
  eq(s.history.length, 120);
  ok(s.times.length <= 40001, `times ${s.times.length}`);
  eq(s.c.results, 200000);
});

// ---------------------------------------------------------------- saved E4a-B board records
test("E4a-B board records (JSON Lines) drive the state: 44 x HAND_CLOSE, 4 epochs, exact scores", () => {
  const f = join(here, "..", "..", "..", "host", "gesture_server", "tests", "fixtures", "e4a_b_rt2k_board.jsonl");
  const recs = readFileSync(f, "utf8").trim().split("\n").map((l) => JSON.parse(l));
  const s = new GestureState(); s.setConnected(true);
  s.onMessage({ ...hello(5), source: "test_fixture" }, 0);
  recs.forEach((r, i) => {
    const m = {                                            // gesture_server.protocol.result_message
      schema_version: 1, type: "result", source: "test_fixture", server_seq: i + 1, result_index: r.result_index,
      reset_epoch: r.epoch, epoch_seq: r.epoch_seq, window_end_seq: r.window_end_seq, esi_timestamp: r.esi_timestamp,
      gesture_id: r.gesture_id, gesture: gestureLabel(r.gesture_id), argmax: r.argmax, scores: r.scores, flags: r.flags,
      drop_before: Boolean(r.flags & 2), cnn_lat_cycles: r.cnn_lat_cycles, detect_cycle: r.detect_cycle,
      publish_cycle: r.publish_cycle, ps_read_monotonic_ns: r.ps_read_ns, mailbox_drop_cnt: 0, network_drop_cnt: 0,
      valid: true, fixture_origin: "e4a_b_rt2k_board.jsonl" };
    eq(s.onMessage(m, i * 50).problems, [], `record ${i}`);
  });
  eq([s.c.results, s.c.epochChanges, s.c.invalid, s.targetLabel], [44, 3, 0, "HAND_CLOSE"]);
  eq(s.last.scores, [-738711, 875018, -342080, 19713, -324352, -1131159]);
  eq(new Set(s.history.map((h) => h.label)).size, 1);
});

// ---------------------------------------------------------------- E4c-A multi-gesture records
test("E4c-A six-class records (fixture, firmware order): pose target follows gesture_id through all six classes", () => {
  const f = join(here, "..", "..", "..", "host", "gesture_server", "tests", "fixtures", "e4c_mg6_expected.json");
  const fx = JSON.parse(readFileSync(f, "utf8"));
  const s = new GestureState(); s.setConnected(true);
  s.onMessage({ ...hello(11), source: "test_fixture" }, 0);
  const PASSES = 2, L = fx.segment_length;
  let n = 0;
  const targets = [];
  fx.segments.forEach((seg, si) => {
    for (let p = 0; p < PASSES; p++) {
      const run = si * PASSES + p;
      seg.decisions.forEach((d, di) => {
        n += 1;
        const m = { schema_version: 1, type: "result", source: "test_fixture", server_seq: n, result_index: n, reset_epoch: 1,
                    epoch_seq: n - 1, window_end_seq: fx.seq0 + run * L + d.end, esi_timestamp: d.end, gesture_id: d.argmax,
                    gesture: gestureLabel(d.argmax), argmax: d.argmax, scores: d.scores, flags: di === 0 ? 0x1d : 0x19,
                    drop_before: false, cnn_lat_cycles: 11152, detect_cycle: 0, publish_cycle: 0, ps_read_monotonic_ns: n,
                    mailbox_drop_cnt: 0, network_drop_cnt: 0, valid: true };
        eq(s.onMessage(m, n * 50).problems, [], `record ${n}`);
        targets.push(s.targetLabel);
      });
    }
  });
  const blocks = [];
  for (let i = 0; i < targets.length; i += 3 * PASSES) blocks.push([...new Set(targets.slice(i, i + 3 * PASSES))]);
  eq(blocks, LABELS.map((l) => [l]));
  ok(LABELS.every((l) => POSES[l]), "a preset for every class");
  eq([s.c.results, s.c.invalid, s.c.duplicates, s.c.outOfOrder, s.c.uncertain], [36, 0, 0, 0, 0]);
});

console.log(`\nJS SUMMARY: ${pass}/${pass + fail} passed`);
process.exit(fail ? 1 : 0);
