"""Saved projects: each job folder carries a project.json, so finished runs survive a restart.

The running job's live state stays in memory (pipeline.Job). project.json is a
snapshot of it, written when the job starts and again when it ends; on startup
every job folder that has one is loaded back, so its results stay reachable.
"""
import json
import os
import time
from pathlib import Path

from .pipeline import TERMINAL_STAGES, Job

PROJECT_FILE = "project.json"
SCHEMA = 1
MAX_NAME_LENGTH = 80
INTERRUPTED = "interrupted: the app stopped while this project was running"


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
        "pose_backend": job.pose_backend,
        "saved": time.time(),
        "state": state,
    }
    path = job.work / PROJECT_FILE
    tmp = path.with_name(PROJECT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_all(jobs_dir: Path) -> list[Job]:
    """Every saved project under jobs_dir, as finished Jobs.

    A project whose snapshot is not in a terminal stage was cut off mid-run (the
    app stopped), so it comes back as an error rather than as a job that claims
    to still be running. Unreadable files, and projects made with a preset that
    no longer exists, are skipped.
    """
    jobs = []
    for path in sorted(jobs_dir.glob(f"*/{PROJECT_FILE}")):
        try:
            data = json.loads(path.read_text())
            job = Job(data["id"], data["preset"], data["pose_backend"])
            state = dict(data["state"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        job.work = path.parent
        if state.get("stage") not in TERMINAL_STAGES:
            # A mid-run checkpoint URL may point at a preview file that was deleted.
            state.update(stage="error", error=INTERRUPTED, message=INTERRUPTED, checkpoint=None)
        job.update(**state)
        jobs.append(job)
    return jobs


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
        "preset": job.preset_name,
        "gaussians": gaussians,
        "thumbnail": sample[0] if sample else None,
    }
