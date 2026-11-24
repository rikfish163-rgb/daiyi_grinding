"""Auditable, intervenable closed-loop boundaries for offline grinding.

A policy is called once at each checkpoint. Scripted directives at the same
boundary are applied in queue order before the policy. Parameter edits invalidate
planning; no edit changes contact allowance, observation evidence or a latched
safety stop. Pause/resume operates at boundaries, not inside a robot command.

``SavedRunReplayAdapter`` replays sensor and motion evidence without executing
commands. Its counterfactual path/feed proposals never constitute new machining,
new measurements or an observational release. Other offline adapters implement
``GrindingAdapter`` and must supply fresh planning and execution receipts.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal, Protocol

from . import project

CHECKPOINT_SCHEMA = "daiyi-programmable-grinding-checkpoint-v1"
TRACE_SCHEMA = "daiyi-programmable-grinding-trace-v1"
REPLAY_SCHEMA = "daiyi-saved-grinding-replay-v1"


class GrindingStage(StrEnum):
    """Safe intervention points in the existing scan/plan/reinspect loop."""

    POST_PERCEPTION = "POST_PERCEPTION"
    POST_PLANNING = "POST_PLANNING"
    PRE_EXECUTION = "PRE_EXECUTION"
    POST_REINSPECTION = "POST_REINSPECTION"
    PRE_REGRIND = "PRE_REGRIND"


InterventionAction = Literal[
    "continue",
    "mask_regions",
    "exclude_paths",
    "adjust_feed_scale",
    "set_regrind_budget",
    "request_extra_views",
    "pause",
    "abort_safe",
]
ACTION_KEYS = {
    "continue": set(),
    "mask_regions": {"region_ids"},
    "exclude_paths": {"path_ids"},
    "adjust_feed_scale": {"feed_scale"},
    "set_regrind_budget": {"regrind_budget"},
    "request_extra_views": {"count"},
    "pause": set(),
    "abort_safe": set(),
}
REGION_FIELDS = {
    "id",
    "kind",
    "status",
    "status_reason",
    "normal_height_mm",
    "machining_required",
    "observations",
    "anchors",
    "observed_anchors",
    "anchor_coverage",
    "overgrind",
    "negative_limit_risk",
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
QUALITY_FIELDS = {
    "verified",
    "needs_rework",
    "uncertain",
    "overgrind",
    "negative_limit_risk",
    "insufficient",
    "invalid_observations",
    "automatic_regrind_allowed",
    "observational_release",
    "coverage_scope",
    "signed_limits_mm",
    "uncertainty_model",
}
PATH_FIELDS = {
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
    "filter",
    "regrind",
}


def _json_copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _integer(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _positive_number(value, name):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


def _ids(value, name):
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of integer identities")
    return tuple(dict.fromkeys(_integer(v, name, minimum=1) for v in value))


def _timestamp():
    return datetime.now(UTC).isoformat()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _observed_regions(rows):
    return [
        {key: _json_copy(row[key]) for key in REGION_FIELDS if key in row}
        for row in rows
    ]


def _quality_report(value):
    if value.get("truth_used_for_decision", False) is not False:
        raise ValueError("Reinspection must not use truth for decisions")
    return {
        **{
            key: _json_copy(value[key])
            for key in QUALITY_FIELDS
            if key in value
        },
        "regions": _observed_regions(value.get("regions", [])),
        "truth_used_for_decision": False,
    }


def _observed_paths(paths):
    result = []
    for path in paths:
        if (
            path.get("regrind", {}).get("truth_used_for_control", False)
            is not False
        ):
            raise ValueError("Regrind paths must not use truth for control")
        result.append(
            {
                **{
                    key: _json_copy(path[key])
                    for key in PATH_FIELDS
                    if key in path
                },
                "recipe": {
                    key: _json_copy(path["recipe"][key])
                    for key in (
                        "tool",
                        "feed_mm_s",
                        "force_target_n",
                        "overlap",
                    )
                    if key in path["recipe"]
                },
            }
        )
    return result


def _hard_stop(quality, execution=None):
    if execution:
        if execution.get("unexpected_contact_count") or execution.get(
            "unexpected_contacts"
        ):
            return "unexpected_collision_requires_disposition"
        if not execution.get("motion_gate_passed", False):
            return "motion_safety_gate"
        if not execution.get("execution_complete", False):
            return "incomplete_execution_requires_review"
    rows = quality.get("regions", [])
    if quality.get("overgrind") or any(row.get("overgrind") for row in rows):
        return "overgrind_requires_disposition"
    if quality.get("negative_limit_risk") or any(
        row.get("negative_limit_risk") for row in rows
    ):
        return "negative_limit_requires_review"
    lower_limit = quality.get("signed_limits_mm", {}).get("lower")
    if lower_limit is not None and any(
        bounds is not None and bounds[0] is not None and bounds[0] < lower_limit
        for row in rows
        for bounds in row.get("anchor_bounds_mm", [])
    ):
        return "negative_limit_requires_review"
    if quality.get("invalid_observations"):
        return "invalid_reinspection_requires_review"
    return None


@dataclass(frozen=True)
class StageCheckpoint:
    """A detached, finite JSON snapshot; dimensions use the workstation units."""

    round_index: int
    stage: GrindingStage
    observed_region_states: tuple[dict, ...] = ()
    candidate_path_ids: tuple[int, ...] = ()
    proposed_parameters: tuple[dict, ...] = ()
    controller_decision: dict = field(default_factory=dict)
    safety_hard_stop: bool = False
    stop_reason: str | None = None

    def __post_init__(self):
        _integer(self.round_index, "round_index")
        object.__setattr__(self, "stage", GrindingStage(self.stage))
        object.__setattr__(
            self,
            "candidate_path_ids",
            _ids(self.candidate_path_ids, "path_ids"),
        )
        if type(self.safety_hard_stop) is not bool:
            raise ValueError("safety_hard_stop must be a boolean")
        if self.stop_reason is not None and not isinstance(
            self.stop_reason, str
        ):
            raise ValueError("stop_reason must be text or None")
        if self.safety_hard_stop and not self.stop_reason:
            raise ValueError("A hard safety stop requires a reason")
        for name in ("observed_region_states", "proposed_parameters"):
            object.__setattr__(
                self, name, tuple(_json_copy(getattr(self, name)))
            )
        object.__setattr__(
            self, "controller_decision", _json_copy(self.controller_decision)
        )

    def to_dict(self) -> dict:
        """Return an independent JSON document."""
        return _json_copy(asdict(self))

    @classmethod
    def from_dict(cls, value: dict) -> StageCheckpoint:
        """Decode the same public snapshot contract."""
        return cls(**value)


@dataclass(frozen=True)
class InterventionDirective:
    """One operator/script request with explicit identity and justification.

    Payload keys are ``region_ids``, ``path_ids``, ``feed_scale``,
    ``regrind_budget`` or extra-view ``count``, according to the action.
    Continue, pause and safe abort take an empty payload. Feed scale is absolute
    relative to the unedited recipe, not a repeatedly compounded multiplier.
    """

    action: InterventionAction
    payload: dict = field(default_factory=dict)
    actor: str = "script"
    reason: str = ""

    def __post_init__(self):
        if self.action not in ACTION_KEYS:
            raise ValueError(f"Unsupported intervention action: {self.action}")
        if not isinstance(self.payload, dict):
            raise ValueError("Intervention payload must be an object")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("Intervention actor is required")
        if not isinstance(self.reason, str):
            raise ValueError("Intervention reason must be text")
        object.__setattr__(self, "payload", _json_copy(self.payload))

    def to_dict(self) -> dict:
        """Return a queue- and audit-compatible JSON record."""
        return _json_copy(asdict(self))


class SafetyVeto(RuntimeError):
    """A recorded intervention or execution admission was refused."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GrindingAdapter(Protocol):
    """Offline process boundary, keeping motion and evidence outside policy.

    Perception returns ``regions``, ``paths``, ``registered_targets`` and
    optional ``protected_points``. Planning returns ``paths`` (admitted paths)
    and ``preflight``. Execution returns ``motion_gate_passed``,
    ``execution_complete`` and ``unexpected_contact_count``. Non-replay
    execution also proves ``safe_retracted=True`` before exposing another
    pause/abort boundary. Reinspection returns the existing signed region
    report. All payloads are finite JSON.

    Plans must apply to the exact supplied recipes and return freshly checked
    commands/receipts when ``replay_only`` is False. Execution must never reuse
    old commands after a path/feed edit. State restoration is explicit and must
    not start execution. Hardware adapters are not provided by this module.
    """

    replay_only: bool
    mode: str
    removal_rate_mm_s: float | None

    def perceive(self) -> dict: ...
    def plan(self, paths: list[dict], *, round_index: int) -> dict: ...
    def execute(self, planning: dict, *, round_index: int) -> dict: ...
    def reinspect(self, *, round_index: int) -> dict: ...
    def request_extra_views(
        self, *, stage: GrindingStage, round_index: int, count: int
    ) -> dict | None: ...
    def save_state(self) -> dict: ...
    def restore_state(self, value: dict) -> None: ...


