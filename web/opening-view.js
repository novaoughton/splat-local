// Where a finished room opens: from inside it, at one of the capture cameras.
//
// Framing the whole point cloud from outside puts the camera beyond the walls,
// so turning to look around faces empty space. Every capture camera is inside
// the room by construction, and its viewpoint is the one training fitted most
// closely, so it's clean. Of those, open at the one that sees the most of the
// room: the most sparse points within a cone ahead of it, past arm's length.
//
// Pure maths, no three.js. Cameras are {position: [x, y, z], rotation:
// [qw, qx, qy, qz]} (camera-to-world, COLMAP axes: z forward); positions is a
// flat [x, y, z, ...] array, all in the reconstruction's own coordinates.

const MAX_SAMPLES = 5000; // points scored per camera; plenty for ranking
const HALF_ANGLE = (32 * Math.PI) / 180; // roughly the viewer's field of view

function forwardOf([w, x, y, z]) {
  // The camera's z axis: the third column of its rotation matrix.
  return [2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)];
}

// A camera further than this from the cloud's centre, in units of the cloud's
// median spread, was misplaced by the solver: it sees the whole room in its cone
// and so would always win, but from outside it. Matches STRAY_CAMERA_SPREADS in
// server/stages/poses_colmap.py, which drops these from new runs.
const STRAY_SPREADS = 3.5;

const median = (values) => {
  const sorted = Float64Array.from(values).sort();
  return sorted.length ? sorted[sorted.length >> 1] : 0;
};

// minDepth: ignore points closer than this (a wall right in front of the lens
// shouldn't win). Returns {position, forward} or null.
export function pickOpeningCamera(cameras, positions, minDepth) {
  if (!cameras || !cameras.length) return null;
  const n = positions ? positions.length / 3 : 0;
  const stride = Math.max(1, Math.floor(n / MAX_SAMPLES));
  const cosLimit = Math.cos(HALF_ANGLE);

  const sample = [];
  for (let i = 0; i < n; i += stride) sample.push(i);
  const centre = [0, 1, 2].map((a) => median(sample.map((i) => positions[i * 3 + a])));
  const spread = median(sample.map((i) => Math.hypot(
    positions[i * 3] - centre[0], positions[i * 3 + 1] - centre[1], positions[i * 3 + 2] - centre[2])));
  const inRoom = cameras.filter((cam) => !(spread > 0) ||
    Math.hypot(cam.position[0] - centre[0], cam.position[1] - centre[1], cam.position[2] - centre[2]) <= STRAY_SPREADS * spread);

  let best = null, bestScore = -1;
  for (const cam of inRoom.length ? inRoom : cameras) {
    const [cx, cy, cz] = cam.position;
    const [fx, fy, fz] = forwardOf(cam.rotation);
    let score = 0;
    for (let i = 0; i < n; i += stride) {
      const vx = positions[i * 3] - cx, vy = positions[i * 3 + 1] - cy, vz = positions[i * 3 + 2] - cz;
      const depth = vx * fx + vy * fy + vz * fz;
      if (depth < minDepth) continue;
      if (depth > cosLimit * Math.hypot(vx, vy, vz)) score++;
    }
    if (score > bestScore) { bestScore = score; best = { position: [cx, cy, cz], forward: [fx, fy, fz] }; }
  }
  return best;
}
