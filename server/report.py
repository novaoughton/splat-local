"""The per-project debug report: what each stage did, how long it took, and what it cost.

A Recorder runs alongside a job. A background thread samples the pipeline's CPU and
memory every few seconds — the app process plus every tool it launched (sharp-frames,
ffmpeg, objcap, Brush, splat-transform), with memory measured as Activity Monitor
does, GPU allocations included — and system-wide memory and swap. Stages add their own
details with note(). When the job ends, however it ends, the lot is written to
exports/debug-report.md: readable tables first, the raw data as JSON at the bottom.

Nothing here may stop a job: every probe fails soft.
"""
import json
import os
import platform
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

REPORT_NAME = "debug-report.md"
SAMPLE_EVERY_S = 5.0
# A sampler tick this late means the Mac was asleep (or the app was frozen).
SLEEP_GAP_S = 60.0


def note(job, section: str, **data) -> None:
    """Add details to a section of the job's report (no-op without a recorder)."""
    recorder = getattr(job, "recorder", None)
    if recorder is not None:
        recorder.notes.setdefault(section, {}).update(data)


# --- probes ------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_ps(text: str) -> dict[int, tuple[int, float, int]]:
    """`ps -A -o pid=,ppid=,%cpu=,rss=` → {pid: (ppid, cpu %, rss KB)}."""
    procs = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                procs[int(parts[0])] = (int(parts[1]), float(parts[2]), int(parts[3]))
            except ValueError:
                continue
    return procs