class SavedRunReplayAdapter:
    """Read-only adapter for real saved workstation results, not new motion."""

    replay_only = True

    def __init__(self, data: dict):
        self.restore_state(data)

    @property
    def mode(self) -> str:
        return self._data["mode"]

    @property
    def removal_rate_mm_s(self) -> float | None:
        return self._data.get("removal_rate_mm_s")

    @classmethod
    def from_run(cls, result_path: Path | str) -> SavedRunReplayAdapter:
        """Load sensor reports and path provenance, excluding evaluation truth."""
        result_path = Path(result_path).resolve()
        directory = result_path.parent
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (
            result.get("controller_stopping", {}).get(
                "truth_used_for_control", False
            )
            is not False
        ):
            raise ValueError("Saved controller decisions must be truth-free")
        config_path = directory / "config.json"
        config = (
            json.loads(config_path.read_text(encoding="utf-8"))
            if config_path.is_file()
            else {}
        )
        paths = json.loads(
            (directory / "paths.json").read_text(encoding="utf-8")
        )
        detections = json.loads(
            (directory / "detections.json").read_text(encoding="utf-8")
        )
        targets = json.loads(
            (directory / "registered_regions.json").read_text(encoding="utf-8")
        )
        tracking_limit = json.loads(
            (project.ROOT / "config/workstation.json").read_text(
                encoding="utf-8"
            )
        )["planning"]["tracking_p95_limit_mm"]
        rounds = []
        for index, execution in enumerate(result.get("executions", [])):
            recheck = result["reinspections"][index]
            previous = result["reinspections"][index - 1] if index else None
            motion_gate = execution["unexpected_contact_count"] == 0 and all(
                execution.get(key) is not None
                and execution[key] < tracking_limit
                for key in ("tracking_trace_p95_mm", "surface_tracking_p95_mm")
            )
            rounds.append(
                {
                    "preflight": _json_copy(
                        previous.get("followup_preflight", {})
                        if previous
                        else result["preflight"]
                    ),
                    "execution": {
                        **{
                            key: execution[key]
                            for key in (
                                "unexpected_contact_count",
                                "unexpected_contacts",
                                "tracking_trace_p95_mm",
                                "surface_tracking_p95_mm",
                                "simulated_seconds",
                            )
                            if key in execution
                        },
                        "motion_gate_passed": motion_gate,
                        "execution_complete": True,
                    },
                    "quality": _quality_report(recheck["region_reinspection"]),
                }
            )
        return cls(
            {
                "schema": REPLAY_SCHEMA,
                "source_result": str(result_path),
                "mode": result["mode"],
                "regions": _observed_regions(detections),
                "paths": _observed_paths(paths),
                "registered_targets": targets,
                "protected_points": [
                    point
                    for target in targets
                    if target["kind"] == "review"
                    for point in target["points"]
                ],
                "rounds": rounds,
                "removal_rate_mm_s": config.get("removal_rate_mm_s"),
            }
        )

    def perceive(self) -> dict:
        """Return initial observed regions and their permitted nominal traces."""
        return _json_copy(
            {
                key: self._data[key]
                for key in (
                    "regions",
                    "paths",
                    "registered_targets",
                    "protected_points",
                )
            }
        )

    def plan(self, paths: list[dict], *, round_index: int) -> dict:
        """Replay source admission while marking all commands as unavailable."""
        source = (
            self._data["rounds"][round_index]["preflight"]
            if round_index < len(self._data["rounds"])
            else {}
        )
        return {
            "paths": _json_copy(paths),
            "preflight": {
                "passed": source.get("passed", False),
                "source_preflight": _json_copy(source),
                "source_result": self._data["source_result"],
                "source_round_index": round_index,
                "replay_only": True,
                "fresh_motion_plan": False,
                "commands_executable": False,
            },
            "replay_only": True,
        }

    def execute(self, planning: dict, *, round_index: int) -> dict:
        """Return historical feedback; do not execute the proposed path list."""
        return {
            **_json_copy(self._data["rounds"][round_index]["execution"]),
            "replay_only": True,
            "source_kind": "saved_simulation_evidence_replay",
            "source_result": self._data["source_result"],
            "source_round_index": round_index,
            "replayed_candidate_path_ids": [
                path["segment_id"] for path in planning["paths"]
            ],
            "new_motion_executed": False,
            "new_material_removal_measured": False,
        }

    def reinspect(self, *, round_index: int) -> dict:
        """Return source signed measurements, not counterfactual measurements."""
        return _json_copy(self._data["rounds"][round_index]["quality"])

    def request_extra_views(
        self, *, stage: GrindingStage, round_index: int, count: int
    ) -> dict | None:
        """A saved bundle cannot provide independent new views."""
        return None

    def save_state(self) -> dict:
        """Embed the read-only evidence so checkpoint loading need not rescan."""
        return _json_copy(self._data)

    def restore_state(self, value: dict) -> None:
        """Restore a finite offline replay bundle without invoking a simulator."""
        if value.get("schema") != REPLAY_SCHEMA:
            raise ValueError("Unsupported saved-run replay schema")
        data = _json_copy(value)
        data["regions"] = _observed_regions(data["regions"])
        data["paths"] = _observed_paths(data["paths"])
        for row in data["rounds"]:
            row["quality"] = _quality_report(row["quality"])
        if data.get("removal_rate_mm_s") is not None:
            _positive_number(data["removal_rate_mm_s"], "removal_rate_mm_s")
        self._data = data


