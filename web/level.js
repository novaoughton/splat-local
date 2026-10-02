// Which way is up? COLMAP places a reconstruction at an arbitrary angle — it
// has no idea where gravity points — so a room can arrive tilted by 30° or
// more, and turning around the viewer's vertical then also tilts the view.
//
// People film with the phone level side to side, even while tilting it up
// and down, so every camera's right-hand axis is roughly horizontal: the
// vertical is the direction most perpendicular to all of them (the smallest
// eigenvector of Σ r rᵀ). Unlike averaging the cameras' up vectors, pitching
// down at a desk or floor doesn't bias it. Checked against the largest plane
// in each test scene's point cloud: within 1–2° for the desk and the
// re-filmed bedroom. The plain average stays as the fallback for footage
// where the right axes don't pin a direction down (all from one viewpoint).
//
// Pure maths, no three.js: cameras are {rotation: [qw, qx, qy, qz]}
// (camera-to-world, COLMAP axes: x right, y down, z forward). Returns the up
// direction in the reconstruction's own coordinates, or null.

function rotate([w, x, y, z], [vx, vy, vz]) {
  // v + 2w(q×v) + 2q×(q×v), with q = (x, y, z)
  const cx = y * vz - z * vy, cy = z * vx - x * vz, cz = x * vy - y * vx;
  return [
    vx + 2 * (w * cx + y * cz - z * cy),
    vy + 2 * (w * cy + z * cx - x * cz),
    vz + 2 * (w * cz + x * cy - y * cx),
  ];
}

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const norm = (a) => scale(a, 1 / Math.hypot(...a));
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const mul = (M, v) => M.map((row) => dot(row, v));

function solve3(M, b) {
  // Cramer's rule; M is symmetric positive (semi)definite here.
  const det = (A) => dot(A[0], cross(A[1], A[2]));
  const d = det(M);
  if (Math.abs(d) < 1e-18) return null;
  return [0, 1, 2].map((c) => det(M.map((row, r) => row.map((v, k) => (k === c ? b[r] : v)))) / d);
}

export function estimateUp(cameras) {
  if (!cameras || cameras.length < 3) return null;
  const n = cameras.length;
  let meanUp = [0, 0, 0];
  const M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (const cam of cameras) {
    const q = cam.rotation;
    const up = rotate(q, [0, -1, 0]); // image y points down
    const r = rotate(q, [1, 0, 0]);
    meanUp = meanUp.map((v, i) => v + up[i] / n);
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) M[i][j] += (r[i] * r[j]) / n;
  }
  if (Math.hypot(...meanUp) < 1e-6) return null;
  meanUp = norm(meanUp);

  // Smallest eigenvector by inverse iteration (a hair of regularisation keeps
  // a rank-2 M invertible).
  const Mr = M.map((row, i) => row.map((v, j) => v + (i === j ? 1e-9 : 0)));
  let v = meanUp;
  for (let k = 0; k < 30; k++) {
    const next = solve3(Mr, v);
    if (!next) return meanUp;
    v = norm(next);
  }
  // Is it well determined? Compare its eigenvalue with the next one up, found
  // as the smaller eigenvalue of M restricted to the plane perpendicular to v.
  const l0 = dot(v, mul(M, v));
  const a = norm(cross(v, Math.abs(v[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0]));
  const b = cross(v, a);
  const [p, q, r] = [dot(a, mul(M, a)), dot(a, mul(M, b)), dot(b, mul(M, b))];
  const l1 = (p + r) / 2 - Math.sqrt(((p - r) / 2) ** 2 + q * q);
  if (!(l1 > 0) || l0 / l1 > 0.3) return meanUp;
  return dot(v, meanUp) < 0 ? scale(v, -1) : v;
}