def descendants(procs: dict, root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid in procs:
            out.append(pid)
        todo.extend(children.get(pid, []))
    return out


_UNITS = {"B": 1 / 2**20, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


def parse_footprint(text: str) -> dict[int, float]:
    """`footprint -p …` → {pid: MB}. Footprint counts GPU memory, unlike RSS."""
    out = {}
    for m in re.finditer(r"\[(\d+)\]:.*?Footprint:\s*([\d.]+)\s*(B|KB|MB|GB)", text):
        out[int(m.group(1))] = float(m.group(2)) * _UNITS[m.group(3)]
    return out


def parse_vm_stat(text: str) -> dict[str, float]:
    """`vm_stat` → MB used by apps, wired and compressed memory."""
    page = int(re.search(r"page size of (\d+)", text).group(1)) if "page size of" in text else 16384
    pages = {m.group(1): int(m.group(2)) for m in re.finditer(r'^"?([^":]+)"?:\s+(\d+)\.', text, re.M)}
    mb = lambda key: pages.get(key, 0) * page / 2**20  # noqa: E731
    return {
        "app_mb": mb("Anonymous pages") - mb("Pages purgeable") if "Anonymous pages" in pages else mb("Pages active"),
        "wired_mb": mb("Pages wired down"),
        "compressed_mb": mb("Pages occupied by compressor"),
    }


def parse_swap(text: str) -> float | None:
    m = re.search(r"used = ([\d.]+)M", text)
    return float(m.group(1)) if m else None


def parse_power(text: str) -> dict:
    """`pmset -g batt` → {"source": "AC"|"Battery", "percent": n}."""
    out = {}
    if "AC Power" in text:
        out["source"] = "AC"
    elif "Battery Power" in text:
        out["source"] = "Battery"
    m = re.search(r"(\d+)%", text)
    if m:
        out["percent"] = int(m.group(1))
    return out


def machine_info() -> dict:
    info = {"python": platform.python_version()}
    for key, name in [("machdep.cpu.brand_string", "chip"), ("hw.ncpu", "cores"), ("hw.memsize", "ram_bytes")]:
        value = _run(["sysctl", "-n", key]).strip()
        if value:
            info[name] = int(value) if value.isdigit() else value
    info["macos"] = _run(["sw_vers", "-productVersion"]).strip() or platform.mac_ver()[0] or None
    if isinstance(info.get("ram_bytes"), int):
        info["ram_gb"] = round(info["ram_bytes"] / 2**30)
    return info


def probe_video(path: Path) -> dict:
    """Duration, resolution, frame rate, codec, colour (HDR?) and size of the input."""
    out = _run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", "-select_streams", "v:0", str(path),
    ], timeout=30)
    try:
        data = json.loads(out)
    except ValueError:
        return {"file": path.name, "bytes": path.stat().st_size if path.exists() else None}
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    rotation = None
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = side["rotation"]
    rate = stream.get("avg_frame_rate") or "0/1"
    num, _, den = rate.partition("/")
    return {
        "file": path.name,
        "bytes": int(fmt.get("size") or 0) or None,
        "duration_s": round(float(fmt.get("duration") or 0), 2),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "rotation": rotation,
        "fps": round(float(num) / float(den), 2) if den and float(den) else None,
        "codec": stream.get("codec_name"),
        "bit_rate": int(fmt.get("bit_rate") or 0) or None,
        "pixel_format": stream.get("pix_fmt"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "hdr": stream.get("color_transfer") in ("arib-std-b67", "smpte2084"),
    }


# --- the recorder --------------------------------------------------------------------

class Recorder:
    def __init__(self, job):
        self.job = job
        self.notes: dict[str, dict] = {}
        self.stages: list[dict] = []  # {name, start, end, status}
        self.samples: list[dict] = []
        self.started = time.time()
        self.ended = None
        self.machine = {}
        self.power_start = {}
        self.power_end = {}
        self.terminal: dict = {}
        self._stop = threading.Event()
        self._thread = None
        self._ncpu = os.cpu_count() or 1

    # lifecycle
    def start(self):
        self.machine = machine_info()
        self.power_start = parse_power(_run(["pmset", "-g", "batt"]))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stage(self, name: str):
        now = time.time()
        if self.stages and self.stages[-1]["end"] is None:
            self.stages[-1].update(end=now, status="ok")
        self.stages.append({"name": name, "start": now, "end": None, "status": None})

    def finish(self, terminal: dict):
        """terminal: the fields the job is about to end with (stage, error, ...)."""
        self.terminal = terminal
        self.ended = time.time()
        outcome = terminal.get("stage")
        if self.stages and self.stages[-1]["end"] is None:
            self.stages[-1].update(end=self.ended, status="ok" if outcome == "done" else outcome)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=SAMPLE_EVERY_S * 2)
        self.power_end = parse_power(_run(["pmset", "-g", "batt"]))

    def _current_stage(self):
        return self.stages[-1]["name"] if self.stages and self.stages[-1]["end"] is None else None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.samples.append(self._sample())
            except Exception:
                pass
            self._stop.wait(SAMPLE_EVERY_S)

    def _sample(self) -> dict:
        procs = parse_ps(_run(["ps", "-A", "-o", "pid=,ppid=,%cpu=,rss="]))
        tree = descendants(procs, os.getpid())
        footprints = parse_footprint(_run(["footprint", *sum((["-p", str(p)] for p in tree), [])]))
        system = parse_vm_stat(_run(["vm_stat"]))
        sample = {
            "t": round(time.time(), 1),
            "stage": self._current_stage(),
            # % of the whole machine (100 = every core busy)
            "cpu_pct": round(sum(procs[p][1] for p in tree) / self._ncpu, 1),
            "rss_mb": round(sum(procs[p][2] for p in tree) / 1024),
            "footprint_mb": round(sum(footprints.values())) if footprints else None,
            "swap_mb": parse_swap(_run(["sysctl", "-n", "vm.swapusage"])),
            **{k: round(v) for k, v in system.items()},
        }
        state, _ = self.job.snapshot()
        checkpoint = state.get("checkpoint") or {}
        if checkpoint.get("step") is not None:
            sample["train_step"] = checkpoint["step"]
        return sample

    # building the report
    def data(self) -> dict:
        state = {**self.job.snapshot()[0], **self.terminal}
        import dataclasses
        input_video = next(self.job.work.glob("input.*"), None)
        return {
            "report_version": 1,
            "project": {
                "id": self.job.id,
                "name": state.get("name"),
                "outcome": state.get("stage"),
                "failed_stage": state.get("failed_stage"),
                "error": state.get("error"),
                "preset": self.job.preset_name,
                "outputs": self.job.outputs,
                "pose_backend": self.job.pose_backend,
                "started": self.started,
                "ended": self.ended,
            },
            "machine": {**self.machine, "power_start": self.power_start, "power_end": self.power_end},
            "settings": dataclasses.asdict(self.job.preset),
            "versions": state.get("versions"),
            "input": probe_video(input_video) if input_video else None,
            "stages": [
                {**s, "duration_s": round((s["end"] or time.time()) - s["start"], 1),
                 **summarize(self.samples, s["start"], s["end"] or time.time())}
                for s in self.stages
            ],
            "notes": self.notes,
            "results": {
                "frames": {k: v for k, v in (state.get("frames") or {}).items() if k != "sample"},
                "cameras_placed": len(state.get("cameras") or []),
                "stray_cameras": state.get("stray_cameras"),
                "mesh": state.get("mesh"),
                "mesh_error": state.get("mesh_error"),
                "artifacts": [{k: v for k, v in a.items() if k != "url"} for a in state.get("artifacts") or []],
            },
            "sleep_gaps": sleep_gaps(self.samples),
            "warnings": warnings(self),
            "samples": self.samples,
        }

    def write(self) -> Path | None:
        try:
            data = self.data()
            out = self.job.work / "exports" / REPORT_NAME
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render_markdown(data))
            return out
        except Exception:
            return None


