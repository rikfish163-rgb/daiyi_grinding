"""Automatic follow-up grinding must respect signed sensor failure states."""

import unittest

from daiyi_grinding.workstation_run import continuation_reason


class WorkstationStoppingTests(unittest.TestCase):
    def test_overgrind_stops_even_when_another_path_and_budget_remain(self):
        reason = continuation_reason(
            "removal", True, {"overgrind": 1}, [object()], 0, 3, False
        )
        self.assertEqual(reason, "overgrind_requires_disposition")

    def test_negative_uncertainty_and_invalid_views_stop_followup(self):
        for quality, expected in (
            (
                {"negative_limit_risk": 1},
                "negative_limit_requires_review",
            ),
            (
                {"invalid_observations": [{"view": 2, "reason": "bad camera"}]},
                "invalid_reinspection_requires_review",
            ),
        ):
            with self.subTest(quality=quality):
                self.assertEqual(
                    continuation_reason(
                        "removal", True, quality, [object()], 0, 3, False
                    ),
                    expected,
                )

    def test_positive_rework_and_budget_keep_their_existing_behavior(self):
        quality = {"needs_rework": 1, "overgrind": 0}
        for motion, paths, index, expected in (
            (True, [object()], 0, "followup_planning"),
            (True, [object()], 2, "pass_budget_exhausted"),
            (True, [], 0, "reinspection_requires_review"),
            (False, [object()], 0, "motion_safety_gate"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(
                    continuation_reason(
                        "removal", motion, quality, paths, index, 3, False
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
