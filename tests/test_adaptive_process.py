"""Checks for plant adaptation and locally debiased reinspection."""

import unittest

import numpy as np

from daiyi_grinding import adaptive_process as process

MACHINED_ROLES = [process.MACHINED] * 3


def _baseline_rows(height, include_sides=True, include_points=True):
    rows = []
    for index in range(24):
        row = {"height_mm": height, "sigma_mm": 0.015}
        if include_sides:
            row["side"] = -1 if index < 12 else 1
        if include_points:
            row["point"] = [0.2 + 0.001 * index, 0.0, 0.3]
        rows.append(row)
    return rows


def _production_region(height, count=9):
    return {
        "id": 1,
        "kind": "seam",
        "anchor_estimates_mm": [height] * count,
        "anchor_bounds_mm": [[height - 0.04, height + 0.04]] * count,
        "points_m": [[0.0, 0.0, 0.3]] * count,
    }


def _observation(heights, sigma=0.02, roles=MACHINED_ROLES):
    return process.AnchorObservation(
        heights_mm=np.asarray(heights, dtype=float),
        sigma_mm=np.full(len(heights), sigma),
        roles=np.asarray(roles),
    )


class PlantAndRateTests(unittest.TestCase):
    def test_rate_cases_converge_without_truth_access(self):
        cases = [
            case
            for case in process.DEFAULT_CASES
            if case.case_id.startswith("rate_")
        ]
        self.assertEqual(
            {case.rate_scale for case in cases},
            {0.4, 0.7, 1.0, 1.3, 1.8},
        )
        results = [process.run_process_case(case, 0) for case in cases]
        for item in results:
            with self.subTest(case=item["case_id"]):
                self.assertTrue(item["converged"])
                self.assertLessEqual(
                    abs(item["rate_error"]),
                    0.2 * item["case"]["rate_scale"],
                )
                self.assertLessEqual(item["pass_count"], 10)
                self.assertTrue(item["released"])
                self.assertFalse(item["false_release"])
                self.assertLessEqual(item["true_peak_mm"], process.TOLERANCE_MM)
                self.assertFalse(item["truth_used_for_control"])
                self.assertFalse(
                    item["final_estimate"]["truth_used_for_control"]
                )
                for row in item["passes"]:
                    self.assertFalse(row["estimate"]["truth_used_for_control"])
                    self.assertFalse(row["plan"]["truth_used_for_control"])

    def test_controller_depends_only_on_observations(self):
        before = _observation([1.0, 1.1, 1.2])
        after = _observation([0.7, 0.8, 0.9])
        first = process.AdaptiveProcessController()
        second = process.AdaptiveProcessController()
        first.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        second.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        self.assertEqual(first.estimate.record(), second.estimate.record())
        self.assertEqual(
            first.recommend(0.2, force_n=60.0).record(),
            second.recommend(0.2, force_n=60.0).record(),
        )
        record = first.decision_record()
        self.assertFalse(record["truth_used_for_control"])

    def test_duplicate_and_empty_pairs_do_not_update(self):
        controller = process.AdaptiveProcessController()
        before = _observation([1.0, 1.1, 1.2])
        after = _observation([0.7, 0.8, 0.9])
        controller.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        self.assertEqual(controller.estimate.updates, 1)
        controller.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        self.assertEqual(controller.estimate.updates, 1)
        self.assertEqual(controller.estimate.duplicate_updates_ignored, 1)
        controller.update_from_observation(
            before, after, engaged_seconds=0.0, force_n=60.0
        )
        controller.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=0.0
        )
        self.assertEqual(controller.estimate.updates, 1)
        self.assertEqual(controller.estimate.invalid_updates_ignored, 2)
        self.assertFalse(controller.estimate.truth_used_for_control)

    def test_low_snr_freezes_instead_of_amplifying(self):
        controller = process.AdaptiveProcessController()
        before = _observation([1.0, 1.0, 1.0])
        after = _observation([0.99, 0.99, 0.99])
        estimate = controller.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        self.assertEqual(estimate.updates, 0)
        self.assertEqual(estimate.low_snr_freezes, 1)
        self.assertEqual(estimate.relative_rate, 1.0)
        self.assertFalse(estimate.truth_used_for_control)

    def test_nonpositive_removal_never_updates_or_clips_the_gain(self):
        controller = process.AdaptiveProcessController(
            relative_rate=1.3, relative_sigma=0.1
        )
        before = _observation([0.5, 0.5, 0.5])
        after = _observation([0.8, 0.8, 0.8])
        estimate = controller.update_from_observation(
            before, after, engaged_seconds=1.0, force_n=60.0
        )
        self.assertEqual(estimate.updates, 0)
        self.assertEqual(estimate.relative_rate, 1.3)
        self.assertEqual(estimate.relative_sigma, 0.1)
        self.assertEqual(estimate.nonpositive_removal_freezes, 1)
        self.assertFalse(estimate.truth_used_for_control)

    def test_thick_stock_is_split_into_roughing_then_finishing(self):
        controller = process.AdaptiveProcessController()
        plan = controller.plan_staged_passes(np.full(10, 3.0))
        self.assertEqual(plan.status, "ready")
        self.assertGreaterEqual(len(plan.passes), 7)
        self.assertEqual(plan.passes[0].stage, "roughing")
        self.assertEqual(plan.passes[-1].stage, "finishing")
        total = sum(item.planned_cut_mm for item in plan.passes)
        self.assertAlmostEqual(total, 2.98, places=6)
        for item in plan.passes:
            self.assertFalse(item.record()["truth_used_for_control"])
            self.assertLessEqual(
                item.planned_cut_mm,
                controller.limits.max_cut_roughing_mm + 1e-9
                if item.stage == "roughing"
                else controller.limits.max_cut_finishing_mm + 1e-9,
            )

    def test_overgrind_and_nonuniform_stock_hold(self):
        controller = process.AdaptiveProcessController()
        overgrind = controller.plan_staged_passes(np.full(5, -0.35))
        self.assertEqual(overgrind.status, "overgrind_hold")
        self.assertFalse(overgrind.passes)
        uneven = controller.plan_staged_passes(np.array([2.5, 2.1, 2.1, 2.1]))
        self.assertEqual(uneven.status, "overgrind_limited")
        self.assertTrue(uneven.passes)
        self.assertLess(uneven.planned_total_cut_mm, uneven.measured_peak_mm)

    def test_wear_tracks_the_effective_gain_downward(self):
        case = next(
            item
            for item in process.DEFAULT_CASES
            if item.case_id == "wear_severe"
        )
        result = process.run_process_case(case, 0)
        self.assertLess(result["final_estimate"]["relative_rate"], 1.0)
        self.assertFalse(result["false_release"])
        self.assertEqual(result["truth_used_for_control"], False)


