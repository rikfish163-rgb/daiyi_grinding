"""Bounded alternative approaches retain CAD permission and full admission."""

import copy
import unittest
from dataclasses import replace

import numpy as np

from daiyi_grinding.planning_alternatives import (
    PlanningRejectionSummary,
    evaluate_planning_alternatives,
    generate_approach_candidates,
)
from daiyi_grinding.workpiece_packages import load_workpiece_package
from daiyi_grinding.workstation_tooling import fixed_tool


def permitted_path(segment_id=7):
    return {
        "segment_id": segment_id,
        "detection_id": 1,
        "kind": "burr",
        "coordinate_frame": "workpiece_097",
        "nominal_xyz": np.c_[
            np.linspace(0.02, 0.04, 11),
            np.full(11, 0.12),
            np.full(11, 0.28),
        ].tolist(),
        "normal": [0.0, 1.0, 0.0],
        "material_allowance_mm": 0.08,
        "recipe": {"tool": "belt", "feed_mm_s": 7},
    }


def accepted_plan(paths, clearance_mm=5.0, duration_s=12.0):
    return (
        [{"t": duration_s, "q": np.zeros(6), "phase": "trace"}],
        paths,
        {
            "passed": True,
            "all_paths_passed": True,
            "source_coverage_ratio": 1.0,
            "contact_count": 0,
            "unexpected_contacts": [],
            "duration_s": duration_s,
            "rows": [
                {"segment_id": path["segment_id"], "status": "passed"}
                for path in paths
            ],
            "planning_alternative_geometry": {
                "minimum_clamp_belt_clearance_mm": clearance_mm,
                "best_orientation_minimum_clamp_belt_clearance_mm": clearance_mm,
                "scope": "sampled_nominal_process_poses",
            },
        },
    )


def rejected_plan(paths, reason, *, clearance_mm=None, exhausted=False):
    return (
        [],
        [],
        {
            "passed": False,
            "all_paths_passed": False,
            "source_coverage_ratio": 0.0,
            "rows": [
                {
                    "segment_id": path["segment_id"],
                    "status": "blocked",
                    "reason": reason,
                    "budget": {"exhausted": exhausted},
                }
                for path in paths
            ],
            "planning_alternative_geometry": {
                "minimum_clamp_belt_clearance_mm": clearance_mm,
                "best_orientation_minimum_clamp_belt_clearance_mm": clearance_mm,
                "scope": "sampled_nominal_process_poses",
            },
        },
    )


