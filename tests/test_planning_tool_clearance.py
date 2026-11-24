"""Plan the actual tool plane around a shallow nominal CAD protrusion."""

import unittest
from types import SimpleNamespace

import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.planning_stress import PlanningFixture
from daiyi_grinding.workstation_motion import transform
from daiyi_grinding.workstation_planning import build_plan


class NominalToolFixture(PlanningFixture):
    """A linear Cartesian fixture with one nominal surface above the root."""

    def __init__(self, required_gap_m=0.000212):
        super().__init__()
        self.required_gap_m = required_gap_m
        self.lower = np.full(6, -10.0)
        self.upper = -self.lower
        self.tool = SimpleNamespace(
            key="belt",
            geom="abrasive_belt",
            contact=np.array([-0.2, 0.0, 1.1]),
            candidates=lambda: iter([np.array([-0.2, 0.0, 1.1])]),
        )
        self.model = SimpleNamespace(
            geom=lambda index: SimpleNamespace(
                name=("abrasive_belt", "wp_nominal_patch")[index]
            )
        )

    def body_transform(self, body, data=None):
        del body
        q = (self.data if data is None else data).qpos
        return transform(Rotation.from_euler("xyz", q[3:]).as_matrix(), q[:3])

    def ik(self, pose, seed):
        del seed
        return (
            np.r_[
                pose[:3, 3],
                Rotation.from_matrix(pose[:3, :3]).as_euler("xyz"),
            ],
            0.0,
            0.0,
        )

    def set_scratch_pose(self, q, attached=False):
        self.scratch.qpos = np.asarray(q).copy()
        gap = q[0] - self.tool.contact[0]
        at_tool = (
            attached
            and abs(q[1]) < 0.04
            and abs(q[2] - self.tool.contact[2]) < 0.07
            and abs(gap) < 0.001
        )
        self.scratch.contact = (
            [
                SimpleNamespace(
                    dist=gap - self.required_gap_m,
                    geom1=0,
                    geom2=1,
                    frame=np.array([1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]),
                )
            ]
            if at_tool and gap < self.required_gap_m
            else []
        )


def surface_path(allowance_mm):
    return {
        "segment_id": 1,
        "detection_id": 1,
        "kind": "burr",
        "nominal_xyz": [[0, 0, 0], [0, 0.002, 0], [0, 0.004, 0]],
        "normal": [-1.0, 0, 0],
        "material_allowance_mm": allowance_mm,
        "recipe": {"feed_mm_s": 7},
    }


class NominalToolPlanningTests(unittest.TestCase):
    def test_plan_uses_requested_path_allowance_without_changing_root(self):
        path = surface_path(0.24)
        commands, admitted, checks = build_plan(NominalToolFixture(), [path])
        self.assertTrue(checks["passed"])
        self.assertEqual(len(admitted), 1)
        self.assertEqual(checks["rows"][0]["tool_clearance_adjustments"], 0)
        self.assertAlmostEqual(admitted[0]["material_allowance_mm"], 0.24)
        for command in commands:
            if command["phase"] == "trace":
                self.assertAlmostEqual(command["q"][0], -0.2 + 0.00024)
        self.assertEqual(admitted[0]["nominal_xyz"], path["nominal_xyz"])
        self.assertEqual(path["material_allowance_mm"], 0.24)

    def test_shallow_nominal_contact_is_replanned_outward(self):
        path = surface_path(0.08)
        sim = NominalToolFixture()
        commands, admitted, checks = build_plan(sim, [path])
        self.assertTrue(checks["all_paths_passed"])
        row = checks["rows"][0]
        self.assertAlmostEqual(row["requested_material_allowance_mm"], 0.08)
        self.assertAlmostEqual(row["nominal_tool_penetration_mm"], 0.132)
        self.assertGreater(row["tool_clearance_adjustments"], 0)
        self.assertGreaterEqual(row["material_allowance_mm"], 0.212)
        self.assertLess(row["material_allowance_mm"], 0.214)
        for command in commands:
            if command["phase"] == "trace":
                sim.set_scratch_pose(command["q"], True)
                self.assertFalse(sim.scratch.contact)
        self.assertEqual(admitted[0]["nominal_xyz"], path["nominal_xyz"])
        self.assertEqual(path["material_allowance_mm"], 0.08)

    def test_required_separation_beyond_remaining_target_blocks_segment(self):
        commands, admitted, checks = build_plan(
            NominalToolFixture(required_gap_m=0.00045),
            [surface_path(0.08)],
        )
        self.assertFalse(commands)
        self.assertFalse(admitted)
        self.assertFalse(checks["passed"])
        self.assertIn("超过剩余高度目标", checks["rows"][0]["reason"])

    def test_clearance_motion_keeps_the_material_recipe(self):
        path = surface_path(0.08)
        commands, admitted, checks = build_plan(
            NominalToolFixture(), [path], mode="clearance"
        )
        self.assertTrue(checks["passed"])
        self.assertEqual(admitted[0]["material_allowance_mm"], 0.08)
        self.assertEqual(checks["rows"][0]["clearance_mm"], 3.0)
        for command in commands:
            if command["phase"] == "trace":
                self.assertAlmostEqual(command["q"][0], -0.2 + 0.003)


if __name__ == "__main__":
    unittest.main()
