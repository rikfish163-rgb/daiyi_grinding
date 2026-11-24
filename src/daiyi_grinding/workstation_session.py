"""Checkpointed, operator-intervenable offline ESTUN workstation sessions.

Sessions never command hardware. ``DONE`` closes the bounded workflow; only
``observational_release`` reports the existing signed sensor acceptance gate.
A step executes one region segment, including its checked return to the fixture.
"""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import project

os.environ.setdefault("MUJOCO_GL", "egl")

SESSION_ROOT = project.RUNS / "enterprise_offline_20261123/sessions"
MUJOCO_LOCK = project.RUNS / "enterprise_offline_20261123/mujoco.lock"
STAGES = (
    "IDLE",
    "SCANNED",
    "DETECTED",
    "PLANNED",
    "EXECUTING",
    "RESCANNED",
    "DONE",
    "PAUSED",
    "ABORTED",
    "STOPPED_SAFETY",
)
TERMINAL_STAGES = {"DONE", "ABORTED"}
PARAMETER_DEFAULTS = {
    "feed_scale": 1.0,
    "normal_offset_mm": 0.0,
    "passes": 1,
    "dwell_scale": 1.0,
}


class ProcessAdapter(Protocol):
    """Propose segment parameters from the checkpoint's sensor evidence only."""

    def __call__(self, segment: dict, session_state: dict) -> dict | None:
        """Return feed/offset/passes/dwell overrides, or no change."""
        ...


class ReinspectionCalibrator(Protocol):
    """Attach local-reference measurements without replacing the safety gate."""

    def __call__(self, report: dict, session_state: dict) -> dict:
        """Return supplemental calibration evidence for process adaptation."""
        ...


class SessionBackend(Protocol):
    """Simulation boundary; the default implementation uses real MuJoCo."""

    def scan(self, output: Path, **options) -> dict: ...
    def plan(self, path: dict) -> tuple[list, list, dict]: ...
    def preflight(self, commands: list) -> dict: ...
    def execute(self, commands: list, **options) -> dict: ...
    def safe_retract(self, **options) -> dict: ...
    def save_checkpoint(self, output: Path) -> None: ...
    def stock_summary(self) -> dict: ...
    def close(self) -> None: ...


class SessionStateError(RuntimeError):
    """An action is incompatible with the current session state."""


class SessionSafetyError(RuntimeError):
    """A mandatory admission or measured-motion gate rejected execution."""


def _timestamp():
    return datetime.now(UTC).isoformat()


def _json_value(value):
    if isinstance(value, Path):
        return str(value)
    return value.tolist()


def _json_data(value):
    return json.loads(json.dumps(value, default=_json_value, allow_nan=False))


def _write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _truth_free(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"truth_used_for_control", "truth_used_for_decision"}:
                if item is not False:
                    raise SessionSafetyError(f"{key} 必须严格为 False")
            _truth_free(item)
    elif isinstance(value, list):
        for item in value:
            _truth_free(item)


def _observed_anchors(value):
    if isinstance(value, list):
        return [
            {
                key: copy.deepcopy(row[key])
                for key in (
                    "height_mm",
                    "sigma_mm",
                    "side",
                    "point",
                    "point_m",
                    "xyz_m",
                    "normal",
                    "normal_m",
                    "region_id",
                )
                if key in row
            }
            for row in value
            if isinstance(row, dict)
        ]
    if isinstance(value, dict):
        return {
            key: copy.deepcopy(value[key])
            for key in (
                "heights_mm",
                "sigma_mm",
                "sides",
                "points_m",
                "anchor_points_m",
                "normals_m",
                "anchor_estimates_mm",
                "anchor_bounds_mm",
            )
            if key in value
        }
    return value if value is None or type(value) is int else None


def _observed_baseline(value):
    if not isinstance(value, dict):
        return None
    return {
        **{
            key: _observed_anchors(value[key])
            for key in ("baseline", "protected", "machined")
            if key in value
        },
        **{
            key: copy.deepcopy(value[key])
            for key in ("source", "source_observation", "scan_index", "frame")
            if key in value
        },
    }


def _observed_report(report):
    fields = {
        "verified",
        "needs_rework",
        "uncertain",
        "overgrind",
        "insufficient",
        "negative_limit_risk",
        "automatic_regrind_allowed",
        "coverage_scope",
        "signed_limits_mm",
        "observational_release",
        "truth_used_for_decision",
        "invalid_observations",
        "uncertainty_model",
    }
    region_fields = {
        "id",
        "kind",
        "status",
        "status_reason",
        "positive_excess",
        "overgrind",
        "negative_limit_risk",
        "observations",
        "anchors",
        "observed_anchors",
        "anchor_coverage",
        "peak_mm",
        "trough_mm",
        "signed_peak_mm",
        "lower_peak_mm",
        "upper_peak_mm",
        "anchor_bounds_mm",
        "anchor_estimates_mm",
        "anchor_observation_counts",
        "anchor_positive_lower_mm",
        "anchor_positive_estimates_mm",
        "anchor_positive_observation_counts",
        "anchor_positive_view_indices",
        "points_m",
        "anchor_points_m",
    }
    return {
        **{key: copy.deepcopy(report[key]) for key in fields if key in report},
        "regions": [
            {
                key: copy.deepcopy(row[key])
                for key in region_fields
                if key in row
            }
            for row in report.get("regions", [])
        ],
        **(
            {"local_baseline": _observed_baseline(report["local_baseline"])}
            if "local_baseline" in report
            else {}
        ),
        **{
            key: _observed_anchors(report[key])
            for key in ("baseline_anchors", "machined")
            if key in report
        },
    }


def _observed_segment(segment):
    path_fields = {
        "segment_id",
        "detection_id",
        "kind",
        "xyz",
        "nominal_xyz",
        "observed_xyz",
        "normal",
        "normal_height_mm",
        "machining_required",
        "coordinate_frame",
        "material_allowance_mm",
        "clearance_mm",
        "tool_diameter_mm",
        "tool_width_mm",
        "view_index",
        "grasp_setup",
        "workpiece_id",
        "split_from",
    }
    return {
        **{
            key: copy.deepcopy(segment[key])
            for key in (
                "segment_id",
                "region_id",
                "workpiece_id",
                "grasp_setup",
                "params",
                "completed_passes",
                "resume_from_point",
                "round_index",
                "split_from",
            )
            if key in segment
        },
        "path": {
            **{
                key: copy.deepcopy(segment["path"][key])
                for key in path_fields
                if key in segment["path"]
            },
            "recipe": {
                key: copy.deepcopy(segment["path"]["recipe"][key])
                for key in ("tool", "feed_mm_s", "force_target_n", "overlap")
                if key in segment["path"]["recipe"]
            },
        },
    }


def _number(value, name, minimum, maximum):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} 须为 {minimum}–{maximum} 内的有限数")
    return float(value)


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 须为 {minimum}–{maximum} 内的整数")
    return value


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value
    ):
        raise ValueError("session_id 须为 1–64 位字母、数字、连字符或下划线")
    return value


def _polygon(value):
    if not isinstance(value, list) or not 3 <= len(value) <= 100:
        raise ValueError("ROI 须包含 3–100 个像素顶点")
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("ROI 顶点须为 [x, y]")
        _number(point[0], "ROI x", 0, 1023)
        _number(point[1], "ROI y", 0, 767)
    area = (
        abs(
            sum(
                point[0] * value[(index + 1) % len(value)][1]
                - value[(index + 1) % len(value)][0] * point[1]
                for index, point in enumerate(value)
            )
        )
        / 2
    )
    if area < 1:
        raise ValueError("ROI 面积过小")
    return value


def _configure_cpu():
    for key, value in {
        "MUJOCO_GL": "egl",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }.items():
        os.environ[key] = value


def validate_session_config(body) -> dict:
    """Validate the SDK/HTTP creation boundary, without simulator creation."""
    _configure_cpu()
    from .casting_profiles import BASE_PROFILE, available_profiles
    from .workstation_defects import WORKSTATION_CASES
    from .workstation_jobs import JOB_FIELDS, job_profile
    from .workstation_model import CAMERA_SETUPS, GRASP_SETUPS

    if not isinstance(body, dict):
        raise ValueError("请求必须是 JSON 对象")
    defaults = {
        "workpiece_id": "097",
        "seed": 7007,
        "case": "mixed",
        "mode": "removal",
        "views": "multi",
        "tool": "belt",
        "casting_profile": BASE_PROFILE,
        "workpiece_profile": None,
        "grasp_setup": "original",
        "camera_setup": "original",
        "detector": "geometric",
        "checkpoint": None,
        "roi": None,
        "max_passes": 3,
        "realtime_factor": 0.0,
        "removal_rate_mm_s": None,
        "targeted_rescan": False,
    }
    if set(body) - set(defaults):
        raise ValueError(
            "未知会话参数：" + ", ".join(sorted(set(body) - set(defaults)))
        )
    configuration = {**defaults, **body}
    profile = job_profile(configuration["workpiece_profile"])
    if profile:
        configuration.update({key: profile[key] for key in JOB_FIELDS})
    choices = {
        "workpiece_id": ("097", "021"),
        "case": WORKSTATION_CASES,
        "mode": ("removal", "clearance"),
        "views": ("single", "multi"),
        "tool": ("belt", "wheel"),
        "grasp_setup": GRASP_SETUPS,
        "camera_setup": CAMERA_SETUPS,
        "casting_profile": available_profiles(),
        "detector": ("geometric", "learned", "hybrid"),
    }
    for key, permitted in choices.items():
        if (
            not isinstance(configuration[key], str)
            or configuration[key] not in permitted
        ):
            raise ValueError(f"不支持的 {key}")
    configuration["seed"] = _integer(configuration["seed"], "seed", 0, 1000000)
    configuration["max_passes"] = _integer(
        configuration["max_passes"], "max_passes", 1, 10
    )
    configuration["realtime_factor"] = _number(
        configuration["realtime_factor"], "realtime_factor", 0, 100
    )
    if type(configuration["targeted_rescan"]) is not bool:
        raise ValueError("targeted_rescan 须为布尔值")
    if configuration["removal_rate_mm_s"] is not None:
        configuration["removal_rate_mm_s"] = _number(
            configuration["removal_rate_mm_s"], "removal_rate_mm_s", 0.0001, 100
        )
    if configuration["roi"] is not None:
        _polygon(configuration["roi"])
    checkpoint = configuration["checkpoint"]
    if checkpoint is not None and not isinstance(checkpoint, (str, Path)):
        raise ValueError("checkpoint 须为本地权重路径")
    if configuration["detector"] != "geometric":
        from .learned_defects import registered_checkpoint

        checkpoint = checkpoint or registered_checkpoint()
        if not checkpoint or not Path(checkpoint).is_file():
            raise ValueError("专用识别模型尚未训练并验证")
    configuration["checkpoint"] = (
        str(Path(checkpoint).resolve()) if checkpoint else None
    )
    return configuration


