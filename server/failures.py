"""Plain-language explanations for a failed job: which stage, what went wrong, what to try.

The raw error still travels with the job (state["error"]) for anyone who wants
the technical detail; this is the part written for the person who filmed the room.
"""
import re

STAGE_LABEL = {"frames": "Frames", "poses": "Camera positions", "mesh": "Mesh", "train": "Training", "export": "Export"}

CAPTURE_TIPS = [
    "Move slowly and smoothly, and don't swing quickly from one area to another.",
    "Overlap a lot: every part of the room should stay in view for a few seconds.",
    "Turn on all the lights and keep them constant.",
    "Stand back from plain walls, doors, windows, mirrors and screens; the software needs texture to track.",
    "Keep the video to about 2–3 minutes, so the frames the preset picks stay close together.",
]

_POSES_TITLE = "Couldn't work out where the camera was"


def _rules():
    """(stage or None for any stage, pattern, builder(match) -> fields), first match wins."""
    return [
        (None, r"No space left on device", lambda m: dict(
            title="Ran out of disk space",
            detail="The disk filled up while this project was running. Delete projects you no longer "
                   "need from the project list, then run it again.",
        )),
        (None, r"^interrupted", lambda m: dict(
            title="The app was closed mid-run",
            detail="The app stopped while this project was running, so it never finished. "
                   "Start it again from the same video.",
        )),
        (None, r"^unsaved run", lambda m: dict(
            title="Nothing to open",
            detail="This folder is from an older version of the app or an upload that failed, so "
                   "there is no result to show. Delete it to free the space.",
        )),
        ("poses", r"only (\d+)/(\d+) frames registered", lambda m: dict(
            title=_POSES_TITLE,
            detail=f"Only {m[1]} of {m[2]} frames could be placed in 3D, and at least 30% are needed "
                   "to train. This almost always comes from the footage: frames that don't overlap "
                   "enough, or surfaces with too little texture to track.",
            tips=CAPTURE_TIPS, capture_guide=True,
        )),
        ("poses", r"could not reconstruct any camera poses|did not produce a usable reconstruction", lambda m: dict(
            title=_POSES_TITLE,
            detail="None of the frames could be linked together in 3D. This almost always comes from "
                   "the footage: frames that don't overlap enough, or surfaces with too little "
                   "texture to track.",
            tips=CAPTURE_TIPS, capture_guide=True,
        )),
        ("poses", r"DA3 backend not installed", lambda m: dict(
            title="Depth Anything 3 isn't installed",
            detail="Choose COLMAP as the pose backend, or install DA3 with: uv sync --group da3",
        )),
        ("frames", r"no frames were extracted|ffprobe|ffmpeg", lambda m: dict(
            title="Couldn't read the video",
            detail="The video file couldn't be decoded. Check it plays on your Mac; if it does, "
                   "export it again as MP4 or MOV and try once more.",
        )),
        ("train", r"brush training failed", lambda m: dict(
            title="Training crashed",
            detail="The trainer stopped with an error. The usual cause is running out of memory: close "
                   "other heavy apps, or try the Preview preset, then run it again.",
        )),
        ("train", r"produced no checkpoints", lambda m: dict(
            title="Training produced no result",
            detail="The trainer finished without saving a scene. Run the project again; if it keeps "
                   "happening, the technical details below will help.",
        )),
        ("mesh", r"not supported on this Mac", lambda m: dict(
            title="This Mac can't build meshes",
            detail="Meshes use Apple's Object Capture, which needs an Apple Silicon Mac (or an Intel Mac "
                   "with a recent AMD GPU) running macOS 12 or later. Choose Gaussian splat instead.",
        )),
        ("mesh", r"swiftc not found|failed to build", lambda m: dict(
            title="The mesh helper couldn't be built",
            detail="Building meshes needs Apple's Command Line Tools. Install them with: "
                   "xcode-select --install, then run the project again.",
        )),
        ("mesh", r"frames were placed by both", lambda m: dict(
            title="Couldn't line the mesh up with the splat",
            detail="Object Capture and the camera-position step agreed on too few frames to place the "
                   "mesh where the splat is. This usually comes from the footage: frames that don't "
                   "overlap enough, or glass and other surfaces that are hard to track.",
            tips=CAPTURE_TIPS, capture_guide=True,
        )),
        ("mesh", r"object capture failed|produced no mesh", lambda m: dict(
            title="Couldn't build the mesh",
            detail="Object Capture couldn't make a mesh from these frames. It needs plenty of overlap "
                   "and surfaces with texture; glass walls and plain, close-up surfaces are the usual "
                   "problems.",
            tips=CAPTURE_TIPS, capture_guide=True,
        )),
        ("export", r"no trained checkpoint", lambda m: dict(
            title="Nothing to export",
            detail="Training didn't leave a scene to export. Run the project again.",
        )),
    ]


def explain(stage: str | None, error: str | None) -> dict:
    """What the error panel shows for a job that failed during `stage`."""
    error = error or ""
    for rule_stage, pattern, build in _rules():
        if rule_stage not in (None, stage):
            continue
        m = re.search(pattern, error, re.IGNORECASE)
        if m:
            return {"stage": stage, "tips": [], "capture_guide": False, **build(m)}
    label = STAGE_LABEL.get(stage, "the run")
    return {
        "stage": stage,
        "title": f"Failed during {label.lower() if stage in STAGE_LABEL else label}",
        "detail": "Something unexpected stopped the job. The technical details below may help.",
        "tips": [],
        "capture_guide": False,
    }
