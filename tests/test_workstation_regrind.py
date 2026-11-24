"""Sensor-confirmed high points select only permitted local follow-up traces."""

import unittest

import numpy as np

from daiyi_grinding.reinspection_stress import _sensor_observation
from daiyi_grinding.workstation_quality import Reinspection
from daiyi_grinding.workstation_regrind import (
    additional_regrind_paths,
    regrind_paths,
)


class LocalRegrindTests(unittest.TestCase):
    def setUp(self):
        points = np.column_stack(
            [np.arange(21) * 0.001, np.zeros(21), np.ones(21)]
        )
        self.target = {
            "id": 1,
            "kind": "seam",
            "points": points,
            "normals": np.tile([0, 0, -1.0], (21, 1)),
        }
        self.source = {
            "detection_id": 1,
            "segment_id": 8,
            "kind": "seam",
            "xyz": points.tolist(),
            "nominal_xyz": points.tolist(),
            "normal": [0, 0, -1.0],
            "coordinate_frame": "workpiece_097",
            "material_allowance_mm": 0.08,
            "tool_diameter_mm": 158.9,
            "tool_width_mm": 75.192,
            "recipe": {"tool": "belt", "feed_mm_s": 10},
            "view_index": 1,
        }

    def quality(self, heights, *, counts=None):
        return {
            "automatic_regrind_allowed": True,
            "insufficient": 0,
            "uncertain": 0,
            "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
            "regions": [
                {
                    "id": 1,
                    "status_reason": "positive_excess",
                    "observed_anchors": len(heights),
                    "anchors": len(heights),
                    "anchor_bounds_mm": [
                        [height - 0.05, height + 0.05] for height in heights
                    ],
                    "anchor_estimates_mm": list(heights),
                    "anchor_observation_counts": [30] * len(heights),
                    "anchor_positive_lower_mm": [
                        height - 0.05 for height in heights
                    ],
                    "anchor_positive_estimates_mm": list(heights),
                    "anchor_positive_observation_counts": (
                        [2] * len(heights) if counts is None else counts
                    ),
                }
            ],
        }

    def test_high_point_below_region_quantile_selects_local_trace(self):
        heights = [0.1] * 21
        heights[10] = 0.5
        self.assertLess(np.quantile(heights, 0.8), 0.3)
        paths, report = regrind_paths(
            [self.target],
            self.quality(heights),
            [self.source],
            start_segment_id=20,
        )
        self.assertEqual(report["positive_anchor_count"], 1)
        self.assertEqual(report["reason"], "local_regrind")
        self.assertFalse(report["truth_used_for_control"])
        self.assertEqual(len(paths), 1)
        path = paths[0]
        self.assertEqual(path["segment_id"], 20)
        self.assertEqual(path["regrind"]["source_segment_id"], 8)
        self.assertEqual(path["material_allowance_mm"], 0.08)
        self.assertEqual(path["recipe"], self.source["recipe"])
        self.assertLess(len(path["xyz"]), len(self.source["xyz"]))
        self.assertIn(self.source["xyz"][10], path["xyz"])
        self.assertTrue(
            all(point in self.source["xyz"] for point in path["xyz"])
        )
        np.testing.assert_allclose(
            path["observed_xyz"],
            [self.target["points"][10] + [0, 0, -0.0005]],
        )
        self.assertEqual(path["xyz"], path["nominal_xyz"])

    def test_repeated_local_ridge_survives_other_views_of_nominal_base(self):
        heights = [0.1] * 21
        heights[10] = 0.5
        quality = self.quality(heights)
        region = quality["regions"][0]
        region["anchor_bounds_mm"][10] = [-0.05, 0.55]
        region["anchor_positive_observation_counts"][10] = 9
        paths, report = regrind_paths([self.target], quality, [self.source])
        self.assertEqual(report["reason"], "local_regrind")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["regrind"]["minimum_observations"], 9)
        self.assertGreater(paths[0]["regrind"]["minimum_lower_mm"], 0.3)

    def test_same_round_local_aliases_do_not_schedule_duplicate_grinding(self):
        points = np.asarray(self.source["nominal_xyz"])[7:14]
        candidates = [
            {**self.source, "nominal_xyz": points.tolist()},
            {
                **self.source,
                "nominal_xyz": (points + [0, 0.0007, 0]).tolist(),
            },
        ]
        added = additional_regrind_paths([], candidates)
        self.assertEqual(len(added), 1)
        self.assertIs(added[0], candidates[0])

    def test_finite_capacity_keeps_enough_trace_time_for_measured_high_points(
        self,
    ):
        heights = [0.1] * 21
        heights[10] = 0.5
        paths, _ = regrind_paths(
            [self.target],
            self.quality(heights),
            [self.source],
            rate_mm_s=0.01,
        )
        path = paths[0]
        trace_length_mm = (
            np.linalg.norm(
                np.diff(np.asarray(path["nominal_xyz"]), axis=0), axis=1
            ).sum()
            * 1000
        )
        duration_s = trace_length_mm / path["recipe"]["feed_mm_s"]
        # The scan's positive upper bound is 0.55 mm; retain the permitted
        # 0.08 mm allowance rather than inheriting the original short duration.
        self.assertGreaterEqual(duration_s * 0.01, 0.55 - 0.08)
        self.assertLess(
            path["recipe"]["feed_mm_s"], self.source["recipe"]["feed_mm_s"]
        )

    def test_path_height_uses_same_view_as_confirmed_positive_lower(self):
        heights = [0.1] * 21
        heights[10] = 0.5
        quality = self.quality(heights)
        quality["regions"][0]["anchor_estimates_mm"][10] = 0.8
        quality["regions"][0]["anchor_bounds_mm"][10] = [-0.05, 1.6]
        paths, _ = regrind_paths([self.target], quality, [self.source])
        self.assertAlmostEqual(paths[0]["normal_height_mm"], 0.5)
        np.testing.assert_allclose(
            paths[0]["observed_xyz"],
            [self.target["points"][10] + [0, 0, -0.0005]],
        )

    def test_sensor_report_recovers_repeated_ridge_next_to_nominal_ray(self):
        nominal_view, target = _sensor_observation(0.0)
        nominal_view["T"][0, 3] = 0.001
        nominal_view["reference_xyz"] += [0.001, 0, 0]
        nominal_view["xyz"] += [0.001, 0, 0]
        ridge_view, _ = _sensor_observation(0.0)
        ridge_view["xyz"][18, 18, 2] = 1.0 - 0.0005
        ridge_view["depth_sample_counts"] = np.full(
            (64, 64), 9, dtype=np.uint16
        )
        ridge_view["depth_frame_count"] = 9
        tracker = Reinspection([target])
        tracker.observe(nominal_view, 0)
        tracker.observe(ridge_view, 1)
        quality = tracker.report(0.3)
        region = quality["regions"][0]
        self.assertLess(region["anchor_bounds_mm"][0][0], 0.3)
        self.assertGreater(region["anchor_positive_lower_mm"][0], 0.3)
        self.assertEqual(region["anchor_positive_observation_counts"][0], 9)
        points = [
            (target["points"][0] + shift).tolist()
            for shift in ([-0.001, 0, 0], [0, 0, 0], [0.001, 0, 0])
        ]
        source = {
            **self.source,
            "xyz": points,
            "nominal_xyz": points,
        }
        paths, report = regrind_paths([target], quality, [source])
        self.assertEqual(report["reason"], "local_regrind")
        self.assertEqual(report["positive_anchor_count"], 1)
        self.assertEqual(paths[0]["regrind"]["minimum_observations"], 9)
        np.testing.assert_allclose(
            paths[0]["observed_xyz"],
            [target["points"][0] + [0, 0, -0.0005]],
        )

    def test_single_observation_or_unconfirmed_excess_creates_no_path(self):
        heights = [0.1] * 21
        heights[10] = 0.5
        counts = [2] * 21
        counts[10] = 1
        cases = (
            self.quality(heights, counts=counts),
            self.quality([0.33] * 21),
            self.quality([0.1] * 21),
        )
        for quality in cases:
            with self.subTest(quality=quality):
                paths, report = regrind_paths(
                    [self.target], quality, [self.source]
                )
                self.assertEqual(paths, [])
                self.assertEqual(
                    report["reason"], "no_confirmed_positive_excess"
                )

    def test_negative_missing_and_uncertain_observations_stop_followup(self):
        for update, expected in (
            (
                {"automatic_regrind_allowed": False},
                "reinspection_disposition",
            ),
            ({"insufficient": 1}, "insufficient_reinspection"),
            ({"uncertain": 1}, "uncertain_reinspection"),
        ):
            with self.subTest(update=update):
                quality = {**self.quality([0.5] * 21), **update}
                paths, report = regrind_paths(
                    [self.target], quality, [self.source]
                )
                self.assertEqual(paths, [])
                self.assertEqual(report["reason"], expected)

    def test_local_trace_does_not_bridge_existing_permission_gap(self):
        points = np.array(
            [[0, 0, 1], [0.001, 0, 1], [0.020, 0, 1], [0.021, 0, 1]]
        )
        target = {
            **self.target,
            "points": points,
            "normals": np.tile([0, 0, -1.0], (4, 1)),
        }
        source = {
            **self.source,
            "xyz": points.tolist(),
            "nominal_xyz": points.tolist(),
        }
        paths, report = regrind_paths(
            [target], self.quality([0.5] * 4), [source]
        )
        self.assertEqual(len(paths), 2)
        self.assertEqual(report["unmapped_anchors"], [])
        for path in paths:
            self.assertLess(
                np.linalg.norm(np.diff(path["xyz"], axis=0), axis=1).max(),
                0.005,
            )

    def test_nearby_other_cad_face_does_not_gain_a_trace(self):
        target = {
            **self.target,
            "normals": -self.target["normals"],
        }
        paths, report = regrind_paths(
            [target], self.quality([0.5] * 21), [self.source]
        )
        self.assertEqual(paths, [])
        self.assertEqual(report["reason"], "unmapped_positive_excess")
        self.assertEqual(len(report["unmapped_anchors"]), 21)

    def test_review_region_does_not_gain_a_trace(self):
        paths, report = regrind_paths(
            [{**self.target, "kind": "review"}],
            self.quality([0.5] * 21),
            [self.source],
        )
        self.assertEqual(paths, [])
        self.assertEqual(report["positive_anchor_count"], 0)

    def test_unmapped_high_point_remains_visible_in_report(self):
        target = {
            **self.target,
            "points": self.target["points"] + [0, 0.01, 0],
        }
        paths, report = regrind_paths(
            [target], self.quality([0.5] * 21), [self.source]
        )
        self.assertEqual(paths, [])
        self.assertEqual(report["positive_anchor_count"], 21)
        self.assertEqual(report["reason"], "unmapped_positive_excess")
        self.assertEqual(
            report["unmapped_anchors"],
            [{"target_id": 1, "anchor_index": index} for index in range(21)],
        )


if __name__ == "__main__":
    unittest.main()
