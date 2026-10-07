import math
import subprocess
import sys
from pathlib import Path

from PIL import Image

from .. import report
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
    report.note(job, "frames", planned=count, planned_spacing_s=round(spacing, 3),
                candidate_fps=fps, window_candidates=batch, selection="sharp-frames batched")
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
        report.note(job, "frames", selection="ffmpeg uniform (sharp-frames failed)")
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

    try:
        report.note(job, "frames", image_stats=image_stats(image_files))
    except Exception:
        pass  # stats are for the report only

    stride = max(1, len(image_files) // 8)
    sample = [job.file_url(f"frames/{p.name}") for p in image_files[::stride][:8]]

    job.update(
        frames={"count": len(image_files), "spacing_s": round(duration / len(image_files), 2), "sample": sample},
        progress=1.0,
        message=f"extracted {len(image_files)} frames",
    )


def image_stats(paths: list[Path], thumb: int = 128) -> dict:
    """Size, colour and exposure of the selected frames, from small copies of each.

    brightness_spread is how much frame brightness moves through the video (the
    5th-95th percentile range of per-frame mean luminance, 0-1). Auto-exposure
    swinging between a bright window and a dark corner shows up here.
    """
    import numpy as np

    with Image.open(paths[0]) as im:
        width, height = im.size
    means, lum_all, sat, clipped, crushed = [], [], [], 0, 0
    total = 0
    for path in paths:
        with Image.open(path) as im:
            im.draft("RGB", (thumb, thumb))  # JPEG: decode at reduced size, fast
            small = im.convert("RGB")
            small.thumbnail((thumb, thumb))
            rgb = np.asarray(small, dtype=np.float32)
            hsv = np.asarray(small.convert("HSV"), dtype=np.float32)
        lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
        means.append(rgb.reshape(-1, 3).mean(0))
        lum_all.append(lum.ravel()[::7])
        sat.append(hsv[..., 1].mean() / 255)
        clipped += int((rgb.max(-1) >= 250).sum())
        crushed += int((lum <= 5).sum())
        total += lum.size
    means = np.array(means)
    frame_lum = 0.2126 * means[:, 0] + 0.7152 * means[:, 1] + 0.0722 * means[:, 2]
    lum_all = np.concatenate(lum_all)
    p5, p50, p95 = np.percentile(lum_all, [5, 50, 95])
    return {
        "width": width,
        "height": height,
        "megapixels_total": round(len(paths) * width * height / 1e6, 1),
        "mean_rgb": [round(float(v), 1) for v in means.mean(0)],
        "luminance_p5_p50_p95": [round(float(p5), 1), round(float(p50), 1), round(float(p95), 1)],
        "mean_saturation": round(float(np.mean(sat)), 3),
        "clipped_highlights_pct": round(100 * clipped / total, 2),
        "crushed_shadows_pct": round(100 * crushed / total, 2),
        "frame_brightness_min_max": [round(float(frame_lum.min()), 1), round(float(frame_lum.max()), 1)],
        "brightness_spread": round(float((np.percentile(frame_lum, 95) - np.percentile(frame_lum, 5)) / 255), 3),
    }
