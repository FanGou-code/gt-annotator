import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bbox import normalize_bbox


class NormalizeBboxTests(unittest.TestCase):
    def test_valid_box_passthrough(self):
        self.assertEqual(normalize_bbox([0.1, 0.2, 0.3, 0.4]), [0.1, 0.2, 0.3, 0.4])

    def test_full_frame_box(self):
        self.assertEqual(normalize_bbox([0, 0, 1, 1]), [0.0, 0.0, 1.0, 1.0])

    def test_clipping_to_unit_square(self):
        self.assertEqual(normalize_bbox([-0.5, 0.2, 1.5, 0.8]), [0.0, 0.2, 1.0, 0.8])

    def test_numeric_strings_accepted(self):
        self.assertEqual(normalize_bbox(["0.1", "0.2", "0.3", "0.4"]), [0.1, 0.2, 0.3, 0.4])

    def test_rejects_wrong_length(self):
        for values in ([], [0.1, 0.2, 0.3], [0.1] * 5):
            with self.assertRaises(ValueError):
                normalize_bbox(values)

    def test_rejects_scalar_and_string_input(self):
        for values in (0.5, "0.1,0.2,0.3,0.4", None):
            with self.assertRaises(ValueError):
                normalize_bbox(values)

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            normalize_bbox([0.1, "x", 0.3, 0.4])

    def test_rejects_non_finite(self):
        for values in ([float("nan"), 0.1, 0.3, 0.4], [0.1, float("inf"), 0.3, 0.4]):
            with self.assertRaises(ValueError):
                normalize_bbox(values)

    def test_rejects_inverted_box(self):
        with self.assertRaises(ValueError):
            normalize_bbox([0.5, 0.5, 0.4, 0.6])
        with self.assertRaises(ValueError):
            normalize_bbox([0.1, 0.5, 0.3, 0.5])

    def test_rejects_empty_after_clipping(self):
        with self.assertRaises(ValueError):
            normalize_bbox([-1.0, -1.0, -0.5, -0.5])


if __name__ == "__main__":
    unittest.main()
