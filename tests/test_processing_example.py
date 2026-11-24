import base64
import json
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding.processing_example import export_example


class ProcessingExampleTests(unittest.TestCase):
    def test_export_separates_mapped_and_executed_regrind_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "run"
            source.mkdir()
            mapping = {
                "positive_anchor_count": 24,
                "path_count": 4,
                "duplicate_trace_count": 1,
                "candidate_path_count": 3,
                "unmapped_anchors": [],
                "reason": "local_regrind",
                "truth_used_for_control": False,
            }
            result = {
                "path_count": 4,
                "admitted_path_count": 4,
                "executions": [{}, {}],
                "reinspections": [
                    {
                        "remaining_paths": 3,
                        "followup_preflight": {"admitted_count": 3},
                        "regrind": mapping,
                    },
                    {"remaining_paths": 0},
                ],
            }
            (source / "result.json").write_text(json.dumps(result))
            (source / "config.json").write_text(
                json.dumps(
                    {
                        "max_passes": 3,
                        "camera_setup": "original",
                        "removal_rate_mm_s": 0.009,
                    }
                )
            )
            target = export_example(source, Path(directory) / "example")
            summary = json.loads(target.with_name("summary.json").read_text())
            self.assertEqual(summary["rounds"][0]["admitted_path_count"], 4)
            self.assertEqual(summary["rounds"][1]["candidate_path_count"], 3)
            self.assertEqual(summary["rounds"][1]["admitted_path_count"], 3)
            self.assertEqual(summary["rounds"][0]["regrind"], mapping)
            document = target.read_text()
            self.assertIn("映射子段", document)
            self.assertIn("去重后候选段", document)
            self.assertIn("实际下一轮接纳段", document)
            self.assertIn("<td>4</td><td>1</td><td>3</td><td>3</td>", document)
            self.assertIn("--camera-setup original", document)
            self.assertIn("--removal-rate-mm-s 0.009", document)

    def test_multiple_rounds_preserve_capture_counts_and_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "run"
            source.mkdir()
            mapping = {
                "positive_anchor_count": 3,
                "path_count": 1,
                "unmapped_anchors": [],
                "reason": "local_regrind",
                "truth_used_for_control": False,
            }
            result = {
                "seed": 120483,
                "case": "thin",
                "task_status": "complete",
                "motion_gate_passed": True,
                "observational_release": True,
                "geometric_goal_complete": True,
                "goal_complete": True,
                "path_count": 1,
                "admitted_path_count": 1,
                "controller_stopping": {
                    "reason": "observed_regions_verified",
                    "truth_used_for_control": False,
                },
                "executions": [
                    {"surface_tracking_p95_mm": 0.12},
                    {"surface_tracking_p95_mm": 0.11},
                ],
                "reinspections": [
                    {
                        "view_count": 6,
                        "remaining_paths": 1,
                        "stopping_reason": "followup_planning",
                        "followup_preflight": {"admitted_count": 1},
                        "regrind": mapping,
                    },
                    {
                        "view_count": 6,
                        "remaining_paths": 0,
                        "stopping_reason": "observed_regions_verified",
                    },
                ],
            }
            (source / "result.json").write_text(json.dumps(result))
            (source / "config.json").write_text(json.dumps({"max_passes": 3}))
            path = {
                "segment_id": 1,
                "detection_id": 1,
                "xyz": [[0, 0, 0.3], [0.02, 0, 0.3]],
            }
            (source / "paths.json").write_text(json.dumps([path]))
            (source / "rescan_01_paths.json").write_text(json.dumps([path]))
            (source / "rescan_02_paths.json").write_text("[]")
            (source / "rescan_01_views.json").write_text(
                json.dumps([{"index": index} for index in range(6)])
            )
            (source / "rescan_02_views.json").write_text(
                json.dumps(
                    [
                        {"index": index, "depth_frame_count": 9}
                        for index in range(6)
                    ]
                )
            )
            target = export_example(source, Path(directory) / "example")
            summary = json.loads(target.with_name("summary.json").read_text())
            self.assertTrue(summary["goal_complete"])
            self.assertEqual(summary["execution_rounds"], 2)
            self.assertIsNone(summary["rounds"][0]["depth_frame_count"])
            self.assertEqual(summary["rounds"][1]["depth_frame_count"], 54)
            self.assertEqual(summary["rounds"][0]["regrind"], mapping)
            self.assertEqual(
                summary["rounds"][0]["stopping_reason"], "followup_planning"
            )
            document = target.read_text()
            self.assertIn("回磨 1", document)
            self.assertIn("54 / 6", document)
            self.assertIn("已进入下一轮加工", document)
            self.assertIn("观测区域已验证", document)
            self.assertIn("持续正超差锚点", document)
            self.assertEqual(document.count("<polyline "), 2)
            self.assertIn("本轮没有加工路径候选", document)

    def test_export_preserves_observation_hold_and_embeds_saved_images(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "run"
            source.mkdir()
            result = {
                "seed": 120483,
                "case": "thin",
                "task_status": "partial",
                "motion_gate_passed": True,
                "observational_release": False,
                "geometric_goal_complete": True,
                "goal_complete": False,
                "controller_stopping": {
                    "reason": "reinspection_requires_review",
                    "truth_used_for_control": False,
                },
                "reinspections": [{"region_reinspection": {}}],
                "executions": [],
            }
            (source / "result.json").write_text(json.dumps(result))
            image = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAA"
                "AC0lEQVR42mP8/x8AAwMCAO+a9mIAAAAASUVORK5CYII="
            )
            (source / "detection_paths.png").write_bytes(image)
            (source / "after_detection_paths.png").write_bytes(image)
            target = export_example(source, Path(directory) / "example")
            summary = json.loads(target.with_name("summary.json").read_text())
            self.assertTrue(summary["motion_gate_passed"])
            self.assertTrue(summary["geometric_goal_complete"])
            self.assertFalse(summary["observational_release"])
            self.assertFalse(summary["goal_complete"])
            document = target.read_text()
            self.assertIn("复检需要复核", document)
            self.assertIn("待独立现场量测", document)
            self.assertEqual(document.count('src="data:image/png;base64,'), 2)
            self.assertIn(base64.b64encode(image).decode("ascii"), document)
            self.assertIn("本次没有执行磨削", document)
            self.assertEqual(
                json.loads((source / "result.json").read_text()), result
            )

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "run"
            source.mkdir()
            (source / "result.json").write_text("{}")
            output = Path(directory) / "example"
            output.mkdir()
            saved = output / "index.html"
            saved.write_text("existing example")
            with self.assertRaises(FileExistsError):
                export_example(source, output)
            self.assertEqual(saved.read_text(), "existing example")


if __name__ == "__main__":
    unittest.main()
