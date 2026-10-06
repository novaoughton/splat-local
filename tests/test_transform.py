import copy
import unittest

from server.projects import clean_transform

VALID = {
    "assets": {"position": [1, 2, 3], "rotation_deg": [0, 90, 0], "scale": 0.52},
    "anchor": {"position": [0, 0, 0], "rotation_deg": [0, -30.5, 0]},
    "scaled": True,
    "level_up": [0.01, -0.99, 0.02],
}


class CleanTransformTests(unittest.TestCase):
    def test_valid_transform_round_trips_as_floats(self):
        t = clean_transform(VALID)
        self.assertEqual(t["assets"]["position"], [1.0, 2.0, 3.0])
        self.assertEqual(t["assets"]["scale"], 0.52)
        self.assertTrue(t["scaled"])
        self.assertEqual(t["level_up"], [0.01, -0.99, 0.02])

    def test_level_up_is_optional(self):
        data = copy.deepcopy(VALID)
        del data["level_up"]
        self.assertIsNone(clean_transform(data)["level_up"])

    def test_rejects_malformed(self):
        bad = []
        for path, value in [
            (("assets", "scale"), 0),
            (("assets", "scale"), -1),
            (("assets", "position"), [1, 2]),
            (("anchor", "rotation_deg"), [0, float("nan"), 0]),
            (("anchor", "position"), [0, "1", 0]),
            (("assets", "position"), [True, 0, 0]),
        ]:
            data = copy.deepcopy(VALID)
            data[path[0]][path[1]] = value
            bad.append(data)
        bad += [None, [], {"assets": VALID["assets"]}]
        for data in bad:
            with self.assertRaises(ValueError, msg=data):
                clean_transform(data)


if __name__ == "__main__":
    unittest.main()
