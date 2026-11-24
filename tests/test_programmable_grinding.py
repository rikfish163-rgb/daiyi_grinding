"""Boundary interventions, saved evidence replay and permanent safety vetoes."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding import project
from daiyi_grinding.dataset_pipeline import interventions_to_hard_examples
from daiyi_grinding.programmable_grinding import (
    REPLAY_SCHEMA,
    GrindingStage,
    InterventionDirective,
    ProgrammableGrindingSession,
    SafetyVeto,
    SavedRunReplayAdapter,
    StageCheckpoint,
)

DEMO_ROOT = (
    project.RUNS
    / "enterprise_offline_20261123/dataset_scale_20261124/programmable_demo"
)
HISTORICAL_RESULT = (
    project.RUNS
    / "enterprise_offline_20261123/functional_resume/thin_120483_finite_03/result.json"
)
FIXED_TIME = "2026-11-24T00:00:00+00:00"


def replay_data():
    """Build separated permitted CAD traces and two signed observation rounds."""
    targets = [
        {
            "id": identity,
            "kind": "seam",
            "points": [
                [offset + index * 0.001, 0.0, 1.0] for index in range(21)
            ],
            "normals": [[0.0, 0.0, -1.0]] * 21,
        }
        for identity, offset in enumerate((0.0, 0.3, 0.6), start=1)
    ]
    paths = [
        {
            "segment_id": target["id"],
            "detection_id": target["id"],
            "kind": "seam",
            "xyz": copy.deepcopy(target["points"]),
            "nominal_xyz": copy.deepcopy(target["points"]),
            "normal": [0.0, 0.0, -1.0],
            "normal_height_mm": 0.6,
            "machining_required": True,
            "material_allowance_mm": 0.08,
            "coordinate_frame": "workpiece_097",
            "view_index": 0,
            "recipe": {
                "tool": "belt",
                "feed_mm_s": 10.0,
                "force_target_n": None,
                "overlap": None,
            },
        }
        for target in targets
    ]
    rounds = []
    for height, verified in ((0.6, False), (0.1, True)):
        rounds.append(
            {
                "preflight": {"passed": True},
                "execution": {
                    "unexpected_contact_count": 0,
                    "unexpected_contacts": [],
                    "execution_complete": True,
                    "motion_gate_passed": True,
                },
                "quality": {
                    "verified": 3 if verified else 0,
                    "needs_rework": 0 if verified else 3,
                    "uncertain": 0,
                    "insufficient": 0,
                    "overgrind": 0,
                    "negative_limit_risk": 0,
                    "invalid_observations": [],
                    "automatic_regrind_allowed": True,
                    "observational_release": verified,
                    "truth_used_for_decision": False,
                    "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
                    "regions": [
                        {
                            "id": target["id"],
                            "kind": "seam",
                            "status": "verified"
                            if verified
                            else "needs_rework",
                            "status_reason": "signed_interval_within_limits"
                            if verified
                            else "positive_excess",
                            "overgrind": False,
                            "negative_limit_risk": False,
                            "observed_anchors": 21,
                            "anchors": 21,
                            "anchor_bounds_mm": [[height - 0.05, height + 0.05]]
                            * 21,
                            "anchor_estimates_mm": [height] * 21,
                            "anchor_observation_counts": [2] * 21,
                            "anchor_positive_lower_mm": [height - 0.05] * 21,
                            "anchor_positive_estimates_mm": [height] * 21,
                            "anchor_positive_observation_counts": [2] * 21,
                        }
                        for target in targets
                    ],
                },
            }
        )
    return {
        "schema": REPLAY_SCHEMA,
        "source_result": "sensor-fixture:three-separated-nominal-traces",
        "mode": "removal",
        "regions": [
            {"id": target["id"], "kind": "seam", "normal_height_mm": 0.6}
            for target in targets
        ],
        "paths": paths,
        "registered_targets": targets,
        "protected_points": [],
        "rounds": rounds,
        "removal_rate_mm_s": None,
    }


def session(data=None, **options):
    return ProgrammableGrindingSession(
        SavedRunReplayAdapter(replay_data() if data is None else data),
        clock=lambda: FIXED_TIME,
        **options,
    )


def temporary_directory():
    """Keep all test checkpoint writes inside the assigned experiment folder."""
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="test-checkpoint-", dir=DEMO_ROOT)


class ProgrammableBoundaryTests(unittest.TestCase):
    def test_new_views_return_to_an_intervenable_perception_boundary(self):
        class ViewAdapter(SavedRunReplayAdapter):
            def __init__(self, data):
                super().__init__(data)
                self.view_requests = 0

            def request_extra_views(self, *, stage, round_index, count):
                self.view_requests += count
                return self.perceive()

        for use_policy in (False, True):
            with self.subTest(use_policy=use_policy):
                adapter = ViewAdapter(replay_data())
                calls = []

                def policy(checkpoint):
                    calls.append(checkpoint.stage)
                    if (
                        checkpoint.stage == GrindingStage.PRE_EXECUTION
                        and not adapter.view_requests
                    ):
                        return InterventionDirective(
                            "request_extra_views", {"count": 1}
                        )
                    if (
                        checkpoint.stage == GrindingStage.POST_PERCEPTION
                        and adapter.view_requests
                    ):
                        return InterventionDirective(
                            "mask_regions", {"region_ids": [2]}
                        )
                    return InterventionDirective("continue")

                grinding = ProgrammableGrindingSession(
                    adapter, policy=policy if use_policy else None
                )
                if not use_policy:
                    grinding.step()
                    grinding.step()
                    self.assertEqual(
                        grinding.checkpoint.stage, GrindingStage.PRE_EXECUTION
                    )
                    grinding.queue_directive(
                        InterventionDirective(
                            "request_extra_views", {"count": 1}
                        )
                    )
                    grinding.queue_directive(
                        InterventionDirective(
                            "mask_regions", {"region_ids": [2]}
                        ),
                        stage=GrindingStage.POST_PERCEPTION,
                    )
                self.assertEqual(grinding.run()["status"], "DONE")
                self.assertEqual(adapter.view_requests, 1)
                self.assertEqual(grinding.summary()["masked_region_ids"], [2])
                self.assertEqual(
                    grinding.session_trace()["executions"][0][
                        "replayed_candidate_path_ids"
                    ],
                    [1, 3],
                )
                if use_policy:
                    self.assertEqual(
                        calls.count(GrindingStage.POST_PERCEPTION), 2
                    )
                self.assertEqual(
                    grinding.session_trace()["pending_directives"], []
                )

    def test_mid_run_region_mask_and_path_exclusion_persist_into_regrind(self):
        grinding = session()
        grinding.queue_directive(
            InterventionDirective(
                "mask_regions",
                {"region_ids": [2]},
                "operator",
                "False-positive review",
            ),
            stage=GrindingStage.POST_PLANNING,
            round_index=0,
        )
        grinding.queue_directive(
            InterventionDirective(
                "exclude_paths",
                {"path_ids": [3]},
                "script",
                "Withhold an unexecuted segment",
            ),
            stage=GrindingStage.PRE_EXECUTION,
            round_index=0,
        )
        result = grinding.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["masked_region_ids"], [2])
        self.assertEqual(result["excluded_path_ids"], [3])
        trace = grinding.session_trace()
        self.assertEqual(
            trace["executions"][0]["replayed_candidate_path_ids"], [1]
        )
        followup = next(
            row for row in trace["checkpoints"] if row["stage"] == "PRE_REGRIND"
        )
        self.assertEqual(followup["candidate_path_ids"], [4])
        self.assertEqual(
            followup["controller_decision"]["local_regrind"][
                "positive_anchor_count"
            ],
            63,
        )
        self.assertFalse(
            followup["controller_decision"]["truth_used_for_control"]
        )
        requests = interventions_to_hard_examples(grinding.interventions)[
            "requests"
        ]
        self.assertEqual(
            {row["requested_kind"] for row in requests},
            {"false_positive_review", "skipped_segment_review"},
        )

    def test_mask_uses_actual_tool_footprint_not_only_detection_identity(self):
        data = replay_data()
        data["registered_targets"][1]["points"] = [[0.01, 0.005, 1.0]] * 21
        grinding = session(data)
        grinding.queue_directive(
            InterventionDirective("mask_regions", {"region_ids": [2]})
        )
        result = grinding.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(
            grinding.session_trace()["executions"][0][
                "replayed_candidate_path_ids"
            ],
            [3],
        )

    def test_feed_bounds_and_regrind_cap_are_applied_and_logged(self):
        grinding = session(regrind_budget=0)
        grinding.queue_directive(
            InterventionDirective("adjust_feed_scale", {"feed_scale": 99.0}),
            stage=GrindingStage.POST_PLANNING,
        )
        grinding.queue_directive(
            InterventionDirective(
                "set_regrind_budget", {"regrind_budget": 100}
            ),
            stage=GrindingStage.POST_PLANNING,
        )
        grinding.queue_directive(
            InterventionDirective("adjust_feed_scale", {"feed_scale": 0.0001}),
            stage=GrindingStage.PRE_EXECUTION,
        )
        grinding.queue_directive(
            InterventionDirective("set_regrind_budget", {"regrind_budget": 0}),
            stage=GrindingStage.POST_REINSPECTION,
        )
        result = grinding.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["completed_rounds"], 1)
        self.assertEqual(result["stop_reason"], "pass_budget_exhausted")
        self.assertEqual(result["feed_scale"], 0.35)
        caps = [row for row in grinding.interventions if row.get("clamped")]
        self.assertEqual(
            {
                row["after"].get("feed_scale")
                for row in caps
                if "feed_scale" in row["after"]
            },
            {0.35, 1.75},
        )
        budget = next(row for row in caps if "regrind_budget" in row["after"])
        self.assertEqual(
            budget["after"]["regrind_budget"], result["max_regrinds"]
        )
        plans = [
            row
            for row in grinding.session_trace()["events"]
            if row["action"] == "planning_completed"
        ]
        self.assertEqual(len(plans), 3)
        for checkpoint in grinding.session_trace()["checkpoints"]:
            for params in checkpoint["proposed_parameters"]:
                self.assertEqual(params["material_allowance_mm"], 0.08)
                self.assertEqual(params["normal_offset_mm"], 0.0)

    def test_feed_scale_is_absolute_and_invalidates_planning(self):
        grinding = session()
        grinding.step()
        self.assertEqual(grinding.checkpoint.stage, GrindingStage.POST_PLANNING)
        grinding.apply_directive(
            InterventionDirective("adjust_feed_scale", {"feed_scale": 0.8})
        )
        grinding.apply_directive(
            InterventionDirective("adjust_feed_scale", {"feed_scale": 0.8})
        )
        checkpoint = grinding.checkpoint
        self.assertTrue(
            all(
                row["feed_mm_s"] == 8.0
                for row in checkpoint.proposed_parameters
            )
        )
        self.assertFalse(checkpoint.controller_decision["can_execute"])
        self.assertTrue(
            all(
                row["requires_fresh_motion_plan"]
                for row in checkpoint.proposed_parameters
            )
        )
        grinding.run()
        self.assertEqual(grinding.summary()["status"], "DONE")

    def test_pause_save_reload_resume_consumes_each_intervention_once(self):
        grinding = session()
        grinding.queue_directive(
            InterventionDirective(
                "pause", actor="operator", reason="Review follow-up"
            ),
            stage=GrindingStage.PRE_REGRIND,
        )
        grinding.queue_directive(
            InterventionDirective("set_regrind_budget", {"regrind_budget": 1}),
            stage=GrindingStage.PRE_REGRIND,
        )
        paused = grinding.run()
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(paused["completed_rounds"], 1)
        with temporary_directory() as directory:
            path = Path(directory) / "paused.json"
            grinding.save_checkpoint(path)
            saved = json.loads(path.read_text())
            self.assertEqual(len(saved["state"]["queue"]), 1)
            resumed = ProgrammableGrindingSession.load_checkpoint(
                path, clock=lambda: FIXED_TIME
            )
            self.assertEqual(
                resumed.checkpoint.to_dict(), grinding.checkpoint.to_dict()
            )
            resumed.resume(actor="operator")
            count = len(resumed.interventions)
            resumed.resume(actor="operator")
            self.assertEqual(len(resumed.interventions), count)
            completed = resumed.run()
            self.assertEqual(completed["status"], "DONE")
            self.assertEqual(completed["completed_rounds"], 2)
            self.assertEqual(
                sum(
                    row["directive_action"] == "pause"
                    for row in resumed.interventions
                ),
                1,
            )
            self.assertEqual(
                sum(
                    row["directive_action"] == "set_regrind_budget"
                    for row in resumed.interventions
                ),
                1,
            )
            self.assertEqual(
                len(resumed.session_trace()["pending_directives"]), 0
            )
            terminal = Path(directory) / "terminal.json"
            resumed.save_checkpoint(terminal)
            restored = ProgrammableGrindingSession.load_checkpoint(
                terminal, clock=lambda: FIXED_TIME
            )
            before = restored.session_trace()
            restored.run()
            restored.resume()
            self.assertEqual(restored.session_trace(), before)

    def test_policy_is_reattached_but_not_replayed_at_the_paused_boundary(self):
        calls = []

        def policy(checkpoint):
            calls.append((checkpoint.round_index, checkpoint.stage))
            return InterventionDirective(
                "pause"
                if checkpoint.stage == GrindingStage.PRE_REGRIND
                else "continue"
            )

        grinding = session(policy=policy)
        grinding.run()
        with temporary_directory() as directory:
            path = Path(directory) / "policy.json"
            grinding.save_checkpoint(path)
            with self.assertRaisesRegex(ValueError, "Reattach"):
                ProgrammableGrindingSession.load_checkpoint(path)
            resumed = ProgrammableGrindingSession.load_checkpoint(
                path, policy=policy
            )
            resumed.resume()
            resumed.run()
            self.assertEqual(resumed.status, "DONE")
        self.assertEqual(calls.count((0, GrindingStage.PRE_REGRIND)), 1)

    def test_extra_view_request_cannot_fabricate_new_saved_observations(self):
        grinding = session()
        grinding.queue_directive(
            InterventionDirective("request_extra_views", {"count": 2})
        )
        result = grinding.run()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["stop_reason"], "extra_views_unavailable")
        self.assertEqual(result["completed_rounds"], 0)
        self.assertFalse(grinding.interventions[0]["after"]["available"])

    def test_snapshot_is_serializable_detached_and_truth_free(self):
        data = replay_data()
        data["regions"][0]["ground_truth_height_mm"] = -1.0
        data["rounds"][0]["quality"]["generated_defects"] = 20

        def policy(checkpoint):
            StageCheckpoint.from_dict(
                json.loads(json.dumps(checkpoint.to_dict()))
            )
            checkpoint.controller_decision["reason"] = "unsafe_force_grinding"
            if checkpoint.observed_region_states:
                checkpoint.observed_region_states[0]["id"] = 999
            return InterventionDirective("continue")

        grinding = session(data, policy=policy)
        self.assertEqual(grinding.run()["status"], "DONE")
        serialized = json.dumps(grinding.session_trace())
        self.assertNotIn("ground_truth_height_mm", serialized)
        self.assertNotIn("generated_defects", serialized)
        self.assertNotIn("unsafe_force_grinding", serialized)
        self.assertTrue(
            all(
                row["id"] in {1, 2, 3}
                for row in grinding.checkpoint.observed_region_states
            )
        )
        self.assertFalse(grinding.summary()["observational_release"])
        self.assertTrue(grinding.summary()["source_observational_release"])


class ProgrammableSafetyTests(unittest.TestCase):
    def test_continue_and_budget_are_vetoed_after_signed_observation_hard_stop(
        self,
    ):
        cases = (
            ("overgrind", {"overgrind": 1}, "overgrind_requires_disposition"),
            (
                "invalid_view",
                {"invalid_observations": [{"reason": "invalid camera"}]},
                "invalid_reinspection_requires_review",
            ),
            (
                "negative_bound",
                {"negative_limit_risk": 1},
                "negative_limit_requires_review",
            ),
        )
        for name, updates, reason in cases:
            with self.subTest(case=name):
                data = replay_data()
                data["rounds"][0]["quality"].update(updates)
                data["rounds"][0]["quality"]["automatic_regrind_allowed"] = (
                    False
                )
                checkpoints = []

                def policy(checkpoint):
                    checkpoints.append(checkpoint)
                    return InterventionDirective(
                        "continue", actor="unsafe-script"
                    )

                grinding = session(data, policy=policy)
                result = grinding.run()
                self.assertEqual(result["status"], "STOPPED_SAFETY")
                self.assertEqual(result["stop_reason"], reason)
                self.assertTrue(checkpoints[-1].safety_hard_stop)
                self.assertEqual(
                    checkpoints[-1].stage, GrindingStage.POST_REINSPECTION
                )
                self.assertTrue(
                    any(
                        row["status"] == "vetoed"
                        for row in grinding.interventions
                    )
                )
                for directive in (
                    InterventionDirective("continue"),
                    InterventionDirective(
                        "set_regrind_budget", {"regrind_budget": 100}
                    ),
                ):
                    with self.assertRaises(SafetyVeto) as caught:
                        grinding.apply_directive(directive)
                    self.assertEqual(caught.exception.code, "safety_hard_stop")
                    self.assertEqual(
                        grinding.interventions[-1]["status"], "vetoed"
                    )
                    self.assertEqual(
                        grinding.interventions[-1]["stop_reason"], reason
                    )
                self.assertEqual(grinding.summary()["completed_rounds"], 1)
                with temporary_directory() as directory:
                    path = Path(directory) / "hard-stop.json"
                    grinding.save_checkpoint(path)
                    restored = ProgrammableGrindingSession.load_checkpoint(path)
                    with self.assertRaises(SafetyVeto):
                        restored.resume()
                    self.assertTrue(restored.checkpoint.safety_hard_stop)

    def test_negative_signed_lower_bound_alone_latches_the_safety_stop(self):
        data = replay_data()
        data["rounds"][0]["quality"]["regions"][0]["anchor_bounds_mm"][0] = [
            -0.31,
            0.65,
        ]
        grinding = session(data)
        result = grinding.run()
        self.assertEqual(result["status"], "STOPPED_SAFETY")
        self.assertEqual(
            result["stop_reason"], "negative_limit_requires_review"
        )

    def test_missing_anchor_report_holds_without_a_nonfinite_or_none_error(
        self,
    ):
        data = replay_data()
        quality = data["rounds"][0]["quality"]
        quality.update(insufficient=1, observational_release=False)
        quality["regions"][0].update(
            anchor_bounds_mm=[[None, None]] * 21, observed_anchors=0
        )
        grinding = session(data)
        result = grinding.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["stop_reason"], "reinspection_requires_review")
        self.assertFalse(result["observational_release"])

    def test_unexpected_collision_or_incomplete_execution_cannot_regrind(self):
        for update, reason in (
            (
                {"unexpected_contact_count": 1},
                "unexpected_collision_requires_disposition",
            ),
            (
                {"execution_complete": False},
                "incomplete_execution_requires_review",
            ),
            ({"motion_gate_passed": False}, "motion_safety_gate"),
        ):
            with self.subTest(reason=reason):
                data = replay_data()
                data["rounds"][0]["execution"].update(update)
                grinding = session(data)
                result = grinding.run()
                self.assertEqual(result["status"], "STOPPED_SAFETY")
                self.assertEqual(result["stop_reason"], reason)
                with self.assertRaises(SafetyVeto):
                    grinding.apply_directive(InterventionDirective("continue"))
                self.assertEqual(result["completed_rounds"], 1)

    def test_payload_cannot_clear_safety_or_change_contact_allowance(self):
        grinding = session()
        for directive in (
            InterventionDirective("continue", {"safety_hard_stop": False}),
            InterventionDirective(
                "adjust_feed_scale",
                {"feed_scale": 1.0, "material_allowance_mm": -0.2},
            ),
        ):
            with self.assertRaises(SafetyVeto) as caught:
                grinding.apply_directive(directive)
            self.assertEqual(caught.exception.code, "unsupported_override")
            self.assertEqual(grinding.interventions[-1]["status"], "vetoed")
        self.assertEqual(grinding.summary()["completed_rounds"], 0)

    def test_safe_abort_is_terminal_and_never_executes_a_path(self):
        grinding = session()
        grinding.queue_directive(
            InterventionDirective("abort_safe", actor="operator")
        )
        self.assertEqual(grinding.run()["status"], "ABORTED")
        self.assertEqual(grinding.summary()["completed_rounds"], 0)
        with self.assertRaises(SafetyVeto) as caught:
            grinding.apply_directive(InterventionDirective("continue"))
        self.assertEqual(caught.exception.code, "terminal_session")


class HistoricalReplayTests(unittest.TestCase):
    def test_real_saved_run_replays_two_observation_rounds_without_new_motion(
        self,
    ):
        adapter = SavedRunReplayAdapter.from_run(HISTORICAL_RESULT)
        grinding = ProgrammableGrindingSession(
            adapter, clock=lambda: FIXED_TIME
        )
        result = grinding.run()
        self.assertEqual(result["status"], "DONE")
        self.assertEqual(result["completed_rounds"], 2)
        self.assertEqual(result["stop_reason"], "observed_regions_verified")
        self.assertTrue(result["source_observational_release"])
        self.assertFalse(result["observational_release"])
        self.assertTrue(result["replay_only"])
        for row in grinding.session_trace()["executions"]:
            self.assertFalse(row["new_motion_executed"])
            self.assertFalse(row["new_material_removal_measured"])
        followup = next(
            row
            for row in grinding.session_trace()["checkpoints"]
            if row["stage"] == "PRE_REGRIND"
        )
        self.assertEqual(len(followup["candidate_path_ids"]), 3)
        self.assertEqual(
            followup["controller_decision"]["local_regrind"][
                "duplicate_trace_count"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
