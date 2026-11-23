"""Concrete planning failures, without constructing a full CAD workstation."""

import unittest
from types import SimpleNamespace

import numpy as np

from daiyi_grinding.planning_stress import (
    PlanningFixture,
    aggregate_workpieces,
    fixture_command,
    run_suite,
)
from daiyi_grinding.workstation_motion import (
    bind_commands,
    curve_interval_speed_bound,
    joint_curve,
    preflight,
    transform,
)
from daiyi_grinding.workstation_planning import (
    Commands,
    PlanningBudget,
    PlanningBudgetExceeded,
)
from daiyi_grinding.workstation_transfers import bridge, shorten_transfers


class NarrowObstacleScene:
    """A deterministic seam for collision sampling on the real joint curve."""

    def __init__(self):
        self.data = SimpleNamespace(qpos=np.zeros(6))
        self.q_index = np.arange(6)
        self.lower = np.full(6, -2.0)
        self.upper = np.full(6, 2.0)
        self.scratch = SimpleNamespace(contact=[])
        self.tool = SimpleNamespace(geom="belt")
        self.model = SimpleNamespace(
            geom=lambda index: SimpleNamespace(
                name=("robot_tip", "fixed_obstacle")[index]
            )
        )

    def set_scratch_pose(self, q, attached=False):
        del attached
        self.scratch.contact = (
            [SimpleNamespace(dist=-0.001, geom1=0, geom2=1)]
            if 0.05 <= q[0] <= 0.07
            else []
        )


