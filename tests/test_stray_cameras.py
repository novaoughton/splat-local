import sys
import types
import unittest
from types import SimpleNamespace

import numpy as np

# CI has no pycolmap; drop_stray_cameras only needs a reconstruction-shaped object.
sys.modules.setdefault("pycolmap", types.ModuleType("pycolmap"))
from server.stages.poses_colmap import drop_stray_cameras  # noqa: E402


class FakeRecon:
    def __init__(self, points, cameras):
        self.points3D = {i: SimpleNamespace(xyz=np.array(p, float)) for i, p in enumerate(points)}
        self.images = {
            i: SimpleNamespace(name=f"{i:05d}.jpg", frame_id=i, has_pose=True,
                               projection_center=lambda c=np.array(c, float): c)
            for i, c in enumerate(cameras)
        }
        self.deregistered = []

    def deregister_frame(self, frame_id):
        self.deregistered.append(frame_id)
        self.images[frame_id].has_pose = False


def room(cameras):
    rng = np.random.default_rng(0)
    return FakeRecon(rng.uniform(-2, 2, size=(500, 3)), cameras)


class StrayCameraTests(unittest.TestCase):
    def test_cameras_inside_the_room_stay(self):
        recon = room([[0, 0, 0], [1, 0.5, -1], [-1.5, 0, 1.5]])
        self.assertEqual(drop_stray_cameras(recon), [])
        self.assertEqual(recon.deregistered, [])

    def test_camera_far_outside_is_dropped(self):
        # Library Study Room Full had one 344 spreads out; this one is ~100.
        recon = room([[0, 0, 0], [1, 0, 1], [200, 50, 0]])
        self.assertEqual(drop_stray_cameras(recon), ["00002.jpg"])
        self.assertEqual(recon.deregistered, [2])

    def test_too_few_points_to_judge(self):
        recon = FakeRecon([[0, 0, 0]] * 5, [[1000, 0, 0]])
        self.assertEqual(drop_stray_cameras(recon), [])


if __name__ == "__main__":
    unittest.main()