# --- summaries -----------------------------------------------------------------------

def summarize(samples: list[dict], start: float, end: float) -> dict:
    within = [s for s in samples if start <= s["t"] <= end]
    if not within:
        return {"samples": 0}

    def peak(key):
        values = [s[key] for s in within if s.get(key) is not None]
        return max(values) if values else None

    cpu = [s["cpu_pct"] for s in within if s.get("cpu_pct") is not None]
    swap = [s["swap_mb"] for s in within if s.get("swap_mb") is not None]
    return {
        "samples": len(within),
        "peak_footprint_mb": peak("footprint_mb"),
        "peak_rss_mb": peak("rss_mb"),
        "mean_cpu_pct": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "peak_cpu_pct": max(cpu) if cpu else None,
        "peak_wired_mb": peak("wired_mb"),
        "peak_compressed_mb": peak("compressed_mb"),
        "peak_swap_mb": max(swap) if swap else None,
        "swap_growth_mb": round(max(swap) - swap[0]) if swap else None,
    }


def sleep_gaps(samples: list[dict]) -> list[dict]:
    """Spans with no samples at all: the Mac was asleep (lid closed) or frozen."""
    gaps = []
    for a, b in zip(samples, samples[1:]):
        if b["t"] - a["t"] > SLEEP_GAP_S:
            gaps.append({"from": a["t"], "to": b["t"], "minutes": round((b["t"] - a["t"]) / 60, 1), "stage": a.get("stage")})
    return gaps


def warnings(rec: Recorder) -> list[str]:
    out = []
    ram_mb = (rec.machine.get("ram_bytes") or 0) / 2**20
    peak_fp = max((s.get("footprint_mb") or 0 for s in rec.samples), default=0)
    if ram_mb and peak_fp > 0.75 * ram_mb:
        out.append(f"The pipeline peaked at {peak_fp / 1024:.1f} GB, {peak_fp / ram_mb:.0%} of this Mac's {ram_mb / 1024:.0f} GB.")
    swap = [s["swap_mb"] for s in rec.samples if s.get("swap_mb") is not None]
    if swap and max(swap) - swap[0] > 2048:
        out.append(f"Swap grew by {(max(swap) - swap[0]) / 1024:.1f} GB: memory ran short and the Mac slowed down.")
    for gap in sleep_gaps(rec.samples):
        out.append(f"No samples for {gap['minutes']} min during {gap['stage'] or 'the run'}: the Mac was probably asleep.")
    if rec.power_start.get("source") == "Battery" or rec.power_end.get("source") == "Battery":
        out.append("Ran on battery for at least part of the job; it is slower and may be throttled.")
    state = {**rec.job.snapshot()[0], **rec.terminal}
    frames = (state.get("frames") or {}).get("count")
    cameras = len(state.get("cameras") or [])
    if frames and cameras and cameras / frames < 0.8:
        out.append(f"Only {cameras}/{frames} frames ({cameras / frames:.0%}) were placed by camera solving.")
    if state.get("stray_cameras"):
        out.append(f"{len(state['stray_cameras'])} misplaced camera(s) (off the walking path or outside the room) were dropped.")
    gaussians = next((a.get("gaussians") for a in state.get("artifacts") or [] if a.get("name") == "scene.ply"), None)
    if gaussians and gaussians >= rec.job.preset.max_splats:
        out.append(f"Training hit the {rec.job.preset.max_splats:,} splat cap; growth stopped early.")
    images = rec.notes.get("frames", {}).get("image_stats") or {}
    if (images.get("brightness_spread") or 0) > 0.25:
        out.append("Frame brightness varies a lot across the video; locking exposure would help.")
    return out


# --- rendering -----------------------------------------------------------------------