class WorkstationSession:
    """Own one durable simulation checkpoint and its intervention history.

    ``backend_factory(directory, configuration, checkpoint_path)`` is the
    simulation boundary for offline/mock integrations. It is not exposed over
    HTTP. A caller already holding the project flock passes
    ``mujoco_lock_held=True`` to avoid reacquiring the same non-reentrant lock.
    """

    def __init__(
        self,
        directory,
        state,
        *,
        process_adapter=None,
        reinspection_calibrator=None,
        backend_factory=None,
        mujoco_lock_held=False,
    ):
        self.directory = Path(directory).resolve()
        self._state = state
        self.process_adapter = process_adapter
        self.reinspection_calibrator = reinspection_calibrator
        self._backend_factory = backend_factory
        self._mujoco_lock_held = mujoco_lock_held
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._continuous_lock = threading.Lock()
        self._continuous_owner = None
        self._pause_requested = threading.Event()
        self._abort_requested = threading.Event()

    @property
    def session_id(self) -> str:
        return self._state["session_id"]

    @property
    def stage(self) -> str:
        return self._state["stage"]

    def status(self) -> dict:
        """Return a detached, JSON-compatible checkpoint view."""
        with self._state_lock:
            return copy.deepcopy(self._state)

    def checkpoint(self) -> dict:
        """Read the last atomic material/phase checkpoint, not live feedback."""
        return json.loads(
            (self.directory / "session_state.json").read_text(encoding="utf-8")
        )

    def segments(self) -> list[dict]:
        """Return ordered segments, including completed and excluded history."""
        return self.status()["segments"]

    def interventions(self) -> list[dict]:
        """Read the structured, append-only intervention log."""
        with self._state_lock:
            path = self.directory / "interventions.jsonl"
            return [
                json.loads(line)
                for line in path.read_text().splitlines()
                if line.strip()
            ]

    def _save(self, backend=None):
        with self._state_lock:
            revision = self._state["checkpoint_index"] + 1
            if backend is not None:
                folder = self.directory / "checkpoints"
                folder.mkdir(exist_ok=True)
                path = folder / f"material_{revision:06d}.npz"
                backend.save_checkpoint(path)
                self._state["material_checkpoint"] = str(
                    path.relative_to(self.directory)
                )
                self._state["stock"] = _json_data(backend.stock_summary())
            self._state["checkpoint_index"] = revision
            self._state["executed_segments"] = [
                segment["segment_id"]
                for segment in self._state["segments"]
                if segment["status"] == "executed"
            ]
            self._state["remaining_segments"] = [
                segment["segment_id"]
                for segment in self._state["segments"]
                if segment["status"]
                in {"pending", "blocked", "executing", "partial"}
            ]
            self._state["updated_at"] = _timestamp()
            _truth_free(self._state)
            _write_json(self.directory / "session_state.json", self._state)

    def _transition(self, stage, *, reason=None, backend=None):
        with self._state_lock:
            previous = self.stage
            self._state["stage"] = stage
            if reason is not None:
                self._state["stop_reason"] = reason
            if previous != stage:
                self._state["transitions"].append(
                    {
                        "timestamp": _timestamp(),
                        "from": previous,
                        "to": stage,
                        "reason": reason,
                    }
                )
            self._save(backend)

    def _log(self, action, target_id, before, after, reason=""):
        with self._state_lock:
            entry = {
                "timestamp": _timestamp(),
                "stage": self.stage,
                "action": action,
                "target_id": target_id,
                "before": _json_data(before),
                "after": _json_data(after),
                "diff": {
                    key: {"before": before.get(key), "after": after.get(key)}
                    for key in before.keys() | after.keys()
                    if before.get(key) != after.get(key)
                },
                "reason": reason,
            }
            with (self.directory / "interventions.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    json.dumps(entry, ensure_ascii=False, allow_nan=False)
                    + "\n"
                )
            self._state["intervention_count"] += 1

    @contextmanager
    def _operation(self, action):
        if (
            self._continuous_owner is not None
            and self._continuous_owner != threading.get_ident()
        ):
            raise SessionStateError("该会话正在连续推进；可请求暂停或中止")
        if not self._operation_lock.acquire(blocking=False):
            raise SessionStateError("当前阶段正在运行；可请求暂停退刀或中止")
        try:
            if self.stage in TERMINAL_STAGES:
                raise SessionStateError(f"{self.stage} 会话不能继续修改或执行")
            self._state["operation"] = action
            self._save()
            yield
        finally:
            try:
                self._state["operation"] = None
                self._save()
            finally:
                self._operation_lock.release()

    @contextmanager
    def _runtime(self, grasp_setup=None):
        stream = None
        if self._backend_factory is None:
            for key, value in {
                "MUJOCO_GL": "egl",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
            }.items():
                os.environ[key] = value
            if not self._mujoco_lock_held:
                MUJOCO_LOCK.parent.mkdir(parents=True, exist_ok=True)
                stream = MUJOCO_LOCK.open("a+")
                fcntl.flock(stream, fcntl.LOCK_EX)
        backend = None
        try:
            checkpoint = self._state["material_checkpoint"]
            configuration = {
                **self._state["config"],
                "grasp_setup": grasp_setup
                or self._state["config"]["grasp_setup"],
            }
            factory = self._backend_factory or _MujocoBackend
            backend = factory(
                self.directory,
                configuration,
                self.directory / checkpoint if checkpoint else None,
            )
            yield backend
        finally:
            try:
                if backend is not None:
                    self._save(backend)
                    backend.close()
            finally:
                if stream is not None:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                    stream.close()

    def _publish(self, value):
        with self._state_lock:
            self._state["live"] = _json_data(value)

    def _require_plugins(self):
        for name in ("process_adapter", "reinspection_calibrator"):
            if self._state["plugins"][name] and getattr(self, name) is None:
                raise SessionStateError(
                    f"该检查点使用 {name}，续跑时须重新注入同一插件"
                )

    def _observed_context(self):
        """Construct a whitelist, never redact a complete simulator/state object."""
        region_fields = {
            "id",
            "kind",
            "length_mm",
            "width_mm",
            "height_mm",
            "normal_height_mm",
            "confidence",
            "machining_required",
            "approval",
            "masked",
            "manual",
            "best_view",
            "view_indices",
            "normal_height_quantile",
            "target_remaining_height_mm",
        }
        return {
            "session_id": self.session_id,
            "workpiece_id": self._state["workpiece_id"],
            "stage": self.stage,
            "config": copy.deepcopy(self._state["config"]),
            "regions": [
                {
                    key: copy.deepcopy(region[key])
                    for key in region_fields
                    if key in region
                }
                for region in self._state["regions"]
            ],
            "registered_regions": [
                {
                    key: copy.deepcopy(target[key])
                    for key in ("id", "kind", "points", "normals")
                }
                for target in self._state["registered_regions"]
            ],
            "segments": [
                _observed_segment(segment)
                for segment in self._state["segments"]
            ],
            "observation_snapshots": [
                str(self.directory / reference)
                for reference in self._state["observation_snapshots"]
            ],
            "prediction_revision": self._state["prediction_revision"],
            "reinspection": _observed_report(self._state["reinspection"]),
            "execution_feedback": [
                {
                    key: copy.deepcopy(execution[key])
                    for key in (
                        "segment_id",
                        "unexpected_contact_count",
                        "tracking_trace_p95_mm",
                        "surface_tracking_p95_mm",
                        "simulated_seconds",
                        "simulation_steps",
                        "engaged_duration_s",
                        "contact_duration_s",
                        "removal_contact_s",
                        "duration_s",
                        "commanded_force_n",
                        "force_n",
                        "interrupted",
                        "last_phase",
                        "max_contact_force_n",
                        "tool_contact_steps",
                    )
                    if key in execution
                }
                for execution in self._state["execution_history"]
            ],
            "execution_history": [
                {
                    key: copy.deepcopy(execution[key])
                    for key in (
                        "segment_id",
                        "unexpected_contact_count",
                        "tracking_trace_p95_mm",
                        "surface_tracking_p95_mm",
                        "simulated_seconds",
                        "simulation_steps",
                        "engaged_duration_s",
                        "contact_duration_s",
                        "removal_contact_s",
                        "duration_s",
                        "commanded_force_n",
                        "force_n",
                        "interrupted",
                        "last_phase",
                        "max_contact_force_n",
                        "tool_contact_steps",
                    )
                    if key in execution
                }
                for execution in self._state["execution_history"]
            ],
            **(
                {
                    "local_baseline": _observed_baseline(
                        self._state["local_baseline"]
                    )
                }
                if self._state.get("local_baseline") is not None
                else {}
            ),
            "process_state": copy.deepcopy(self._state["process_state"]),
            "calibration": copy.deepcopy(self._state["calibration"]),
            "truth_used_for_control": False,
            "truth_used_for_decision": False,
        }

    def _apply_process_adapter(self, segment):
        if self.process_adapter is None:
            return
        proposal = self.process_adapter(
            _observed_segment(segment), self._observed_context()
        )
        if proposal is None:
            return
        if not isinstance(proposal, dict) or set(proposal) - set(
            PARAMETER_DEFAULTS
        ) - {
            "process_state",
            "reason",
            "truth_used_for_control",
            "truth_used_for_decision",
        }:
            raise ValueError(
                "process_adapter 须返回段参数、process_state 或 None"
            )
        _truth_free(proposal)
        evidence = getattr(self.process_adapter, "last_evidence", None)
        process_state = proposal.get(
            "process_state", self._state["process_state"]
        )
        if "process_state" not in proposal and callable(evidence):
            try:
                process_state = evidence()
            except (KeyError, ValueError) as error:
                self._state["process_evidence_error"] = str(error)
                self._log(
                    "process_evidence_unavailable",
                    segment["segment_id"],
                    {},
                    {"error": str(error)},
                    "保留已有插件状态；本次参数仍须通过安全预检",
                )
        if not isinstance(process_state, dict):
            raise ValueError("process_state 须为 JSON 对象")
        _truth_free(process_state)
        updates = {
            key: value
            for key, value in proposal.items()
            if key in PARAMETER_DEFAULTS
            and key not in segment["manual_param_keys"]
        }
        skipped = type(updates.get("passes")) is int and updates["passes"] == 0
        if skipped:
            del updates["passes"]
        if updates:
            self._update_params(
                segment,
                updates,
                reason=proposal.get(
                    "reason", "process_adapter 基于观测的参数建议"
                ),
            )
        if skipped:
            segment.update(
                status="skipped", skip_reason="process_adapter_observed_skip"
            )
            self._log(
                "process_skip",
                segment["segment_id"],
                {"status": "pending"},
                {"status": "skipped", "requested_passes": 0},
                "工艺插件根据观测建议零道次",
            )
        self._state["process_state"] = _json_data(process_state)
        self._save()

    def _apply_calibration(self):
        if self.reinspection_calibrator is None:
            return
        calibration = self.reinspection_calibrator(
            _observed_report(self._state["reinspection"]),
            self._observed_context(),
        )
        if not isinstance(calibration, dict):
            raise ValueError("reinspection_calibrator 须返回 JSON 补充报告")
        if (
            "bias_veto" in calibration
            and type(calibration["bias_veto"]) is not bool
        ):
            raise ValueError("calibration.bias_veto 须为布尔值")
        if "status" in calibration and (
            not isinstance(calibration["status"], str)
            or not calibration["status"]
            or len(calibration["status"]) > 80
        ):
            raise ValueError("calibration.status 须为非空状态文本")
        _truth_free(calibration)
        self._state["calibration"] = _json_data(calibration)
        reason = self._calibration_stop_reason()
        if reason:
            self._state["observational_release"] = False
            self._state["stop_reason"] = reason

    def _calibration_stop_reason(self):
        calibration = self._state["calibration"] or {}
        if calibration.get("bias_veto"):
            return "reinspection_calibration_bias_veto"
        if "status" in calibration and calibration["status"] != "pass":
            return "reinspection_calibration_" + calibration["status"]
        return None

    def _calibration_blocks_followup(self):
        calibration = self._state["calibration"] or {}
        return bool(calibration.get("bias_veto")) or (
            "status" in calibration
            and calibration["status"] not in {"pass", "positive_excess"}
        )

    def _pending(self):
        return [
            segment
            for segment in self._state["segments"]
            if segment["status"] in {"pending", "blocked"}
        ]

    def _region(self, identity):
        _integer(identity, "region_id", 1, 1000000)
        region = next(
            (r for r in self._state["regions"] if r["id"] == identity), None
        )
        if region is None:
            raise ValueError("找不到该登记区域")
        return region

    def _scan(self, backend, *, angles_deg=None, extra_frames=0):
        number = len(self._state["scans"]) + 1
        folder = self.directory / "scans" / f"scan_{number:04d}"
        result = _json_data(
            backend.scan(
                folder,
                angles_deg=angles_deg,
                extra_frames=extra_frames,
                revision=self._state["prediction_revision"] + 1,
                targets=self._state["registered_regions"] or None,
                permitted_paths=self._state["permitted_paths"],
                whole_workpiece=self._scope_complete(),
                publish=self._publish,
            )
        )
        _truth_free(result)
        result["snapshot_id"] = number
        result["observations"] = [
            str((folder / reference).relative_to(self.directory))
            for reference in result["observations"]
        ]
        path = folder / "scan.json"
        _write_json(path, result)
        self._state["scan_snapshot"] = str(path.relative_to(self.directory))
        self._state["scans"].append(self._state["scan_snapshot"])
        self._state["observation_snapshots"] = result["observations"]
        self._state["prediction_revision"] = result["perception"][
            "prediction_revision"
        ]
        self._state["perception"] = result["perception"]
        if self._state["registered_regions"]:
            self._state["reinspection"] = result["perception"].get(
                "region_reinspection", {}
            )
            self._state["local_baseline"] = _observed_baseline(
                result.get(
                    "local_baseline",
                    self._state["reinspection"].get("local_baseline"),
                )
            )
            self._apply_calibration()
        return result

    def _scope_complete(self):
        return (
            self._state["config"]["roi"] is None
            and all(
                region["approval"] == "approved"
                or not region.get("machining_required")
                for region in self._state["regions"]
            )
            and not any(
                s["status"] == "skipped" for s in self._state["segments"]
            )
        )

    def _snapshot(self):
        return json.loads(
            (self.directory / self._state["scan_snapshot"]).read_text(
                encoding="utf-8"
            )
        )

    def _accept_initial_scan(self):
        snapshot = self._snapshot()
        self._state["regions"] = [
            {**region, "approval": "pending", "masked": False, "manual": False}
            for region in snapshot["detections"]
        ]
        self._state["registered_regions"] = snapshot["registered_regions"]
        self._state["protected_points"] = snapshot.get("protected_points", [])
        self._state["permitted_paths"] = copy.deepcopy(snapshot["paths"])
        self._add_segments(snapshot["paths"])

    def _add_segments(self, paths):
        for path in paths:
            identity = self._state["next_segment_id"]
            self._state["next_segment_id"] += 1
            self._state["segments"].append(
                {
                    "segment_id": identity,
                    "region_id": path["detection_id"],
                    "path": {**path, "segment_id": identity},
                    "params": dict(PARAMETER_DEFAULTS),
                    "manual_param_keys": [],
                    "grasp_setup": path.get(
                        "grasp_setup", self._state["config"]["grasp_setup"]
                    ),
                    "workpiece_id": path.get(
                        "workpiece_id", self._state["workpiece_id"]
                    ),
                    **(
                        {"split_from": path["split_from"]}
                        if "split_from" in path
                        else {}
                    ),
                    "status": "pending",
                    "needs_preflight": True,
                    "preflight": None,
                    "commands_ref": None,
                    "completed_passes": 0,
                    "resume_from_point": 0,
                    "round_index": self._state["round_index"],
                }
            )

    def approve_regions(self, region_ids) -> dict:
        """Approve observed machining regions; review/normal structures stay held."""
        if (
            not isinstance(region_ids, list)
            or not region_ids
            or len(region_ids) > 1000
        ):
            raise ValueError("region_ids 须为非空区域编号列表")
        regions = [self._region(identity) for identity in region_ids]
        if any(
            r["kind"] not in {"seam", "burr"}
            or not r.get("machining_required")
            or r["masked"]
            for r in regions
        ):
            raise ValueError("保护结构、待复核区域或已屏蔽区域不可批准加工")
        with self._operation("approve_regions"):
            before = {str(r["id"]): r["approval"] for r in regions}
            for region in regions:
                region["approval"] = "approved"
            for segment in self._pending():
                segment["needs_preflight"] = True
            self._log(
                "approve_regions",
                region_ids,
                before,
                {str(r["id"]): r["approval"] for r in regions},
            )
        return self.status()

    def add_manual_region(self, roi_spec) -> dict:
        """Register one measurable saved-view ROI; manual selection is not approval.

        ``roi_spec`` has ``polygon`` pixel vertices, optional ``view_index``
        (default 0), optional measured ``kind`` and a text ``reason``. The real
        backend reruns the existing CAD-reference detector and protection rules.
        """
        if not isinstance(roi_spec, dict) or set(roi_spec) - {
            "polygon",
            "view_index",
            "kind",
            "reason",
        }:
            raise ValueError(
                "roi_spec 须包含 polygon 和可选 view_index/kind/reason"
            )
        _polygon(roi_spec.get("polygon"))
        if "kind" in roi_spec and roi_spec["kind"] not in {"seam", "burr"}:
            raise ValueError("人工 ROI 类型仅可为 seam 或 burr")
        reason = roi_spec.get("reason", "人工补充观测区域")
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("reason 须为不超过 2000 字符的文本")
        with self._operation("add_manual_region"):
            if not self._state["regions"] and self.stage != "DETECTED":
                raise SessionStateError("须先完成初始扫描和检测")
            index = _integer(
                roi_spec.get("view_index", 0),
                "view_index",
                0,
                len(self._state["observation_snapshots"]) - 1,
            )
            if not self._state["safe_retracted"]:
                raise SessionStateError("须先完成安全退刀")
            with self._runtime() as backend:
                registration = _json_data(
                    backend.manual_region(
                        roi_spec,
                        self.directory
                        / self._state["observation_snapshots"][index],
                    )
                )
                _truth_free(registration)
                identity = (
                    max(
                        (region["id"] for region in self._state["regions"]),
                        default=0,
                    )
                    + 1
                )
                region = {
                    **registration["region"],
                    "id": identity,
                    "approval": "pending",
                    "manual": True,
                    "masked": False,
                    "roi_spec": roi_spec,
                    "source_observation": self._state["observation_snapshots"][
                        index
                    ],
                }
                self._state["regions"].append(region)
                self._state["registered_regions"].append(
                    {**registration["target"], "id": identity}
                )
                paths = [
                    {**path, "detection_id": identity}
                    for path in registration["paths"]
                ]
                self._add_segments(paths)
                self._state["permitted_paths"].extend(
                    copy.deepcopy(s["path"])
                    for s in self._state["segments"]
                    if s["region_id"] == identity
                )
                for segment in self._pending():
                    segment["needs_preflight"] = True
                self._log(
                    "add_manual_region",
                    identity,
                    {},
                    {"roi_spec": roi_spec, "approval": "pending"},
                    reason,
                )
        return self.status()

    def rescan_views(self, angles_deg=None, extra_frames=None) -> dict:
        """Acquire new fixed-camera evidence without discarding completed work."""
        if angles_deg is not None:
            if (
                not isinstance(angles_deg, list)
                or not 1 <= len(angles_deg) <= 24
            ):
                raise ValueError("angles_deg 须为 1–24 个角度的列表")
            for angle in angles_deg:
                _number(angle, "angle_deg", 0, 360)
        frames = (
            0
            if extra_frames is None
            else _integer(extra_frames, "extra_frames", 0, 8)
        )
        self._require_plugins()
        with self._operation("rescan_views"):
            if self.stage == "IDLE" or not self._state["safe_retracted"]:
                raise SessionStateError("补拍前须有初始观测并完成安全退刀")
            previous = self._state["scan_snapshot"]
            with self._runtime() as backend:
                result = self._scan(
                    backend, angles_deg=angles_deg, extra_frames=frames
                )
                self._state["protected_points"] = result.get(
                    "protected_points", []
                )
                for segment in self._pending():
                    segment["needs_preflight"] = True
                    segment["commands_ref"] = None
                self._log(
                    "rescan_views",
                    None,
                    {"snapshot": previous},
                    {
                        "snapshot": self._state["scan_snapshot"],
                        "angles_deg": angles_deg,
                        "extra_frames": frames,
                    },
                )
        return self.status()

    def _populate_followup(self):
        import numpy as np
        from scipy.spatial import cKDTree

        quality = self._state["reinspection"]
        if (
            self._state["round_index"] + 1
            >= self._state["config"]["max_passes"]
        ):
            raise SessionStateError("复检加工轮数已达到本会话预算")
        if (
            self._calibration_blocks_followup()
            or quality.get("overgrind")
            or quality.get("negative_limit_risk")
            or quality.get("invalid_observations")
            or not quality.get("automatic_regrind_allowed", True)
        ):
            raise SessionSafetyError(
                "复检偏置、负向风险或无效观测未处置，拒绝回磨"
            )
        snapshot = self._snapshot()
        mapping = {}
        detections = {region["id"]: region for region in snapshot["detections"]}
        for target in snapshot["registered_regions"]:
            points = np.asarray(target["points"])
            existing = next(
                (
                    registered
                    for registered in self._state["registered_regions"]
                    if registered["kind"] == target["kind"]
                    and len(registered["points"])
                    and len(points)
                    and np.mean(
                        cKDTree(registered["points"]).query(points)[0] <= 0.002
                    )
                    >= 0.75
                ),
                None,
            )
            if existing is None:
                identity = (
                    max((r["id"] for r in self._state["regions"]), default=0)
                    + 1
                )
                region = detections.get(
                    target["id"],
                    {
                        "id": identity,
                        "kind": target["kind"],
                        "machining_required": target["kind"]
                        in {"seam", "burr"},
                    },
                )
                self._state["regions"].append(
                    {
                        **region,
                        "id": identity,
                        "approval": "pending",
                        "masked": False,
                        "manual": False,
                    }
                )
                self._state["registered_regions"].append(
                    {**target, "id": identity}
                )
                mapping[target["id"]] = identity
            else:
                mapping[target["id"]] = existing["id"]
        paths = [
            {
                **path,
                "detection_id": path["detection_id"]
                if "regrind" in path
                else mapping.get(path["detection_id"], path["detection_id"]),
            }
            for path in snapshot["paths"]
        ]
        excluded = [
            segment
            for segment in self._state["segments"]
            if segment["status"] == "skipped"
        ]
        permitted = [
            path
            for path in paths
            if self._region(path["detection_id"])["approval"] != "masked"
            and not any(
                path["detection_id"] == segment["region_id"]
                and np.dot(path["normal"], segment["path"]["normal"]) >= 0.9
                and np.any(
                    cKDTree(segment["path"]["nominal_xyz"]).query(
                        path["nominal_xyz"]
                    )[0]
                    <= 0.002
                )
                for segment in excluded
            )
        ]
        self._state["round_index"] += 1
        self._state["protected_points"] = snapshot.get("protected_points", [])
        self._state["permitted_paths"].extend(
            copy.deepcopy(path) for path in permitted if "regrind" not in path
        )
        self._add_segments(permitted)

    def mask_false_positive(self, region_id, reason="") -> dict:
        """Exclude a region while retaining its measured anchors and audit trail."""
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("reason 须为不超过 2000 字符的文本")
        with self._operation("mask_false_positive"):
            region = self._region(region_id)
            before = {
                "approval": region["approval"],
                "masked": region["masked"],
            }
            region.update(approval="masked", masked=True, mask_reason=reason)
            for segment in self._pending():
                segment["needs_preflight"] = True
                if segment["region_id"] == region_id:
                    segment.update(
                        status="skipped",
                        skip_reason=reason or "masked_false_positive",
                    )
            self._log(
                "mask_false_positive",
                region_id,
                before,
                {"approval": "masked", "masked": True},
                reason,
            )
        return self.status()

    def skip_segment(self, segment_id, reason="") -> dict:
        """Skip unexecuted work without removing it from the checkpoint history."""
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("reason 须为不超过 2000 字符的文本")
        with self._operation("skip_segment"):
            segment = self._segment(segment_id)
            before = {"status": segment["status"]}
            segment.update(status="skipped", skip_reason=reason)
            self._log(
                "skip_segment",
                segment_id,
                before,
                {"status": "skipped"},
                reason,
            )
        return self.status()

    def split_segment(self, segment_id, ratio=0.5) -> dict:
        """Split a remaining nominal trace by arc length, retaining its endpoint."""
        _number(ratio, "ratio", 0.001, 0.999)
        with self._operation("split_segment"):
            segment = self._segment(segment_id)
            if segment["completed_passes"]:
                raise SessionStateError(
                    "已有完整加工遍数的段不能切分，请完成剩余遍数"
                )
            path = copy.deepcopy(segment["path"])
            for key in ("xyz", "nominal_xyz"):
                path[key] = path[key][segment["resume_from_point"] :]
            lengths = [
                math.dist(first, second)
                for first, second in zip(
                    path["nominal_xyz"], path["nominal_xyz"][1:]
                )
            ]
            total = sum(lengths)
            if total <= 0:
                raise ValueError("零长度路径不能切分")
            offset, index = total * ratio, 0
            while index < len(lengths) - 1 and offset > lengths[index]:
                offset -= lengths[index]
                index += 1
            amount = offset / lengths[index]
            halves = [copy.deepcopy(path), copy.deepcopy(path)]
            for key in ("xyz", "nominal_xyz"):
                point = [
                    a * (1 - amount) + b * amount
                    for a, b in zip(path[key][index], path[key][index + 1])
                ]
                halves[0][key] = path[key][: index + 1] + [point]
                halves[1][key] = [point] + path[key][index + 1 :]
            segment["status"] = "superseded"
            start = len(self._state["segments"])
            self._add_segments(
                [
                    {
                        **half,
                        "grasp_setup": segment["grasp_setup"],
                        "workpiece_id": segment["workpiece_id"],
                        "split_from": segment_id,
                    }
                    for half in halves
                ]
            )
            children = self._state["segments"][start:]
            for index, child in enumerate(children):
                child["params"] = dict(segment["params"])
                child["split_ratio"] = ratio
                child["split_part"] = index + 1
            # Children replace their parent in execution order, not just in history.
            insertion = self._state["segments"].index(segment) + 1
            del self._state["segments"][start:]
            self._state["segments"][insertion:insertion] = children
            for pending in self._pending():
                pending["needs_preflight"] = True
            self._log(
                "split_segment",
                segment_id,
                {"status": "pending"},
                {
                    "status": "superseded",
                    "children": [c["segment_id"] for c in children],
                    "ratio": ratio,
                },
            )
        return self.status()

    def switch_grasp(self, grasp_setup, segment_id=None) -> dict:
        """Select a simulated fixture/grasp at the checked safe loading boundary."""
        from .workstation_model import GRASP_SETUPS

        if not isinstance(grasp_setup, str) or grasp_setup not in GRASP_SETUPS:
            raise ValueError("不支持的夹持方案")
        with self._operation("switch_grasp"):
            if not self._state["safe_retracted"]:
                raise SessionStateError("切夹持前必须完成安全退刀和放回装夹区")
            segments = (
                [self._segment(segment_id)]
                if segment_id is not None
                else self._pending()
            )
            before = {str(s["segment_id"]): s["grasp_setup"] for s in segments}
            for segment in segments:
                segment["grasp_setup"] = grasp_setup
                segment["path"]["grasp_setup"] = grasp_setup
            if segment_id is None:
                self._state["config"]["grasp_setup"] = grasp_setup
            for segment in self._pending():
                segment["needs_preflight"] = True
                segment["commands_ref"] = None
            self._log(
                "switch_grasp",
                segment_id,
                before,
                {str(s["segment_id"]): s["grasp_setup"] for s in segments},
                "装夹安全位中的仿真配置切换；实机换夹持未验证",
            )
        return self.status()

    def replan_remaining(self) -> dict:
        """Rebuild only unexecuted work and enforce the current continuous gate."""
        with self._operation("replan_remaining"):
            if self.stage in {"IDLE", "SCANNED"}:
                raise SessionStateError("须先完成区域检测与审批")
            if self._state["stop_kind"] in {"motion", "retract"}:
                raise SessionSafetyError(
                    "实际运动或退刀失败未处置，不能通过重规划绕过"
                )
            paused = self.stage in {"PAUSED", "STOPPED_SAFETY"}
            previous = self.stage
            if previous == "RESCANNED":
                self._populate_followup()
            for segment in self._pending():
                segment["needs_preflight"] = True
            self._plan_remaining()
            self._log(
                "replan_remaining",
                None,
                {"stage": previous},
                {
                    "pending_segment_ids": [
                        s["segment_id"] for s in self._pending()
                    ]
                },
            )
            if paused:
                self._state["resume_stage"] = "PLANNED"
                self._transition(previous)
        return self.status()

    def _segment(self, identity):
        _integer(identity, "segment_id", 1, 1000000)
        segment = next(
            (s for s in self._state["segments"] if s["segment_id"] == identity),
            None,
        )
        if segment is None:
            raise ValueError("找不到该路径段")
        if segment["status"] not in {"pending", "blocked"}:
            raise SessionStateError("只能修改尚未执行、未跳过的路径段")
        return segment

    def _update_params(self, segment, updates, *, reason="", manual=False):
        for key, value in updates.items():
            if key == "passes":
                _integer(
                    value, key, max(1, segment["completed_passes"] + 1), 10
                )
            elif key == "normal_offset_mm":
                _number(value, key, -10, 10)
            else:
                _number(
                    value,
                    key,
                    0.1 if key == "feed_scale" else 0.25,
                    3 if key == "feed_scale" else 4,
                )
        before = dict(segment["params"])
        segment["params"].update(updates)
        if manual:
            segment["manual_param_keys"] = sorted(
                set(segment["manual_param_keys"]) | set(updates)
            )
        segment["needs_preflight"] = True
        segment["commands_ref"] = None
        segment["preflight"] = None
        segment["status"] = "pending"
        self._log(
            "set_segment_params",
            segment["segment_id"],
            before,
            dict(segment["params"]),
            reason,
        )

    def set_segment_params(
        self,
        segment_id,
        feed_scale=None,
        normal_offset_mm=None,
        passes=None,
        dwell_scale=None,
    ) -> dict:
        """Edit a pending segment; admission must rerun before any motion.

        Positive normal offset increases the stock left above the nominal CAD.
        The schema accepts inward proposals so the mandatory preflight can reject
        them with a persisted stop reason instead of silently clipping the edit.
        """
        updates = {
            key: value
            for key, value in {
                "feed_scale": feed_scale,
                "normal_offset_mm": normal_offset_mm,
                "passes": passes,
                "dwell_scale": dwell_scale,
            }.items()
            if value is not None
        }
        if not updates:
            raise ValueError("请提供至少一个段参数")
        with self._operation("set_segment_params"):
            self._update_params(self._segment(segment_id), updates, manual=True)
        return self.status()

    def _request_stop(self, action, *, abort=False):
        event = self._abort_requested if abort else self._pause_requested
        if self.stage in TERMINAL_STAGES:
            if self.stage == "ABORTED" and abort:
                return self.status()
            raise SessionStateError("已结束会话不能暂停")
        event.set()
        acquired = self._operation_lock.acquire(blocking=False)
        try:
            with self._state_lock:
                self._state[
                    "abort_requested" if abort else "pause_requested"
                ] = True
                self._log(
                    action,
                    self._state["active_segment_id"],
                    {
                        "stage": self.stage,
                        "safe_retracted": self._state["safe_retracted"],
                    },
                    {"requested": True},
                    "程序化中止" if abort else "程序化暂停退刀",
                )
                self._save()
            if acquired:
                self._state["resume_stage"] = (
                    self._state["resume_stage"] or self.stage
                )
                if not self._state["safe_retracted"]:
                    with self._runtime() as backend:
                        self._complete_stop(backend)
                else:
                    self._state["pause_requested"] = False
                    self._state["abort_requested"] = False
                    self._transition(
                        "ABORTED" if abort else "PAUSED",
                        reason="operator_abort" if abort else "operator_pause",
                    )
        finally:
            if acquired:
                self._operation_lock.release()
        return self.status()

    def pause(self) -> dict:
        """Request checked retraction; a running step stops at a feedback boundary."""
        return self._request_stop("pause")

    def safe_retract(self) -> dict:
        """Pause only after the actual carried workpiece has safely returned."""
        return self._request_stop("safe_retract")

    def abort(self) -> dict:
        """Stop and retract without allowing any subsequent continuation."""
        return self._request_stop("abort", abort=True)

    def _complete_stop(self, backend, segment=None):
        retracted = _json_data(backend.safe_retract(publish=self._publish))
        self._state["last_safe_retract"] = retracted
        self._state["safe_retracted"] = bool(retracted["passed"])
        if not retracted["passed"]:
            self._stop(
                "安全退刀未通过连续预检或实际接触验收",
                segment=segment,
                backend=backend,
                kind="retract",
            )
        if segment is not None and segment["status"] == "executing":
            segment["status"] = "pending"
            segment["needs_preflight"] = True
        self._state["active_segment_id"] = None
        self._state["pause_requested"] = False
        self._state["abort_requested"] = False
        self._state["resume_stage"] = self._state["resume_stage"] or self.stage
        self._transition(
            "ABORTED" if self._abort_requested.is_set() else "PAUSED",
            reason="operator_abort"
            if self._abort_requested.is_set()
            else "operator_pause",
            backend=backend,
        )

    def resume(self) -> dict:
        """Return to the saved phase; motion always rechecks pending segments."""
        with self._operation("resume"):
            if self.stage not in {"PAUSED", "STOPPED_SAFETY"}:
                raise SessionStateError("仅暂停或预检停止的会话可恢复")
            if self._state["stop_kind"] in {"motion", "retract"}:
                raise SessionSafetyError(
                    "实际运动或退刀失败尚未处置，不能恢复执行"
                )
            if not self._state["safe_retracted"]:
                with self._runtime() as backend:
                    result = _json_data(
                        backend.safe_retract(publish=self._publish)
                    )
                    self._state["last_safe_retract"] = result
                    if not result["passed"]:
                        self._stop(
                            "断点恢复的安全退刀未通过",
                            backend=backend,
                            kind="retract",
                        )
                    self._state["safe_retracted"] = True
            before = {"stage": self.stage}
            target = self._state["resume_stage"] or "PLANNED"
            self._pause_requested.clear()
            self._abort_requested.clear()
            self._state["pause_requested"] = False
            self._state["abort_requested"] = False
            self._state["resume_stage"] = None
            self._state["stop_kind"] = None
            self._state["stop_reason"] = None
            for segment in self._pending():
                segment["needs_preflight"] = True
            self._log("resume", None, before, {"stage": target})
            self._transition(target)
        return self.status()

    def _stop(self, reason, *, segment=None, backend=None, kind="preflight"):
        if segment is not None:
            segment["status"] = "blocked"
            segment["needs_preflight"] = True
        self._state["stop_kind"] = kind
        self._state["resume_stage"] = (
            "EXECUTING" if self.stage == "EXECUTING" else "PLANNED"
        )
        self._transition("STOPPED_SAFETY", reason=reason, backend=backend)
        raise SessionSafetyError(reason)

    def _prepare_segment(self, backend, segment):
        region = self._region(segment["region_id"])
        if region["approval"] != "approved" or region["masked"]:
            segment["preflight"] = {
                "passed": False,
                "reason": "region_approval_required",
            }
            return []
        path = copy.deepcopy(segment["path"])
        from .workstation_model import CELL_CONFIG

        path["recipe"]["feed_mm_s"] *= segment["params"]["feed_scale"]
        path["material_allowance_mm"] += segment["params"]["normal_offset_mm"]
        if (
            not 0
            <= path["material_allowance_mm"]
            <= CELL_CONFIG["planning"]["target_remaining_height_mm"]
        ):
            segment["preflight"] = {
                "passed": False,
                "parameter_gate_passed": False,
                "reason": "法向偏置越过名义 CAD 或既有剩余高度门限，拒绝生成加工命令",
                "requested_material_allowance_mm": path[
                    "material_allowance_mm"
                ],
                "continuous_preflight": {
                    "passed": False,
                    "reason": "没有可准入命令",
                },
            }
            segment["needs_preflight"] = True
            segment["status"] = "blocked"
            return []
        if segment["resume_from_point"]:
            for key in ("xyz", "nominal_xyz"):
                path[key] = path[key][segment["resume_from_point"] :]
        import numpy as np

        from .workstation_inspection import exclude_review_footprint
        from .workstation_tooling import fixed_tool

        protected = list(self._state["protected_points"])
        regions = {region["id"]: region for region in self._state["regions"]}
        for target in self._state["registered_regions"]:
            region = regions[target["id"]]
            if target["kind"] == "review" or region["approval"] != "approved":
                protected.extend(target["points"])
        permitted, blocked = exclude_review_footprint(
            [path],
            np.asarray(protected),
            fixed_tool(self._state["config"]["tool"]).footprint_radius,
        )
        if not permitted:
            segment["preflight"] = {
                "passed": False,
                "protected_region_gate_passed": False,
                "reason": "砂带/磨具包络与保护、未审批或屏蔽区域相交",
                "blocked": blocked,
            }
            segment["needs_preflight"] = True
            segment["status"] = "blocked"
            return []
        commands, admitted, planned = backend.plan(path)
        coverage = _number(
            planned.get("source_coverage_ratio", 1.0),
            "source_coverage_ratio",
            0,
            1,
        )
        subsegments = [
            {
                "segment_id": child.get("segment_id"),
                "split_from": child.get("split_from"),
                "source_interval_mm": child.get("source_interval_mm"),
                "grasp_setup": child.get("grasp_setup", segment["grasp_setup"]),
            }
            for child in admitted
        ]
        if any(
            child["grasp_setup"] != segment["grasp_setup"]
            for child in subsegments
        ):
            segment["preflight"] = {
                **_json_data(planned),
                "passed": False,
                "admitted_subsegments": subsegments,
                "reason": "多夹持命令须保留 operations/regrasp_checkpoints，不能摊平为单段执行",
            }
            segment["needs_preflight"] = True
            segment["status"] = "blocked"
            return []
        if commands and segment["params"]["dwell_scale"] != 1:
            clock, previous = 0.0, 0.0
            adjusted = []
            for command in commands:
                duration = command["t"] - previous
                previous = command["t"]
                clock += duration * (
                    segment["params"]["dwell_scale"]
                    if command.get("phase") in {"trace", "approach"}
                    else 1
                )
                adjusted.append({**command, "t": clock})
            commands = adjusted
        checked = (
            backend.preflight(commands)
            if commands and admitted and planned["passed"]
            else {"passed": False}
        )
        check = _json_data(
            {
                **planned,
                "continuous_preflight": checked,
                "source_coverage_ratio": coverage,
                "admitted_subsegments": subsegments,
                "all_paths_passed": bool(
                    planned.get("all_paths_passed", coverage >= 1 - 1e-8)
                ),
                "parameter_gate_passed": True,
                "protected_region_gate_passed": True,
                "passed": bool(
                    commands
                    and admitted
                    and planned["passed"]
                    and checked["passed"]
                ),
            }
        )
        segment["preflight"] = check
        segment["needs_preflight"] = not check["passed"]
        segment["status"] = "pending" if check["passed"] else "blocked"
        if not check["passed"]:
            return []
        folder = self.directory / "plans"
        folder.mkdir(exist_ok=True)
        path = (
            folder
            / f"segment_{segment['segment_id']:06d}_{self._state['checkpoint_index']:06d}.json"
        )
        _write_json(path, _json_data(commands))
        segment["commands_ref"] = str(path.relative_to(self.directory))
        return commands

    def _plan_remaining(self):
        groups = list(dict.fromkeys(s["grasp_setup"] for s in self._pending()))
        for grasp_setup in groups:
            with self._runtime(grasp_setup) as backend:
                for segment in self._pending():
                    if segment["grasp_setup"] == grasp_setup:
                        self._prepare_segment(backend, segment)
        self._transition("PLANNED")

    def _motion_passed(self, execution, *, interrupted=False):
        from .workstation_model import CELL_CONFIG

        limit = CELL_CONFIG["planning"]["tracking_p95_limit_mm"]
        contacts = execution.get("unexpected_contact_count")
        return (
            type(contacts) is int
            and contacts == 0
            and all(
                (interrupted and execution.get(key) is None)
                or (
                    type(execution.get(key)) in (int, float)
                    and math.isfinite(execution[key])
                    and execution[key] < limit
                )
                for key in ("tracking_trace_p95_mm", "surface_tracking_p95_mm")
            )
        )

    def _clear_stop_requests(self):
        self._pause_requested.clear()
        self._abort_requested.clear()
        self._state["pause_requested"] = False
        self._state["abort_requested"] = False

    def _execute_next(self):
        pending = self._pending()
        if not pending:
            with self._runtime() as backend:
                self._scan(backend)
                self._transition("RESCANNED", backend=backend)
            return
        segment = pending[0]
        if segment["completed_passes"] == 0:
            self._apply_process_adapter(segment)
        if segment["status"] == "skipped":
            self._transition("EXECUTING")
            return
        with self._runtime(segment["grasp_setup"]) as backend:
            if self._pause_requested.is_set() or self._abort_requested.is_set():
                self._complete_stop(backend, segment)
                return
            commands = self._prepare_segment(backend, segment)
            if not commands:
                self._stop(
                    segment["preflight"].get(
                        "reason", "该段未通过连续安全预检"
                    ),
                    segment=segment,
                    backend=backend,
                )
            self._state["active_segment_id"] = segment["segment_id"]
            segment["status"] = "executing"
            self._state["safe_retracted"] = False
            self._transition("EXECUTING", backend=backend)
            try:
                execution = _json_data(
                    backend.execute(
                        commands,
                        mode=self._state["config"]["mode"],
                        prefix=f"segment_{segment['segment_id']:06d}_pass_{segment['completed_passes']:02d}_attempt_{self._state['checkpoint_index']:06d}_",
                        publish=self._publish,
                        stop_requested=lambda: (
                            self._pause_requested.is_set()
                            or self._abort_requested.is_set()
                        ),
                    )
                )
            except (ValueError, RuntimeError) as error:
                self._clear_stop_requests()
                retracted = _json_data(
                    backend.safe_retract(publish=self._publish)
                )
                self._state["safe_retracted"] = bool(retracted["passed"])
                self._state["last_safe_retract"] = retracted
                self._state["motion_gate_passed"] = False
                self._state["execution_history"].append(
                    {
                        "segment_id": segment["segment_id"],
                        "error": str(error),
                        "interrupted": True,
                    }
                )
                self._stop(
                    str(error), segment=segment, backend=backend, kind="motion"
                )
            _truth_free(execution)
            self._state["execution_history"].append(
                {
                    "segment_id": segment["segment_id"],
                    "pass_index": segment["completed_passes"],
                    **execution,
                }
            )
            if not self._motion_passed(
                execution, interrupted=bool(execution.get("interrupted"))
            ):
                self._state["motion_gate_passed"] = False
                self._clear_stop_requests()
                retracted = _json_data(
                    backend.safe_retract(publish=self._publish)
                )
                self._state["safe_retracted"] = bool(retracted["passed"])
                self._state["last_safe_retract"] = retracted
                self._stop(
                    "实际运动的接触或跟踪误差验收未通过",
                    segment=segment,
                    backend=backend,
                    kind="motion",
                )
            if execution.get("interrupted"):
                segment["resume_from_point"] = min(
                    len(segment["path"]["nominal_xyz"]) - 2,
                    segment["resume_from_point"]
                    + execution.get("resume_from_point", 0),
                )
                self._complete_stop(backend, segment)
                return
            segment["completed_passes"] += 1
            segment["resume_from_point"] = 0
            segment["status"] = (
                "executed"
                if segment["completed_passes"] >= segment["params"]["passes"]
                else "pending"
            )
            if segment["preflight"]["source_coverage_ratio"] < 1 - 1e-8:
                segment["executed_subsegments"] = segment["preflight"][
                    "admitted_subsegments"
                ]
                segment["uncovered_source_fraction"] = (
                    1 - segment["preflight"]["source_coverage_ratio"]
                )
                intervals = sorted(
                    child["source_interval_mm"]
                    for child in segment["executed_subsegments"]
                    if child["source_interval_mm"] is not None
                )
                gaps, cursor = [], 0.0
                for start, end in intervals:
                    if start > cursor:
                        gaps.append([cursor, start])
                    cursor = max(cursor, end)
                length = (
                    sum(
                        math.dist(a, b)
                        for a, b in zip(
                            segment["path"]["nominal_xyz"],
                            segment["path"]["nominal_xyz"][1:],
                        )
                    )
                    * 1000
                )
                if intervals and cursor < length:
                    gaps.append([cursor, length])
                segment["uncovered_source_intervals_mm"] = (
                    gaps if intervals else None
                )
                if segment["status"] == "executed":
                    segment["status"] = "partial"
            self._state["active_segment_id"] = None
            self._state["motion_gate_passed"] = True
            self._state["safe_retracted"] = True
            for remaining in self._pending():
                remaining["needs_preflight"] = True
            self._save(backend)

    def run_until_pause(self, max_steps=100) -> dict:
        """Advance bounded steps, stopping for review, pause, safety or completion."""
        _integer(max_steps, "max_steps", 1, 1000)
        if not self._continuous_lock.acquire(blocking=False):
            raise SessionStateError("当前会话已有连续推进所有者")
        self._continuous_owner = threading.get_ident()
        try:
            for _ in range(max_steps):
                if self.stage in TERMINAL_STAGES | {"PAUSED", "STOPPED_SAFETY"}:
                    break
                if self.stage in {"DETECTED", "PLANNED", "EXECUTING"} and any(
                    self._region(segment["region_id"])["approval"] != "approved"
                    for segment in self._pending()
                ):
                    self._state["stop_reason"] = "awaiting_region_approval"
                    self._save()
                    break
                self.step_next()
            else:
                if self.stage not in TERMINAL_STAGES | {
                    "PAUSED",
                    "STOPPED_SAFETY",
                }:
                    self.pause()
            return self.status()
        finally:
            self._continuous_owner = None
            self._continuous_lock.release()

    def step_next(self) -> dict:
        """Advance one explicit phase or execute one admitted segment."""
        self._require_plugins()
        with self._operation("step_next"):
            if self.stage in {"PAUSED", "STOPPED_SAFETY"}:
                raise SessionStateError(
                    "会话已暂停或被安全门停止，请先处理停因并恢复"
                )
            if self.stage == "IDLE":
                with self._runtime() as backend:
                    self._scan(backend)
                    self._transition("SCANNED", backend=backend)
            elif self.stage == "SCANNED":
                self._accept_initial_scan()
                self._transition("DETECTED")
            elif self.stage == "DETECTED":
                self._plan_remaining()
            elif self.stage in {"PLANNED", "EXECUTING"}:
                self._execute_next()
            elif self.stage == "RESCANNED":
                from .workstation_run import continuation_reason

                quality = self._state["reinspection"]
                calibration_reason = self._calibration_stop_reason()
                self._state["observational_release"] = bool(
                    quality.get("observational_release")
                    and self._state["motion_gate_passed"]
                    and self._scope_complete()
                    and self._state["config"]["mode"] == "removal"
                    and calibration_reason is None
                )
                snapshot = self._snapshot()
                reason = continuation_reason(
                    self._state["config"]["mode"],
                    self._state["motion_gate_passed"],
                    quality,
                    snapshot["paths"],
                    self._state["round_index"],
                    self._state["config"]["max_passes"],
                    self._state["observational_release"],
                )
                self._state["controller_stopping_reason"] = reason
                if (
                    reason == "followup_planning"
                    and not self._calibration_blocks_followup()
                ):
                    self._populate_followup()
                    self._plan_remaining()
                else:
                    self._transition(
                        "DONE", reason=calibration_reason or reason
                    )
            if (
                self._pause_requested.is_set() or self._abort_requested.is_set()
            ) and self.stage not in TERMINAL_STAGES | {
                "PAUSED",
                "STOPPED_SAFETY",
            }:
                self._state["resume_stage"] = self.stage
                with self._runtime() as backend:
                    self._complete_stop(backend)
        return self.status()


