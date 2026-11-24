"""Exercise measured local excess through reinspection and bounded follow-up."""

import unittest

import numpy as np

from daiyi_grinding.perception import Detection
from daiyi_grinding.workstation_inspection import machining_required
from daiyi_grinding.workstation_quality import Reinspection
from daiyi_grinding.workstation_regrind import regrind_paths
from daiyi_grinding.workstation_run import continuation_reason


class ObservedRegrindTests(unittest.TestCase):
    def evidence(self, heights_mm):
        points = np.column_stack(
            [
                np.arange(len(heights_mm)) / 1000,
                np.zeros(len(heights_mm)),
                np.ones(len(heights_mm)),
            ]
        )
        target = {
            "id": 1,
            "kind": "seam",
            "points": points,
            "normals": np.tile([0, 0, -1.0], (len(points), 1)),
        }
        reference = np.tile(points, (2, 1, 1))
        measured = reference.copy()
        measured[:, :, 2] -= np.asarray(heights_mm) / 1000
        observation = {
            "T": np.eye(4),
            "K": np.diag([1000.0, 1000.0, 1]),
            "surface": np.ones((2, len(points)), bool),
            "selection": np.ones((2, len(points)), bool),
            "reference_xyz": reference,
            "xyz": measured,
            "normals": np.tile([0, 0, -1.0], (2, len(points), 1)),
            "noise_sigma_mm": 0.02,
            "depth_sample_counts": np.full((2, len(points)), 9),
            "depth_frame_count": 9,
        }
        source = {
            "segment_id": 1,
            "detection_id": 1,
            "kind": "seam",
            "nominal_xyz": points.tolist(),
            "xyz": points.tolist(),
            "normal": [0, 0, -1],
            "material_allowance_mm": 0.08,
        }
        return target, observation, source

    def test_local_high_points_below_region_quantile_enter_bounded_regrind(
        self,
    ):
        heights = np.full(17, 0.1)
        heights[7:9] = 0.5
        target, observation, source = self.evidence(heights)
        pixels = np.column_stack(
            [np.zeros(len(heights), dtype=int), np.arange(len(heights))]
        )
        detection = Detection(
            1, "seam", pixels, observation["xyz"][0], 16, 1, 0.5, 1, ""
        )
        self.assertFalse(machining_required(detection, observation))
        tracker = Reinspection([target])
        tracker.observe(observation, 0)
        quality = tracker.report(0.3)
        paths, report = regrind_paths([target], quality, [source])
        self.assertGreater(len(paths), 0)
        self.assertEqual(report["unmapped_anchors"], [])
        self.assertEqual(
            continuation_reason("removal", True, quality, paths, 0, 3, False),
            "followup_planning",
        )
        self.assertEqual(
            continuation_reason("removal", True, quality, paths, 2, 3, False),
            "pass_budget_exhausted",
        )

    def test_measured_overgrind_stops_despite_other_positive_excess(self):
        heights = np.full(17, 0.1)
        heights[7:9] = 0.5
        heights[3] = -0.42
        target, observation, source = self.evidence(heights)
        tracker = Reinspection([target])
        tracker.observe(observation, 0)
        quality = tracker.report(0.3)
        paths, _ = regrind_paths([target], quality, [source])
        self.assertEqual(paths, [])
        self.assertEqual(
            continuation_reason(
                "removal", True, quality, [source], 0, 3, False
            ),
            "overgrind_requires_disposition",
        )


if __name__ == "__main__":
    unittest.main()
