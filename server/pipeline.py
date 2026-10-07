import asyncio
import os
import signal
import subprocess
import threading
from pathlib import Path

from . import failures, presets, report

JOBS_DIR = Path(os.environ.get("SPLAT_JOBS_DIR", "jobs"))

TERMINAL_STAGES = ("done", "error", "cancelled")


class JobCancelled(Exception):
    pass


# What a job makes: a Gaussian splat, a textured mesh (Object Capture), or both.
OUTPUTS = ("splat", "mesh", "both")


class Job:
    def __init__(self, job_id: str, preset_name: str, pose_backend: str, outputs: str = "splat"):
        self.id = job_id
        self.work = JOBS_DIR / job_id
        self.preset_name = preset_name
        self.preset = presets.PRESETS[preset_name]
        self.pose_backend = pose_backend
        self.outputs = outputs
        self.cancelled = False
        self.proc: subprocess.Popen | None = None
        self.recorder: "report.Recorder | None" = None  # set while the pipeline runs
        self._lock = threading.Lock()
        self.version = 0
        self.state = {
            "job_id": job_id,
            "stage": "frames",
            "progress": 0.0,
            "message": "queued",
            "input_url": None,
            "frames": None,
            "sparse_url": None,
            "cameras": None,
            "checkpoint": None,
            "artifacts": None,
            "error": None,
            "outputs": outputs,
        }

    def file_url(self, rel_path: str) -> str:
        return f"/api/jobs/{self.id}/files/{rel_path}"

    def update(self, **fields):
        with self._lock:
            self.state.update(fields)
            self.version += 1

    def snapshot(self):
        with self._lock:
            return dict(self.state), self.version

    @property
    def running(self) -> bool:
        state, _ = self.snapshot()
        return state["stage"] not in TERMINAL_STAGES

    def check_cancelled(self):
        if self.cancelled:
            raise JobCancelled()

    def cancel(self):
        self.cancelled = True
        _kill_group(self.proc)


def _kill_group(proc: subprocess.Popen | None):
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def run_subprocess(
    job: Job, cmd: list[str], cwd: Path | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run a blocking subprocess in its own process group so it can be killed on cancel.

    `timeout` (seconds) bounds the run so a wedged tool can't hang the job forever; on
    expiry the whole group is killed and subprocess.TimeoutExpired is raised.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    job.proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        job.proc = None
        job.check_cancelled()
        raise
    job.proc = None
    job.check_cancelled()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# One reconstruction at a time: the trainer wants the whole GPU. This slot is
# the single source of truth for "is something running" — the job registry in
# main.py only remembers finished jobs so their files stay reachable.
_active_job: Job | None = None
_active_lock = threading.Lock()


def try_start(job: Job) -> bool:
    """Claim the active slot for `job`; fails if another job is still running."""
    global _active_job
    with _active_lock:
        if _active_job is not None and _active_job.running:
            return False
        _active_job = job
        return True


def release(job: Job) -> None:
    """Hand the slot back for a job that claimed it but never ran.

    Without this, a claim that fails before `start()` (an upload that dies
    mid-copy, say) leaves the slot held by a job that will never reach a
    terminal stage, and every later job is refused until the server restarts.
    """
    global _active_job
    with _active_lock:
        if _active_job is job:
            _active_job = None


def active_job_id() -> str | None:
    job = _active_job
    return job.id if job is not None and job.running else None


def stage_names(outputs: str) -> list[str]:
    """The stages a job runs. Poses always run: the splat trains on them, and the
    mesh is lined up with them. The mesh goes first in "both": it takes minutes
    where training takes a quarter of an hour, so it's ready to look at sooner."""
    names = ["frames", "poses"]
    if outputs in ("mesh", "both"):
        names.append("mesh")
    if outputs in ("splat", "both"):
        names += ["train", "export"]
    return names


def _run_sync(job: Job):
    # Imported here, not at module scope: the stages import back from this
    # module for run_subprocess and JobCancelled.
    from .stages import export as export_stage
    from .stages import frames as frames_stage
    from .stages import mesh as mesh_stage
    from .stages import poses_colmap
    from .stages import poses_da3
    from .stages import train_brush

    job.work.mkdir(parents=True, exist_ok=True)
    poses_stage = poses_da3 if job.pose_backend == "da3" else poses_colmap
    runners = {
        "frames": frames_stage.run,
        "poses": poses_stage.run,
        "mesh": mesh_stage.run,
        "train": train_brush.run,
        "export": export_stage.run,
    }
    job.recorder = report.Recorder(job)
    try:
        job.recorder.start()
    except Exception:
        pass  # the report is a by-product; it must never stop the job
    terminal = {"stage": "error", "error": "the pipeline stopped unexpectedly", "message": "error"}
    try:
        terminal = _run_stages(job, runners)
    finally:
        # The report goes out with the final state: a browser stops listening
        # once it sees a terminal stage, so anything published after would be missed.
        terminal["artifacts"] = _write_report(job, terminal)
        job.update(**terminal)


def _write_report(job: Job, terminal: dict) -> list | None:
    """Write the debug report; return the artifact list with it added."""
    recorder, job.recorder = job.recorder, None
    artifacts = job.snapshot()[0].get("artifacts")
    try:
        recorder.finish(terminal)
        path = recorder.write()
    except Exception:
        return artifacts
    if path is None:
        return artifacts
    others = [a for a in (artifacts or []) if a.get("name") != report.REPORT_NAME]
    return others + [{
        "name": report.REPORT_NAME,
        "url": job.file_url(f"exports/{report.REPORT_NAME}"),
        "bytes": path.stat().st_size,
    }]


def _run_stages(job: Job, runners: dict) -> dict:
    """Run every stage; return the terminal state fields (not yet applied)."""
    try:
        for name in stage_names(job.outputs):
            job.check_cancelled()
            if job.recorder:
                job.recorder.stage(name)
            job.update(stage=name, progress=0.0, message=f"starting {name}")
            if name == "mesh" and job.outputs == "both":
                # The splat doesn't depend on the mesh: record a mesh failure and carry on.
                try:
                    runners[name](job, job.work, job.preset)
                except JobCancelled:
                    raise
                except Exception as exc:
                    job.update(mesh_error=str(exc), mesh_failure=failures.explain("mesh", str(exc)))
                continue
            runners[name](job, job.work, job.preset)
        job.check_cancelled()
        return {"stage": "done", "progress": 1.0, "message": "done"}
    except JobCancelled:
        return {"stage": "cancelled", "message": "cancelled"}
    except Exception as exc:
        # "stage" is about to become "error"; keep the one that failed.
        failed_stage = job.snapshot()[0]["stage"]
        return {
            "stage": "error", "error": str(exc), "message": str(exc),
            "failed_stage": failed_stage, "failure": failures.explain(failed_stage, str(exc)),
        }


async def start(job: Job):
    await asyncio.to_thread(_run_sync, job)