class MultiRegionPairingTests(unittest.TestCase):
    @staticmethod
    def _state(estimates, executions, segments):
        regions = []
        for identity, values in enumerate(estimates, 1):
            regions.append(
                {
                    "id": identity,
                    "anchor_estimates_mm": list(values),
                    "anchor_bounds_mm": [
                        [value - 0.04, value + 0.04] for value in values
                    ],
                }
            )
        return {
            "reinspection": {"regions": regions},
            "execution_history": executions,
            "segments": segments,
        }

    def test_multi_region_different_durations_pair_by_region(self):
        segments = [
            {"segment_id": 1, "region_id": 1},
            {"segment_id": 2, "region_id": 2},
        ]
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        adapter.absorb_session_state(
            self._state([[1.0] * 9, [1.0] * 9], [], segments)
        )
        adapter.absorb_session_state(
            self._state(
                [[0.73] * 9, [0.46] * 9],
                [
                    {
                        "segment_id": 1,
                        "engaged_duration_s": 1.0,
                        "commanded_force_n": 60.0,
                    },
                    {
                        "segment_id": 2,
                        "engaged_duration_s": 2.0,
                        "commanded_force_n": 60.0,
                    },
                ],
                segments,
            )
        )
        estimate = adapter.controller.estimate
        self.assertEqual(estimate.updates, 2)
        self.assertLess(abs(estimate.relative_rate - 1.0), 0.02)
        self.assertGreaterEqual(estimate.upper, 1.0)
        self.assertEqual(adapter.last_evidence()["consumed_executions"], 2)
        events = adapter.identification_events
        self.assertEqual({event["region_id"] for event in events}, {1, 2})
        self.assertEqual({event["execution_count"] for event in events}, {1})

    def test_same_region_passes_aggregate_and_zero_contact_region_freezes(self):
        segments = [
            {"segment_id": 1, "region_id": 1},
            {"segment_id": 2, "region_id": 1},
            {"segment_id": 3, "region_id": 2},
        ]
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        adapter.absorb_session_state(
            self._state([[1.0] * 9, [1.0] * 9], [], segments)
        )
        adapter.absorb_session_state(
            self._state(
                [[0.73] * 9, [1.0] * 9],
                [
                    {
                        "segment_id": 1,
                        "engaged_duration_s": 0.5,
                        "commanded_force_n": 60.0,
                    },
                    {
                        "segment_id": 2,
                        "engaged_duration_s": 0.5,
                        "commanded_force_n": 60.0,
                    },
                    {
                        "segment_id": 3,
                        "tool_contact_steps": 0,
                    },
                ],
                segments,
            )
        )
        estimate = adapter.controller.estimate
        self.assertEqual(estimate.updates, 1)
        self.assertLess(abs(estimate.relative_rate - 1.0), 0.02)
        self.assertGreaterEqual(estimate.invalid_updates_ignored, 1)
        self.assertEqual(adapter.identification_events[0]["region_id"], 1)
        self.assertEqual(adapter.identification_events[0]["execution_count"], 2)

    def test_ambiguous_unmapped_batch_is_frozen(self):
        segments = []
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        adapter.absorb_session_state(
            self._state([[1.0] * 9, [1.0] * 9], [], segments)
        )
        adapter.absorb_session_state(
            self._state(
                [[0.73] * 9, [0.46] * 9],
                [
                    {"engaged_duration_s": 1.0, "commanded_force_n": 60.0},
                    {"engaged_duration_s": 2.0, "commanded_force_n": 60.0},
                ],
                segments,
            )
        )
        estimate = adapter.controller.estimate
        self.assertEqual(estimate.updates, 0)
        self.assertEqual(estimate.relative_rate, 1.0)
        self.assertGreaterEqual(estimate.invalid_updates_ignored, 1)
        self.assertEqual(adapter.last_evidence()["consumed_executions"], 2)


