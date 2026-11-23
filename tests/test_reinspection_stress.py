"""Signed reinspection and offline sensor-array stress contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daiyi_grinding.reinspection_stress import (
    TOLERANCE_MM,
    _left_roi_selection,
    _sensor_observation,
    main,
    run,
)
from daiyi_grinding.workstation_quality import Reinspection


class SignedReinspectionTests(unittest.TestCase):
    def test_negative_overgrind_is_rejected_with_signed_bounds(self):
        observation, target = _sensor_observation(-0.36, noise_sigma_mm=0.025)
        tracker = Reinspection([target])
        self.assertEqual(tracker.observe(observation, 0), 4)
        result = tracker.report(TOLERANCE_MM)
        region = result["regions"][0]
        self.assertEqual(region["status"], "needs_rework")
        self.assertEqual(region["status_reason"], "overgrind")
        self.assertAlmostEqual(region["signed_peak_mm"], -0.36, places=10)
        self.assertAlmostEqual(region["lower_peak_mm"], -0.41, places=10)
        self.assertAlmostEqual(region["upper_peak_mm"], -0.31, places=10)
        self.assertAlmostEqual(
            region["anchor_bounds_mm"][0][0], -0.41, places=10
        )
        self.assertAlmostEqual(
            region["anchor_bounds_mm"][0][1], -0.31, places=10
        )
        self.assertFalse(result["observational_release"])
        self.assertEqual(result["overgrind"], 1)
        self.assertFalse(result["automatic_regrind_allowed"])

    def test_scaled_normals_do_not_understate_signed_excess(self):
        observation, target = _sensor_observation(0.325, noise_sigma_mm=0.0)
        target["normals"] *= 0.95
        tracker = Reinspection([target])
        tracker.observe(observation, 0)
        region = tracker.report(TOLERANCE_MM)["regions"][0]
        self.assertEqual(region["status_reason"], "positive_excess")
        self.assertAlmostEqual(region["peak_mm"], 0.325, places=10)

    def test_invalid_tolerance_cannot_verify_a_region(self):
        observation, target = _sensor_observation(0.08)
        tracker = Reinspection([target])
        tracker.observe(observation, 0)
        for tolerance in (np.nan, np.inf, -0.3, 0.0, True):
            with self.subTest(tolerance=tolerance):
                with self.assertRaisesRegex(ValueError, "positive finite"):
                    tracker.report(tolerance)

    def test_invalid_additional_view_holds_previous_complete_coverage(self):
        observation, target = _sensor_observation(0.08)
        invalid, _ = _sensor_observation(0.08, invalid_noise=True)
        tracker = Reinspection([target])
        tracker.observe(observation, 0)
        self.assertTrue(tracker.report(TOLERANCE_MM)["observational_release"])
        tracker.observe(invalid, 1)
        result = tracker.report(TOLERANCE_MM)
        self.assertFalse(result["observational_release"])
        self.assertFalse(result["automatic_regrind_allowed"])

    def test_nan_noise_is_invalid_and_does_not_release(self):
        observation, target = _sensor_observation(0.08, invalid_noise=True)
        tracker = Reinspection([target])
        self.assertEqual(tracker.observe(observation, 0), 0)
        result = tracker.report(TOLERANCE_MM)
        self.assertEqual(result["insufficient"], 1)
        self.assertEqual(len(result["invalid_observations"]), 1)
        self.assertIn(
            "noise is invalid", result["invalid_observations"][0]["reason"]
        )
        self.assertFalse(result["observational_release"])

    def test_backface_mismatch_and_roi_hold_without_neighbour_inference(self):
        cases = (
            {"normals": np.array([0.2, 0.1, 0.97])},
            {"reference_shift_m": 0.003},
            {"selection": _left_roi_selection()},
        )
        for parameters in cases:
            observation, target = _sensor_observation(0.08, **parameters)
            tracker = Reinspection([target])
            tracker.observe(observation, 0)
            result = tracker.report(TOLERANCE_MM)
            with self.subTest(parameters=parameters):
                self.assertEqual(result["insufficient"], 1)
                self.assertFalse(result["observational_release"])

    def test_supplementary_view_recovers_anchor_coverage(self):
        first, target = _sensor_observation(
            0.08, selection=_left_roi_selection(), noise_sigma_mm=0.025
        )
        second, _ = _sensor_observation(0.08, noise_sigma_mm=0.025)
        tracker = Reinspection([target])
        tracker.observe(first, 0)
        before = tracker.report(TOLERANCE_MM)
        self.assertEqual(before["regions"][0]["observed_anchors"], 2)
        tracker.observe(second, 1)
        after = tracker.report(TOLERANCE_MM)
        self.assertEqual(after["regions"][0]["observed_anchors"], 4)
        self.assertEqual(after["regions"][0]["status"], "verified")
        self.assertTrue(after["observational_release"])

    def test_invalid_target_arrays_are_rejected(self):
        _, target = _sensor_observation(0.08)
        target["points"][0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "finite paired"):
            Reinspection([target])


class ReinspectionSuiteTests(unittest.TestCase):
    def test_default_low_resource_suite_meets_all_expectations(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reinspection"
            result = run(output)
            self.assertEqual(result["total_cases"], 12)
            self.assertEqual(result["verified"], 2)
            self.assertEqual(result["rejected"], 10)
            self.assertEqual(result["negative_rejections"], 1)
            self.assertEqual(result["unexpected_failures"], 0)
            self.assertTrue(result["all_expectations_met"])
            evidence = json.loads((output / "evidence.json").read_text())
            for key in (
                "task",
                "scope",
                "commands",
                "inputs",
                "metrics",
                "failures",
                "artifacts",
                "real_data_pending",
            ):
                self.assertIn(key, evidence)
            self.assertFalse(evidence["scope"]["mujoco_used"])
            self.assertFalse(evidence["scope"]["truth_used_for_release"])
            self.assertTrue(
                evidence["real_data_pending"][
                    "independent_real_097_postgrind_sensor_scan"
                ]
            )
            for case_file in output.glob("*/case.json"):
                case = json.loads(case_file.read_text())
                region = case["report"]["regions"][0]
                self.assertEqual(len(region["anchor_bounds_mm"]), 4)
            self.assertEqual(len(evidence["inputs"]["files"]), 4)

    def test_cli_returns_nonzero_for_unexpected_suite_failures(self):
        result = {
            "output": "unused",
            "verified": 0,
            "rejected": 1,
            "unexpected_failures": 1,
        }
        with (
            patch("sys.argv", ["reinspection_stress", "--output", "unused"]),
            patch(
                "daiyi_grinding.reinspection_stress.run",
                return_value=result,
            ),
        ):
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
