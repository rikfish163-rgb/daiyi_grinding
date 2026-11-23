"""Regression and fault-injection checks for the offline process sandbox."""

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daiyi_grinding import process_sandbox as sandbox


class ProcessSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configuration = sandbox.load_config()
        cls.results = {
            case["case_id"]: case
            for case in sandbox.run_suite(cls.configuration)
        }

    def test_default_matrix_is_paired_and_deterministic(self):
        self.assertEqual(len(self.results), 36)
        for pair in self.results.values():
            with self.subTest(case=pair["case_id"]):
                self.assertFalse(pair["contract_failures"])
                adaptive, fixed = pair["adaptive"], pair["fixed"]
                self.assertEqual(
                    adaptive["measurement_seed"], fixed["measurement_seed"]
                )
                self.assertEqual(
                    adaptive["initial_residual_mm"],
                    fixed["initial_residual_mm"],
                )
                self.assertEqual(adaptive["scans"][0], fixed["scans"][0])
        sample = self.results["baseline_moderate"]
        self.assertEqual(
            sandbox.run_suite(self.configuration, [sample["case_id"]]),
            [sample],
        )
        with self.assertRaisesRegex(ValueError, "at least one case"):
            sandbox.run_suite(self.configuration, [])

    def test_two_stage_feedback_follows_scans_and_uses_no_plant_parameters(
        self,
    ):
        result = self.results["baseline_moderate"]["adaptive"]
        self.assertTrue(result["passed"])
        self.assertEqual(
            [row["stage"] for row in result["trajectory"]],
            ["roughing", "roughing", "finishing", "finishing"],
        )
        for index, row in enumerate(result["trajectory"]):
            self.assertEqual(row["input_scan_index"], index)
            self.assertEqual(row["output_scan_index"], index + 1)
        for policy in sandbox.POLICIES:
            with self.subTest(policy=policy):
                pair = self.results["baseline_moderate"]
                first = pair[policy]["trajectory"][0]
                hidden_change = deepcopy(pair["case"])
                hidden_change.update(
                    material_factor=0.25,
                    hardness_factor=1.1,
                    initial_wear_index=2,
                )
                changed = sandbox.run_policy(
                    hidden_change, self.configuration, policy
                )
                self.assertEqual(
                    first["commanded_force_n"],
                    changed["trajectory"][0]["commanded_force_n"],
                )
                self.assertNotEqual(
                    first["removed_depth_max_mm"],
                    changed["trajectory"][0]["removed_depth_max_mm"],
                )
                new_observation = deepcopy(pair["case"])
                new_observation["measurement"]["bias_mm"] = 0.05
                changed = sandbox.run_policy(
                    new_observation, self.configuration, policy
                )
                self.assertEqual(
                    first["commanded_force_n"]
                    != changed["trajectory"][0]["commanded_force_n"],
                    policy == "adaptive",
                )

    def test_signed_tolerance_and_overgrind_stops(self):
        self.assertTrue(sandbox._accepted_interval(np.array([-0.3, 0.3]), 0.0))
        for extreme in (-0.3, 0.3):
            with self.subTest(extreme=extreme):
                self.assertFalse(
                    sandbox._accepted_interval(np.array([extreme]), 0.001)
                )
        invalid = deepcopy(self.configuration)
        invalid["model_parameters"]["surface_tolerance_mm"] = 0.31
        with self.assertRaisesRegex(ValueError, "±0.3 mm"):
            sandbox.validate_config(invalid)
        for policy in sandbox.POLICIES:
            incoming = self.results["initial_undercut"][policy]
            self.assertEqual(incoming["stop_reason"], "initial_undercut")
            self.assertFalse(incoming["trajectory"])
            result = self.results["combined_undercut"][policy]
            self.assertEqual(result["stop_reason"], "undercut")
            self.assertEqual(len(result["trajectory"]), 1)
            self.assertLess(result["worst_negative_residual_mm"], -0.3)
            self.assertEqual(
                result["audit"]["undercut_points"],
                list(range(sandbox.POINT_COUNT)),
            )
            self.assertFalse(result["passed"])

    def test_missing_or_disallowed_protected_points_hold_release(self):
        for policy in sandbox.POLICIES:
            for name, reason in (
                ("missing_initial_measurement", "measurement_missing"),
                ("protected_path_overlap", "protected_zone_intersection"),
                (
                    "protected_measurement_outside",
                    "protected_zone_out_of_tolerance",
                ),
                ("collision_proxy_block", "collision_proxy"),
            ):
                with self.subTest(case=name, policy=policy):
                    result = self.results[name][policy]
                    self.assertEqual(result["stop_reason"], reason)
                    self.assertFalse(result["trajectory"])
                    self.assertFalse(result["passed"])
            result = self.results["missing_protected_measurement"][policy]
            self.assertEqual(result["stop_reason"], "measurement_missing")
            self.assertEqual(result["scans"][-1]["missing_points"], [2])
            self.assertIsNone(result["scans"][-1]["signed_measurement_mm"][2])
            self.assertNotEqual(
                result["residual_profile_mm"][0],
                result["initial_residual_mm"][0],
            )
            for protected_index in (2, 4):
                self.assertEqual(
                    result["residual_profile_mm"][protected_index],
                    result["initial_residual_mm"][protected_index],
                )
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["trajectory"]), 1)

    def test_overload_trips_at_engagement_without_removal(self):
        for policy in sandbox.POLICIES:
            result = self.results["very_large_stock"][policy]
            self.assertEqual(result["stop_reason"], "force_limit_engaged")
            (attempt,) = result["trajectory"]
            self.assertGreater(attempt["measured_force_n"], 90.0)
            self.assertEqual(attempt["engaged_duration_s"], 0.0)
            self.assertEqual(attempt["removed_depth_max_mm"], 0.0)
            self.assertEqual(
                result["initial_residual_mm"], result["residual_profile_mm"]
            )
            self.assertFalse(result["passed"])

    def test_force_dropout_latches_even_if_partial_work_measures_in_band(self):
        case = deepcopy(self.results["force_dropout_after_engagement"]["case"])
        case["initial_mm"]["base"] = 0.35
        case["material_factor"] = 2.0
        case["measurement"].update(sigma_mm=0.0, uncertainty_mm=0.02)
        for policy in sandbox.POLICIES:
            with self.subTest(policy=policy):
                initial = self.results["force_dropout_initial"][policy]
                self.assertFalse(initial["trajectory"])
                self.assertEqual(
                    initial["blocked_attempt"]["stop_reason"],
                    "force_sensor_dropout",
                )
                result = sandbox.run_policy(case, self.configuration, policy)
                (attempt,) = result["trajectory"]
                self.assertEqual(result["stop_reason"], "force_sensor_dropout")
                self.assertEqual(result["elapsed_s"], 1.0)
                self.assertIsNone(attempt["measured_force_n"])
                self.assertFalse(attempt["force_sensor_available"])
                self.assertFalse(attempt["pass_completed"])
                self.assertTrue(result["accepted_by_measurement"])
                self.assertFalse(result["passed"])

    def test_runtime_thermal_stage_and_rework_limits(self):
        preflight = deepcopy(self.results["runtime_stop_isolated"]["case"])
        preflight["max_runtime_s"] = 0.1
        for policy in sandbox.POLICIES:
            with self.subTest(policy=policy):
                result = sandbox.run_policy(
                    preflight, self.configuration, policy
                )
                self.assertFalse(result["trajectory"])
                self.assertEqual(
                    result["blocked_attempt"]["stop_reason"], "budget_stop"
                )
                result = self.results["runtime_stop_isolated"][policy]
                self.assertEqual(result["elapsed_s"], 2.0)
                self.assertEqual(len(result["trajectory"]), 1)
                self.assertFalse(result["trajectory"][-1]["pass_completed"])
                self.assertEqual(result["stop_reason"], "budget_stop")
                self.assertFalse(result["passed"])
                thermal = self.results["thermal_guard_isolated"][policy]
                self.assertEqual(thermal["stop_reason"], "thermal_guard")
                self.assertAlmostEqual(thermal["temperature_rise_k"], 12.0)
                self.assertFalse(thermal["trajectory"][-1]["pass_completed"])
                for case_id, reason in (
                    ("rework_budget_stop", "rework_budget"),
                    ("pass_budget_stop", "pass_limit"),
                ):
                    result = self.results[case_id][policy]
                    self.assertEqual(result["stop_reason"], reason)
                    self.assertEqual(
                        result["pass_counts"], {"roughing": 0, "finishing": 1}
                    )
                    self.assertEqual(result["rework_pass_count"], 0)
                    self.assertFalse(result["passed"])

    def test_tool_wear_reduces_removal_at_equal_force_and_feed(self):
        sample = self.results["baseline_moderate"]
        worn_case = deepcopy(sample["case"])
        worn_case["initial_wear_index"] = 1.5
        worn = sandbox.run_policy(worn_case, self.configuration, "adaptive")
        before = sample["adaptive"]["trajectory"][0]
        after = worn["trajectory"][0]
        self.assertEqual(after["measured_force_n"], before["measured_force_n"])
        self.assertEqual(after["actual_feed_mm_s"], before["actual_feed_mm_s"])
        self.assertLess(
            after["removed_depth_max_mm"], before["removed_depth_max_mm"]
        )
        self.assertGreater(
            after["tool_wear_index"], worn_case["initial_wear_index"]
        )

    def test_blind_bias_is_an_audited_false_release(self):
        pair = self.results["hidden_measurement_bias"]
        for policy in sandbox.POLICIES:
            result = pair[policy]
            self.assertTrue(result["accepted_by_measurement"])
            self.assertTrue(result["passed"])
            self.assertFalse(result["true_signed_conforming"])
            self.assertTrue(result["audit"]["false_release"])
            self.assertAlmostEqual(
                result["scans"][0]["signed_measurement_mm"][0], 0.22
            )
            self.assertEqual(result["scans"][0]["uncertainty_mm"], 0.02)
            self.assertFalse(result["trajectory"])
        undeclared = deepcopy(pair["case"])
        undeclared["expected_false_release"] = False
        self.assertEqual(
            len(
                sandbox.run_case(undeclared, self.configuration)[
                    "contract_failures"
                ]
            ),
            2,
        )


