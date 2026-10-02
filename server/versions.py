"""Tool versions, recorded with each project so a result can be traced to what made it.

Every probe is best-effort: a tool that is missing or answers oddly records None
rather than holding up the job.
"""
import importlib.metadata
import subprocess
import sys
from pathlib import Path

from .stages import export, train_brush

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _first_line(cmd: list[str], cwd: Path | None = None, timeout: float = 60) -> str | None:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0].strip() if lines else None


def _package(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def collect(pose_backend: str) -> dict:
    ffmpeg = _first_line(["ffmpeg", "-version"])  # "ffmpeg version 8.1.2 Copyright ..."
    versions = {
        "splat_local": _first_line(["git", "describe", "--always", "--dirty"], cwd=_PROJECT_ROOT),
        "python": sys.version.split()[0],
        "ffmpeg": ffmpeg.split()[2] if ffmpeg and len(ffmpeg.split()) > 2 else ffmpeg,
        "sharp_frames": _package("sharp-frames"),
        "pycolmap": _package("pycolmap"),
        "brush": _first_line([train_brush._resolve_bin(), "--version"]),
        "splat_transform": _first_line(export._NPX_CMD + ["--version"]),
    }
    try:
        import pycolmap
        versions["colmap"] = pycolmap.COLMAP_version
    except Exception:
        versions["colmap"] = None
    if pose_backend == "da3":
        from .stages.poses_da3 import DA3_MODEL
        versions["da3_model"] = DA3_MODEL
    return versions
