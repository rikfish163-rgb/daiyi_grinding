"""Concrete planning failures, without constructing a full CAD workstation."""

import unittest
from types import SimpleNamespace
from unittest import mock

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

INVALID_SECONDS = (
    float("nan"),
    float("inf"),
    float("-inf"),
    -0.01,
    True,
    False,
    np.bool_(True),
    np.float32("nan"),
    "12",
    1 + 0j,
    [12],
    {"seconds": 12},
)
INVALID_COUNTS = (
    float("nan"),
    float("inf"),
    float("-inf"),
    -1,
    0.0,
    4.0,
    0.5,
    True,
    False,
    np.bool_(False),
    np.float64(4.0),
    np.int64(-1),
    "4",
    4 + 0j,
    [4],
    {"limit": 4},
)


class BudgetValidationTests(unittest.TestCase):
    """Reject malformed limits before clocks, IK or collision work begins."""

    def test_invalid_seconds_are_rejected_before_reading_the_clock(self):
        for value in INVALID_SECONDS:
            with self.subTest(seconds=repr(value)):
                clock = mock.Mock(return_value=0.0)
                with self.assertRaisesRegex(ValueError, "seconds"):
                    PlanningBudget(value, now=clock)
                clock.assert_not_called()

    def test_invalid_call_limits_are_rejected_before_reading_the_clock(self):
        for name in ("max_ik_calls", "max_collision_checks"):
            for value in INVALID_COUNTS:
                with self.subTest(limit=name, value=repr(value)):
                    clock = mock.Mock(return_value=0.0)
                    with self.assertRaisesRegex(ValueError, name):
                        PlanningBudget(**{name: value}, now=clock)
                    clock.assert_not_called()

    def test_default_optional_limits_keep_the_existing_calling_api(self):
        budget = PlanningBudget(now=lambda: 0.0)
        budget.check("ik")
        budget.check("collision")
        self.assertIsNone(budget.seconds)
        self.assertEqual(budget.limits, {"ik": None, "collision": None})
        self.assertEqual(budget.calls, {"ik": 1, "collision": 1})
        self.assertFalse(budget.exhausted)

    def test_finite_positional_seconds_stop_at_the_original_deadline(self):
        clock = [0.0]
        budget = PlanningBudget(12, now=lambda: clock[0])
        clock[0] = 11.999
        budget.check("ik")
        clock[0] = 12.0
        with self.assertRaises(PlanningBudgetExceeded):
            budget.check("collision")
        self.assertEqual(budget.calls, {"ik": 1, "collision": 0})
        self.assertTrue(budget.exhausted)

    def test_zero_seconds_and_call_limits_block_the_first_operation(self):
        for options, operation in (
            ({"seconds": 0}, "ik"),
            ({"seconds": 0.0}, "collision"),
            ({"max_ik_calls": 0}, "ik"),
            ({"max_collision_checks": 0}, "collision"),
        ):
            with self.subTest(options=options):
                budget = PlanningBudget(**options, now=lambda: 0.0)
                with self.assertRaises(PlanningBudgetExceeded):
                    budget.check(operation)
                self.assertEqual(budget.calls, {"ik": 0, "collision": 0})

    def test_integer_call_limits_allow_exactly_the_declared_calls(self):
        for name, operation in (
            ("max_ik_calls", "ik"),
            ("max_collision_checks", "collision"),
        ):
            for value in (2, np.int64(2)):
                with self.subTest(limit=name, value=repr(value)):
                    budget = PlanningBudget(**{name: value}, now=lambda: 0.0)
                    budget.check(operation)
                    budget.check(operation)
                    with self.assertRaises(PlanningBudgetExceeded):
                        budget.check(operation)
                    self.assertEqual(budget.calls[operation], 2)

    def test_numpy_real_seconds_remain_valid(self):
        clock = [0.0]
        for seconds in (np.float32(0.5), np.float64(0.5)):
            with self.subTest(seconds=repr(seconds)):
                clock[0] = 0.0
                budget = PlanningBudget(seconds, now=lambda: clock[0])
                clock[0] = 0.25
                budget.check("ik")
                clock[0] = 0.5
                with self.assertRaises(PlanningBudgetExceeded):
                    budget.check()

    def test_invalid_preflight_budget_is_rejected_before_curve_or_contacts(
        self,
    ):
        sim = PlanningFixture()
        commands = [fixture_command(0.4)]
        for value in (*INVALID_COUNTS, None):
            with self.subTest(sample_budget=repr(value)):
                with (
                    mock.patch(
                        "daiyi_grinding.workstation_motion.joint_curve"
                    ) as curve,
                    mock.patch.object(sim, "set_scratch_pose") as contact_pose,
                ):
                    with self.assertRaisesRegex(ValueError, "sample_budget"):
                        preflight(sim, commands, sample_budget=value)
                    curve.assert_not_called()
                    contact_pose.assert_not_called()

    def test_empty_commands_do_not_hide_an_invalid_sample_budget(self):
        for value in (*INVALID_COUNTS, None):
            with self.subTest(sample_budget=repr(value)):
                with self.assertRaisesRegex(ValueError, "sample_budget"):
                    preflight(None, [], sample_budget=value)

    def test_nonnegative_sample_counts_keep_the_existing_sampling_boundary(
        self,
    ):
        commands = [fixture_command(0.4)]
        default = preflight(PlanningFixture(), commands)
        self.assertTrue(default["passed"])
        self.assertEqual(default["checked_interpolated_samples"], 35)
        self.assertEqual(default["joint_step_bound_rad"], 0.012)
        for value in (0, 34):
            with self.subTest(sample_budget=value):
                blocked = preflight(
                    PlanningFixture(), commands, sample_budget=value
                )
                self.assertFalse(blocked["passed"])
                self.assertTrue(blocked["budget_exhausted"])
                self.assertEqual(blocked["checked_commands"], 0)
        for value in (35, np.int64(35)):
            with self.subTest(sample_budget=repr(value)):
                checked = preflight(
                    PlanningFixture(), commands, sample_budget=value
                )
                self.assertTrue(checked["passed"])
                self.assertEqual(checked["checked_interpolated_samples"], 35)

    def test_large_integer_limits_are_not_coerced_into_nonfinite_floats(self):
        limit = 10**400
        budget = PlanningBudget(
            limit,
            max_ik_calls=limit,
            max_collision_checks=limit,
            now=lambda: 0.0,
        )
        budget.check("ik")
        self.assertEqual(budget.seconds, limit)
        self.assertEqual(budget.calls["ik"], 1)
        checked = preflight(
            PlanningFixture(), [fixture_command(0.4)], sample_budget=limit
        )
        self.assertTrue(checked["passed"])


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
