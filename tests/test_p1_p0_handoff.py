"""Cross-module handoff checks between P1, P0-A and P0-B (no MuJoCo)."""

import copy
import tempfile
import unittest
from pathlib import Path

import numpy as np

from daiyi_grinding import adaptive_process as process
from daiyi_grinding import dataset_pipeline, workpiece_packages
from daiyi_grinding import workstation_session as session_module


def _path(allowance_mm=0.08, detection_id=1, grasp_setup="original"):
    return {
        "detection_id": detection_id,
        "kind": "seam",
        "xyz": [[0.0, 0.0, 0.3], [0.01, 0.0, 0.3]],
        "nominal_xyz": [[0.0, 0.0, 0.3], [0.01, 0.0, 0.3]],
        "observed_xyz": [[0.0, 0.0, 0.3008], [0.01, 0.0, 0.3008]],
        "normal": [0.0, 0.0, 1.0],
        "normal_height_mm": 0.8,
        "machining_required": True,
        "coordinate_frame": "workpiece_097",
        "material_allowance_mm": allowance_mm,
        "clearance_mm": 3,
        "tool_diameter_mm": 100.0,
        "tool_width_mm": 20.0,
        "grasp_setup": grasp_setup,
        "workpiece_id": "097",
        "recipe": {
            "tool": "belt",
            "feed_mm_s": 30.0,
            "force_target_n": None,
            "overlap": None,
        },
    }


def _detection(identity=1):
    return {
        "id": identity,
        "kind": "seam",
        "machining_required": True,
        "normal_height_mm": 0.8,
        "height_mm": 0.8,
        "length_mm": 10.0,
        "width_mm": 2.0,
        "confidence": 0.9,
    }


def _anchors(height_mm, count=9, side=None, point=None, sigma_mm=0.02):
    rows = []
    for index in range(count):
        row = {
            "height_mm": height_mm,
            "sigma_mm": sigma_mm,
            "point": point or [0.0, 0.0, 0.3],
        }
        if side is not None:
            row["side"] = side
        rows.append(row)
    return rows


def _baseline(height_mm=0.0, include_sides=True):
    rows = []
    for index in range(24):
        row = {
            "height_mm": height_mm,
            "sigma_mm": 0.015,
            "point": [0.2 + 0.001 * index, 0.0, 0.3],
        }
        if include_sides:
            row["side"] = -1 if index < 12 else 1
        rows.append(row)
    return rows


def _quality():
    return {
        "verified": 1,
        "needs_rework": 0,
        "uncertain": 0,
        "overgrind": 0,
        "negative_limit_risk": 0,
        "insufficient": 0,
        "automatic_regrind_allowed": True,
        "coverage_scope": "six_view",
        "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
        "observational_release": True,
        "truth_used_for_decision": False,
        "invalid_observations": [],
        "uncertainty_model": {"sigma_multiplier": 2},
        "regions": [
            {
                "id": 1,
                "kind": "seam",
                "status": "verified",
                "status_reason": "signed_interval_within_limits",
                "positive_excess": False,
                "overgrind": False,
                "negative_limit_risk": False,
                "anchors": 9,
                "observed_anchors": 9,
                "anchor_bounds_mm": [[0.08, 0.12]] * 9,
                "anchor_estimates_mm": [0.10] * 9,
                "anchor_observation_counts": [9] * 9,
            }
        ],
    }


