"""Offline controller plan binding, feedback ordering and recovery tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from daiyi_grinding.offline_controller import (
    CANONICAL_UNITS,
    DEFAULT_ROBOT_XML,
    FEEDBACK_SCHEMA,
    QUALITY_SCHEMA,
    CompletionRejected,
    FeedbackRejected,
    MockController,
    MockFeedbackSource,
    PlanRejected,
    RecoveryRejected,
    StateRejected,
    load_plan,
    load_plan_from_run,
    load_joint_limits,
    run_mock_cycle,
    sha256_file,
    write_case_files,
)

HISTORICAL = (
    Path(__file__).resolve().parents[1]
    / "runs/daiyi_full_chain_20261122/final/thin_120483"
)
BASE_TIME = 1_800_000_000.0


class Clock:
    """A deterministic wall clock used only by the offline mock."""

    def __init__(self, now=BASE_TIME):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def identity(serial="mock-serial-01", units=None):
    return {
        "part_id": "097",
        "serial": serial,
        "cad_revision": "097-cad-revision",
        "fixture_id": "fixture-A",
        "fixture_revision": "fixture-rev-01",
        "tool_id": "belt",
        "frame": "workpiece_097",
        "units": dict(units or CANONICAL_UNITS),
    }


def command(
    index,
    *,
    t,
    segment_id,
    phase,
    attached,
    point,
    detection_id,
    q=None,
):
    values = list(q or [0.5, -0.1, 0.8, 2.0, 0.5, -2.0])
    values[0] += 0.001 * index
    return {
        "t": t,
        "q": values,
        "pose": [
            [1.0, 0.0, 0.0, 0.1],
            [0.0, 1.0, 0.0, 0.2],
            [0.0, 0.0, 1.0, 0.3],
            [0.0, 0.0, 0.0, 1.0],
        ],
        "phase": phase,
        "attached": attached,
        "point": point,
        "detection_id": detection_id,
        "segment_id": segment_id,
    }


def build_case(
    directory,
    *,
    blocked_segment=None,
    segments=3,
    commands_per_segment=3,
    valid_for_s=3600.0,
    issued_at_s=BASE_TIME,
    include_unapproved_preview=True,
    command_overrides=None,
    load_plan_now=True,
):
    directory = Path(directory)
    commands = [
        command(
            0,
            t=0.1,
            segment_id=0,
            phase="transfer",
            attached=False,
            point=None,
            detection_id=0,
        ),
        command(
            1,
            t=0.2,
            segment_id=0,
            phase="home",
            attached=False,
            point=None,
            detection_id=0,
        ),
    ]
    for segment in range(1, segments + 1):
        if segment == blocked_segment:
            continue
        for offset in range(commands_per_segment):
            phase = (
                "approach" if offset == 0
                else "retract" if offset == commands_per_segment - 1
                else "trace"
            )
            commands.append(
                command(
                    len(commands),
                    t=1.0 + 0.5 * segment + 0.05 * offset,
                    segment_id=segment,
                    phase=phase,
                    attached=True,
                    point=[0.001, 0.002, 0.003],
                    detection_id=segment,
                )
            )
    if command_overrides is not None:
        command_overrides(commands)
    rows = []
    for segment in range(1, segments + 1):
        rows.append(
            {
                "segment_id": segment,
                "detection_id": segment,
                "kind": "seam",
                "point_count": commands_per_segment,
                "status": (
                    "blocked" if segment == blocked_segment else "passed"
                ),
                "coverage": 0.0 if segment == blocked_segment else 1.0,
                "reason": (
                    "motion preflight blocked"
                    if segment == blocked_segment
                    else "complete segment passed"
                ),
            }
        )
    preflight = {
        "passed": blocked_segment is None,
        "checked_commands": len(commands),
        "contact_count": 0 if blocked_segment is None else 1,
        "rows": rows,
        "scope": "synthetic test preflight for the exact command list",
    }
    preview = []
    for segment in range(1, segments + 1):
        preview.append(
            {
                "segment": segment,
                "detection_id": segment,
                "view": 0,
                "frame": "workpiece_097",
                "x_mm": 1.0 * segment,
                "y_mm": 2.0,
                "z_mm": 3.0,
                "nx": 0.0,
                "ny": 0.0,
                "nz": 1.0,
                "tool": "belt",
            }
        )
    if include_unapproved_preview:
        preview.append(
            {
                "segment": 99,
                "detection_id": 99,
                "view": 0,
                "frame": "workpiece_097",
                "x_mm": 4.0,
                "y_mm": 5.0,
                "z_mm": 6.0,
                "nx": 0.0,
                "ny": 0.0,
                "nz": 1.0,
                "tool": "belt",
            }
        )
    result = {
        "schema_version": 3,
        "scene": "workstation",
        "cad_workpiece": "097",
        "coordinate_frame": "workpiece_097",
        "tool": {"key": "belt"},
        "grasp_setup": "original",
        "fixture_loading_yaw_deg": 180,
        "workpiece_profile": {"sha256": "profile-sha-for-test"},
        "task_status": "partial",
        "goal_complete": False,
        "observational_release": False,
        "motion_gate_passed": True,
        "path_count": segments,
        "admitted_path_count": segments - (1 if blocked_segment else 0),
    }
    write_case_files(
        directory,
        commands=commands,
        preflight=preflight,
        preview_rows=preview,
        result=result,
    )
    values = identity()
    if not load_plan_now:
        return None, values
    plan = load_plan(
        directory,
        identity=values,
        issued_at_s=issued_at_s,
        expires_at_s=issued_at_s + valid_for_s,
        require_preview=True,
    )
    return plan, values


def quality(
    plan, *, status="complete", covered=None, independent=True, controller=None
):
    covered = list(plan.admitted_segments if covered is None else covered)
    suffix = plan.plan_sha256[:16]
    binding = (
        controller.quality_binding() if controller is not None else {
            **plan.identity.to_dict(),
            "plan_sha256": plan.plan_sha256,
            "request_id": f"mock-request-{suffix}",
            "execution_id": f"mock-execution-{suffix}-01",
            "execution_attempt": 1,
        }
    )
    return {
        **binding,
        "schema": QUALITY_SCHEMA,
        "source_kind": "mock_synthetic_quality",
        "independent": independent,
        "real_evidence": False,
        "task_status": status,
        "covered_segments": covered,
        "segment_results": {str(segment): "pass" for segment in covered},
        "observational_release": status == "complete",
        "goal_complete": status == "complete",
        "scope": "independent offline quality fixture, not real measurement",
    }


def batches(plan, *, batch_size=2):
    source = MockFeedbackSource(
        plan,
        f"mock-request-{plan.plan_sha256[:16]}",
        f"mock-execution-{plan.plan_sha256[:16]}-01",
        batch_size=batch_size,
    )
    return list(source)


class HistoricalRunTests(unittest.TestCase):
    def test_historical_run_is_validated_read_only(self):
        files = [
            HISTORICAL / "motion_commands.json",
            HISTORICAL / "preflight.json",
            HISTORICAL / "estun_path_preview.csv",
            HISTORICAL / "result.json",
        ]
        before = {path.name: sha256_file(path) for path in files}
        plan = load_plan_from_run(HISTORICAL, valid_for_s=30 * 86400)
        after = {path.name: sha256_file(path) for path in files}
        self.assertEqual(before, after)
        self.assertEqual(len(plan.commands), 1159)
        self.assertEqual(plan.admitted_segments, (1, 2, 3, 4, 5, 6))
        self.assertEqual(plan.blocked_segments, ())
        self.assertAlmostEqual(plan.duration_s, 205.09575, places=4)
        self.assertEqual(plan.preview["rows"], 84)
        self.assertFalse(plan.preview["executable"])
        self.assertEqual(plan.preview["role"], "candidate_path_preview")
        roles = {artifact.role: artifact for artifact in plan.artifacts}
        self.assertTrue(roles["accepted_motion_commands"].executable)
        self.assertFalse(roles["motion_preflight"].executable)
        self.assertFalse(roles["candidate_path_preview"].executable)
        self.assertEqual(
            plan.limits.source_sha256,
            sha256_file(DEFAULT_ROBOT_XML),
        )
        self.assertEqual(plan.result_evidence["task_status"], "partial")
        self.assertEqual(len(plan.plan_sha256), 64)
        checked = plan.revalidate(
            now_s=plan.issued_at_s + 100, check_expiry=False
        )
        self.assertGreaterEqual(checked["checked_artifacts"], 5)

    def test_historical_mock_cycle_is_execution_complete_only(self):
        plan = load_plan_from_run(HISTORICAL, valid_for_s=30 * 86400)
        with tempfile.TemporaryDirectory() as directory:
            result = run_mock_cycle(
                plan,
                Path(directory) / "cycle",
                batch_size=64,
            )
            controller = result["controller"]
            report = controller.completion_report()
            self.assertEqual(controller.state, "FINISHED")
            self.assertTrue(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
            self.assertEqual(report["missing_command_count"], 0)
            self.assertEqual(report["blocked_source_segments"], [])
            self.assertIn("quality_not_complete", report["reasons"])
            receipt = json.loads(
                (Path(directory) / "cycle/controller_receipt.json").read_text()
            )
            self.assertEqual(receipt["source_kind"], "synthetic")
            self.assertFalse(receipt["mock_identity"]["real_controller"])
            self.assertTrue(receipt["real_acceptance_pending"])
            self.assertTrue(receipt["unexpected_contacts"] == 0)


class PlanValidationTests(unittest.TestCase):
    def test_exact_preflight_binding_and_complete_segment_are_required(self):
        for change, code in (
            ("command", "preflight_binding_mismatch"),
            ("coverage", "partial_preflight"),
            ("detection", "preflight_detection_mismatch"),
            ("retract", "incomplete_machining_segment"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                build_case(directory)
                commands_path = Path(directory) / "motion_commands.json"
                preflight_path = Path(directory) / "preflight.json"
                commands = json.loads(commands_path.read_text())
                preflight = json.loads(preflight_path.read_text())
                if change == "command":
                    commands[2]["q"][0] += 0.01
                    commands_path.write_text(json.dumps(commands))
                elif change == "coverage":
                    preflight["rows"][0]["coverage"] = 0.99
                    preflight_path.write_text(json.dumps(preflight))
                elif change == "detection":
                    preflight["rows"][0]["detection_id"] = 99
                    preflight_path.write_text(json.dumps(preflight))
                else:
                    commands[4]["phase"] = "trace"
                    write_case_files(directory, commands=commands, preflight=preflight)
                with self.assertRaises(PlanRejected) as caught:
                    load_plan(
                        directory, identity=identity(), issued_at_s=BASE_TIME,
                        expires_at_s=BASE_TIME + 3600,
                    )
                self.assertEqual(caught.exception.code, code)

    def test_robot_limits_require_explicit_radians(self):
        for angle in ("degree", ""):
            with self.subTest(angle=angle), tempfile.TemporaryDirectory() as directory:
                content = DEFAULT_ROBOT_XML.read_text().replace(
                    'angle="radian"', f'angle="{angle}"'
                )
                path = Path(directory) / "scene.xml"
                path.write_text(content)
                with self.assertRaises(PlanRejected) as caught:
                    load_joint_limits(path)
                self.assertEqual(caught.exception.code, "robot_unit_mismatch")

    def test_runtime_commands_cannot_diverge_from_plan_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            altered = replace(plan.commands[2], q=(0.7, -0.1, 0.8, 2.0, 0.5, -2.0))
            plan.commands = (*plan.commands[:2], altered, *plan.commands[3:])
            controller = MockController(plan, Path(directory) / "controller", clock=Clock())
            with self.assertRaises(PlanRejected) as caught:
                controller.confirm("operator")
            self.assertEqual(caught.exception.code, "plan_hash_mismatch")
            self.assertEqual(controller.state, "HELD")

    def test_plan_hash_timeline_joint_and_units_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, values = build_case(directory)
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=values,
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                    expected_plan_sha256="0" * 64,
                )
            self.assertEqual(caught.exception.code, "plan_hash_mismatch")
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(units={**CANONICAL_UNITS, "point": "mm"}),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "unit_mismatch")
            self.assertEqual(
                plan.manifest["execution_source"][
                    "candidate_preview_executable"
                ],
                False,
            )

    def test_timeline_order_and_joint_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:

            def duplicate_time(commands):
                commands[3]["t"] = commands[2]["t"]

            build_case(
                directory,
                command_overrides=duplicate_time,
                load_plan_now=False,
            )
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "timeline_order")
        with tempfile.TemporaryDirectory() as directory:

            def bad_joint(commands):
                commands[2]["q"][2] = 9.0

            build_case(
                directory,
                command_overrides=bad_joint,
                load_plan_now=False,
            )
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "joint_limit")

    def test_nonfinite_and_invalid_pose_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            build_case(directory)
            path = Path(directory) / "motion_commands.json"
            values = json.loads(path.read_text())
            values[2]["point"][0] = float("nan")
            path.write_text(
                json.dumps(values, ensure_ascii=False, allow_nan=True)
            )
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "nonfinite_json")
        with tempfile.TemporaryDirectory() as directory:

            def bad_pose(commands):
                commands[2]["pose"][3][0] = 1.0

            build_case(
                directory,
                command_overrides=bad_pose,
                load_plan_now=False,
            )
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "invalid_pose")

    def test_only_complete_preflight_pass_segments_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            build_case(directory)
            path = Path(directory) / "preflight.json"
            value = json.loads(path.read_text())
            value["rows"][2]["status"] = "blocked"
            path.write_text(json.dumps(value, ensure_ascii=False))
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "unapproved_segment")
        with tempfile.TemporaryDirectory() as directory:
            build_case(directory)
            path = Path(directory) / "preflight.json"
            value = json.loads(path.read_text())
            value["rows"].append(
                {
                    "segment_id": 9,
                    "status": "passed",
                    "reason": "no command block",
                }
            )
            path.write_text(json.dumps(value, ensure_ascii=False))
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(
                caught.exception.code,
                "preflight_passed_segment_missing_commands",
            )

    def test_csv_and_missing_sources_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            preview = Path(directory) / "estun_path_preview.csv"
            preview.write_bytes(preview.read_bytes() + b"corrupt\n")
            with self.assertRaises(PlanRejected) as caught:
                plan.revalidate(now_s=BASE_TIME + 1)
            self.assertEqual(caught.exception.code, "source_hash_mismatch")
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            with self.assertRaises(PlanRejected):
                controller.confirm("operator")
            self.assertEqual(controller.state, "HELD")
            self.assertEqual(controller.fault_code, "source_hash_mismatch")
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            (Path(directory) / "preflight.json").unlink()
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            with self.assertRaises(PlanRejected) as caught:
                controller.confirm("operator")
            self.assertEqual(caught.exception.code, "missing_source")
            self.assertEqual(controller.state, "HELD")
        with tempfile.TemporaryDirectory() as directory:
            build_case(directory, include_unapproved_preview=False)
            (Path(directory) / "estun_path_preview.csv").unlink()
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                )
            self.assertEqual(caught.exception.code, "missing_source")

    def test_csv_cannot_be_used_as_motion_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            build_case(directory)
            with self.assertRaises(PlanRejected) as caught:
                load_plan(
                    directory,
                    identity=identity(),
                    issued_at_s=BASE_TIME,
                    expires_at_s=BASE_TIME + 3600,
                    require_preview=True,
                    commands_path=Path(directory) / "estun_path_preview.csv",
                )
            self.assertEqual(caught.exception.code, "invalid_json")


class StateMachineTests(unittest.TestCase):
    def test_rejected_batch_is_atomic_and_can_recover_from_trusted_log(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(plan, Path(directory) / "controller", clock=clock)
            controller.confirm("operator")
            controller.start()
            batch = batches(plan)[0]
            batch["samples"][1]["status"] = "invalid"
            with self.assertRaises(FeedbackRejected) as caught:
                controller.ingest_feedback(batch)
            self.assertEqual(caught.exception.code, "invalid_feedback_status")
            self.assertEqual(controller.covered_commands, set())
            self.assertEqual(controller.segment_coverage, {})
            self.assertEqual(controller.feedback_path.read_text(), "")
            self.assertEqual(controller.last_sequence, 0)
            resumed = MockController.resume(controller.checkpoint_path, plan, clock=clock)
            self.assertEqual(resumed.covered_commands, set())
            self.assertEqual(resumed.state, "HELD")
            resumed.recover(
                explicit_mock_recovery=True, operator="operator", acknowledge_hold=True
            )
            resumed.confirm("operator")
            resumed.start()
            resumed.run_mock(MockFeedbackSource(plan, resumed.request_id, resumed.execution_id))
            resumed.set_quality_evidence(quality(plan, controller=resumed))
            self.assertTrue(resumed.finish()["task_quality_complete"])

    def test_invalid_numbers_sequence_and_missing_feedback_hold(self):
        for change, code in (
            ("sequence_gap", "feedback_sequence_gap"),
            ("command_gap", "feedback_command_gap"),
            ("boolean_sequence", "invalid_integer"),
            ("missing_q", "invalid_vector"),
            ("boolean_completed", "invalid_feedback"),
            ("nan", "invalid_feedback"),
            ("early_time", "feedback_command_time_mismatch"),
            ("time_regression", "feedback_time_regression"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                controller = MockController(plan, Path(directory) / "controller", clock=Clock())
                controller.confirm("operator")
                controller.start()
                batch = batches(plan)[0]
                if change == "sequence_gap":
                    batch["sequence"] = 2
                elif change == "command_gap":
                    batch["samples"] = batch["samples"][1:]
                elif change == "boolean_sequence":
                    batch["sequence"] = True
                elif change == "missing_q":
                    batch["samples"][0].pop("q_actual")
                elif change == "boolean_completed":
                    batch["completed"] = 1
                elif change == "nan":
                    batch["samples"][0]["q_actual"][0] = float("nan")
                elif change == "early_time":
                    batch["samples"][0]["t_s"] = 0.01
                else:
                    batch["samples"][1]["t_s"] = batch["samples"][0]["t_s"]
                with self.assertRaises(FeedbackRejected) as caught:
                    controller.ingest_feedback(batch)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(controller.state, "HELD")
                self.assertEqual(controller.feedback_batches, 0)
                self.assertEqual(controller.covered_commands, set())

    def test_clock_regression_dropout_and_stream_end_hold(self):
        for change, code in (
            ("clock", "controller_clock_regression"),
            ("dropout", "feedback_timeout"),
            ("stream_end", "feedback_stream_ended"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                clock = Clock()
                controller = MockController(plan, Path(directory) / "controller", clock=clock)
                controller.confirm("operator")
                controller.start()
                source = batches(plan)
                controller.ingest_feedback(source[0])
                if change == "stream_end":
                    result = controller.run_mock([])
                    self.assertEqual(result["error"]["code"], code)
                else:
                    clock.advance(-1.0 if change == "clock" else 6.0)
                    with self.assertRaises((PlanRejected, FeedbackRejected)) as caught:
                        controller.check_feedback_timeout()
                    self.assertEqual(caught.exception.code, code)
                self.assertEqual(controller.state, "HELD")
                self.assertFalse(controller.completion_report()["execution_complete"])

    def test_active_command_sources_and_program_remain_hash_bound(self):
        for name, code in (
            ("motion_commands.json", "source_hash_mismatch"),
            ("preflight.json", "source_hash_mismatch"),
            ("estun_path_preview.csv", "source_hash_mismatch"),
            ("mock_program.json", "program_hash_mismatch"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                controller = MockController(plan, Path(directory) / "controller", clock=Clock())
                controller.confirm("operator")
                controller.start()
                source = batches(plan)
                controller.ingest_feedback(source[0])
                path = (
                    controller.workdir / name if name == "mock_program.json"
                    else Path(directory) / name
                )
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaises(PlanRejected) as caught:
                    controller.ingest_feedback(source[1])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(controller.state, "HELD")
                self.assertEqual(controller.covered_commands, {0, 1})

    def test_checkpoint_progress_is_derived_from_full_log(self):
        for field, value, code in (
            ("last_sequence", 999, "checkpoint_feedback_mismatch"),
            ("last_t_s", 999.0, "checkpoint_feedback_mismatch"),
            ("last_command_index", 10, "checkpoint_feedback_mismatch"),
            ("completion_claimed", True, "checkpoint_feedback_mismatch"),
            ("covered_commands", [0, True], "checkpoint_feedback_mismatch"),
            ("execution_attempts", 8, "checkpoint_event_mismatch"),
            ("state", "FINISHED", "checkpoint_event_mismatch"),
            ("last_feedback_clock_s", BASE_TIME - 1, "checkpoint_event_mismatch"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                clock = Clock()
                controller = MockController(plan, Path(directory) / "controller", clock=clock)
                controller.confirm("operator")
                controller.start()
                controller.ingest_feedback(batches(plan)[0])
                checkpoint = json.loads(controller.checkpoint_path.read_text())
                checkpoint[field] = value
                controller.checkpoint_path.write_text(json.dumps(checkpoint))
                with self.assertRaises(StateRejected) as caught:
                    MockController.resume(controller.checkpoint_path, plan, clock=clock)
                self.assertEqual(caught.exception.code, code)

    def test_resume_checks_log_and_program_payload_after_hash_rebinding(self):
        for change, code in (
            ("feedback", "invalid_feedback_log"),
            ("program", "program_hash_mismatch"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                clock = Clock()
                controller = MockController(plan, Path(directory) / "controller", clock=clock)
                controller.confirm("operator")
                controller.start()
                controller.ingest_feedback(batches(plan)[0])
                checkpoint = json.loads(controller.checkpoint_path.read_text())
                path = controller.feedback_path if change == "feedback" else controller.program_path
                payload = json.loads(path.read_text())
                payload["serial" if change == "feedback" else "plan_sha256"] = "other"
                path.write_text(json.dumps(payload) + "\n")
                checkpoint[
                    "feedback_sha256" if change == "feedback" else "program_sha256"
                ] = sha256_file(path)
                controller.checkpoint_path.write_text(json.dumps(checkpoint))
                with self.assertRaises(StateRejected) as caught:
                    MockController.resume(controller.checkpoint_path, plan, clock=clock)
                self.assertEqual(caught.exception.code, code)

    def test_unstarted_resume_is_held_and_receipts_match_final_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(plan, Path(directory) / "controller", clock=clock)
            resumed = MockController.resume(controller.checkpoint_path, plan, clock=clock)
            self.assertEqual(resumed.state, "HELD")
            self.assertEqual(resumed.feedback_batches, 0)
            with self.assertRaises(StateRejected):
                resumed.start()
            resumed.recover(
                explicit_mock_recovery=True, operator="operator", acknowledge_hold=True
            )
            resumed.confirm("operator")
            resumed.start()
            resumed.run_mock(MockFeedbackSource(plan, resumed.request_id, resumed.execution_id))
            resumed.finish()
            terminal = MockController.resume(resumed.checkpoint_path, plan, clock=clock)
            self.assertEqual(terminal.state, "FINISHED")
            receipt = json.loads((terminal.workdir / "controller_receipt.json").read_text())
            for key in (
                "plan", "program", "feedback", "completion_report", "events",
                "checkpoint", "feedback_journal",
            ):
                self.assertEqual(
                    receipt[key]["sha256"], sha256_file(receipt[key]["path"]), key
                )
            feedback = json.loads((terminal.workdir / "feedback_evidence.json").read_text())
            self.assertEqual(feedback["program_sha256"], receipt["program"]["sha256"])
            self.assertTrue(receipt["execution_complete"])
            self.assertFalse(receipt["task_quality_complete"])
            self.assertTrue(receipt["real_acceptance_pending"])

    def test_quality_is_bound_to_plan_execution_and_full_fixture_identity(self):
        for field, value, code in (
            ("plan_sha256", "f" * 64, "quality_binding_mismatch"),
            ("request_id", "old-request", "quality_binding_mismatch"),
            ("execution_id", "old-execution", "quality_binding_mismatch"),
            ("execution_attempt", 2, "quality_binding_mismatch"),
            ("execution_attempt", True, "invalid_integer"),
            ("fixture_id", "fixture-B", "quality_identity_mismatch"),
            ("fixture_revision", "old-fixture", "quality_identity_mismatch"),
            ("tool_id", "wheel", "quality_identity_mismatch"),
            ("frame", "world", "quality_identity_mismatch"),
            ("units", {}, "quality_unit_mismatch"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                controller = MockController(plan, Path(directory) / "controller", clock=Clock())
                controller.confirm("operator")
                controller.start()
                controller.run_mock(MockFeedbackSource(plan, controller.request_id, controller.execution_id))
                evidence = quality(plan)
                evidence[field] = value
                with self.assertRaises(StateRejected) as caught:
                    controller.set_quality_evidence(evidence)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(controller.state, "HELD")
                self.assertFalse(controller.completion_report()["task_quality_complete"])

    def test_quality_requires_explicit_release_and_rejects_signed_risk(self):
        for change in (
            "missing_release", "missing_goal", "overgrind", "negative_limit_risk",
            "region_overgrind", "invalid_view", "automatic_regrind_disallowed",
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                plan, _ = build_case(directory)
                controller = MockController(plan, Path(directory) / "controller", clock=Clock())
                controller.confirm("operator")
                controller.start()
                controller.run_mock(MockFeedbackSource(plan, controller.request_id, controller.execution_id))
                evidence = quality(plan)
                if change == "missing_release":
                    evidence.pop("observational_release")
                elif change == "missing_goal":
                    evidence.pop("goal_complete")
                elif change in {"overgrind", "negative_limit_risk"}:
                    evidence[change] = 1
                elif change == "region_overgrind":
                    evidence["regions"] = [{"id": 1, "status": "verified", "overgrind": True}]
                elif change == "invalid_view":
                    evidence["regions"] = [{"id": 1, "status": "verified", "observations": [6]}]
                else:
                    evidence["automatic_regrind_allowed"] = False
                controller.set_quality_evidence(evidence)
                report = controller.finish()
                self.assertTrue(report["execution_complete"])
                self.assertFalse(report["task_quality_complete"])
                self.assertTrue(report["quality"]["reasons"])

    def test_quality_snapshot_and_file_hash_are_independent_of_caller_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            controller = MockController(plan, Path(directory) / "controller", clock=Clock())
            controller.confirm("operator")
            controller.start()
            controller.run_mock(MockFeedbackSource(plan, controller.request_id, controller.execution_id))
            evidence = quality(plan)
            controller.set_quality_evidence(evidence)
            evidence["covered_segments"].clear()
            evidence["segment_results"].clear()
            self.assertTrue(controller.completion_report()["task_quality_complete"])
            controller.quality_path.write_bytes(controller.quality_path.read_bytes() + b"\n")
            with self.assertRaises(PlanRejected) as caught:
                controller.finish()
            self.assertEqual(caught.exception.code, "quality_hash_mismatch")
            self.assertEqual(controller.state, "HELD")
            self.assertFalse(controller.completion_report()["task_quality_complete"])

    def test_reported_contact_cannot_be_cleared_by_recovery_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(plan, Path(directory) / "controller", clock=clock)
            controller.confirm("operator")
            controller.start()
            source = batches(plan)
            source[0]["samples"][0]["unexpected_contacts"] = 1
            controller.ingest_feedback(source[0])
            self.assertEqual(controller.state, "HELD")
            controller.recover(
                explicit_mock_recovery=True, operator="operator", acknowledge_hold=True
            )
            controller.confirm("operator")
            controller.start()
            controller.run_mock(source[1:])
            controller.set_quality_evidence(quality(plan, controller=controller))
            with self.assertRaises(CompletionRejected):
                controller.finish()
            self.assertEqual(controller.total_contacts, 1)
            self.assertFalse(controller.completion_report()["task_quality_complete"])

    def test_plan_confirm_feedback_finish_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            result = controller.run_mock(
                MockFeedbackSource(
                    plan,
                    controller.request_id,
                    controller.execution_id,
                    batch_size=2,
                )
            )
            self.assertIsNone(result["error"])
            controller.set_quality_evidence(quality(plan))
            report = controller.finish()
            self.assertEqual(controller.state, "FINISHED")
            self.assertTrue(report["execution_complete"])
            self.assertTrue(report["task_quality_complete"])
            receipt = json.loads(
                (
                    Path(directory) / "controller/controller_receipt.json"
                ).read_text()
            )
            self.assertEqual(receipt["plan"]["sha256"], plan.plan_sha256)
            self.assertEqual(receipt["source_kind"], "synthetic")
            self.assertEqual(
                receipt["feedback"]["sha256"],
                sha256_file(
                    Path(directory) / "controller/feedback_evidence.json"
                ),
            )
            feedback = json.loads(
                (
                    Path(directory) / "controller/feedback_evidence.json"
                ).read_text()
            )
            self.assertEqual(feedback["schema"], FEEDBACK_SCHEMA)
            self.assertEqual(
                feedback["program_sha256"],
                sha256_file(Path(directory) / "controller/mock_program.json"),
            )
            self.assertTrue(feedback["completed"])
            stamps = [sample["t_s"] for sample in feedback["samples"]]
            self.assertEqual(stamps, sorted(set(stamps)))
            self.assertEqual(len(feedback["samples"]), len(plan.commands))

    def test_completion_callback_is_not_task_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            controller.run_mock(
                MockFeedbackSource(
                    plan,
                    controller.request_id,
                    controller.execution_id,
                    batch_size=4,
                )
            )
            report = controller.finish()
            self.assertEqual(controller.state, "FINISHED")
            self.assertTrue(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
            self.assertEqual(report["quality"]["status"], "missing")
            controller.set_quality_evidence(quality(plan))
            self.assertTrue(
                controller.completion_report()["task_quality_complete"]
            )
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            controller.run_mock(
                MockFeedbackSource(
                    plan,
                    controller.request_id,
                    controller.execution_id,
                    batch_size=4,
                )
            )
            controller.set_quality_evidence(quality(plan, status="partial"))
            report = controller.finish()
            self.assertTrue(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
            self.assertIn("quality_not_complete", report["reasons"])

    def test_duplicate_feedback_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = batches(plan, batch_size=2)[0]
            first = controller.ingest_feedback(batch)
            second = controller.ingest_feedback(batch)
            self.assertTrue(first["accepted"])
            self.assertFalse(second["accepted"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(controller.feedback_batches, 1)
            self.assertEqual(
                len(controller.covered_commands), len(batch["samples"])
            )
            self.assertEqual(controller.state, "EXECUTING")

    def test_duplicate_conflict_and_out_of_order_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = batches(plan, batch_size=2)[0]
            controller.ingest_feedback(batch)
            conflict = json.loads(json.dumps(batch))
            conflict["samples"][0]["q_actual"][0] += 0.001
            with self.assertRaises(FeedbackRejected) as caught:
                controller.ingest_feedback(conflict)
            self.assertEqual(
                caught.exception.code, "feedback_duplicate_conflict"
            )
            self.assertEqual(controller.state, "HELD")
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            first, second = batches(plan, batch_size=2)[:2]
            controller.ingest_feedback(first)
            second["samples"][0]["command_index"] = 0
            second["samples"][0]["segment_id"] = 0
            with self.assertRaises(FeedbackRejected) as caught:
                controller.ingest_feedback(second)
            self.assertEqual(caught.exception.code, "feedback_out_of_order")
            self.assertEqual(controller.state, "HELD")

    def test_feedback_binding_and_limit_mismatch_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = batches(plan, batch_size=2)[0]
            wrong_plan = json.loads(json.dumps(batch))
            wrong_plan["plan_sha256"] = "f" * 64
            with self.assertRaises(FeedbackRejected) as caught:
                controller.ingest_feedback(wrong_plan)
            self.assertEqual(caught.exception.code, "feedback_binding_mismatch")
            self.assertEqual(controller.state, "HELD")
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = batches(plan, batch_size=2)[0]
            bad_q = json.loads(json.dumps(batch))
            bad_q["samples"][0]["q_actual"][2] = 9.0
            with self.assertRaises(FeedbackRejected) as caught:
                controller.ingest_feedback(bad_q)
            self.assertEqual(caught.exception.code, "feedback_joint_limit")
            self.assertEqual(controller.state, "HELD")

    def test_expired_plan_cannot_confirm_or_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory, valid_for_s=1.0)
            clock = Clock()
            clock.advance(2.0)
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            with self.assertRaises(PlanRejected) as caught:
                controller.confirm("operator")
            self.assertEqual(caught.exception.code, "plan_expired")
            self.assertEqual(controller.state, "HELD")
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory, valid_for_s=2.0)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            source = list(
                MockFeedbackSource(
                    plan,
                    controller.request_id,
                    controller.execution_id,
                    batch_size=1,
                )
            )
            controller.ingest_feedback(source[0])
            clock.advance(3.0)
            with self.assertRaises(PlanRejected) as caught:
                controller.ingest_feedback(source[1])
            self.assertEqual(caught.exception.code, "plan_expired")
            self.assertEqual(controller.state, "HELD")

    def test_process_exception_defaults_to_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            result = run_mock_cycle(
                plan,
                Path(directory) / "cycle",
                clock=clock,
                batch_size=2,
                exception_at=3,
            )
            controller = result["controller"]
            self.assertEqual(controller.state, "HELD")
            self.assertTrue(result["run"]["unexpected_exception"])
            self.assertEqual(
                result["run"]["error"]["code"], "mock_process_exception"
            )
            report = controller.completion_report()
            self.assertFalse(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
            checkpoint = json.loads(
                (Path(directory) / "cycle/checkpoint.json").read_text()
            )
            self.assertEqual(checkpoint["resume_policy"], "hold")
            self.assertEqual(checkpoint["fault_code"], "mock_process_exception")
            with self.assertRaises(StateRejected):
                controller.finish()

    def test_cancel_and_reject_are_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            controller.cancel("operator cancelled before completion")
            self.assertEqual(controller.state, "CANCELLED")
            with self.assertRaises(StateRejected):
                controller.ingest_feedback(batches(plan, batch_size=2)[0])
            with self.assertRaises(StateRejected):
                controller.finish()
            report = controller.completion_report()
            self.assertFalse(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            controller = MockController(
                plan, Path(directory) / "controller", clock=Clock()
            )
            controller.reject("operator rejected this mock plan")
            self.assertEqual(controller.state, "REJECTED")
            with self.assertRaises(StateRejected):
                controller.confirm("operator")

    def test_partial_feedback_and_early_callback_cannot_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = batches(plan, batch_size=2)[0]
            controller.ingest_feedback(batch)
            with self.assertRaises(CompletionRejected) as caught:
                controller.finish()
            self.assertEqual(controller.state, "HELD")
            self.assertFalse(caught.exception.report["execution_complete"])
            self.assertTrue(caught.exception.report["missing_segments"])
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            batch = json.loads(json.dumps(batches(plan, batch_size=1)[0]))
            batch["completed"] = True
            controller.ingest_feedback(batch)
            self.assertEqual(controller.state, "HELD")
            self.assertEqual(
                controller.fault_code, "incomplete_completion_callback"
            )
            self.assertTrue(
                controller.completion_report()["early_completion_claim"]
            )

    def test_blocked_source_segment_prevents_task_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory, blocked_segment=3)
            self.assertEqual(plan.admitted_segments, (1, 2))
            self.assertEqual(plan.blocked_segments, (3,))
            self.assertNotIn(
                3, {command.segment_id for command in plan.commands}
            )
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            controller.run_mock(
                MockFeedbackSource(
                    plan,
                    controller.request_id,
                    controller.execution_id,
                    batch_size=2,
                )
            )
            controller.set_quality_evidence(quality(plan))
            report = controller.finish()
            self.assertTrue(report["execution_complete"])
            self.assertFalse(report["task_quality_complete"])
            self.assertEqual(report["blocked_source_segments"], [3])
            self.assertIn("blocked_source_segments", report["reasons"])
            self.assertEqual(report["missing_segments"], [])

    def test_interrupt_resume_requires_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            source = MockFeedbackSource(
                plan,
                controller.request_id,
                controller.execution_id,
                batch_size=2,
            )
            iterator = iter(source)
            controller.ingest_feedback(next(iterator))
            checkpoint = Path(directory) / "controller/checkpoint.json"
            resumed = MockController.resume(checkpoint, plan, clock=clock)
            self.assertEqual(resumed.state, "HELD")
            self.assertEqual(resumed.fault_code, "interrupted_restart")
            with self.assertRaises(RecoveryRejected) as caught:
                resumed.recover(explicit_mock_recovery=False)
            self.assertEqual(
                caught.exception.code, "explicit_mock_recovery_required"
            )
            resumed.recover(
                explicit_mock_recovery=True,
                operator="operator",
                acknowledge_hold=True,
            )
            self.assertEqual(resumed.state, "PLANNED")
            resumed.confirm("operator")
            resumed.start()
            remaining = MockFeedbackSource(
                plan,
                resumed.request_id,
                resumed.execution_id,
                batch_size=2,
                start_command_index=resumed.next_command_index,
                start_sequence=resumed.next_sequence,
            )
            resumed.run_mock(remaining)
            resumed.set_quality_evidence(quality(plan, controller=resumed))
            report = resumed.finish()
            self.assertTrue(report["execution_complete"])
            self.assertTrue(report["task_quality_complete"])

    def test_recovery_rejects_changed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, _ = build_case(directory)
            clock = Clock()
            controller = MockController(
                plan, Path(directory) / "controller", clock=clock
            )
            controller.confirm("operator")
            controller.start()
            controller.ingest_feedback(batches(plan, batch_size=2)[0])
            checkpoint = Path(directory) / "controller/checkpoint.json"
            resumed = MockController.resume(checkpoint, plan, clock=clock)
            preview = Path(directory) / "estun_path_preview.csv"
            preview.write_bytes(preview.read_bytes() + b"changed\n")
            with self.assertRaises(RecoveryRejected) as caught:
                resumed.recover(
                    explicit_mock_recovery=True,
                    operator="operator",
                    acknowledge_hold=True,
                )
            self.assertEqual(caught.exception.code, "source_hash_mismatch")
            self.assertEqual(resumed.state, "HELD")
            self.assertFalse(resumed.completion_report()["execution_complete"])


if __name__ == "__main__":
    unittest.main()
