"""Saved projects: each job folder carries a project.json, so finished runs survive a restart.

The running job's live state stays in memory (pipeline.Job). project.json is a
snapshot of it, written when the job starts and again when it ends; on startup
every job folder is loaded back, so its results stay reachable and its disk
space can be reclaimed from the app.
"""
import dataclasses
import json
import os
import shutil
import time
from pathlib import Path

from . import failures
from .pipeline import TERMINAL_STAGES, Job
from .presets import DEFAULT_PRESET

PROJECT_FILE = "project.json"
SCHEMA = 1
MAX_NAME_LENGTH = 80
INTERRUPTED = "interrupted: the app stopped while this project was running"
UNSAVED = "unsaved run: this folder has no project.json (it predates saved projects, or its upload failed)"


def clean_name(name: str | None, fallback: str) -> str:
    """Collapse whitespace and cap the length; an empty name falls back."""
    name = " ".join((name or "").split())[:MAX_NAME_LENGTH]
    return name or fallback


def save(job: Job) -> None:
    """Write the job's current state to project.json, atomically."""
    state, _ = job.snapshot()
    data = {
        "schema": SCHEMA,
        "id": job.id,
        "preset": job.preset_name,
        # The values the run used, in case the preset is retuned later.
        "preset_settings": dataclasses.asdict(job.preset),
        "pose_backend": job.pose_backend,
        "outputs": job.outputs,
        "saved": time.time(),
        "state": state,
    }
    path = job.work / PROJECT_FILE
    tmp = path.with_name(PROJECT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _load(path: Path) -> Job | None:
    try:
        data = json.loads(path.read_text())
        job = Job(data["id"], data["preset"], data["pose_backend"], data.get("outputs", "splat"))
        state = dict(data["state"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    job.work = path.parent
    if state.get("stage") not in TERMINAL_STAGES:
        # A mid-run checkpoint URL may point at a preview file that was deleted.
        state.update(
            stage="error", error=INTERRUPTED, message=INTERRUPTED, checkpoint=None,
            failed_stage=state.get("stage"), failure=failures.explain(state.get("stage"), INTERRUPTED),
        )
    job.update(**state)
    return job


def _unsaved(folder: Path) -> Job:
    """A job folder with no readable project.json, listed only so it can be deleted."""
    job = Job(folder.name, DEFAULT_PRESET, "colmap")
    job.work = folder
    job.preset_name = "unknown"
    job.update(
        name=f"Unsaved run {folder.name}", created=folder.stat().st_mtime,
        stage="error", error=UNSAVED, message=UNSAVED, failure=failures.explain(None, UNSAVED),
    )
    return job


def load_all(jobs_dir: Path) -> list[Job]:
    """Every job folder under jobs_dir, as a finished Job.

    A project whose snapshot is not in a terminal stage was cut off mid-run (the
    app stopped), so it comes back as an error rather than as a job that claims
    to still be running. A folder with no readable project.json — or one made
    with a preset that no longer exists — comes back as an unsaved run.
    """
    if not jobs_dir.is_dir():
        return []
    jobs = []
    for folder in sorted(p for p in jobs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        path = folder / PROJECT_FILE
        job = _load(path) if path.is_file() else None
        jobs.append(job or _unsaved(folder))
    return jobs


def disk_bytes(folder: Path) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# Working data a finished project no longer needs to be viewed or downloaded:
# training snapshots (the final one is already copied to exports/scene.ply),
# the pose solver's database and models, the undistorted training images, and
# Object Capture's input links and raw (unaligned) output.
_WORKING_DIRS = ("checkpoints", "colmap", "colmap_da3", "dataset", "mesh_input", "mesh_raw")


def _working_paths(job: Job) -> list[Path]:
    """What clean() removes. Frames used as thumbnails in the UI are kept."""
    state, _ = job.snapshot()
    keep = {url.rsplit("/", 1)[-1] for url in (state.get("frames") or {}).get("sample") or []}
    paths = [job.work / name for name in _WORKING_DIRS if (job.work / name).exists()]
    frames = job.work / "frames"
    if frames.is_dir():
        paths += [p for p in frames.iterdir() if p.name not in keep]
    return paths


def _size(path: Path) -> int:
    return disk_bytes(path) if path.is_dir() else path.lstat().st_size


def reclaimable(job: Job) -> int:
    """Bytes clean() would free; 0 unless the project finished."""
    if job.snapshot()[0]["stage"] != "done":
        return 0
    return sum(_size(p) for p in _working_paths(job))


def clean(job: Job) -> int:
    """Remove a finished project's working files; returns the bytes freed.

    The project still opens and views exactly as before: the exports, the
    source video, the sparse cloud and the thumbnail frames all stay. What it
    loses is the ability to retrain without starting again from the video.
    """
    state, _ = job.snapshot()
    if state["stage"] != "done":
        raise ValueError("only a finished project can be cleaned")
    # Without Node the viewer shows the final checkpoint itself; point it at
    # the identical copy in exports/ before checkpoints/ goes.
    checkpoint = state.get("checkpoint")
    if checkpoint and "/checkpoints/" in checkpoint["url"]:
        if not (job.work / "exports" / "scene.ply").is_file():
            raise ValueError("no exported scene.ply to keep showing")
        job.update(checkpoint={**checkpoint, "url": job.file_url("exports/scene.ply")})
    freed = 0
    for path in _working_paths(job):
        size = _size(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        freed += size
    job.update(cleaned=True)
    save(job)
    return freed


def delete(job: Job, jobs_dir: Path) -> None:
    """Remove the job's folder, refusing anything that is not directly inside jobs_dir."""
    work = job.work.resolve()
    if work.parent != jobs_dir.resolve() or work.name != job.id:
        raise ValueError(f"refusing to delete {work}: not a job folder")
    shutil.rmtree(work)


def summary(job: Job) -> dict:
    """The fields the project list shows."""
    state, _ = job.snapshot()
    artifacts = state.get("artifacts") or []
    gaussians = next((a.get("gaussians") for a in artifacts if a.get("name") == "scene.ply"), None)
    sample = (state.get("frames") or {}).get("sample") or []
    return {
        "id": job.id,
        "name": state.get("name") or job.id,
        "created": state.get("created"),
        "stage": state["stage"],
        "error": state.get("error"),
        "failed_stage": state.get("failed_stage"),
        "preset": job.preset_name,
        "outputs": job.outputs,
        "gaussians": gaussians,
        "triangles": (state.get("mesh") or {}).get("triangles"),
        "thumbnail": sample[0] if sample else None,
        "bytes": disk_bytes(job.work),
    }
