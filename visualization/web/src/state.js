// Client-side result state (no DOM, no Three.js: unit-tested with node, test/run_tests.mjs).
//
// Losses are kept apart:
//   mailboxDrop        hardware: mailbox_drop_cnt of the current epoch (+ DROP_BEFORE records)
//   networkDrop        the server's queue for THIS client overflowed (network_drop_cnt)
//   missedDisconnected server_seq gap across a reconnect, not explained by networkDrop
// Duplicates / out-of-order (server_seq not increasing) and invalid messages are counted and ignored.
// "Stale": no live result for staleMs or the socket is not open; the pose is kept, never turned
// into a REST classification.

import { LABELS } from "./gestures.js";
import { gestureLabel, validate } from "./protocol.js";

export class GestureState {
  constructor({ staleMs = 1500, historyMax = 120, rateWindowMs = 2000 } = {}) {
    this.staleMs = staleMs;
    this.historyMax = historyMax;
    this.rateWindowMs = rateWindowMs;
    this.source = null;
    this.sourceNote = "";
    this.serverId = null;
    this.connected = false;
    this.reconnects = 0;
    this.last = null;               // last accepted result message
    this.lastLocalMs = null;        // local time the last result was produced (history: back-dated)
    this.targetLabel = null;        // pose target: a class label, or null (keep the current pose)
    this.display = "WAITING";       // label shown (class, UNCERTAIN, NO_DECISION, INVALID(n))
    this.lastSeq = null;
    this.lastNetDrop = 0;
    this.afterReconnect = false;
    this.status = null;
    this.history = [];
    this.times = [];
    this.c = { results: 0, history: 0, duplicates: 0, outOfOrder: 0, invalid: 0, epochChanges: 0,
               missedDisconnected: 0, unexplainedGap: 0, networkDrop: 0, mailboxDrop: 0, dropBeforeRecords: 0,
               uncertain: 0, protocolErrors: 0, serverRestarts: 0, historySkipped: 0 };
  }

  setConnected(on) {
    if (on && !this.connected && this.lastSeq !== null) this.afterReconnect = true;
    if (on && !this.connected && this.everConnected) this.reconnects += 1;
    if (on) this.everConnected = true;
    this.connected = on;
  }

  // returns {kind, accepted, problems}
  onMessage(m, nowMs) {
    const problems = validate(m);
    if (problems.length) {
      if (m && m.type === "result") this.c.invalid += 1; else this.c.protocolErrors += 1;
      if (m && m.type === "result") this.display = `INVALID(${m.gesture_id})`;
      return { kind: m && m.type, accepted: false, problems };
    }
    if (m.type === "hello") return this.onHello(m, nowMs);
    if (m.type === "status") { this.status = m; return { kind: "status", accepted: true, problems: [] }; }
    if (m.type === "pong") return { kind: "pong", accepted: true, problems: [] };
    return this.onResult(m, nowMs, false);
  }

  onHello(m, nowMs) {
    const id = m.server && m.server.started_monotonic_ns;
    if (this.serverId !== null && id !== this.serverId) {        // another server process: new sequence
      this.c.serverRestarts += 1;
      this.lastSeq = null;
      this.lastNetDrop = 0;
      this.afterReconnect = false;
    }
    this.serverId = id;
    this.source = m.source;
    this.sourceNote = m.source_note || "";
    const now = m.server && m.server.now_monotonic_ns;
    for (const h of m.history || []) {
      // history overlapping what this client already has is expected after a reconnect: skip it
      if (this.lastSeq !== null && h.server_seq <= this.lastSeq) { this.c.historySkipped += 1; continue; }
      if (validate(h).length === 0) this.onResult(h, nowMs, true, now);
    }
    return { kind: "hello", accepted: true, problems: [] };
  }

  onResult(m, nowMs, fromHistory, serverNowNs) {
    if (this.lastSeq !== null && m.server_seq <= this.lastSeq) {
      if (m.server_seq === this.lastSeq) this.c.duplicates += 1; else this.c.outOfOrder += 1;
      return { kind: "result", accepted: false, problems: ["not newer than the last result"] };
    }
    if (this.lastSeq !== null && m.server_seq > this.lastSeq + 1) {
      const gap = m.server_seq - this.lastSeq - 1;
      const net = Math.max(0, m.network_drop_cnt - this.lastNetDrop);
      const rest = Math.max(0, gap - net);
      if (this.afterReconnect) this.c.missedDisconnected += rest; else this.c.unexplainedGap += rest;
    }
    this.afterReconnect = false;
    this.lastSeq = m.server_seq;
    this.lastNetDrop = m.network_drop_cnt;
    this.c.networkDrop = m.network_drop_cnt;
    if (this.last && m.reset_epoch !== this.last.reset_epoch) this.c.epochChanges += 1;
    this.c.mailboxDrop = m.mailbox_drop_cnt;
    if (m.drop_before) this.c.dropBeforeRecords += 1;
    this.last = m;
    // history results are back-dated by their age on the server, so an old one shows as stale
    let t = nowMs;
    if (fromHistory && serverNowNs !== undefined) t = nowMs - Math.max(0, (serverNowNs - m.ps_read_monotonic_ns) / 1e6);
    this.lastLocalMs = t;
    if (fromHistory) this.c.history += 1; else { this.c.results += 1; this.times.push(nowMs); }
    const g = m.gesture_id;
    if (!m.valid) {
      this.c.invalid += 1;
      this.display = `INVALID(${g})`;
    } else if (g >= 0 && g < LABELS.length) {
      this.targetLabel = LABELS[g];
      this.display = LABELS[g];
    } else if (g === -1 || g === -2) {
      this.c.uncertain += 1;
      this.display = gestureLabel(g);       // UNCERTAIN / NO_DECISION: pose unchanged
    } else {
      this.c.invalid += 1;
      this.display = gestureLabel(g);       // INVALID(n): out-of-range class, pose unchanged
    }
    this.history.push({ seq: m.server_seq, epoch: m.reset_epoch, index: m.result_index, label: this.display,
                        gesture_id: g, argmax: m.argmax, scores: m.scores, window_end_seq: m.window_end_seq,
                        segment: m.segment,
                        source: m.source, history: Boolean(fromHistory), rx_ms: nowMs });
    if (this.history.length > this.historyMax) this.history.splice(0, this.history.length - this.historyMax);
    this.trimTimes(nowMs);
    return { kind: "result", accepted: true, problems: [] };
  }

  trimTimes(nowMs) {
    while (this.times.length && nowMs - this.times[0] > this.rateWindowMs) this.times.shift();
  }

  rateHz(nowMs) {
    this.trimTimes(nowMs);
    if (this.times.length < 2) return 0;
    return ((this.times.length - 1) * 1000) / Math.max(1, this.times[this.times.length - 1] - this.times[0]);
  }

  ageMs(nowMs) {
    return this.lastLocalMs === null ? null : nowMs - this.lastLocalMs;
  }

  isStale(nowMs) {
    const a = this.ageMs(nowMs);
    return !this.connected || a === null || a > this.staleMs;
  }
}