class ApproachCandidateTests(unittest.TestCase):
    def test_a_bounded_prefix_reaches_both_tilt_signs_and_distinct_rolls(self):
        candidates = generate_approach_candidates(
            [permitted_path()], max_candidates=128
        )
        self.assertEqual(len(candidates), 128)
        self.assertEqual(
            {candidate.tilt_deg for candidate in candidates},
            {0.0, 1.0, -1.0, 2.0, -2.0},
        )
        self.assertGreaterEqual(
            len({candidate.roll_deg for candidate in candidates}), 5
        )

    def test_yaw_pitch_groups_are_bounded_without_changing_cad_normals(self):
        path = permitted_path()
        original = copy.deepcopy(path)
        tool = fixed_tool()
        search = load_workpiece_package().contact_search
        candidates = generate_approach_candidates(
            [path], tool=tool, search=search, max_candidates=1000
        )
        self.assertTrue(
            any(candidate.tilt_deg != 0 for candidate in candidates)
        )
        self.assertTrue(
            any(candidate.belt_side == -1 for candidate in candidates)
        )
        self.assertTrue(
            any(candidate.belt_side == 1 for candidate in candidates)
        )
        for candidate in candidates:
            self.assertLessEqual(abs(candidate.tilt_deg), 2.0)
            self.assertEqual(candidate.paths[0]["normal"], path["normal"])
            self.assertEqual(
                candidate.paths[0]["nominal_xyz"], path["nominal_xyz"]
            )
            self.assertEqual(candidate.paths[0]["recipe"], path["recipe"])
            self.assertEqual(candidate.paths[0]["material_allowance_mm"], 0.08)
            for rotation in candidate.rotations(path["normal"]):
                np.testing.assert_allclose(
                    rotation.T @ rotation, np.eye(3), atol=1e-12
                )
                self.assertAlmostEqual(np.linalg.det(rotation), 1.0)
                normal_world = rotation @ np.asarray(path["normal"])
                deviation = np.rad2deg(
                    np.arctan2(
                        np.linalg.norm(np.cross(normal_world, tool.normal)),
                        normal_world @ tool.normal,
                    )
                )
                self.assertLessEqual(deviation, 2.0 + 1e-9)
                self.assertAlmostEqual(
                    deviation, abs(candidate.tilt_deg), places=8
                )
                self.assertGreater(normal_world @ tool.normal, 0.99)
        self.assertEqual(path, original)

    def test_contacts_reflect_inside_the_existing_front_face_edge_margin(self):
        tool = fixed_tool()
        search = load_workpiece_package().contact_search
        candidates = generate_approach_candidates(
            [permitted_path()],
            tool=tool,
            search=search,
            roll_candidates_deg=(45,),
            tilt_candidates_deg=(0,),
        )
        contacts = np.asarray(
            [candidate.contact_world_m for candidate in candidates]
        )
        np.testing.assert_array_equal(
            contacts[:, 0], np.full(len(contacts), tool.contact[0])
        )
        half = tool.half_size[1:] - search.edge_margin_mm / 1000
        self.assertTrue(
            np.all(np.abs(contacts[:, 1:] - tool.contact[1:]) <= half + 1e-12)
        )
        for contact in contacts:
            reflected = contact.copy()
            reflected[1] = 2 * tool.contact[1] - contact[1]
            self.assertTrue(
                np.any(np.all(np.isclose(contacts, reflected), axis=1))
            )

    def test_splits_cover_all_source_intervals_without_joining_sources(self):
        first = permitted_path()
        second = permitted_path(19)
        second["nominal_xyz"] = (
            np.asarray(second["nominal_xyz"]) + [0.05, 0, 0]
        ).tolist()
        original = copy.deepcopy([first, second])
        candidates = generate_approach_candidates(
            [first, second],
            roll_candidates_deg=(45,),
            tilt_candidates_deg=(0,),
            split_points_mm={7: (6.0, 12.0), 19: (10.0,)},
        )
        split = next(
            candidate for candidate in candidates if len(candidate.paths) > 2
        )
        self.assertEqual(len(split.paths), 5)
        self.assertEqual(len({path["segment_id"] for path in split.paths}), 5)
        for source in original:
            parts = [
                path
                for path in split.paths
                if path["source_segment_id"] == source["segment_id"]
            ]
            intervals = sorted(path["source_interval_mm"] for path in parts)
            self.assertAlmostEqual(intervals[0][0], 0.0)
            self.assertAlmostEqual(intervals[-1][1], 20.0)
            for previous, following in zip(intervals[:-1], intervals[1:]):
                self.assertAlmostEqual(previous[1] - following[0], 3.0)
            for part in parts:
                self.assertEqual(part["normal"], source["normal"])
                self.assertEqual(part["recipe"], source["recipe"])
                self.assertGreaterEqual(
                    part["source_interval_mm"][1]
                    - part["source_interval_mm"][0],
                    4.0,
                )
                points = np.asarray(part["nominal_xyz"])
                self.assertTrue(
                    np.all(points[:, 0] >= source["nominal_xyz"][0][0])
                )
                self.assertTrue(
                    np.all(points[:, 0] <= source["nominal_xyz"][-1][0])
                )
                self.assertTrue(
                    np.all(
                        np.linalg.norm(np.diff(points, axis=0), axis=1)
                        <= 0.005 + 1e-12
                    )
                )
        self.assertEqual([first, second], original)

    def test_a_short_target_is_not_split_below_the_declared_minimum(self):
        path = permitted_path()
        path["nominal_xyz"] = path["nominal_xyz"][:3]
        candidates = generate_approach_candidates(
            [path], include_midpoint_splits=True
        )
        self.assertTrue(
            all(len(candidate.paths) == 1 for candidate in candidates)
        )

    def test_a_permission_gap_is_rejected_before_subdivision(self):
        path = permitted_path()
        path["nominal_xyz"][5][0] += 0.018
        with self.assertRaisesRegex(ValueError, "permission gap"):
            generate_approach_candidates([path], include_midpoint_splits=True)

    def test_out_of_envelope_tilts_and_exhausted_belt_faces_are_rejected(self):
        search = load_workpiece_package().contact_search
        with self.assertRaisesRegex(ValueError, "tilt"):
            generate_approach_candidates(
                [permitted_path()], tilt_candidates_deg=(0, 3)
            )
        with self.assertRaisesRegex(ValueError, "margin"):
            generate_approach_candidates(
                [permitted_path()], search=replace(search, edge_margin_mm=40)
            )
        with self.assertRaises(ValueError):
            generate_approach_candidates([permitted_path()], max_candidates=0)


