// The in-app viewer: shows a reconstruction as it is built.
//
// Layers arrive in order — the COLMAP sparse point cloud, the camera frusta,
// then training checkpoints as Brush exports them — plus, when the project
// asked for one, the Object Capture mesh, already lined up with the splat.
// The splat and the mesh are alternatives on screen (setLayer); the point
// cloud shows only until either has arrived. Scene setup, navigation and the
// frame loop come from the shared rig.
import * as THREE from "three";
import { SplatMesh } from "@sparkjsdev/spark";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";
import { createRig, LOD, percentile } from "splat-viewer/core.js";
import { parsePLYAsync } from "splat-viewer/ply.js";
import { estimateUp } from "./level.js";
import { pickOpeningCamera } from "./opening-view.js";
import { createGrid } from "./grid.js";

// The rig's default: COLMAP is +Y down, so the world group is turned 180° about X.
const FLIP = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI);
const DEG = THREE.MathUtils.DEG2RAD;
// Unity's Euler order: Z, then X, then Y.
const EULER_ORDER = "YXZ";

export function createViewer(canvas) {
  const rig = createRig(canvas);
  const { world, camera, controls } = rig;

  let pointCloud = null, frusta = null, splat = null, mesh = null;
  let layer = "splat"; // which of splat / mesh to show when both exist
  let meshUrl = null;
  let frustaVisible = true;
  let loading = false, nextUrl = null, currentCheckpointUrl = null;
  let home = null; // the framing frame() chose, for resetView()
  let lastPositions = null; // the sparse cloud, to reframe once the scene is levelled
  let lastCameras = null; // capture cameras, for the opening view inside the room
  let levelUp = null; // the up direction levelWorld() used, saved with transforms
  let baseRadius = null; // half the sparse cloud's extent, in reconstruction units

  // The user's transforms, saved with the project (baking them into downloads
  // is M3). `assets` places the splat and mesh together; `anchor` places the
  // grid, which is the frame exports will be re-expressed in: its Y up, its Z
  // forward. Both sit in the levelled frame the user sees:
  //   scene → assets → world (flip + levelling) → splat / mesh / cloud / frusta
  //   scene → anchor → grid
  const assets = new THREE.Group();
  rig.scene.remove(world);
  assets.add(world);
  rig.scene.add(assets);
  const anchor = new THREE.Group();
  const grid = createGrid();
  anchor.add(grid.object);
  anchor.visible = false;
  rig.scene.add(anchor);
  assets.rotation.order = anchor.rotation.order = EULER_ORDER;
  let gridCell = 1;

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
    levelUp = up ? [...up] : null;
    world.quaternion.copy(q);
    assets.updateMatrixWorld(true);
    rig.invalidate();
  }

  // Drop a layer and free its GPU memory. SplatMesh owns its buffers and cleans
  // up after itself; plain three.js objects need their geometry and material
  // released by hand.
  function discard(obj) {
    if (!obj) return null;
    world.remove(obj);
    if (obj.dispose) obj.dispose();
    else {
      obj.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          if (o.material.map) o.material.map.dispose();
          o.material.dispose();
        }
      });
    }
    rig.invalidate(); // every removal is a scene change the rig cannot see
    return null;
  }

  // Splat or mesh, whichever is chosen; failing that whichever exists; the
  // sparse cloud only while neither has arrived.
  function applyVisibility() {
    const showMesh = !!mesh && (layer === "mesh" || !splat);
    if (mesh) mesh.visible = showMesh;
    if (splat) splat.visible = !showMesh;
    if (pointCloud) pointCloud.visible = !splat && !mesh;
    rig.invalidate();
  }

  function setLayer(name) {
    layer = name;
    applyVisibility();
  }

  // The Object Capture mesh, in the same reconstruction coordinates as the
  // splat (the mesh stage moved it there), so it inherits the world group's
  // levelling. Drawn unlit: the photos already carry the room's lighting.
  async function loadMesh(url) {
    if (url === meshUrl) return;
    meshUrl = url;
    const dir = url.slice(0, url.lastIndexOf("/") + 1);
    try {
      const text = await (await fetch(url)).text();
      const loader = new OBJLoader();
      const mtlName = (text.match(/^mtllib\s+(.+)$/m) || [])[1];
      if (mtlName) {
        const materials = await new MTLLoader().setPath(dir).loadAsync(mtlName.trim());
        materials.preload();
        loader.setMaterials(materials);
      }
      const obj = loader.parse(text);
      let triangles = 0;
      obj.traverse((o) => {
        if (!o.isMesh) return;
        const old = Array.isArray(o.material) ? o.material[0] : o.material;
        const map = old && old.map;
        if (map) map.colorSpace = THREE.SRGBColorSpace;
        o.material = new THREE.MeshBasicMaterial(map ? { map, side: THREE.DoubleSide } : { color: 0x9a9a9a, side: THREE.DoubleSide });
        if (old && old !== o.material) old.dispose();
        const g = o.geometry;
        triangles += (g.index ? g.index.count : g.attributes.position.count) / 3;
      });
      if (url !== meshUrl) { discard(obj); return; } // superseded while loading
      mesh = discard(mesh);
      mesh = obj;
      mesh.userData.triangles = Math.round(triangles);
      world.add(mesh);
      applyVisibility();
    } catch (e) {
      console.error("mesh load failed:", e);
    }
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
    baseRadius = size / 2;
    rig.radius = baseRadius * assets.scale.x;
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
    rebuildGrid();
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
        world.add(pointCloud);
        applyVisibility(); // a checkpoint or a mesh supersedes the sparse cloud
      })
      .catch((e) => console.error("sparse cloud load failed:", e));
  }

  function setCameras(cameras) {
    frusta = discard(frusta);
    lastCameras = cameras && cameras.length ? cameras : null;
    levelWorld(estimateUp(cameras));
    frame(); // reframe in the levelled scene, from inside it
    if (!lastCameras) return;
    const d = Math.max((baseRadius ?? rig.radius) * 0.06, 0.05), hw = d * 0.5, hh = d * 0.375;
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

  // A big scene takes seconds to appear (~10 s for 1.3M splats); say so while
  // nothing is on screen yet. Later checkpoints replace a visible one silently.
  let onLoadingChange = null;
  function setLoading(on) { if (onLoadingChange) onLoadingChange(on); }

  async function doLoadCheckpoint(url) {
    const first = !splat;
    if (first) setLoading(true);
    try {
      const loaded = new SplatMesh({ url, lod: LOD });
      await loaded.initialized;
      world.add(loaded);
      splat = discard(splat);
      splat = loaded;
      currentCheckpointUrl = url;
      applyVisibility();
    } catch (e) {
      console.error("checkpoint load failed:", e);
    } finally {
      if (first) setLoading(false);
      loading = false;
      if (nextUrl) { const u = nextUrl; nextUrl = null; loadCheckpoint(u); }
    }
  }

  // --- transforms ------------------------------------------------------------
  const round = (v) => Math.round(v * 1e6) / 1e6;

  function applyPose(obj, { position = [0, 0, 0], rotation_deg = [0, 0, 0], scale } = {}) {
    obj.position.set(...position);
    obj.rotation.set(rotation_deg[0] * DEG, rotation_deg[1] * DEG, rotation_deg[2] * DEG, EULER_ORDER);
    if (scale !== undefined) obj.scale.setScalar(scale);
    obj.updateMatrixWorld(true);
  }

  function poseOf(obj, withScale) {
    const pose = {
      position: obj.position.toArray().map(round),
      rotation_deg: [obj.rotation.x, obj.rotation.y, obj.rotation.z].map((r) => round(r / DEG)),
    };
    if (withScale) pose.scale = round(obj.scale.x);
    return pose;
  }

  // Navigation speed, clipping and the grid's reach follow the asset scale.
  function assetsChanged() {
    assets.updateMatrixWorld(true);
    if (baseRadius) {
      rig.radius = baseRadius * assets.scale.x;
      camera.near = Math.max(rig.radius * 0.01, 0.001);
      camera.far = rig.radius * 60;
      camera.updateProjectionMatrix();
    }
    rebuildGrid();
    rig.invalidate();
  }

  // null resets both to identity.
  function setTransform(t) {
    applyPose(assets, { scale: 1, ...(t?.assets || {}) });
    applyPose(anchor, t?.anchor || {});
    assetsChanged();
  }

  function getTransform() {
    return { assets: poseOf(assets, true), anchor: poseOf(anchor, false), level_up: levelUp };
  }

  function rebuildGrid() {
    if (!anchor.visible) return;
    // Far enough to cover the room from wherever the anchor sits.
    const centre = new THREE.Vector3().setFromMatrixPosition(assets.matrixWorld);
    const reach = (baseRadius ?? 3) * assets.scale.x * 3 + centre.distanceTo(anchor.position);
    grid.build(gridCell, reach);
  }

  function setGrid({ visible, cell }) {
    if (cell) gridCell = cell;
    anchor.visible = visible;
    rebuildGrid();
    rig.invalidate();
  }

  // Unity-style drag handles on the assets or the anchor. Scale stays uniform:
  // splats can't take a non-uniform one.
  const gizmo = new TransformControls(camera, canvas);
  gizmo.setSize(0.8);
  rig.scene.add(gizmo.getHelper());
  let onTransformChange = null;
  let scaleBefore = 1;
  gizmo.addEventListener("change", () => rig.invalidate());
  gizmo.addEventListener("dragging-changed", (e) => { controls.enabled = !e.value; });
  gizmo.addEventListener("mouseDown", () => { scaleBefore = assets.scale.x; });
  gizmo.addEventListener("objectChange", () => {
    if (gizmo.object === assets) {
      const s = assets.scale;
      const dragged = [s.x, s.y, s.z].reduce((a, b) => (Math.abs(b - scaleBefore) > Math.abs(a - scaleBefore) ? b : a));
      s.setScalar(Math.max(dragged, 1e-6));
      assetsChanged();
    } else {
      rebuildGrid();
    }
    if (onTransformChange) onTransformChange(getTransform());
  });

  // target: "assets" | "anchor" | null; mode: "translate" | "rotate" | "scale".
  function setGizmo(target, mode = "translate") {
    if (!target) gizmo.detach();
    else {
      gizmo.attach(target === "anchor" ? anchor : assets);
      gizmo.setMode(target === "anchor" && mode === "scale" ? "translate" : mode);
    }
    rig.invalidate();
  }

  // Point the camera at the assets' or the anchor's origin, where the gizmo is;
  // either can be well away from the part of the room in view.
  function focus(target) {
    const at = new THREE.Vector3().setFromMatrixPosition((target === "anchor" ? anchor : assets).matrixWorld);
    const back = new THREE.Vector3().subVectors(camera.position, controls.target).normalize();
    controls.target.copy(at);
    camera.position.copy(at).addScaledVector(back, rig.radius * 0.4);
    controls.update();
    rig.invalidate();
  }

  // Put the anchor (and the grid) on a point picked in the scene, e.g. the floor.
  function placeAnchor(point) {
    anchor.position.copy(point);
    anchor.updateMatrixWorld(true);
    rebuildGrid();
    rig.invalidate();
  }

  // Level the anchor to the floor the user clicked. Three picked points alone
  // make a poor plane: near the camera the floor's splats are large and soft,
  // picks land centimetres off, and a small triangle then tilts by tens of
  // degrees (26° on the library room). So the picks only say *where* the floor
  // is: the plane is fitted to the sparse cloud's points around them, near
  // their height, with outliers trimmed. If too few points are there, or the
  // fit disagrees with the room's own level by over 15°, the room's level wins.
  // The anchor moves onto the plane under the picks' centre and keeps its
  // heading. Returns {ok, method, points, tiltDeg}.
  function setAnchorFromPlane(picks) {
    const c = new THREE.Vector3();
    picks.forEach((p) => c.add(p));
    c.divideScalar(picks.length);
    let span = 0;
    for (const a of picks) for (const b of picks) span = Math.max(span, a.distanceTo(b));
    const reach = Math.max(span, rig.radius * 0.15);
    const band = Math.min(reach * 0.25, rig.radius * 0.03);

    const xs = [], ys = [], zs = [];
    if (lastPositions) {
      const v = new THREE.Vector3();
      for (let i = 0; i < lastPositions.length; i += 3) {
        v.set(lastPositions[i], lastPositions[i + 1], lastPositions[i + 2]).applyMatrix4(world.matrixWorld);
        if (Math.abs(v.y - c.y) < band && Math.hypot(v.x - c.x, v.z - c.z) < reach) { xs.push(v.x); ys.push(v.y); zs.push(v.z); }
      }
    }
    // y = a·x + b·z + d by least squares, trimming points far off the plane.
    let fit = null, keep = xs.map(() => true);
    for (let round = 0; round < 4 && keep.filter(Boolean).length >= 20; round++) {
      const S = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], r = [0, 0, 0];
      xs.forEach((x, i) => {
        if (!keep[i]) return;
        const row = [x, zs[i], 1];
        for (let j = 0; j < 3; j++) { r[j] += row[j] * ys[i]; for (let k = 0; k < 3; k++) S[j][k] += row[j] * row[k]; }
      });
      fit = solve3(S, r);
      if (!fit) break;
      const res = xs.map((x, i) => Math.abs(fit[0] * x + fit[1] * zs[i] + fit[2] - ys[i]));
      const kept = res.filter((_, i) => keep[i]).sort((p, q) => p - q);
      const limit = 2.5 * kept[kept.length >> 1] + 1e-9;
      keep = res.map((d) => d <= limit);
    }
    let n = fit ? new THREE.Vector3(-fit[0], 1, -fit[1]).normalize() : null;
    const tiltDeg = n ? THREE.MathUtils.radToDeg(n.angleTo(new THREE.Vector3(0, 1, 0))) : null;
    const method = n && tiltDeg <= 15 ? "fit" : "level";
    if (method === "level") n = new THREE.Vector3(0, 1, 0);
    const height = method === "fit" ? fit[0] * c.x + fit[1] * c.z + fit[2] : c.y;

    const z = new THREE.Vector3(0, 0, 1).applyQuaternion(anchor.quaternion);
    z.addScaledVector(n, -z.dot(n));
    if (z.lengthSq() < 1e-8) z.set(1, 0, 0).addScaledVector(n, -n.x); // heading was along the normal
    z.normalize();
    const x = new THREE.Vector3().crossVectors(n, z);
    anchor.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, n, z));
    placeAnchor(new THREE.Vector3(c.x, height, c.z));
    return { ok: true, method, points: keep.filter(Boolean).length, tiltDeg };
  }

  function solve3(M, b) {
    const det = (A) => A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1])
      - A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0]) + A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0]);
    const d = det(M);
    if (Math.abs(d) < 1e-12) return null;
    return [0, 1, 2].map((col) => det(M.map((row, i) => row.map((v, k) => (k === col ? b[i] : v)))) / d);
  }

  // --- picking points ----------------------------------------------------------
  // Click `count` points on whatever is showing; onDone gets them in scene
  // coordinates. A click is a press and release that barely moved, so orbit
  // drags still work meanwhile.
  const raycaster = new THREE.Raycaster();
  const measureGroup = new THREE.Group();
  measureGroup.renderOrder = 20;
  rig.scene.add(measureGroup);
  let measure = null; // { points, count, onPoint, onDone }
  let pressedAt = null;

  function pickPoint(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    raycaster.setFromCamera(new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    ), camera);
    raycaster.params.Points.threshold = rig.radius * 0.01;
    const targets = [mesh && mesh.visible && mesh, splat && splat.visible && splat, pointCloud].filter(Boolean);
    for (const target of targets) {
      try {
        const hit = raycaster.intersectObject(target, true)[0];
        if (hit) return hit.point.clone();
      } catch (e) {
        console.warn("measure: picking failed on", target.type, e);
      }
    }
    return null;
  }

  function drawMeasure() {
    measureGroup.children.slice().forEach((o) => { measureGroup.remove(o); o.geometry.dispose(); o.material.dispose(); });
    if (!measure || !measure.points.length) { rig.invalidate(); return; }
    const pos = measure.points.flatMap((p) => p.toArray());
    const geo = () => new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    const style = { color: 0xe3a53d, depthTest: false, transparent: true };
    measureGroup.add(new THREE.Points(geo(), new THREE.PointsMaterial({ ...style, size: 9, sizeAttenuation: false })));
    if (measure.points.length === 2) measureGroup.add(new THREE.Line(geo(), new THREE.LineBasicMaterial(style)));
    measureGroup.children.forEach((o) => { o.renderOrder = 20; });
    rig.invalidate();
  }

  canvas.addEventListener("pointerdown", (e) => { pressedAt = measure ? [e.clientX, e.clientY] : null; });
  canvas.addEventListener("pointerup", (e) => {
    if (!measure || !pressedAt || measure.points.length >= measure.count) return;
    const moved = Math.hypot(e.clientX - pressedAt[0], e.clientY - pressedAt[1]);
    pressedAt = null;
    if (moved > 4) return;
    const p = pickPoint(e.clientX, e.clientY);
    if (measure.onPoint) measure.onPoint(measure.points.length, !!p);
    if (!p) return;
    measure.points.push(p);
    drawMeasure();
    if (measure.points.length === measure.count) measure.onDone(measure.points.map((v) => v.clone()));
  });

  function startMeasure({ count = 2, onPoint, onDone }) {
    measure = { points: [], count, onPoint, onDone };
    drawMeasure();
  }

  function clearMeasure() {
    measure = null;
    drawMeasure();
  }

  function reset() {
    clearMeasure();
    setGizmo(null);
    anchor.visible = false;
    baseRadius = null;
    setTransform(null);
    pointCloud = discard(pointCloud);
    frusta = discard(frusta);
    splat = discard(splat);
    mesh = discard(mesh);
    meshUrl = null;
    layer = "splat";
    currentCheckpointUrl = null;
    nextUrl = null;
    loading = false;
    home = null;
    lastPositions = null;
    lastCameras = null;
    levelWorld(null);
    rig.resetView();
  }

  return {
    loadSparse, setCameras, setFrustaVisible, loadCheckpoint, loadMesh, setLayer, reset, resetView,
    setTransform, getTransform, setGrid, setGizmo, startMeasure, clearMeasure, focus, placeAnchor, setAnchorFromPlane,
    set onTransformChange(fn) { onTransformChange = fn; },
    set onLoadingChange(fn) { onLoadingChange = fn; },
    get meshTriangles() { return mesh ? mesh.userData.triangles : null; },
  };
}