class StubBackend:
    """Pure-Python ``SessionBackend``; never constructs or steps MuJoCo."""

    def __init__(self, directory, config, checkpoint, scenario="pass"):
        self.directory = Path(directory)
        self.config = dict(config)
        self.checkpoint = checkpoint
        self.scenario = scenario
        self.scans = 0
        self.executions = 0

    def scan(self, output, **options):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            output / "view_00_observation.npz", stub=np.zeros(3)
        )
        self.scans += 1
        if options.get("targets"):
            return self._rescan()
        return {
            "detections": [_detection()],
            "paths": [_path()],
            "perception": {"prediction_revision": 1},
            "registered_regions": [
                {
                    "id": 1,
                    "kind": "seam",
                    "points": [[0.0, 0.0, 0.3], [0.01, 0.0, 0.3]],
                    "normals": [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
                }
            ],
            "protected_points": [],
            "views": [],
            "observations": ["view_00_observation.npz"],
        }

    def _rescan(self):
        result = {
            "detections": [],
            "paths": [],
            "perception": {
                "prediction_revision": 2,
                "region_reinspection": _quality(),
            },
            "registered_regions": [],
            "protected_points": [],
            "views": [],
            "observations": ["view_00_observation.npz"],
        }
        if self.scenario == "pass":
            result["local_baseline"] = {
                "machined": _anchors(0.10),
                "baseline": _baseline(0.0),
                "protected": _anchors(0.0, count=8, sigma_mm=0.015),
            }
        elif self.scenario == "bias_veto":
            result["local_baseline"] = {
                "machined": _anchors(0.18),
                "baseline": _baseline(-0.18),
                "protected": _anchors(-0.18, count=8, sigma_mm=0.015),
            }
        elif self.scenario == "sides_missing":
            result["local_baseline"] = {
                "machined": _anchors(0.10),
                "baseline": _baseline(0.0, include_sides=False),
                "protected": _anchors(0.0, count=8, sigma_mm=0.015),
            }
        elif self.scenario == "baseline_unavailable":
            pass
        else:
            raise ValueError(f"Unknown stub scenario: {self.scenario}")
        return result

    def plan(self, path):
        return (
            [{"t": 0.0, "phase": "trace", "point": [0.0, 0.0, 0.3]}],
            [path],
            {"passed": True},
        )

    def preflight(self, commands):
        return {"passed": bool(commands)}

    def execute(self, commands, **options):
        self.executions += 1
        return {
            "unexpected_contact_count": 0,
            "tracking_trace_p95_mm": 0.05,
            "surface_tracking_p95_mm": 0.05,
            "simulated_seconds": 1.0,
            "simulation_steps": 10,
            "engaged_duration_s": 1.0,
            "commanded_force_n": 60.0,
            "truth_used_for_control": False,
        }

    def safe_retract(self, **options):
        return {"passed": True}

    def manual_region(self, roi_spec, observation_path):
        return {
            "region": {
                "id": 2,
                "kind": "seam",
                "machining_required": True,
                "normal_height_mm": 0.5,
                "height_mm": 0.5,
                "length_mm": 10.0,
                "width_mm": 2.0,
                "confidence": 0.8,
            },
            "target": {
                "id": 1,
                "kind": "seam",
                "points": [[0.05, 0.0, 0.3], [0.06, 0.0, 0.3]],
                "normals": [[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
            },
            "paths": [_path(detection_id=2)],
        }

    def save_checkpoint(self, output):
        Path(output).write_bytes(b"stub-checkpoint")

    def stock_summary(self):
        return {"remaining_height_mm": 0.8, "source": "stub"}

    def close(self):
        return None


def _factory(scenario):
    def build(directory, config, checkpoint):
        return StubBackend(directory, config, checkpoint, scenario)

    return build


def _create(directory, scenario, adapters=True):
    controller = process.AdaptiveProcessController()
    reinspector = process.LocalBaselineReinspector()
    session = session_module.create_session(
        Path(directory),
        session_id="handoff",
        seed=11,
        case="mixed",
        mode="removal",
        views="multi",
        tool="belt",
        detector="geometric",
        max_passes=2,
        process_adapter=controller.as_session_adapter() if adapters else None,
        reinspection_calibrator=reinspector.as_session_calibrator()
        if adapters
        else None,
        backend_factory=_factory(scenario),
    )
    return session, controller


def _advance_to_planned(session):
    session.step_next()  # IDLE -> SCANNED
    session.step_next()  # SCANNED -> DETECTED
    session.approve_regions([1])
    session.step_next()  # DETECTED -> PLANNED


class SessionClosedLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="daiyi-handoff-")
        cls.root = Path(cls.temporary.name)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def _run(self, scenario, name):
        session, controller = _create(self.root / name, scenario)
        _advance_to_planned(session)
        session.step_next()  # execute the only segment
        session.step_next()  # no pending -> rescan
        session.step_next()  # RESCANNED -> DONE
        return session, controller

    def test_full_session_persists_plugin_evidence_and_reloads(self):
        session, controller = self._run("pass", "session_pass")
        state = session.status()
        self.assertEqual(state["stage"], "DONE")
        self.assertTrue(state["observational_release"])
        self.assertEqual(state["stop_reason"], "observed_regions_verified")
        self.assertIsInstance(state["process_state"], dict)
        self.assertIn("estimate", state["process_state"])
        self.assertIn("last_decision", state["process_state"])
        decision = state["process_state"]["last_decision"]
        self.assertIsNotNone(decision)
        self.assertEqual(decision["normal_offset_mm"], 0.2)
        self.assertLessEqual(
            abs(decision["normal_offset_mm"]), process.TOLERANCE_MM
        )
        segment = state["segments"][0]
        self.assertTrue(segment["preflight"]["parameter_gate_passed"])
        self.assertEqual(segment["params"]["normal_offset_mm"], 0.2)
        self.assertEqual(segment["params"]["passes"], 1)
        self.assertLessEqual(
            abs(
                segment["path"]["material_allowance_mm"]
                + segment["params"]["normal_offset_mm"]
            ),
            process.TOLERANCE_MM,
        )
        self.assertEqual(state["calibration"]["status"], "pass")
        self.assertIs(state["calibration"]["truth_used_for_decision"], False)
        self.assertEqual(state["intervention_count"], 2)
        self.assertEqual(
            {entry["action"] for entry in session.interventions()},
            {"approve_regions", "set_segment_params"},
        )
        reloaded = session_module.load_session(
            session.directory,
            process_adapter=controller.as_session_adapter(),
            reinspection_calibrator=(
                process.LocalBaselineReinspector().as_session_calibrator()
            ),
            backend_factory=_factory("pass"),
        )
        reloaded_state = reloaded.status()
        self.assertEqual(reloaded.stage, "DONE")
        self.assertEqual(
            reloaded_state["process_state"], state["process_state"]
        )
        self.assertEqual(reloaded_state["calibration"], state["calibration"])
        self.assertFalse(reloaded_state["truth_used_for_control"])
        self.assertFalse(reloaded_state["truth_used_for_decision"])

    def test_bias_veto_blocks_p0a_release(self):
        session, _ = self._run("bias_veto", "session_veto")
        state = session.status()
        self.assertFalse(state["observational_release"])
        self.assertEqual(
            state["stop_reason"], "reinspection_calibration_bias_veto"
        )
        self.assertTrue(state["calibration"]["bias_veto"])
        self.assertEqual(state["stage"], "DONE")
        self.assertFalse(state["truth_used_for_decision"])

    def test_hold_and_missing_baseline_block_p0a_release(self):
        for scenario, expected in (
            ("sides_missing", "reinspection_calibration_hold"),
            (
                "baseline_unavailable",
                "reinspection_calibration_baseline_unavailable",
            ),
        ):
            with self.subTest(scenario=scenario):
                session, _ = self._run(scenario, f"session_{scenario}")
                state = session.status()
                self.assertFalse(state["observational_release"])
                self.assertEqual(state["stop_reason"], expected)
                self.assertEqual(state["stage"], "DONE")

    def test_poisoned_truth_fields_do_not_change_plugin_decisions(self):
        session, _ = _create(self.root / "session_poison", "pass")
        _advance_to_planned(session)
        segment = copy.deepcopy(session.segments()[0])
        state = session._observed_context()
        clean_adapter = process.AdaptiveProcessController().as_session_adapter()
        clean = clean_adapter(segment, state)
        poisoned_segment = copy.deepcopy(segment)
        poisoned_segment["truth_used_for_control"] = True
        poisoned_segment["truth_residual_mm"] = [9.9]
        poisoned_segment["path"]["truth_residual_mm"] = [9.9]
        poisoned_state = copy.deepcopy(state)
        poisoned_state["truth_used_for_control"] = True
        poisoned_state["truth_residual_mm"] = [9.9]
        poisoned_state["plant"] = {"rate_scale": 9.9}
        poisoned_adapter = (
            process.AdaptiveProcessController().as_session_adapter()
        )
        poisoned = poisoned_adapter(poisoned_segment, poisoned_state)
        for key in process.SESSION_OVERRIDE_KEYS:
            self.assertEqual(clean[key], poisoned[key])
        self.assertIs(clean["truth_used_for_control"], False)
        self.assertIs(poisoned["truth_used_for_control"], False)
        clean_calibrator = (
            process.LocalBaselineReinspector().as_session_calibrator()
        )
        report = {
            "machined": _anchors(0.10),
            "local_baseline": {
                "baseline": _baseline(0.0),
                "protected": _anchors(0.0, count=8, sigma_mm=0.015),
            },
        }
        clean_calibration = clean_calibrator(report, state)
        poisoned_report = copy.deepcopy(report)
        poisoned_report["truth_used_for_decision"] = True
        poisoned_report["truth_residual_mm"] = [9.9]
        poisoned_report["plant"] = {"rate_scale": 9.9}
        poisoned_calibration = clean_calibrator(poisoned_report, poisoned_state)
        for key in ("status", "bias_mm", "bias_veto", "naive_status"):
            self.assertEqual(clean_calibration[key], poisoned_calibration[key])
        self.assertIs(clean_calibration["truth_used_for_decision"], False)
        self.assertIs(poisoned_calibration["truth_used_for_decision"], False)


class InterventionFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="daiyi-handoff-")
        cls.root = Path(cls.temporary.name)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_interventions_feed_the_dataset_mapper(self):
        session, _ = _create(self.root / "session_interventions", "pass")
        session.step_next()
        session.step_next()  # DETECTED
        session.add_manual_region(
            {
                "polygon": [[0, 0], [10, 0], [10, 10]],
                "view_index": 0,
                "kind": "seam",
                "reason": "handoff manual ROI",
            }
        )
        session.set_segment_params(
            1, feed_scale=0.6, normal_offset_mm=0.05, passes=2
        )
        session.skip_segment(1, reason="operator skip")
        session.mask_false_positive(1, reason="operator mask")
        entries = session.interventions()
        self.assertEqual(
            {entry["action"] for entry in entries},
            {
                "add_manual_region",
                "set_segment_params",
                "skip_segment",
                "mask_false_positive",
            },
        )
        result = dataset_pipeline.interventions_to_hard_examples(entries)
        self.assertEqual(
            result["schema"], "daiyi-intervention-hard-examples-v1"
        )
        self.assertEqual(len(result["requests"]), 4)
        by_action = {
            request["source"]["action"]: request
            for request in result["requests"]
        }
        self.assertEqual(
            by_action["add_manual_region"]["requested_kind"], "manual_roi"
        )
        self.assertEqual(
            by_action["set_segment_params"]["requested_kind"], "thick_stock"
        )
        self.assertEqual(
            by_action["set_segment_params"]["changed"]["passes"]["after"], 2
        )
        self.assertEqual(
            by_action["skip_segment"]["requested_kind"],
            "skipped_segment_review",
        )
        self.assertEqual(
            by_action["mask_false_positive"]["requested_kind"],
            "false_positive_review",
        )
        for request in result["requests"]:
            self.assertIs(request["truth_used_for_control"], False)
            self.assertIsInstance(
                request["source"]["target_id"], (int, type(None))
            )


class WorkpiecePackageHandoffTests(unittest.TestCase):
    def test_p0b_packages_map_identically_into_p1(self):
        for workpiece_id in ("097", "021"):
            with self.subTest(workpiece=workpiece_id):
                p0b = workpiece_packages.load_workpiece_package(workpiece_id)
                p1 = dataset_pipeline.load_workpiece_package(p0b)
                self.assertEqual(p1.workpiece_id, p0b.workpiece_id)
                self.assertEqual(p1.package_id, p0b.workpiece_id)
                self.assertEqual(
                    p1.mesh_path.resolve(), p0b.mesh_path.resolve()
                )
                self.assertEqual(p1.units, p0b.declaration["units"])
                self.assertEqual(
                    [zone["name"] for zone in p1.protection_regions],
                    [zone.name for zone in p0b.protected_zones],
                )
                for record, zone in zip(
                    p1.protection_regions, p0b.protected_zones
                ):
                    np.testing.assert_allclose(
                        np.asarray(record["bounds_m"], float),
                        zone.bounds_m,
                    )
                self.assertEqual(
                    p1.metadata["coordinate_frame"],
                    p0b.declaration["coordinate_frame"],
                )
                self.assertEqual(
                    p1.metadata["fixture_id"],
                    p0b.declaration["fixture_id"],
                )

    def test_staged_plan_keeps_p0b_segment_metadata(self):
        controller = process.AdaptiveProcessController()
        plan = controller.plan_staged_passes(np.full(15, 1.0))
        self.assertEqual(plan.status, "ready")
        segment = {
            "region_id": 3,
            "grasp_setup": "original",
            "split_from": 7,
            "workpiece_id": "097",
        }
        context = process.segment_process_context(segment, plan)
        self.assertEqual(context["region_id"], 3)
        self.assertEqual(context["grasp_setup"], "original")
        self.assertEqual(context["split_from"], 7)
        self.assertEqual(context["workpiece_id"], "097")
        self.assertEqual(context["stage"], plan.passes[0].stage)
        self.assertEqual(
            context["planned_cut_mm"], plan.passes[0].planned_cut_mm
        )
        self.assertEqual(context["planned_passes"], len(plan.passes))
        self.assertIs(context["truth_used_for_control"], False)
        no_split = {"region_id": 4, "grasp_setup": "clock_90_simulation"}
        context = process.segment_process_context(no_split, plan)
        self.assertNotIn("split_from", context)
        self.assertEqual(context["grasp_setup"], "clock_90_simulation")

    def test_real_session_split_segments_keep_split_from(self):
        with tempfile.TemporaryDirectory(prefix="daiyi-handoff-") as root:
            session, _ = _create(Path(root) / "session_split", "pass")
            _advance_to_planned(session)
            session.split_segment(1, 0.5)
            children = [
                segment
                for segment in session.segments()
                if segment.get("split_from") == 1
            ]
            self.assertEqual(len(children), 2)
            plan = process.AdaptiveProcessController().plan_staged_passes(
                np.full(10, 0.8)
            )
            context = process.segment_process_context(children[0], plan)
            self.assertEqual(context["split_from"], 1)
            self.assertEqual(context["grasp_setup"], children[0]["grasp_setup"])
            self.assertEqual(context["region_id"], children[0]["region_id"])
            self.assertEqual(context["planned_passes"], len(plan.passes))


if __name__ == "__main__":
    unittest.main()
