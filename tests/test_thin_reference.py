"""Thin-reference regressions with independently constructed RGB-D signals."""

import unittest
from dataclasses import replace

import numpy as np

from daiyi_grinding.perception import roi_mask
from daiyi_grinding.reference_detection import detect_reference
from daiyi_grinding.thin_reference import (
    THIN_REFERENCE_CONFIG,
    detect_thin_reference,
)


class ThinReferenceTests(unittest.TestCase):
    def setUp(self):
        self.shape = (240, 320)
        self.rgb = np.full((*self.shape, 3), 160, np.uint8)
        self.intrinsics = np.array(
            [[1010.0, 0, 159.5], [0, 1010.0, 119.5], [0, 0, 1.0]]
        )
        self.transform = np.eye(4)
        self.transform[:3, :3] = np.diag([1.0, -1.0, -1.0])
        self.transform[:3, 3] = [-0.49, -0.13, 1.13]
        self.reference = np.full(self.shape, 0.505, np.float32)

    def run_detector(self, observed, reference=None, **kwargs):
        return detect_thin_reference(
            self.rgb,
            observed,
            self.reference if reference is None else reference,
            self.intrinsics,
            self.transform,
            **kwargs,
        )

    def test_one_pixel_ridge_survives_reference_differencing(self):
        observed = self.reference.copy()
        observed[120, 40:280] -= 0.00050
        old, _ = detect_reference(
            self.rgb,
            observed,
            self.reference,
            self.intrinsics,
            self.transform,
        )
        self.assertFalse(old)
        detections, observation = self.run_detector(observed)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "seam")
        self.assertGreater(len(detections[0].pixels), 200)
        self.assertGreater(detections[0].length_mm, 100)
        self.assertGreater(observation["residual"][120, 100], 0.00045)

    def test_low_signal_tail_connects_to_a_strong_seed(self):
        observed = self.reference.copy()
        observed[120, 40:80] -= 0.00055
        observed[120, 80:280] -= 0.00023
        detections, _ = self.run_detector(observed)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "seam")
        self.assertGreater(detections[0].length_mm, 100)

    def test_metric_join_retains_weak_fragment_with_a_nearby_seed(self):
        observed = self.reference.copy()
        observed[120, 40:43] -= 0.00055
        observed[120, 43:45] = np.nan
        observed[120, 45:52] -= 0.00023
        detections, _ = self.run_detector(
            observed,
            config=replace(
                THIN_REFERENCE_CONFIG,
                minimum_pixels=6,
                component_join_mm=2.0,
            ),
        )
        self.assertEqual(len(detections), 1)
        self.assertEqual(
            set(map(tuple, detections[0].pixels)),
            {(120, column) for column in [*range(40, 43), *range(45, 52)]},
        )

    def test_metric_join_requires_a_seed_in_each_connected_region(self):
        observed = self.reference.copy()
        observed[120, 40:50] -= 0.00055
        observed[120, 70:90] -= 0.00023
        detections, observation = self.run_detector(
            observed,
            config=replace(
                THIN_REFERENCE_CONFIG,
                minimum_pixels=6,
                component_join_mm=2.0,
            ),
        )
        self.assertEqual(len(detections), 1)
        self.assertEqual(
            set(map(tuple, detections[0].pixels)),
            {(120, column) for column in range(40, 50)},
        )
        self.assertFalse(observation["unresolved_components"])

    def test_sparse_seed_retains_nominal_reacquisition_candidates(self):
        observed = self.reference.copy()
        observed[120, 40:43] -= 0.00055
        detections, observation = self.run_detector(
            observed,
            config=replace(
                THIN_REFERENCE_CONFIG,
                minimum_pixels=6,
                component_join_mm=2.0,
            ),
        )
        self.assertFalse(detections)
        self.assertFalse(observation["mask"].any())
        candidates = observation["unresolved_components"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["pixels"], [[120, 40], [120, 41], [120, 42]]
        )
        self.assertEqual(candidates[0]["seed_count"], 3)
        self.assertEqual(candidates[0]["support_count"], 3)
        self.assertEqual(candidates[0]["minimum_pixels"], 6)
        np.testing.assert_allclose(
            np.asarray(candidates[0]["nominal_xyz"])[:, 2], 0.625, atol=1e-7
        )
        self.assertGreater(candidates[0]["optical_residual_range_mm"][0], 0.54)

    def test_random_noise_does_not_create_processing_regions(self):
        rng = np.random.default_rng(19)
        observed = self.reference + rng.normal(0, 0.00006, self.reference.shape)
        detections, observation = self.run_detector(observed)
        self.assertFalse(detections)
        self.assertGreater(observation["noise_sigma_mm"], 0.05)

    def test_missing_pixels_do_not_become_positive_material(self):
        observed = self.reference.copy()
        observed[120, 40:280] -= 0.0005
        observed[120, 80:90] = np.nan
        detections, _ = self.run_detector(observed)
        for detection in detections:
            self.assertFalse(
                np.isnan(observed[tuple(detection.pixels.T)]).any()
            )

    def test_reference_edge_shift_is_guarded(self):
        reference = self.reference.copy()
        reference[:, :70] += 0.015
        observed = reference.copy()
        observed[:, 69] -= 0.005
        detections, observation = self.run_detector(observed, reference)
        self.assertFalse(detections)
        self.assertTrue(
            observation["reference_discontinuity_guard"][:, 69].all()
        )

    def test_roi_cannot_reclassify_full_broad_mismatch_as_burr(self):
        observed = self.reference.copy()
        observed[100:124, 65:255] -= 0.002
        full, _ = self.run_detector(observed)
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0].kind, "preserve")
        roi = [[140, 103], [153, 103], [153, 119], [140, 119]]
        cropped, _ = self.run_detector(observed, roi=roi)
        self.assertEqual(len(cropped), 1)
        self.assertEqual(cropped[0].kind, "preserve")
        self.assertEqual(cropped[0].id, full[0].id)
        permission = roi_mask(self.shape, roi)
        self.assertTrue(permission[tuple(cropped[0].pixels.T)].all())

    def test_large_protrusion_becomes_review(self):
        observed = self.reference.copy()
        observed[100:112, 150:162] -= 0.008
        detections, _ = self.run_detector(observed)
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "review")

    def test_shape_and_invalid_depth_rejected(self):
        with self.assertRaisesRegex(ValueError, "shape mismatch"):
            self.run_detector(self.reference, self.reference[:100])
        with self.assertRaisesRegex(ValueError, "valid depth"):
            self.run_detector(np.full(self.shape, np.nan))


if __name__ == "__main__":
    unittest.main()
