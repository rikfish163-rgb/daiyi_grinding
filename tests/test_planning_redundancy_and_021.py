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
    def test_registered_packages_preserve_metric_identity_and_grasp_frames(self):
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
            [[0, 0, 1, -0.421], [1, 0, 0, -0.090], [0, 1, 0, 0.310], [0, 0, 0, 1]],
        )
        np.testing.assert_allclose(loading, np.diag([-1.0, -1.0, 1.0]))
        with self.assertRaisesRegex(ValueError, "未登记"):
            load_workpiece_package("unregistered_part")


class Workstation021Tests(unittest.TestCase):
    def test_021_model_loads_and_renders_its_declared_cad_and_collision_pieces(self):
        from daiyi_grinding.workstation_model import Workstation

        with tempfile.TemporaryDirectory() as directory:
            sim = Workstation(Path(directory), case="normal", workpiece_id="021")
            try:
                self.assertEqual(sim.workpiece_id, "021")
                self.assertEqual(sim.model.body("workpiece_021").id, sim.workpiece)
                self.assertGreater(sim.model.geom("wp_proxy_0").contype[0], 0)
                image = sim.render(width=160, height=120)
                self.assertEqual(image.shape, (120, 160, 3))
                labels = sim.render(width=160, height=120, mode="segmentation")
                self.assertGreater(
                    np.count_nonzero(labels[..., 0] == sim.model.geom("wp_0").id),
                    0,
                )
            finally:
                sim.close()


class ContactRedundancyTests(unittest.TestCase):
    def test_clocked_grasp_uses_safe_belt_width_to_admit_interrupted_trace(self):
        from daiyi_grinding.project import ROOT
        from daiyi_grinding.workstation_model import Workstation
        from daiyi_grinding.workstation_motion import preflight
        from daiyi_grinding.workstation_planning import build_plan

        source = ROOT / "runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_prepare_01"
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
                    abs(row["tool_contact_world"][1] - sim.tool.contact[1]), 0.025
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
            and 0.029 < np.linalg.norm(pose[1:3, 3] - self.tool.contact[1:]) < 0.08
        ):
            raise ValueError("加工姿态不可达：超过解析工位的局部工作半径")
        return (
            np.r_[pose[:3, 3], Rotation.from_matrix(pose[:3, :3]).as_euler("xyz")],
            0.0,
            0.0,
        )


class ReachabilitySplitTests(unittest.TestCase):
    def test_long_path_retains_only_verified_subsegments_with_controlled_overlap(self):
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
            phases = {command["phase"] for command in commands if command["segment_id"] == row["segment_id"]}
            self.assertTrue({"approach", "trace", "retract"} <= phases)
            self.assertLessEqual(max(np.linalg.norm(point) for point in row["nominal_xyz"]), 0.029)
        self.assertEqual(path["nominal_xyz"], points.tolist())
        self.assertEqual(checks["blocked_count"], 1)


if __name__ == "__main__":
    unittest.main()
