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

// minDepth: ignore points closer than this (a wall right in front of the lens
// shouldn't win). Returns {position, forward} or null.
export function pickOpeningCamera(cameras, positions, minDepth) {
  if (!cameras || !cameras.length) return null;
  const n = positions ? positions.length / 3 : 0;
  const stride = Math.max(1, Math.floor(n / MAX_SAMPLES));
  const cosLimit = Math.cos(HALF_ANGLE);

  let best = null, bestScore = -1;
  for (const cam of cameras) {
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