def create_session(
    session_dir=None,
    *,
    session_id=None,
    workpiece_id="097",
    seed=7007,
    case="mixed",
    mode="removal",
    views="multi",
    tool="belt",
    casting_profile=None,
    workpiece_profile=None,
    grasp_setup="original",
    camera_setup="original",
    detector="geometric",
    checkpoint=None,
    roi=None,
    max_passes=3,
    realtime_factor=0.0,
    removal_rate_mm_s=None,
    targeted_rescan=False,
    process_adapter: ProcessAdapter | None = None,
    reinspection_calibrator: ReinspectionCalibrator | None = None,
    backend_factory=None,
    mujoco_lock_held=False,
) -> WorkstationSession:
    """Create an offline session, or adopt an explicitly reused identity.

    Adopting the same identity retains the first configuration and checkpoint;
    other existing outputs are never overwritten. Plugins are re-injected when
    loading, while their evidence remains in the JSON checkpoint.
    """
    identity = (
        _identity(session_id) if session_id is not None else uuid.uuid4().hex
    )
    directory = Path(session_dir or SESSION_ROOT / identity).resolve()
    dependencies = {
        "process_adapter": process_adapter,
        "reinspection_calibrator": reinspection_calibrator,
        "backend_factory": backend_factory,
        "mujoco_lock_held": mujoco_lock_held,
    }
    if directory.exists():
        if (directory / "session_state.json").is_file() and session_id:
            existing = load_session(directory, **dependencies)
            if existing.session_id != session_id:
                raise ValueError("目录属于另一会话")
            return existing
        if any(directory.iterdir()):
            raise FileExistsError("会话输出目录已有记录，拒绝覆盖")
    values = {
        "workpiece_id": workpiece_id,
        "seed": seed,
        "case": case,
        "mode": mode,
        "views": views,
        "tool": tool,
        "workpiece_profile": workpiece_profile,
        "grasp_setup": grasp_setup,
        "camera_setup": camera_setup,
        "detector": detector,
        "checkpoint": checkpoint,
        "roi": roi,
        "max_passes": max_passes,
        "realtime_factor": realtime_factor,
        "removal_rate_mm_s": removal_rate_mm_s,
        "targeted_rescan": targeted_rescan,
    }
    if casting_profile is not None:
        values["casting_profile"] = casting_profile
    configuration = validate_session_config(values)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "interventions.jsonl").touch()
    state = {
        "schema_version": 1,
        "session_id": identity,
        "stage": "IDLE",
        "workpiece_id": configuration["workpiece_id"],
        "config": configuration,
        "created_at": _timestamp(),
        "checkpoint_index": 0,
        "operation": None,
        "resume_stage": None,
        "stop_reason": None,
        "stop_kind": None,
        "safe_retracted": True,
        "pause_requested": False,
        "abort_requested": False,
        "active_segment_id": None,
        "regions": [],
        "registered_regions": [],
        "segments": [],
        "next_segment_id": 1,
        "round_index": 0,
        "executed_segments": [],
        "remaining_segments": [],
        "protected_points": [],
        "permitted_paths": [],
        "scans": [],
        "scan_snapshot": None,
        "observation_snapshots": [],
        "material_checkpoint": None,
        "stock": None,
        "live": None,
        "prediction_revision": -1,
        "perception": None,
        "reinspection": {},
        "execution_history": [],
        "motion_gate_passed": None,
        "process_state": {},
        "calibration": None,
        "local_baseline": None,
        "observational_release": False,
        "enterprise_measured": False,
        "truth_used_for_control": False,
        "truth_used_for_decision": False,
        "intervention_count": 0,
        "transitions": [],
        "plugins": {
            "process_adapter": process_adapter is not None,
            "reinspection_calibrator": reinspection_calibrator is not None,
        },
    }
    session = WorkstationSession(directory, state, **dependencies)
    session._save()
    return session


