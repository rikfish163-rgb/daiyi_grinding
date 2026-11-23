"""Reliability stress evidence, fault matrix and virtual-clock tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding.reliability_stress import (
    DEFAULT_LOOPS,
    DEFAULT_RUN_DIR,
    MINIMUM_LOOPS,
    SCENARIOS,
    run_reliability_stress,
    run_virtual_reliability,
)


def sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class VirtualReliabilityTests(unittest.TestCase):
    def test_800_hours_are_accelerated_coverage_not_acceptance(self):
        trace = run_virtual_reliability(800.0, heartbeat_s=300.0)
        self.assertEqual(trace["schema"], "daiyi-reliability-evidence-v1")
        self.assertEqual(trace["source_kind"], "virtual_reliability")
        self.assertEqual(trace["clock_kind"], "virtual")
        self.assertEqual(trace["virtual_hours"], 800.0)
        self.assertEqual(trace["maximum_gap_s"], 300.0)
        self.assertGreaterEqual(trace["heartbeat_count"], 9600)
        self.assertTrue(trace["accelerated_coverage"])
        self.assertTrue(trace["real_800h_pending"])
        self.assertTrue(trace["complete"])
        self.assertLess(trace["wall_seconds"], 10.0)
        stamps = [event["t_s"] for event in trace["heartbeats"]]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertEqual(stamps[0], 0.0)
        self.assertEqual(stamps[-1], 800.0 * 3600.0)

    def test_short_virtual_trace_does_not_claim_target(self):
        trace = run_virtual_reliability(2.0, heartbeat_s=300.0)
        self.assertEqual(trace["virtual_hours"], 2.0)
        self.assertFalse(trace["meets_target"])
        self.assertFalse(trace["complete"])
        self.assertTrue(trace["real_800h_pending"])


class ReliabilityEvidenceTests(unittest.TestCase):
    def test_full_fault_matrix_small_budget_outputs_evidence(self):
        historical_files = [
            DEFAULT_RUN_DIR / "motion_commands.json",
            DEFAULT_RUN_DIR / "preflight.json",
            DEFAULT_RUN_DIR / "estun_path_preview.csv",
            DEFAULT_RUN_DIR / "result.json",
        ]
        before = {path.name: sha256(path) for path in historical_files}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reliability"
            evidence = run_reliability_stress(
                output,
                loops=len(SCENARIOS),
                virtual_hours=2.0,
                enforce_minimum=False,
            )
            after = {path.name: sha256(path) for path in historical_files}
            self.assertEqual(before, after)
            self.assertEqual(
                evidence["schema"], "daiyi-reliability-stress-evidence-v1"
            )
            self.assertTrue(evidence["scope"]["offline_only"])
            self.assertFalse(evidence["scope"]["mujoco_imported"])
            self.assertFalse(evidence["scope"]["physical_socket_used"])
            self.assertTrue(evidence["scope"]["real_800h_pending"])
            self.assertEqual(evidence["failures"], [])
            self.assertTrue(evidence["real_data_pending"])
            self.assertTrue(evidence["task"])
            self.assertTrue(evidence["commands"])
            metrics = evidence["metrics"]
            self.assertEqual(metrics["loops"]["actual_cycles"], len(SCENARIOS))
            self.assertEqual(metrics["loops"]["mismatches"], 0)
            self.assertEqual(metrics["loops"]["unexpected_exceptions"], 0)
            self.assertEqual(
                metrics["loops"]["expected_rejections"],
                metrics["loops"]["observed_expected_rejections"],
            )
            self.assertEqual(
                metrics["loops"]["expected_accepts"],
                metrics["loops"]["observed_expected_accepts"],
            )
            self.assertEqual(metrics["loops"]["expected_rejections"], 16)
            self.assertEqual(
                metrics["loops"]["task_quality_complete_cycles"], 3
            )
            self.assertGreater(metrics["wall_seconds"], 0)
            self.assertGreater(metrics["peak_rss_kb"], 0)
            self.assertEqual(
                metrics["peak_resource_method"],
                "resource.getrusage(RUSAGE_SELF).ru_maxrss",
            )
            historical = evidence["historical_validation"]
            self.assertTrue(historical["sources_unchanged"])
            self.assertEqual(historical["plan_summary"]["command_count"], 1159)
            self.assertEqual(
                historical["plan_summary"]["admitted_segments"],
                [1, 2, 3, 4, 5, 6],
            )
            self.assertTrue(historical["mock_cycle"]["execution_complete"])
            self.assertFalse(historical["mock_cycle"]["task_quality_complete"])
            self.assertFalse(historical["reverified"]["collision"])
            self.assertFalse(historical["reverified"]["motion"])
            self.assertEqual(
                historical["plan_summary"]["result_evidence"]["task_status"],
                "partial",
            )
            virtual = evidence["virtual_reliability"]
            self.assertEqual(virtual["virtual_hours"], 2.0)
            self.assertFalse(virtual["meets_target"])
            self.assertTrue(virtual["accelerated_coverage"])
            self.assertTrue(virtual["real_800h_pending"])
            receipt = json.loads(
                (output / "reliability_receipt.json").read_text()
            )
            self.assertEqual(receipt["clock_kind"], "virtual")
            self.assertEqual(receipt["source_kind"], "virtual_reliability")
            self.assertFalse(receipt["complete"])
            controller_receipt = json.loads(
                (output / "historical_mock/controller_receipt.json").read_text()
            )
            self.assertTrue(controller_receipt["execution_complete"])
            self.assertFalse(controller_receipt["task_quality_complete"])
            self.assertFalse(
                controller_receipt["mock_identity"]["real_controller"]
            )
            self.assertTrue(controller_receipt["real_acceptance_pending"])
            for item in evidence["inputs"]:
                self.assertTrue(Path(item["path"]).is_file())
                self.assertEqual(len(item["sha256"]), 64)
                self.assertTrue(item["source"])
            for item in evidence["artifacts"]:
                path = output / item["path"]
                self.assertTrue(path.is_file(), item["path"])
                self.assertEqual(sha256(path), item["sha256"])
            report = (output / "report.md").read_text()
            self.assertIn("虚拟 800 小时", report)
            self.assertIn("不能作为企业 800 小时无故障验收", report)
            self.assertIn("task_quality_complete", report)
            loop_summary = json.loads(
                (output / "loops/summary.json").read_text()
            )
            self.assertEqual(loop_summary["mismatches"], 0)
            self.assertEqual(loop_summary["unexpected_exceptions"], 0)
            loop_records = [
                json.loads(line)
                for line in (output / "loops/index.jsonl")
                .read_text()
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(len(loop_records), len(SCENARIOS))
            self.assertTrue(all(record["match"] for record in loop_records))
            self.assertEqual(
                {record["scenario"] for record in loop_records},
                {scenario.name for scenario in SCENARIOS},
            )

    def test_minimum_loop_and_800h_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "too_short"
            with self.assertRaises(ValueError):
                run_reliability_stress(
                    output,
                    loops=MINIMUM_LOOPS - 1,
                    virtual_hours=800.0,
                    enforce_minimum=True,
                )
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                run_reliability_stress(
                    Path(directory) / "short_hours",
                    loops=MINIMUM_LOOPS,
                    virtual_hours=799.0,
                    enforce_minimum=True,
                )
            existing = Path(directory) / "existing"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                run_reliability_stress(
                    existing,
                    loops=MINIMUM_LOOPS,
                    virtual_hours=800.0,
                    enforce_minimum=True,
                )

    def test_defaults_cover_required_fault_names(self):
        self.assertGreaterEqual(DEFAULT_LOOPS, MINIMUM_LOOPS)
        names = {scenario.name for scenario in SCENARIOS}
        required = {
            "duplicate_idempotent",
            "duplicate_conflict",
            "out_of_order_feedback",
            "expired_plan",
            "csv_hash_corrupt",
            "missing_preflight",
            "process_exception",
            "cancel",
            "interrupt_recover",
            "partial_feedback",
            "support_missing_feedback",
        }
        self.assertTrue(required <= names)
        self.assertTrue(
            any(scenario.expected == "reject" for scenario in SCENARIOS)
        )
        self.assertTrue(
            any(scenario.expected == "task_complete" for scenario in SCENARIOS)
        )


if __name__ == "__main__":
    unittest.main()