class BiasInterceptionTests(unittest.TestCase):
    def test_hidden_bias_is_caught_and_true_conformance_is_released(self):
        results = [
            process.run_bias_interception_case(case, 0)
            for case in process.BIAS_CASES
        ]
        for item in results:
            with self.subTest(case=item["case_id"]):
                self.assertTrue(item["calibrated_correct"])
                self.assertTrue(item["naive_wrong"])
                self.assertFalse(item["truth_used_for_decision"])
                record = item["verdict"]
                self.assertFalse(record["truth_used_for_decision"])
                self.assertTrue(record["gate_unchanged"])
        false_release = [
            item
            for item in results
            if item["case_id"].endswith("false_release")
        ]
        false_reject = [
            item for item in results if item["case_id"].endswith("false_reject")
        ]
        for item in false_release:
            self.assertTrue(item["verdict"]["bias_veto"])
            self.assertTrue(item["verdict"]["naive_false_release"])
        for item in false_reject:
            self.assertFalse(item["verdict"]["bias_veto"])
            self.assertTrue(item["verdict"]["naive_false_reject"])

    def test_calibrator_never_widens_the_gate(self):
        with self.assertRaisesRegex(ValueError, "0.3 mm"):
            process.LocalBaselineConfig(tolerance_mm=0.31)
        with self.assertRaisesRegex(ValueError, "0.3 mm"):
            process.ProcessLimits(tolerance_mm=0.31)
        rng = np.random.default_rng(0)
        baseline = process.SignedAnchors(
            heights_mm=-0.22 + rng.normal(0.0, 0.015, 24),
            sigma_mm=0.015,
            sides=np.array([-1] * 12 + [1] * 12),
        )
        machined = process.SignedAnchors(
            heights_mm=0.14 + rng.normal(0.0, 0.02, 9),
            sigma_mm=0.02,
        )
        verdict = process.LocalBaselineReinspector().evaluate(
            machined, baseline
        )
        self.assertEqual(verdict.status, "positive_excess")
        self.assertTrue(verdict.bias_veto)
        self.assertGreater(verdict.bias_mm, -0.3)
        self.assertLess(verdict.bias_mm, -0.15)
        self.assertFalse(verdict.truth_used_for_decision)

    def test_overlapping_or_uncertain_reference_holds_without_release(self):
        reinspector = process.LocalBaselineReinspector()
        points = np.array(
            [[0.0, 0.0, 0.0], [0.001, 0.0, 0.0], [0.002, 0.0, 0.0]]
        )
        baseline = process.SignedAnchors(
            heights_mm=np.zeros(24),
            sigma_mm=0.015,
            sides=np.array([-1] * 12 + [1] * 12),
            points_m=np.tile(points, (8, 1)),
        )
        machined = process.SignedAnchors(
            heights_mm=np.full(6, 0.1),
            sigma_mm=0.02,
            points_m=points[:3],
        )
        overlap = reinspector.evaluate(machined, baseline)
        self.assertEqual(overlap.status, "hold")
        self.assertEqual(overlap.reason, "baseline_overlaps_target")
        self.assertTrue(overlap.bias_veto)
        self.assertFalse(overlap.truth_used_for_decision)
        uncertain_baseline = process.SignedAnchors(
            heights_mm=np.linspace(-0.5, 0.5, 24),
            sigma_mm=0.015,
            sides=np.array([-1] * 12 + [1] * 12),
        )
        uncertain = reinspector.evaluate(machined, uncertain_baseline)
        self.assertEqual(uncertain.status, "hold")
        self.assertEqual(uncertain.reason, "baseline_inconsistent")
        self.assertTrue(uncertain.bias_veto)

    def test_missing_baseline_holds_without_veto(self):
        reinspector = process.LocalBaselineReinspector()
        baseline = process.SignedAnchors(heights_mm=np.zeros(4), sigma_mm=0.02)
        machined = process.SignedAnchors(
            heights_mm=np.full(5, 0.1), sigma_mm=0.02
        )
        verdict = reinspector.evaluate(machined, baseline)
        self.assertEqual(verdict.status, "hold")
        self.assertEqual(verdict.reason, "baseline_insufficient")
        self.assertFalse(verdict.bias_veto)


