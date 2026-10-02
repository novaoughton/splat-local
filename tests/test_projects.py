import json
import tempfile
import unittest
from pathlib import Path

from server import projects
from server.pipeline import Job


class ProjectPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs_dir = Path(self.temp.name)

    def make_job(self, job_id, preset="high", **state):
        job = Job(job_id, preset, "colmap")
        job.work = self.jobs_dir / job_id
        job.work.mkdir()
        job.update(**state)
        return job

    def test_finished_project_round_trips(self):
        artifacts = [{"name": "scene.ply", "url": "/api/jobs/a/files/exports/scene.ply", "bytes": 5, "gaussians": 1178163}]
        job = self.make_job(
            "a", name="Bedroom", created=100.0, stage="done", artifacts=artifacts,
            frames={"count": 2, "sample": ["/api/jobs/a/files/frames/1.jpg"]},
        )
        projects.save(job)

        [loaded] = projects.load_all(self.jobs_dir)
        state, _ = loaded.snapshot()
        self.assertEqual((loaded.id, loaded.preset_name, loaded.pose_backend), ("a", "high", "colmap"))
        self.assertEqual(loaded.work, job.work)
        self.assertEqual(state["stage"], "done")
        self.assertEqual(state["artifacts"], artifacts)
        self.assertEqual(projects.summary(loaded), {
            "id": "a", "name": "Bedroom", "created": 100.0, "stage": "done", "error": None, "failed_stage": None,
            "preset": "high", "gaussians": 1178163, "thumbnail": "/api/jobs/a/files/frames/1.jpg",
            "bytes": (job.work / projects.PROJECT_FILE).stat().st_size,
        })

    def test_project_cut_off_mid_run_loads_as_interrupted(self):
        job = self.make_job(
            "b", name="Desk", stage="train",
            checkpoint={"url": "/api/jobs/b/files/checkpoints/preview_04000.ply", "step": 4000},
        )
        projects.save(job)

        [loaded] = projects.load_all(self.jobs_dir)
        state, _ = loaded.snapshot()
        self.assertEqual(state["stage"], "error")
        self.assertEqual(state["error"], projects.INTERRUPTED)
        self.assertIsNone(state["checkpoint"])
        self.assertFalse(loaded.running)

    def test_folders_without_a_usable_project_file_load_as_unsaved_runs(self):
        projects.save(self.make_job("good", stage="done"))
        (self.jobs_dir / "garbled").mkdir()
        (self.jobs_dir / "garbled" / projects.PROJECT_FILE).write_text("{not json")
        (self.jobs_dir / "old").mkdir()
        (self.jobs_dir / "old" / projects.PROJECT_FILE).write_text(json.dumps(
            {"id": "old", "preset": "retired", "pose_backend": "colmap", "state": {"stage": "done"}}
        ))
        (self.jobs_dir / "legacy").mkdir()  # a job folder from before project.json existed
        (self.jobs_dir / "legacy" / "frame.jpg").write_bytes(b"x" * 10)
        (self.jobs_dir / ".hidden").mkdir()
        (self.jobs_dir / "stray.txt").write_text("not a folder")

        loaded = {j.id: j for j in projects.load_all(self.jobs_dir)}
        self.assertEqual(sorted(loaded), ["garbled", "good", "legacy", "old"])
        for job_id in ("garbled", "legacy", "old"):
            summary = projects.summary(loaded[job_id])
            self.assertEqual((summary["stage"], summary["error"], summary["preset"]),
                             ("error", projects.UNSAVED, "unknown"))
            self.assertEqual(summary["name"], f"Unsaved run {job_id}")
            self.assertFalse(loaded[job_id].running)
        self.assertEqual(projects.summary(loaded["legacy"])["bytes"], 10)

    def test_load_all_on_a_missing_jobs_dir(self):
        self.assertEqual(projects.load_all(self.jobs_dir / "nope"), [])

    def test_delete_removes_the_whole_folder(self):
        job = self.make_job("d", stage="done")
        (job.work / "exports").mkdir()
        (job.work / "exports" / "scene.ply").write_bytes(b"splats")
        projects.save(job)

        projects.delete(job, self.jobs_dir)
        self.assertFalse(job.work.exists())
        self.assertEqual(projects.load_all(self.jobs_dir), [])

    def test_delete_refuses_folders_outside_the_jobs_dir(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        job = Job("e", "high", "colmap")
        job.work = Path(outside.name) / "e"
        job.work.mkdir()
        with self.assertRaises(ValueError):
            projects.delete(job, self.jobs_dir)
        self.assertTrue(job.work.exists())

        job.work = self.jobs_dir  # the jobs dir itself
        with self.assertRaises(ValueError):
            projects.delete(job, self.jobs_dir)
        self.assertTrue(self.jobs_dir.exists())

    def test_save_leaves_no_temporary_file(self):
        job = self.make_job("c", stage="done")
        projects.save(job)
        projects.save(job)
        self.assertEqual(sorted(p.name for p in job.work.iterdir()), [projects.PROJECT_FILE])

    def test_clean_name(self):
        self.assertEqual(projects.clean_name("  my   room \n", "x"), "my room")
        self.assertEqual(projects.clean_name("", "IMG_0042"), "IMG_0042")
        self.assertEqual(projects.clean_name(None, "IMG_0042"), "IMG_0042")
        self.assertEqual(len(projects.clean_name("a" * 200, "x")), projects.MAX_NAME_LENGTH)


if __name__ == "__main__":
    unittest.main()
