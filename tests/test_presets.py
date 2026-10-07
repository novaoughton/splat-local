import unittest

from server.presets import PRESETS, frame_plan


class FramePlanTests(unittest.TestCase):
    def test_long_video_keeps_the_preset_spacing(self):
        # Dorm Video 3 (5m27s) on High: one frame per 0.8 s.
        count, spacing = frame_plan(PRESETS["high"], 327)
        self.assertEqual(count, 409)
        self.assertAlmostEqual(spacing, 0.8, places=2)

    def test_short_video_gets_the_floor_and_closer_frames(self):
        count, spacing = frame_plan(PRESETS["high"], 30)
        self.assertEqual(count, PRESETS["high"].min_frames)
        self.assertLess(spacing, PRESETS["high"].frame_spacing_s)

    def test_higher_presets_take_more_frames(self):
        counts = [frame_plan(PRESETS[name], 420)[0] for name in ("preview", "high", "max")]
        self.assertEqual(counts, [420, 525, 600])

    def test_zero_length_video_does_not_divide_by_zero(self):
        count, spacing = frame_plan(PRESETS["preview"], 0)
        self.assertEqual(count, PRESETS["preview"].min_frames)
        self.assertGreater(spacing, 0)


if __name__ == "__main__":
    unittest.main()
