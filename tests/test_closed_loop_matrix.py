"""Audit completed dynamics separately from candidate and admitted paths."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts/validate_closed_loop_matrix.py"
)
SPEC = importlib.util.spec_from_file_location("closed_loop_matrix", SCRIPT)
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)


class ClosedLoopReportTests(unittest.TestCase):
    def row(self, result, case="burr"):
        with tempfile.TemporaryDirectory() as directory:
            episode = Path(directory)
            (episode / "result.json").write_text(json.dumps(result))
            (episode / "status.json").write_text('{"stage": "complete"}')
            return MATRIX.episode_row(
                {
                    "case": case,
                    "seed": 123,
                    "episode": str(episode),
                    "exit_code": 0,
                    "timed_out": False,
                    "wall_seconds": 1,
                    "log": "example.log",
                }
            )

    def test_followup_admission_without_execution_is_not_counted(self):
        row = self.row(
            {
                "path_count": 7,
                "admitted_path_count": 3,
                "preflight": {"passed": True, "admitted_count": 3},
                "executions": [{"unexpected_contact_count": 0}],
                "reinspections": [
                    {
                        "followup_preflight": {
                            "passed": True,
                            "admitted_count": 2,
                        },
                        "metrics": {"f1": 0.6},
                    }
                ],
            }
        )
        self.assertEqual(row["executed_segments_across_passes"], 3)
        self.assertEqual(row["initial_candidates"], 7)
        self.assertEqual(row["post_f1"], 0.6)

    def test_small_residual_does_not_hide_missing_classification(self):
        row = self.row(
            {
                "perception": {
                    "generated_defects": 3,
                    "metrics": {"matched_defect_count": 1},
                },
                "final_stock": {
                    "remaining_peak_mm": 0.2,
                    "residual_regions": [
                        {"name": "small", "remaining_peak_mm": 0.2}
                    ],
                },
                "goal_complete": False,
            }
        )
        self.assertFalse(row["validation_pass"])
        self.assertEqual(row["remaining_region_count"], 0)
        self.assertIn(
            "initial_object_detection_or_classification_incomplete",
            row["outcome_failures"],
        )

    def test_normal_pass_requires_no_false_positive_and_no_motion(self):
        result = {
            "perception": {
                "generated_defects": 0,
                "metrics": {"fp": 0, "f1": None},
            },
            "path_count": 0,
            "robot_executed": False,
        }
        self.assertTrue(self.row(result, "normal")["normal_no_action_pass"])
        result["perception"]["metrics"]["fp"] = 1
        self.assertFalse(self.row(result, "normal")["normal_no_action_pass"])
        result["perception"]["metrics"]["fp"] = 0
        result["reinspections"] = [{"metrics": {"fp": 1}, "remaining_paths": 0}]
        self.assertFalse(self.row(result, "normal")["normal_no_action_pass"])


if __name__ == "__main__":
    unittest.main()