class ProcessSandboxCliTests(unittest.TestCase):
    def test_entry_point_preserves_outputs_and_hashes_inputs_and_artifacts(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "default"
            with (
                patch.object(sandbox, "OUTPUT_ROOT", root),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(sandbox.main(["--output", "default"]), 0)
                evidence_file = output / "evidence.json"
                evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
                evidence_hash = sandbox.sha256(evidence_file)
                with self.assertRaises(FileExistsError):
                    sandbox.main(["--output", str(output)])
                self.assertEqual(sandbox.sha256(evidence_file), evidence_hash)
                with self.assertRaisesRegex(ValueError, "under"):
                    sandbox.main(
                        ["--output", str(root.parent / "out-of-scope")]
                    )
            self.assertTrue(evidence["task"])
            self.assertFalse(evidence["scope"]["truth_for_control_or_release"])
            self.assertTrue(evidence["commands"])
            self.assertTrue(evidence["real_data_pending"])
            self.assertEqual(evidence["metrics"]["case_count"], 36)
            self.assertEqual(evidence["metrics"]["unexpected_failure_count"], 0)
            self.assertFalse(evidence["failures"]["unexpected_suite_failures"])
            self.assertEqual(len(evidence["failures"]["false_releases"]), 2)
            self.assertGreater(
                len(evidence["failures"]["process_rejections"]), 0
            )
            for record in evidence["inputs"]:
                self.assertEqual(
                    sandbox.sha256(record["path"]), record["sha256"]
                )
            for record in evidence["artifacts"]:
                self.assertEqual(
                    sandbox.sha256(output / record["path"]), record["sha256"]
                )

    def test_cli_contract_mismatch_exits_nonzero_with_evidence(self):
        configuration = sandbox.load_config()
        target = next(
            case
            for case in configuration["cases"]
            if case["case_id"] == "force_dropout_initial"
        )
        target["expected_stop"] = "budget_stop"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fault config.json"
            path.write_text(json.dumps(configuration), encoding="utf-8")
            with (
                patch.object(sandbox, "OUTPUT_ROOT", root),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    sandbox.main(
                        [
                            "--config",
                            str(path),
                            "--output",
                            "failed",
                            "--cases",
                            target["case_id"],
                        ]
                    ),
                    1,
                )
            evidence = json.loads((root / "failed/evidence.json").read_text())
            self.assertEqual(evidence["metrics"]["unexpected_failure_count"], 2)
            self.assertEqual(
                evidence["failures"]["unexpected_suite_failures"][0]["case_id"],
                target["case_id"],
            )
            self.assertIn(str(path), shlex.split(evidence["commands"][0]))
            self.assertIn(
                target["case_id"], shlex.split(evidence["commands"][0])
            )
            recorded_config = next(
                record
                for record in evidence["inputs"]
                if record["role"] == "process_sandbox_config"
            )
            self.assertEqual(recorded_config["path"], str(path))
            self.assertEqual(recorded_config["sha256"], sandbox.sha256(path))

    def test_default_suite_imports_outside_checkout_without_mujoco_or_torch(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from daiyi_grinding import process_sandbox
assert len(process_sandbox.run_suite(process_sandbox.load_config())) == 36
assert not {'torch', 'mujoco'}.intersection(sys.modules)
""",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unexpected_model_exception_propagates_to_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
from unittest.mock import patch
from daiyi_grinding import process_sandbox
with patch.object(process_sandbox, 'run_suite', side_effect=RuntimeError('injected model fault')):
    raise SystemExit(process_sandbox.main(['--output', 'never_written']))
""",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("injected model fault", result.stderr)
