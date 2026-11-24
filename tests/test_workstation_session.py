"""Public resumable-session SDK and local HTTP control-plane regressions."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from daiyi_grinding.workstation_session import (
    MUJOCO_LOCK,
    SessionSafetyError,
    SessionStateError,
    create_session,
    load_session,
)


def inherited_mujoco_lock():
    """Util-linux flock passes its open lock descriptor to the test command."""
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            if os.readlink(descriptor) == str(MUJOCO_LOCK.resolve()):
                return True
        except FileNotFoundError:
            continue
    return False


class ReplayableSimulation:
    """Replace only the expensive simulator boundary with fixed observations."""

    def __init__(self, directory, config, checkpoint):
        self.directory = directory
        self.config = config
        self.completed = (
            json.loads(checkpoint.read_text())["completed"] if checkpoint else []
        )

    def scan(self, output, **options):
        output.mkdir(parents=True)
        (output / "view_00_observation.npz").write_bytes(b"fixed sensor fixture")
        paths = [
            {
                "segment_id": identity,
                "detection_id": identity,
                "kind": "burr",
                "xyz": [[identity * 0.2, 0, 0], [identity * 0.2 + 0.01, 0, 0]],
                "nominal_xyz": [
                    [identity * 0.2, 0, 0],
                    [identity * 0.2 + 0.01, 0, 0],
                ],
                "normal": [0, 0, 1],
                "recipe": {"feed_mm_s": 7},
                "material_allowance_mm": 0.08,
                "machining_required": True,
                "coordinate_frame": "workpiece_097",
            }
            for identity in (1, 2)
            if identity not in self.completed
        ]
        return {
            "detections": [
                {"id": p["detection_id"], "kind": "burr", "machining_required": True}
                for p in paths
            ],
            "paths": paths,
            "registered_regions": [
                {
                    "id": p["detection_id"],
                    "kind": "burr",
                    "points": p["nominal_xyz"],
                    "normals": [[0, 0, 1], [0, 0, 1]],
                }
                for p in paths
            ],
            "protected_points": [],
            "observations": ["view_00_observation.npz"],
            "views": [],
            "perception": {
                "prediction_revision": options["revision"],
                "region_reinspection": {
                    "observational_release": not paths,
                    "automatic_regrind_allowed": True,
                    "regions": [],
                    "truth_used_for_decision": False,
                },
            },
        }

    def plan(self, path):
        return (
            [{"t": 1, "segment_id": path["segment_id"]}],
            [path],
            {"passed": True, "rows": [], "motion_binding": {"fixture": "mock"}},
        )

    def preflight(self, commands):
        return {"passed": True, "sampling_policy": "bounded_joint"}

    def execute(self, commands, **options):
        self.completed.append(commands[0]["segment_id"])
        return {
            "unexpected_contact_count": 0,
            "tracking_trace_p95_mm": 0.1,
            "surface_tracking_p95_mm": 0.1,
            "simulation_steps": 10,
            "engaged_duration_s": 7.0,
            "force_n": 42.0,
            "material_removal_simulated": True,
            "truth_used_for_control": False,
        }

    def safe_retract(self, **options):
        return {"passed": True, "fixture_attached": True, "grasp_attached": False}

    def save_checkpoint(self, output):
        output.write_text(json.dumps({"completed": self.completed}))

    def stock_summary(self):
        return {"completed_mock_segments": list(self.completed)}

    def close(self):
        pass


class ManualRegionSimulation(ReplayableSimulation):
    def manual_region(self, roi_spec, observation_path):
        return {
            "region": {"id": 1, "kind": "burr", "machining_required": True},
            "target": {"id": 1, "kind": "burr", "points": [[0.6, 0, 0], [0.61, 0, 0]], "normals": [[0, 0, 1], [0, 0, 1]]},
            "paths": [{"segment_id": 1, "detection_id": 1, "kind": "burr", "xyz": [[0.6, 0, 0], [0.61, 0, 0]], "nominal_xyz": [[0.6, 0, 0], [0.61, 0, 0]], "normal": [0, 0, 1], "recipe": {"feed_mm_s": 7}, "material_allowance_mm": 0.08, "machining_required": True}],
        }


class BaselineObservationSimulation(ReplayableSimulation):
    def scan(self, output, **options):
        result = super().scan(output, **options)
        report = result["perception"]["region_reinspection"]
        anchors = {"heights_mm": [0.01, 0.02], "sigma_mm": [0.01, 0.01],
                   "points_m": [[-0.1, 0, 0], [0.1, 0, 0]], "sides": [-1, 1],
                   "evaluation_stock": "poison"}
        report.update(local_baseline={"baseline": anchors, "protected": anchors}, baseline_anchors=anchors, machined=anchors)
        return result


class ResidualSimulation(ReplayableSimulation):
    def scan(self, output, **options):
        result = super().scan(output, **options)
        if 1 in self.completed and 2 in self.completed:
            result["detections"] = [{"id": 1, "kind": "burr", "machining_required": True}]
            result["registered_regions"] = [{"id": 1, "kind": "burr", "points": [[0.2, 0, 0], [0.21, 0, 0]], "normals": [[0, 0, 1], [0, 0, 1]]}]
            result["paths"] = [{"segment_id": 1, "detection_id": 1, "kind": "burr", "xyz": [[0.2, 0, 0], [0.21, 0, 0]], "nominal_xyz": [[0.2, 0, 0], [0.21, 0, 0]], "normal": [0, 0, 1], "recipe": {"feed_mm_s": 7}, "material_allowance_mm": 0.08, "machining_required": True}]
            result["perception"]["region_reinspection"].update(observational_release=False, needs_rework=1)
        return result


class PausableSimulation(ReplayableSimulation):
    started = threading.Event()
    proceed = threading.Event()

    def execute(self, commands, **options):
        self.started.set()
        if not self.proceed.wait(timeout=10):
            raise RuntimeError("test fixture was not released")
        if options["stop_requested"]():
            return {"interrupted": True, "resume_from_point": 0}
        return super().execute(commands, **options)


class PoisonedEvaluationSimulation(ReplayableSimulation):
    def scan(self, output, **options):
        result = super().scan(output, **options)
        result["perception"].update(metrics={"f1": -100}, generated_defects=999999)
        for path in result["paths"]:
            path["evaluation_stock"] = "must not enter adapter input"
        return result

    def stock_summary(self):
        return {**super().stock_summary(), "evaluation_stock": 99999,
                "private_defects": [{"truth": "poison"}]}


class SessionCheckpointTests(unittest.TestCase):
    def test_create_and_reload_preserve_identity_and_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session"
            session = create_session(output, session_id="session-test", seed=42)
            restored = load_session(output)
            self.assertEqual(restored.status()["session_id"], "session-test")
            self.assertEqual(restored.status()["stage"], "IDLE")
            self.assertEqual(restored.status()["config"]["seed"], 42)
            self.assertEqual(session.status(), restored.status())
            adopted = create_session(output, session_id="session-test", seed=99)
            self.assertEqual(adopted.status()["config"]["seed"], 42)

    def test_stage_progression_and_reload_do_not_repeat_executed_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session"
            session = create_session(output, backend_factory=ReplayableSimulation)
            self.assertEqual(session.step_next()["stage"], "SCANNED")
            self.assertEqual(session.step_next()["stage"], "DETECTED")
            session.approve_regions([1, 2])
            self.assertEqual(session.step_next()["stage"], "PLANNED")
            self.assertEqual(session.step_next()["stage"], "EXECUTING")
            session = load_session(output, backend_factory=ReplayableSimulation)
            self.assertEqual(session.status()["executed_segments"], [1])
            session.step_next()
            self.assertEqual(session.status()["executed_segments"], [1, 2])
            self.assertEqual(session.step_next()["stage"], "RESCANNED")
            self.assertEqual(session.step_next()["stage"], "DONE")
            self.assertEqual(
                session.status()["stock"]["completed_mock_segments"], [1, 2]
            )
            self.assertFalse(session.status()["truth_used_for_control"])
            self.assertFalse(session.status()["truth_used_for_decision"])

    def test_paused_parameter_edit_is_rechecked_and_unsafe_offset_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session"
            session = create_session(output, backend_factory=ReplayableSimulation)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            session.step_next()
            session.step_next()
            session.pause()
            session.set_segment_params(2, feed_scale=0.75, normal_offset_mm=-1)
            self.assertTrue(session.segments()[1]["needs_preflight"])
            session.resume()
            with self.assertRaises(SessionSafetyError):
                session.step_next()
            restored = load_session(output, backend_factory=ReplayableSimulation)
            self.assertEqual(restored.stage, "STOPPED_SAFETY")
            self.assertEqual(restored.status()["executed_segments"], [1])
            self.assertFalse(restored.segments()[1]["preflight"]["passed"])
            restored.set_segment_params(2, normal_offset_mm=0)
            restored.resume()
            restored.step_next()
            self.assertEqual(restored.status()["executed_segments"], [1, 2])
            edits = [i for i in restored.interventions() if i["action"] == "set_segment_params"]
            self.assertEqual(edits[0]["diff"]["normal_offset_mm"], {"before": 0, "after": -1})
            self.assertEqual(edits[0]["stage"], "PAUSED")

    def test_mask_skip_split_and_grasp_changes_keep_auditable_segment_history(self):
        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=ReplayableSimulation)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            session.mask_false_positive(1, reason="人工确认反光误检")
            session.split_segment(2, ratio=0.4)
            session.switch_grasp("opposite_simulation", segment_id=4)
            session.skip_segment(3, reason="暂不加工左半段")
            self.assertEqual(session.segments()[1]["status"], "superseded")
            self.assertEqual(session.segments()[3]["split_from"], 2)
            self.assertEqual(session.segments()[3]["grasp_setup"], "opposite_simulation")
            self.assertTrue(session.segments()[3]["needs_preflight"])
            session.replan_remaining()
            session.step_next()
            self.assertEqual(session.status()["executed_segments"], [4])
            self.assertEqual(session.segments()[0]["status"], "skipped")
            self.assertEqual(session.segments()[2]["status"], "skipped")
            actions = {entry["action"] for entry in session.interventions()}
            self.assertTrue({"mask_false_positive", "split_segment", "switch_grasp", "skip_segment", "replan_remaining"}.issubset(actions))
            mask = next(i for i in session.interventions() if i["action"] == "mask_false_positive")
            self.assertEqual(mask["reason"], "人工确认反光误检")

    def test_real_097_scan_exports_a_reloadable_simulation_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session"
            session = create_session(output, case="normal", views="single", mujoco_lock_held=inherited_mujoco_lock())
            state = session.step_next()
            self.assertEqual(state["stage"], "SCANNED")
            self.assertTrue((output / state["material_checkpoint"]).is_file())
            self.assertEqual(len(state["observation_snapshots"]), 1)
            restored = load_session(output, mujoco_lock_held=inherited_mujoco_lock())
            self.assertEqual(restored.step_next()["stage"], "DETECTED")
            self.assertFalse(restored.status()["enterprise_measured"])
            self.assertEqual(restored.status()["workpiece_id"], "097")

    def test_active_execution_remains_queryable_and_pause_has_one_owner(self):
        PausableSimulation.started.clear()
        PausableSimulation.proceed.clear()
        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=PausableSimulation)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            session.step_next()
            errors = []

            def advance():
                try:
                    session.step_next()
                except Exception as error:
                    errors.append(error)

            worker = threading.Thread(target=advance)
            worker.start()
            try:
                self.assertTrue(PausableSimulation.started.wait(timeout=10))
                self.assertEqual(session.status()["stage"], "EXECUTING")
                with self.assertRaises(SessionStateError):
                    session.step_next()
                paused = session.pause()
                self.assertTrue(paused["pause_requested"])
            finally:
                PausableSimulation.proceed.set()
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(session.stage, "PAUSED")
            self.assertTrue(session.status()["safe_retracted"])
            session.resume()
            session.step_next()
            self.assertEqual(session.status()["executed_segments"], [1])

    def test_plugin_state_and_bias_veto_are_restored_without_evaluation_inputs(self):
        decisions = []
        with tempfile.TemporaryDirectory() as directory:
            for name, factory in (("clean", ReplayableSimulation), ("poison", PoisonedEvaluationSimulation)):
                received = []

                def adapt(segment, context):
                    received.append(json.dumps([segment, context]))
                    return {"feed_scale": 0.75, "process_state": {"proposals": context["process_state"].get("proposals", 0) + 1}}

                def calibrate(report, context):
                    received.append(json.dumps([report, context]))
                    return {"bias_veto": True, "recommended_action": "recalibrate", "truth_used_for_decision": False}

                output = Path(directory) / name
                session = create_session(output, backend_factory=factory, process_adapter=adapt, reinspection_calibrator=calibrate)
                session.step_next()
                session.step_next()
                session.approve_regions([1, 2])
                session.step_next()
                session.step_next()
                self.assertEqual(session.status()["process_state"], {"proposals": 1})
                without_plugin = load_session(output, backend_factory=factory)
                with self.assertRaises(SessionStateError):
                    without_plugin.step_next()
                session = load_session(output, backend_factory=factory, process_adapter=adapt, reinspection_calibrator=calibrate)
                session.step_next()
                session.step_next()
                session.step_next()
                self.assertFalse(session.status()["observational_release"])
                self.assertTrue(session.status()["calibration"]["bias_veto"])
                self.assertEqual(session.status()["stop_reason"], "reinspection_calibration_bias_veto")
                self.assertEqual(session.status()["process_state"], {"proposals": 2})
                for data in received:
                    for forbidden in ("evaluation_stock", "private_defects", "generated_defects", '"metrics"'):
                        self.assertNotIn(forbidden, data)
                decisions.append(session.status()["executed_segments"])
            self.assertEqual(decisions, [[1, 2], [1, 2]])

    def test_manual_roi_rescan_and_pass_parameters_are_saved_and_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=ManualRegionSimulation)
            session.step_next()
            session.step_next()
            session.add_manual_region({"view_index": 0, "polygon": [[10, 10], [30, 10], [30, 30], [10, 30]], "kind": "burr"})
            manual = session.status()["regions"][-1]
            self.assertTrue(manual["manual"])
            self.assertEqual(manual["approval"], "pending")
            session.approve_regions([1, 2, 3])
            session.set_segment_params(1, passes=2, dwell_scale=1.25)
            session.rescan_views(angles_deg=[0, 60], extra_frames=2)
            self.assertEqual(len(session.status()["scans"]), 2)
            self.assertTrue(all(segment["needs_preflight"] for segment in session.segments()))
            session.replan_remaining()
            session.step_next()
            self.assertEqual(session.segments()[0]["completed_passes"], 1)
            self.assertEqual(session.segments()[0]["status"], "pending")
            self.assertTrue(session.segments()[0]["needs_preflight"])
            session.step_next()
            self.assertEqual(session.segments()[0]["completed_passes"], 2)
            self.assertEqual(session.status()["stock"]["completed_mock_segments"], [1, 1])
            self.assertTrue(any(i["action"] == "add_manual_region" for i in session.interventions()))
            self.assertTrue(any(i["action"] == "rescan_views" for i in session.interventions()))
            with self.assertRaises(ValueError):
                session.rescan_views(angles_deg=[float("nan")])

    def test_observed_regrind_uses_registered_identity_and_stops_at_pass_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=ResidualSimulation, max_passes=2)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            for _ in range(12):
                if session.stage == "DONE":
                    break
                session.step_next()
            self.assertEqual(session.stage, "DONE")
            self.assertEqual(session.status()["executed_segments"], [1, 2, 3])
            self.assertEqual(session.segments()[-1]["region_id"], 1)
            self.assertEqual(session.status()["stop_reason"], "pass_budget_exhausted")
            self.assertFalse(session.status()["observational_release"])
            self.assertFalse(session.status()["truth_used_for_control"])

    def test_explicit_replanning_after_rescan_populates_only_observed_remaining_work(self):
        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=ResidualSimulation, max_passes=2)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            session.step_next()
            session.step_next()
            session.step_next()
            self.assertEqual(session.step_next()["stage"], "RESCANNED")
            self.assertEqual(session.replan_remaining()["stage"], "PLANNED")
            self.assertEqual(session.status()["remaining_segments"], [3])
            self.assertEqual(session.status()["round_index"], 1)

    def test_every_nonpassing_calibration_status_blocks_native_release(self):
        with tempfile.TemporaryDirectory() as directory:
            for status in ("hold", "baseline_unavailable", "positive_excess", "overgrind", "uncertain"):
                with self.subTest(status=status):
                    session = create_session(Path(directory) / status, backend_factory=ReplayableSimulation, max_passes=1,
                                             reinspection_calibrator=lambda report, context: {"status": status, "bias_veto": False})
                    session.step_next()
                    session.step_next()
                    session.approve_regions([1, 2])
                    for _ in range(5):
                        if session.stage != "DONE":
                            session.step_next()
                    self.assertFalse(session.status()["observational_release"])
                    self.assertEqual(session.status()["stop_reason"], "reinspection_calibration_" + status)

    def test_actual_p1_adapter_zero_pass_and_evidence_persist_without_motion(self):
        from daiyi_grinding.adaptive_process import AdaptiveProcessController, SessionProcessAdapter

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "session"
            adapter = SessionProcessAdapter(AdaptiveProcessController())
            session = create_session(output, backend_factory=ReplayableSimulation, process_adapter=adapter)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            session.step_next()
            session.step_next()
            self.assertEqual(session.segments()[0]["status"], "skipped")
            self.assertEqual(session.status()["execution_history"], [])
            evidence = session.status()["process_state"]
            self.assertTrue(evidence)
            self.assertFalse(evidence["truth_used_for_control"])
            self.assertEqual(load_session(output, backend_factory=ReplayableSimulation).status()["process_state"], evidence)

    def test_local_baseline_geometry_and_measured_duration_reach_calibrator(self):
        received = []

        def calibrate(report, context):
            received.append((report, context))
            return {"status": "hold", "bias_veto": False}

        with tempfile.TemporaryDirectory() as directory:
            session = create_session(Path(directory) / "session", backend_factory=BaselineObservationSimulation,
                                     reinspection_calibrator=calibrate)
            session.step_next()
            session.step_next()
            session.approve_regions([1, 2])
            for _ in range(4):
                session.step_next()
            report, context = received[-1]
            for key in ("local_baseline", "baseline_anchors", "machined"):
                self.assertIn(key, report)
            self.assertEqual(report["machined"]["points_m"], [[-0.1, 0, 0], [0.1, 0, 0]])
            self.assertEqual(context["local_baseline"]["baseline"]["sides"], [-1, 1])
            self.assertEqual(context["execution_history"][-1]["engaged_duration_s"], 7)
            self.assertEqual(context["execution_history"][-1]["force_n"], 42)
            self.assertEqual(context["execution_history"], context["execution_feedback"])
            self.assertNotIn("evaluation_stock", json.dumps(received))


if __name__ == "__main__":
    unittest.main()
