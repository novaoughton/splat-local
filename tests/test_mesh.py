import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import server.stages
from server import align, failures, pipeline, projects, versions
from server.pipeline import Job
from server.stages import mesh as mesh_stage


def rotation(axis, deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    x, y, z = np.array(axis, float) / np.linalg.norm(axis)
    return np.array([
        [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
        [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
        [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
    ])


# A transform from "Object Capture space" to "COLMAP space" for the tests to recover.
S, R, T = 4.0, rotation([1, 2, 3], 70), np.array([0.5, -1.0, 2.0])


class AlignTests(unittest.TestCase):
    def test_similarity_recovers_a_known_transform(self):
        src = np.random.default_rng(1).normal(size=(30, 3))
        s, Rf, t = align.similarity(src, align.apply(S, R, T, src))
        self.assertAlmostEqual(s, S, places=6)
        np.testing.assert_allclose(Rf, R, atol=1e-6)
        np.testing.assert_allclose(t, T, atol=1e-6)

    def test_robust_fit_ignores_frames_the_solvers_disagree_on(self):
        rng = np.random.default_rng(2)
        src = rng.normal(size=(60, 3))
        dst = align.apply(S, R, T, src) + rng.normal(scale=0.002, size=(60, 3))
        dst[:12] += rng.normal(scale=2.0, size=(12, 3))  # 20% badly solved frames
        s, Rf, t, keep, _ = align.robust_similarity(src, dst)
        self.assertFalse(keep[:12].any())
        self.assertAlmostEqual(s, S, places=2)
        np.testing.assert_allclose(Rf, R, atol=2e-3)

    def test_fit_cameras_matches_by_frame_name(self):
        src = np.random.default_rng(3).normal(size=(20, 3))
        names = [f"frame_{i:05d}.jpg" for i in range(20)]
        poses = [{"name": n, "t": p.tolist()} for n, p in zip(names, src)]
        poses.append({"name": "frame_99999.jpg", "t": [9, 9, 9]})  # Object Capture only
        centres = dict(zip(names, align.apply(S, R, T, src)))
        s, _, _, stats = align.fit_cameras(poses, centres)
        self.assertAlmostEqual(s, S, places=5)
        self.assertEqual((stats["matched"], stats["used"]), (20, 20))
        self.assertLess(stats["residual_pct"], 0.01)

    def test_fit_cameras_needs_enough_shared_frames(self):
        poses = [{"name": f"f{i}.jpg", "t": [i, 0, 0]} for i in range(5)]
        with self.assertRaisesRegex(ValueError, "frames were placed by both"):
            align.fit_cameras(poses, {f"f{i}.jpg": np.array([i, 0.0, 0]) for i in range(5)})

    def test_transform_obj_moves_vertices_and_turns_normals(self):
        obj = textwrap.dedent("""\
            # test
            mtllib baked_mesh_x.mtl
            v 1 0 0
            v 0 1 0 0.5 0.5 0.5
            v 0 0 1
            v 1 1 1
            vt 0.25 0.75
            vn 0 0 1
            usemtl g0
            f 1/1/1 2/1/1 3/1/1
            f 1/1/1 2/1/1 3/1/1 4/1/1
            """)
        with tempfile.TemporaryDirectory() as temp:
            src, dst = Path(temp) / "in.obj", Path(temp) / "out.obj"
            src.write_text(obj)
            shape = align.transform_obj(src, dst, S, R, T, mtllib="mesh.mtl")
            lines = dst.read_text().splitlines()
        self.assertEqual(shape, {"vertices": 4, "triangles": 3})  # a triangle and a quad
        self.assertIn("mtllib mesh.mtl", lines)
        vs = [l.split() for l in lines if l.startswith("v ")]
        np.testing.assert_allclose([float(x) for x in vs[0][1:4]], align.apply(S, R, T, np.array([1.0, 0, 0])), atol=1e-5)
        self.assertEqual(vs[1][4:], ["0.5", "0.5", "0.5"])  # vertex colour kept
        n = [float(x) for x in next(l for l in lines if l.startswith("vn ")).split()[1:]]
        np.testing.assert_allclose(n, R @ [0, 0, 1], atol=1e-5)
        for kept in ("vt 0.25 0.75", "usemtl g0", "f 1/1/1 2/1/1 3/1/1 4/1/1"):
            self.assertIn(kept, lines)


class StageSelectionTests(unittest.TestCase):
    def test_outputs_choose_the_stages(self):
        self.assertEqual(pipeline.stage_names("splat"), ["frames", "poses", "train", "export"])
        self.assertEqual(pipeline.stage_names("mesh"), ["frames", "poses", "mesh"])
        self.assertEqual(pipeline.stage_names("both"), ["frames", "poses", "mesh", "train", "export"])

    def run_with(self, outputs, mesh_run):
        ran = []
        def stage(name):
            return SimpleNamespace(run=lambda job, work, preset: ran.append(name))
        fakes = {"frames": stage("frames"), "poses_colmap": stage("poses"), "poses_da3": stage("poses"),
                 "train_brush": stage("train"), "export": stage("export"), "mesh": SimpleNamespace(run=mesh_run)}
        modules = {f"server.stages.{n}": m for n, m in fakes.items()}
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, modules), \
             patch.multiple(server.stages, create=True, **fakes):
            job = Job("j", "high", "colmap", outputs)
            job.work = Path(temp)
            pipeline._run_sync(job)
            return job.snapshot()[0], ran

    def failing_mesh(self, job, work, preset):
        raise RuntimeError("object capture failed: not enough overlap")

    def test_a_mesh_failure_doesnt_stop_the_splat(self):
        state, ran = self.run_with("both", self.failing_mesh)
        self.assertEqual(state["stage"], "done")
        self.assertEqual(ran, ["frames", "poses", "train", "export"])
        self.assertIn("not enough overlap", state["mesh_error"])
        self.assertEqual(state["mesh_failure"]["title"], "Couldn't build the mesh")

    def test_a_mesh_only_failure_fails_the_job(self):
        state, ran = self.run_with("mesh", self.failing_mesh)
        self.assertEqual((state["stage"], state["failed_stage"]), ("error", "mesh"))
        self.assertEqual(state["failure"]["title"], "Couldn't build the mesh")
        self.assertEqual(ran, ["frames", "poses"])

    def test_cancelling_during_the_mesh_still_cancels(self):
        def cancelled(job, work, preset):
            raise pipeline.JobCancelled()
        state, ran = self.run_with("both", cancelled)
        self.assertEqual(state["stage"], "cancelled")
        self.assertEqual(ran, ["frames", "poses"])


FAKE_OBJCAP = '''#!{python}
# Stand-in for tools/objcap: writes a mesh and poses in "Object Capture space",
# i.e. COLMAP space mapped through the inverse of the test transform.
import json, sys
from pathlib import Path
images, out, detail, poses = sys.argv[1:5]
spec = json.loads(Path({spec!r}).read_text())
out = Path(out); out.mkdir(parents=True, exist_ok=True)
print("progress 0.500")
(out / "baked_mesh_t.obj").write_text("mtllib baked_mesh_t.mtl\\n" + "".join(f"v {{x}} {{y}} {{z}}\\n" for x, y, z in spec["verts"]) + "f 1 2 3\\n")
(out / "baked_mesh_t.mtl").write_text("newmtl g0\\nmap_Kd baked_mesh_t_tex0.png\\n")
(out / "baked_mesh_t_tex0.png").write_bytes(b"png")
names = sorted(p.name for p in Path(images).iterdir())
Path(poses).write_text(json.dumps({{"poses": [{{"id": i, "name": n, "t": spec["oc"][n]}} for i, n in enumerate(names) if n in spec["oc"]]}}))
print("poses", len(spec["oc"]))
print("progress 1.000")
print("done invalid=0 skipped=2")
'''


class MeshStageTests(unittest.TestCase):
    def test_mesh_stage_writes_an_aligned_mesh_and_a_download(self):
        rng = np.random.default_rng(4)
        names = [f"frame_{i:05d}.jpg" for i in range(1, 25)]
        colmap = {n: rng.normal(size=3) for n in names}
        inv = lambda p: (R.T @ (p - T)) / S  # COLMAP space -> "Object Capture space"
        verts_colmap = rng.normal(size=(3, 3))
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "job"
            (work / "frames").mkdir(parents=True)
            for n in names:
                (work / "frames" / n).write_bytes(b"jpg")
            spec = Path(temp) / "spec.json"
            spec.write_text(json.dumps({"oc": {n: inv(p).tolist() for n, p in colmap.items()},
                                        "verts": [inv(v).tolist() for v in verts_colmap]}))
            fake = Path(temp) / "objcap"
            fake.write_text(FAKE_OBJCAP.format(python=sys.executable, spec=str(spec)))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            future = time.time() + 3600  # newer than tools/objcap/main.swift, so it isn't rebuilt
            os.utime(fake, (future, future))

            job = Job("m", "high", "colmap", "both")
            job.work = work
            job.update(artifacts=[{"name": "scene.ply", "url": "u", "bytes": 1}])
            with patch.dict(os.environ, {"OBJCAP_BIN": str(fake)}), \
                 patch.object(mesh_stage, "colmap_centres", return_value=colmap):
                mesh_stage.run(job, work, job.preset)
            state, _ = job.snapshot()

            out = work / "exports" / "mesh"
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["baked_mesh_t_tex0.png", "mesh.mtl", "mesh.obj"])
            obj = (out / "mesh.obj").read_text().splitlines()
            self.assertEqual(obj[0], "mtllib mesh.mtl")
            moved = np.array([[float(x) for x in l.split()[1:4]] for l in obj if l.startswith("v ")])
            np.testing.assert_allclose(moved, verts_colmap, atol=1e-4)  # back on COLMAP's (the splat's) coordinates
            with zipfile.ZipFile(work / "exports" / "mesh.zip") as zf:
                self.assertEqual(sorted(zf.namelist()), ["mesh/baked_mesh_t_tex0.png", "mesh/mesh.mtl", "mesh/mesh.obj"])
            self.assertTrue(all(p.is_symlink() for p in (work / "mesh_input").iterdir()))

        self.assertEqual(state["mesh"]["triangles"], 1)
        self.assertEqual((state["mesh"]["frames_used"], state["mesh"]["frames_total"]), (22, 24))
        self.assertEqual(state["mesh"]["alignment"]["matched"], 24)
        self.assertEqual([a["name"] for a in state["artifacts"]], ["scene.ply", "mesh.zip"])
        self.assertEqual(state["progress"], 1.0)

    def test_unsupported_mac_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "frames").mkdir()
            (work / "frames" / "frame_00001.jpg").write_bytes(b"jpg")
            fake = work / "objcap"
            fake.write_text(f"#!{sys.executable}\nprint('error Object Capture is not supported on this Mac')\nraise SystemExit(2)\n")
            fake.chmod(0o755)
            future = time.time() + 3600
            os.utime(fake, (future, future))
            job = Job("u", "high", "colmap", "mesh")
            job.work = work
            with patch.dict(os.environ, {"OBJCAP_BIN": str(fake)}), \
                 self.assertRaisesRegex(RuntimeError, "not supported on this Mac"):
                mesh_stage.run(job, work, job.preset)
        self.assertEqual(failures.explain("mesh", "Object Capture is not supported on this Mac")["title"],
                         "This Mac can't build meshes")


class MeshProjectTests(unittest.TestCase):
    def test_outputs_and_mesh_survive_a_restart_and_clean_keeps_the_mesh(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs_dir = Path(temp)
            job = Job("k", "high", "colmap", "mesh")
            job.work = jobs_dir / "k"
            for rel in ("exports/mesh/mesh.obj", "exports/mesh.zip", "mesh_input/frame_00001.jpg",
                        "mesh_raw/model/baked.obj", "frames/frame_00001.jpg", "dataset/images/a.jpg"):
                (job.work / rel).parent.mkdir(parents=True, exist_ok=True)
                (job.work / rel).write_bytes(b"x" * 10)
            job.update(stage="done", frames={"count": 1, "sample": [job.file_url("frames/frame_00001.jpg")]},
                       mesh={"url": job.file_url("exports/mesh/mesh.obj"), "triangles": 63104})
            projects.save(job)

            [loaded] = projects.load_all(jobs_dir)
            self.assertEqual(loaded.outputs, "mesh")
            summary = projects.summary(loaded)
            self.assertEqual((summary["outputs"], summary["triangles"]), ("mesh", 63104))

            projects.clean(loaded)
            left = sorted(str(p.relative_to(job.work)) for p in job.work.rglob("*") if p.is_file())
            self.assertEqual(left, ["exports/mesh.zip", "exports/mesh/mesh.obj", "frames/frame_00001.jpg", "project.json"])

    def test_projects_saved_before_outputs_existed_load_as_splat(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "old").mkdir()
            (Path(temp) / "old" / projects.PROJECT_FILE).write_text(json.dumps(
                {"id": "old", "preset": "high", "pose_backend": "colmap", "state": {"stage": "done"}}))
            [loaded] = projects.load_all(Path(temp))
        self.assertEqual(loaded.outputs, "splat")

    def test_versions_record_macos_for_meshes(self):
        with patch.object(versions, "_first_line", return_value="26.3.1"), \
             patch.object(versions, "_package", return_value=None):
            self.assertEqual(versions.collect("colmap", "both")["macos"], "26.3.1")
            self.assertNotIn("macos", versions.collect("colmap", "splat"))


if __name__ == "__main__":
    unittest.main()
