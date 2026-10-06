import math
import subprocess
import sys
from pathlib import Path

from PIL import Image

from ..pipeline import run_subprocess
from ..presets import frame_plan

_SHARP_FRAMES_BIN = str(Path(sys.executable).parent / "sharp-frames")


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _ffmpeg_fallback(job, video: Path, out_dir: Path, spacing: float):
    run_subprocess(job, [
        "ffmpeg", "-y", "-i", str(video),
        "-vf", f"fps={1 / spacing}", "-q:v", "2",
        str(out_dir / "%05d.jpg"),
    ])


def run(job, work: Path, preset):
    video = next(work.glob("input.*"))
    out_dir = work / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(video)
    count, spacing = frame_plan(preset, duration)
    job.update(message=f"selecting the sharpest frame every {spacing:.2f} s (~{count} frames)", progress=0.05)
    # Candidates at 10 fps (more if the spacing is short, so every window has at least
    # 3), then batched selection keeps the sharpest candidate in each window. Unlike
    # best-n, which picks the sharpest N anywhere, this can't bunch frames and leave gaps.
    fps = min(30, max(10, math.ceil(3 / spacing)))
    batch = max(1, round(fps * spacing))
    try:
        run_subprocess(job, [
            _SHARP_FRAMES_BIN, str(video), str(out_dir),
            "--fps", str(fps),
            "--selection-method", "batched",
            "--batch-size", str(batch),
            "--batch-buffer", "0",
            "--format", "jpg",
            "--force-overwrite",
        ])
    except (subprocess.CalledProcessError, FileNotFoundError):
        job.update(message="sharp-frames failed, falling back to uniform ffmpeg extraction")
        _ffmpeg_fallback(job, video, out_dir, spacing)

    job.check_cancelled()

    image_files = sorted(out_dir.glob("*.jpg"))
    if not image_files:
        raise RuntimeError("no frames were extracted from the video")

    job.update(message="downscaling frames", progress=0.5)
    for path in image_files:
        with Image.open(path) as im:
            if max(im.size) > preset.max_resolution:
                im.thumbnail((preset.max_resolution, preset.max_resolution), Image.LANCZOS)
                im.save(path)

    stride = max(1, len(image_files) // 8)
    sample = [job.file_url(f"frames/{p.name}") for p in image_files[::stride][:8]]

    job.update(
        frames={"count": len(image_files), "spacing_s": round(duration / len(image_files), 2), "sample": sample},
        progress=1.0,
        message=f"extracted {len(image_files)} frames",
    )
