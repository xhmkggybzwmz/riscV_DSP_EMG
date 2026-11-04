// Six preset hand poses (E2/E3 class order) and the pose interpolation.
// A preset is a visual target for a classification result, not a reconstruction of joint angles.
//
// Angles in degrees. Model frame (hand.js): fingers point +Y, palm faces +Z (towards the viewer).
// Finger flexion = rotation about the joint's X axis, positive curls towards the palm (+Z).
// spread = rotation about Z at the MCP (positive leans towards the thumb side, -X; fanning out =
//          index / middle positive, ring / little negative).
// thumb: cmcAbd (angle from the palm axis towards the radial side, in the palm plane), cmcOpp
//        (opposition: palmar tilt + axial rotation), mcp, ip (flexion).
// wrist: flex > 0 bends the hand towards the palm side (flexion), < 0 extension; dev = radial/ulnar.

export const LABELS = ["REST", "HAND_CLOSE", "HAND_OPEN", "WRIST_FLEX", "WRIST_EXT", "PINCH"];
export const FINGERS = ["index", "middle", "ring", "little"];

const f = (mcp, pip, dip, spread = 0) => ({ mcp, pip, dip, spread });

export const POSES = {
  REST: {
    fingers: { index: f(18, 22, 12, 2), middle: f(20, 25, 14, 0), ring: f(24, 28, 15, -2), little: f(28, 30, 16, -5) },
    thumb: { cmcAbd: 40, cmcOpp: 25, mcp: 15, ip: 10 },
    wrist: { flex: 8, dev: 0 },
  },
  HAND_CLOSE: {
    fingers: { index: f(88, 100, 65, 0), middle: f(90, 102, 65, 0), ring: f(90, 100, 62, 0), little: f(88, 96, 60, 0) },
    thumb: { cmcAbd: 25, cmcOpp: 70, mcp: 45, ip: 40 },
    wrist: { flex: 0, dev: 0 },
  },
  HAND_OPEN: {
    fingers: { index: f(-8, 0, 0, 12), middle: f(-8, 0, 0, 2), ring: f(-8, 0, 0, -9), little: f(-8, 0, 0, -20) },
    thumb: { cmcAbd: 60, cmcOpp: 0, mcp: 0, ip: 0 },
    wrist: { flex: -5, dev: 0 },
  },
  WRIST_FLEX: {
    fingers: { index: f(20, 25, 12, 2), middle: f(22, 28, 14, 0), ring: f(26, 30, 15, -2), little: f(30, 32, 16, -5) },
    thumb: { cmcAbd: 40, cmcOpp: 25, mcp: 15, ip: 10 },
    wrist: { flex: 65, dev: 0 },
  },
  WRIST_EXT: {
    fingers: { index: f(5, 10, 5, 4), middle: f(5, 10, 5, 0), ring: f(6, 12, 6, -4), little: f(8, 12, 6, -8) },
    thumb: { cmcAbd: 50, cmcOpp: 10, mcp: 5, ip: 5 },
    wrist: { flex: -55, dev: 0 },
  },
  PINCH: {
    fingers: { index: f(52, 48, 22, -4), middle: f(25, 30, 15, 0), ring: f(28, 32, 16, -3), little: f(32, 34, 17, -6) },
    thumb: { cmcAbd: 35, cmcOpp: 60, mcp: 25, ip: 20 },
    wrist: { flex: 5, dev: 0 },
  },
};

// flat joint vector (stable order) <-> pose
export const JOINTS = [
  ...FINGERS.flatMap((n) => ["mcp", "pip", "dip", "spread"].map((j) => `${n}.${j}`)),
  "thumb.cmcAbd", "thumb.cmcOpp", "thumb.mcp", "thumb.ip", "wrist.flex", "wrist.dev",
];

export function poseVector(pose) {
  return JOINTS.map((k) => {
    const [a, b] = k.split(".");
    if (a === "thumb") return pose.thumb[b];
    if (a === "wrist") return pose.wrist[b];
    return pose.fingers[a][b];
  });
}

export function vectorPose(v) {
  const p = { fingers: {}, thumb: {}, wrist: {} };
  JOINTS.forEach((k, i) => {
    const [a, b] = k.split(".");
    if (a === "thumb") p.thumb[b] = v[i];
    else if (a === "wrist") p.wrist[b] = v[i];
    else (p.fingers[a] = p.fingers[a] || {})[b] = v[i];
  });
  return p;
}

export function poseFor(label) {
  return POSES[label] || null;
}

// First-order (exponential) approach towards the target, frame-rate independent:
// after dt seconds a fraction 1 - exp(-dt / tau) of the remaining error is removed;
// 95 % of a step is reached after 3 tau (tau 0.12 s -> 0.36 s). This is display smoothing only.
export function stepTowards(current, target, dt, tau = 0.12) {
  const a = tau <= 0 ? 1 : 1 - Math.exp(-Math.max(0, dt) / tau);
  return current.map((c, i) => c + (target[i] - c) * a);
}

export function maxError(a, b) {
  return a.reduce((m, x, i) => Math.max(m, Math.abs(x - b[i])), 0);
}
