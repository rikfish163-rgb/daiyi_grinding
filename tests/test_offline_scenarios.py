"""Scenario schema, source split and current CAD changeover boundaries."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from daiyi_grinding import offline_scenarios
from daiyi_grinding.casting_defects import generate_casting_defects
from daiyi_grinding.casting_profiles import load_profile, sensor_for_profile


class OfflineScenarioPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configuration = offline_scenarios.load_config()

    def test_small_suite_has_explicit_metadata_and_fresh_frozen_splits(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            development = offline_scenarios.build_plan(
                self.configuration, output
            )
            evaluation = offline_scenarios.build_plan(
                self.configuration, output, "scenario_evaluation"
            )
            self.assertEqual(len(development), 10)
            self.assertEqual(len(evaluation), 10)
            for row in development:
                missing = set(offline_scenarios.SCENARIO_FIELDS) - set(row)
                self.assertFalse(missing)
                self.assertEqual(row["part_id"], "097")
                self.assertEqual(row["cad_id"], "097")
                self.assertEqual(row["planned_view_count"], 6)
            self.assertEqual(
                {row["entity_group_id"] for row in development},
                {"synthetic097_seed97101"},
            )
            self.assertEqual(
                {row["entity_group_id"] for row in evaluation},
                {"synthetic097_seed97111"},
            )
            self.assertTrue(
                all(row["split"] == "development" for row in development)
            )
            self.assertTrue(
                all(row["split"] == "scenario_evaluation" for row in evaluation)
            )

    def test_same_entity_seed_cannot_cross_split(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = offline_scenarios.build_plan(
                self.configuration, Path(directory)
            )
            rows[1]["split"] = "scenario_evaluation"
            with self.assertRaisesRegex(ValueError, "Same entity"):
                offline_scenarios.validate_rows(self.configuration, rows)

    def test_training_validation_test_and_known_regression_seeds_are_rejected(
        self,
    ):
        for seed in (9301, 9501, 9601, 9903):
            with self.subTest(seed=seed):
                configuration = copy.deepcopy(self.configuration)
                configuration["seed_registry"]["development"] = [seed]
                with self.assertRaisesRegex(ValueError, "seeds|seed"):
                    offline_scenarios.validate_seed_registry(configuration)

    def test_external_split_plan_is_not_just_a_static_exclusion_list(self):
        with tempfile.TemporaryDirectory() as directory:
            split_plan = Path(directory) / "split_plan.json"
            split_plan.write_text(json.dumps({"training_seeds": [97101]}))
            configuration = copy.deepcopy(self.configuration)
            configuration["seed_registry"]["historical_split_plans"] = [
                str(split_plan)
            ]
            original = offline_scenarios.repository_path

            def resolve(path):
                return (
                    Path(path) if Path(path).is_absolute() else original(path)
                )

            with mock.patch.object(
                offline_scenarios, "repository_path", side_effect=resolve
            ):
                with self.assertRaisesRegex(ValueError, "reuses"):
                    offline_scenarios.validate_seed_registry(configuration)

    def test_full_matrix_is_large_and_dry_plan_does_not_load_mujoco(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan"
            with mock.patch("mujoco.MjModel.from_xml_path") as model:
                summary = offline_scenarios.run(
                    self.configuration,
                    output,
                    matrix="full",
                    dry_plan=True,
                )
            model.assert_not_called()
            self.assertTrue(summary["passed"])
            self.assertEqual(summary["metrics"]["generated_frames"], 0)
            self.assertEqual(
                summary["metrics"]["full_matrix_dry_plan_rows"], 240
            )
            self.assertEqual(summary["validation_failures"], [])
            for entry in summary["inputs"] + summary["artifacts"]:
                self.assertEqual(
                    offline_scenarios.sha256(entry["path"]), entry["sha256"]
                )
            manifest = json.loads(
                (output / "scenario_manifest.json").read_text()
            )
            self.assertEqual(len(manifest["scenarios"]), 240)
            self.assertEqual(summary["resource"]["mujoco_processes"], 0)
            self.assertFalse(summary["scope"]["training_executed"])
            self.assertEqual(
                summary["metrics"]["split_entity_counts"]["development"][
                    "entity_groups"
                ],
                3,
            )

    def test_full_matrix_cannot_start_implicit_large_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "dry-plan"):
                offline_scenarios.run(
                    self.configuration,
                    Path(directory) / "forbidden",
                    matrix="full",
                )

    def test_explicit_sensor_stress_survives_team_prior_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            rows = offline_scenarios.build_plan(self.configuration, output)
            stresses = [row for row in rows if "sensor_override" in row]
            self.assertEqual(len(stresses), 2)
            for row in stresses:
                with self.subTest(scenario=row["scenario_id"]):
                    settings = offline_scenarios._settings_for_row(row, output)
                    effective = sensor_for_profile(settings, row["entity_seed"])
                    self.assertEqual(
                        {key: effective[key] for key in row["sensor_override"]},
                        row["sensor_override"],
                    )


class OfflineProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configuration = offline_scenarios.load_config()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.profiles = offline_scenarios.materialize_profiles(
            cls.configuration, Path(cls.temporary.name)
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_override_hashes_and_source_train_only_small_burrs(self):
        for name, record in self.profiles.items():
            with self.subTest(profile=name):
                self.assertEqual(
                    offline_scenarios.sha256(record["path"]),
                    self.configuration["profile_overrides"][name]["sha256"],
                )
        settings = self.profiles["burr_small"]["settings"]
        rows = settings["compact_excess_prior"]["training_instances"]
        self.assertEqual(len(rows), 150)
        self.assertTrue(
            all(
                row["optimizer_split"] == "train"
                and row["semantic_code"] == 602
                and row["height_mm"] <= 1.0
                and max(row["radii_uv_mm"]) <= 2.3
                and min(row["radii_uv_mm"]) <= 1.75
                for row in rows
            )
        )
        load_profile(settings)

    def test_thin_thick_and_small_geometry_uses_existing_rooted_generator(self):
        cases = (
            ("flash_thin", "casting_flash", 0.35, 0.85),
            ("flash_thick", "casting_flash", 4.8, 5.8),
            ("interrupted_low", "casting_interrupted", 0.35, 0.9),
            ("burr_small", "casting_burr", 0.0, 1.0),
        )
        for name, case, lower, upper in cases:
            with self.subTest(profile=name):
                items = generate_casting_defects(
                    offline_scenarios.repository_path(
                        self.configuration["parts"]["097"]["cad_path"]
                    ),
                    97101,
                    case,
                    self.profiles[name]["settings"],
                )
                self.assertTrue(items)
                for mesh, item in items:
                    self.assertTrue(mesh.is_watertight)
                    self.assertGreater(mesh.volume, 0)
                    self.assertGreaterEqual(item["height_peak_mm"], lower)
                    self.assertLessEqual(item["height_peak_mm"], upper)
                    self.assertTrue(item["geometry_validation"]["watertight"])
                    self.assertGreater(
                        item["geometry_validation"][
                            "connected_rooted_fragments"
                        ],
                        0,
                    )
                    if source := item["parameters"].get(
                        "compact_prior_instance"
                    ):
                        self.assertEqual(source["optimizer_split"], "train")


class ChangeoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configuration = offline_scenarios.load_config()
        cls.parts = offline_scenarios.validate_parts(cls.configuration)

    def test_station_boundary_tracks_simulation_packages_without_physical_claim(
        self,
    ):
        boundary = offline_scenarios.inspect_station_boundary()
        self.assertEqual(boundary["supported_part_cad"], ["021", "097"])
        self.assertEqual(boundary["physical_changeover_certified"], [])
        self.assertTrue(all(boundary["checks"].values()))

    def test_021_and_cao_train_are_rejected_without_renaming(self):
        rehearsal = offline_scenarios.rehearse_changeovers(self.configuration)
        self.assertEqual(len(rehearsal["cases"]), 2)
        for case in rehearsal["cases"]:
            self.assertTrue(case["expected_rejection"])
            self.assertFalse(case["import_result"]["accepted"])
            self.assertFalse(case["renamed_to_097"])
            self.assertFalse(case["new_holdout_claimed"])
            self.assertFalse(case["import_template_result"]["accepted"])
            self.assertEqual(rehearsal["physical_machine_time"], "pending")

    def test_missing_changeover_fields_are_not_autofilled(self):
        result = offline_scenarios.import_changeover({"part_id": "new"})
        self.assertFalse(result["schema_valid"])
        self.assertFalse(result["accepted"])
        self.assertIn("T_workpiece_gripper_m", result["missing_fields"])

    def test_complete_metadata_still_does_not_fake_main_station_support(self):
        part = self.parts["021"]
        manifest = offline_scenarios.changeover_manifest_template(part)
        manifest.update(
            cad_path=str(offline_scenarios.repository_path(part["cad_path"])),
            coordinate_frame="enterprise_datum_pending_confirmation",
            T_workpiece_gripper_m=np.eye(4).tolist(),
        )
        result = offline_scenarios.import_changeover(manifest)
        self.assertTrue(result["schema_valid"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["stage"], "main_station_capability")
        self.assertFalse(result["main_station_executed"])

    def test_public_train_io_is_read_but_not_relabelled_holdout_or_097(self):
        result = offline_scenarios.inspect_cao_public_input(self.configuration)
        self.assertEqual(result["source_split"], "train")
        self.assertFalse(result["source_is_new_holdout"])
        self.assertFalse(result["nominal_mesh_available_in_release"])
        self.assertFalse(result["main_station_changeover_accepted"])
        self.assertEqual(result["readable_public_io"]["trajectory_rows"], 1024)
        self.assertLess(
            result["readable_public_io"]["backprojection_numeric_max_mm"],
            0.01,
        )
        self.assertLess(
            result["readable_public_io"]["world_frame_numeric_max_mm"],
            0.01,
        )
        self.assertFalse(result["trajectory_sent_to_robot"])


if __name__ == "__main__":
    unittest.main()
