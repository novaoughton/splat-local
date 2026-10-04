import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server.stages
from server import failures, pipeline, projects
from server.pipeline import Job


class ExplainTests(unittest.TestCase):
    def test_too_few_registered_frames_points_at_the_footage(self):
        f = failures.explain("poses", "only 24/200 frames registered — recapture the scene with more overlap")
        self.assertEqual(f["stage"], "poses")
        self.assertEqual(f["title"], "Couldn't work out where the camera was")
        self.assertIn("Only 24 of 200 frames", f["detail"])
        self.assertTrue(f["capture_guide"])
        self.assertEqual(f["tips"], failures.CAPTURE_TIPS)

    def test_no_poses_at_all(self):
        f = failures.explain("poses", "COLMAP could not reconstruct any camera poses — recapture")
        self.assertIn("None of the frames", f["detail"])
        self.assertTrue(f["capture_guide"])

    def test_each_known_error(self):
        cases = [
            ("frames", "no frames were extracted from the video", "Couldn't read the video"),
            ("frames", "Command '['ffprobe', '-v', 'error']' returned non-zero exit status 1.", "Couldn't read the video"),
            ("poses", "DA3 backend not installed — run: uv sync --group da3", "Depth Anything 3 isn't installed"),
            ("train", "brush training failed:\nthread panicked: out of memory", "Training crashed"),
            ("train", "brush training produced no checkpoints", "Training produced no result"),
            ("export", "no trained checkpoint to export", "Nothing to export"),
            ("train", "[Errno 28] No space left on device: 'export_500.ply'", "Ran out of disk space"),
            ("train", projects.INTERRUPTED, "The app was closed mid-run"),
            (None, projects.UNSAVED, "Nothing to open"),
        ]
        for stage, error, title in cases:
            with self.subTest(error=error):
                f = failures.explain(stage, error)
                self.assertEqual(f["title"], title)
                self.assertFalse(f["capture_guide"])

    def test_rules_only_match_their_own_stage(self):
        # "ffmpeg" in a training error is not a video-decoding problem.
        f = failures.explain("train", "ffmpeg something")
        self.assertEqual(f["title"], "Failed during training")

    def test_unknown_error_names_the_stage(self):
        self.assertEqual(failures.explain("poses", "Segmentation fault")["title"], "Failed during camera positions")
        self.assertEqual(failures.explain(None, None)["title"], "Failed during the run")


class PipelineFailureTests(unittest.TestCase):
    def test_failed_stage_and_explanation_are_recorded(self):
        def poses_fail(job, work, preset):
            raise RuntimeError("only 24/200 frames registered — recapture the scene")

        fakes = {
            "frames": SimpleNamespace(run=lambda job, work, preset: None),
            "poses_colmap": SimpleNamespace(run=poses_fail),
            "poses_da3": SimpleNamespace(run=poses_fail),
            "train_brush": SimpleNamespace(run=lambda *a: self.fail("training should not run")),
            "export": SimpleNamespace(run=lambda *a: self.fail("export should not run")),
        }
        modules = {f"server.stages.{name}": module for name, module in fakes.items()}
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, modules), \
             patch.multiple(server.stages, create=True, **fakes):
            job = Job("f", "high", "colmap")
            job.work = Path(temp)
            pipeline._run_sync(job)
            state, _ = job.snapshot()

        self.assertEqual(state["stage"], "error")
        self.assertEqual(state["failed_stage"], "poses")
        self.assertEqual(state["failure"]["title"], "Couldn't work out where the camera was")
        self.assertIn("only 24/200", state["error"])

    def test_interrupted_project_keeps_the_stage_it_stopped_in(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Job("g", "high", "colmap")
            job.work = Path(temp) / "g"
            job.work.mkdir()
            job.update(stage="train")
            projects.save(job)
            [loaded] = projects.load_all(Path(temp))
        state, _ = loaded.snapshot()
        self.assertEqual(state["failed_stage"], "train")
        self.assertEqual(state["failure"]["title"], "The app was closed mid-run")
        self.assertEqual(projects.summary(loaded)["failed_stage"], "train")


if __name__ == "__main__":
    unittest.main()
