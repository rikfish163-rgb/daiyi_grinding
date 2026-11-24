"""Reliability stress evidence, fault matrix and virtual-clock tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    def test_nonfinite_boolean_and_unbounded_virtual_clocks_are_rejected(self):
        for kwargs in (
            {"hours": float("nan")},
            {"hours": True},
            {"hours": 10**400},
            {"heartbeat_s": 1e-100},
            {"heartbeat_s": float("inf")},
            {"minimum_hours": 0},
            {"minimum_hours": 10**400},
            {"boot_id": ""},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_virtual_reliability(**kwargs)

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
        self.assertEqual(
            {event["boot_id"] for event in trace["heartbeats"]},
            {trace["boot_id"]},
        )
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
                virtual_hours=800.0,
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
                metrics["loops"]["expected_rejection_codes"],
                metrics["loops"]["observed_rejection_codes"],
            )
            self.assertEqual(
                metrics["loops"]["expected_accepts"],
                metrics["loops"]["observed_expected_accepts"],
            )
            self.assertEqual(
                metrics["loops"]["expected_rejections"],
                sum(scenario.expected == "reject" for scenario in SCENARIOS),
            )
            self.assertEqual(
                metrics["loops"]["task_quality_complete_cycles"],
                sum(
                    scenario.expected == "task_complete"
                    for scenario in SCENARIOS
                ),
            )
            self.assertEqual(
                metrics["loops"]["real_task_quality_complete_cycles"], 0
            )
            self.assertEqual(metrics["loops"]["sample_count"], len(SCENARIOS))
            self.assertGreater(metrics["loops"]["control_feedback_samples"], 0)
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
            self.assertTrue(
                historical["plan_summary"]["preflight_binding"][
                    "legacy_unbound_preflight"
                ]
            )
            self.assertTrue(
                historical["historical_source_scope"]["current_source_drift"]
            )
            self.assertFalse(evidence["scope"]["torch_imported"])
            self.assertTrue(
                evidence["source_consistency"]["offline_sources_unchanged"]
            )
            self.assertEqual(
                historical["plan_summary"]["result_evidence"]["task_status"],
                "partial",
            )
            virtual = evidence["virtual_reliability"]
            self.assertEqual(virtual["virtual_hours"], 800.0)
            self.assertTrue(virtual["meets_target"])
            self.assertTrue(virtual["accelerated_coverage"])
            self.assertTrue(virtual["real_800h_pending"])
            receipt = json.loads(
                (output / "reliability_receipt.json").read_text()
            )
            self.assertEqual(receipt["clock_kind"], "virtual")
            self.assertEqual(receipt["source_kind"], "virtual_reliability")
            self.assertTrue(receipt["complete"])
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
                if "snapshot_path" in item:
                    self.assertEqual(
                        sha256(output / item["snapshot_path"]), item["sha256"]
                    )
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
            by_scenario = {
                record["scenario"]: record for record in loop_records
            }
            self.assertEqual(
                by_scenario["feedback_batch_atomic"]["covered_command_count"], 0
            )
            self.assertEqual(
                by_scenario["feedback_batch_atomic"]["feedback_samples"], 0
            )
            self.assertIn(
                "overgrind", by_scenario["quality_overgrind"]["quality_reasons"]
            )
            self.assertIn(
                "negative_limit_risk",
                by_scenario["quality_negative_limit_risk"]["quality_reasons"],
            )
            self.assertTrue(
                by_scenario["quality_invalid_view"]["quality_region_failures"][
                    0
                ]["invalid_view"]
            )
            self.assertEqual(
                {record["scenario"] for record in loop_records},
                {scenario.name for scenario in SCENARIOS},
            )
            loop_artifacts = json.loads(
                (output / "loops/artifacts.json").read_text()
            )
            for item in loop_artifacts["artifacts"]:
                self.assertEqual(sha256(output / item["path"]), item["sha256"])
            virtual_rules = evidence["virtual_fault_rules"]
            self.assertEqual(virtual_rules["case_count"], 5)
            self.assertEqual(virtual_rules["matched"], 5)
            self.assertTrue(
                all(
                    case["real_800h_pending"] for case in virtual_rules["cases"]
                )
            )
            self.assertTrue(
                all(
                    case["fault_rule_matched"]
                    for case in virtual_rules["cases"]
                )
            )
            by_rule = {case["case"]: case for case in virtual_rules["cases"]}
            self.assertEqual(by_rule["healthy"]["observed"], "pass")
            self.assertTrue(
                all(
                    not case["evaluation_input_failures"]
                    for case in by_rule.values()
                )
            )
            for name in ("health_fault", "heartbeat_gap"):
                self.assertEqual(
                    by_rule[name]["expected_rejection_reasons"][0]["code"],
                    "criterion_not_met",
                )
            self.assertEqual(
                by_rule["clock_restart"]["expected_rejection_reasons"][0][
                    "code"
                ],
                "reliability_restart",
            )
            self.assertEqual(
                by_rule["clock_mismatch"]["expected_rejection_reasons"][0][
                    "code"
                ],
                "reliability_clock_mismatch",
            )

    def test_mock_entry_cannot_import_models_or_open_network_sockets(self):
        code = """
import builtins, json, socket, sys, tempfile
from pathlib import Path
original = builtins.__import__
attempts = []
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'mujoco'}:
        attempts.append(name)
        raise AssertionError('heavy import is forbidden')
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs):
    attempts.append('network_socket')
    raise AssertionError('network sockets are forbidden')
builtins.__import__ = guarded
socket.socket = forbidden
socket.create_connection = forbidden
from daiyi_grinding.reliability_stress import run_reliability_stress, SCENARIOS
with tempfile.TemporaryDirectory() as directory:
    result = run_reliability_stress(
        Path(directory) / 'guarded', loops=len(SCENARIOS),
        virtual_hours=2.0, enforce_minimum=False,
    )
    print(json.dumps({'attempts': attempts, 'failures': result['failures']}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["attempts"], [])
        self.assertEqual(output["failures"], [])

    def test_minimum_loop_and_800h_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            for kwargs in (
                {"loops": True},
                {"seed": True},
                {"virtual_hours": True},
                {"virtual_hours": float("inf")},
                {"virtual_hours": 10**400},
            ):
                invalid = Path(directory) / "invalid_numeric_input"
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    run_reliability_stress(invalid, **kwargs)
                self.assertFalse(invalid.exists())
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
            "feedback_batch_atomic",
            "feedback_sequence_gap",
            "checkpoint_counter_corrupt",
            "checkpoint_log_binding",
            "quality_binding",
            "quality_hash_changed",
            "quality_overgrind",
            "quality_negative_limit_risk",
            "quality_invalid_view",
            "clock_regression",
            "feedback_dropout",
            "source_changed_during_execution",
            "receipt_hashes",
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
