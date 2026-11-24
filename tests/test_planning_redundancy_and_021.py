"""Workpiece packages and collision-gated contact, split and regrasp planning."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.planning_stress import PlanningFixture
from daiyi_grinding.workstation_motion import transform


class WorkpiecePackageTests(unittest.TestCase):
    def test_invalid_cad_units_and_nonrigid_grasps_are_rejected_at_loading_boundary(
        self,
    ):
        from daiyi_grinding.project import ROOT
        from daiyi_grinding.workpiece_packages import load_workpiece_package

        declaration = json.loads(
            (ROOT / "config/workpieces/wp_097.json").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, field in enumerate(
                ("cad", "units", "rigid", "missing", "margin")
            ):
                row = json.loads(json.dumps(declaration))
                if field == "cad":
                    row["cad_path"] = (
                        "assets/ier220_workstation/meshes/workpiece/wp_021_c0.stl"
                    )
                elif field == "units":
                    row["units"] = "mm"
                elif field == "rigid":
                    row["grasp_setups"][0]["gripper_from_workpiece"][0][2] = 2
                elif field == "margin":
                    row["contact_search"]["edge_margin_mm"] = 50
                else:
                    row["cad_path"] = (
                        "assets/ier220_workstation/meshes/workpiece/not_registered.stl"
                    )
                path = Path(directory) / f"invalid_{index}.json"
                path.write_text(json.dumps(row))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_workpiece_package("097", package_path=path)

    def test_registered_packages_preserve_metric_identity_and_grasp_frames(
        self,
    ):
        from daiyi_grinding.workpiece_packages import load_workpiece_package

        original = load_workpiece_package("097")
        alternate = load_workpiece_package("021")
        self.assertEqual(original.coordinate_frame, "workpiece_097")
        self.assertEqual(alternate.coordinate_frame, "workpiece_021")
        self.assertEqual(alternate.mesh_path.name, "wp_021_c0.stl")
        self.assertEqual(alternate.source_scene.name, "scene_workpiece_021.xml")
        grip, loading = original.grasp_frames("original")
        np.testing.assert_allclose(
            grip,
            [
                [0, 0, 1, -0.421],
                [1, 0, 0, -0.090],
                [0, 1, 0, 0.310],
                [0, 0, 0, 1],
            ],
        )
        np.testing.assert_allclose(loading, np.diag([-1.0, -1.0, 1.0]))
        with self.assertRaisesRegex(ValueError, "未登记"):
            load_workpiece_package("unregistered_part")


class Workstation021Tests(unittest.TestCase):
    def test_021_nominal_cad_path_passes_real_robot_and_continuous_collision_gates(
        self,
    ):
        from daiyi_grinding.workstation_model import Workstation
        from daiyi_grinding.workstation_motion import preflight
        from daiyi_grinding.workstation_planning import build_plan

        path = {
            "segment_id": 1,
            "detection_id": 1,
            "kind": "burr",
            "coordinate_frame": "workpiece_021",
            "nominal_xyz": [
                [-0.205, 0.089, 0.361],
                [-0.204, 0.089, 0.361],
                [-0.203, 0.089, 0.361],
            ],
            "normal": [0, 0, 1],
            "recipe": {"feed_mm_s": 7},
        }
        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(directory, case="normal", workpiece_id="021")
            try:
                commands, admitted, checks = build_plan(sim, [path])
                self.assertTrue(admitted, checks)
                self.assertTrue(checks["all_paths_passed"], checks)
                self.assertTrue(preflight(sim, commands)["passed"])
                self.assertEqual(checks["contact_count"], 0)
                self.assertEqual(
                    admitted[0]["coordinate_frame"], "workpiece_021"
                )
            finally:
                sim.close()

    def test_021_model_loads_and_renders_its_declared_cad_and_collision_pieces(
        self,
    ):
        from daiyi_grinding.workstation_model import Workstation

        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(
                Path(directory), case="normal", workpiece_id="021"
            )
            try:
                self.assertEqual(sim.workpiece_id, "021")
                self.assertEqual(
                    sim.model.body("workpiece_021").id, sim.workpiece
                )
                self.assertGreater(sim.model.geom("wp_proxy_0").contype[0], 0)
                image = sim.render(width=160, height=120)
                self.assertEqual(image.shape, (120, 160, 3))
                labels = sim.render(width=160, height=120, mode="segmentation")
                self.assertGreater(
                    np.count_nonzero(
                        labels[..., 0] == sim.model.geom("wp_0").id
                    ),
                    0,
                )
            finally:
                sim.close()


class ContactRedundancyTests(unittest.TestCase):
    def test_clocked_grasp_uses_safe_belt_width_to_admit_interrupted_trace(
        self,
    ):
        from daiyi_grinding.project import ROOT
        from daiyi_grinding.workstation_model import Workstation
        from daiyi_grinding.workstation_motion import preflight
        from daiyi_grinding.workstation_planning import build_plan

        source = (
            ROOT
            / "runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_prepare_01"
        )
        paths = json.loads((source / "paths.json").read_text())
        profile = json.loads((source / "profile.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(
                directory,
                seed=97101,
                case="casting_interrupted",
                casting_profile=profile,
                grasp_setup="clock_90_simulation",
            )
            try:
                commands, admitted, checks = build_plan(sim, paths)
                self.assertTrue(admitted, checks)
                self.assertTrue(checks["all_paths_passed"], checks)
                self.assertTrue(preflight(sim, commands)["passed"])
                row = checks["rows"][0]
                self.assertGreater(
                    abs(row["tool_contact_world"][1] - sim.tool.contact[1]),
                    0.025,
                )
                self.assertEqual(row["grasp_setup"], "clock_90_simulation")
                self.assertEqual(checks["contact_count"], 0)
            finally:
                sim.close()


class ReachLimitedCell(PlanningFixture):
    """An analytical robot boundary with a finite reachable process radius."""

    def __init__(self):
        super().__init__()
        self.lower = np.full(6, -10.0)
        self.upper = -self.lower
        self.tool = SimpleNamespace(
            key="belt",
            geom="belt",
            contact=np.array([-0.2, 0.0, 1.1]),
            candidates=lambda: iter([np.array([-0.2, 0.0, 1.1])]),
        )

    def body_transform(self, body, data=None):
        del body
        q = (self.data if data is None else data).qpos
        return transform(Rotation.from_euler("xyz", q[3:]).as_matrix(), q[:3])

    def ik(self, pose, seed):
        del seed
        if (
            abs(pose[0, 3] - self.tool.contact[0]) < 0.004
            and 0.029
            < np.linalg.norm(pose[1:3, 3] - self.tool.contact[1:])
            < 0.08
        ):
            raise ValueError("加工姿态不可达：超过解析工位的局部工作半径")
        return (
            np.r_[
                pose[:3, 3], Rotation.from_matrix(pose[:3, :3]).as_euler("xyz")
            ],
            0.0,
            0.0,
        )


class ReachabilitySplitTests(unittest.TestCase):
    def test_a_permission_gap_is_not_bridged_or_split_into_an_unchecked_trace(
        self,
    ):
        from daiyi_grinding.workstation_planning import build_plan

        path = {
            "segment_id": 1,
            "detection_id": 1,
            "kind": "seam",
            "nominal_xyz": [
                [0, 0, 0],
                [0, 0.002, 0],
                [0, 0.020, 0],
                [0, 0.022, 0],
            ],
            "normal": [-1, 0, 0],
            "recipe": {"feed_mm_s": 10},
        }
        commands, admitted, checks = build_plan(ReachLimitedCell(), [path])
        self.assertFalse(commands)
        self.assertFalse(admitted)
        self.assertFalse(checks["passed"])
        self.assertIn("许可支持缺口", checks["rows"][0]["reason"])
        self.assertEqual(checks["source_coverage_ratio"], 0.0)

    def test_long_path_retains_only_verified_subsegments_with_controlled_overlap(
        self,
    ):
        from daiyi_grinding.workstation_planning import build_plan

        points = np.c_[np.zeros(21), np.linspace(0, 0.04, 21), np.zeros(21)]
        path = {
            "segment_id": 7,
            "detection_id": 1,
            "kind": "seam",
            "nominal_xyz": points.tolist(),
            "xyz": points.tolist(),
            "normal": [-1.0, 0, 0],
            "recipe": {"feed_mm_s": 10},
        }
        commands, admitted, checks = build_plan(ReachLimitedCell(), [path])
        self.assertGreaterEqual(len(admitted), 2, checks)
        self.assertTrue(checks["passed"], checks)
        self.assertFalse(checks["all_paths_passed"])
        self.assertGreater(checks["source_coverage_ratio"], 0)
        self.assertLess(checks["source_coverage_ratio"], 1)
        intervals = sorted(row["source_interval_mm"] for row in admitted)
        self.assertAlmostEqual(intervals[0][1] - intervals[1][0], 3.0)
        self.assertTrue(all(row["split_from"] == 7 for row in admitted))
        for row in admitted:
            self.assertGreater(row["coverage_ratio"], 0)
            self.assertLess(row["coverage_ratio"], 1)
            phases = {
                command["phase"]
                for command in commands
                if command["segment_id"] == row["segment_id"]
            }
            self.assertTrue({"approach", "trace", "retract"} <= phases)
            self.assertLessEqual(
                max(np.linalg.norm(point) for point in row["nominal_xyz"]),
                0.029,
            )
        self.assertEqual(path["nominal_xyz"], points.tolist())
        self.assertEqual(checks["blocked_count"], 1)
        self.assertEqual(checks["rows"][0]["status"], "partial")
        self.assertEqual(len(checks["admitted_rows"]), len(admitted))
        for row in checks["admitted_rows"]:
            self.assertEqual(row["status"], "passed")
            self.assertEqual(row["source_segment_id"], 7)
            self.assertEqual(row["split_from"], 7)
            self.assertEqual(row["coverage"], 1.0)
            self.assertEqual(
                row["command_count"],
                sum(
                    command["segment_id"] == row["segment_id"]
                    for command in commands
                ),
            )
            self.assertRegex(row["command_sha256"], "^[0-9a-f]{64}$")


class MultiGraspPlanningTests(unittest.TestCase):
    def test_fixture_regrasp_requires_integrated_clocking_and_never_teleports_state(
        self,
    ):
        from daiyi_grinding.workstation_inspection import Inspection
        from daiyi_grinding.workstation_model import Workstation

        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(directory, case="normal")
            try:
                initial = sim.data.qpos.copy()
                with self.assertRaises(ValueError):
                    sim.activate_grasp_setup("clock_90_simulation")
                np.testing.assert_array_equal(sim.data.qpos, initial)
                Inspection(sim, 7007).rotate(90)
                settled = sim.data.qpos.copy()
                pose = sim.body_transform(sim.workpiece).copy()
                result = sim.activate_grasp_setup("clock_90_simulation")
                self.assertTrue(result["checkpoint_verified"])
                self.assertFalse(result["workpiece_pose_teleported"])
                self.assertFalse(result["joint_positions_teleported"])
                self.assertEqual(sim.grasp_setup, "clock_90_simulation")
                np.testing.assert_array_equal(sim.data.qpos, settled)
                np.testing.assert_allclose(
                    sim.body_transform(sim.workpiece), pose, atol=1e-12
                )
            finally:
                sim.close()

    def test_automatic_grasp_fallback_emits_verified_operations_and_fixture_checkpoint(
        self,
    ):
        from daiyi_grinding.project import ROOT
        from daiyi_grinding.workstation_model import Workstation
        from daiyi_grinding.workstation_planning import build_plan

        source = (
            ROOT
            / "runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_prepare_01"
        )
        paths = json.loads((source / "paths.json").read_text())
        profile = json.loads((source / "profile.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(
                directory,
                seed=97101,
                case="casting_interrupted",
                casting_profile=profile,
            )
            try:
                before = sim.data.qpos.copy()
                operations, admitted, checks = build_plan(
                    sim, paths, grasp_setups=("original", "clock_90_simulation")
                )
                self.assertTrue(admitted, checks)
                self.assertTrue(checks["passed"], checks)
                self.assertEqual(checks["segment_count"], 1)
                self.assertEqual(checks["source_coverage_ratio"], 1.0)
                self.assertTrue(checks["regrasp_checkpoints"])
                checkpoint = checks["regrasp_checkpoints"][0]
                self.assertEqual(checkpoint["from_grasp_setup"], "original")
                self.assertEqual(
                    checkpoint["to_grasp_setup"], "clock_90_simulation"
                )
                self.assertFalse(checkpoint["transition_executed"])
                self.assertEqual(
                    operations[0]["grasp_setup"], "clock_90_simulation"
                )
                for operation in operations:
                    self.assertTrue(operation["preflight"]["passed"])
                    self.assertEqual(operation["preflight"]["contact_count"], 0)
                    self.assertTrue(
                        all(
                            command["grasp_setup"] == operation["grasp_setup"]
                            for command in operation["commands"]
                        )
                    )
                np.testing.assert_array_equal(sim.data.qpos, before)
                self.assertEqual(sim.grasp_setup, "original")
            finally:
                sim.close()


if __name__ == "__main__":
    unittest.main()
