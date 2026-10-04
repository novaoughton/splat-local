// The in-app viewer: shows a reconstruction as it is built.
//
// Three layers arrive in order, each replacing the last — the COLMAP sparse
// point cloud, the camera frusta, then training checkpoints as Brush exports
// them. Scene setup, navigation and the frame loop come from the shared rig.
import * as THREE from "three";
import { SplatMesh } from "@sparkjsdev/spark";
import { createRig, LOD, percentile } from "splat-viewer/core.js";
import { parsePLYAsync } from "splat-viewer/ply.js";
import { estimateUp } from "./level.js";
import { pickOpeningCamera } from "./opening-view.js";

// The rig's default: COLMAP is +Y down, so the world group is turned 180° about X.
const FLIP = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI);

export function createViewer(canvas) {
  const rig = createRig(canvas);
  const { world, camera, controls } = rig;

  let pointCloud = null, frusta = null, splat = null;
  let frustaVisible = true;
  let loading = false, nextUrl = null, currentCheckpointUrl = null;
  let home = null; // the framing frame() chose, for resetView()
  let lastPositions = null; // the sparse cloud, to reframe once the scene is levelled
  let lastCameras = null; // capture cameras, for the opening view inside the room

  // Stand the reconstruction upright: turn the world so the up direction the
  // cameras imply (see level.js) becomes the viewer's vertical. Navigation —
  // orbit, and the arrow-key turn — works around that vertical, so without
  // this a tilted room makes every turn tilt the view as well.
  function levelWorld(up) {
    const q = FLIP.clone();
    if (up) {
      const u = new THREE.Vector3(...up).applyQuaternion(FLIP).normalize();
      q.premultiply(new THREE.Quaternion().setFromUnitVectors(u, new THREE.Vector3(0, 1, 0)));
    }
    world.quaternion.copy(q);
    world.updateMatrixWorld(true);
    rig.invalidate();
  }

  // Drop a layer and free its GPU memory. SplatMesh owns its buffers and cleans
  // up after itself; plain three.js objects need their geometry and material
  // released by hand.
  function discard(obj) {
    if (!obj) return null;
    world.remove(obj);
    if (obj.dispose) obj.dispose();
    else { obj.geometry.dispose(); obj.material.dispose(); }
    rig.invalidate(); // every removal is a scene change the rig cannot see
    return null;
  }

  // Frame the scene from the sparse cloud (reconstruction-space positions,
  // outliers clamped). With the capture cameras known it opens inside the
  // room at one of them (see opening-view.js); before that — or for footage
  // with no cameras — it looks in on the whole cloud from outside.
  const MAX_PITCH = THREE.MathUtils.degToRad(10), MIN_PITCH = THREE.MathUtils.degToRad(-20);
  function frame() {
    const positions = lastPositions;
    if (!positions) return;
    const n = positions.length / 3;
    const xs = new Array(n), ys = new Array(n), zs = new Array(n);
    for (let i = 0; i < n; i++) {
      xs[i] = positions[i * 3];
      ys[i] = positions[i * 3 + 1];
      zs[i] = positions[i * 3 + 2];
    }
    const lo = [percentile(xs, 0.05), percentile(ys, 0.05), percentile(zs, 0.05)];
    const hi = [percentile(xs, 0.95), percentile(ys, 0.95), percentile(zs, 0.95)];
    // The centre in viewer space, through the world group's flip and levelling.
    const centre = new THREE.Vector3(
      (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2,
    ).applyMatrix4(world.matrixWorld);
    const size = Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], 0.05);
    rig.radius = size / 2;
    camera.near = Math.max(rig.radius * 0.01, 0.001);
    camera.far = rig.radius * 60;
    camera.updateProjectionMatrix();

    const inside = lastCameras && pickOpeningCamera(lastCameras, positions, rig.radius * 0.15);
    if (inside) {
      camera.position.set(...inside.position).applyMatrix4(world.matrixWorld);
      // Keep the capture camera's heading, but not much of its tilt: it may
      // have been pointed at the floor.
      const dir = new THREE.Vector3(...inside.forward).applyQuaternion(world.quaternion).normalize();
      let heading = new THREE.Vector3(dir.x, 0, dir.z);
      if (heading.lengthSq() < 1e-4) heading = centre.clone().sub(camera.position).setY(0); // straight down
      heading.normalize();
      const pitch = THREE.MathUtils.clamp(Math.asin(dir.y), MIN_PITCH, MAX_PITCH);
      dir.copy(heading).multiplyScalar(Math.cos(pitch)).setY(Math.sin(pitch));
      // A target just ahead: drag-orbit then pivots close by, so it reads as
      // looking around rather than swinging across the room.
      controls.target.copy(camera.position).addScaledVector(dir, rig.radius * 0.5);
    } else {
      const dist = rig.radius * 2.4;
      camera.position.set(centre.x + dist * 0.55, centre.y + dist * 0.4, centre.z + dist * 0.75);
      controls.target.copy(centre);
    }
    controls.update();
    home = { position: camera.position.clone(), target: controls.target.clone(), radius: rig.radius };
    rig.invalidate();
  }

  // Back to the framing the scene opened with; the rig's own default (the
  // origin) is the fallback before anything has been framed.
  function resetView() {
    if (!home) { rig.resetView(); return; }
    rig.radius = home.radius;
    camera.position.copy(home.position);
    camera.near = Math.max(home.radius * 0.01, 0.001);
    camera.far = home.radius * 60;
    camera.updateProjectionMatrix();
    controls.target.copy(home.target);
    controls.update();
    rig.invalidate();
  }

  function loadSparse(url) {
    fetch(url)
      .then((r) => r.arrayBuffer())
      .then(parsePLYAsync)
      .then(({ positions, colors }) => {
        lastPositions = positions;
        frame();
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
        const mat = new THREE.PointsMaterial({
          size: Math.max(rig.radius * 0.004, 0.004),
          vertexColors: true,
          sizeAttenuation: true,
        });
        pointCloud = discard(pointCloud);
        pointCloud = new THREE.Points(geo, mat);
        pointCloud.visible = !splat; // a checkpoint supersedes the sparse cloud
        world.add(pointCloud);
        rig.invalidate();
      })
      .catch((e) => console.error("sparse cloud load failed:", e));
  }

  function setCameras(cameras) {
    frusta = discard(frusta);
    lastCameras = cameras && cameras.length ? cameras : null;
    levelWorld(estimateUp(cameras));
    frame(); // reframe in the levelled scene, from inside it
    if (!lastCameras) return;
    const d = Math.max(rig.radius * 0.06, 0.05), hw = d * 0.5, hh = d * 0.375;
    const corners = [[-hw, -hh, -d], [hw, -hh, -d], [hw, hh, -d], [-hw, hh, -d]];
    const q = new THREE.Quaternion(), p = new THREE.Vector3(), v = new THREE.Vector3();
    const pos = [];
    for (const cam of cameras) {
      const [qw, qx, qy, qz] = cam.rotation;
      q.set(qx, qy, qz, qw); // reorder cam-to-world [qw,qx,qy,qz] -> THREE (x,y,z,w)
      p.set(cam.position[0], cam.position[1], cam.position[2]);
      const w = corners.map((c) => v.set(c[0], c[1], c[2]).applyQuaternion(q).add(p).clone());
      for (const c of w) pos.push(p.x, p.y, p.z, c.x, c.y, c.z); // apex -> corner
      for (let i = 0; i < 4; i++) { // base
        const a = w[i], b = w[(i + 1) % 4];
        pos.push(a.x, a.y, a.z, b.x, b.y, b.z);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    frusta = new THREE.LineSegments(
      geo,
      new THREE.LineBasicMaterial({ color: 0xe3a53d, transparent: true, opacity: 0.5 }),
    );
    frusta.visible = frustaVisible;
    world.add(frusta);
    rig.invalidate();
  }

  function setFrustaVisible(v) {
    frustaVisible = v;
    if (frusta) { frusta.visible = v; rig.invalidate(); }
  }

  // Checkpoints can land faster than they decode; keep only the newest pending
  // one so the viewer never falls behind training.
  function loadCheckpoint(url) {
    if (url === currentCheckpointUrl) return;
    if (loading) { nextUrl = url; return; }
    loading = true;
    doLoadCheckpoint(url);
  }

  async function doLoadCheckpoint(url) {
    try {
      const mesh = new SplatMesh({ url, lod: LOD });
      await mesh.initialized;
      world.add(mesh);
      splat = discard(splat);
      splat = mesh;
      currentCheckpointUrl = url;
      if (pointCloud) pointCloud.visible = false;
      rig.invalidate();
    } catch (e) {
      console.error("checkpoint load failed:", e);
    } finally {
      loading = false;
      if (nextUrl) { const u = nextUrl; nextUrl = null; loadCheckpoint(u); }
    }
  }

  function reset() {
    pointCloud = discard(pointCloud);
    frusta = discard(frusta);
    splat = discard(splat);
    currentCheckpointUrl = null;
    nextUrl = null;
    loading = false;
    home = null;
    lastPositions = null;
    lastCameras = null;
    levelWorld(null);
    rig.resetView();
  }

  return { loadSparse, setCameras, setFrustaVisible, loadCheckpoint, reset, resetView };
}