def _when(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "—"


def _dur(seconds):
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _gb(mb):
    return "—" if mb is None else f"{mb / 1024:.1f} GB"


def _kv_table(d: dict) -> list[str]:
    lines = ["| | |", "|---|---|"]
    rows = []
    for k, v in d.items():
        if isinstance(v, dict):  # one level of nesting reads better as rows
            rows += [(f"{k}: {sk}", sv) for sk, sv in v.items()]
        else:
            rows.append((k, v))
    for k, v in rows:
        if isinstance(v, float):
            v = f"{v:g}"
        elif isinstance(v, (dict, list)):
            v = json.dumps(v)
        lines.append(f"| {k} | {v} |")
    return lines


def render_markdown(data: dict) -> str:
    p = data["project"]
    total = (p["ended"] or time.time()) - p["started"]
    lines = [
        f"# Debug report: {p['name'] or p['id']}",
        "",
        f"**Outcome:** {p['outcome']}" + (f" (failed at {p['failed_stage']}: {p['error']})" if p.get("error") else ""),
        f"**Preset:** {p['preset']} · **Outputs:** {p['outputs']} · **Poses:** {p['pose_backend']} · **Project:** `{p['id']}`",
        f"**Started:** {_when(p['started'])} · **Ended:** {_when(p['ended'])} · **Total:** {_dur(total)}",
        "",
    ]
    if data["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in data["warnings"]] + [""]

    lines += [
        "## Stages",
        "",
        "Memory is the pipeline's footprint (the app plus the tools it launched, GPU memory included). "
        "CPU is a share of the whole machine.",
        "",
        "| Stage | Started | Ended | Took | Peak memory | Mean / peak CPU | Peak swap (growth) | Status |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in data["stages"]:
        cpu = f"{s.get('mean_cpu_pct', '—')}% / {s.get('peak_cpu_pct', '—')}%" if s.get("samples") else "—"
        swap = f"{_gb(s.get('peak_swap_mb'))} (+{_gb(s.get('swap_growth_mb'))})" if s.get("samples") else "—"
        lines.append(
            f"| {s['name']} | {_when(s['start'])[11:]} | {_when(s['end'])[11:]} | {_dur(s['duration_s'])} | "
            f"{_gb(s.get('peak_footprint_mb'))} | {cpu} | {swap} | {s['status'] or 'running'} |"
        )
    lines.append("")

    if data.get("input"):
        lines += ["## Input video", ""] + _kv_table(data["input"]) + [""]
    for section, values in data["notes"].items():
        values = dict(values)
        timeline = values.pop("checkpoints", None) if section == "train" else None
        lines += [f"## {section.capitalize()}", ""] + _kv_table(values) + [""]
        if timeline:
            lines += ["| Step | Time | Splats | Steps/s since previous |", "|---|---|---|---|"]
            prev_step, prev_s = 0, 0
            for c in timeline:
                rate = (c["step"] - prev_step) / (c["seconds"] - prev_s) if c["step"] and c["seconds"] > prev_s else None
                lines.append(f"| {c['step']} | {_dur(c['seconds'])} | {c['splats'] or '—'} | {f'{rate:.1f}' if rate else '—'} |")
                prev_step, prev_s = c["step"] or prev_step, c["seconds"]
            lines.append("")
    results = {k: v for k, v in data["results"].items() if v not in (None, [], {}) and k != "artifacts"}
    lines += ["## Results", ""] + _kv_table(results) + [""]
    if data["results"].get("artifacts"):
        lines += ["| Download | Size | Splats / triangles |", "|---|---|---|"]
        for a in data["results"]["artifacts"]:
            count = a.get("gaussians") or a.get("triangles")
            lines.append(f"| {a['name']} | {(a.get('bytes') or 0) / 2**20:.1f} MB | {f'{count:,}' if count else '—'} |")
        lines.append("")
    lines += ["## Settings", ""] + _kv_table(data["settings"]) + [""]
    if data.get("versions"):
        lines += ["## Tool versions", ""] + _kv_table(data["versions"]) + [""]
    lines += ["## Machine", ""] + _kv_table(data["machine"]) + [""]
    lines += [
        "## Raw data",
        "",
        f"Everything above, plus a resource sample every {SAMPLE_EVERY_S:g} s.",
        "",
        "```json",
        json.dumps(data, indent=1, default=str),
        "```",
        "",
    ]
    return "\n".join(lines)
