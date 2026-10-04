"""Move a mesh from Object Capture's coordinate frame into COLMAP's, onto the splat.

Object Capture solves its own cameras, so its mesh comes out at an arbitrary
position, rotation and scale. It also reports the camera pose it solved for
each frame; COLMAP solved the same frames. Fitting a similarity transform
between the two sets of camera centres maps one frame onto the other.

A few frames are always solved differently by the two (on the night-time
bedroom test, 48 of 175), so the fit is refined on the frames that agree: on
that test the camera centres then matched to 0.6% of the camera path, and the
moved mesh sat within ~0.5% of the room's size of COLMAP's points (~2 cm in a
4 m room).
"""
from pathlib import Path

import numpy as np

MIN_MATCHED = 10  # fewer shared frames than this can't pin a 7-DoF transform down reliably
OUTLIER_FACTOR = 2.5  # refit without frames whose residual is over 2.5x the median
ROUNDS = 5


def similarity(src: np.ndarray, dst: np.ndarray):
    """Least-squares s, R, t with dst ≈ s·R·src + t (Umeyama)."""
    ms, md = src.mean(0), dst.mean(0)
    s0, d0 = src - ms, dst - md
    U, S, Vt = np.linalg.svd(d0.T @ s0 / len(src))
    D = np.diag([1.0, 1.0, np.sign(np.linalg.det(U @ Vt))])
    R = U @ D @ Vt
    s = float((S * np.diag(D)).sum() / (s0 ** 2).sum(1).mean())
    return s, R, md - s * R @ ms


def apply(s: float, R: np.ndarray, t: np.ndarray, points: np.ndarray) -> np.ndarray:
    return s * points @ R.T + t


def robust_similarity(src: np.ndarray, dst: np.ndarray):
    """Similarity fit, refit on the agreeing correspondences.

    Returns (s, R, t, keep, residuals), residuals measured for every pair.
    """
    keep = np.ones(len(src), bool)
    # Near-perfect agreement makes the median ~0; below this floor, residuals
    # are rounding, not disagreement.
    floor = 1e-6 * (float(np.linalg.norm(dst - dst.mean(0), axis=1).mean()) or 1.0)
    for _ in range(ROUNDS):
        s, R, t = similarity(src[keep], dst[keep])
        residuals = np.linalg.norm(apply(s, R, t, src) - dst, axis=1)
        new_keep = residuals <= max(OUTLIER_FACTOR * np.median(residuals[keep]), floor)
        if new_keep.sum() < MIN_MATCHED or (new_keep == keep).all():
            break
        keep = new_keep
    return s, R, t, keep, residuals


def fit_cameras(oc_poses: list[dict], colmap_centres: dict[str, np.ndarray]):
    """Transform from Object Capture's frame to COLMAP's, matched by frame name.

    oc_poses: [{"name": "frame_00001.jpg", "t": [x, y, z], ...}] as objcap writes them.
    Returns (s, R, t, stats). Raises ValueError when too few frames are shared.
    """
    pairs = [(p["t"], colmap_centres[p["name"]]) for p in oc_poses if p.get("name") in colmap_centres]
    if len(pairs) < MIN_MATCHED:
        raise ValueError(
            f"only {len(pairs)} frames were placed by both Object Capture and COLMAP; "
            f"at least {MIN_MATCHED} are needed to line the mesh up with the splat"
        )
    src = np.array([a for a, _ in pairs], float)
    dst = np.array([b for _, b in pairs], float)
    s, R, t, keep, residuals = robust_similarity(src, dst)
    spread = float(np.linalg.norm(dst - dst.mean(0), axis=1).mean()) or 1.0
    stats = {
        "matched": len(pairs),
        "used": int(keep.sum()),
        "residual_pct": round(100 * float(np.median(residuals[keep])) / spread, 2),
        "scale": round(s, 6),
    }
    return s, R, t, stats


def transform_obj(src: Path, dst: Path, s: float, R: np.ndarray, t: np.ndarray, mtllib: str | None = None) -> dict:
    """Rewrite an OBJ with every vertex moved by s·R·v + t and every normal turned by R.

    A uniform positive scale and a proper rotation keep the face winding, so
    faces and texture coordinates pass through untouched. Optionally renames
    the mtllib reference. Returns {"vertices": n, "triangles": n}.
    """
    vertices = triangles = 0
    with open(src) as fin, open(dst, "w") as fout:
        for line in fin:
            if line.startswith("v "):
                parts = line.split()
                p = apply(s, R, t, np.array([float(v) for v in parts[1:4]]))
                rest = " ".join(parts[4:])  # optional per-vertex colour
                fout.write(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}{' ' + rest if rest else ''}\n")
                vertices += 1
            elif line.startswith("vn "):
                n = R @ np.array([float(v) for v in line.split()[1:4]])
                n /= np.linalg.norm(n) or 1.0
                fout.write(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}\n")
            elif mtllib and line.startswith("mtllib "):
                fout.write(f"mtllib {mtllib}\n")
            else:
                if line.startswith("f "):
                    triangles += max(len(line.split()) - 3, 0)  # a polygon of k vertices is k-2 triangles
                fout.write(line)
    return {"vertices": vertices, "triangles": triangles}