class ProgrammableGrindingSession:
    """Run, inspect and persist bounded offline grinding decisions.

    A queued directive is consumed before a stage advances, and a callback is
    never re-invoked for an already-decided checkpoint after resume. Only
    unconsumed directives remain in the checkpoint queue. A saved callback must
    be reattached explicitly on load; Python code is not serialized.
    """

    def __init__(
        self,
        adapter: GrindingAdapter,
        *,
        policy: Callable[[StageCheckpoint], InterventionDirective]
        | None = None,
        regrind_budget: int | None = None,
        clock: Callable[[], str] = _timestamp,
    ):
        from .adaptive_process import ProcessLimits

        config = json.loads(
            (project.ROOT / "config/workstation.json").read_text(
                encoding="utf-8"
            )
        )["planning"]
        self._max_regrinds = config["max_reinspection_passes"]
        self._max_allowance_mm = config["target_remaining_height_mm"]
        self._feed_bounds = ProcessLimits().feed_scale_bounds
        self.adapter = adapter
        self.policy = policy
        self._clock = clock
        self._state = {
            "status": "READY",
            "stage": None,
            "round_index": 0,
            "regions": [],
            "registered_targets": [],
            "protected_points": [],
            "base_paths": [],
            "permitted_paths": [],
            "planning": {},
            "planning_dirty": True,
            "safe_boundary": True,
            "quality": {},
            "masked_regions": [],
            "excluded_paths": [],
            "feed_scale": 1.0,
            "regrind_budget": min(
                self._max_regrinds,
                _integer(regrind_budget, "regrind_budget")
                if regrind_budget is not None
                else self._max_regrinds,
            ),
            "hard_stop_reason": None,
            "stop_reason": None,
            "policy_applied": False,
            "queue": [],
            "checkpoints": [],
            "events": [],
            "interventions": [],
            "executions": [],
            "regrind_report": {},
            "next_path_id": 1,
        }

    @property
    def status(self) -> str:
        return self._state["status"]

    @property
    def round_index(self) -> int:
        return self._state["round_index"]

    @property
    def checkpoint(self) -> StageCheckpoint:
        """Expose a detached snapshot; policy edits cannot mutate session state."""
        self._start()
        paths = self._filtered_paths()
        decision = self._decision(paths)
        return StageCheckpoint(
            round_index=self.round_index,
            stage=GrindingStage(self._state["stage"]),
            observed_region_states=tuple(
                {**row, "masked": row["id"] in self._state["masked_regions"]}
                for row in self._state["regions"]
            ),
            candidate_path_ids=tuple(path["segment_id"] for path in paths),
            proposed_parameters=tuple(
                {
                    "path_id": path["segment_id"],
                    "region_id": path["detection_id"],
                    "feed_scale": self._state["feed_scale"],
                    "feed_mm_s": path["recipe"]["feed_mm_s"],
                    "material_allowance_mm": path["material_allowance_mm"],
                    "normal_offset_mm": 0.0,
                    "force_target_n": path["recipe"].get("force_target_n"),
                    "requires_fresh_motion_plan": self._state["planning_dirty"],
                }
                for path in paths
            ),
            controller_decision=decision,
            safety_hard_stop=self._state["hard_stop_reason"] is not None,
            stop_reason=self._state["hard_stop_reason"]
            or self._state["stop_reason"]
            or decision["reason"],
        )

    @property
    def interventions(self) -> list[dict]:
        """Return rows accepted by interventions_to_hard_examples."""
        return _json_copy(self._state["interventions"])

    def queue_directive(
        self,
        directive: InterventionDirective,
        *,
        stage: GrindingStage | str | None = None,
        round_index: int | None = None,
    ) -> None:
        """Schedule one request at an explicit stage/round or the current one."""
        self._state["queue"].append(
            {
                "stage": GrindingStage(
                    stage
                    or self._state["stage"]
                    or GrindingStage.POST_PERCEPTION
                ).value,
                "round_index": _integer(
                    self.round_index if round_index is None else round_index,
                    "round_index",
                ),
                "directive": directive.to_dict(),
            }
        )

    def apply_directive(
        self, directive: InterventionDirective
    ) -> StageCheckpoint:
        """Apply one request, recording accepted edits and explicit vetoes.

        This does not execute a stage. ``step``/``run`` drive the adapter. An
        explicit continue resumes a paused boundary, but never clears a hard
        safety stop. Reductions/exclusions remain inspectable while stopped.
        """
        self._start()
        before = self.checkpoint.to_dict()
        try:
            self._enforce(directive)
            self._apply(directive)
        except (SafetyVeto, ValueError) as error:
            veto = (
                error
                if isinstance(error, SafetyVeto)
                else SafetyVeto("invalid_directive_payload", str(error))
            )
            self._record_intervention(
                directive,
                directive.action,
                None,
                {},
                {},
                status="vetoed",
                details={"veto_code": veto.code, "veto_reason": str(veto)},
            )
            self._event(
                "safety_veto", before=before, code=veto.code, reason=str(veto)
            )
            if veto is error:
                raise
            raise veto from error
        self._event(
            "intervention_applied",
            directive=directive.to_dict(),
            before=before,
            after=self.checkpoint.to_dict(),
        )
        return self.checkpoint

    def step(self) -> StageCheckpoint:
        """Resolve one checkpoint, then advance to the next closed-loop boundary."""
        self._start()
        if self.status in {"PAUSED", "DONE", "ABORTED"}:
            return self.checkpoint
        current = (self._state["stage"], self.round_index)
        checkpoint_count = len(self._state["checkpoints"])
        try:
            while True:
                pending = next(
                    (
                        item
                        for item in self._state["queue"]
                        if (item["stage"], item["round_index"]) == current
                    ),
                    None,
                )
                if pending is None:
                    break
                self._state["queue"].remove(pending)
                self.apply_directive(
                    InterventionDirective(**pending["directive"])
                )
                if self.status in {"PAUSED", "ABORTED", "STOPPED_SAFETY"}:
                    if self.status == "STOPPED_SAFETY":
                        self._state["policy_applied"] = True
                    return self.checkpoint
                if len(self._state["checkpoints"]) != checkpoint_count:
                    return self.checkpoint
            if not self._state["policy_applied"]:
                self._state["policy_applied"] = True
                self.apply_directive(
                    self.policy(self.checkpoint)
                    if self.policy is not None
                    else InterventionDirective("continue", actor="session")
                )
            if len(self._state["checkpoints"]) != checkpoint_count:
                return self.checkpoint
            if self.status == "READY":
                self._advance()
        except SafetyVeto as veto:
            self._event("stage_safety_hold", code=veto.code, reason=str(veto))
            if self._state["hard_stop_reason"]:
                self._state["status"] = "STOPPED_SAFETY"
                self._state["policy_applied"] = True
            else:
                self._state["status"] = "PAUSED"
                self._state["stop_reason"] = "intervention_requires_review"
        return self.checkpoint

    def run(self) -> dict:
        """Advance until completion, pause, safe abort or a mandatory safety hold."""
        self._start()
        while self.status == "READY":
            self.step()
        if (
            self.status == "STOPPED_SAFETY"
            and not self._state["policy_applied"]
        ):
            self.step()
        return self.summary()

    def resume(
        self, *, actor: str = "operator", reason: str = ""
    ) -> StageCheckpoint:
        """Idempotently resume a boundary pause, never a confirmed safety stop."""
        self._start()
        if self._state["hard_stop_reason"]:
            return self.apply_directive(
                InterventionDirective("continue", actor=actor, reason=reason)
            )
        if self.status != "PAUSED":
            return self.checkpoint
        return self.apply_directive(
            InterventionDirective("continue", actor=actor, reason=reason)
        )

    def save_checkpoint(self, path: Path | str) -> None:
        """Persist the exact boundary, unconsumed queue and adapter evidence."""
        _write_json(
            path,
            {
                "schema": CHECKPOINT_SCHEMA,
                "checkpoint": self.checkpoint.to_dict(),
                "state": _json_copy(self._state),
                "adapter_state": self.adapter.save_state(),
                "replay_only": self.adapter.replay_only,
                "policy_required": self.policy is not None,
                "truth_used_for_control": False,
            },
        )

    @classmethod
    def load_checkpoint(
        cls,
        path: Path | str,
        *,
        adapter: GrindingAdapter | None = None,
        policy: Callable[[StageCheckpoint], InterventionDirective]
        | None = None,
        clock: Callable[[], str] = _timestamp,
    ) -> ProgrammableGrindingSession:
        """Restore without executing, requiring explicit resume for live pauses."""
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError("Unsupported programmable checkpoint schema")
        checkpoint = StageCheckpoint.from_dict(value["checkpoint"])
        state = value["state"]
        if (
            value.get("policy_required")
            and policy is None
            and state["status"] not in {"DONE", "ABORTED", "STOPPED_SAFETY"}
        ):
            raise ValueError(
                "Reattach the stage policy when loading this checkpoint"
            )
        if adapter is None:
            adapter = SavedRunReplayAdapter(value["adapter_state"])
        else:
            adapter.restore_state(value["adapter_state"])
        if adapter.replay_only is not value["replay_only"]:
            raise ValueError(
                "Checkpoint adapter execution scope does not match"
            )
        session = cls(adapter, policy=policy, clock=clock)
        if set(state) != set(session._state):
            raise ValueError("Checkpoint session state fields do not match")
        _integer(state["round_index"], "round_index")
        budget = _integer(state["regrind_budget"], "regrind_budget")
        feed = _positive_number(state["feed_scale"], "feed_scale")
        if (
            budget > session._max_regrinds
            or not session._feed_bounds[0] <= feed <= session._feed_bounds[1]
        ):
            raise ValueError(
                "Checkpoint process parameters exceed the safe envelope"
            )
        if state["status"] not in {
            "READY",
            "PAUSED",
            "DONE",
            "ABORTED",
            "STOPPED_SAFETY",
        }:
            raise ValueError("Unknown checkpoint session status")
        session._state = _json_copy(state)
        session._latch(_hard_stop(state["quality"]))
        for row in state["executions"]:
            session._latch(_hard_stop({}, row))
        if session.checkpoint.to_dict() != checkpoint.to_dict():
            raise ValueError("Checkpoint snapshot and session state disagree")
        if session.status == "READY":
            session._state.update(
                status="PAUSED", stop_reason="loaded_checkpoint_requires_resume"
            )
        return session

    def summary(self) -> dict:
        """Distinguish workflow completion, source evidence and new acceptance."""
        self._start()
        decision = self._decision(self._filtered_paths())
        return {
            "status": self.status,
            "stage": self._state["stage"],
            "round_index": self.round_index,
            "completed_rounds": len(self._state["executions"]),
            "regrind_budget": self._state["regrind_budget"],
            "max_regrinds": self._max_regrinds,
            "feed_scale": self._state["feed_scale"],
            "feed_scale_bounds": list(self._feed_bounds),
            "masked_region_ids": list(self._state["masked_regions"]),
            "excluded_path_ids": list(self._state["excluded_paths"]),
            "safety_hard_stop": self._state["hard_stop_reason"] is not None,
            "stop_reason": self._state["hard_stop_reason"]
            or self._state["stop_reason"]
            or decision["reason"],
            "replay_only": self.adapter.replay_only,
            "source_observational_release": self._state["quality"].get(
                "observational_release", False
            )
            if self.adapter.replay_only
            else None,
            "observational_release": bool(
                not self.adapter.replay_only
                and self.status == "DONE"
                and decision["reason"] == "observed_regions_verified"
                and not self._state["hard_stop_reason"]
            ),
            "real_acceptance_pending": True,
            "truth_used_for_control": False,
        }

    def session_trace(self) -> dict:
        """Return the complete before/after audit without raw generative truth."""
        return {
            "schema": TRACE_SCHEMA,
            "summary": self.summary(),
            "checkpoints": _json_copy(self._state["checkpoints"]),
            "events": _json_copy(self._state["events"]),
            "interventions": self.interventions,
            "executions": _json_copy(self._state["executions"]),
            "pending_directives": _json_copy(self._state["queue"]),
            "scope": (
                "Saved sensor/feedback evidence replay; edited proposals are not new motion or post-edit measurements."
                if self.adapter.replay_only
                else "Offline adapter execution and signed sensor disposition; enterprise measurement remains separate."
            ),
            "truth_used_for_control": False,
        }

    def write_trace(self, path: Path | str) -> None:
        """Save the full auditable session_trace.json document."""
        _write_json(path, self.session_trace())

    def _start(self):
        if self._state["stage"] is not None:
            return
        observed = self.adapter.perceive()
        self._state["regions"] = _observed_regions(observed["regions"])
        self._state["registered_targets"] = _json_copy(
            observed["registered_targets"]
        )
        self._state["protected_points"] = _json_copy(
            observed.get("protected_points", [])
        )
        self._set_paths(observed["paths"])
        self._latch(_hard_stop(observed))
        self._capture(GrindingStage.POST_PERCEPTION)

    def _set_paths(self, paths):
        from .workstation_planning import _path_rejection
        from .workstation_tooling import fixed_tool

        observed = _observed_paths(paths)
        ids = [path["segment_id"] for path in observed]
        if len(_ids(ids, "path_ids")) != len(ids):
            raise ValueError("Candidate path identities must be unique")
        for path in observed:
            _positive_number(path["recipe"]["feed_mm_s"], "feed_mm_s")
            allowance = path["material_allowance_mm"]
            if (
                type(allowance) not in (int, float)
                or not 0 <= allowance <= self._max_allowance_mm
            ):
                raise SafetyVeto(
                    "contact_allowance_gate",
                    "Contact allowance crosses nominal CAD or remaining-height limits",
                )
            rejection, _ = _path_rejection(
                path, None, fixed_tool(path["recipe"]["tool"])
            )
            if rejection:
                raise SafetyVeto("path_permission_gate", rejection)
        self._state["base_paths"] = observed
        self._state["next_path_id"] = max(
            self._state["next_path_id"], max(ids, default=0) + 1
        )
        self._state["planning_dirty"] = True

    def _filtered_paths(self):
        from .workstation_tooling import fixed_tool

        masked = self._state["masked_regions"]
        excluded = self._state["excluded_paths"]
        paths = [
            copy.deepcopy(path)
            for path in self._state["base_paths"]
            if path["detection_id"] not in masked
            and not set(
                path.get("regrind", {}).get("registered_target_ids", [])
            )
            & set(masked)
            and path["segment_id"] not in excluded
            and path.get("regrind", {}).get("source_segment_id") not in excluded
            and path.get("filter", {}).get("source_segment_id") not in excluded
        ]
        protected = self._state["protected_points"] + [
            point
            for target in self._state["registered_targets"]
            if target["id"] in masked or target["kind"] == "review"
            for point in target["points"]
        ]
        result = []
        for path in paths:
            if protected:
                import numpy as np

                from .workstation_inspection import exclude_review_footprint

                permitted, _ = exclude_review_footprint(
                    [path],
                    np.asarray(protected),
                    fixed_tool(path["recipe"]["tool"]).footprint_radius,
                )
                if not permitted:
                    continue
            path["recipe"]["feed_mm_s"] *= self._state["feed_scale"]
            result.append(path)
        return result

    def _decision(self, paths):
        stage = self._state["stage"]
        if self._state["hard_stop_reason"]:
            reason = self._state["hard_stop_reason"]
        elif stage in {
            GrindingStage.POST_REINSPECTION,
            GrindingStage.PRE_REGRIND,
        }:
            from .workstation_run import continuation_reason

            quality = self._state["quality"]
            reason = continuation_reason(
                self.adapter.mode,
                True,
                quality,
                paths,
                self.round_index,
                self._state["regrind_budget"] + 1,
                quality.get("observational_release", False),
            )
        else:
            reason = (
                "fresh_planning_required"
                if self._state["planning_dirty"]
                else "ready_for_execution"
            )
        gate = self._state["planning"].get("preflight", {}).get("passed", False)
        return {
            "reason": reason,
            "can_execute": bool(
                gate
                and stage == GrindingStage.PRE_EXECUTION
                and self.status == "READY"
                and not self.adapter.replay_only
                and not self._state["planning_dirty"]
                and not self._state["hard_stop_reason"]
            ),
            "can_replay": bool(
                gate
                and stage == GrindingStage.PRE_EXECUTION
                and self.status == "READY"
                and self.adapter.replay_only
                and not self._state["planning_dirty"]
                and not self._state["hard_stop_reason"]
            ),
            "can_regrind": reason == "followup_planning",
            "regrind_budget": self._state["regrind_budget"],
            "regrinds_used": self.round_index,
            "planning_gate_passed": gate,
            "quality": {
                key: self._state["quality"][key]
                for key in QUALITY_FIELDS
                if key in self._state["quality"]
            },
            "local_regrind": self._state["regrind_report"],
            "replay_only": self.adapter.replay_only,
            "truth_used_for_control": False,
        }

    def _enforce(self, directive):
        if set(directive.payload) != ACTION_KEYS[directive.action]:
            raise SafetyVeto(
                "unsupported_override",
                "The action payload cannot override safety, evidence or contact parameters",
            )
        if self.status in {"DONE", "ABORTED"}:
            raise SafetyVeto(
                "terminal_session",
                "A terminal grinding session cannot be restarted",
            )
        if (
            directive.action in {"pause", "abort_safe"}
            and not self._state["safe_boundary"]
        ):
            raise SafetyVeto(
                "safe_retract_required",
                "The adapter has not established a safe retracted boundary",
            )
        if self._state["hard_stop_reason"] and directive.action in {
            "continue",
            "adjust_feed_scale",
            "set_regrind_budget",
        }:
            raise SafetyVeto(
                "safety_hard_stop", self._state["hard_stop_reason"]
            )

    def _apply(self, directive):
        action = directive.action
        payload = directive.payload
        if action in {"mask_regions", "exclude_paths"}:
            key = "region_ids" if action == "mask_regions" else "path_ids"
            identities = _ids(payload[key], key)
            known = (
                {row["id"] for row in self._state["regions"]}
                if action == "mask_regions"
                else {
                    path["segment_id"]
                    for path in self._state["base_paths"]
                    + self._state["permitted_paths"]
                }
            )
            if set(identities) - known:
                raise ValueError(
                    f"Unknown {key}: {sorted(set(identities) - known)}"
                )
            state_key = (
                "masked_regions"
                if action == "mask_regions"
                else "excluded_paths"
            )
            before = list(self._state[state_key])
            self._state[state_key] = sorted(set(before) | set(identities))
            self._state["planning_dirty"] |= before != self._state[state_key]
            for identity in identities:
                self._record_intervention(
                    directive,
                    "mask_false_positive"
                    if action == "mask_regions"
                    else "skip_segment",
                    identity,
                    {"masked": identity in before}
                    if action == "mask_regions"
                    else {"status": "pending"},
                    {"masked": True}
                    if action == "mask_regions"
                    else {"status": "skipped"},
                )
            return
        if action == "adjust_feed_scale":
            requested = _positive_number(payload["feed_scale"], "feed_scale")
            applied = min(
                self._feed_bounds[1], max(self._feed_bounds[0], requested)
            )
            before = self._state["feed_scale"]
            path_ids = [path["segment_id"] for path in self._filtered_paths()]
            self._state["feed_scale"] = applied
            self._state["planning_dirty"] |= before != applied
            for identity in path_ids or [None]:
                self._record_intervention(
                    directive,
                    "set_segment_params",
                    identity,
                    {"feed_scale": before},
                    {"feed_scale": applied},
                    details={
                        "requested_feed_scale": requested,
                        "clamped": applied != requested,
                    },
                )
            return
        if action == "set_regrind_budget":
            requested = _integer(payload["regrind_budget"], "regrind_budget")
            before = self._state["regrind_budget"]
            self._state["regrind_budget"] = min(requested, self._max_regrinds)
            self._record_intervention(
                directive,
                action,
                None,
                {"regrind_budget": before},
                {"regrind_budget": self._state["regrind_budget"]},
                details={
                    "requested_regrind_budget": requested,
                    "clamped": requested > self._max_regrinds,
                },
            )
            return
        if action == "request_extra_views":
            count = _integer(payload["count"], "count", minimum=1)
            observed = self.adapter.request_extra_views(
                stage=GrindingStage(self._state["stage"]),
                round_index=self.round_index,
                count=count,
            )
            self._record_intervention(
                directive,
                action,
                None,
                {},
                {"requested_views": count, "available": observed is not None},
            )
            if observed is None:
                self._state.update(
                    status="STOPPED_SAFETY"
                    if self._state["hard_stop_reason"]
                    else "PAUSED",
                    stop_reason="extra_views_unavailable",
                )
                return
            if self._state["stage"] in {
                GrindingStage.POST_REINSPECTION,
                GrindingStage.PRE_REGRIND,
            }:
                self._observe_quality(observed)
                self._capture(GrindingStage.POST_REINSPECTION)
            else:
                self._state["regions"] = _observed_regions(observed["regions"])
                self._state["registered_targets"] = _json_copy(
                    observed["registered_targets"]
                )
                self._state["protected_points"] = _json_copy(
                    observed.get("protected_points", [])
                )
                self._set_paths(observed["paths"])
                self._latch(_hard_stop(observed))
                self._capture(GrindingStage.POST_PERCEPTION)
            return
        before = {"status": self.status}
        if action == "pause":
            self._state.update(status="PAUSED", stop_reason="operator_pause")
        elif action == "abort_safe":
            self._state.update(
                status="ABORTED", stop_reason="operator_abort_safe"
            )
        elif action == "continue" and self.status == "PAUSED":
            self._state.update(status="READY", stop_reason=None)
        self._record_intervention(
            directive, action, None, before, {"status": self.status}
        )

    def _plan(self):
        paths = self._filtered_paths()
        if not paths:
            self._state.update(
                status="DONE", stop_reason="no_paths_after_intervention"
            )
            self._event("no_executable_paths")
            return
        planning = _json_copy(
            self.adapter.plan(paths, round_index=self.round_index)
        )
        if not self.adapter.replay_only and planning.get("replay_only", False):
            raise SafetyVeto(
                "replay_plan_not_executable",
                "Replay evidence cannot authorize new motion",
            )
        if not planning["preflight"].get("passed", False):
            self._state.update(
                status="PAUSED", stop_reason="planning_safety_gate"
            )
            self._state["planning"] = planning
            self._event("planning_safety_gate")
            return
        admitted = _observed_paths(planning["paths"])
        if not admitted:
            raise SafetyVeto(
                "no_admitted_path", "A passed gate must include admitted paths"
            )
        requested_by_id = {path["segment_id"]: path for path in paths}
        if any(
            path != requested_by_id.get(path["segment_id"]) for path in admitted
        ):
            raise SafetyVeto(
                "planning_path_mismatch",
                "Admission must bind the exact current path and recipe",
            )
        if not self.adapter.replay_only and not planning["preflight"].get(
            "fresh_motion_plan", False
        ):
            raise SafetyVeto(
                "fresh_motion_plan_required",
                "An offline execution requires a newly checked motion plan",
            )
        self._state["planning"] = planning
        self._state["planning_dirty"] = False
        permitted_ids = {
            path["segment_id"] for path in self._state["permitted_paths"]
        }
        admitted_ids = {path["segment_id"] for path in admitted}
        self._state["base_paths"] = [
            path
            for path in self._state["base_paths"]
            if path["segment_id"] in admitted_ids
        ]
        self._state["permitted_paths"].extend(
            copy.deepcopy(path)
            for path in self._state["base_paths"]
            if path["segment_id"] in admitted_ids
            and path["segment_id"] not in permitted_ids
            and "regrind" not in path
        )
        self._event(
            "planning_completed",
            path_ids=sorted(admitted_ids),
            replay_only=self.adapter.replay_only,
        )

    def _observe_quality(self, observed):
        quality = _quality_report(observed)
        self._state["quality"] = quality
        self._state["regions"] = quality["regions"]
        self._latch(_hard_stop(quality))
        if self._state["hard_stop_reason"]:
            self._set_paths([])
            return
        from .workstation_regrind import additional_regrind_paths, regrind_paths

        permitted = [
            path
            for path in self._state["permitted_paths"]
            if path["segment_id"] not in self._state["excluded_paths"]
            and path["detection_id"] not in self._state["masked_regions"]
        ]
        paths, report = regrind_paths(
            self._state["registered_targets"],
            quality,
            permitted,
            start_segment_id=self._state["next_path_id"],
            rate_mm_s=self.adapter.removal_rate_mm_s,
        )
        unique = additional_regrind_paths([], paths)
        self._state["regrind_report"] = {
            **report,
            "duplicate_trace_count": len(paths) - len(unique),
        }
        self._set_paths(unique)

    def _advance(self):
        stage = GrindingStage(self._state["stage"])
        if self._state["hard_stop_reason"]:
            raise SafetyVeto(
                "safety_hard_stop", self._state["hard_stop_reason"]
            )
        if stage == GrindingStage.POST_PERCEPTION:
            self._plan()
            self._capture(GrindingStage.POST_PLANNING)
            return
        if stage == GrindingStage.POST_PLANNING:
            if self._state["planning_dirty"]:
                self._plan()
            if self.status == "READY":
                self._capture(GrindingStage.PRE_EXECUTION)
            return
        if stage == GrindingStage.PRE_EXECUTION:
            if self._state["planning_dirty"]:
                self._plan()
            if self.status != "READY":
                return
            execution = _json_copy(
                self.adapter.execute(
                    copy.deepcopy(self._state["planning"]),
                    round_index=self.round_index,
                )
            )
            self._state["executions"].append(execution)
            self._state["safe_boundary"] = (
                self.adapter.replay_only
                or execution.get("safe_retracted", False) is True
            )
            self._latch(_hard_stop({}, execution))
            if not self._state["safe_boundary"]:
                self._latch("unsafe_execution_boundary_requires_retract")
            self._event("execution_evidence", execution=execution)
            if self._state["hard_stop_reason"]:
                return
            self._observe_quality(
                self.adapter.reinspect(round_index=self.round_index)
            )
            self._capture(GrindingStage.POST_REINSPECTION)
            return
        if stage == GrindingStage.POST_REINSPECTION:
            decision = self._decision(self._filtered_paths())
            if decision["can_regrind"]:
                self._capture(GrindingStage.PRE_REGRIND)
            else:
                self._state.update(
                    status="DONE", stop_reason=decision["reason"]
                )
                self._event("workflow_completed", reason=decision["reason"])
            return
        decision = self._decision(self._filtered_paths())
        if not decision["can_regrind"]:
            self._state.update(status="DONE", stop_reason=decision["reason"])
            self._event("workflow_completed", reason=decision["reason"])
            return
        self._state["round_index"] += 1
        self._plan()
        self._capture(GrindingStage.POST_PLANNING)

    def _latch(self, reason):
        if reason and not self._state["hard_stop_reason"]:
            self._state.update(
                hard_stop_reason=reason,
                stop_reason=reason,
                status="STOPPED_SAFETY",
            )
            self._event("safety_hard_stop", reason=reason)

    def _capture(self, stage):
        self._state["stage"] = stage.value
        self._state["policy_applied"] = False
        self._state["checkpoints"].append(self.checkpoint.to_dict())
        self._event(
            "checkpoint_created",
            stage=stage.value,
            round_index=self.round_index,
        )

    def _event(self, action, **details):
        self._state["events"].append(
            {
                "sequence": len(self._state["events"]) + 1,
                "timestamp": self._clock(),
                "action": action,
                **_json_copy(details),
            }
        )

    def _record_intervention(
        self,
        directive,
        action,
        target_id,
        before,
        after,
        *,
        status="applied",
        details=None,
    ):
        self._state["interventions"].append(
            {
                "timestamp": self._clock(),
                "stage": self._state["stage"],
                "round_index": self.round_index,
                "action": action,
                "directive_action": directive.action,
                "target_id": target_id,
                "actor": directive.actor,
                "reason": directive.reason,
                "before": _json_copy(before),
                "after": _json_copy(after),
                "diff": {
                    key: {"before": before.get(key), "after": after.get(key)}
                    for key in before.keys() | after.keys()
                    if before.get(key) != after.get(key)
                },
                "status": status,
                "payload": directive.to_dict()["payload"],
                "safety_hard_stop": self._state["hard_stop_reason"] is not None,
                "stop_reason": self._state["hard_stop_reason"],
                **(details or {}),
                "truth_used_for_control": False,
            }
        )