def load_session(
    session_dir,
    *,
    process_adapter: ProcessAdapter | None = None,
    reinspection_calibrator: ReinspectionCalibrator | None = None,
    backend_factory=None,
    mujoco_lock_held=False,
) -> WorkstationSession:
    """Load a durable session without constructing or integrating MuJoCo."""
    directory = Path(session_dir).resolve()
    state = json.loads(
        (directory / "session_state.json").read_text(encoding="utf-8")
    )
    if state.get("schema_version") != 1 or state.get("stage") not in STAGES:
        raise ValueError("会话检查点格式或阶段无效")
    _identity(state["session_id"])
    if (
        state.get("truth_used_for_control") is not False
        or state.get("truth_used_for_decision") is not False
    ):
        raise ValueError("会话检查点不满足观测驱动控制约束")
    _truth_free(state)
    session = WorkstationSession(
        directory,
        state,
        process_adapter=process_adapter,
        reinspection_calibrator=reinspection_calibrator,
        backend_factory=backend_factory,
        mujoco_lock_held=mujoco_lock_held,
    )
    if state["operation"] is not None:
        for segment in state["segments"]:
            if segment["status"] == "executing":
                segment["status"] = "pending"
                segment["needs_preflight"] = True
        state["active_segment_id"] = None
        state["resume_stage"] = (
            "EXECUTING" if state["stage"] == "EXECUTING" else state["stage"]
        )
        state["operation"] = None
        session._transition(
            "PAUSED",
            reason="执行进程中断；从最近已落盘的仿真边界恢复，续跑前必须退刀和重检",
        )
    return session


