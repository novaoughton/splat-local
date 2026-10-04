"""Mesh stage: Apple Object Capture on the selected frames, moved onto the splat.

Runs tools/objcap (a small Swift CLI around RealityKit's PhotogrammetrySession)
on the frames the frames stage picked, then uses the camera poses it reports
to move the mesh into COLMAP's coordinates (see align.py), so splat and mesh
line up. Writes exports/mesh/ (mesh.obj, mesh.mtl, texture maps) and
exports/mesh.zip for download.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from .. import align
from ..pipeline import JobCancelled

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = _PROJECT_ROOT / "tools" / "objcap" / "main.swift"
INPUT_DIR = "mesh_input"  # symlinks to the selected frames: Object Capture wants a folder of images only
RAW_DIR = "mesh_raw"  # Object Capture's own output, in its own coordinate frame
EXPORT_DIR = "exports/mesh"
ZIP_NAME = "mesh.zip"
_TEXTURE_SUFFIXES = (".png", ".jpg", ".jpeg")


def resolve_bin() -> Path:
    env = os.environ.get("OBJCAP_BIN")
    return Path(env) if env else _PROJECT_ROOT / "vendor" / "objcap"


def ensure_built(job) -> Path:
    """The objcap binary, compiled from tools/objcap if missing or older than its source."""
    bin_path = resolve_bin()
    if bin_path.exists() and bin_path.stat().st_mtime >= SOURCE.stat().st_mtime:
        return bin_path
    job.update(message="building the Object Capture helper (first run only)")
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["swiftc", "-O", str(SOURCE), "-o", str(bin_path)],
                       check=True, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RuntimeError("objcap could not be built: swiftc not found; "
                           "install the Xcode Command Line Tools (xcode-select --install)")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("objcap failed to build:\n" + (exc.stderr or "")[-2000:])
    return bin_path


def colmap_centres(work: Path) -> dict:
    """Camera centre per frame name, from the undistorted COLMAP model both pose backends write."""
    import numpy as np
    import pycolmap

    rec = pycolmap.Reconstruction(str(work / "dataset" / "sparse"))
    return {im.name: np.array(im.cam_from_world().inverse().translation) for im in rec.images.values()}


def _fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _reader(pipe, q):
    for line in iter(pipe.readline, ""):
        q.put(line)
    q.put(None)


def _run_objcap(job, bin_path: Path, images: Path, out: Path, detail: str, poses: Path) -> dict:
    proc = subprocess.Popen(
        [str(bin_path), str(images), str(out), detail, str(poses)],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    job.proc = proc
    q: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    result = {"error": None, "invalid": 0, "skipped": 0}
    tail: list[str] = []
    while True:
        if job.cancelled:
            break
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if line is None:
            break
        line = line.strip()
        if line.startswith("progress "):
            fraction = float(line.split()[1])
            job.update(progress=min(fraction, 1.0) * 0.9, message=f"building the mesh ({fraction:.0%})")
        elif line.startswith("done "):
            for part in line.split()[1:]:
                key, _, value = part.partition("=")
                result[key] = int(value)
        elif line.startswith("error "):
            result["error"] = line[len("error "):]
        elif line and not line.startswith(("poses ", "model ")):
            tail = (tail + [line])[-10:]  # RealityKit's own warnings, for a failure message
    proc.wait()
    job.proc = None
    if job.cancelled:
        raise JobCancelled()
    if proc.returncode == 2:
        raise RuntimeError("Object Capture is not supported on this Mac")
    if proc.returncode != 0:
        raise RuntimeError("object capture failed: " + (result["error"] or "\n".join(tail) or f"exit {proc.returncode}"))
    return result


def run(job, work: Path, preset):
    frames = sorted((work / "frames").glob("*.jpg"))
    if not frames:
        raise RuntimeError("no frames to build a mesh from")
    images = _fresh_dir(work / INPUT_DIR)
    for frame in frames:
        (images / frame.name).symlink_to(frame.resolve())
    raw = _fresh_dir(work / RAW_DIR)
    poses_path = raw / "poses.json"

    bin_path = ensure_built(job)
    job.check_cancelled()
    job.update(message="building the mesh", progress=0.0)
    counts = _run_objcap(job, bin_path, images, raw / "model", preset.mesh_detail, poses_path)

    objs = sorted((raw / "model").glob("*.obj"))
    if not objs or not poses_path.exists():
        raise RuntimeError("object capture produced no mesh")
    job.update(message="lining the mesh up with the splat", progress=0.92)
    oc_poses = json.loads(poses_path.read_text())["poses"]
    s, R, t, stats = align.fit_cameras(oc_poses, colmap_centres(work))

    out = _fresh_dir(work / EXPORT_DIR)
    mtls = sorted((raw / "model").glob("*.mtl"))
    shape = align.transform_obj(objs[0], out / "mesh.obj", s, R, t, mtllib="mesh.mtl" if mtls else None)
    if mtls:
        shutil.copyfile(mtls[0], out / "mesh.mtl")
    for texture in (raw / "model").iterdir():
        if texture.suffix.lower() in _TEXTURE_SUFFIXES:
            shutil.copyfile(texture, out / texture.name)

    zip_path = work / "exports" / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out.iterdir()):
            zf.write(f, f"mesh/{f.name}")

    frames_used = len(frames) - counts.get("skipped", 0) - counts.get("invalid", 0)
    mesh = {
        "url": job.file_url(f"{EXPORT_DIR}/mesh.obj"),
        "triangles": shape["triangles"],
        "frames_used": frames_used,
        "frames_total": len(frames),
        "alignment": stats,
    }
    artifact = {"name": ZIP_NAME, "url": job.file_url(f"exports/{ZIP_NAME}"),
                "bytes": zip_path.stat().st_size, "triangles": shape["triangles"]}
    state, _ = job.snapshot()
    others = [a for a in (state.get("artifacts") or []) if a.get("name") != ZIP_NAME]
    job.update(mesh=mesh, artifacts=others + [artifact], progress=1.0,
               message=f"mesh ready: {shape['triangles']:,} triangles from {frames_used} of {len(frames)} frames")
