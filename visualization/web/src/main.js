// E4b-A gesture view: WebSocket results -> GestureState -> pose target -> smoothed HandModel.
// URL parameters: ws=<url> (default ws://<this host>/ws), stale=<ms> (1500), tau=<s> (0.12),
// pose=<LABEL> (no WebSocket: show one preset, for pose checks), cycle=<s> (no WebSocket: step
// through the six presets locally every s seconds; labelled LOCAL POSE PREVIEW, not data).

import { LABELS, POSES, poseVector, vectorPose, stepTowards } from "./gestures.js";
import { GestureState } from "./state.js";
import { WsClient } from "./websocket.js";
import { HandModel } from "./hand.js";

const THREE_CDN = "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js";
const qs = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);

async function loadThree() {
  try {
    return await import("../vendor/three.module.js");      // optional local copy (offline use)
  } catch (e) {
    return await import(THREE_CDN);
  }
}

// source spi_live: the CNN classes 0..2 are contraction levels, the pose follows the measured activity
const SPI_NAMES = { REST: "REST", HAND_CLOSE: "LIGHT", HAND_OPEN: "STRONG" };
const REST_VECTOR = poseVector(POSES.REST), CLOSE_VECTOR = poseVector(POSES.HAND_CLOSE);
export function intensityVector(x) {
  return REST_VECTOR.map((r, i) => r + (CLOSE_VECTOR[i] - r) * x);
}

function sourceBadge(src, preview) {
  if (preview) return ["LOCAL POSE PREVIEW (no data)", "badge-sim"];
  return {
    hardware_replay: ["KV260 HARDWARE (FPGA replay of public EMG data)", "badge-hw"],
    spi_live: ["KV260 EMG over SPI (STM32 > SPI > DSP > CNN), one channel", "badge-hw"],
    synthetic_demo: ["SYNTHETIC DEMO - simulated classifications, not CNN output", "badge-sim"],
    test_fixture: ["SAVED RECORDS (JSON Lines replay)", "badge-fix"],
  }[src] || ["no source yet", "badge-none"];
}