class _MujocoBackend:
    """Reuse the production inspector, planner and servo under the caller's lock."""

    def __init__(self, directory, config, checkpoint):
        import mujoco
        import numpy as np

        from .workstation_model import Workstation
        from .workstation_motion import GeometricRemoval

        if config["workpiece_id"] != "097":
            raise SessionStateError(
                "该会话后端当前仅接入 097；021 需接入对应 WorkpiecePackage 后端"
            )
        self.directory, self.config = directory, config
        self.sim = Workstation(
            directory / "models" / config["grasp_setup"],
            config["seed"],
            config["case"],
            tool=config["tool"],
            casting_profile=config["casting_profile"],
            grasp_setup=config["grasp_setup"],
            camera_setup=config["camera_setup"],
        )
        self.sim.output = directory
        self.removal = GeometricRemoval(
            self.sim, rate_mm_s=config["removal_rate_mm_s"]
        )
        self.last_phase = "home"
        self.planned_path = None
        self.learned_detector = None
        if checkpoint is not None:
            with np.load(checkpoint, allow_pickle=False) as saved:
                if saved["mesh_vert"].shape != self.sim.model.mesh_vert.shape:
                    raise SessionSafetyError(
                        "恢复模型的余料网格结构已变化，不能复用旧检查点"
                    )
                self.sim.model.mesh_vert[:] = saved["mesh_vert"]
                if str(saved["grasp_setup"].item()) == config["grasp_setup"]:
                    expected = mujoco.mj_stateSize(
                        self.sim.model, mujoco.mjtState.mjSTATE_INTEGRATION
                    )
                    if len(saved["mj_state"]) != expected:
                        raise SessionSafetyError(
                            "恢复模型的动力学状态结构已变化"
                        )
                    mujoco.mj_setState(
                        self.sim.model,
                        self.sim.data,
                        saved["mj_state"],
                        mujoco.mjtState.mjSTATE_INTEGRATION,
                    )
                    self.last_phase = str(saved["last_phase"].item())
                else:
                    # A simulated regrasp resets the fixture configuration only
                    # after the prior session checkpoint reached its loading boundary.
                    self.sim.data.time = float(saved["simulation_time_s"])
                names = saved["part_names"].tolist()
                for part in self.removal.parts:
                    index = names.index(part["name"])
                    part["vertices"] = saved[f"vertices_{index}"].copy()
                    part["initial"] = saved[f"initial_{index}"].copy()
            self.removal.changed = True
            mujoco.mj_forward(self.sim.model, self.sim.data)
        self._feedback = []

    def scan(self, output, **options):
        from .workstation_inspection import Inspection
        from .workstation_model import CELL_CONFIG
        from .workstation_web import export_scene

        if not (self.directory / "model.json").exists():
            export_scene(self.sim)
        if (
            self.config["detector"] != "geometric"
            and self.learned_detector is None
        ):
            from .learned_defects import LearnedDetector

            self.learned_detector = LearnedDetector(self.config["checkpoint"])
        output.mkdir(parents=True, exist_ok=False)
        angles = options["angles_deg"] or (
            [0]
            if self.config["views"] == "single"
            else CELL_CONFIG["turntable"]["angles_deg"]
        )
        expanded = [
            angle
            for angle in angles
            for _ in range(options["extra_frames"] + 1)
        ]
        inspection = Inspection(
            self.sim,
            self.config["seed"],
            roi=self.config["roi"],
            revision=options["revision"],
            publish=options["publish"],
            learned_detector=self.learned_detector,
            hybrid=self.config["detector"] == "hybrid",
            targets=options["targets"],
            permitted_paths=options["permitted_paths"],
            whole_workpiece=options["whole_workpiece"],
            targeted_rescan=self.config["targeted_rescan"],
            removal_rate_mm_s=self.config["removal_rate_mm_s"],
        )
        self.sim.output = output
        try:
            detections, paths, perception = inspection.scan(expanded)
        finally:
            self.sim.output = self.directory
        self.last_phase = "home"
        if options["extra_frames"] and inspection.reinspection is not None:
            # Extra fixed-angle repeats do not change the signed tolerance or
            # remove any anchor. Full coverage still requires all six angles.
            whole = (
                options["whole_workpiece"]
                and self.config["roi"] is None
                and set(float(a) % 360 for a in angles)
                >= set(CELL_CONFIG["turntable"]["angles_deg"])
            )
            quality = inspection.reinspection.report(
                CELL_CONFIG["planning"]["target_remaining_height_mm"],
                whole_workpiece_scan=whole,
            )
            quality.update(
                independent_geometric_signals=inspection.geometric_review,
                local_regrind=perception["region_reinspection"][
                    "local_regrind"
                ],
            )
            quality["observational_release"] = (
                quality["observational_release"]
                and not inspection.geometric_review
            )
            perception["region_reinspection"] = quality
        protected = [
            point.tolist()
            for points in inspection.review_points
            for point in points
        ]
        return {
            "detections": detections,
            "paths": paths,
            "perception": perception,
            "registered_regions": inspection.registered_targets(
                include_unselected=True
            ),
            "protected_points": protected,
            "views": inspection.views,
            "observations": [
                f"view_{index:02d}_observation.npz"
                for index in range(len(inspection.views))
            ],
            "requested_extra_frames": options["extra_frames"],
        }

    def plan(self, path):
        from .workstation_planning import build_plan

        self.planned_path = path
        return build_plan(self.sim, [path], self.config["mode"])

    def preflight(self, commands):
        from .workstation_motion import preflight

        if not commands or "motion_binding" not in commands[0]:
            return {
                "passed": False,
                "reason": "缺少当前场景的 motion_binding，拒绝未绑定命令",
            }
        check = preflight(self.sim, commands)
        return {
            **check,
            "motion_binding_checked": not check.get("stale_plan", False),
        }

    def execute(self, commands, **options):
        import csv

        import numpy as np

        from .workstation_motion import execute

        started = float(self.sim.data.time)

        def publish(value):
            self.last_phase = value["phase"]
            options["publish"](value)

        result, self.removal = execute(
            self.sim,
            commands,
            options["mode"],
            publish,
            realtime_factor=self.config["realtime_factor"],
            removal=self.removal,
            prefix=options["prefix"],
            stop_requested=options["stop_requested"],
        )
        self.last_phase = result.get("last_phase") or "home"
        reference = options["prefix"] + "execution.csv"
        engaged, force_integral, previous = 0.0, 0.0, started
        with (self.directory / reference).open(newline="") as stream:
            for row in csv.DictReader(stream):
                timestamp = float(row["time_s"])
                if row["phase"] in {"trace", "approach"}:
                    duration = max(0.0, timestamp - previous)
                    engaged += duration
                    force_integral += duration * max(
                        0.0, float(row["tool_force_n"])
                    )
                previous = timestamp
        result.update(
            engaged_duration_s=engaged,
            force_n=force_integral / engaged if engaged else 0.0,
            engagement_source="actual logged approach/trace servo intervals; force is the logged interval mean",
            feedback_reference=reference,
            truth_used_for_control=False,
        )
        if result["interrupted"]:
            elapsed = float(self.sim.data.time) - started
            completed = [
                command
                for command in commands
                if command["t"] <= elapsed
                and command.get("phase") == "trace"
                and command.get("point") is not None
            ]
            index = 0
            if completed and self.planned_path is not None:
                points = np.asarray(self.planned_path["nominal_xyz"])
                index = min(
                    len(points) - 2,
                    int(
                        np.linalg.norm(
                            points - np.asarray(completed[-1]["point"]), axis=1
                        ).argmin()
                    ),
                )
            result["resume_from_point"] = index
        return result

    def safe_retract(self, **options):
        import numpy as np

        from .workstation_model import LOADING_POSITION
        from .workstation_motion import (
            NORMAL,
            PROCESS_RETREAT_M,
            bind_commands,
            execute,
            transform,
        )
        from .workstation_planning import Commands

        builder = Commands(self.sim)
        builder.attached = bool(self.sim.data.eq_active[self.sim.gripper_weld])
        loading = transform(self.sim.loading_rotation, LOADING_POSITION)
        lift = transform(
            self.sim.loading_rotation, LOADING_POSITION + [0, 0, 0.38]
        )
        try:
            if builder.attached:
                actual = self.sim.body_transform(self.sim.workpiece)
                if self.last_phase in {"approach", "trace", "retract"}:
                    retreat = actual.copy()
                    retreat[:3, 3] -= NORMAL * PROCESS_RETREAT_M
                    builder.leg(retreat, "retract", speed=0.025)
                builder.leg(
                    transform(actual[:3, :3], [-0.6, -0.65, 1.10]),
                    "return",
                    speed=0.08,
                )
                builder.leg(
                    transform(self.sim.loading_rotation, [-0.6, -0.65, 1.10]),
                    "return",
                    speed=0.08,
                )
                builder.leg(lift, "return", speed=0.08)
                builder.leg(loading, "release", speed=0.07, event="release")
            elif not bool(self.sim.data.eq_active[self.sim.fixture_weld]):
                raise SessionSafetyError(
                    "工件未被抓手或定位夹具约束，拒绝假定安全退刀"
                )
            builder.leg(
                transform(
                    self.sim.loading_rotation, LOADING_POSITION + [0, 0, 0.35]
                ),
                "home",
                speed=0.08,
            )
            commands = bind_commands(self.sim, builder.items)
            check = self.preflight(commands)
            if not check["passed"]:
                return {
                    "passed": False,
                    "preflight": check,
                    "reason": "安全退刀连续曲线未准入",
                }
            result, self.removal = execute(
                self.sim,
                commands,
                "clearance",
                options["publish"],
                realtime_factor=0,
                removal=self.removal,
                prefix="safe_retract_" + uuid.uuid4().hex[:12] + "_",
            )
            self.last_phase = "home"
            passed = (
                result["unexpected_contact_count"] == 0
                and bool(self.sim.data.eq_active[self.sim.fixture_weld])
                and not bool(self.sim.data.eq_active[self.sim.gripper_weld])
            )
            return {
                "passed": passed,
                "preflight": check,
                "execution": result,
                "fixture_attached": bool(
                    self.sim.data.eq_active[self.sim.fixture_weld]
                ),
                "grasp_attached": bool(
                    self.sim.data.eq_active[self.sim.gripper_weld]
                ),
                "source": "checked retract/return and actual integrated fixture release",
            }
        except (ValueError, RuntimeError) as error:
            self.sim.data.ctrl[self.sim.arm_actuators] = self.sim.data.qpos[
                self.sim.q_index
            ].copy()
            self.sim.data.ctrl[self.sim.model.actuator("spindle_speed").id] = 0
            return {
                "passed": False,
                "reason": str(error),
                "joint_positions_teleported": False,
                "joint_angles_deg": np.rad2deg(
                    self.sim.data.qpos[self.sim.q_index]
                ).tolist(),
            }

    def manual_region(self, roi_spec, observation_path):
        import numpy as np

        from .thin_reference import detect_thin_reference
        from .workstation_inspection import (
            BOUNDS,
            CELL_DETECTOR_CONFIG,
            apply_review_envelope,
            observed_paths,
            surface_normals,
        )
        from .workstation_quality import surface_anchors

        with np.load(observation_path, allow_pickle=False) as captured:
            candidates, observation = detect_thin_reference(
                captured["rgb"],
                captured["depth"],
                captured["reference_depth"],
                captured["K"],
                captured["T_workpiece_camera"],
                roi=roi_spec["polygon"],
                inspection_bounds=BOUNDS,
                reference_object_mask=captured["reference_object_mask"],
                allow_sloped_surface=True,
                config=CELL_DETECTOR_CONFIG,
            )
            observation["normals"] = surface_normals(
                observation["reference_xyz"], captured["T_workpiece_camera"]
            )
        observation["tool"] = self.sim.tool
        candidates = apply_review_envelope(candidates, observation)
        if any(
            candidate.kind in {"preserve", "review"} for candidate in candidates
        ):
            raise SessionSafetyError(
                "人工 ROI 覆盖正常保留结构或超高余料，不能绕过保护分类"
            )
        eligible = [
            (candidate, observed_paths(candidate, observation))
            for candidate in candidates
        ]
        eligible = [
            (candidate, paths) for candidate, paths in eligible if paths
        ]
        if len(eligible) != 1:
            raise ValueError(
                "人工 ROI 须只包含一个具有有效三维余量证据的可加工区域"
            )
        candidate, paths = eligible[0]
        if "kind" in roi_spec and candidate.kind != roi_spec["kind"]:
            raise SessionSafetyError("人工区域类型与测得几何分类不符")
        points, normals = surface_anchors(candidate, observation)
        return {
            "region": {**candidate.summary(), "machining_required": True},
            "target": {
                "id": candidate.id,
                "kind": candidate.kind,
                "points": points,
                "normals": normals,
            },
            "paths": paths,
            "source_observation": str(observation_path),
        }

    def save_checkpoint(self, output):
        import mujoco
        import numpy as np

        state = np.empty(
            mujoco.mj_stateSize(
                self.sim.model, mujoco.mjtState.mjSTATE_INTEGRATION
            )
        )
        mujoco.mj_getState(
            self.sim.model,
            self.sim.data,
            state,
            mujoco.mjtState.mjSTATE_INTEGRATION,
        )
        arrays = {
            "mj_state": state,
            "mesh_vert": self.sim.model.mesh_vert.copy(),
            "grasp_setup": np.asarray(self.config["grasp_setup"]),
            "simulation_time_s": np.asarray(float(self.sim.data.time)),
            "last_phase": np.asarray(self.last_phase),
            "part_names": np.asarray(
                [part["name"] for part in self.removal.parts], dtype=str
            ),
            **{
                f"vertices_{index}": part["vertices"]
                for index, part in enumerate(self.removal.parts)
            },
            **{
                f"initial_{index}": part["initial"]
                for index, part in enumerate(self.removal.parts)
            },
        }
        temporary = output.with_suffix(".npz.tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(stream, **arrays)
        temporary.replace(output)

    def stock_summary(self):
        return self.removal.summary()

    def close(self):
        self.sim.close()


def validate_session_action(action: str, parameters: dict) -> dict:
    """Validate JSON intervention arguments before admitting an HTTP worker."""
    allowed = {
        "step_next": set(),
        "pause": set(),
        "safe_retract": set(),
        "resume": set(),
        "abort": set(),
        "replan_remaining": set(),
        "run_until_pause": {"max_steps"},
        "approve_regions": {"region_ids"},
        "mask_false_positive": {"region_id", "reason"},
        "add_manual_region": {"roi_spec"},
        "rescan_views": {"angles_deg", "extra_frames"},
        "set_segment_params": {
            "segment_id",
            "feed_scale",
            "normal_offset_mm",
            "passes",
            "dwell_scale",
        },
        "split_segment": {"segment_id", "ratio"},
        "switch_grasp": {"grasp_setup", "segment_id"},
        "skip_segment": {"segment_id", "reason"},
    }
    if action not in allowed or not isinstance(parameters, dict):
        raise ValueError("未知会话动作或非 JSON 对象参数")
    if set(parameters) - allowed[action]:
        raise ValueError(
            "未知动作参数："
            + ", ".join(sorted(set(parameters) - allowed[action]))
        )
    required = {
        "approve_regions": {"region_ids"},
        "mask_false_positive": {"region_id"},
        "add_manual_region": {"roi_spec"},
        "set_segment_params": {"segment_id"},
        "split_segment": {"segment_id"},
        "switch_grasp": {"grasp_setup"},
        "skip_segment": {"segment_id"},
    }.get(action, set())
    if required - set(parameters):
        raise ValueError(
            "缺少动作参数：" + ", ".join(sorted(required - set(parameters)))
        )
    for key in ("segment_id", "region_id"):
        if key in parameters and parameters[key] is not None:
            _integer(parameters[key], key, 1, 1000000)
    if "reason" in parameters and (
        not isinstance(parameters["reason"], str)
        or len(parameters["reason"]) > 2000
    ):
        raise ValueError("reason 须为不超过 2000 字符的文本")
    if action == "run_until_pause":
        _integer(parameters.get("max_steps", 100), "max_steps", 1, 1000)
    if action == "approve_regions":
        ids = parameters["region_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise ValueError("region_ids 须为非空区域编号列表")
        for identity in ids:
            _integer(identity, "region_id", 1, 1000000)
    if action == "set_segment_params":
        updates = {
            key: value
            for key, value in parameters.items()
            if key in PARAMETER_DEFAULTS and value is not None
        }
        if not updates:
            raise ValueError("请提供至少一个段参数")
        for key, value in updates.items():
            if key == "passes":
                _integer(value, key, 1, 10)
            elif key == "normal_offset_mm":
                _number(value, key, -10, 10)
            else:
                _number(
                    value,
                    key,
                    0.1 if key == "feed_scale" else 0.25,
                    3 if key == "feed_scale" else 4,
                )
    if action == "split_segment":
        _number(parameters.get("ratio", 0.5), "ratio", 0.001, 0.999)
    if action == "switch_grasp":
        from .workstation_model import GRASP_SETUPS

        if (
            not isinstance(parameters["grasp_setup"], str)
            or parameters["grasp_setup"] not in GRASP_SETUPS
        ):
            raise ValueError("不支持的夹持方案")
    if action == "rescan_views":
        angles = parameters.get("angles_deg")
        if angles is not None:
            if not isinstance(angles, list) or not 1 <= len(angles) <= 24:
                raise ValueError("angles_deg 须为 1–24 个角度的列表")
            for angle in angles:
                _number(angle, "angle_deg", 0, 360)
        frames = parameters.get("extra_frames")
        if frames is not None:
            _integer(frames, "extra_frames", 0, 8)
    if action == "add_manual_region":
        spec = parameters["roi_spec"]
        if not isinstance(spec, dict) or set(spec) - {
            "polygon",
            "view_index",
            "kind",
            "reason",
        }:
            raise ValueError(
                "roi_spec 须包含 polygon 和可选 view_index/kind/reason"
            )
        _polygon(spec.get("polygon"))
        _integer(spec.get("view_index", 0), "view_index", 0, 1000)
        if "kind" in spec and spec["kind"] not in {"seam", "burr"}:
            raise ValueError("人工 ROI 类型仅可为 seam 或 burr")
    return dict(parameters)
