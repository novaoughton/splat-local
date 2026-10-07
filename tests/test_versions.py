import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import projects, versions
from server.pipeline import Job
from server.stages import export


def fake_run(outputs):
    """subprocess.run stand-in: answers any command containing a key, else fails like a missing tool."""
    def run(cmd, **kwargs):
        for key, text in outputs.items():
            if key in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
        raise FileNotFoundError(cmd[0])
    return run


class VersionTests(unittest.TestCase):
    def test_collects_each_tool(self):
        outputs = {
            "git": "892e2c4-dirty\n",
            "ffmpeg": "ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with clang\n",
            "brush": "brush-cli 1.0.0\n",
            "splat-transform": "splat-transform v3.9.0 (435b972)\n",
        }
        with patch.object(versions.subprocess, "run", side_effect=fake_run(outputs)), \
             patch.object(versions.train_brush, "_resolve_bin", return_value="/x/brush"), \
             patch.object(versions, "_package", side_effect=lambda name: {"sharp-frames": "0.3.1", "pycolmap": "4.1.0"}[name]):
            found = versions.collect("colmap")
        self.assertEqual(found["splat_local"], "892e2c4-dirty")
        self.assertEqual(found["ffmpeg"], "8.1.2")
        self.assertEqual(found["brush"], "brush-cli 1.0.0")
        self.assertEqual(found["splat_transform"], "splat-transform v3.9.0 (435b972)")
        self.assertEqual((found["sharp_frames"], found["pycolmap"]), ("0.3.1", "4.1.0"))
        self.assertIn("python", found)
        self.assertIn("colmap", found)
        self.assertNotIn("da3_model", found)

    def test_missing_tools_record_none(self):
        with patch.object(versions.subprocess, "run", side_effect=fake_run({})), \
             patch.object(versions, "_package", return_value=None):
            found = versions.collect("colmap")
        for key in ("splat_local", "ffmpeg", "brush", "splat_transform", "sharp_frames", "pycolmap"):
            self.assertIsNone(found[key], key)

    def test_timeouts_record_none(self):
        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        with patch.object(versions.subprocess, "run", side_effect=hang):
            self.assertIsNone(versions._first_line(["npx", "x"]))

    def test_splat_transform_is_pinned(self):
        self.assertEqual(export._NPX_CMD[-1], f"@playcanvas/splat-transform@{export.SPLAT_TRANSFORM_VERSION}")
        setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
        self.assertIn(f"@playcanvas/splat-transform@{export.SPLAT_TRANSFORM_VERSION} ", setup)

    def test_project_file_records_versions_and_preset_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Job("v", "preview", "colmap")
            job.work = Path(temp)
            job.update(versions={"brush": "brush-cli 1.0.0"})
            projects.save(job)
            data = json.loads((job.work / projects.PROJECT_FILE).read_text())
        self.assertEqual(data["state"]["versions"], {"brush": "brush-cli 1.0.0"})
        self.assertEqual(data["preset_settings"]["total_steps"], 10_000)
        self.assertEqual(data["preset_settings"]["frame_spacing_s"], 1.0)


if __name__ == "__main__":
    unittest.main()
