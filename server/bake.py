"""Unity export: the splat and mesh re-expressed in the frame the user set up in the viewer.

The viewer draws   scene → assets (A) → world (W) → content (COLMAP coordinates)
and the grid as    scene → anchor (N).
W turns COLMAP's +Y-down frame upright (180° about X) and levels it (web/viewer.js
levelWorld). A places the splat and mesh together: position, rotation in degrees
applied Z, then X, then Y (three.js "YXZ", Unity's order), and a uniform scale, which
measuring sets so that units are metres. N is the anchor.

The export is p' = N⁻¹ · A · W · p: right-handed, +Y up, +Z the anchor's forward,
the anchor at the origin. Unity's handedness conversion is the importer's job (OBJ)
or the splat plugin's (checked per plugin in M4).

Pure numpy apart from run(), so the maths is testable without the pipeline's tools.
"""
import json
import math
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

SPLAT_NAME = "scene-unity.ply"
MESH_ZIP_NAME = "mesh-unity.zip"
TRANSFORM_NAME = "unity-transform.json"
FILE_NAMES = (SPLAT_NAME, MESH_ZIP_NAME, TRANSFORM_NAME)

FLIP = np.diag([1.0, -1.0, -1.0])  # the rig's world group: 180° about X
# splat-transform works in the PLY frame turned 180° about z (x and y negated);
# see the M3 note in the tracker. F is its own inverse.
_F = np.diag([-1.0, -1.0, 1.0])


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def euler_yxz(rotation_deg) -> np.ndarray:
    """three.js Euler order "YXZ": the matrix Ry·Rx·Rz (Z applied first)."""
    x, y, z = (math.radians(v) for v in rotation_deg)
    return _rot_y(y) @ _rot_x(x) @ _rot_z(z)


def pose_matrix(position, rotation_deg, scale: float = 1.0) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = euler_yxz(rotation_deg) * scale
    m[:3, 3] = position
    return m


def _quat_matrix(x, y, z, w) -> np.ndarray:
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def from_unit_vectors(a, b) -> np.ndarray:
    """The rotation taking unit vector a onto b: three.js Quaternion.setFromUnitVectors."""
    r = float(np.dot(a, b)) + 1
    if r < 1e-8:  # opposite: any half-turn about a perpendicular axis, as three picks it
        q = (-a[1], a[0], 0.0, 0.0) if abs(a[0]) > abs(a[2]) else (0.0, -a[2], a[1], 0.0)
    else:
        c = np.cross(a, b)
        q = (c[0], c[1], c[2], r)
    q = np.array(q, dtype=float)
    return _quat_matrix(*(q / np.linalg.norm(q)))


def world_rotation(level_up) -> np.ndarray:
    """W: the flip upright, then the levelling that turns the scene's up onto +Y."""
    if level_up is None:
        return FLIP.copy()
    u = FLIP @ np.asarray(level_up, dtype=float)
    u /= np.linalg.norm(u)
    return from_unit_vectors(u, np.array([0.0, 1.0, 0.0])) @ FLIP


def export_matrix(transform: dict) -> np.ndarray:
    assets, anchor = transform["assets"], transform["anchor"]
    a = pose_matrix(assets["position"], assets["rotation_deg"], assets["scale"])
    n = pose_matrix(anchor["position"], anchor["rotation_deg"])
    w = np.eye(4)
    w[:3, :3] = world_rotation(transform.get("level_up"))
    return np.linalg.inv(n) @ a @ w


def export_similarity(transform: dict):
    """(s, R, t) with p' = s·R·p + t."""
    m = export_matrix(transform)
    s = float(np.cbrt(np.linalg.det(m[:3, :3])))
    return s, m[:3, :3] / s, m[:3, 3].copy()


def euler_zyx_deg(R: np.ndarray):
    """Angles (x, y, z) in degrees with R = Rz·Ry·Rx: splat-transform's -r."""
    sy = -float(np.clip(R[2, 0], -1.0, 1.0))
    y = math.asin(sy)
    if abs(sy) < 1 - 1e-9:
        x = math.atan2(R[2, 1], R[2, 2])
        z = math.atan2(R[1, 0], R[0, 0])
    else:  # gimbal lock: fold z into x
        x = math.atan2(-R[1, 2], R[1, 1])
        z = 0.0
    return tuple(math.degrees(v) for v in (x, y, z))