class PlanningAlternativeEvaluationTests(unittest.TestCase):
    def candidates(self, path):
        return generate_approach_candidates(
            [path],
            roll_candidates_deg=(45,),
            tilt_candidates_deg=(0,),
            max_candidates=3,
        )

    def test_clearance_and_ik_rejections_recover_through_the_planner_seam(self):
        path = permitted_path()
        original = copy.deepcopy(path)
        calls = []

        def planner(paths, candidate):
            calls.append(candidate.candidate_id)
            self.assertEqual(paths[0]["normal"], original["normal"])
            self.assertEqual(paths[0]["material_allowance_mm"], 0.08)
            if candidate.candidate_id == 0:
                return rejected_plan(
                    paths,
                    "加工姿态干涉：gripper_proxy_1 / belt_front_proxy_93（26.855 mm）",
                    clearance_mm=-26.8554321,
                )
            if candidate.candidate_id == 1:
                return rejected_plan(
                    paths, "ESTUN 姿态不可达：位置 3.36 mm，姿态 4.63°"
                )
            return accepted_plan(paths, clearance_mm=4.250731)

        result = evaluate_planning_alternatives(
            [path], planner, candidates=self.candidates(path)
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.best_candidate.candidate_id, 2)
        self.assertIsNotNone(result.plan)
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(result.rejection.counts["belt_clamp_clearance"], 1)
        self.assertEqual(result.rejection.counts["ik_failure"], 1)
        self.assertFalse(
            result.rejection.fixture_change_required_within_candidates
        )
        self.assertEqual(path, original)

    def test_selects_largest_measured_clearance_then_shortest_duration(self):
        path = permitted_path()
        options = ((2.0, 2.0), (7.0, 25.0), (7.0, 12.0))

        def planner(paths, candidate):
            clearance, duration = options[candidate.candidate_id]
            return accepted_plan(paths, clearance, duration)

        result = evaluate_planning_alternatives(
            [path], planner, candidates=self.candidates(path)
        )
        self.assertEqual(result.best_candidate.candidate_id, 2)
        self.assertEqual(result.plan[2]["duration_s"], 12.0)

    def test_all_collisions_report_measured_candidate_scoped_infeasibility(
        self,
    ):
        path = permitted_path()
        clearances = (-26.8554321, -4.9751123, -0.4987129)

        def planner(paths, candidate):
            return rejected_plan(
                paths,
                "加工姿态干涉：gripper_proxy_3 / belt_front_proxy_91（0.499 mm）",
                clearance_mm=clearances[candidate.candidate_id],
            )

        result = evaluate_planning_alternatives(
            [path], planner, candidates=self.candidates(path)
        )
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.plan)
        self.assertIsNone(result.best_candidate)
        self.assertTrue(result.search_complete)
        self.assertEqual(
            result.rejection.minimum_clamp_belt_clearance_mm, -26.8554321
        )
        self.assertEqual(
            result.rejection.best_candidate_minimum_clamp_belt_clearance_mm,
            -0.4987129,
        )
        self.assertTrue(
            result.rejection.fixture_change_required_within_candidates
        )
        self.assertIn("finite candidate", result.rejection.scope)
        self.assertEqual(result.rejection.required_clearance_mm, 0.0)

    def test_partial_coverage_and_retained_contacts_cannot_be_called_success(
        self,
    ):
        path = permitted_path()

        def planner(paths, candidate):
            plan = accepted_plan(paths)
            if candidate.candidate_id == 0:
                plan[2]["all_paths_passed"] = False
                plan[2]["source_coverage_ratio"] = 0.5
            else:
                plan[2]["unexpected_contacts"] = [
                    {
                        "pair": ["gripper_proxy_0", "belt_front_proxy_1"],
                        "penetration_mm": 1.0,
                        "phase": "approach",
                    }
                ]
                plan[2]["contact_count"] = 1
            return plan

        result = evaluate_planning_alternatives(
            [path], planner, candidates=self.candidates(path)
        )
        self.assertNotEqual(result.status, "passed")
        self.assertIsNone(result.plan)
        self.assertIn("segment_continuity", result.rejection.counts)
        self.assertIn("approach_retreat_collision", result.rejection.counts)

    def test_no_negative_clearance_relaxation_is_allowed(self):
        path = permitted_path()
        with self.assertRaisesRegex(ValueError, "clearance"):
            evaluate_planning_alternatives(
                [path],
                lambda paths, candidate: accepted_plan(paths),
                minimum_clamp_clearance_mm=-0.4,
            )
        result = evaluate_planning_alternatives(
            [path],
            lambda paths, candidate: accepted_plan(paths, clearance_mm=-0.1),
            candidates=self.candidates(path),
        )
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.plan)
        self.assertTrue(
            result.rejection.fixture_change_required_within_candidates
        )

        def unmeasured_plan(paths, candidate):
            plan = accepted_plan(paths)
            del plan[2]["planning_alternative_geometry"]
            return plan

        with self.assertRaisesRegex(ValueError, "clearance"):
            evaluate_planning_alternatives(
                [path],
                unmeasured_plan,
                candidates=self.candidates(path),
                minimum_clamp_clearance_mm=1.0,
            )
        with self.assertRaisesRegex(ValueError, "clearance"):
            evaluate_planning_alternatives(
                [path],
                lambda paths, candidate: accepted_plan(
                    paths, clearance_mm=float("nan")
                ),
                candidates=self.candidates(path),
            )

    def test_budget_exhaustion_is_not_a_geometric_infeasibility_certificate(
        self,
    ):
        path = permitted_path()
        result = evaluate_planning_alternatives(
            [path],
            lambda paths, candidate: rejected_plan(
                paths,
                "规划预算耗尽：每段时间上限",
                clearance_mm=-2.0,
                exhausted=True,
            ),
            candidates=self.candidates(path),
        )
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(
            result.rejection.fixture_change_required_within_candidates
        )
        self.assertFalse(result.search_complete)

    def test_connection_node_limit_is_not_a_geometric_certificate(self):
        path = permitted_path()
        result = evaluate_planning_alternatives(
            [path],
            lambda paths, candidate: rejected_plan(
                paths,
                "连接段避障搜索达到时间/节点上限",
                clearance_mm=-2.0,
            ),
            candidates=self.candidates(path),
        )
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(result.search_complete)
        self.assertFalse(
            result.rejection.fixture_change_required_within_candidates
        )

    def test_attempt_limit_reports_an_incomplete_search(self):
        path = permitted_path()
        result = evaluate_planning_alternatives(
            [path],
            lambda paths, candidate: rejected_plan(
                paths,
                "加工姿态干涉：gripper_proxy_0 / belt_front_proxy_1（1.000 mm）",
                clearance_mm=-1,
            ),
            candidates=self.candidates(path),
            max_attempts=1,
        )
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(result.search_complete)
        self.assertFalse(
            result.rejection.fixture_change_required_within_candidates
        )

    def test_unexpected_planner_errors_are_not_hidden_as_infeasibility(self):
        path = permitted_path()

        def broken_planner(paths, candidate):
            raise RuntimeError("planner programming failure")

        with self.assertRaisesRegex(RuntimeError, "programming failure"):
            evaluate_planning_alternatives(
                [path], broken_planner, candidates=self.candidates(path)
            )


