"""Failure boundaries for the serial offline runner, using tiny subprocesses."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from daiyi_grinding import enterprise_offline as runner

PLANNING_STUB = r"""
import hashlib
import json
import os
import sys
from pathlib import Path

out, source = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True)
def write(name, value):
    (out / name).write_text(json.dumps(value, allow_nan=False))
def ref(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
suite = {
    "guard_count": 1, "guards_passed": 1,
    "cases": [{"id": "fixture_rejection", "guard_passed": True, "kind": "mock"}],
}
write("suite.json", suite)
evidence = {
    "task": "pure subprocess stub",
    "scope": {"offline": True, "mock": True},
    "commands": [{"argv": sys.argv}],
    "inputs": [ref(source)],
    "metrics": {"stress_suite": {"guard_count": 1, "guards_passed": 1}},
    "failures": [],
    "artifacts": [ref(out / "suite.json")],
    "real_data_pending": ["independent real measurements"],
}
write("evidence.json", evidence)
print("retained stdout", flush=True)
print("retained stderr", file=sys.stderr, flush=True)
"""


class OfflineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("src", "tests", "config"):
            (self.root / name).mkdir()
        self.source = self.root / "src/token.py"
        self.source.write_text("frozen = 1\n", encoding="utf-8")
        (self.root / "tests/test_token.py").write_text("# frozen tests\n")
        (self.root / "config/model.json").write_text('{"frozen": true}\n')
        (self.root / "pyproject.toml").write_text("[project]\nname='stub'\n")
        self.output = self.root / "bundle"

    def planning_output(self, destination=None):
        destination = destination or self.root / "task_output"
        destination.mkdir(parents=True)
        suite = {
            "guard_count": 1,
            "guards_passed": 1,
            "cases": [
                {
                    "id": "expected_rejection",
                    "kind": "mock",
                    "guard_passed": True,
                }
            ],
        }
        runner.write_json(destination / "suite.json", suite)
        evidence = {
            "task": "pure fixture",
            "scope": {"offline": True, "mock": True},
            "commands": [["python", "fixture"]],
            "inputs": [runner.file_reference(self.source)],
            "metrics": {"stress_suite": {"guard_count": 1, "guards_passed": 1}},
            "failures": [],
            "artifacts": [runner.file_reference(destination / "suite.json")],
            "real_data_pending": ["real observations"],
        }
        runner.write_json(destination / "evidence.json", evidence)
        return runner.Task(
            "planning_stress", destination, ("fixture",)
        ), evidence

    def run_stub(self, suffix="", *, timeout=5.0, names=("planning_stress",)):
        def tasks(output, selected, **kwargs):
            return [
                runner.Task(
                    name,
                    output / "tasks" / name,
                    (
                        sys.executable,
                        "-c",
                        PLANNING_STUB + suffix,
                        str(output / "tasks" / name),
                        str(self.source),
                    ),
                )
                for name in selected
            ]

        with mock.patch.object(runner, "build_tasks", side_effect=tasks):
            return runner.run(
                self.output,
                root=self.root,
                tasks=names,
                task_timeout=timeout,
            )

    def execute(self, name, code, *, timeout=5.0, lock_owner="not_required"):
        before = runner.snapshot_sources(self.root)
        task = runner.Task(
            name,
            self.root / f"output_{name}",
            (sys.executable, "-c", code),
            lock_owner,
        )
        return runner.execute_task(
            task,
            self.root / f"receipt_{name}",
            root=self.root,
            freeze=runner.SourceFreeze(self.root, before),
            timeout=timeout,
        )

    def main_chain_output(self, *, normal=False, hold_reason="overgrind"):
        name = runner.MAIN_CHAIN_NAMES[1 if normal else 0]
        destination = self.root / name
        destination.mkdir()
        execution = None
        if not normal:
            execution = {
                "simulation_steps": 2,
                "simulated_seconds": 0.02,
                "unexpected_contact_count": 0,
                "joint_positions_teleported_during_execution": False,
                "tracking_trace_p95_mm": 0.04,
                "surface_tracking_p95_mm": 0.08,
            }
        result = {
            "case": "normal" if normal else "thin",
            "seed": 9904 if normal else 120483,
            "path_count": 0 if normal else 1,
            "admitted_path_count": 0 if normal else 1,
            "robot_executed": not normal,
            "material_removal_simulated": not normal,
            "motion_gate_passed": None if normal else True,
            "execution": execution,
            "executions": [] if normal else [execution],
            "goal_complete": False,
            "observational_release": False,
            "task_status": "no_defect_detected" if normal else "partial",
            "controller_stopping": {
                "reason": hold_reason,
                "truth_used_for_control": False,
            },
        }
        config = {
            "detector": "geometric",
            "tool": "belt",
            "grasp_setup": "original",
            "views": "multi",
            "mode": "removal",
            "max_passes": 1,
            "realtime_factor": 0,
            "cell": {"planning": {"tracking_p95_limit_mm": 1.0}},
        }
        documents = {
            "result.json": result,
            "status.json": {
                "stage": "complete",
                "task_status": result["task_status"],
            },
            "config.json": config,
            "motion_commands.json": [] if normal else [{"segment_id": 1}],
            "cycle_receipt.json": {
                "evidence_scope": "simulation",
                "part": "097",
                "quality": {
                    key: result[key]
                    for key in (
                        "observational_release",
                        "motion_gate_passed",
                        "task_status",
                    )
                },
            },
        }
        if not normal:
            documents["pass_00_execution.json"] = execution
            documents["replay.json"] = {"scope": "pure fixture"}
            columns = [
                "time_s",
                *[f"q{index}_rad" for index in range(1, 7)],
                *[f"actual_{axis}_m" for axis in "xyz"],
                *[f"target_{axis}_m" for axis in "xyz"],
                "error_mm",
            ]
            (destination / "pass_00_execution.csv").write_text(
                ",".join(columns)
                + "\n"
                + ",".join(["0", *(["0"] * (len(columns) - 1))])
                + "\n"
                + ",".join(["0.01", *(["0"] * (len(columns) - 1))])
                + "\n"
            )
        for filename, value in documents.items():
            runner.write_json(destination / filename, value)
        source_manifest = self.root / "sources_before.json"
        runner.write_json(source_manifest, runner.snapshot_sources(self.root))
        task = runner.Task(name, destination, ("stub",), "parent")
        receipt = {
            "argv": ["pure_fixture"],
            "cwd": str(self.root),
            "environment": runner.ENVIRONMENT,
            "exit_code": 0,
        }
        runner._main_chain_evidence(task, receipt, source_manifest)
        return task

    def test_import_does_not_load_task_modules_or_torch(self):
        code = (
            "import sys; from daiyi_grinding import enterprise_offline; "
            "assert 'torch' not in sys.modules; "
            "assert 'mujoco' not in sys.modules; "
            "assert 'daiyi_grinding.workstation_run' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_strict_json_rejects_duplicates_and_all_nonfinite_forms(self):
        for text in (
            '{"same": 1, "same": 2}',
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"value": -Infinity}',
            '{"value": 1e999}',
        ):
            with self.subTest(text=text):
                with self.assertRaises(runner.EvidenceError):
                    runner.strict_json(text)

    def test_missing_evidence_is_a_failure(self):
        task = runner.Task("planning_stress", self.root / "absent", ("stub",))
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertEqual(result.errors[0]["code"], "missing_evidence")

    def test_missing_required_envelope_field_is_a_failure(self):
        task, evidence = self.planning_output()
        del evidence["real_data_pending"]
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn("real_data_pending", result.errors[0]["reason"])

    def test_hash_tampering_is_detected_even_with_passed_metrics(self):
        task, _ = self.planning_output()
        suite = runner.read_json(task.output / "suite.json")
        suite["unreported_change"] = True
        runner.write_json(task.output / "suite.json", suite)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "hash_mismatch", {error["code"] for error in result.errors}
        )

    def test_deleted_artifact_is_not_an_expected_rejection(self):
        task, _ = self.planning_output()
        (task.output / "suite.json").unlink()
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "missing_file", {error["code"] for error in result.errors}
        )

    def test_missing_input_hash_is_not_accepted(self):
        task, evidence = self.planning_output()
        evidence["inputs"][0].pop("sha256")
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "missing_or_invalid_hash",
            {error["code"] for error in result.errors},
        )

    def test_bare_missing_path_in_a_mixed_input_list_is_not_ignored(self):
        task, evidence = self.planning_output()
        evidence["inputs"].append("missing_input.json")
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "missing_file", {error["code"] for error in result.errors}
        )

    def test_reference_shapes_are_resolved_and_verified(self):
        task, evidence = self.planning_output()
        source_hash = runner.sha256(self.source)
        config = self.root / "config/model.json"
        evidence["inputs"] = {
            "source_root": str(self.root),
            "files": [{"path": "src/token.py", "sha256": source_hash}],
            "paired": {
                "cloud_path": str(self.source),
                "cloud_sha256": source_hash,
                "manifest_path": "config/model.json",
                "manifest_sha256": runner.sha256(config),
            },
            "manifest": {
                "batch_manifest": "config/model.json",
                "batch_manifest_sha256": runner.sha256(config),
            },
            "artifact_sha256": {
                "src/token.py": source_hash,
                "config/model.json": runner.sha256(config),
            },
        }
        evidence["artifacts"] = {
            "by_path": {"suite.json": runner.sha256(task.output / "suite.json")}
        }
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(len(result.references), 7)
        self.assertTrue(
            all(row["status"] == "verified" for row in result.references)
        )

    def test_nonzero_declared_command_cannot_be_hidden_by_suite_counts(self):
        task, evidence = self.planning_output()
        evidence["commands"] = [{"argv": ["check"], "returncode": 2}]
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "declared_command_failed",
            {error["code"] for error in result.errors},
        )

    def test_suite_counts_without_explicit_case_results_do_not_pass(self):
        task, evidence = self.planning_output()
        runner.write_json(task.output / "suite.json", {"guards_passed": 1})
        evidence["artifacts"] = [
            runner.file_reference(task.output / "suite.json")
        ]
        runner.write_json(task.output / "evidence.json", evidence)
        self.assertFalse(runner.validate_evidence(task, root=self.root).passed)

    def test_malformed_artifact_structure_is_a_recorded_failure(self):
        task, evidence = self.planning_output()
        runner.write_json(task.output / "suite.json", ["invalid structure"])
        evidence["artifacts"] = [
            runner.file_reference(task.output / "suite.json")
        ]
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertTrue(result.errors)

    def test_evidence_cannot_hash_itself(self):
        task, evidence = self.planning_output()
        evidence["artifacts"].append(
            runner.file_reference(task.output / "evidence.json")
        )
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertIn(
            "self_evidence_hash", {error["code"] for error in result.errors}
        )

    def test_reproduced_missing_feedback_is_a_scoped_expected_rejection(self):
        destination = self.root / "acceptance"
        case_root = destination / "synthetic_suite/controller_feedback_missing"
        case_root.mkdir(parents=True)
        absent = case_root / "absent.json"
        missing = {
            "path": str(absent),
            "sha256": None,
            "expected_sha256": "a" * 64,
            "role": "controller_feedback",
            "integrity": "missing",
        }
        case = {
            "case": "controller_feedback_missing",
            "expected": {"source_integrity": "fail"},
            "observed": {"source_integrity": "fail"},
            "expected_decision_reproduced": True,
            "all_enterprise_targets_unknown": True,
            "inputs": [missing],
        }
        suite_path = destination / "synthetic_suite/suite_summary.json"
        runner.write_json(
            suite_path,
            {
                "case_count": 1,
                "expected_decisions_reproduced": 1,
                "cases": [case],
            },
        )
        evidence = {
            "task": "acceptance missing-feedback mock",
            "scope": "offline synthetic",
            "commands": [["stub"]],
            "inputs": [runner.file_reference(self.source), missing],
            "metrics": {
                "synthetic_cases": 1,
                "synthetic_expected_decisions_reproduced": 1,
                "virtual_800h_is_real_800h": False,
                "real_enterprise_acceptances": 0,
            },
            "failures": [
                {
                    "assessment": "synthetic/controller_feedback_missing",
                    "code": "missing_artifact",
                }
            ],
            "artifacts": [runner.file_reference(suite_path)],
            "real_data_pending": ["real data"],
        }
        runner.write_json(destination / "evidence.json", evidence)
        task = runner.Task("enterprise_acceptance", destination, ("stub",))
        result = runner.validate_evidence(task, root=self.root)
        self.assertTrue(result.passed, result.errors)
        self.assertIn(
            "expected_missing_rejected",
            {row["status"] for row in result.references},
        )
        evidence["inputs"][1] = {
            **missing,
            "path": str(self.root / "ordinary_missing"),
        }
        runner.write_json(destination / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "missing_file", {error["code"] for error in result.errors}
        )

    def test_expected_rejection_and_pending_real_do_not_fail_software(self):
        summary = self.run_stub()
        self.assertTrue(summary["software_suite_passed"])
        self.assertFalse(summary["enterprise_acceptance_passed"])
        self.assertTrue(summary["source_unchanged"])
        self.assertEqual(summary["new_physical_cases"], 0)
        catalog = runner.read_json(self.output / "failure_catalog.json")
        self.assertEqual(catalog["software_errors"], [])
        self.assertEqual(len(catalog["expected_rejections"]), 1)
        self.assertEqual(len(catalog["pending_real"]), 1)
        for name in (
            "summary.json",
            "evidence.json",
            "report.md",
            "experience.jsonl",
            "experience.csv",
            "failure_catalog.json",
            "next_experiments.json",
        ):
            self.assertTrue((self.output / name).is_file(), name)
        evidence = runner.read_json(self.output / "evidence.json")
        self.assertTrue(
            all(
                Path(ref["path"]) != self.output / "evidence.json"
                for ref in evidence["artifacts"]
            )
        )
        receipt = runner.read_json(
            self.output / "receipts/planning_stress/receipt.json"
        )
        self.assertEqual(receipt["environment"], runner.ENVIRONMENT)
        self.assertIn("-c", receipt["argv"])
        self.assertEqual(receipt["exit_code"], 0)
        self.assertGreater(receipt["peak_rss_kib"], 0)

    def test_nonzero_child_exit_is_retained_and_fails_the_bundle(self):
        summary = self.run_stub("\nraise SystemExit(7)\n")
        self.assertFalse(summary["software_suite_passed"])
        row = summary["tasks"][0]
        self.assertEqual(row["exit_code"], 7)
        self.assertIn(
            "nonzero_exit", {error["code"] for error in row["failures"]}
        )
        receipts = self.output / "receipts/planning_stress"
        self.assertIn("retained stdout", (receipts / "stdout.log").read_text())
        self.assertIn("retained stderr", (receipts / "stderr.log").read_text())

    def test_source_change_kills_the_group_and_skips_remaining_tasks(self):
        suffix = (
            "\nimport time\n"
            "source.write_text('frozen = 2\\n')\n"
            "time.sleep(10)\n"
        )
        summary = self.run_stub(
            suffix, names=("planning_stress", "reinspection_stress")
        )
        self.assertFalse(summary["software_suite_passed"])
        self.assertFalse(summary["source_unchanged"])
        self.assertIn("src/token.py", summary["changed_source_files"])
        self.assertTrue(summary["tasks"][0]["executed"])
        self.assertFalse(summary["tasks"][1]["executed"])
        self.assertEqual(summary["executed_task_count"], 1)
        self.assertTrue((self.output / "sources_after.json").is_file())

    def test_timeout_kills_descendant_processes_and_retains_logs(self):
        marker = self.root / "escaped_child"
        descendant = (
            "import time; from pathlib import Path; time.sleep(0.8); "
            f"Path({str(marker)!r}).write_text('should not happen')"
        )
        suffix = (
            "\nimport subprocess, time\n"
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
            "time.sleep(10)\n"
        )
        summary = self.run_stub(suffix, timeout=0.3)
        self.assertFalse(summary["software_suite_passed"])
        self.assertIn(
            "task_timeout",
            {error["code"] for error in summary["tasks"][0]["failures"]},
        )
        time.sleep(0.9)
        self.assertFalse(marker.exists())
        receipt = runner.read_json(
            self.output / "receipts/planning_stress/receipt.json"
        )
        self.assertTrue(receipt["timed_out"])
        self.assertGreater(receipt["peak_rss_kib"], 0)

    def test_peak_rss_is_per_child_and_not_cumulative(self):
        large = self.execute(
            "large",
            "import time; buffer = bytearray(64 * 1024 * 1024); time.sleep(0.1)",
        )
        small = self.execute("small", "pass")
        self.assertEqual((large["exit_code"], small["exit_code"]), (0, 0))
        self.assertGreater(large["peak_rss_kib"], small["peak_rss_kib"] + 32000)
        resource = runner.read_json(self.root / "receipt_small/resource.json")
        self.assertFalse(resource["cumulative_parent_resource_used"])
        self.assertIn("wait4(child_pid)", resource["method"])

    def test_bare_main_chain_parent_holds_shared_lock(self):
        lock = self.root / "runs/enterprise_offline_20261123/mujoco.lock"
        code = (
            "import fcntl; from pathlib import Path\n"
            f"stream = Path({str(lock)!r}).open('a+')\n"
            "try:\n"
            "    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    print('parent lock observed')\n"
            "else:\n"
            "    raise SystemExit(5)\n"
        )
        receipt = self.execute("locked", code, lock_owner="parent")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn(
            "parent lock observed",
            (self.root / "receipt_locked/stdout.log").read_text(),
        )

    def test_normal_main_chain_null_execution_is_a_valid_zero_action_control(
        self,
    ):
        task = self.main_chain_output(normal=True)
        result = runner.validate_evidence(task, root=self.root)
        self.assertTrue(result.passed, result.errors)
        self.assertFalse(result.evidence["metrics"]["robot_executed"])
        self.assertIsNone(result.evidence["metrics"]["simulated_seconds"])
        self.assertFalse(
            result.evidence["scope"]["enterprise_acceptance_passed"]
        )

    def test_signed_main_chain_hold_does_not_fail_completed_software(self):
        task = self.main_chain_output(hold_reason="negative_limit_risk")
        result = runner.validate_evidence(task, root=self.root)
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.cases[0]["outcome"], "observed_quality_hold")
        self.assertEqual(
            result.cases[0]["details"]["stopping_reason"], "negative_limit_risk"
        )
        self.assertFalse(
            result.cases[0]["details"]["enterprise_acceptance_passed"]
        )

    def test_thin_main_chain_declared_gate_without_feedback_does_not_pass(self):
        task = self.main_chain_output()
        (task.output / "pass_00_execution.csv").unlink()
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn(
            "missing_file", {error["code"] for error in result.errors}
        )

    def test_normal_main_chain_cannot_hide_motion_in_execution_record(self):
        task = self.main_chain_output(normal=True)
        result = runner.read_json(task.output / "result.json")
        result["execution"] = {"simulation_steps": 1}
        runner.write_json(task.output / "result.json", result)
        evidence = runner.read_json(task.output / "evidence.json")
        for reference in evidence["artifacts"]:
            if Path(reference["path"]).name == "result.json":
                reference["sha256"] = runner.sha256(task.output / "result.json")
        runner.write_json(task.output / "evidence.json", evidence)
        check = runner.validate_evidence(task, root=self.root)
        self.assertFalse(check.passed)
        self.assertIn("zero paths and zero actions", check.errors[-1]["reason"])

    def test_virtual_800_hours_cannot_claim_real_reliability(self):
        task, evidence = self.planning_output()
        task = runner.Task("reliability_stress", task.output, ("stub",))
        evidence["scope"] = {
            "real_800h_pending": False,
            "real_controller_used": False,
        }
        evidence["metrics"] = {
            "loops": {
                "actual_cycles": 120,
                "mismatches": 0,
                "unexpected_exceptions": 0,
                "meets_minimum": True,
            },
            "virtual": {
                "complete": True,
                "meets_target": True,
                "virtual_hours": 800,
                "real_800h_pending": False,
            },
        }
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn("pending real 800-hour", result.errors[-1]["reason"])

    def test_process_injected_false_releases_pass_contract_but_remain_failures(
        self,
    ):
        destination = self.root / "process"
        (destination / "cases").mkdir(parents=True)
        artifacts, false_releases = [], []
        for index in range(36):
            name = "hidden_measurement_bias" if index == 0 else f"stub_{index}"
            false = index == 0
            row = {
                "case_id": name,
                "case": {
                    "expected_false_release": false,
                    "expected_stop": None,
                },
                "contract_failures": [],
            }
            for policy in ("adaptive", "fixed"):
                row[policy] = {
                    "passed": True,
                    "audit": {"false_release": false},
                    "stop_reason": "signed_tolerance_reached",
                    "failures": ["measurement_acceptance_true_mismatch"]
                    if false
                    else [],
                }
                if false:
                    false_releases.append(
                        {
                            "case_id": name,
                            "policy": policy,
                            "expected_fault": True,
                            "false_release": True,
                        }
                    )
            path = destination / "cases" / f"{name}.json"
            runner.write_json(path, row)
            artifacts.append(runner.file_reference(path))
        metrics = {
            "case_count": 36,
            "policy_run_count": 72,
            "unexpected_failure_count": 0,
            **{
                policy: {
                    "pass_count": 36,
                    "conforming_release_count": 35,
                    "false_release_count": 1,
                    "rejection_count": 0,
                }
                for policy in ("adaptive", "fixed")
            },
        }
        runner.write_json(destination / "summary.json", metrics)
        evidence = {
            "task": "pure process stub",
            "scope": {"offline_only": True, "process_model": "synthetic proxy"},
            "commands": [["pure_stub"]],
            "inputs": [runner.file_reference(self.source)],
            "metrics": metrics,
            "failures": {
                "process_rejections": [],
                "false_releases": false_releases,
                "unexpected_suite_failures": [],
            },
            "artifacts": [
                *artifacts,
                runner.file_reference(destination / "summary.json"),
            ],
            "real_data_pending": ["independent signed measurements"],
        }
        runner.write_json(destination / "evidence.json", evidence)
        task = runner.Task("process_sandbox", destination, ("stub",))
        check = runner.validate_evidence(task, root=self.root)
        self.assertTrue(check.passed, check.errors)
        known = [
            row
            for row in check.cases
            if row["outcome"] == "known_false_release"
        ]
        self.assertEqual(len(known), 2)
        self.assertEqual(len(check.cases), 72)
        evidence["failures"]["false_releases"] = []
        runner.write_json(destination / "evidence.json", evidence)
        check = runner.validate_evidence(task, root=self.root)
        self.assertFalse(check.passed)
        self.assertIn("two injected", check.errors[-1]["reason"])

    def test_default_commands_do_not_dry_plan_or_double_lock_modules(self):
        tasks = runner.build_tasks(
            self.output,
            runner.TASK_NAMES,
            root=self.root,
            python=sys.executable,
            suite_id="test",
        )
        self.assertEqual(tuple(task.name for task in tasks), runner.TASK_NAMES)
        for task in tasks:
            self.assertNotIn("--dry-plan", task.argv)
            self.assertNotEqual(task.lock_owner, "parent")
        planning = next(
            task for task in tasks if task.name == "planning_stress"
        )
        self.assertNotIn("--mujoco-fixture", planning.argv)
        process = next(task for task in tasks if task.name == "process_sandbox")
        self.assertTrue(
            process.output.is_relative_to(
                self.root / "runs/enterprise_offline_20261123/process"
            )
        )
        extended = runner.build_tasks(
            self.output,
            runner.TASK_NAMES,
            root=self.root,
            include_main_chain=True,
        )
        self.assertEqual(len(extended), 10)
        planning = next(
            task for task in extended if task.name == "planning_stress"
        )
        self.assertIn("--mujoco-fixture", planning.argv)
        self.assertEqual(planning.lock_owner, "module")
        thin, normal = extended[-2:]
        self.assertEqual(
            (thin.lock_owner, normal.lock_owner), ("parent", "parent")
        )
        self.assertIn("120483", thin.argv)
        self.assertIn("9904", normal.argv)
        self.assertIn("097_thin_seam", thin.argv)
        self.assertIn("normal", normal.argv)

    def test_reused_output_and_invalid_selection_are_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "new directory"):
            runner.run(self.output, root=self.root)
        for tasks in ([], ["planning_stress"] * 2, ["invented_task"]):
            with self.subTest(tasks=tasks):
                with self.assertRaises(ValueError):
                    runner.run(self.root / "fresh", root=self.root, tasks=tasks)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    runner.run(
                        self.root / "fresh",
                        root=self.root,
                        task_timeout=timeout,
                    )

    def test_source_snapshots_detect_added_removed_and_changed_files(self):
        before = runner.snapshot_sources(self.root)
        self.source.write_text("frozen = 2\n")
        added = self.root / "tests/new_test.py"
        added.write_text("# added\n")
        (self.root / "config/model.json").unlink()
        cache = self.root / "src/__pycache__"
        cache.mkdir()
        (cache / "token.pyc").write_bytes(b"generated")
        after = runner.snapshot_sources(self.root)
        self.assertEqual(
            runner.changed_sources(before, after),
            ["config/model.json", "src/token.py", "tests/new_test.py"],
        )

    def test_dry_plan_with_passed_flag_cannot_count_as_scenarios(self):
        task, evidence = self.planning_output()
        task = runner.Task("offline_scenarios", task.output, ("stub",))
        evidence["passed"] = True
        evidence["metrics"] = {
            "generated_episodes": 0,
            "generated_frames": 0,
            "geometry_validated_episodes": 0,
            "planned_scenarios": 240,
        }
        runner.write_json(task.output / "evidence.json", evidence)
        result = runner.validate_evidence(task, root=self.root)
        self.assertFalse(result.passed)
        self.assertIn("actually render", result.errors[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