def splat_transform_args(s: float, R: np.ndarray, t: np.ndarray) -> list[str]:
    """splat-transform actions that move PLY points by p' = s·R·p + t.

    Its frame is the PLY frame with x and y negated (F), so it gets F·R·F and F·t.
    Actions run in order: scale about the origin, rotate, then translate.
    """
    x, y, z = euler_zyx_deg(_F @ R @ _F)
    ft = _F @ t
    return [
        "-s", f"{s:.9g}",
        "-r", f"{x:.9g},{y:.9g},{z:.9g}",
        "-t", f"{ft[0]:.9g},{ft[1]:.9g},{ft[2]:.9g}",
    ]


# --- running it --------------------------------------------------------------------

def _transform_record(transform: dict, s, R, t) -> dict:
    m = np.eye(4)
    m[:3, :3] = s * R
    m[:3, 3] = t
    return {
        "frame": "right-handed, +Y up, +Z forward (the anchor), anchor at the origin",
        "units": "metres" if transform.get("scaled") else "reconstruction units (scale not measured)",
        "matrix": m.round(9).tolist(),  # row-major; p' = M · [x, y, z, 1]
        "scale": s,
        "rotation": R.round(9).tolist(),
        "translation": t.round(9).tolist(),
        "source_transform": transform,
        "baked_at": time.time(),
    }


def run(job) -> dict:
    """Write the Unity files for a finished job; return the new artifacts' entries."""
    from . import align
    from .stages import export as export_stage

    state, _ = job.snapshot()
    transform = state.get("transform")
    if not transform:
        raise ValueError("no transform saved for this project")
    s, R, t = export_similarity(transform)
    exports = job.work / "exports"
    record = _transform_record(transform, s, R, t)
    artifacts = []

    scene = exports / "scene.ply"
    if scene.is_file():
        job.update(message="exporting the splat for Unity")
        target = exports / SPLAT_NAME
        if not export_stage._transform(job, SPLAT_NAME, [str(scene), *splat_transform_args(s, R, t), str(target)]):
            raise RuntimeError("splat-transform could not write the Unity splat")
        gaussians = next((a.get("gaussians") for a in state.get("artifacts") or [] if a.get("name") == "scene.ply"), None)
        artifacts.append({"name": SPLAT_NAME, "bytes": target.stat().st_size, "gaussians": gaussians})

    mesh_dir = exports / "mesh"
    if (mesh_dir / "mesh.obj").is_file():
        job.update(message="exporting the mesh for Unity")
        with tempfile.TemporaryDirectory(dir=exports, prefix=".unity-mesh-") as temp:
            out = Path(temp)
            shape = align.transform_obj(mesh_dir / "mesh.obj", out / "mesh.obj", s, R, t)
            for f in mesh_dir.iterdir():
                if f.name != "mesh.obj" and f.is_file():
                    shutil.copyfile(f, out / f.name)
            (out / "transform.json").write_text(json.dumps(record, indent=1))
            partial = exports / (MESH_ZIP_NAME + ".part")
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(out.iterdir()):
                    zf.write(f, f"mesh-unity/{f.name}")
            partial.replace(exports / MESH_ZIP_NAME)
        artifacts.append({"name": MESH_ZIP_NAME, "bytes": (exports / MESH_ZIP_NAME).stat().st_size,
                          "triangles": shape["triangles"]})

    if not artifacts:
        raise ValueError("this project has neither a splat nor a mesh to export")
    (exports / TRANSFORM_NAME).write_text(json.dumps(record, indent=1))
    artifacts.append({"name": TRANSFORM_NAME, "bytes": (exports / TRANSFORM_NAME).stat().st_size})
    for a in artifacts:
        a["url"] = job.file_url(f"exports/{a['name']}")
    return artifacts
