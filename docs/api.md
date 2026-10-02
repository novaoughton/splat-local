# API contract

## Endpoints

- `GET /api/jobs` — saved projects, newest first: `[{"id", "name", "created", "stage", "error", "preset", "gaussians", "thumbnail", "bytes"}]`, where `bytes` is the folder's size on disk. Includes projects from earlier runs of the app (see [Saved projects](#saved-projects)).
- `DELETE /api/jobs/{id}` — delete the project and its whole folder, downloads included. 409 if the job is still running (cancel it first).
- `POST /api/jobs` — multipart form: `video` (file), `preset` (`preview|high|max`, default `high`), `pose_backend` (`colmap|da3`, default `colmap`), `name` (optional; defaults to the video's file name without extension). Returns `{"job_id": str}`. 409 if a job is already running.
- `GET /api/jobs/active` — `{"job_id": str | null}` for the currently running job (lets any tab attach).
- `GET /api/jobs/{id}` — JSON snapshot of job state (same shape as SSE `state` payload).
- `GET /api/jobs/{id}/events` — SSE stream. On connect, emits current state, then updates.
- `POST /api/jobs/{id}/cancel` — cancel the job (kills running stage process).
- `GET /api/jobs/{id}/files/{path}` — serves files from the job dir (frames, sparse.ply, checkpoints, exports).

Static mounts, in match order: `/vendor/spark` and `/vendor/three` (the two viewer libraries only — `vendor/` also holds the ~5 GB Brush source tree), `/viewer` (the viewer engine shared with the demo site), then `/` -> `web/`.

## SSE events

Every event is `event: state` with a full JSON job snapshot:

```json
{
  "job_id": "abc123",
  "name": "Living room",
  "created": 1790971275.0,
  "stage": "frames|poses|train|export|done|error|cancelled",
  "progress": 0.42,
  "message": "human-readable status line",
  "input_url": "/api/jobs/abc123/files/input.mp4",
  "frames": {"count": 200, "sample": ["/api/jobs/abc123/files/frames/00001.jpg"]},
  "sparse_url": "/api/jobs/abc123/files/sparse.ply",
  "cameras": [{"position": [x,y,z], "rotation": [qw,qx,qy,qz]}],
  "checkpoint": {"url": ".../checkpoints/splat_10000.ply", "step": 10000, "total_steps": 30000},
  "artifacts": [{"name": "scene.ply", "url": "...", "bytes": 123, "gaussians": 135575, "fill_ratio": 46.7}],
  "error": null,
  "failed_stage": null,
  "failure": null
}
```

When a job fails, `error` keeps the raw message, `failed_stage` names the stage it failed in, and `failure` (from `server/failures.py`) explains it for the person who filmed the room:

```json
{"stage": "poses", "title": "Couldn't work out where the camera was",
 "detail": "Only 24 of 200 frames could be placed in 3D, and at least 30% are needed to train. ...",
 "tips": ["Move slowly and smoothly, ..."], "capture_guide": true}
```

`capture_guide` is true when the footage is the likely cause; the UI then links `/capture.html`.

`input_url` is the uploaded video, set as soon as the upload lands and served with range
support so it can be scrubbed. The UI plays it beside the viewer for the whole run; the tab
that did the upload uses its own blob instead, so this is for reloads and second tabs.

Fields are null/absent until their stage produces them. `checkpoint` is what the viewer loads: during training an SH-degree-1 preview copy of the latest export (`checkpoints/preview_*.ply`, 2.6× smaller than the full checkpoint), the full-SH `.ply` once training ends, then `exports/scene-view.sog` once the export stage has built it (`step`/`total_steps` stay as they were).

`gaussians` and `fill_ratio` (average overdraw layers per pixel, from `splat-transform --stats`) are present per artifact when Node is available; `name`/`url`/`bytes` are always present.

## Export artifacts

- **Archive** — `scene.ply`, `scene.spz`: what the user downloads. Full resolution, SH3, only NaN/Inf/degenerate gaussians removed. No quality decision is applied.
- **View** — `scene-view.sog`: what the viewer loads. Same scene with an opacity floor (`Preset.view_opacity_min`) and Morton reordering, so viewer-side cleanup never affects the download.

Without Node (`npx`), only the raw `scene.ply` checkpoint copy is produced and the viewer keeps the last training checkpoint.

## Job directory layout

`jobs/{id}/`: `project.json`, `input.<ext>`, `frames/*.jpg`, `colmap/` (db + sparse), `dataset/` (undistorted images + sparse for Brush), `sparse.ply`, `checkpoints/*.ply` (Brush's `export_*.ply` originals, kept; plus at most two transient `preview_*.ply` stream copies while training runs), `exports/*`

## Saved projects

Each job folder carries a `project.json`, written when the job starts and again when it ends:

```json
{"schema": 1, "id": "abc123", "preset": "high", "preset_settings": {"frames": 200, "total_steps": 18000, "...": "..."},
 "pose_backend": "colmap", "saved": 1790972396.1, "state": { ...the job snapshot above... }}
```

`preset_settings` records the preset's values as the run used them. `state.versions` records the tools that made the result, probed when the job starts; a probe that fails records `null`:

```json
{"splat_local": "892e2c4", "python": "3.12.15", "ffmpeg": "8.1.2", "sharp_frames": "0.3.1", "pycolmap": "4.1.0",
 "colmap": "COLMAP 4.1.0", "brush": "brush-cli 1.0.0", "splat_transform": "splat-transform v3.9.0 (435b972)"}
```

(`da3_model` is added for Depth Anything 3 runs.) splat-transform is pinned (`SPLAT_TRANSFORM_VERSION` in `server/stages/export.py`), so its version only changes when the pin does.

On startup the server loads every `jobs/*/project.json` back into its job registry, so finished projects stay listable and their files stay servable across restarts. A project saved mid-run (the app stopped before it finished) loads as `stage: "error"` with an "interrupted" error. A folder with no readable `project.json` (one from before saved projects, a failed upload, or a preset that no longer exists) loads as an "Unsaved run" with `preset: "unknown"`, so it still shows up and can be deleted.
