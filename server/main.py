import asyncio
import dataclasses
import json
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline, projects, versions
from .presets import DEFAULT_PRESET, PRESETS
from .pipeline import Job
from .stages import frames as frames_stage

app = FastAPI()

# Every job this process has seen, running or finished, so its state and files
# stay reachable after it ends — plus every saved project from earlier runs of
# the app. Which one is *running* is pipeline's to say.
JOBS: dict[str, Job] = {job.id: job for job in projects.load_all(pipeline.JOBS_DIR)}
# Held only so the event loop cannot garbage-collect a running pipeline task.
_background_tasks: set[asyncio.Task] = set()


async def _run_and_save(job: Job):
    try:
        # Probed off the event loop: the npx and brush calls take a second or two.
        try:
            found = await asyncio.to_thread(versions.collect, job.pose_backend, job.outputs)
        except Exception:
            found = None  # a version probe must never stop the job itself
        job.update(versions=found)
        projects.save(job)
        await pipeline.start(job)
    finally:
        projects.save(job)


@app.get("/api/presets")
async def list_presets():
    """Each preset's settings, so the UI can estimate frame counts and length limits."""
    return {name: dataclasses.asdict(preset) for name, preset in PRESETS.items()}


# Plain def, not async: summaries walk each folder for its disk size, so FastAPI
# runs this in its threadpool instead of on the event loop.
@app.get("/api/jobs")
def list_jobs():
    """Saved projects, newest first."""
    jobs = sorted(JOBS.values(), key=lambda j: j.snapshot()[0].get("created") or 0, reverse=True)
    return [projects.summary(job) for job in jobs]


@app.get("/api/jobs/{job_id}/disk")
def job_disk(job_id: str):
    """Disk use, and how much "clean up" would free."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {"bytes": projects.disk_bytes(job.work), "reclaimable": projects.reclaimable(job)}


@app.post("/api/jobs/{job_id}/clean")
async def clean_job(job_id: str):
    """Remove a finished project's working files, keeping everything it shows."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.snapshot()[0]["stage"] != "done":
        raise HTTPException(409, "only a finished project can be cleaned up")
    try:
        freed = await asyncio.to_thread(projects.clean, job)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"freed": freed, "bytes": projects.disk_bytes(job.work)}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete a project and its whole folder, downloads included."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.running:
        raise HTTPException(409, "the job is still running; cancel it before deleting")
    await asyncio.to_thread(projects.delete, job, pipeline.JOBS_DIR)
    del JOBS[job_id]
    return {"deleted": job_id}


@app.post("/api/jobs")
async def create_job(
    video: UploadFile,
    preset: str = Form(DEFAULT_PRESET),
    pose_backend: str = Form("colmap"),
    name: str = Form(""),
    outputs: str = Form("splat"),
):
    if preset not in PRESETS:
        raise HTTPException(400, f"unknown preset '{preset}'")
    if pose_backend not in ("colmap", "da3"):
        raise HTTPException(400, f"unknown pose_backend '{pose_backend}'")
    if outputs not in pipeline.OUTPUTS:
        raise HTTPException(400, f"unknown outputs '{outputs}'")

    job_id = uuid4().hex[:12]
    job = Job(job_id, preset, pose_backend, outputs)
    if not pipeline.try_start(job):
        raise HTTPException(409, "a job is already running")

    # The slot is claimed but the job has not started, so nothing else can free
    # it: an upload that dies here has to hand it back itself.
    try:
        job.work.mkdir(parents=True, exist_ok=True)
        ext = Path(video.filename or "input.mp4").suffix or ".mp4"
        with (job.work / f"input{ext}").open("wb") as f:
            shutil.copyfileobj(video.file, f)
        # The start screen already stops an over-long video; this catches other clients.
        limit = PRESETS[preset].max_video_s
        if limit is not None:
            duration = await asyncio.to_thread(frames_stage.probe_duration, job.work / f"input{ext}")
            if duration > limit:
                shutil.rmtree(job.work, ignore_errors=True)
                raise HTTPException(
                    400, f"the video runs {duration:.0f} s; the '{preset}' preset takes up to {limit:.0f} s",
                )
        # input_url is published so any tab can show the footage next to the
        # reconstruction — the one that uploaded still has the bytes, but a
        # reload or a second tab only has the job id.
        job.update(
            input_url=job.file_url(f"input{ext}"),
            name=projects.clean_name(name, Path(video.filename or "untitled").stem),
            created=time.time(),
        )
        projects.save(job)
    except Exception:
        pipeline.release(job)
        raise

    JOBS[job_id] = job
    task = asyncio.create_task(_run_and_save(job))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"job_id": job_id}


@app.get("/api/jobs/active")
async def active_job():
    """The running job, if any — lets a fresh tab attach to a run in progress."""
    return {"job_id": pipeline.active_job_id()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    state, _ = job.snapshot()
    return state


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")

    async def stream():
        last_version = None
        last_sent = 0.0
        while True:
            if await request.is_disconnected():
                return
            state, version = job.snapshot()
            if version != last_version:
                yield f"event: state\ndata: {json.dumps(state)}\n\n"
                last_version = version
                last_sent = time.monotonic()
                if state["stage"] in pipeline.TERMINAL_STAGES:
                    return
            elif time.monotonic() - last_sent > 15:
                yield ": heartbeat\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    job.cancel()
    state, _ = job.snapshot()
    return state


@app.get("/api/jobs/{job_id}/files/{path:path}")
async def job_file(job_id: str, path: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    root = job.work.resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(403, "invalid path")
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


# Mount the two viewer libraries individually rather than all of vendor/: that
# directory also holds the Brush source tree and its build output (~5 GB), none
# of which the browser has any business fetching.
app.mount("/vendor/spark", StaticFiles(directory="vendor/spark"), name="vendor_spark")
app.mount("/vendor/three", StaticFiles(directory="vendor/three"), name="vendor_three")
# The viewer engine web/ and site/ share; pages resolve it via the "splat-viewer/"
# import map entry, so this path and the site's ./viewer/ can differ freely.
app.mount("/viewer", StaticFiles(directory="viewer"), name="viewer")
app.mount("/", StaticFiles(directory="web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="127.0.0.1", port=8000)
