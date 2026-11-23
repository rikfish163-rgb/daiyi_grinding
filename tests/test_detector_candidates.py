"""Candidate regressions for measured thin ridges and permission boundaries."""

import unittest

import numpy as np

from daiyi_grinding.detector_candidates import (
    CANDIDATE_METHODS,
    candidate_configuration,
    detect_candidate,
)
from daiyi_grinding.perception import detect
from daiyi_grinding.planning import plan


class DetectorCandidateTests(unittest.TestCase):
    def setUp(self):
        self.shape = (240, 320)
        self.intrinsics = np.array(
            [[1010.0, 0, 159.5], [0, 1010.0, 119.5], [0, 0, 1.0]]
        )
        self.transform = np.eye(4)
        self.transform[:3, :3] = np.diag([1.0, -1.0, -1.0])
        self.transform[:3, 3] = [-0.49, -0.13, 1.13]
        self.rgb = np.full((*self.shape, 3), 160, np.uint8)
        self.depth = np.full(self.shape, 0.505, np.float32)

    def run_candidate(self, depth, method, **kwargs):
        return detect_candidate(
            self.rgb,
            depth,
            self.intrinsics,
            self.transform,
            method=method,
            **kwargs,
        )

    def test_normal_plane_produces_no_processing_regions(self):
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                detections, observation = self.run_candidate(self.depth, method)
                self.assertFalse(detections)
                self.assertFalse(plan(detections, observation))

    def test_blank_depth_rejected(self):
        depth = np.full(self.shape, np.nan)
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                with self.assertRaisesRegex(ValueError, "valid depth"):
                    self.run_candidate(depth, method)

    def test_thin_ridge_extends_from_strong_seed(self):
        depth = self.depth.copy()
        depth[119:124, 40:281] -= 0.00028
        depth[119:124, 40:70] -= 0.00023
        baseline, _ = detect(self.rgb, depth, self.intrinsics, self.transform)
        self.assertFalse(any(item.kind == "seam" for item in baseline))
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                detections, _ = self.run_candidate(depth, method)
                seams = [item for item in detections if item.kind == "seam"]
                self.assertEqual(len(seams), 1)
                self.assertGreater(seams[0].length_mm, 100.0)

    def test_roi_fragment_of_normal_rib_stays_preserve(self):
        depth = self.depth.copy()
        depth[100:116, 65:255] -= 0.002
        roi = [[140, 101], [152, 101], [152, 113], [140, 113]]
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                full, _ = self.run_candidate(depth, method)
                self.assertTrue(full)
                self.assertTrue(all(item.kind == "preserve" for item in full))
                cropped, observation = self.run_candidate(
                    depth, method, roi=roi
                )
                self.assertTrue(cropped)
                self.assertTrue(
                    all(item.kind == "preserve" for item in cropped)
                )
                self.assertFalse(plan(cropped, observation))
                self.assertEqual(cropped[0].id, full[0].id)

    def test_inspection_bounds_can_follow_a_different_fixture(self):
        depth = self.depth.copy()
        depth[119:124, 40:281] -= 0.002
        relocated = self.transform.copy()
        relocated[:3, 3] += [1.0, 0.7, 0.25]
        bounds = ((0.33, 0.43, 0.862), (0.69, 0.73, 0.905))
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                detections, _ = detect_candidate(
                    self.rgb,
                    depth,
                    self.intrinsics,
                    relocated,
                    method=method,
                    inspection_bounds=bounds,
                )
                self.assertTrue(any(item.kind == "seam" for item in detections))

    def test_invalid_bounds_rejected(self):
        with self.assertRaisesRegex(ValueError, "Inspection bounds"):
            self.run_candidate(
                self.depth,
                "multiscale",
                inspection_bounds=((1, 1, 1), (0, 0, 0)),
            )

    def test_noisy_rib_keeps_its_full_preservation_class(self):
        depth = self.depth.copy()
        depth[100:116, 65:255] -= 0.002
        rng = np.random.default_rng(77)
        depth += rng.normal(0, 0.00025, depth.shape).astype(np.float32)
        for method in CANDIDATE_METHODS:
            with self.subTest(method=method):
                detections, observation = self.run_candidate(depth, method)
                self.assertEqual(observation["selected_backend"], "geometry")
                ribs = [item for item in detections if item.length_mm > 50]
                self.assertTrue(ribs)
                self.assertTrue(all(item.kind == "preserve" for item in ribs))

    def test_report_config_copy_cannot_mutate_detector(self):
        configuration = candidate_configuration("multiscale")
        configuration["opening_radii_mm"][0] = 1000
        self.assertEqual(
            candidate_configuration("multiscale")["opening_radii_mm"][0],
            3.0,
        )


if __name__ == "__main__":
    unittest.main()