class SessionAdapterTests(unittest.TestCase):
    def test_process_adapter_matches_p0a_signature(self):
        controller = process.AdaptiveProcessController()
        adapter = controller.as_session_adapter()
        segment = {
            "segment_id": 3,
            "region_id": 1,
            "path": {
                "normal_height_mm": 0.8,
                "material_allowance_mm": 0.08,
                "recipe": {"feed_mm_s": 30.0},
                "nominal_xyz": [[0.0, 0.0, 0.0], [0.06, 0.0, 0.0]],
            },
        }
        state = {
            "stage": "PLANNED",
            "reinspection": {},
            "execution_history": [],
        }
        overrides = adapter(segment, state)
        self.assertIsInstance(overrides, dict)
        self.assertLessEqual(
            set(overrides),
            set(process.SESSION_OVERRIDE_KEYS) | {"truth_used_for_control"},
        )
        self.assertIs(overrides["truth_used_for_control"], False)
        self.assertGreaterEqual(
            overrides["feed_scale"],
            controller.limits.feed_scale_bounds[0],
        )
        self.assertLessEqual(
            overrides["feed_scale"],
            controller.limits.feed_scale_bounds[1],
        )
        self.assertGreater(overrides["normal_offset_mm"], 0.0)
        self.assertEqual(overrides["passes"], 1)
        decision = controller.decision_record()
        self.assertFalse(decision["truth_used_for_control"])
        self.assertIn("decision", decision)
        self.assertEqual(
            controller(segment, state)["truth_used_for_control"], False
        )
        evidence = adapter.last_evidence()
        self.assertIn("last_decision", evidence)
        self.assertFalse(evidence["truth_used_for_control"])

    @staticmethod
    def _state(estimates, executions):
        return {
            "reinspection": {
                "regions": [
                    {
                        "id": 1,
                        "anchor_estimates_mm": list(estimates),
                        "anchor_bounds_mm": [
                            [value - 0.04, value + 0.04] for value in estimates
                        ],
                    }
                ]
            },
            "execution_history": executions,
        }

    def test_no_contact_duration_is_not_removal_evidence(self):
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        adapter.absorb_session_state(self._state([1.0] * 3, []))
        adapter.absorb_session_state(
            self._state(
                [0.7] * 3,
                [
                    {
                        "duration_s": 1.0,
                        "force_n": 60.0,
                        "tool_contact_steps": 0,
                    }
                ],
            )
        )
        estimate = adapter.controller.estimate
        self.assertEqual(estimate.updates, 0)
        self.assertEqual(estimate.relative_rate, 1.0)
        self.assertEqual(estimate.relative_sigma, 0.45)
        self.assertEqual(estimate.invalid_updates_ignored, 1)
        self.assertEqual(adapter.last_evidence()["consumed_executions"], 1)

    def test_native_mujoco_contact_fields_derive_engaged_time(self):
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        adapter.absorb_session_state(self._state([1.0] * 3, []))
        adapter.absorb_session_state(
            self._state(
                [0.7] * 3,
                [
                    {
                        "tool_contact_steps": 500,
                        "simulation_steps": 1000,
                        "simulated_seconds": 2.0,
                        "max_contact_force_n": 45.0,
                    }
                ],
            )
        )
        self.assertEqual(adapter.controller.estimate.updates, 1)
        event = adapter.identification_events[-1]
        self.assertAlmostEqual(event["engaged_seconds"], 1.0)
        self.assertAlmostEqual(event["force_n"], 45.0)

    def test_cross_instance_restore_keeps_posterior_and_consumption(self):
        first = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        execution_one = {
            "engaged_duration_s": 1.0,
            "commanded_force_n": 60.0,
        }
        first.absorb_session_state(self._state([1.0] * 3, []))
        first.absorb_session_state(self._state([0.7] * 3, [execution_one]))
        self.assertEqual(first.controller.estimate.updates, 1)
        evidence = first.last_evidence()
        rate = evidence["estimate"]["relative_rate"]
        sigma = evidence["estimate"]["relative_sigma"]
        self.assertEqual(evidence["consumed_executions"], 1)
        self.assertEqual(evidence["previous_estimates"]["1"], [0.7, 0.7, 0.7])
        second = process.SessionProcessAdapter(
            process.AdaptiveProcessController()
        )
        restored = dict(
            self._state([0.7] * 3, [execution_one]),
            process_state=evidence,
        )
        second.absorb_session_state(restored)
        self.assertEqual(second.controller.estimate.updates, 1)
        self.assertEqual(second.controller.estimate.relative_rate, rate)
        self.assertEqual(second.controller.estimate.relative_sigma, sigma)
        self.assertFalse(rate == 1.0 and sigma == 0.45)
        second.absorb_session_state(self._state([0.7] * 3, [execution_one]))
        self.assertEqual(second.controller.estimate.updates, 1)
        execution_two = {
            "engaged_duration_s": 1.0,
            "commanded_force_n": 60.0,
        }
        second.absorb_session_state(
            self._state([0.5] * 3, [execution_one, execution_two])
        )
        self.assertEqual(second.controller.estimate.updates, 2)
        self.assertEqual(second.last_evidence()["consumed_executions"], 2)

    def test_zero_excess_adapter_returns_no_pass_and_evidence(self):
        controller = process.AdaptiveProcessController()
        adapter = controller.as_session_adapter()
        segment = {
            "segment_id": 9,
            "region_id": 1,
            "path": {
                "normal_height_mm": 0.003,
                "material_allowance_mm": 0.08,
                "recipe": {"feed_mm_s": 30.0},
                "nominal_xyz": [[0.0, 0.0, 0.0], [0.01, 0.0, 0.0]],
            },
        }
        overrides = adapter(
            segment, {"stage": "PLANNED", "execution_history": []}
        )
        self.assertEqual(overrides["passes"], 0)
        self.assertIs(overrides["truth_used_for_control"], False)
        evidence = adapter.last_evidence()
        self.assertIsNotNone(evidence["last_decision"])
        self.assertEqual(
            evidence["last_decision"]["stop_reason"],
            "already_within_target",
        )
        self.assertEqual(evidence["last_decision"]["passes"], 0)
        self.assertIs(evidence["truth_used_for_control"], False)

    def test_process_limits_enforce_shared_hard_gates(self):
        for kwargs in (
            {"max_normal_offset_mm": 0.4, "max_allowance_mm": 0.8},
            {"max_normal_offset_mm": 0.25},
            {"max_allowance_mm": 0.35},
            {"max_normal_offset_mm": 0.20, "max_allowance_mm": 0.15},
            {"feed_scale_bounds": (0.05, 1.0)},
            {"feed_scale_bounds": (0.5, 3.5)},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    process.ProcessLimits(**kwargs)
        limits = process.ProcessLimits(
            max_normal_offset_mm=0.20, max_allowance_mm=0.30
        )
        self.assertEqual(
            limits.max_normal_offset_mm,
            process.MAX_NORMAL_OFFSET_MM,
        )
        self.assertEqual(limits.max_allowance_mm, process.MAX_ALLOWANCE_MM)
        self.assertEqual(limits.feed_scale_bounds, (0.35, 1.75))
        adapter = process.SessionProcessAdapter(
            process.AdaptiveProcessController(limits)
        )
        overrides = adapter(
            {
                "segment_id": 1,
                "path": {
                    "normal_height_mm": 0.8,
                    "material_allowance_mm": 0.08,
                    "recipe": {"feed_mm_s": 30.0},
                    "nominal_xyz": [[0.0, 0.0, 0.0], [0.06, 0.0, 0.0]],
                },
            },
            {"stage": "PLANNED", "execution_history": []},
        )
        self.assertLessEqual(
            abs(overrides["normal_offset_mm"]),
            process.MAX_NORMAL_OFFSET_MM,
        )
        self.assertLessEqual(
            abs(0.08 + overrides["normal_offset_mm"]),
            process.MAX_ALLOWANCE_MM,
        )

    def test_normal_offset_stays_inside_the_p0a_safety_window(self):
        controller = process.AdaptiveProcessController()
        adapter = controller.as_session_adapter()
        segment = {
            "segment_id": 2,
            "path": {
                "normal_height_mm": 0.8,
                "material_allowance_mm": 0.08,
                "recipe": {"feed_mm_s": 30.0},
                "nominal_xyz": [[0.0, 0.0, 0.0], [0.06, 0.0, 0.0]],
            },
        }
        state = {"stage": "PLANNED", "execution_history": []}
        overrides = adapter(segment, state)
        combined = (
            segment["path"]["material_allowance_mm"]
            + overrides["normal_offset_mm"]
        )
        self.assertLessEqual(abs(overrides["normal_offset_mm"]), 0.20)
        self.assertLessEqual(abs(combined), 0.30)
        evidence = adapter.last_evidence()
        self.assertIsNone(
            evidence["last_decision"]["stop_reason"]
            if evidence["last_decision"] is not None
            else None
        )
        decision = evidence["last_decision"]
        self.assertGreaterEqual(decision["offset_limited_mm"], 0.0)
        self.assertFalse(evidence["truth_used_for_control"])

    def test_session_adapter_absorbs_signed_evidence(self):
        controller = process.AdaptiveProcessController()
        adapter = controller.as_session_adapter()
        segment = {
            "segment_id": 1,
            "path": {
                "normal_height_mm": 0.5,
                "material_allowance_mm": 0.08,
                "recipe": {"feed_mm_s": 30.0},
            },
        }
        pre = {
            "reinspection": {
                "regions": [
                    {
                        "id": 1,
                        "anchor_estimates_mm": [1.0, 1.1, 1.2],
                        "anchor_bounds_mm": [
                            [0.96, 1.04],
                            [1.06, 1.14],
                            [1.16, 1.24],
                        ],
                    }
                ]
            },
            "execution_history": [],
        }
        adapter(segment, pre)
        self.assertEqual(controller.estimate.updates, 0)
        post = {
            "reinspection": {
                "regions": [
                    {
                        "id": 1,
                        "anchor_estimates_mm": [0.7, 0.8, 0.9],
                        "anchor_bounds_mm": [
                            [0.66, 0.74],
                            [0.76, 0.84],
                            [0.86, 0.94],
                        ],
                    }
                ]
            },
            "execution_history": [
                {"engaged_duration_s": 1.0, "commanded_force_n": 60.0}
            ],
        }
        adapter(segment, post)
        self.assertGreaterEqual(controller.estimate.updates, 1)
        self.assertTrue(adapter.identification_events)
        self.assertFalse(controller.estimate.truth_used_for_control)
        without_duration = dict(post, execution_history=[])
        adapter(segment, without_duration)
        self.assertEqual(
            controller.estimate.updates,
            1,
        )

    def test_calibrator_keeps_points_and_requires_both_sides(self):
        reinspector = process.LocalBaselineReinspector()
        with self.assertRaisesRegex(ValueError, "0.3 mm"):
            process.LocalBaselineConfig(tolerance_mm=0.31)
        baseline_no_sides = process.SignedAnchors(
            heights_mm=np.zeros(24),
            sigma_mm=0.015,
            sides=None,
        )
        machined = process.SignedAnchors(
            heights_mm=np.full(6, 0.1),
            sigma_mm=0.02,
        )
        verdict = reinspector.evaluate(machined, baseline_no_sides)
        self.assertEqual(verdict.status, "hold")
        self.assertEqual(verdict.reason, "baseline_sides_missing")
        self.assertFalse(verdict.truth_used_for_decision)

    def test_production_regions_list_is_calibrated(self):
        calibrator = process.LocalBaselineReinspector().as_session_calibrator()
        report = {
            "regions": [_production_region(0.10)],
            "local_baseline": {"baseline": _baseline_rows(0.0)},
        }
        result = calibrator(report, {})
        self.assertNotEqual(result["status"], "baseline_unavailable")
        self.assertEqual(result["status"], "pass")
        self.assertLess(abs(result["bias_mm"]), 0.01)
        self.assertFalse(result["bias_veto"])
        self.assertFalse(result["truth_used_for_decision"])

    def test_production_regions_hidden_bias_is_blocked(self):
        calibrator = process.LocalBaselineReinspector().as_session_calibrator()
        positive = {
            "regions": [_production_region(0.18)],
            "local_baseline": {"baseline": _baseline_rows(-0.18)},
        }
        result = calibrator(positive, {})
        self.assertEqual(result["status"], "positive_excess")
        self.assertEqual(result["naive_status"], "pass")
        self.assertTrue(result["bias_veto"])
        negative = {
            "regions": [_production_region(-0.18)],
            "local_baseline": {"baseline": _baseline_rows(0.18)},
        }
        result = calibrator(negative, {})
        self.assertEqual(result["status"], "overgrind")
        self.assertEqual(result["naive_status"], "pass")
        self.assertTrue(result["bias_veto"])

    def test_baseline_anchors_fallback_and_empty_regions(self):
        calibrator = process.LocalBaselineReinspector().as_session_calibrator()
        fallback = {
            "regions": [_production_region(0.10)],
            "baseline_anchors": _baseline_rows(0.0),
        }
        result = calibrator(fallback, {})
        self.assertEqual(result["status"], "pass")
        empty = {
            "regions": [
                {
                    "id": 1,
                    "anchor_estimates_mm": [None, None],
                    "anchor_bounds_mm": [[None, None], [None, None]],
                }
            ],
            "local_baseline": {"baseline": _baseline_rows(0.0)},
        }
        result = calibrator(empty, {})
        self.assertEqual(result["status"], "baseline_unavailable")

    def test_calibrator_dict_points_trigger_overlap_hold(self):
        calibrator = process.LocalBaselineReinspector().as_session_calibrator()
        point = [0.0, 0.0, 0.0]
        report = {
            "machined": [
                {"height_mm": 0.1, "sigma_mm": 0.02, "point_m": point}
                for _ in range(6)
            ],
            "local_baseline": {
                "baseline": [
                    {
                        "height_mm": 0.0,
                        "sigma_mm": 0.015,
                        "side": -1 if index < 12 else 1,
                        "point_m": point,
                    }
                    for index in range(24)
                ]
            },
        }
        result = calibrator(report, {"stage": "RESCANNED"})
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["reason"], "baseline_overlaps_target")
        self.assertTrue(result["bias_veto"])
        self.assertFalse(result["truth_used_for_decision"])
        self.assertTrue(result["gate_unchanged"])

    def test_calibrator_matches_p0a_signature(self):
        calibrator = process.LocalBaselineReinspector().as_session_calibrator()
        report = {
            "machined": [
                {"height_mm": 0.14, "sigma_mm": 0.02} for _ in range(9)
            ],
            "local_baseline": {
                "baseline": [
                    {"height_mm": -0.22, "sigma_mm": 0.015, "side": -1}
                    for _ in range(12)
                ]
                + [
                    {"height_mm": -0.22, "sigma_mm": 0.015, "side": 1}
                    for _ in range(12)
                ],
                "protected": [
                    {"height_mm": -0.22, "sigma_mm": 0.015} for _ in range(8)
                ],
            },
        }
        result = calibrator(report, {"stage": "RESCANNED"})
        self.assertEqual(result["status"], "positive_excess")
        self.assertTrue(result["bias_veto"])
        self.assertTrue(result["gate_unchanged"])
        self.assertIs(result["truth_used_for_decision"], False)
        unavailable = calibrator({"machined": []}, {"stage": "RESCANNED"})
        self.assertEqual(unavailable["status"], "baseline_unavailable")
        self.assertFalse(unavailable["bias_veto"])
        self.assertIs(unavailable["truth_used_for_decision"], False)


if __name__ == "__main__":
    unittest.main()
