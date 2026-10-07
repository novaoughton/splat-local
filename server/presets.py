from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    # Frames are taken one per `frame_spacing_s` of video, so a longer video gets more of
    # them. Spacing, not count, decides whether poses can be solved: Dorm Video 3 (5m27s)
    # placed 60/200 frames 1.6 s apart and 243/400 at 0.8 s. `min_frames` keeps short
    # clips from getting too few views; it only ever moves frames closer together.
    frame_spacing_s: float
    min_frames: int
    max_resolution: int
    total_steps: int
    growth_stop: int
    max_splats: int
    export_every: int
    lpips_weight: float = 0.0
    # Opacity floor for the viewer-only artifact; the downloadable archive is never filtered.
    # Measured on a 135,575-splat scene (fill ratio = average overdraw layers per pixel):
    #   no filter -> 135,575 splats, 46.7 fill · gt,0.05 -> 109,551, 36.4 · gt,0.15 -> 59,748, 22.1
    # Median splat opacity there was 0.127, so 0.15 halves the scene — a big win that needs
    # human visual sign-off first. 0.05 drops near-invisible splats only: safe as a default.
    view_opacity_min: float = 0.05
    # Object Capture's detail level for the optional mesh (Preview/High/Max -> reduced/medium/full).
    # Medium gave ~63k triangles for a bedroom in ~2 min; full is denser and slower.
    mesh_detail: str = "medium"
    # Longest video the preset accepts, in seconds. None until the stress test measures
    # how memory and time grow with frame count.
    max_video_s: float | None = None


PRESETS = {
    "preview": Preset(
        frame_spacing_s=1.0,
        min_frames=60,
        max_resolution=1536,
        total_steps=10_000,
        growth_stop=6_000,
        # 3M, not 1.5M: the 69 s desk clip hit 1.5M by step 4,000 of 10,000 and stopped
        # growing. Training peaked at 5.5 GB there (~2.3 KB per splat), so 3M is ~9 GB.
        max_splats=3_000_000,
        export_every=500,
        mesh_detail="reduced",
    ),
    "high": Preset(
        frame_spacing_s=0.8,
        min_frames=80,
        max_resolution=2048,
        # 18k, not 30k: held-out PSNR stops moving once densification stops. Growth ends at
        # 15k either way, and the 12k steps after it were buying nothing measurable — n=5 per
        # arm puts the difference at +0.39 dB in 18k's favour, 95% CI [-0.16, +0.93], against
        # 1.78x the training speed. Not a quality *gain*: the interval includes zero. The
        # claim is that it is no worse, and 2x faster. See docs/step-count.md.
        # growth_stop stays at 15k on purpose — it is the arm that was actually measured.
        total_steps=18_000,
        growth_stop=15_000,
        max_splats=4_000_000,
        export_every=1000,
    ),
    "max": Preset(
        frame_spacing_s=0.7,
        min_frames=100,
        max_resolution=2560,
        total_steps=45_000,
        growth_stop=25_000,
        max_splats=6_000_000,
        export_every=1000,
        # No LPIPS: Brush runs VGG-LPIPS on the full-size render with gradients, and at
        # 2560 px that took training past 25 GB within 90 s (4.1 GB without it), stalling
        # a 24 GB Mac. Revisit by having Brush downsample both images before LPIPS.
        lpips_weight=0.0,
        mesh_detail="full",
    ),
}

DEFAULT_PRESET = "high"


def frame_plan(preset: Preset, duration: float) -> tuple[int, float]:
    """How many frames a video of `duration` seconds gets, and how far apart they are.
    web/app.js mirrors this for the start screen's estimate."""
    duration = max(duration, 0.1)
    count = max(preset.min_frames, round(duration / preset.frame_spacing_s))
    return count, duration / count
