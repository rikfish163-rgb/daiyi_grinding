"""Fault-oriented checks for offline evidence and enterprise target boundaries."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding.enterprise_acceptance import (
    BUNDLE_SCHEMA,
    DEFAULT_REQUIREMENTS,
    EvidenceError,
    artifact_reference,
    assess_bundle,
    evaluate_evidence,
    evaluate_run,
    load_requirements,
    run_synthetic_suite,
)
from daiyi_grinding.workstation_delivery import (
    site_acceptance,
    surface_measurement_report,
)


class EnterpriseAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="daiyi_acceptance_test_"
        )
        cls.root = Path(cls.temporary.name)
        cls.suite = run_synthetic_suite(cls.root / "suite")
        cls.cases = {case["case"]: case for case in cls.suite["cases"]}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def report(self, name="synthetic_normal"):
        return json.loads(
            Path(self.cases[name]["assessment"]).read_text(encoding="utf-8")
        )

    def matrix(self, name="synthetic_normal"):
        return {row["id"]: row for row in self.report(name)["matrix"]}

    def copy_case(self, name="synthetic_normal"):
        destination = (
            Path(tempfile.mkdtemp(prefix="modified_case_", dir=self.root))
            / "case"
        )
        shutil.copytree(self.root / "suite" / name, destination)
        return destination

    def rewrite_role(self, directory, role, change):
        manifest = directory / "input.json"
        bundle = json.loads(manifest.read_text())
        path = directory / bundle["artifacts"][role]["path"]
        payload = json.loads(path.read_text())
        change(payload)
        path.write_text(json.dumps(payload, allow_nan=False))
        bundle["artifacts"][role] = artifact_reference(
            path, relative_to=directory
        )
        manifest.write_text(json.dumps(bundle, allow_nan=False))
        return evaluate_evidence(manifest)

    def test_all_named_faults_reproduce_their_expected_decisions(self):
        self.assertEqual(self.suite["case_count"], 27)
        self.assertEqual(self.suite["expected_decisions_reproduced"], 27)
        self.assertTrue(
            all(
                case["expected_decision_reproduced"]
                for case in self.cases.values()
            )
        )

    def test_synthetic_success_cannot_become_real_enterprise_acceptance(self):
        report = self.report()
        self.assertFalse(report["enterprise_acceptance_passed"])
        self.assertFalse(report["enterprise_targets_evidenced"])
        for row in report["matrix"]:
            if row["kind"] == "enterprise_target":
                self.assertEqual(row["status"], "unknown", row["id"])
                self.assertFalse(row["real_evidence"], row["id"])
        self.assertEqual(
            self.matrix()["signed_surface"]["evidence_status"], "pass"
        )
        self.assertEqual(
            self.matrix()["localization"]["evidence_status"], "pass"
        )

    def test_empty_evidence_remains_unknown_and_not_vacuously_successful(self):
        report = assess_bundle(
            {"schema": BUNDLE_SCHEMA, "artifacts": {}}, base=self.root
        )
        self.assertFalse(report["enterprise_targets_evidenced"])
        self.assertFalse(report["enterprise_acceptance_passed"])
        self.assertTrue(
            all(row["status"] == "unknown" for row in report["matrix"])
        )

    def test_perfect_binary_score_does_not_hide_wrong_defect_categories(self):
        matrix = self.matrix("high_f1_wrong_class")
        self.assertEqual(matrix["defect_binary"]["measured"]["f1"], 1)
        self.assertEqual(matrix["defect_binary"]["status"], "unknown")
        self.assertEqual(matrix["defect_category"]["measured"]["matched"], 1)
        self.assertEqual(matrix["defect_category"]["measured"]["generated"], 3)
        self.assertEqual(matrix["defect_category"]["status"], "fail")
        self.assertEqual(matrix["simulation_cycle"]["status"], "fail")
        self.assertEqual(matrix["localization"]["status"], "unknown")

    def test_motion_is_strict_at_one_mm_and_not_a_surface_measurement(self):
        matrix = self.matrix("motion_at_boundary")
        self.assertEqual(
            matrix["motion_tracking"]["measured"]["surface_p95_max_mm"], 1
        )
        self.assertEqual(matrix["motion_tracking"]["status"], "fail")
        self.assertEqual(matrix["signed_surface"]["evidence_status"], "pass")
        self.assertEqual(matrix["signed_surface"]["status"], "unknown")

    def test_no_motion_control_does_not_claim_a_motion_pass(self):
        matrix = self.matrix("normal_no_action")
        self.assertEqual(matrix["motion_tracking"]["status"], "unknown")
        self.assertIsNone(
            matrix["motion_tracking"]["measured"]["surface_p95_max_mm"]
        )
        self.assertIsNone(matrix["defect_binary"]["measured"]["f1"])
        self.assertEqual(matrix["simulation_cycle"]["status"], "pass")
        self.assertTrue(
            matrix["simulation_cycle"]["measured"]["normal_no_action_control"]
        )
        self.assertFalse(
            matrix["simulation_cycle"]["measured"][
                "nontrivial_grinding_complete"
            ]
        )

    def test_signed_overcut_positive_excess_and_uncertainty_are_rejected(self):
        for name in (
            "positive_surface_excess",
            "negative_surface_overcut",
            "surface_uncertainty_exceeds",
        ):
            with self.subTest(name=name):
                row = self.matrix(name)["signed_surface"]
                self.assertEqual(row["evidence_status"], "fail")
                self.assertFalse(row["measured"]["surface_measurement_passed"])
                self.assertEqual(len(row["measured"]["out_of_tolerance"]), 1)
        self.assertEqual(
            self.matrix("negative_surface_overcut")["signed_surface"][
                "measured"
            ]["out_of_tolerance"],
            [{"zone": "protected", "point_id": "p1"}],
        )

    def test_missing_protected_zone_cannot_be_erased_by_manifest_registration(
        self,
    ):
        missing = self.matrix("protection_points_missing")["signed_surface"]
        self.assertEqual(missing["measured"]["missing_zones"], ["protected"])
        erased = self.matrix("protection_registration_missing")[
            "signed_surface"
        ]
        self.assertTrue(erased["measured"]["surface_measurement_passed"])
        self.assertFalse(erased["measured"]["protection_coverage_verified"])
        self.assertEqual(erased["evidence_status"], "fail")

    def test_hash_damage_mixed_part_and_nonfinite_measurements_leave_reasons(
        self,
    ):
        expected = {
            "csv_hash_damage": "哈希",
            "mixed_csv_serial": "同一件",
            "csv_nan": "有效毫米数",
            "json_nan": "非有限数",
        }
        for name, text in expected.items():
            with self.subTest(name=name):
                report = self.report(name)
                self.assertEqual(
                    self.matrix(name)["source_integrity"]["status"], "fail"
                )
                self.assertTrue(
                    any(
                        text in failure["reason"]
                        for failure in report["failures"]
                    )
                )
                json.dumps(report, allow_nan=False)

    def test_localization_uses_independent_3d_points_and_all_target_classes(
        self,
    ):
        matrix = self.matrix()
        location = matrix["localization"]["measured"]
        self.assertGreater(location["maximum_upper_error_mm"], 0.04 + 0.02)
        self.assertLess(location["maximum_upper_error_mm"], 0.1)
        missing = self.matrix("localization_class_missing")["localization"]
        self.assertEqual(
            missing["measured"]["missing_defect_classes"], ["casting_root"]
        )
        self.assertEqual(missing["evidence_status"], "fail")
        self.assertEqual(
            self.matrix("localization_overlimit")["localization"][
                "evidence_status"
            ],
            "fail",
        )

    def test_wall_clock_occupancy_unions_overlap_and_ignores_run_duration(self):
        matrix = self.matrix()
        self.assertEqual(matrix["changeover"]["measured"]["wall_seconds"], 1200)
        self.assertEqual(
            matrix["new_product_occupancy"]["measured"]["wall_seconds"], 5500
        )
        self.assertEqual(
            matrix["simulation_cycle"]["measured"]["run_wall_seconds"], 30
        )
        planned = self.matrix("planned_time_as_wall")
        self.assertEqual(planned["changeover"]["evidence_status"], "fail")
        self.assertEqual(planned["source_integrity"]["status"], "fail")

    def test_control_receipts_require_completed_feedback_and_binding(self):
        for name in (
            "controller_incomplete",
            "controller_plan_hash_damage",
            "controller_feedback_missing",
            "mixed_controller_identity",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.matrix(name)["controller_commissioning"][
                        "evidence_status"
                    ],
                    "fail",
                )
        missing = self.report("controller_feedback_missing")
        self.assertTrue(
            any(
                failure["code"] == "missing_artifact"
                for failure in missing["failures"]
            )
        )
        mismatch = self.report("mixed_controller_identity")
        self.assertTrue(
            any(
                failure["code"] == "mixed_identity"
                for failure in mismatch["failures"]
            )
        )

    def test_preflight_status_records_keep_the_actual_blocking_reason(self):
        report = self.rewrite_role(
            self.copy_case(),
            "run_result",
            lambda value: value.update(
                preflight={
                    "blocked_count": 1,
                    "rows": [
                        {
                            "segment_id": 2,
                            "status": "blocked",
                            "reason": "synthetic_collision_budget_exhausted",
                        }
                    ],
                }
            ),
        )
        cycle = next(
            row for row in report["matrix"] if row["id"] == "simulation_cycle"
        )
        self.assertEqual(
            cycle["measured"]["preflight_rejections"][0]["reason"],
            "synthetic_collision_budget_exhausted",
        )

    def test_virtual_800_hours_are_distinct_from_real_continuous_uptime(self):
        matrix = self.matrix()
        self.assertEqual(matrix["virtual_reliability"]["status"], "pass")
        self.assertEqual(
            matrix["virtual_reliability"]["measured"]["elapsed_hours"], 800
        )
        self.assertEqual(matrix["continuous_reliability"]["status"], "unknown")
        self.assertLess(self.suite["wall_seconds"], 800 * 3600)
        for name in (
            "virtual_fault",
            "virtual_restart",
            "virtual_heartbeat_gap",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.matrix(name)["virtual_reliability"]["evidence_status"],
                    "fail",
                )

    def test_json_duplicates_nonfinite_numbers_and_boolean_counts_fail_closed(
        self,
    ):
        for raw, code in (
            (
                b'{"schema":"x","schema":"y","artifacts":{}}',
                "duplicate_json_key",
            ),
            (
                b'{"schema":"daiyi-enterprise-input-v1","artifacts":{},"n":NaN}',
                "nonfinite_json",
            ),
        ):
            directory = self.copy_case()
            path = directory / "invalid_input.json"
            path.write_bytes(raw)
            with self.assertRaises(EvidenceError) as raised:
                evaluate_evidence(path)
            self.assertEqual(raised.exception.code, code)
        report = self.rewrite_role(
            self.copy_case(),
            "run_result",
            lambda value: value["perception"]["metrics"].update(tp=True),
        )
        self.assertTrue(
            any(
                failure["code"] == "invalid_number"
                for failure in report["failures"]
            )
        )

    def test_missing_uncertainty_and_unknown_calibration_do_not_pass(self):
        no_uncertainty = self.rewrite_role(
            self.copy_case(),
            "localization",
            lambda value: value["samples"][0].pop("uncertainty_mm"),
        )
        self.assertTrue(
            any(
                failure["code"] == "invalid_number"
                for failure in no_uncertainty["failures"]
            )
        )
        uncalibrated = self.rewrite_role(
            self.copy_case(),
            "controller_receipt",
            lambda value: value.pop("tcp_calibrated"),
        )
        row = next(
            row
            for row in uncalibrated["matrix"]
            if row["id"] == "controller_commissioning"
        )
        self.assertEqual(row["evidence_status"], "fail")

    def test_site_interface_rejects_rehearsal_and_preserves_legacy_roles_boundary(
        self,
    ):
        manifest = self.root / "suite/synthetic_normal/site_manifest.json"
        with self.assertRaises(ValueError):
            site_acceptance(manifest)
        directory = self.copy_case()
        path = directory / "site_manifest.json"
        payload = json.loads(path.read_text())
        payload.pop("zone_roles")
        path.write_text(json.dumps(payload))
        report = surface_measurement_report(
            path, allowed_source_kinds={"synthetic", "example"}
        )
        self.assertTrue(report["surface_measurement_passed"])
        self.assertFalse(report["protection_coverage_verified"])

    def test_csv_duplicate_header_missing_column_and_unknown_zone_are_rejected(
        self,
    ):
        for content in (
            "serial,zone,point_id,before_mm,after_mm,uncertainty_mm,after_mm\n"
            "SYNTHETIC-097-A,machined,m1,0.8,0.1,0.02,0.1\n",
            "serial,zone,point_id,before_mm,after_mm\n"
            "SYNTHETIC-097-A,machined,m1,0.8,0.1\n",
            "serial,zone,point_id,before_mm,after_mm,uncertainty_mm\n"
            "SYNTHETIC-097-A,unregistered,m1,0.8,0.1,0.02\n",
        ):
            with self.subTest(content=content):
                directory = self.copy_case()
                csv_path = directory / "surface.csv"
                csv_path.write_text(content)
                path = directory / "site_manifest.json"
                payload = json.loads(path.read_text())
                payload["measurements_sha256"] = artifact_reference(csv_path)[
                    "sha256"
                ]
                path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    surface_measurement_report(
                        path, allowed_source_kinds={"synthetic", "example"}
                    )

    def test_measurement_manifest_is_bound_to_the_bytes_already_checked(self):
        manifest = self.root / "suite/synthetic_normal/site_manifest.json"
        with self.assertRaisesRegex(ValueError, "清单哈希"):
            surface_measurement_report(
                manifest,
                allowed_source_kinds={"synthetic", "example"},
                expected_manifest_sha256="0" * 64,
            )
        report = surface_measurement_report(
            manifest,
            allowed_source_kinds={"synthetic", "example"},
            expected_manifest_sha256=artifact_reference(manifest)["sha256"],
        )
        self.assertEqual(
            report["manifest_sha256"], artifact_reference(manifest)["sha256"]
        )

    def test_historical_no_motion_missing_optional_flags_stays_unknown(self):
        directory = self.copy_case("normal_no_action")
        result = directory / "run_result.json"
        payload = json.loads(result.read_text())
        payload.pop("motion_gate_passed")
        payload.pop("geometric_goal_complete")
        (directory / "result.json").write_text(json.dumps(payload))
        report = evaluate_run(directory)
        rows = {row["id"]: row for row in report["matrix"]}
        self.assertEqual(rows["simulation_cycle"]["status"], "pass")
        self.assertEqual(rows["motion_tracking"]["status"], "unknown")
        # Source hashes are absent in this deliberately small fixture.
        self.assertEqual(rows["source_integrity"]["status"], "fail")


class RequirementsAndCliTests(unittest.TestCase):
    def test_sources_are_pinned_and_original_requirement_status_is_draft(self):
        requirements = load_requirements()
        self.assertEqual(
            requirements["sources"]["requirements_draft"]["status"], "draft"
        )
        self.assertEqual(
            requirements["sources"]["vision_targets"]["status"],
            "vision_targets",
        )
        self.assertFalse(requirements["conventions"]["unknown_is_pass"])

    def test_custom_configuration_cannot_weaken_the_enterprise_targets(self):
        original = json.loads(DEFAULT_REQUIREMENTS.read_text())
        for name, field, value in (
            ("localization", "maximum_error_mm", 1),
            ("signed_surface", "upper_mm", 1),
            ("changeover", "maximum_seconds", 1801),
            ("new_product_occupancy", "maximum_seconds", 7201),
            ("continuous_reliability", "minimum_hours", 1),
        ):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                changed = copy.deepcopy(original)
                row = next(
                    row for row in changed["requirements"] if row["id"] == name
                )
                row["target"][field] = value
                path = Path(temporary) / "requirements.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(EvidenceError):
                    load_requirements(path)

    def test_configuration_cannot_remove_the_enterprise_evidence_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = json.loads(DEFAULT_REQUIREMENTS.read_text())
            for row in payload["requirements"]:
                if row["kind"] == "enterprise_target":
                    row["kind"] = "offline_gate"
            path = Path(temporary) / "requirements.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(EvidenceError, "证据层级"):
                load_requirements(path)

    def test_cli_refuses_to_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / "existing"
            existing.mkdir()
            preserved = existing / "evidence.json"
            preserved.write_text('{"preserved":true}\n')
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "daiyi_grinding.enterprise_acceptance",
                    "--suite-only",
                    "--output",
                    str(existing),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("新目录", result.stderr)
            self.assertEqual(preserved.read_text(), '{"preserved":true}\n')

    def test_import_does_not_load_device_or_learning_dependencies(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import daiyi_grinding.enterprise_acceptance; "
                "assert 'torch' not in sys.modules; "
                "assert 'mujoco' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