def run_saved_demo(result_path: Path | str, output: Path | str) -> dict:
    """Replay a real saved run plus a pauseable scripted operator scenario."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("The demonstration requires an empty output directory")
    baseline = ProgrammableGrindingSession(
        SavedRunReplayAdapter.from_run(result_path)
    )
    baseline.run()
    baseline.write_trace(output / "baseline_session_trace.json")
    session = ProgrammableGrindingSession(
        SavedRunReplayAdapter.from_run(result_path)
    )
    initial = session.checkpoint
    session.queue_directive(
        InterventionDirective(
            "exclude_paths",
            {"path_ids": [initial.candidate_path_ids[-1]]},
            "operator",
            "Withhold one candidate for local review",
        ),
        stage=GrindingStage.POST_PLANNING,
        round_index=0,
    )
    session.queue_directive(
        InterventionDirective(
            "adjust_feed_scale",
            {"feed_scale": 0.8},
            "script",
            "Propose a slower recipe within the process envelope",
        ),
        stage=GrindingStage.PRE_EXECUTION,
        round_index=0,
    )
    session.queue_directive(
        InterventionDirective(
            "set_regrind_budget",
            {"regrind_budget": 20},
            "operator",
            "Request bounded follow-up; configured cap remains authoritative",
        ),
        stage=GrindingStage.POST_REINSPECTION,
        round_index=0,
    )
    session.queue_directive(
        InterventionDirective(
            "pause",
            actor="operator",
            reason="Inspect the local follow-up proposal before continuing",
        ),
        stage=GrindingStage.PRE_REGRIND,
        round_index=0,
    )
    paused = session.run()
    if paused["status"] != "PAUSED":
        raise RuntimeError(
            f"The saved demonstration did not reach its pause: {paused}"
        )
    session.save_checkpoint(output / "checkpoint_paused.json")
    resumed = ProgrammableGrindingSession.load_checkpoint(
        output / "checkpoint_paused.json"
    )
    resumed.resume(
        actor="operator",
        reason="Resume the saved evidence replay, not hardware",
    )
    result = resumed.run()
    resumed.write_trace(output / "session_trace.json")
    (output / "interventions.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            for row in resumed.interventions
        ),
        encoding="utf-8",
    )
    from .dataset_pipeline import interventions_to_hard_examples

    hard_examples = interventions_to_hard_examples(resumed.interventions)
    _write_json(output / "hard_examples.json", hard_examples)
    summary = {
        "baseline": baseline.summary(),
        "paused": paused,
        "resumed": result,
        "hard_example_requests": len(hard_examples["requests"]),
        "source_result": str(Path(result_path).resolve()),
        "output": str(output),
        "new_motion_executed": False,
        "new_material_removal_measured": False,
        "real_acceptance_pending": True,
    }
    _write_json(output / "demo_summary.json", summary)
    return summary


def main(argv=None):
    """Run a read-only saved-result demonstration via ``python -m``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run_saved_demo(args.result, args.output),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
