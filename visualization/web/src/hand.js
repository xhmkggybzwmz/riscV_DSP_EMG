// Articulated hand built from Three.js primitives (no external model file).
// Hierarchy: root -> forearm (fixed) ; root -> wrist (flex / dev) -> palm
//   palm -> finger MCP (spread about Z, flexion about X) -> PIP -> DIP -> tip
//   palm -> thumb CMC (abduction / opposition) -> MCP -> IP -> tip
// Frame: fingers +Y, palm faces +Z; positive flexion curls towards the palm (+Z), see gestures.js.

import { FINGERS } from "./gestures.js";

const D2R = Math.PI / 180;
// segment lengths (model units ~ cm): proximal, middle, distal
const FINGER_GEOM = {
  index: { x: -2.4, base: 4.3, len: [3.9, 2.3, 1.9], r: 0.85 },
  middle: { x: -0.8, base: 4.6, len: [4.3, 2.7, 2.0], r: 0.88 },
  ring: { x: 0.8, base: 4.4, len: [4.0, 2.5, 1.9], r: 0.84 },
  little: { x: 2.3, base: 3.9, len: [3.2, 1.9, 1.7], r: 0.74 },
};

export class HandModel {
  constructor(THREE, { side = "right" } = {}) {
    this.THREE = THREE;
    this.root = new THREE.Group();
    this.skin = new THREE.MeshStandardMaterial({ color: 0xe0ac8f, roughness: 0.65, metalness: 0.0 });
    this.joint = new THREE.MeshStandardMaterial({ color: 0xc99579, roughness: 0.7 });
    this.nail = new THREE.MeshStandardMaterial({ color: 0xf2d6c9, roughness: 0.4 });
    this.forearmMat = new THREE.MeshStandardMaterial({ color: 0xd9a487, roughness: 0.7 });
    this.joints = {};
    this.materials = [this.skin, this.joint, this.nail, this.forearmMat];
    this.build();
    if (side === "left") this.root.scale.x = -1;
  }

  capsule(radius, length, mat) {
    const T = this.THREE;
    const g = new T.Group();
    const cyl = new T.Mesh(new T.CylinderGeometry(radius, radius * 0.96, length, 20), mat);
    cyl.position.y = length / 2;
    g.add(cyl);
    const end = new T.Mesh(new T.SphereGeometry(radius * 0.96, 20, 14), mat);
    end.position.y = length;
    g.add(end);
    return g;
  }

  build() {
    const T = this.THREE;
    // forearm (does not move with the wrist)
    const fore = new T.Mesh(new T.CylinderGeometry(2.4, 2.9, 12, 28), this.forearmMat);
    fore.scale.z = 0.62;
    fore.position.y = -6.2;
    this.root.add(fore);
    // wrist joint
    const wrist = new T.Group();
    this.root.add(wrist);
    this.joints.wrist = wrist;
    const wristBall = new T.Mesh(new T.SphereGeometry(2.25, 24, 16), this.joint);
    wristBall.scale.set(1.25, 0.7, 0.75);
    wrist.add(wristBall);
    // palm
    const palm = new T.Mesh(new T.BoxGeometry(6.8, 6.2, 2.4, 2, 2, 2), this.skin);
    palm.position.y = 3.3;
    wrist.add(palm);
    const palmPad = new T.Mesh(new T.SphereGeometry(1, 20, 14), this.skin);
    palmPad.scale.set(3.3, 3.0, 0.9);
    palmPad.position.set(0, 3.0, 0.75);
    wrist.add(palmPad);
    // fingers
    for (const name of FINGERS) {
      const g = FINGER_GEOM[name];
      const mcp = new T.Group();
      mcp.position.set(g.x, g.base + 2.0, 0);
      wrist.add(mcp);
      const knuckle = new T.Mesh(new T.SphereGeometry(g.r * 1.05, 18, 12), this.joint);
      mcp.add(knuckle);
      const segs = [];
      let parent = mcp;
      g.len.forEach((L, i) => {
        const jointGroup = i === 0 ? mcp : new T.Group();
        if (i > 0) {
          jointGroup.position.y = g.len[i - 1];
          parent.add(jointGroup);
        }
        const r = g.r * (1 - 0.1 * i);
        jointGroup.add(this.capsule(r, L, this.skin));
        if (i === 2) {
          const nail = new T.Mesh(new T.BoxGeometry(r * 1.2, L * 0.55, 0.12), this.nail);
          nail.position.set(0, L * 0.62, -r * 0.92);
          jointGroup.add(nail);
        }
        segs.push(jointGroup);
        parent = jointGroup;
      });
      this.joints[name] = { mcp: segs[0], pip: segs[1], dip: segs[2] };
    }
    // thumb: CMC at the radial (-X) base of the palm, pointing up and out
    const cmc = new T.Group();
    cmc.position.set(-3.2, 1.2, 0.6);
    wrist.add(cmc);
    const thenar = new T.Mesh(new T.SphereGeometry(1, 18, 12), this.skin);
    thenar.scale.set(1.5, 2.1, 1.1);
    thenar.position.set(0.6, 0.8, 0.3);
    cmc.add(thenar);
    // CMC as three nested rotations: abduction in the palm plane (Z), palmar tilt (X), axial
    // rotation of the thumb (Y, opposition): then MCP / IP flexion curls across the palm
    const abd = new T.Group();
    cmc.add(abd);
    const fwd = new T.Group();
    abd.add(fwd);
    const meta = new T.Group();
    fwd.add(meta);
    meta.add(this.capsule(1.0, 3.2, this.skin));
    const tMcp = new T.Group();
    tMcp.position.y = 3.2;
    meta.add(tMcp);
    tMcp.add(this.capsule(0.95, 2.9, this.skin));
    const tIp = new T.Group();
    tIp.position.y = 2.9;
    tMcp.add(tIp);
    tIp.add(this.capsule(0.9, 2.3, this.skin));
    const tNail = new T.Mesh(new T.BoxGeometry(1.0, 1.2, 0.12), this.nail);
    tNail.position.set(0, 1.4, -0.85);
    tIp.add(tNail);
    this.joints.thumb = { abd, fwd, twist: meta, mcp: tMcp, ip: tIp };
    this.root.position.y = -1.5;
  }

  // pose: gestures.js structure (degrees)
  apply(pose) {
    const j = this.joints;
    j.wrist.rotation.set(pose.wrist.flex * D2R, 0, pose.wrist.dev * D2R);
    for (const name of FINGERS) {
      const p = pose.fingers[name];
      j[name].mcp.rotation.set(p.mcp * D2R, 0, p.spread * D2R, "ZXY");
      j[name].pip.rotation.set(p.pip * D2R, 0, 0);
      j[name].dip.rotation.set(p.dip * D2R, 0, 0);
    }
    // thumb: cmcAbd = angle from the palm's long axis towards the radial side (palm plane);
    // cmcOpp = opposition: palmar tilt and axial rotation, so flexion curls towards the little finger
    const t = pose.thumb;
    j.thumb.abd.rotation.set(0, 0, t.cmcAbd * D2R);
    j.thumb.fwd.rotation.set((10 + 0.7 * t.cmcOpp) * D2R, 0, 0);
    j.thumb.twist.rotation.set(0, 1.2 * t.cmcOpp * D2R, 0);
    j.thumb.mcp.rotation.set(t.mcp * D2R, 0, 0);
    j.thumb.ip.rotation.set(t.ip * D2R, 0, 0);
  }

  setDimmed(on) {
    for (const m of this.materials) {
      m.transparent = on;
      m.opacity = on ? 0.45 : 1.0;
    }
  }
}
