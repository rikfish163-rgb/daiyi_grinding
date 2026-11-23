"""Registered reference depth detection and ROI classification regression."""

import unittest

import numpy as np

from daiyi_grinding.perception import detect
from daiyi_grinding.reference_detection import detect_reference


class ReferenceDetectionTests(unittest.TestCase):
    def setUp(self):
        self.shape = (240, 320)
        self.K = np.array([[1010.0, 0, 159.5], [0, 1010.0, 119.5], [0, 0, 1.0]])
        self.T = np.eye(4)
        self.T[:3, :3] = np.diag([1.0, -1.0, -1.0])
        self.T[:3, 3] = [-0.49, -0.13, 1.13]
        self.rgb = np.full((*self.shape, 3), 160, dtype=np.uint8)
        self.reference = np.full(self.shape, 0.505, dtype=np.float32)

    def detect(self, depth, reference=None, roi=None):
        return detect_reference(
            self.rgb,
            depth,
            self.reference if reference is None else reference,
            self.K,
            self.T,
            roi=roi,
        )[0]

    def test_normal_design_rib_in_both_views_is_preserved(self):
        nominal = self.reference.copy()
        nominal[80:100, 25:295] -= 0.004
        self.assertEqual(self.detect(nominal, nominal), [])

    def test_thin_positive_ridge_is_detected(self):
        depth = self.reference.copy()
        depth[118:121, 45:280] -= 0.00065
        detections = self.detect(depth)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "seam")

    def test_recess_is_not_positive_excess(self):
        depth = self.reference.copy()
        depth[100:115, 130:155] += 0.003
        self.assertEqual(self.detect(depth), [])

    def test_coarse_protrusion_is_reviewed(self):
        depth = self.reference.copy()
        depth[100:115, 135:150] -= 0.009
        self.assertEqual(self.detect(depth)[0].kind, "review")

    def test_roi_keeps_complete_feature_classification(self):
        depth = self.reference.copy()
        depth[118:121, 45:280] -= 0.001
        polygon = [[100, 100], [130, 100], [130, 140], [100, 140]]
        detected = self.detect(depth, roi=polygon)
        self.assertEqual(detected[0].kind, "seam")
        self.assertLess(len(detected[0].pixels), 200)

    def test_invalid_reference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Too few valid"):
            self.detect(
                self.reference, np.full(self.shape, np.nan, dtype=np.float32)
            )

    def test_configured_volume_translates_geometry_without_changing_masks(self):
        depth = self.reference.copy()
        depth[118:123, 45:280] -= 0.002
        original, _ = detect(self.rgb, depth, self.K, self.T)
        offset = np.array([1.4, 1.2, 0.35])
        translated = self.T.copy()
        translated[:3, 3] += offset
        bounds = (
            np.array([[-0.67, -0.27, 0.612], [-0.31, 0.03, 0.655]]) + offset
        )
        moved, _ = detect(
            self.rgb, depth, self.K, translated, inspection_bounds=bounds
        )
        self.assertTrue(original)
        self.assertEqual(len(original), len(moved))
        for before, after in zip(original, moved):
            self.assertEqual(before.kind, after.kind)
            np.testing.assert_array_equal(before.pixels, after.pixels)
            np.testing.assert_allclose(before.xyz + offset, after.xyz)

    def test_invalid_inspection_bounds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Inspection bounds"):
            detect(
                self.rgb,
                self.reference,
                self.K,
                self.T,
                inspection_bounds=[[0, 0, 0], [-1, 1, 1]],
            )


if __name__ == "__main__":
    unittest.main()