async function main() {
  const THREE = await loadThree();
  const canvas = $("view");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x10141a);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.1));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(8, 14, 18);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xaabbff, 0.5);
  fill.position.set(-12, -4, 6);
  scene.add(fill);
  const hand = new HandModel(THREE);
  const holder = new THREE.Group();
  holder.add(hand.root);
  holder.rotation.set(0.05, -0.35, 0);
  scene.add(holder);
  const radius = 48;
  let yaw = -0.8, pitch = 0.12, drag = null;   // three-quarter view: wrist flexion is visible
  canvas.addEventListener("pointerdown", (e) => { drag = [e.clientX, e.clientY, yaw, pitch]; });
  window.addEventListener("pointerup", () => { drag = null; });
  window.addEventListener("pointermove", (e) => {
    if (!drag) return;
    yaw = drag[2] + (e.clientX - drag[0]) * 0.01;
    pitch = Math.max(-1.2, Math.min(1.2, drag[3] + (e.clientY - drag[1]) * 0.01));
  });

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(1, h);
    camera.position.set(0, 4.5, radius);
    camera.lookAt(0, 4.5, 0);
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);
  resize();

  const tau = Number(qs.get("tau") || 0.12);
  const state = new GestureState({ staleMs: Number(qs.get("stale") || 1500) });
  let current = poseVector(POSES.REST);
  let targetLabel = "REST";
  const fixed = qs.get("pose");
  const cycle = Number(qs.get("cycle") || 0);
  const preview = Boolean(fixed || cycle);
  let wsState = preview ? "not used" : "idle";
  let wsDetail = "";
  let level = null, levelAt = 0;

  // score bars
  const bars = $("scores");
  const barEls = LABELS.map((l) => {
    const row = document.createElement("div");
    row.className = "bar";
    row.innerHTML = `<span class="bl">${l}</span><span class="bt"><span class="bf"></span></span><span class="bv">-</span>`;
    bars.appendChild(row);
    return row;
  });

  if (fixed) {
    targetLabel = LABELS.includes(fixed) ? fixed : "REST";
    current = poseVector(POSES[targetLabel]);
  } else if (!cycle) {
    const url = qs.get("ws") || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
    $("url").textContent = url;
    const client = new WsClient(url, {
      onMessage: (m) => {
        if (m && m.type === "level") { level = m; levelAt = performance.now(); return; }
        state.onMessage(m, performance.now());
      },
      onState: (s, d) => { wsState = s; wsDetail = d || ""; state.setConnected(s === "open"); },
    });
    client.connect();
    window.__gesture = Object.assign(window.__gesture || {}, { state, client });   // browser console / tests
  }

  let tPrev = performance.now();
  let frames = 0, fps = 0, tFps = tPrev;
  const view = { current, target: targetLabel, fps: 0, frames: 0 };   // read-only, for tests / console
  window.__gesture = Object.assign(window.__gesture || {}, { view, state });
  function frame(now) {
    const dt = (now - tPrev) / 1000;
    tPrev = now;
    if (cycle) targetLabel = LABELS[Math.floor(now / 1000 / cycle) % LABELS.length];
    else if (!fixed && state.targetLabel) targetLabel = state.targetLabel;
    const live = !preview && state.source === "spi_live" && level;
    current = stepTowards(current, live ? intensityVector(level.intensity) : poseVector(POSES[targetLabel]), dt, tau);
    hand.apply(vectorPose(current));
    view.current = current;
    view.target = targetLabel;
    view.frames += 1;
    const stale = !preview && state.isStale(now);
    hand.setDimmed(stale);
    holder.rotation.set(pitch, yaw, 0);
    renderer.render(scene, camera);
    frames += 1;
    if (now - tFps > 1000) { fps = (frames * 1000) / (now - tFps); frames = 0; tFps = now; view.fps = fps; }
    updatePanel(now, stale);
    requestAnimationFrame(frame);
  }

  function updatePanel(now, stale) {
    const [txt, cls] = sourceBadge(state.source, preview);
    const b = $("source");
    b.textContent = txt;
    b.className = `badge ${cls}`;
    const spi = state.source === "spi_live";
    $("gesture").textContent = preview ? targetLabel : (spi && SPI_NAMES[state.display]) || state.display;
    $("levelrow").style.display = spi ? "block" : "none";
    if (spi && level) {
      const fresh = now - levelAt < 1000;
      $("levelfill").style.width = `${(100 * level.intensity).toFixed(1)}%`;
      $("levelfill").style.opacity = fresh ? 1 : 0.3;
      $("leveltext").textContent = `${Math.round(100 * level.intensity)} %  (RMS ${level.rms} LSB${level.calibrated ? "" : ", uncalibrated"})`;
    }
    $("gesture").style.opacity = stale ? 0.4 : 1;       // an old classification must not look live
    $("pose").textContent = spi && level ? `follows activity, ${Math.round(100 * level.intensity)} % closed` : targetLabel;
    $("stale").style.display = stale ? "block" : "none";
    $("stale").textContent = !preview && wsState !== "open" ? `DISCONNECTED - showing the last pose` :
      `STALE - no result for ${Math.round((state.ageMs(now) || 0) / 100) / 10} s (last pose kept)`;
    $("conn").textContent = wsState + (wsDetail ? ` (${wsDetail})` : "");
    $("conn").className = wsState === "open" ? "ok" : "warn";
    const m = state.last;
    $("index").textContent = m ? `${m.result_index}  (epoch ${m.reset_epoch}, seq ${m.epoch_seq})` : "-";
    $("serverseq").textContent = m ? m.server_seq : "-";
    $("window").textContent = m ? `${m.window_end_seq} / ts ${m.esi_timestamp}` : "-";
    $("argmax").textContent = m ? `${m.argmax} (gesture_id ${m.gesture_id})` : "-";
    const age = state.ageMs(now);
    $("age").textContent = age === null ? "-" : `${(age / 1000).toFixed(2)} s ago`;
    $("rate").textContent = `${state.rateHz(now).toFixed(1)} Hz   (render ${fps.toFixed(0)} fps)`;
    $("lat").textContent = m ? `${m.cnn_lat_cycles} cycles (${(m.cnn_lat_cycles / 100).toFixed(1)} us @ 100 MHz)` : "-";
    const c = state.c;
    $("drops").textContent = `mailbox ${c.mailboxDrop} (DROP_BEFORE records ${c.dropBeforeRecords}) | network ${c.networkDrop}` +
      ` | missed while disconnected ${c.missedDisconnected}`;
    $("drops").className = c.mailboxDrop || c.networkDrop || c.missedDisconnected ? "warn" : "ok";
    $("other").textContent = `results ${c.results} (+history ${c.history}) | dup ${c.duplicates} | out-of-order ${c.outOfOrder}` +
      ` | invalid ${c.invalid} | uncertain ${c.uncertain} | epoch changes ${c.epochChanges} | reconnects ${state.reconnects}`;
    const sc = m ? m.scores : null;
    const lo = sc ? Math.min(...sc) : 0, hi = sc ? Math.max(...sc) : 1;
    barEls.forEach((row, i) => {
      if (spi) {
        row.style.display = i < 3 ? "" : "none";
        row.querySelector(".bl").textContent = SPI_NAMES[LABELS[i]] || LABELS[i];
      }
      const v = sc ? sc[i] : null;
      row.querySelector(".bv").textContent = v === null ? "-" : String(v);
      row.querySelector(".bf").style.width = v === null ? "0%" : `${(100 * (v - lo)) / Math.max(1, hi - lo)}%`;
      row.classList.toggle("best", Boolean(m) && i === m.argmax);
    });
  }
  requestAnimationFrame(frame);
}

main().catch((e) => {
  document.getElementById("fatal").textContent = `failed to start: ${e}`;
  document.getElementById("fatal").style.display = "block";
});