class PlanningRejectionTaxonomyTests(unittest.TestCase):
    def test_real_chinese_rejection_reasons_remain_distinct(self):
        checks = {
            "passed": False,
            "rows": [
                {
                    "status": "blocked",
                    "reason": "ESTUN 姿态不可达：位置 3.36 mm，姿态 4.63°",
                },
                {"status": "blocked", "reason": "命令超出已发布的六轴关节限位"},
                {
                    "status": "blocked",
                    "reason": "加工姿态干涉：gripper_proxy_1 / belt_front_proxy_93（26.855 mm）",
                },
                {
                    "status": "blocked",
                    "reason": "加工轨迹关节跳变，需独立切段进退刀",
                },
            ],
            "unexpected_contacts": [
                {
                    "pair": ["wp_proxy_2", "belt_front_proxy_5"],
                    "penetration_mm": 0.73,
                    "phase": "retract",
                }
            ],
        }
        summary = PlanningRejectionSummary.from_checks(checks)
        for category in (
            "ik_failure",
            "joint_limit",
            "belt_clamp_clearance",
            "approach_retreat_collision",
            "segment_continuity",
        ):
            self.assertIn(category, summary.counts)
        self.assertIn(
            ("gripper_proxy_1", "belt_front_proxy_93"), summary.collision_pairs
        )
        self.assertIn(
            ("wp_proxy_2", "belt_front_proxy_5"), summary.collision_pairs
        )
        self.assertFalse(summary.fixture_change_required_within_candidates)

    def test_successful_rows_do_not_turn_repaired_attempts_into_failures(self):
        summary = PlanningRejectionSummary.from_checks(
            {
                "passed": True,
                "rows": [
                    {
                        "status": "passed",
                        "failure_reasons": [
                            "ESTUN 姿态不可达：位置 3.36 mm，姿态 4.63°"
                        ],
                    }
                ],
                "unexpected_contacts": [],
            }
        )
        self.assertFalse(summary.counts)
        self.assertFalse(summary.reasons)


if __name__ == "__main__":
    unittest.main()
