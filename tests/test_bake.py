import asyncio
import importlib.util
import json
import math
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from server import bake, pipeline
from server.pipeline import Job

UP = np.array([0.0, 1.0, 0.0])
IDENTITY = {"position": [0, 0, 0], "rotation_deg": [0, 0, 0]}


def transform(assets=None, anchor=None, level_up=None, scaled=False):
    return {"assets": {**IDENTITY, "scale": 1.0, **(assets or {})}, "anchor": {**IDENTITY, **(anchor or {})},
            "level_up": level_up, "scaled": scaled}


class MathTests(unittest.TestCase):
    def test_euler_order_is_yxz(self):
        x, y, z = 20, -35, 70
        expect = bake._rot_y(math.radians(y)) @ bake._rot_x(math.radians(x)) @ bake._rot_z(math.radians(z))
        np.testing.assert_allclose(bake.euler_yxz([x, y, z]), expect)

    def test_levelling_turns_the_scene_up_onto_y(self):
        for up in ([0.1, -0.95, 0.2], [0, -1, 0], [0, 1, 0], [1, 0, 0]):
            u = np.array(up, float) / np.linalg.norm(up)
            W = bake.world_rotation(up)
            np.testing.assert_allclose(W @ u, UP, atol=1e-12)
            np.testing.assert_allclose(W @ W.T, np.eye(3), atol=1e-12)
            self.assertAlmostEqual(np.linalg.det(W), 1.0)
        np.testing.assert_allclose(bake.world_rotation(None), bake.FLIP)

    def test_identity_transform_is_just_the_world_turn(self):
        s, R, t = bake.export_similarity(transform(level_up=[0, -1, 0]))
        self.assertAlmostEqual(s, 1.0)
        np.testing.assert_allclose(R, bake.world_rotation([0, -1, 0]), atol=1e-12)
        np.testing.assert_allclose(t, 0, atol=1e-12)

    def test_a_point_lands_in_the_anchor_frame(self):
        # COLMAP up is -y; assets scaled by 2 and lifted 1; anchor at (0, 1, 3) turned 90° about Y.
        t = transform(assets={"position": [0, 1, 0], "scale": 2.0}, anchor={"position": [0, 1, 3], "rotation_deg": [0, 90, 0]},
                      level_up=[0, -1, 0])
        s, R, tt = bake.export_similarity(t)
        p = np.array([1.0, -2.0, 0.0])  # 2 units "up" in COLMAP, 1 along x
        # world: flip → (1, 2, 0); assets: ×2 + (0,1,0) → (2, 5, 0); anchor⁻¹: −(0,1,3) → (2, 4, −3), then −90° about Y
        expect = bake._rot_y(math.radians(-90)) @ np.array([2.0, 4.0, -3.0])
        np.testing.assert_allclose(s * R @ p + tt, expect, atol=1e-12)

    def test_splat_transform_args_round_trip(self):
        rng = np.random.default_rng(1)
        for _ in range(50):
            t = transform(assets={"position": list(rng.uniform(-5, 5, 3)), "rotation_deg": list(rng.uniform(-180, 180, 3)),
                                  "scale": float(rng.uniform(0.1, 3))},
                          anchor={"position": list(rng.uniform(-5, 5, 3)), "rotation_deg": list(rng.uniform(-180, 180, 3))},
                          level_up=list(rng.uniform(-1, 1, 3)))
            s, R, tt = bake.export_similarity(t)
            args = bake.splat_transform_args(s, R, tt)
            x, y, z = (math.radians(float(v)) for v in args[3].split(","))
            Rst = bake._rot_z(z) @ bake._rot_y(y) @ bake._rot_x(x)
            np.testing.assert_allclose(bake._F @ Rst @ bake._F, R, atol=1e-7)
            np.testing.assert_allclose(bake._F @ np.array([float(v) for v in args[5].split(",")]), tt, atol=1e-6)
            self.assertAlmostEqual(float(args[1]), s, places=6)

    def test_gimbal_lock_still_round_trips(self):
        R = bake._rot_z(0.3) @ bake._rot_y(math.pi / 2) @ bake._rot_x(0.4)
        x, y, z = (math.radians(v) for v in bake.euler_zyx_deg(R))
        np.testing.assert_allclose(bake._rot_z(z) @ bake._rot_y(y) @ bake._rot_x(x), R, atol=1e-9)


OBJ = "mtllib mesh.mtl\nv 1 -2 0\nv 0 0 0\nv 0 -1 1\nvn 0 -1 0\nvt 0 0\nf 1/1/1 2/1/1 3/1/1\n"


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Job("b", "high", "colmap", "mesh")
        self.job.work = Path(self.temp.name)
        mesh = self.job.work / "exports" / "mesh"
        mesh.mkdir(parents=True)
        (mesh / "mesh.obj").write_text(OBJ)
        (mesh / "mesh.mtl").write_text("newmtl m\n")
        (mesh / "tex.png").write_bytes(b"png")

    def test_mesh_only_project_exports_mesh_and_transform(self):
        t = transform(assets={"scale": 2.0}, level_up=[0, -1, 0], scaled=True)
        self.job.update(stage="done", transform=t)
        files = bake.run(self.job)
        self.assertEqual([f["name"] for f in files], [bake.MESH_ZIP_NAME, bake.TRANSFORM_NAME])
        with zipfile.ZipFile(self.job.work / "exports" / bake.MESH_ZIP_NAME) as zf:
            self.assertEqual(sorted(zf.namelist()), ["mesh-unity/mesh.mtl", "mesh-unity/mesh.obj",
                                                     "mesh-unity/tex.png", "mesh-unity/transform.json"])
            first = zf.read("mesh-unity/mesh.obj").decode().splitlines()[1]
        self.assertEqual(first, "v 2.000000 4.000000 0.000000")  # flipped upright, doubled
        record = json.loads((self.job.work / "exports" / bake.TRANSFORM_NAME).read_text())
        self.assertEqual(record["units"], "metres")
        np.testing.assert_allclose(np.array(record["matrix"]) @ [1, -2, 0, 1], [2, 4, 0, 1], atol=1e-9)

    def test_no_transform_is_refused(self):
        self.job.update(stage="done")
        with self.assertRaises(ValueError):
            bake.run(self.job)


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "CI installs numpy only")
class EndpointTests(unittest.TestCase):
    def call(self, job, active=None):
        from fastapi import HTTPException

        from server import main
        with patch.dict(main.JOBS, {job.id: job}, clear=True), patch.object(pipeline, "active_job_id", return_value=active):
            try:
                asyncio.run(main.unity_export(job.id))
            except HTTPException as exc:
                return exc.status_code
        return 202

    def test_refusals(self):
        job = Job("e", "high", "colmap")
        job.update(stage="train")
        self.assertEqual(self.call(job), 409)  # not finished
        job.update(stage="done")
        self.assertEqual(self.call(job, active="other"), 409)  # something is training
        self.assertEqual(self.call(job), 400)  # no transform yet


if __name__ == "__main__":
    unittest.main()