class PlanningStressTests(unittest.TestCase):
    def test_preflight_rejects_collision_between_old_quarter_samples(self):
        """An intermediate obstacle must reject the whole connecting curve."""
        sim = NarrowObstacleScene()
        command = {
            "t": 1.0,
            "q": np.array([0.4, 0, 0, 0, 0, 0]),
            "phase": "transfer",
            "attached": True,
        }
        checked = preflight(sim, [command])
        self.assertFalse(checked["passed"])
        self.assertGreater(checked["contact_count"], 0)

    def test_same_curve_and_geometry_preserve_legacy_ablation(self):
        sim = NarrowObstacleScene()
        commands = [fixture_command(0.4, phase="transfer")]
        legacy = preflight(sim, commands, sampling_policy="legacy_four")
        current = preflight(sim, commands)
        self.assertTrue(legacy["passed"])
        self.assertFalse(current["passed"])
        self.assertGreater(current["checked_interpolated_samples"], 4)

    def test_preflight_budget_exhaustion_does_not_approve_unchecked_curve(self):
        sim = PlanningFixture()
        result = preflight(sim, [fixture_command(0.4)], sample_budget=4)
        self.assertFalse(result["passed"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["checked_commands"], 0)

    def test_invalid_time_nan_and_joint_limits_are_rejected(self):
        sim = PlanningFixture()
        command = fixture_command(0.03)
        for invalid in (0.0, float("nan")):
            with self.subTest(time=invalid):
                self.assertFalse(
                    preflight(sim, [{**command, "t": invalid}])["passed"]
                )
        command["q"][0] = float("nan")
        self.assertFalse(preflight(sim, [command])["passed"])
        command["q"][0] = 1.5
        result = preflight(sim, [command])
        self.assertFalse(result["passed"])
        self.assertTrue(result["joint_limits_failed"])

    def test_cubic_speed_bound_covers_interior_derivative_peak(self):
        """Endpoint displacement alone cannot bound a cubic's sampling step."""
        sim = PlanningFixture()
        commands = [
            {**fixture_command(q), "t": t}
            for q, t in ((0.2, 0.1), (0.4, 1.0), (0.45, 1.01))
        ]
        times, curve = joint_curve(sim, commands)
        for index, duration in enumerate(np.diff(times)):
            bound = curve_interval_speed_bound(curve, index, duration)
            actual = np.abs(
                curve(np.linspace(times[index], times[index + 1], 1001), 1)
            ).max(axis=0)
            np.testing.assert_array_less(actual - 1e-10, bound + 1e-10)

    def test_ik_call_budget_discards_the_entire_leg(self):
        sim = PlanningFixture()
        budget = PlanningBudget(max_ik_calls=2)
        builder = Commands(sim, budget)
        with self.assertRaises(PlanningBudgetExceeded):
            builder.leg(transform(np.eye(3), [0.024, 0, 0]), "trace")
        self.assertEqual(budget.calls["ik"], 2)
        self.assertFalse(builder.items)
        np.testing.assert_array_equal(builder.q, sim.data.qpos)

    def test_deadline_crossed_inside_ik_cannot_retain_result(self):
        sim = PlanningFixture()
        clock = [0.0]
        original = sim.ik

        def slow_ik(pose, seed):
            result = original(pose, seed)
            clock[0] = 0.02
            return result

        sim.ik = slow_ik
        builder = Commands(sim, PlanningBudget(0.01, now=lambda: clock[0]))
        with self.assertRaises(PlanningBudgetExceeded):
            builder.leg(transform(np.eye(3), [0.008, 0, 0]), "transfer")
        self.assertFalse(builder.items)
        self.assertEqual(builder.rrt_repairs, 0)

    def test_transfer_budget_does_not_fall_back_to_an_unbounded_route(self):
        builder = Commands(PlanningFixture(), PlanningBudget(max_ik_calls=0))
        with self.assertRaises(PlanningBudgetExceeded):
            builder.leg(transform(np.eye(3), [0.008, 0, 0]), "transfer")
        self.assertEqual(builder.budget.calls["ik"], 0)
        self.assertEqual(builder.rrt_repairs, 0)
        self.assertFalse(builder.items)

    def test_different_joint_branch_is_not_a_valid_shortcut(self):
        sim = PlanningFixture()
        start, end = fixture_command(0, phase="retract"), fixture_command(0.03)
        end["q"][5] = 2 * np.pi
        self.assertIsNone(bridge(sim, start, end))

    def test_same_branch_shortcut_keeps_exact_original_endpoint(self):
        sim = PlanningFixture()
        start, end = fixture_command(0, phase="retract"), fixture_command(0.03)
        replacement = bridge(sim, start, end)
        self.assertIsNotNone(replacement)
        np.testing.assert_array_equal(replacement[-1]["q"], end["q"])

    def test_fixture_change_rejects_bound_plan_and_transfers(self):
        sim = PlanningFixture()
        commands = bind_commands(sim, [fixture_command(0.04)])
        checks = {**preflight(sim, commands), "segment_count": 1}
        self.assertTrue(checks["passed"])
        sim.gripper_from_workpiece[0, 3] += 0.001
        after = preflight(sim, commands)
        self.assertFalse(after["passed"])
        self.assertTrue(after["stale_plan"])
        motion, accepted, after = shorten_transfers(sim, commands, [1], checks)
        self.assertFalse(motion)
        self.assertFalse(accepted)
        self.assertFalse(after["passed"])

    def test_in_memory_collision_model_change_invalidates_hash(self):
        sim = PlanningFixture()
        sim.model.geom_size = np.ones((2, 3))
        commands = bind_commands(sim, [fixture_command(0.04)])
        sim.model.geom_size[1, 0] += 0.001
        self.assertTrue(preflight(sim, commands)["stale_plan"])

    def test_complete_segments_are_not_whole_workpiece_successes(self):
        result = aggregate_workpieces(
            [{"segments": [True, False]}, {"segments": [True, True]}]
        )
        self.assertEqual(result["source_segment_count"], 4)
        self.assertEqual(result["complete_source_segment_count"], 3)
        self.assertEqual(result["all_segments_verified_workpiece_count"], 1)
        self.assertEqual(result["physical_workpiece_release_count"], 0)

    def test_stress_suite_retains_both_safe_and_blocked_original_segments(self):
        suite = run_suite(seeds=(2026112321, 2026112327))
        self.assertEqual(suite["guards_passed"], suite["guard_count"])
        current = suite["workpiece_counts"]["bounded_joint"]
        self.assertEqual(current["source_segment_count"], 4)
        self.assertEqual(current["complete_source_segment_count"], 2)
        self.assertEqual(current["all_segments_verified_workpiece_count"], 0)


if __name__ == "__main__":
    unittest.main()
