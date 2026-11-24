"""Offline controller reliability stress, fault matrix and virtual 800 h.

The default command validates one real archived simulation command bundle,
replays it through the explicit offline mock once, runs at least 100
low-resource fault/recovery cycles, and generates an accelerated 800-hour
virtual heartbeat trace. No MuJoCo, physical socket, controller or external
message is used. A virtual 800-hour trace is coverage, not real acceptance.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import project
from .offline_controller import (
    CANONICAL_UNITS,
    QUALITY_SCHEMA,
    CompletionRejected,
    ControllerError,
    FeedbackRejected,
    MockController,
    MockFeedbackSource,
    PlanRejected,
    RecoveryRejected,
    StateRejected,
    load_plan,
    load_plan_from_run,
    run_mock_cycle,
    sha256_file,
    write_case_files,
)

DEFAULT_RUN_DIR = project.RUNS / "daiyi_full_chain_20261122/final/thin_120483"
DEFAULT_OUTPUT = project.RUNS / "enterprise_offline_20261123/reliability"
REQUIREMENTS = project.ROOT / "config/enterprise_requirements_20261123.json"
MINIMUM_LOOPS = 100
DEFAULT_LOOPS = 120
DEFAULT_VIRTUAL_HOURS = 800.0
BASE_TIME = 1_800_000_000.0
MAX_HEARTBEAT_GAP = 3600.0
MAX_VIRTUAL_HEARTBEATS = 100_000
REAL_DATA_PENDING = [
    "ESTUN controller joint mapping, program upload and real feedback log",
    "physical controller commissioning with all calibrations verified",
    "real wall-clock 800-hour software reliability log on one boot_id",
    "MuJoCo collision and dynamic tracking replay for the accepted commands",
    "camera, TCP, fixture repeatability and force/removal calibration",
    "independent signed before/after surface measurement",
]
ROLE_MATRIX = {
    "run_result": "historical simulation input only; not rerun",
    "cycle_receipt": "not produced by this controller/reliability slice",
    "source_hashes": "archived producer provenance; current offline sources frozen separately",
    "localization": "pending independent measurement",
    "timing_receipt": "pending real process log",
    "controller_receipt": "offline mock only; real controller pending",
    "reliability_receipt": "virtual accelerated coverage; real 800 h pending",
    "site_manifest": "pending independent signed surface measurements",
}


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _relative(path, output):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(Path(output).resolve()))
    except ValueError:
        return str(path)


def _input(path, role, source):
    path = Path(path).resolve()
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source": source,
    }


def _artifact(path, output, role, source):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "role": role,
        "path": _relative(path, output),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "source": source,
    }


def _run_inputs(run_dir):
    inputs = [
        _input(run_dir / name, "historical_run", source)
        for name, source in (
            ("motion_commands.json", "archived accepted commands; mock only"),
            ("preflight.json", "archived whole-segment admission; not rerun"),
            ("estun_path_preview.csv", "archived preview; never executable"),
            (
                "result.json",
                "archived quality/status; no current quality revalidation",
            ),
        )
    ]
    source_manifest = run_dir / "source_hashes.json"
    if source_manifest.is_file():
        inputs.append(
            _input(
                source_manifest,
                "historical_source_manifest",
                "archived producer provenance; does not claim current producer execution",
            )
        )
    inputs.extend(
        [
            _input(
                project.ROOT / "assets/ier220_workstation/scene.xml",
                "robot_model",
                "XML limits read as data; MuJoCo not imported",
            ),
            _input(
                REQUIREMENTS, "requirements", "enterprise target definitions"
            ),
        ]
    )
    for name, reference in _read_json(REQUIREMENTS).get("sources", {}).items():
        entry = _input(
            project.ROOT / reference["path"],
            "requirement_source",
            f"enterprise specification source: {name}; read only",
        )
        entry["declared_sha256"] = reference["sha256"]
        inputs.append(entry)
    for name in (
        "__init__.py",
        "project.py",
        "offline_controller.py",
        "reliability_stress.py",
        "enterprise_acceptance.py",
        "workstation_delivery.py",
    ):
        inputs.append(
            _input(
                project.SOURCE / name,
                "source",
                "current offline module; frozen independently of archived producer hashes",
            )
        )
    inputs.append(
        _input(
            project.SOURCE / "workstation_run.py",
            "reference_source",
            "current export contract reference; not executed or revalidated here",
        )
    )
    for name in ("test_offline_controller.py", "test_reliability_stress.py"):
        inputs.append(
            _input(
                project.ROOT / "tests" / name,
                "validation_test_source",
                "regression test source; tests run separately from this timed workload",
            )
        )
    return inputs


def _freeze_offline_sources(output, inputs):
    root = output / "source_snapshot"
    root.mkdir()
    artifacts = []
    for item in inputs:
        if item["role"] not in {
            "source",
            "reference_source",
            "validation_test_source",
        }:
            continue
        path = root / Path(item["path"]).name
        path.write_bytes(Path(item["path"]).read_bytes())
        artifact = _artifact(
            path, output, f"{item['role']}_snapshot", item["source"]
        )
        if artifact["sha256"] != item["sha256"]:
            raise RuntimeError("source changed during snapshot capture")
        item["snapshot_path"] = artifact["path"]
        artifacts.append(artifact)
    _write_json(output / "source_inputs.json", inputs)
    return artifacts


@dataclass
class Scenario:
    """One expected controller outcome used in the fault matrix."""

    name: str
    expected: str
    code: str | None = None
    blocked: bool = False


SCENARIOS = (
    Scenario("happy_task_complete", "task_complete"),
    Scenario("happy_execution_only", "execution_complete"),
    Scenario("quality_partial", "execution_complete"),
    Scenario("duplicate_idempotent", "task_complete"),
    Scenario("duplicate_conflict", "reject", "feedback_duplicate_conflict"),
    Scenario("out_of_order_feedback", "reject", "feedback_out_of_order"),
    Scenario("expired_plan", "reject", "plan_expired"),
    Scenario("expired_during_execution", "reject", "plan_expired"),
    Scenario("csv_hash_corrupt", "reject", "source_hash_mismatch"),
    Scenario("missing_preflight", "reject", "missing_source"),
    Scenario("process_exception", "reject", "mock_process_exception"),
    Scenario("cancel", "cancel"),
    Scenario("interrupt_recover", "task_complete"),
    Scenario("partial_feedback", "reject", "completion_incomplete"),
    Scenario("support_missing_feedback", "reject", "feedback_command_gap"),
    Scenario("blocked_segment", "execution_complete", blocked=True),
    Scenario("plan_hash_mismatch", "reject", "plan_hash_mismatch"),
    Scenario("feedback_binding", "reject", "feedback_binding_mismatch"),
    Scenario("timeline_regression", "reject", "timeline_order"),
    Scenario("joint_limit", "reject", "joint_limit"),
    Scenario("nonfinite", "reject", "nonfinite_json"),
    Scenario("csv_as_program", "reject", "invalid_json"),
    Scenario("wrong_units", "reject", "unit_mismatch"),
    Scenario("feedback_batch_atomic", "reject", "invalid_feedback_status"),
    Scenario("feedback_sequence_gap", "reject", "feedback_sequence_gap"),
    Scenario("feedback_time_regression", "reject", "feedback_time_regression"),
    Scenario(
        "feedback_command_time", "reject", "feedback_command_time_mismatch"
    ),
    Scenario("feedback_dropout", "reject", "feedback_timeout"),
    Scenario("feedback_stream_ended", "reject", "feedback_stream_ended"),
    Scenario(
        "source_changed_during_execution", "reject", "source_hash_mismatch"
    ),
    Scenario("program_hash_changed", "reject", "program_hash_mismatch"),
    Scenario(
        "checkpoint_counter_corrupt", "reject", "checkpoint_feedback_mismatch"
    ),
    Scenario("checkpoint_log_binding", "reject", "invalid_feedback_log"),
    Scenario("quality_binding", "reject", "quality_binding_mismatch"),
    Scenario("quality_hash_changed", "reject", "quality_hash_mismatch"),
    Scenario("quality_overgrind", "execution_complete"),
    Scenario("quality_negative_limit_risk", "execution_complete"),
    Scenario("quality_invalid_view", "execution_complete"),
    Scenario("preflight_binding", "reject", "preflight_binding_mismatch"),
    Scenario("preflight_partial_coverage", "reject", "partial_preflight"),
    Scenario("clock_regression", "reject", "controller_clock_regression"),
    Scenario("receipt_hashes", "task_complete"),
)


class VirtualClock:
    """A deterministic epoch clock for accelerated mock cycles."""

    def __init__(self, now=BASE_TIME):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def _identity(seed):
    return {
        "part_id": "097",
        "serial": f"mock-serial-{seed}",
        "cad_revision": f"097-cad-{seed % 97}",
        "fixture_id": "fixture-A",
        "fixture_revision": "fixture-rev-01",
        "tool_id": "belt",
        "frame": "workpiece_097",
        "units": dict(CANONICAL_UNITS),
    }


def _command(
    index,
    *,
    t,
    segment_id,
    phase,
    attached,
    point,
    detection_id,
    seed,
):
    q = [
        0.4 + 0.0001 * (seed % 17) + 0.001 * index,
        -0.1,
        0.8,
        2.0,
        0.5,
        -2.0,
    ]
    return {
        "t": t,
        "q": q,
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


def _bundle(directory, seed, *, blocked=False):
    """Write a small but complete accepted command bundle."""
    commands = [
        _command(
            0,
            t=0.1,
            segment_id=0,
            phase="transfer",
            attached=False,
            point=None,
            detection_id=0,
            seed=seed,
        ),
        _command(
            1,
            t=0.2,
            segment_id=0,
            phase="home",
            attached=False,
            point=None,
            detection_id=0,
            seed=seed,
        ),
    ]
    for segment in range(1, 5):
        if blocked and segment == 4:
            continue
        for offset in range(3):
            commands.append(
                _command(
                    len(commands),
                    t=(
                        1.0
                        + 0.5 * segment
                        + 0.05 * offset
                        + 0.0001 * (seed % 7)
                    ),
                    segment_id=segment,
                    phase=("approach", "trace", "retract")[offset],
                    attached=True,
                    point=[0.001, 0.002, 0.003],
                    detection_id=segment,
                    seed=seed,
                )
            )
    rows = []
    for segment in range(1, 5):
        rows.append(
            {
                "segment_id": segment,
                "detection_id": segment,
                "kind": "seam",
                "point_count": 3,
                "status": ("blocked" if blocked and segment == 4 else "passed"),
                "coverage": 0.0 if blocked and segment == 4 else 1.0,
                "reason": (
                    "synthetic blocked segment"
                    if blocked and segment == 4
                    else "complete synthetic segment passed"
                ),
            }
        )
    preflight = {
        "passed": not blocked,
        "checked_commands": len(commands),
        "contact_count": 1 if blocked else 0,
        "rows": rows,
        "scope": "synthetic reliability preflight for the exact commands",
    }
    preview = []
    for segment in range(1, 5):
        preview.append(
            {
                "segment": segment,
                "detection_id": segment,
                "view": 0,
                "frame": "workpiece_097",
                "x_mm": 10.0 * segment,
                "y_mm": 2.0,
                "z_mm": 3.0,
                "nx": 0.0,
                "ny": 0.0,
                "nz": 1.0,
                "tool": "belt",
            }
        )
    preview.append(
        {
            "segment": 99,
            "detection_id": 99,
            "view": 0,
            "frame": "workpiece_097",
            "x_mm": 99.0,
            "y_mm": 99.0,
            "z_mm": 99.0,
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
        "workpiece_profile": {"sha256": "synthetic-profile"},
        "task_status": "partial",
        "goal_complete": False,
        "observational_release": False,
        "motion_gate_passed": True,
        "path_count": 4,
        "admitted_path_count": 3 if blocked else 4,
    }
    write_case_files(
        directory,
        commands=commands,
        preflight=preflight,
        preview_rows=preview,
        result=result,
    )
    return _identity(seed)


def _load_bundle(
    directory,
    seed,
    *,
    now_s,
    blocked=False,
    valid_for_s=3600.0,
    expected_plan_sha256=None,
    commands_path=None,
    identity_value=None,
):
    values = identity_value or _identity(seed)
    return load_plan(
        directory,
        identity=values,
        issued_at_s=now_s,
        expires_at_s=now_s + valid_for_s,
        require_preview=True,
        expected_plan_sha256=expected_plan_sha256,
        commands_path=commands_path,
    )


def _quality(
    plan, status="complete", covered=None, independent=True, controller=None
):
    covered = list(plan.admitted_segments if covered is None else covered)
    suffix = plan.plan_sha256[:16]
    binding = (
        controller.quality_binding()
        if controller is not None
        else {
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
        "scope": "independent synthetic quality fixture, not real measurement",
    }


def _source(plan, controller, *, batch_size=4, start_index=0, start_sequence=1):
    return MockFeedbackSource(
        plan,
        controller.request_id,
        controller.execution_id,
        batch_size=batch_size,
        start_command_index=start_index,
        start_sequence=start_sequence,
    )


def _start_controller(plan, directory, clock):
    controller = MockController(plan, directory, clock=clock)
    controller.confirm("reliability-operator")
    controller.start()
    return controller


def _run_to_finish(plan, directory, clock, *, quality_evidence=None):
    controller = _start_controller(plan, directory, clock)
    controller.run_mock(_source(plan, controller, batch_size=4))
    if quality_evidence is not None:
        controller.set_quality_evidence(quality_evidence)
    report = controller.finish()
    return controller, report


def _result(
    scenario,
    *,
    observed,
    code=None,
    state=None,
    execution_complete=False,
    task_quality_complete=False,
    error=None,
    plan_sha256=None,
    workspace=None,
    unexpected_exception=False,
    feedback_samples=0,
    covered_command_count=0,
    expected_command_count=0,
    quality_reasons=(),
    quality_region_failures=(),
):
    return {
        "scenario": scenario.name
        if isinstance(scenario, Scenario)
        else scenario,
        "expected": (
            scenario.expected if isinstance(scenario, Scenario) else None
        ),
        "expected_code": (
            scenario.code if isinstance(scenario, Scenario) else None
        ),
        "observed": observed,
        "code": code,
        "state": state,
        "execution_complete": bool(execution_complete),
        "task_quality_complete": bool(task_quality_complete),
        "unexpected_exception": bool(unexpected_exception),
        "error": error,
        "plan_sha256": plan_sha256,
        "workspace": workspace,
        "feedback_samples": feedback_samples,
        "covered_command_count": covered_command_count,
        "expected_command_count": expected_command_count,
        "quality_reasons": list(quality_reasons),
        "quality_region_failures": list(quality_region_failures),
    }


def _finish_result(scenario, controller, report, workspace):
    return _result(
        scenario,
        observed="finished",
        state=controller.state,
        execution_complete=report["execution_complete"],
        task_quality_complete=report["task_quality_complete"],
        plan_sha256=controller.plan.plan_sha256,
        workspace=workspace,
        feedback_samples=report["feedback_samples"],
        covered_command_count=report["covered_command_count"],
        expected_command_count=report["expected_command_count"],
        quality_reasons=report["quality"]["reasons"],
        quality_region_failures=report["quality"].get("region_failures", []),
    )


def _execute_scenario(loop_dir, scenario, seed):
    """Run one scenario against the real offline controller implementation."""
    loop_dir = Path(loop_dir)
    case_dir = loop_dir / "case"
    controller_dir = loop_dir / "controller"
    clock = VirtualClock(BASE_TIME + seed * 0.001)
    _bundle(case_dir, seed, blocked=scenario.blocked)
    try:
        if scenario.name in {
            "timeline_regression",
            "joint_limit",
            "nonfinite",
            "preflight_binding",
        }:
            _bundle(case_dir, seed, blocked=scenario.blocked)
            commands_path = case_dir / "motion_commands.json"
            values = _read_json(commands_path)
            if scenario.name == "timeline_regression":
                values[3]["t"] = values[2]["t"]
                commands_path.write_text(json.dumps(values, ensure_ascii=False))
            elif scenario.name == "joint_limit":
                values[2]["q"][2] = 9.0
                commands_path.write_text(json.dumps(values, ensure_ascii=False))
            elif scenario.name == "nonfinite":
                values[2]["point"][0] = float("nan")
                commands_path.write_text(
                    json.dumps(values, ensure_ascii=False, allow_nan=True)
                )
            else:
                values[2]["q"][0] += 0.01
                commands_path.write_text(json.dumps(values, ensure_ascii=False))
            _load_bundle(
                case_dir,
                seed,
                now_s=clock.now,
                blocked=scenario.blocked,
            )
            raise AssertionError("invalid command bundle was accepted")
        if scenario.name == "preflight_partial_coverage":
            path = case_dir / "preflight.json"
            value = _read_json(path)
            value["rows"][0]["coverage"] = 0.99
            _write_json(path, value)
            _load_bundle(case_dir, seed, now_s=clock.now)
            raise AssertionError("partial preflight coverage was accepted")
        if scenario.name == "csv_as_program":
            _bundle(case_dir, seed)
            _load_bundle(
                case_dir,
                seed,
                now_s=clock.now,
                commands_path=case_dir / "estun_path_preview.csv",
            )
            raise AssertionError("candidate CSV was accepted as commands")
        if scenario.name == "plan_hash_mismatch":
            _bundle(case_dir, seed)
            _load_bundle(
                case_dir,
                seed,
                now_s=clock.now,
                expected_plan_sha256="0" * 64,
            )
            raise AssertionError("wrong plan hash was accepted")
        if scenario.name == "wrong_units":
            _bundle(case_dir, seed)
            values = _identity(seed)
            values["units"] = {**CANONICAL_UNITS, "point": "mm"}
            _load_bundle(
                case_dir,
                seed,
                now_s=clock.now,
                identity_value=values,
            )
            raise AssertionError("non-canonical units were accepted")
        plan = _load_bundle(
            case_dir,
            seed,
            now_s=clock.now,
            blocked=scenario.blocked,
            valid_for_s=(
                2.0
                if scenario.name in {"expired_during_execution", "expired_plan"}
                else 3600.0
            ),
        )
        if scenario.name == "csv_hash_corrupt":
            preview = case_dir / "estun_path_preview.csv"
            preview.write_bytes(preview.read_bytes() + b"corrupted\n")
            controller = MockController(plan, controller_dir, clock=clock)
            controller.confirm("reliability-operator")
            raise AssertionError("corrupted CSV allowed confirmation")
        if scenario.name == "missing_preflight":
            (case_dir / "preflight.json").unlink()
            controller = MockController(plan, controller_dir, clock=clock)
            controller.confirm("reliability-operator")
            raise AssertionError("missing preflight allowed confirmation")
        if scenario.name == "expired_plan":
            clock.advance(3.0)
            controller = MockController(plan, controller_dir, clock=clock)
            controller.confirm("reliability-operator")
            raise AssertionError("expired plan allowed confirmation")
        if scenario.name == "expired_during_execution":
            controller = _start_controller(plan, controller_dir, clock)
            source = list(_source(plan, controller, batch_size=3))
            controller.ingest_feedback(source[0])
            clock.advance(3.0)
            controller.ingest_feedback(source[1])
            raise AssertionError("expired plan continued execution")
        if scenario.name in {
            "feedback_batch_atomic",
            "feedback_sequence_gap",
            "feedback_time_regression",
            "feedback_command_time",
        }:
            controller = _start_controller(plan, controller_dir, clock)
            batch = next(iter(_source(plan, controller, batch_size=3)))
            if scenario.name == "feedback_batch_atomic":
                batch["samples"][1]["status"] = "invalid"
            elif scenario.name == "feedback_sequence_gap":
                batch["sequence"] = 2
            elif scenario.name == "feedback_time_regression":
                batch["samples"][1]["t_s"] = batch["samples"][0]["t_s"]
            else:
                batch["samples"][0]["t_s"] = 0.01
            _write_json(loop_dir / "injected_feedback.json", batch)
            controller.ingest_feedback(batch)
            raise AssertionError("invalid feedback payload was accepted")
        if scenario.name in {
            "feedback_dropout",
            "feedback_stream_ended",
            "clock_regression",
            "source_changed_during_execution",
            "program_hash_changed",
        }:
            controller = _start_controller(plan, controller_dir, clock)
            source = list(_source(plan, controller, batch_size=3))
            controller.ingest_feedback(source[0])
            if scenario.name == "feedback_dropout":
                clock.advance(6.0)
                controller.check_feedback_timeout()
            elif scenario.name == "clock_regression":
                clock.advance(-1.0)
                controller.ingest_feedback(source[1])
            elif scenario.name == "feedback_stream_ended":
                result = controller.run_mock([])
                return _result(
                    scenario,
                    observed="reject",
                    code=result["error"]["code"],
                    state=controller.state,
                    error=result["error"]["message"],
                    plan_sha256=plan.plan_sha256,
                    workspace=_relative(controller_dir, loop_dir.parent.parent),
                    feedback_samples=controller.feedback_samples,
                    covered_command_count=len(controller.covered_commands),
                    expected_command_count=len(plan.commands),
                )
            else:
                path = (
                    controller.program_path
                    if scenario.name == "program_hash_changed"
                    else case_dir / "motion_commands.json"
                )
                path.write_bytes(path.read_bytes() + b"\n")
                controller.ingest_feedback(source[1])
            raise AssertionError("invalid active controller state was accepted")
        if scenario.name in {
            "checkpoint_counter_corrupt",
            "checkpoint_log_binding",
        }:
            controller = _start_controller(plan, controller_dir, clock)
            controller.ingest_feedback(
                next(iter(_source(plan, controller, batch_size=3)))
            )
            controller.hold(
                "interrupted_restart",
                "synthetic process interruption",
                cause="mock_process",
            )
            checkpoint = _read_json(controller.checkpoint_path)
            if scenario.name == "checkpoint_counter_corrupt":
                checkpoint["last_sequence"] = 999
            else:
                batch = _read_json(controller.feedback_path)
                batch["serial"] = "other-serial"
                controller.feedback_path.write_text(
                    json.dumps(batch, ensure_ascii=False, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                checkpoint["feedback_sha256"] = sha256_file(
                    controller.feedback_path
                )
            _write_json(controller.checkpoint_path, checkpoint)
            _write_json(loop_dir / "injected_checkpoint.json", checkpoint)
            MockController.resume(controller.checkpoint_path, plan, clock=clock)
            raise AssertionError("corrupted recovery evidence was accepted")
        if scenario.name in {
            "quality_binding",
            "quality_hash_changed",
            "quality_overgrind",
            "quality_negative_limit_risk",
            "quality_invalid_view",
        }:
            controller = _start_controller(plan, controller_dir, clock)
            controller.run_mock(_source(plan, controller))
            evidence = _quality(plan, controller=controller)
            if scenario.name == "quality_binding":
                evidence["execution_id"] = "previous-execution"
            elif scenario.name == "quality_overgrind":
                evidence["overgrind"] = 1
                evidence["automatic_regrind_allowed"] = False
            elif scenario.name == "quality_negative_limit_risk":
                evidence["negative_limit_risk"] = 1
                evidence["automatic_regrind_allowed"] = False
            elif scenario.name == "quality_invalid_view":
                evidence["regions"] = [
                    {"id": 1, "status": "verified", "observations": [6]}
                ]
            _write_json(loop_dir / "injected_quality.json", evidence)
            controller.set_quality_evidence(evidence)
            if scenario.name == "quality_hash_changed":
                controller.quality_path.write_bytes(
                    controller.quality_path.read_bytes() + b"\n"
                )
            report = controller.finish()
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "process_exception":
            result = run_mock_cycle(
                plan,
                controller_dir,
                clock=clock,
                batch_size=4,
                exception_at=3,
            )
            controller = result["controller"]
            if controller.state != "HELD":
                raise AssertionError("process exception did not hold")
            return _result(
                scenario,
                observed="reject",
                code=controller.fault_code,
                state=controller.state,
                plan_sha256=plan.plan_sha256,
                workspace=_relative(controller_dir, loop_dir.parent.parent),
                error=controller.fault,
                feedback_samples=controller.feedback_samples,
                covered_command_count=len(controller.covered_commands),
                expected_command_count=len(plan.commands),
            )
        if scenario.name == "cancel":
            controller = _start_controller(plan, controller_dir, clock)
            controller.ingest_feedback(
                next(iter(_source(plan, controller, batch_size=3)))
            )
            controller.cancel("reliability loop cancellation")
            return _result(
                scenario,
                observed="cancelled",
                state=controller.state,
                plan_sha256=plan.plan_sha256,
                workspace=_relative(controller_dir, loop_dir.parent.parent),
                feedback_samples=controller.feedback_samples,
                covered_command_count=len(controller.covered_commands),
                expected_command_count=len(plan.commands),
            )
        if scenario.name == "duplicate_conflict":
            controller = _start_controller(plan, controller_dir, clock)
            batch = next(iter(_source(plan, controller, batch_size=3)))
            controller.ingest_feedback(batch)
            conflict = json.loads(json.dumps(batch))
            conflict["samples"][0]["q_actual"][0] += 0.001
            controller.ingest_feedback(conflict)
            raise AssertionError("duplicate conflict was accepted")
        if scenario.name == "out_of_order_feedback":
            controller = _start_controller(plan, controller_dir, clock)
            source = list(_source(plan, controller, batch_size=3))
            controller.ingest_feedback(source[0])
            source[1]["samples"][0]["command_index"] = 0
            source[1]["samples"][0]["segment_id"] = 0
            controller.ingest_feedback(source[1])
            raise AssertionError("out-of-order feedback was accepted")
        if scenario.name == "feedback_binding":
            controller = _start_controller(plan, controller_dir, clock)
            batch = next(iter(_source(plan, controller, batch_size=3)))
            wrong = json.loads(json.dumps(batch))
            wrong["serial"] = "other-serial"
            controller.ingest_feedback(wrong)
            raise AssertionError("wrong feedback binding was accepted")
        if scenario.name == "partial_feedback":
            controller = _start_controller(plan, controller_dir, clock)
            controller.ingest_feedback(
                next(iter(_source(plan, controller, batch_size=3)))
            )
            controller.finish()
            raise AssertionError("partial feedback was marked complete")
        if scenario.name == "support_missing_feedback":
            controller = _start_controller(plan, controller_dir, clock)
            for batch in _source(plan, controller, batch_size=3):
                filtered = json.loads(json.dumps(batch))
                filtered["samples"] = [
                    sample
                    for sample in filtered["samples"]
                    if sample["segment_id"] != 0
                ]
                filtered["completed"] = False
                if filtered["samples"]:
                    controller.ingest_feedback(filtered)
            controller.finish()
            raise AssertionError("missing support feedback was complete")
        if scenario.name == "interrupt_recover":
            controller = _start_controller(plan, controller_dir, clock)
            source = list(_source(plan, controller, batch_size=3))
            controller.ingest_feedback(source[0])
            checkpoint = controller_dir / "checkpoint.json"
            resumed = MockController.resume(checkpoint, plan, clock=clock)
            resumed.recover(
                explicit_mock_recovery=True,
                operator="reliability-operator",
                acknowledge_hold=True,
            )
            resumed.confirm("reliability-operator")
            resumed.start()
            remaining = _source(
                plan,
                resumed,
                batch_size=3,
                start_index=resumed.next_command_index,
                start_sequence=resumed.next_sequence,
            )
            resumed.run_mock(remaining)
            resumed.set_quality_evidence(_quality(plan, controller=resumed))
            report = resumed.finish()
            return _finish_result(
                scenario,
                resumed,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "blocked_segment":
            controller, report = _run_to_finish(
                plan,
                controller_dir,
                clock,
                quality_evidence=_quality(plan),
            )
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "happy_task_complete":
            controller, report = _run_to_finish(
                plan,
                controller_dir,
                clock,
                quality_evidence=_quality(plan),
            )
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "receipt_hashes":
            controller, report = _run_to_finish(
                plan, controller_dir, clock, quality_evidence=_quality(plan)
            )
            receipt = _read_json(controller_dir / "controller_receipt.json")
            for role in (
                "plan",
                "program",
                "feedback",
                "checkpoint",
                "events",
                "completion_report",
                "feedback_journal",
                "quality_evidence",
            ):
                reference = receipt[role]
                if sha256_file(reference["path"]) != reference["sha256"]:
                    raise AssertionError(f"{role} receipt hash mismatch")
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "happy_execution_only":
            controller, report = _run_to_finish(plan, controller_dir, clock)
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "quality_partial":
            controller, report = _run_to_finish(
                plan,
                controller_dir,
                clock,
                quality_evidence=_quality(plan, status="partial"),
            )
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        if scenario.name == "duplicate_idempotent":
            controller = _start_controller(plan, controller_dir, clock)
            source = list(_source(plan, controller, batch_size=3))
            controller.ingest_feedback(source[0])
            controller.ingest_feedback(source[0])
            controller.run_mock(source[1:])
            controller.set_quality_evidence(_quality(plan))
            report = controller.finish()
            before = {
                path.name: sha256_file(path)
                for path in controller_dir.iterdir()
                if path.is_file()
            }
            replay = controller.ingest_feedback(source[-1])
            after = {
                path.name: sha256_file(path)
                for path in controller_dir.iterdir()
                if path.is_file()
            }
            if (
                not replay["idempotent"]
                or replay["accepted"]
                or before != after
            ):
                raise AssertionError(
                    "terminal duplicate mutated controller evidence"
                )
            return _finish_result(
                scenario,
                controller,
                report,
                _relative(controller_dir, loop_dir.parent.parent),
            )
        raise AssertionError(f"unhandled scenario {scenario.name}")
    except (
        PlanRejected,
        FeedbackRejected,
        StateRejected,
        CompletionRejected,
        RecoveryRejected,
        ControllerError,
    ) as error:
        report = (
            controller.completion_report() if "controller" in locals() else None
        )
        return _result(
            scenario,
            observed="reject",
            code=getattr(error, "code", type(error).__name__),
            state=(controller.state if "controller" in locals() else None),
            plan_sha256=(plan.plan_sha256 if "plan" in locals() else None),
            workspace=_relative(controller_dir, loop_dir.parent.parent),
            error=str(error),
            feedback_samples=(
                controller.feedback_samples if "controller" in locals() else 0
            ),
            covered_command_count=(
                len(controller.covered_commands)
                if "controller" in locals()
                else 0
            ),
            expected_command_count=(
                len(plan.commands) if "plan" in locals() else 0
            ),
            execution_complete=report["execution_complete"]
            if report
            else False,
            task_quality_complete=report["task_quality_complete"]
            if report
            else False,
            quality_reasons=report["quality"]["reasons"] if report else (),
            quality_region_failures=(
                report["quality"].get("region_failures", []) if report else ()
            ),
        )
    except Exception as error:  # noqa: BLE001 - fault matrix boundary
        return _result(
            scenario,
            observed="unexpected",
            code=type(error).__name__,
            state=(controller.state if "controller" in locals() else None),
            plan_sha256=(plan.plan_sha256 if "plan" in locals() else None),
            workspace=_relative(controller_dir, loop_dir.parent.parent),
            error=f"{type(error).__name__}: {error}",
            unexpected_exception=True,
        )


def _matches(scenario, result):
    if scenario.expected == "reject":
        if scenario.name in {"quality_binding", "quality_hash_changed"}:
            expected_covered = result["expected_command_count"]
        elif scenario.name in {
            "duplicate_conflict",
            "out_of_order_feedback",
            "expired_during_execution",
            "partial_feedback",
            "feedback_dropout",
            "feedback_stream_ended",
            "source_changed_during_execution",
            "program_hash_changed",
            "checkpoint_counter_corrupt",
            "checkpoint_log_binding",
            "clock_regression",
        }:
            expected_covered = 3
        else:
            expected_covered = 0
        return (
            result["observed"] == "reject"
            and result["code"] == scenario.code
            and result["state"] in {None, "HELD"}
            and not result["execution_complete"]
            and not result["task_quality_complete"]
            and result["covered_command_count"] == expected_covered
            and result["feedback_samples"] == expected_covered
        )
    if scenario.expected == "cancel":
        return (
            result["observed"] == "cancelled" and result["state"] == "CANCELLED"
        )
    if scenario.expected == "task_complete":
        return (
            result["observed"] == "finished"
            and result["state"] == "FINISHED"
            and result["execution_complete"]
            and result["task_quality_complete"]
            and result["covered_command_count"]
            == result["expected_command_count"]
        )
    if scenario.expected == "execution_complete":
        return (
            result["observed"] == "finished"
            and result["state"] == "FINISHED"
            and result["execution_complete"]
            and not result["task_quality_complete"]
            and result["covered_command_count"]
            == result["expected_command_count"]
        )
    raise AssertionError(f"unknown expectation {scenario.expected}")


def run_virtual_reliability(
    hours=DEFAULT_VIRTUAL_HOURS,
    *,
    heartbeat_s=300.0,
    boot_id="virtual-boot-20261123",
    minimum_hours=DEFAULT_VIRTUAL_HOURS,
):
    """Generate a heartbeat trace on a virtual clock without sleeping."""
    for value in (hours, heartbeat_s, minimum_hours):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("virtual time values must be finite numbers")
    try:
        hours = float(hours)
        heartbeat_s = float(heartbeat_s)
        minimum_hours = float(minimum_hours)
    except OverflowError as error:
        raise ValueError(
            "virtual time values exceed the finite numeric range"
        ) from error
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("virtual hours must be positive")
    if (
        not math.isfinite(heartbeat_s)
        or heartbeat_s <= 0
        or heartbeat_s > MAX_HEARTBEAT_GAP
    ):
        raise ValueError("heartbeat interval must be in (0, 3600] seconds")
    if not math.isfinite(minimum_hours) or minimum_hours <= 0:
        raise ValueError("minimum virtual hours must be positive and finite")
    if (
        not isinstance(boot_id, str)
        or not boot_id.strip()
        or boot_id != boot_id.strip()
    ):
        raise ValueError("boot_id must be a non-empty explicit identifier")
    started = time.perf_counter()
    total_seconds = hours * 3600.0
    estimated_count = total_seconds / heartbeat_s
    if (
        not math.isfinite(estimated_count)
        or estimated_count + 2 > MAX_VIRTUAL_HEARTBEATS
    ):
        raise ValueError("virtual trace exceeds the bounded heartbeat budget")
    heartbeats = []
    stamp = 0.0
    while stamp < total_seconds:
        heartbeats.append({"t_s": stamp, "healthy": True, "boot_id": boot_id})
        stamp = min(stamp + heartbeat_s, total_seconds)
    heartbeats.append(
        {"t_s": total_seconds, "healthy": True, "boot_id": boot_id}
    )
    wall_seconds = time.perf_counter() - started
    gaps = [
        after["t_s"] - before["t_s"]
        for before, after in zip(heartbeats, heartbeats[1:])
    ]
    max_gap = max(gaps) if gaps else 0.0
    elapsed = heartbeats[-1]["t_s"] - heartbeats[0]["t_s"]
    meets_minimum = elapsed / 3600.0 >= minimum_hours - 1e-9
    return {
        "schema": "daiyi-reliability-evidence-v1",
        "source_kind": "virtual_reliability",
        "clock_kind": "virtual",
        "boot_id": boot_id,
        "heartbeats": heartbeats,
        "complete": meets_minimum and max_gap <= MAX_HEARTBEAT_GAP,
        "virtual_hours": elapsed / 3600.0,
        "target_hours": minimum_hours,
        "meets_target": meets_minimum,
        "heartbeat_interval_s": heartbeat_s,
        "heartbeat_count": len(heartbeats),
        "maximum_gap_s": max_gap,
        "failures": [],
        "accelerated_coverage": True,
        "real_800h_pending": True,
        "wall_seconds": wall_seconds,
        "scope": (
            "accelerated virtual heartbeat scheduling; not real 800-hour "
            "software reliability evidence"
        ),
    }


def _virtual_rule_checks(output, trace):
    """Exercise the enterprise evaluator with labeled virtual fault fixtures."""
    from .enterprise_acceptance import artifact_reference, assess_bundle

    root = output / "virtual_rules"
    root.mkdir()
    cases = []
    for name in (
        "healthy",
        "health_fault",
        "clock_restart",
        "heartbeat_gap",
        "clock_mismatch",
    ):
        directory = root / name
        directory.mkdir()
        value = json.loads(json.dumps(trace, allow_nan=False))
        midpoint = len(value["heartbeats"]) // 2
        if name == "health_fault":
            value["heartbeats"][midpoint].update(
                healthy=False, reason="synthetic_injected_failure"
            )
        elif name == "clock_restart":
            value["heartbeats"][midpoint]["t_s"] = 0.0
        elif name == "heartbeat_gap":
            value["heartbeats"] = [
                value["heartbeats"][0],
                value["heartbeats"][-1],
            ]
            value["heartbeats"][-1]["t_s"] = max(
                value["heartbeats"][-1]["t_s"], MAX_HEARTBEAT_GAP + 1.0
            )
        elif name == "clock_mismatch":
            value["clock_kind"] = "wall_monotonic"
        path = directory / "trace.json"
        _write_json(path, value)
        bundle = {
            "schema": "daiyi-enterprise-input-v1",
            "artifacts": {
                "reliability_receipt": artifact_reference(
                    path, relative_to=directory
                )
            },
        }
        _write_json(directory / "input.json", bundle)
        report = assess_bundle(bundle, base=directory)
        _write_json(directory / "assessment.json", report)
        rows = {row["id"]: row for row in report["matrix"]}
        expected = "pass" if name == "healthy" and trace["complete"] else "fail"
        observed = rows["virtual_reliability"]["evidence_status"]
        measured = rows["virtual_reliability"]["measured"] or {}
        rejected = [
            failure
            for failure in report["failures"]
            if failure["role"] == "reliability_receipt"
        ]
        rule_failures = [
            failure
            for failure in report["failures"]
            if failure["role"] in {"reliability_receipt", "virtual_reliability"}
        ]
        other_failures = [
            failure
            for failure in report["failures"]
            if failure["role"]
            not in {"reliability_receipt", "virtual_reliability"}
        ]
        codes = {failure["code"] for failure in rejected}
        if name == "healthy":
            rule_match = not rejected
        elif name == "health_fault":
            rule_match = not rejected and any(
                failure["reason"] == "synthetic_injected_failure"
                for failure in measured.get("failures", [])
            )
        elif name == "heartbeat_gap":
            rule_match = (
                not rejected
                and measured.get("maximum_gap_seconds", 0) > MAX_HEARTBEAT_GAP
            )
        else:
            expected_code = (
                "reliability_restart"
                if name == "clock_restart"
                else "reliability_clock_mismatch"
            )
            rule_match = codes == {expected_code}
        real_pending = (
            rows["continuous_reliability"]["status"] == "unknown"
            and not report["enterprise_acceptance_passed"]
        )
        cases.append(
            {
                "case": name,
                "expected": expected,
                "observed": observed,
                "match": (
                    observed == expected
                    and rule_match
                    and real_pending
                    and not other_failures
                ),
                "fault_rule_matched": rule_match,
                "real_800h_pending": real_pending,
                "expected_rejection_reasons": rule_failures,
                "evaluation_input_failures": other_failures,
                "measured": rows["virtual_reliability"]["measured"],
                "reason": rows["virtual_reliability"]["reason"],
                "artifacts": [
                    _artifact(
                        path,
                        output,
                        "virtual_rule_evidence",
                        "synthetic rule fixture",
                    )
                    for path in sorted(directory.iterdir())
                    if path.is_file()
                ],
            }
        )
    summary = {
        "schema": "daiyi-virtual-reliability-rule-checks-v1",
        "scope": "constructed clocks and faults; not a physical soak or failure-rate estimate",
        "case_count": len(cases),
        "matched": sum(case["match"] for case in cases),
        "cases": cases,
        "real_800h_pending": True,
    }
    _write_json(root / "summary.json", summary)
    return summary


def _historical_quality(plan):
    evidence = plan.result_evidence or {}
    identity = plan.identity
    suffix = plan.plan_sha256[:16]
    return {
        **identity.to_dict(),
        "plan_sha256": plan.plan_sha256,
        "request_id": f"mock-request-{suffix}",
        "execution_id": f"mock-execution-{suffix}-01",
        "execution_attempt": 1,
        "schema": QUALITY_SCHEMA,
        "source_kind": "historical_simulation_result",
        "independent": False,
        "real_evidence": False,
        "task_status": evidence.get("task_status", "unknown"),
        "covered_segments": [],
        "segment_results": {},
        "observational_release": evidence.get("observational_release"),
        "goal_complete": evidence.get("goal_complete"),
        "part_id": identity.part_id,
        "serial": identity.serial,
        "cad_revision": identity.cad_revision,
        "fixture_revision": identity.fixture_revision,
        "scope": "archived simulation result; not independent measurement",
    }


def _validate_historical(output, run_dir):
    files = [
        run_dir / "motion_commands.json",
        run_dir / "preflight.json",
        run_dir / "estun_path_preview.csv",
        run_dir / "result.json",
    ]
    if (run_dir / "source_hashes.json").is_file():
        files.append(run_dir / "source_hashes.json")
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"historical run is missing files: {missing}")
    before = {path.name: sha256_file(path) for path in files}
    now_s = time.time()
    plan = load_plan_from_run(
        run_dir,
        issued_at_s=now_s,
        expires_at_s=now_s + 3600.0,
    )
    clock = VirtualClock(now_s + 1.0)
    mock_dir = output / "historical_mock"
    result = run_mock_cycle(
        plan,
        mock_dir,
        clock=clock,
        batch_size=64,
        quality=_historical_quality(plan),
    )
    after = {path.name: sha256_file(path) for path in files}
    controller = result["controller"]
    report = controller.completion_report()
    changed = {
        name: {"before": before[name], "after": after[name]}
        for name in before
        if before[name] != after[name]
    }
    producer = (plan.result_evidence or {}).get("historical_source_manifest")
    source_audit = []
    for name, declared in (producer or {}).get("declared_hashes", {}).items():
        if not name.endswith(".py") or Path(name).name != name:
            continue
        snapshot = run_dir / "source_snapshot" / name
        current = project.SOURCE / name
        source_audit.append(
            {
                "name": name,
                "declared_historical_sha256": declared,
                "archived_snapshot": (
                    _input(
                        snapshot,
                        "archived_source",
                        "historical Python source; not executed",
                    )
                    if snapshot.is_file()
                    else None
                ),
                "snapshot_matches_declared": (
                    sha256_file(snapshot) == declared
                    if snapshot.is_file()
                    else None
                ),
                "current_reference_sha256": sha256_file(current)
                if current.is_file()
                else None,
                "current_reference_executed": False,
            }
        )
    validation = {
        "schema": "daiyi-historical-plan-validation-v1",
        "run_directory": str(run_dir),
        "source_hashes": before,
        "source_hashes_after_mock": after,
        "sources_unchanged": not changed,
        "changed_sources": changed,
        "historical_source_scope": {
            "manifest": producer,
            "python_source_audit": source_audit,
            "current_source_drift": [
                item["name"]
                for item in source_audit
                if item["current_reference_sha256"]
                != item["declared_historical_sha256"]
            ],
            "scope": "archived provenance only; current producer code was not run",
        },
        "plan_sha256": plan.plan_sha256,
        "plan_summary": plan.summary(),
        "mock_cycle": {
            "state": controller.state,
            "execution_complete": report["execution_complete"],
            "task_quality_complete": report["task_quality_complete"],
            "feedback_complete": report["feedback_complete"],
            "command_count": len(plan.commands),
            "admitted_segments": list(plan.admitted_segments),
            "blocked_segments": list(plan.blocked_segments),
            "quality_status": report["quality"]["status"],
            "quality_reasons": report["quality"]["reasons"],
            "receipt": str(mock_dir / "controller_receipt.json"),
        },
        "reverified": {
            "collision": False,
            "dynamics": False,
            "motion": False,
            "tracking": False,
            "quality": False,
        },
        "scope": (
            "historical accepted commands, preflight rows and candidate "
            "preview were parsed and hash-bound; no MuJoCo collision, motion "
            "or quality result was re-verified"
        ),
    }
    _write_json(output / "historical_validation.json", validation)
    return plan, validation


def _loop_summary(records):
    counts = Counter(record["scenario"] for record in records)
    matched = Counter(
        record["scenario"] for record in records if record["match"]
    )
    return {
        "actual_cycles": len(records),
        "scenario_counts": dict(sorted(counts.items())),
        "scenario_matched": dict(sorted(matched.items())),
        "expected_rejections": sum(
            record["expected"] == "reject" for record in records
        ),
        "observed_expected_rejections": sum(
            record["expected"] == "reject" and record["match"]
            for record in records
        ),
        "expected_rejection_codes": dict(
            sorted(
                Counter(
                    record["expected_code"]
                    for record in records
                    if record["expected"] == "reject"
                ).items()
            )
        ),
        "observed_rejection_codes": dict(
            sorted(
                Counter(
                    record["code"]
                    for record in records
                    if record["observed"] == "reject"
                ).items()
            )
        ),
        "expected_accepts": sum(
            record["expected"] != "reject" for record in records
        ),
        "observed_expected_accepts": sum(
            record["expected"] != "reject" and record["match"]
            for record in records
        ),
        "mismatches": sum(not record["match"] for record in records),
        "unexpected_exceptions": sum(
            record["unexpected_exception"] for record in records
        ),
        "execution_complete_cycles": sum(
            record["execution_complete"] for record in records
        ),
        "task_quality_complete_cycles": sum(
            record["task_quality_complete"] for record in records
        ),
        "sample_count": len(records),
        "control_feedback_samples": sum(
            record["feedback_samples"] for record in records
        ),
        "covered_command_count": sum(
            record["covered_command_count"] for record in records
        ),
        "quality_fixture_complete_cycles": sum(
            record["task_quality_complete"] for record in records
        ),
        "real_task_quality_complete_cycles": 0,
        "quality_scope": "synthetic rule fixtures; no part quality was measured",
        "expected_process_exception_cycles": counts.get("process_exception", 0),
    }


def _run_loops(output, run_dir, loops, seed):
    loop_root = output / "loops"
    loop_root.mkdir(parents=True)
    records = []
    for index in range(loops):
        scenario = SCENARIOS[index % len(SCENARIOS)]
        loop_dir = loop_root / f"{index:03d}"
        loop_dir.mkdir()
        started = time.perf_counter()
        try:
            record = _execute_scenario(loop_dir, scenario, seed + index * 7)
        except Exception as error:  # noqa: BLE001 - suite boundary
            record = _result(
                scenario,
                observed="unexpected",
                code=type(error).__name__,
                error=f"{type(error).__name__}: {error}",
                unexpected_exception=True,
                workspace=_relative(loop_dir, output),
            )
        record["loop"] = index
        record["seed"] = seed + index * 7
        record["wall_seconds"] = time.perf_counter() - started
        record["match"] = _matches(scenario, record)
        record["inputs"] = [
            _artifact(
                path,
                output,
                "synthetic_case_input",
                "synthetic fault fixture; includes intentionally rejected inputs",
            )
            for path in sorted((loop_dir / "case").iterdir())
            if path.is_file()
        ]
        record["artifacts"] = [
            _artifact(
                path,
                output,
                "loop_evidence",
                "offline controller, injection or recovery evidence",
            )
            for path in sorted(loop_dir.rglob("*"))
            if path.is_file() and path.parent != loop_dir / "case"
        ]
        records.append(record)
    with (loop_root / "index.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
    summary = _loop_summary(records)
    _write_json(loop_root / "summary.json", summary)
    _write_json(
        loop_root / "artifacts.json",
        {
            "schema": "daiyi-reliability-loop-artifacts-v1",
            "scope": "synthetic loop inputs and outputs; no real measurements",
            "artifacts": [
                artifact
                for record in records
                for artifact in (*record["inputs"], *record["artifacts"])
            ],
        },
    )
    return records, summary


def _report(evidence):
    metrics = evidence["metrics"]
    historical = evidence["historical_validation"]
    loop = metrics["loops"]
    virtual = metrics["virtual"]
    virtual_rules = metrics["virtual_fault_rules"]
    archived_files = sum(
        item["role"] in {"historical_run", "historical_source_manifest"}
        for item in evidence["inputs"]
    )
    legacy_preflight = historical["plan_summary"]["preflight_binding"][
        "legacy_unbound_preflight"
    ]
    lines = [
        "# 离线控制回执与可靠性压力报告",
        "",
        "日期为 2026 年 11 月 23 日。范围仅限离线命令校验、显式 mock "
        "状态机、重复/乱序/断点恢复和心跳调度；**没有连接 ESTUN 控制器、"
        "物理 socket、MuJoCo 或外部消息服务**。",
        "",
        "## 历史仿真输入",
        "",
        f"- 输入目录：`{historical['run_directory']}`，只读使用。",
        f"- 已接受命令 {historical['plan_summary']['command_count']} 条，"
        f"获准段 {historical['plan_summary']['admitted_segments']}，"
        f"拦截段 {historical['plan_summary']['blocked_segments']}。",
        f"- 计划哈希 `{historical['plan_sha256']}`；{archived_files} 份历史文件 mock "
        "前后哈希一致。",
        f"- 原 `result.json` 任务状态为 "
        f"`{historical['plan_summary']['result_evidence']['task_status']}`；"
        "mock 执行齐全也不代表任务质量完成。",
        "- 候选 `estun_path_preview.csv` 仅按哈希绑定，"
        "`executable=false`，从未作为控制器程序。",
        "- 未复验碰撞、动力学、跟踪误差或真实运动；历史结论仍按原记录解释。",
        (
            "- 历史预检没有命令哈希，`legacy_unbound_preflight=true`；本次仅将"
            "归档文件与其段、检测编号及覆盖计数绑定到 mock 计划，"
            "不构成当前碰撞或跟踪准入复验。"
            if legacy_preflight
            else "- 预检与命令哈希匹配；预检本身未在当前代码重新执行。"
        ),
        "- 历史 `source_hashes.json` 与 Python 快照单列于 "
        "`historical_source_scope`；当前 producer 源码漂移仍保留，"
        "当前离线模块与测试另外冻结到 `source_snapshot/`。",
        "",
        "## 控制器状态机",
        "",
        "计划 → 确认 → mock 执行/反馈 → 完成/取消/拒绝；异常、乱序、"
        "重复冲突、过期、来源变更和完成回调不全都默认进入 `HELD`。"
        "恢复必须重新校验全部来源、明确声明 mock 恢复、确认 hold 原因，"
        "并再次确认计划后才允许执行。",
        "",
        "- 计划绑定 part/serial/CAD revision/fixture/tool/frame/units、"
        "时间窗、完整获准段和所有输入哈希。",
        "- 六轴限位直接解析 "
        "`assets/ier220_workstation/scene.xml`（含 joint range 与 "
        "ctrlrange 交集），不导入 MuJoCo。",
        "- 时间轴严格递增；pose 为 world 米制 4x4；point 为工件坐标米制；"
        "所有数值有限且关节在 XML 限位内。",
        "- 只有整段预检 `passed` 且命令块完整的加工段可执行；"
        "被拦截段出现在命令文件会直接拒绝。",
        "- 重复完全相同的反馈批次幂等；同序号不同内容、序号倒退、"
        "时间倒退和命令索引倒退均拒绝并 hold。",
        "- 整批验证完成后才提交反馈日志与覆盖计数；序号与命令索引"
        "必须连续。完成态仅接受原批次的幂等重发，不改写已冻结回执。",
        "- 质量必须另行绑定 plan/request/execution/execution_attempt "
        "及完整工装、磨具、坐标和单位；明确观测放行与目标完成，"
        "保留过磨、负公差风险和非法视角的失败原因。",
        "- 默认 5 s mock 反馈间隔仅在 ingest/poll 调用时检查；"
        "没有后台实时监视或硬件急停验证。",
        "",
        "## 低资源循环结果",
        "",
        f"- 实际循环：{loop['actual_cycles']}（最低要求 "
        f"{loop['minimum_required']}），覆盖 {len(loop['scenario_counts'])} 类场景。",
        f"- 场景样本 {loop['sample_count']}；接受的合成反馈 "
        f"{loop['control_feedback_samples']} 条，覆盖命令 "
        f"{loop['covered_command_count']} 条。",
        f"- 预期拒绝：{loop['expected_rejections']}，观测符合："
        f"{loop['observed_expected_rejections']}。",
        f"- 预期接受/完成：{loop['expected_accepts']}，观测符合："
        f"{loop['observed_expected_accepts']}。",
        f"- 非预期异常：{loop['unexpected_exceptions']}；"
        f"分类不一致：{loop['mismatches']}。",
        f"- 控制执行完整 {loop['execution_complete_cycles']} 次；"
        f"合成质量规则 fixture 完整 {loop['quality_fixture_complete_cycles']} 次；"
        f"真实任务质量完成 {loop['real_task_quality_complete_cycles']} 次。"
        "前两项仅为接口规则样例，不是实物整件验收率。",
        f"- 实际墙钟 {metrics['wall_seconds']:.3f} s，CPU "
        f"{metrics['cpu_seconds']:.3f} s，峰值 RSS "
        f"{metrics['peak_rss_kb'] / 1024:.1f} MiB。"
        "峰值资源由 `resource.getrusage(RUSAGE_SELF).ru_maxrss` 统计，"
        "未启用会改变内存基线的分配追踪器。",
        "",
        "| 故障场景 | 次数 | 符合 |",
        "| --- | ---: | ---: |",
    ]
    for name, count in sorted(loop["scenario_counts"].items()):
        lines.append(
            f"| `{name}` | {count} | {loop['scenario_matched'].get(name, 0)} |"
        )
    lines.extend(
        [
            "",
            "## 虚拟 800 小时",
            "",
            f"- 虚拟时钟 {virtual['virtual_hours']:.1f} h，"
            f"心跳 {virtual['heartbeat_count']} 次，最大间隔 "
            f"{virtual['maximum_gap_s']:.1f} s。",
            f"- `accelerated_coverage=true`，"
            f"`real_800h_pending={str(evidence['real_800h_pending']).lower()}`。",
            f"- 虚拟轨迹构造墙钟 {virtual['wall_seconds']:.6f} s；"
            f"另外检查健康、故障、时钟重启、心跳缺口和时钟类型失配 "
            f"{virtual_rules['case_count']} 个规则样例，"
            f"{virtual_rules['matched']} 个符合预期。",
            "- 8.0e2 h 虚拟覆盖只证明调度器能在该时间轴上产生连续心跳，"
            "**不能作为企业 800 小时无故障验收**。真实验收还需要同一 "
            "boot_id 的墙钟日志、真实控制器回执、零重启和零故障。",
            "- 故障矩阵的输入、次数和注入时间由演练预设；"
            "这些统计不能推断真实失效率、MTBF 或实际长期资源稳定性。",
            "",
            "## 边界与待补证据",
            "",
        ]
    )
    for item in evidence["real_data_pending"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 根集成 API",
            "",
            "```python",
            "from daiyi_grinding.offline_controller import (",
            "    load_plan_from_run, MockController, MockFeedbackSource,",
            "    run_mock_cycle,",
            ")",
            "from daiyi_grinding.reliability_stress import (",
            "    run_reliability_stress, run_virtual_reliability,",
            ")",
            "",
            "plan = load_plan_from_run(run_dir, valid_for_s=3600)",
            "controller = MockController(plan, workdir, clock=clock)",
            "controller.confirm(operator)",
            "controller.start()",
            "controller.run_mock(MockFeedbackSource(",
            "    plan, controller.request_id, controller.execution_id,",
            "))",
            "binding = controller.quality_binding()",
            "# 质量产生方须证明观测来自此绑定；不得给旧质量结果重新贴绑定。",
            "controller.set_quality_evidence(independent_quality)",
            "report = controller.finish()",
            "```",
            "",
            "根流程应把 `execution_complete` 与 `task_quality_complete` "
            "分开使用；完成回调只关闭执行，不能替代独立质量、缺失段和"
            "反馈齐全判定。",
            "",
            "## 原始产物",
            "",
            "- `historical_validation.json`：历史只读校验与 mock 回执。",
            "- `loops/index.jsonl`、`loops/summary.json`：逐循环结果与统计。",
            "- `loops/artifacts.json`：每次合成输入与输出的路径、大小和 SHA256。",
            "- `source_inputs.json`、`source_snapshot/`：本次离线源码冻结。",
            "- `virtual_rules/summary.json`：五类虚拟时钟规则及其企业评价结果。",
            "- `reliability_receipt.json`：虚拟时钟心跳证据。",
            "- `evidence.json`：任务、范围、命令、输入哈希、指标、失败和"
            "待补真实数据。",
            "",
        ]
    )
    return "\n".join(lines)


def run_reliability_stress(
    output,
    *,
    run_dir=DEFAULT_RUN_DIR,
    loops=DEFAULT_LOOPS,
    virtual_hours=DEFAULT_VIRTUAL_HOURS,
    seed=20261123,
    enforce_minimum=True,
    commands=None,
):
    """Run historical validation, the fault matrix and virtual reliability."""
    output = Path(output).resolve()
    if isinstance(loops, bool) or not isinstance(loops, int) or loops < 1:
        raise ValueError("loops must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if isinstance(virtual_hours, bool) or not isinstance(
        virtual_hours, (int, float)
    ):
        raise ValueError("virtual hours must be positive and finite")
    try:
        virtual_hours = float(virtual_hours)
    except OverflowError as error:
        raise ValueError(
            "virtual hours exceed the finite numeric range"
        ) from error
    if not math.isfinite(virtual_hours) or virtual_hours <= 0:
        raise ValueError("virtual hours must be positive and finite")
    if loops < MINIMUM_LOOPS and enforce_minimum:
        raise ValueError(
            f"at least {MINIMUM_LOOPS} low-resource loops are required"
        )
    if virtual_hours < DEFAULT_VIRTUAL_HOURS and enforce_minimum:
        raise ValueError("the virtual reliability target is 800 hours")
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    cpu_started = time.process_time()
    run_dir = Path(run_dir).resolve()
    inputs = _run_inputs(run_dir)
    source_artifacts = _freeze_offline_sources(output, inputs)
    optional_modules_before = {
        name
        for name in sys.modules
        if name.split(".")[0] in {"mujoco", "torch"}
    }
    plan, historical = _validate_historical(output, run_dir)
    records, loop_summary = _run_loops(output, run_dir, loops, seed)
    virtual = run_virtual_reliability(
        virtual_hours,
        minimum_hours=DEFAULT_VIRTUAL_HOURS,
    )
    virtual.update(
        {
            key: getattr(plan.identity, key)
            for key in ("part_id", "serial", "cad_revision", "fixture_revision")
        }
    )
    _write_json(output / "reliability_receipt.json", virtual)
    virtual_rules = _virtual_rule_checks(output, virtual)
    failures = []
    for record in records:
        if not record["match"]:
            failures.append(
                {
                    "loop": record["loop"],
                    "scenario": record["scenario"],
                    "expected": record["expected"],
                    "expected_code": record["expected_code"],
                    "observed": record["observed"],
                    "code": record["code"],
                    "state": record["state"],
                    "error": record["error"],
                    "unexpected_exception": record["unexpected_exception"],
                    "workspace": record["workspace"],
                }
            )
    for case in virtual_rules["cases"]:
        if not case["match"]:
            failures.append(
                {
                    "scope": "virtual_fault_rule",
                    "case": case["case"],
                    "expected": case["expected"],
                    "observed": case["observed"],
                    "fault_rule_matched": case["fault_rule_matched"],
                    "expected_rejection_reasons": case[
                        "expected_rejection_reasons"
                    ],
                    "evaluation_input_failures": case[
                        "evaluation_input_failures"
                    ],
                    "real_800h_pending": case["real_800h_pending"],
                }
            )
    wall_seconds = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    changed_inputs = []
    for item in inputs:
        path = Path(item["path"])
        actual = sha256_file(path) if path.is_file() else None
        if actual != item["sha256"]:
            changed_inputs.append(
                {
                    "path": item["path"],
                    "role": item["role"],
                    "before_sha256": item["sha256"],
                    "after_sha256": actual,
                }
            )
    for item in changed_inputs:
        if item["role"] != "reference_source":
            failures.append({"code": "input_changed_during_run", **item})
    imported_optional = sorted(
        {
            name.split(".")[0]
            for name in sys.modules.keys() - optional_modules_before
            if name.split(".")[0] in {"mujoco", "torch"}
        }
    )
    if imported_optional:
        failures.append(
            {
                "code": "unexpected_heavy_import",
                "modules": imported_optional,
            }
        )
    metrics = {
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "peak_rss_kb": peak_rss,
        "peak_resource_method": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
        "minimum_required_loops": MINIMUM_LOOPS,
        "loops": {
            **loop_summary,
            "minimum_required": MINIMUM_LOOPS,
            "meets_minimum": loop_summary["actual_cycles"] >= MINIMUM_LOOPS,
        },
        "historical": {
            "command_count": len(plan.commands),
            "admitted_segments": list(plan.admitted_segments),
            "blocked_segments": list(plan.blocked_segments),
            "plan_sha256": plan.plan_sha256,
            "mock_execution_complete": historical["mock_cycle"][
                "execution_complete"
            ],
            "mock_task_quality_complete": historical["mock_cycle"][
                "task_quality_complete"
            ],
            "sources_unchanged": historical["sources_unchanged"],
        },
        "virtual": {
            "virtual_hours": virtual["virtual_hours"],
            "heartbeat_count": virtual["heartbeat_count"],
            "maximum_gap_s": virtual["maximum_gap_s"],
            "meets_target": virtual["meets_target"],
            "complete": virtual["complete"],
            "wall_seconds": virtual["wall_seconds"],
            "accelerated_coverage": True,
            "real_800h_pending": True,
        },
        "virtual_fault_rules": {
            "case_count": virtual_rules["case_count"],
            "matched": virtual_rules["matched"],
            "real_800h_pending": True,
        },
    }
    _write_json(output / "metrics.json", metrics)
    evidence = {
        "schema": "daiyi-reliability-stress-evidence-v1",
        "task": (
            "enterprise offline control receipt and reliability for the "
            "eight-role evidence bundle; controller and validation are mock"
        ),
        "scope": {
            "offline_only": True,
            "mujoco_imported": "mujoco" in imported_optional,
            "torch_imported": "torch" in imported_optional,
            "preloaded_optional_modules": sorted(
                {name.split(".")[0] for name in optional_modules_before}
            ),
            "physical_socket_used": False,
            "external_message_used": False,
            "real_controller_used": False,
            "real_command_mapping_assumed": False,
            "historical_run_read_only": True,
            "collision_reverified": False,
            "motion_reverified": False,
            "quality_reverified": False,
            "real_800h_pending": True,
            "virtual_800h_is_coverage_not_acceptance": True,
        },
        "commands": commands
        or [
            [
                ".venv/bin/python",
                "-m",
                "daiyi_grinding.reliability_stress",
                "--output",
                str(output),
                "--loops",
                str(loops),
                "--virtual-hours",
                str(virtual_hours),
            ]
        ],
        "inputs": inputs,
        "source_consistency": {
            "snapshot_at": "before historical replay and fault loops",
            "changed_inputs": changed_inputs,
            "offline_sources_unchanged": not any(
                item["role"] == "source" for item in changed_inputs
            ),
            "scope": "current offline workload only; archived producer hashes remain historical",
        },
        "execution_environment": {
            "python": sys.executable,
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
            "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
        },
        "metrics": metrics,
        "failures": failures,
        "artifacts": [],
        "real_data_pending": REAL_DATA_PENDING,
        "real_800h_pending": True,
        "role_matrix": ROLE_MATRIX,
        "historical_validation": {
            "run_directory": historical["run_directory"],
            "plan_sha256": historical["plan_sha256"],
            "plan_summary": historical["plan_summary"],
            "sources_unchanged": historical["sources_unchanged"],
            "mock_cycle": historical["mock_cycle"],
            "reverified": historical["reverified"],
            "historical_source_scope": historical["historical_source_scope"],
            "scope": historical["scope"],
        },
        "virtual_reliability": {
            "receipt": "reliability_receipt.json",
            "virtual_hours": virtual["virtual_hours"],
            "heartbeat_count": virtual["heartbeat_count"],
            "maximum_gap_s": virtual["maximum_gap_s"],
            "complete": virtual["complete"],
            "meets_target": virtual["meets_target"],
            "accelerated_coverage": True,
            "real_800h_pending": True,
        },
        "virtual_fault_rules": virtual_rules,
    }
    report_path = output / "report.md"
    report_path.write_text(_report(evidence), encoding="utf-8")
    artifact_specs = [
        (
            output / "historical_validation.json",
            "historical_validation",
            "read-only historical plan validation and mock execution",
        ),
        (
            output / "source_inputs.json",
            "source_inputs",
            "start-of-workload inputs and separate current offline source snapshots",
        ),
        (
            output / "historical_mock/controller_receipt.json",
            "controller_receipt",
            "explicit offline mock execution receipt",
        ),
        (
            output / "historical_mock/feedback_evidence.json",
            "controller_feedback",
            "synthetic feedback; not a physical controller trace",
        ),
        (
            output / "historical_mock/checkpoint.json",
            "checkpoint",
            "final historical mock checkpoint",
        ),
        (
            output / "historical_mock/events.jsonl",
            "event_log",
            "historical mock event log",
        ),
        (
            output / "loops/index.jsonl",
            "loop_records",
            "per-loop scenario outcomes and expected classification",
        ),
        (
            output / "loops/summary.json",
            "loop_summary",
            "aggregate fault matrix statistics",
        ),
        (
            output / "loops/artifacts.json",
            "loop_artifacts",
            "hashes of every synthetic loop input and controller output",
        ),
        (
            output / "virtual_rules/summary.json",
            "virtual_fault_rules",
            "enterprise rule checks for constructed faults; real 800 h pending",
        ),
        (
            output / "reliability_receipt.json",
            "reliability_receipt",
            "virtual heartbeat evidence; real 800 h pending",
        ),
        (
            output / "metrics.json",
            "metrics",
            "real wall, CPU, RSS and coverage metrics",
        ),
        (
            report_path,
            "report",
            "human-readable findings and boundaries",
        ),
    ]
    for path, role, source in artifact_specs:
        evidence["artifacts"].append(_artifact(path, output, role, source))
    evidence["artifacts"].extend(source_artifacts)
    for case in virtual_rules["cases"]:
        evidence["artifacts"].extend(case["artifacts"])
    _write_json(output / "evidence.json", evidence)
    return evidence


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Validate one real archived motion command run, then run the "
            "offline controller fault matrix and virtual reliability trace."
        )
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--loops", type=int, default=DEFAULT_LOOPS)
    parser.add_argument(
        "--virtual-hours", type=float, default=DEFAULT_VIRTUAL_HOURS
    )
    parser.add_argument("--seed", type=int, default=20261123)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    output = Path(args.output).resolve()
    command = [
        sys.executable,
        "-m",
        "daiyi_grinding.reliability_stress",
        "--output",
        str(output),
        "--run-dir",
        args.run_dir,
        "--loops",
        str(args.loops),
        "--virtual-hours",
        str(args.virtual_hours),
        "--seed",
        str(args.seed),
    ]
    evidence = run_reliability_stress(
        output,
        run_dir=args.run_dir,
        loops=args.loops,
        virtual_hours=args.virtual_hours,
        seed=args.seed,
        enforce_minimum=True,
        commands=[command],
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "schema": evidence["schema"],
                "loops": evidence["metrics"]["loops"]["actual_cycles"],
                "scenario_count": len(
                    evidence["metrics"]["loops"]["scenario_counts"]
                ),
                "feedback_samples": evidence["metrics"]["loops"][
                    "control_feedback_samples"
                ],
                "execution_complete_cycles": evidence["metrics"]["loops"][
                    "execution_complete_cycles"
                ],
                "quality_fixture_complete_cycles": evidence["metrics"]["loops"][
                    "quality_fixture_complete_cycles"
                ],
                "real_task_quality_complete_cycles": evidence["metrics"][
                    "loops"
                ]["real_task_quality_complete_cycles"],
                "expected_rejections": evidence["metrics"]["loops"][
                    "expected_rejections"
                ],
                "observed_expected_rejections": evidence["metrics"]["loops"][
                    "observed_expected_rejections"
                ],
                "unexpected_exceptions": evidence["metrics"]["loops"][
                    "unexpected_exceptions"
                ],
                "mismatches": evidence["metrics"]["loops"]["mismatches"],
                "virtual_fault_rules": evidence["metrics"][
                    "virtual_fault_rules"
                ],
                "virtual_hours": evidence["metrics"]["virtual"][
                    "virtual_hours"
                ],
                "real_800h_pending": True,
                "wall_seconds": evidence["metrics"]["wall_seconds"],
                "peak_rss_kb": evidence["metrics"]["peak_rss_kb"],
                "failures": len(evidence["failures"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not evidence["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
