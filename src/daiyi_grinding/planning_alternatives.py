"""Bounded approach alternatives and explicit workstation rejection evidence.

CAD permission, normals, stock targets and collision margins are unchanged.
A tilt candidate is a small yaw/pitch group, not a promise that both members
are reachable. The real planner selects a member and checks its complete
approach, trace, retreat, return and C1 servo curve.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from numbers import Integral, Real

import numpy as np

from .workpiece_packages import ContactSearch, load_workpiece_package
from .workstation_tooling import FixedTool, fixed_tool

_ROLLS_DEG = (45, 135, 225, 315, 90, 180, 270, 0, 60, 30, 120, 150)
_PAIR = re.compile(r"([\w]+)\s*/\s*([\w]+)(?:[（(]\s*([\d.]+)\s*mm[）)])?")
_SCOPE = (
    "finite candidate envelope; signed distances at sampled nominal process "
    "poses, not continuous-curve or global geometric feasibility certification"
)


@dataclass(frozen=True)
class ApproachCandidate:
    """A front-face contact and bounded orientation group covering all sources.

    Roll is about the tool normal and does not change the CAD surface normal.
    A nonzero tilt tries pitch about world Y and yaw about world Z separately;
    it never combines their angles or turns onto an undeclared rear belt face.
    """

    candidate_id: int
    paths: tuple[dict, ...]
    roll_deg: float
    tilt_deg: float
    contact_world_m: tuple[float, float, float]
    belt_side: int
    edge_margin_mm: float

    def rotations(
        self, surface_normal: Sequence[float]
    ) -> tuple[np.ndarray, ...]:
        """Return the same normal alignment and yaw/pitch members as the planner."""
        from scipy.spatial.transform import Rotation

        normal = np.array([-1.0, 0.0, 0.0])
        alignment = Rotation.align_vectors([normal], [surface_normal])[0]
        roll = Rotation.from_rotvec(normal * np.deg2rad(self.roll_deg))
        return tuple(
            (
                Rotation.from_rotvec(
                    np.eye(3)[axis] * np.deg2rad(self.tilt_deg)
                )
                * roll
                * alignment
            ).as_matrix()
            for axis in ((1,) if self.tilt_deg == 0 else (1, 2))
        )


@dataclass(frozen=True)
class PlanningRejectionSummary:
    """Typed failure counts and signed clamp clearance, with an explicit scope."""

    counts: dict[str, int] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    collision_pairs: tuple[tuple[str, str], ...] = ()
    minimum_clamp_belt_clearance_mm: float | None = None
    best_candidate_minimum_clamp_belt_clearance_mm: float | None = None
    required_clearance_mm: float = 0.0
    budget_exhausted: bool = False
    fixture_change_required_within_candidates: bool = False
    scope: str = (
        "planner-reported rejections; no geometric feasibility certificate"
    )

    @classmethod
    def from_checks(cls, checks: dict) -> PlanningRejectionSummary:
        """Classify current planner rows without re-reporting repaired attempts."""
        rows = _rejection_rows(checks.get("rows", []))
        reasons = list(
            dict.fromkeys(
                reason
                for row in rows
                for reason in (
                    row.get("reason", ""),
                    *row.get("failure_reasons", []),
                )
                if reason
            )
        )
        if (
            not reasons
            and not checks.get("passed", False)
            and checks.get("reason")
        ):
            reasons.append(checks["reason"])
        counts = Counter()
        pairs = []
        reported_clearances = []
        for reason in reasons:
            counts.update(_reason_categories(reason))
            for first, second, penetration in _PAIR.findall(reason):
                pairs.append((first, second))
                if penetration and _clamp_belt_pair(first, second):
                    reported_clearances.append(-float(penetration))
        for contact in checks.get("unexpected_contacts", []):
            pair = tuple(contact["pair"])
            pairs.append(pair)
            reason = f"{contact.get('phase', '')} collision: {' / '.join(pair)}"
            reasons.append(reason)
            counts.update(_reason_categories(reason))
            if _clamp_belt_pair(*pair):
                reported_clearances.append(-float(contact["penetration_mm"]))
        exhausted = bool(
            checks.get("budget_exhausted")
            or counts["budget_exhausted"]
            or any(row.get("budget", {}).get("exhausted") for row in rows)
        )
        if exhausted and not counts["budget_exhausted"]:
            counts["budget_exhausted"] = 1
        geometry = checks.get("planning_alternative_geometry", {})
        minimum = geometry.get("minimum_clamp_belt_clearance_mm")
        return cls(
            counts=dict(counts),
            reasons=tuple(dict.fromkeys(reasons)),
            collision_pairs=tuple(dict.fromkeys(pairs)),
            minimum_clamp_belt_clearance_mm=(
                float(minimum)
                if minimum is not None
                else min(reported_clearances, default=None)
            ),
            best_candidate_minimum_clamp_belt_clearance_mm=geometry.get(
                "best_orientation_minimum_clamp_belt_clearance_mm"
            ),
            budget_exhausted=exhausted,
            scope=geometry.get("scope", cls.scope),
        )


@dataclass(frozen=True)
class PlanningAlternativesResult:
    """Best complete plan, or candidate-scoped rejection/incomplete-search evidence."""

    status: str
    plan: tuple[list[dict], list[dict], dict] | None
    best_candidate: ApproachCandidate | None
    attempts: tuple[dict, ...]
    rejection: PlanningRejectionSummary
    search_complete: bool


def generate_approach_candidates(
    paths: Sequence[dict],
    *,
    tool: FixedTool | None = None,
    search: ContactSearch | None = None,
    roll_candidates_deg: Sequence[float] = _ROLLS_DEG,
    tilt_candidates_deg: Sequence[float] | None = None,
    split_points_mm: dict[int, Sequence[float]] | None = None,
    include_midpoint_splits: bool = False,
    max_candidates: int = 512,
) -> tuple[ApproachCandidate, ...]:
    """Propose bounded yaw/pitch groups, reflected contacts and complete splits.

    Args:
        paths: Contiguous permitted CAD polylines in metres, with unit normals.
        tool: The configured front-face belt; its geometry is never changed.
        search: The workpiece's declared tilt, edge and subdivision envelope.
        roll_candidates_deg: Normal-axis rolls, in deterministic search order.
        tilt_candidates_deg: Tilts within the declared maximum, or its defaults.
        split_points_mm: Local arc-length cuts keyed by input segment ID. All
            source intervals remain covered with the existing overlap.
        include_midpoint_splits: Also propose independent half-path approaches
            when both halves can meet the declared minimum segment length.
        max_candidates: Positive finite bound on the returned candidate list.

    Raises:
        ValueError: Invalid geometry, an unsupported gap, an exhausted front
            face, or an orientation/subdivision outside the declared envelope.
    """
    tool = tool or fixed_tool()
    search = search or load_workpiece_package().contact_search
    _positive_limit(max_candidates, "max_candidates")
    _front_face(tool, search.edge_margin_mm)
    if not paths:
        raise ValueError("At least one permitted CAD path is required")
    tilts = tuple(
        search.tilt_candidates_deg
        if tilt_candidates_deg is None
        else tilt_candidates_deg
    )
    maximum_tilt = max(abs(value) for value in search.tilt_candidates_deg)
    if not tilts or any(
        not _finite_real(value) or abs(value) > maximum_tilt for value in tilts
    ):
        raise ValueError("tilt exceeds the declared process-normal envelope")
    if not roll_candidates_deg or any(
        not _finite_real(value) for value in roll_candidates_deg
    ):
        raise ValueError("roll candidates must be finite angles")
    sources = _source_paths(paths)
    variants = [sources]
    subdivided = _split_sources(
        sources, search, split_points_mm or {}, include_midpoint_splits
    )
    if subdivided is not None:
        variants.append(subdivided)
    width, length = tool.half_size[1:] - search.edge_margin_mm / 1000
    # Reflections use the same front face and the same five-millimetre margin.
    offsets = (
        (0, 0),
        (width, length),
        (-width, length),
        (width, -length),
        (-width, -length),
        (width, 0),
        (-width, 0),
        (0, length),
        (0, -length),
    )
    offsets = tuple(
        dict.fromkeys(
            (
                *offsets,
                (width, length / 2),
                (-width, length / 2),
                (width, -length / 2),
                (-width, -length / 2),
                *[
                    tuple(np.asarray(point)[1:] - tool.contact[1:])
                    for point in tool.candidates()
                    if np.all(
                        np.abs(np.asarray(point)[1:] - tool.contact[1:])
                        <= [width, length]
                    )
                ],
                *[
                    (y, z)
                    for z in np.linspace(-length, length, 5)
                    for y in np.linspace(-width, width, 5)
                ],
            )
        )
    )
    tilts = tuple(dict.fromkeys(tilts))
    rolls = tuple(
        dict.fromkeys(float(value) % 360 for value in roll_candidates_deg)
    )
    candidates = []
    # Diagonal enumeration reaches both tilt signs and several rolls before a
    # long contact-grid sweep consumes the caller's finite candidate allowance.
    for level in range(max(len(tilts), len(rolls), len(offsets))):
        for tilt_index, tilt in enumerate(tilts[: level + 1]):
            for roll_index, roll in enumerate(rolls[: level + 1]):
                for contact_index, (y, z) in enumerate(offsets[: level + 1]):
                    if max(tilt_index, roll_index, contact_index) != level:
                        continue
                    for variant in variants:
                        candidates.append(
                            ApproachCandidate(
                                candidate_id=len(candidates),
                                paths=variant,
                                roll_deg=roll,
                                tilt_deg=float(tilt),
                                contact_world_m=tuple(
                                    float(value)
                                    for value in tool.contact + [0, y, z]
                                ),
                                belt_side=int(np.sign(y)),
                                edge_margin_mm=search.edge_margin_mm,
                            )
                        )
                        if len(candidates) == max_candidates:
                            return tuple(candidates)
    return tuple(candidates)


def evaluate_planning_alternatives(
    paths: Sequence[dict],
    planner_fn: Callable[
        [list[dict], ApproachCandidate], tuple[list[dict], list[dict], dict]
    ],
    *,
    candidates: Sequence[ApproachCandidate] | None = None,
    minimum_clamp_clearance_mm: float = 0.0,
    max_attempts: int | None = None,
) -> PlanningAlternativesResult:
    """Choose complete collision-free coverage, never a silently relaxed plan.

    The injected planner receives a fresh path list and the candidate group.
    It must return the workstation's commands/admitted/checks tuple. Existing
    multi-grasp operation dictionaries are retained, never flattened.
    Ranking is lexicographic: measured minimum clamp clearance, then shorter
    duration, then fewer independent pieces and smaller process-normal tilt.
    An exhausted budget or untried candidate makes a rejection incomplete,
    not a geometric infeasibility certificate. No model is created here.

    Raises:
        ValueError: A limit or candidate list is invalid, a reported clearance
            is nonfinite, or a positive extra gate lacks a measured clearance.
    """
    _nonnegative_clearance(minimum_clamp_clearance_mm)
    if max_attempts is not None:
        _positive_limit(max_attempts, "max_attempts")
    options = (
        tuple(candidates)
        if candidates is not None
        else generate_approach_candidates(paths)
    )
    if not options:
        raise ValueError("At least one approach candidate is required")
    attempts, summaries = [], []
    best_plan, best_candidate, best_score = None, None, None
    for candidate in options[:max_attempts]:
        plan = planner_fn(copy.deepcopy(list(candidate.paths)), candidate)
        commands, admitted, checks = plan
        checks = copy.deepcopy(checks)
        geometry = checks.get("planning_alternative_geometry", {})
        clearance = geometry.get("minimum_clamp_belt_clearance_mm")
        if (clearance is None and minimum_clamp_clearance_mm > 0) or (
            clearance is not None and not _finite_real(clearance)
        ):
            raise ValueError(
                "Planner must report finite clamp clearance for the requested gate"
            )
        complete = bool(
            commands
            and admitted
            and checks.get("passed")
            and checks.get("all_paths_passed")
            and checks.get("source_coverage_ratio", 0.0) >= 1 - 1e-8
            and not checks.get("contact_count", 0)
            and not checks.get("unexpected_contacts", [])
        )
        if checks.get("passed") and not complete:
            checks["reason"] = (
                "segment continuity/coverage or collision admission incomplete"
            )
            checks["rows"] = [
                *checks.get("rows", []),
                {"status": "blocked", "reason": checks["reason"]},
            ]
        if clearance is not None and clearance < minimum_clamp_clearance_mm:
            complete = False
            if not any(
                "belt_clamp_clearance"
                in _reason_categories(row.get("reason", ""))
                for row in _rejection_rows(checks.get("rows", []))
            ):
                checks["rows"] = [
                    *checks.get("rows", []),
                    {
                        "status": "blocked",
                        "reason": (
                            "belt/clamp clearance violation: "
                            f"{clearance:.9f} mm < {minimum_clamp_clearance_mm:.9f} mm"
                        ),
                    },
                ]
        if not complete:
            checks["passed"] = False
            checks["all_paths_passed"] = False
        summary = PlanningRejectionSummary.from_checks(checks)
        summaries.append(summary)
        attempts.append(
            {
                "candidate_id": candidate.candidate_id,
                "roll_deg": candidate.roll_deg,
                "tilt_deg": candidate.tilt_deg,
                "contact_world_m": list(candidate.contact_world_m),
                "belt_side": candidate.belt_side,
                "path_count": len(candidate.paths),
                "source_intervals_mm": [
                    {
                        "source_segment_id": path["source_segment_id"],
                        "interval_mm": path["source_interval_mm"],
                    }
                    for path in candidate.paths
                ],
                "passed": complete,
                "source_coverage_ratio": checks.get(
                    "source_coverage_ratio", 0.0
                ),
                "minimum_clamp_belt_clearance_mm": clearance,
                "best_orientation_minimum_clamp_belt_clearance_mm": geometry.get(
                    "best_orientation_minimum_clamp_belt_clearance_mm"
                ),
                "checks": checks,
            }
        )
        if complete:
            score = (
                float(clearance) if clearance is not None else -math.inf,
                -float(checks.get("duration_s", math.inf)),
                -len(admitted),
                -abs(candidate.tilt_deg),
            )
            if best_score is None or score > best_score:
                best_plan, best_candidate, best_score = (
                    (commands, admitted, checks),
                    candidate,
                    score,
                )
    search_complete = len(attempts) == len(options) and not any(
        summary.budget_exhausted for summary in summaries
    )
    minimums = [
        summary.minimum_clamp_belt_clearance_mm
        for summary in summaries
        if summary.minimum_clamp_belt_clearance_mm is not None
    ]
    best_minimums = [
        summary.best_candidate_minimum_clamp_belt_clearance_mm
        for summary in summaries
        if summary.best_candidate_minimum_clamp_belt_clearance_mm is not None
    ]
    rejection = PlanningRejectionSummary(
        counts=dict(
            sum((Counter(summary.counts) for summary in summaries), Counter())
        ),
        reasons=tuple(
            dict.fromkeys(
                reason for summary in summaries for reason in summary.reasons
            )
        ),
        collision_pairs=tuple(
            dict.fromkeys(
                pair
                for summary in summaries
                for pair in summary.collision_pairs
            )
        ),
        minimum_clamp_belt_clearance_mm=min(minimums, default=None),
        best_candidate_minimum_clamp_belt_clearance_mm=max(
            best_minimums, default=None
        ),
        required_clearance_mm=minimum_clamp_clearance_mm,
        budget_exhausted=any(summary.budget_exhausted for summary in summaries),
        fixture_change_required_within_candidates=bool(
            best_plan is None
            and search_complete
            and len(best_minimums) == len(attempts)
            and all(
                value < minimum_clamp_clearance_mm for value in best_minimums
            )
        ),
        scope=_SCOPE,
    )
    return PlanningAlternativesResult(
        status="passed"
        if best_plan is not None
        else "infeasible"
        if search_complete
        else "incomplete",
        plan=best_plan,
        best_candidate=best_candidate,
        attempts=tuple(attempts),
        rejection=rejection,
        search_complete=search_complete,
    )


class WorkstationAlternativePlanner:
    """Evaluate candidate groups with the unchanged collision and IK planner.

    Use a fresh adapter for one fixed workstation planning state. This adapter
    does not create a model, move a physical robot or execute a regrasp. The
    caller must hold the repository MuJoCo lock throughout construction and
    evaluation, with the prescribed EGL/single-thread CPU environment.
    """

    def __init__(self, sim, mode="removal", *, minimum_clamp_clearance_mm=0.0):
        _nonnegative_clearance(minimum_clamp_clearance_mm)
        if mode not in {"removal", "clearance"}:
            raise ValueError("Only removal or clearance planning is supported")
        self.sim = sim
        self.mode = mode
        self.minimum_clamp_clearance_mm = minimum_clamp_clearance_mm
        self.staging_q = None
        self.geometry = None

    def __call__(
        self, paths: list[dict], candidate: ApproachCandidate
    ) -> tuple[list[dict], list[dict], dict]:
        from .workstation_planning import (
            Commands,
            PlanningBudget,
            build_plan,
            separate_nominal_tool,
        )

        search = self.sim.workpiece_package.contact_search
        _front_face(self.sim.tool, search.edge_margin_mm)
        contact = np.asarray(candidate.contact_world_m)
        if (
            not np.isfinite(contact).all()
            or not _finite_real(candidate.roll_deg)
            or not _finite_real(candidate.tilt_deg)
            or not _finite_real(candidate.edge_margin_mm)
            or abs(candidate.tilt_deg)
            > max(abs(value) for value in search.tilt_candidates_deg)
            or candidate.edge_margin_mm < search.edge_margin_mm
            or not np.isclose(
                contact[0], self.sim.tool.contact[0], atol=1e-12, rtol=0
            )
            or np.any(
                np.abs(contact[1:] - self.sim.tool.contact[1:])
                > self.sim.tool.half_size[1:]
                - candidate.edge_margin_mm / 1000
                + 1e-12
            )
        ):
            raise ValueError(
                "Candidate exceeds the real workstation tilt/front-face margin envelope"
            )
        view = _PlanningView(self.sim, candidate)
        if self.staging_q is None:
            try:
                self.staging_q = _staging_seed(self.sim)
            except ValueError as error:
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
                                "reason": f"抓取/抬升/转场：{error}",
                            }
                            for path in paths
                        ],
                        "planning_alternative_geometry": {
                            "minimum_clamp_belt_clearance_mm": None,
                            "scope": "no process pose admitted after transfer rejection",
                        },
                    },
                )
        if self.geometry is None:
            self.geometry = _ClampBeltGeometry(self.sim)
        rows, geometry_rows = [], []
        viable = True
        for path in paths:
            members = []
            for rotation in candidate.rotations(path["normal"]):
                builder = Commands(
                    view,
                    PlanningBudget(
                        view.cell_config["planning"][
                            "max_search_seconds_per_segment"
                        ]
                    ),
                )
                builder.q = self.staging_q.copy()
                builder.attached = True
                gap = (
                    path.get(
                        "material_allowance_mm",
                        self.sim.cell_config["planning"][
                            "material_allowance_mm"
                        ],
                    )
                    / 1000
                    if self.mode == "removal"
                    else 0.003
                )
                reason = None
                try:
                    gap, _ = separate_nominal_tool(
                        builder,
                        np.asarray(path["nominal_xyz"]),
                        rotation,
                        contact,
                        gap,
                        self.mode,
                    )
                except ValueError as error:
                    reason = str(error)
                measured = self.geometry.measure(
                    path["nominal_xyz"], rotation, contact, gap
                )
                clearance = measured["minimum_clamp_belt_clearance_mm"]
                if (
                    reason is None
                    and clearance is not None
                    and clearance < self.minimum_clamp_clearance_mm
                ):
                    reason = _clearance_reason(
                        measured, self.minimum_clamp_clearance_mm
                    )
                members.append(
                    {
                        **measured,
                        "source_segment_id": path.get(
                            "source_segment_id", path["segment_id"]
                        ),
                        "reason": reason,
                        "budget": builder.budget.summary(),
                    }
                )
            safe = any(member["reason"] is None for member in members)
            viable = viable and safe
            rows.append(
                {
                    "segment_id": path["segment_id"],
                    "detection_id": path["detection_id"],
                    "status": "statically_safe" if safe else "blocked",
                    "reason": next(
                        (
                            member["reason"]
                            for member in reversed(members)
                            if member["reason"]
                        ),
                        "",
                    ),
                    "failure_reasons": list(
                        dict.fromkeys(
                            member["reason"]
                            for member in members
                            if member["reason"]
                        )
                    ),
                    "budget": {
                        "exhausted": any(
                            member["budget"]["exhausted"] for member in members
                        )
                    },
                }
            )
            geometry_rows.append(members)
        geometric_report = _geometry_report(geometry_rows)
        if not viable:
            return (
                [],
                [],
                {
                    "passed": False,
                    "all_paths_passed": False,
                    "source_coverage_ratio": 0.0,
                    "rows": rows,
                    "planning_alternative_geometry": geometric_report,
                },
            )
        commands, admitted, checks = build_plan(view, paths, self.mode)
        if checks["passed"] and admitted:
            selected = []
            for row in checks["rows"]:
                if row["status"] != "passed":
                    continue
                path = next(
                    path
                    for path in admitted
                    if path["segment_id"] == row["segment_id"]
                )
                measured = self.geometry.measure(
                    path["nominal_xyz"],
                    np.asarray(row["workpiece_rotation_world"]),
                    np.asarray(row["tool_contact_world"]),
                    row["material_allowance_mm"] / 1000
                    if self.mode == "removal"
                    else row["clearance_mm"] / 1000,
                )
                selected.append([measured])
            geometric_report = _geometry_report(selected)
            clearance = geometric_report["minimum_clamp_belt_clearance_mm"]
            if (
                clearance is not None
                and clearance < self.minimum_clamp_clearance_mm
            ):
                commands, admitted = [], []
                checks = {
                    **checks,
                    "passed": False,
                    "all_paths_passed": False,
                    "source_coverage_ratio": 0.0,
                    "reason": _clearance_reason(
                        geometric_report, self.minimum_clamp_clearance_mm
                    ),
                    "rows": [
                        {
                            **row,
                            "status": "blocked",
                            "reason": _clearance_reason(
                                geometric_report,
                                self.minimum_clamp_clearance_mm,
                            ),
                        }
                        for row in checks["rows"]
                    ],
                }
        return (
            commands,
            admitted,
            {**checks, "planning_alternative_geometry": geometric_report},
        )


@dataclass(frozen=True)
class _ContactTool:
    """Restrict search points, not the model's physical abrasive geometry."""

    source: FixedTool
    point: tuple[float, float, float]

    @property
    def key(self):
        return self.source.key

    @property
    def geom(self):
        return self.source.geom

    @property
    def footprint_radius(self):
        return self.source.footprint_radius

    def candidates(self):
        yield np.asarray(self.point)


class _PlanningView:
    """A candidate-specific search view over the same model, frames and limits."""

    def __init__(self, sim, candidate):
        self.sim = sim
        self.tool = _ContactTool(sim.tool, candidate.contact_world_m)
        self.workpiece_package = replace(
            sim.workpiece_package,
            contact_search=replace(
                sim.workpiece_package.contact_search,
                roll_refinement_deg=(candidate.roll_deg - 45.0,),
                tilt_candidates_deg=(candidate.tilt_deg,),
                max_split_depth=0,
            ),
        )
        self.cell_config = {
            **sim.cell_config,
            "planning": {
                **sim.cell_config["planning"],
                "max_roll_candidates": 1,
            },
        }

    def __getattr__(self, name):
        return getattr(self.sim, name)


class _ClampBeltGeometry:
    """Signed native-CCD distances for rigid gripper and fixed belt CAD pieces."""

    def __init__(self, sim):
        import mujoco

        self.sim = sim
        if sim.model.opt.disableflags & int(
            mujoco.mjtDisableBit.mjDSBL_NATIVECCD
        ):
            raise ValueError(
                "Native CCD is required for signed CAD clearance diagnostics"
            )
        names = [sim.model.geom(index).name for index in range(sim.model.ngeom)]
        active = (sim.model.geom_contype != 0) | (
            sim.model.geom_conaffinity != 0
        )
        self.clamp_ids = np.array(
            [
                index
                for index, name in enumerate(names)
                if active[index] and name.startswith("gripper_proxy_")
            ],
            dtype=int,
        )
        self.belt_ids = np.array(
            [
                index
                for index, name in enumerate(names)
                if active[index]
                and (name.startswith("belt_") or name == sim.tool.geom)
            ],
            dtype=int,
        )
        if not len(self.clamp_ids) or not len(self.belt_ids):
            raise ValueError(
                "The model must contain active gripper and belt CAD collision pieces"
            )
        gripper = sim.body_transform(sim.gripper)
        self.local_centres = (
            sim.data.geom_xpos[self.clamp_ids] - gripper[:3, 3]
        ) @ gripper[:3, :3]
        self.local_rotations = gripper[:3, :3].T @ sim.data.geom_xmat[
            self.clamp_ids
        ].reshape(-1, 3, 3)
        self.names = names

    def measure(self, points, rotation, contact, gap):
        """Measure the minimum over all supplied nominal poses, independently of IK."""
        import mujoco

        from .workstation_motion import workpiece_target

        sim = self.sim
        previous_positions = sim.scratch.geom_xpos[self.clamp_ids].copy()
        previous_rotations = sim.scratch.geom_xmat[self.clamp_ids].copy()
        minimum, pair, pose_index, checked_pairs = math.inf, None, None, 0
        radii = (
            sim.model.geom_rbound[self.clamp_ids, None]
            + sim.model.geom_rbound[self.belt_ids]
        )
        try:
            for index, point in enumerate(points):
                grip = (
                    workpiece_target(np.asarray(point), rotation, gap, contact)
                    @ sim.workpiece_from_gripper
                )
                sim.scratch.geom_xpos[self.clamp_ids] = (
                    self.local_centres @ grip[:3, :3].T + grip[:3, 3]
                )
                sim.scratch.geom_xmat[self.clamp_ids] = (
                    grip[:3, :3] @ self.local_rotations
                ).reshape(-1, 9)
                centres = np.linalg.norm(
                    sim.scratch.geom_xpos[self.clamp_ids, None, :]
                    - sim.scratch.geom_xpos[self.belt_ids],
                    axis=2,
                )
                lower = centres - radii
                # Bounding spheres eliminate pairs that cannot improve the exact
                # signed minimum; they do not replace narrow-phase distances.
                closest = float(np.max(centres + radii) + 1)
                closest_pair = None
                for flat in np.argsort(lower, axis=None):
                    first, second = np.unravel_index(flat, lower.shape)
                    if lower[first, second] >= closest:
                        break
                    clamp_id, belt_id = (
                        int(self.clamp_ids[first]),
                        int(self.belt_ids[second]),
                    )
                    distance = mujoco.mj_geomDistance(
                        sim.model, sim.scratch, clamp_id, belt_id, closest, None
                    )
                    checked_pairs += 1
                    if distance < closest:
                        closest, closest_pair = (
                            float(distance),
                            [self.names[clamp_id], self.names[belt_id]],
                        )
                if closest < minimum:
                    minimum, pair, pose_index = closest, closest_pair, index
        finally:
            sim.scratch.geom_xpos[self.clamp_ids] = previous_positions
            sim.scratch.geom_xmat[self.clamp_ids] = previous_rotations
        return {
            "minimum_clamp_belt_clearance_mm": minimum * 1000,
            "closest_pair": pair,
            "nominal_point_index": pose_index,
            "nominal_point_xyz_m": np.asarray(points[pose_index]).tolist(),
            "workpiece_rotation_world": np.asarray(rotation).tolist(),
            "tool_contact_world": np.asarray(contact).tolist(),
            "material_gap_mm": gap * 1000,
            "measured_pose_count": len(points),
            "distance_pair_checks": checked_pairs,
            "scope": "sampled_nominal_process_poses; native CCD and the existing convex CAD collision pieces",
        }


def _staging_seed(sim):
    from .workstation_motion import transform
    from .workstation_planning import Commands

    builder = Commands(sim)
    builder.leg(
        transform(sim.loading_rotation, sim.loading_position),
        "grasp",
        speed=0.07,
        event="grasp",
    )
    builder.leg(
        transform(sim.loading_rotation, sim.loading_position + [0, 0, 0.38]),
        "lift",
        speed=0.08,
    )
    builder.leg(
        transform(sim.loading_rotation, [-0.6, -0.65, 1.10]), "transfer"
    )
    return builder.q.copy()


def _geometry_report(path_members):
    members = [member for group in path_members for member in group]
    if not members:
        return {
            "minimum_clamp_belt_clearance_mm": None,
            "scope": "no measured process poses",
        }
    closest = min(
        members, key=lambda member: member["minimum_clamp_belt_clearance_mm"]
    )
    best_per_source = [
        max(group, key=lambda member: member["minimum_clamp_belt_clearance_mm"])
        for group in path_members
    ]
    limiting_best = min(
        best_per_source,
        key=lambda member: member["minimum_clamp_belt_clearance_mm"],
    )
    return {
        **{
            key: value
            for key, value in closest.items()
            if key not in {"reason", "budget"}
        },
        "best_orientation_minimum_clamp_belt_clearance_mm": limiting_best[
            "minimum_clamp_belt_clearance_mm"
        ],
        "best_orientation_witness": {
            key: value
            for key, value in limiting_best.items()
            if key not in {"reason", "budget"}
        },
        "measured_pose_count": sum(
            member["measured_pose_count"] for member in members
        ),
        "distance_pair_checks": sum(
            member["distance_pair_checks"] for member in members
        ),
    }


def _clearance_reason(geometry, required):
    return (
        "belt/clamp clearance violation: "
        + " / ".join(geometry["closest_pair"])
        + f" ({geometry['minimum_clamp_belt_clearance_mm']:.9f} mm < {required:.9f} mm)"
    )


def _source_paths(paths):
    ids = [path["segment_id"] for path in paths]
    if len(ids) != len(set(ids)):
        raise ValueError("Input segment IDs must be unique")
    result = []
    for path in paths:
        points = np.asarray(path["nominal_xyz"], dtype=float)
        normal = np.asarray(path["normal"], dtype=float)
        if (
            points.ndim != 2
            or points.shape[1] != 3
            or len(points) < 2
            or not np.isfinite(points).all()
        ):
            raise ValueError(
                "CAD paths require at least two finite metric points"
            )
        if (
            normal.shape != (3,)
            or not np.isfinite(normal).all()
            or not np.isclose(np.linalg.norm(normal), 1, atol=1e-3)
        ):
            raise ValueError("CAD paths require a finite unit surface normal")
        steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
        if np.any(steps > 0.005 + 1e-12):
            raise ValueError(
                "CAD permission gap cannot be interpolated or subdivided"
            )
        length = float(steps.sum() * 1000)
        if length <= 0:
            raise ValueError("CAD paths require positive nominal length")
        row = copy.deepcopy(path)
        row.setdefault(
            "source_segment_id", path.get("split_from", path["segment_id"])
        )
        row.setdefault("source_length_mm", length)
        row.setdefault("source_interval_mm", [0.0, length])
        result.append(row)
    return tuple(result)


def _split_sources(paths, search, requested_cuts, midpoint_splits):
    unknown = set(requested_cuts) - {path["segment_id"] for path in paths}
    if unknown:
        raise ValueError("Split points refer to an unknown input segment")
    result, changed = [], False
    next_id = max(path["segment_id"] for path in paths) + 1
    for path in paths:
        length = float(
            np.linalg.norm(np.diff(path["nominal_xyz"], axis=0), axis=1).sum()
            * 1000
        )
        cuts = tuple(requested_cuts.get(path["segment_id"], ()))
        if (
            not cuts
            and midpoint_splits
            and length >= 2 * search.minimum_segment_length_mm
            and search.max_split_depth > 0
        ):
            cuts = (length / 2,)
        if not cuts:
            result.append(path)
            continue
        if (
            any(
                not _finite_real(value) or not 0 < value < length
                for value in cuts
            )
            or tuple(sorted(set(cuts))) != cuts
            or len(cuts) + 1 > 2**search.max_split_depth
            or np.any(
                np.diff((0.0, *cuts, length))
                < search.minimum_segment_length_mm - 1e-8
            )
        ):
            raise ValueError(
                "Split points exceed the declared subdivision/minimum-length envelope"
            )
        from .workstation_planning import slice_path

        half_overlap = search.overlap_mm / 2
        bounds = (0.0, *cuts, length)
        for index, (low, high) in enumerate(zip(bounds[:-1], bounds[1:])):
            result.append(
                slice_path(
                    path,
                    max(0.0, low - half_overlap) if index else 0.0,
                    min(length, high + half_overlap)
                    if index < len(bounds) - 2
                    else length,
                    segment_id=next_id,
                    overlap_mm=search.overlap_mm,
                )
            )
            next_id += 1
        changed = True
    return tuple(result) if changed else None


def _front_face(tool, margin):
    if tool.key != "belt" or not np.allclose(tool.normal, [-1, 0, 0]):
        raise ValueError(
            "Alternative contacts require the configured front-face belt"
        )
    if (
        not _finite_real(margin)
        or margin <= 0
        or np.any(tool.half_size[1:] <= margin / 1000)
    ):
        raise ValueError(
            "Declared belt edge margin exhausts the abrasive front face"
        )


def _positive_limit(value, name):
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _finite_real(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, Real)
        and math.isfinite(value)
    )


def _nonnegative_clearance(value):
    if not _finite_real(value) or value < 0:
        raise ValueError(
            "Required clamp clearance must be finite and nonnegative"
        )


def _rejection_rows(rows):
    return [
        row
        for parent in rows
        for row in ([parent] if parent.get("status") != "passed" else [])
        + _rejection_rows(parent.get("subsegments", []))
    ]


def _clamp_belt_pair(first, second):
    return (
        (first.startswith("gripper_") or "clamp" in first) and "belt" in second
    ) or (
        (second.startswith("gripper_") or "clamp" in second) and "belt" in first
    )


def _reason_categories(reason):
    lower = reason.lower()
    categories = set()
    if any(
        word in lower
        for word in ("不可达", "逆解", "ik failure", "ik failed", "unreachable")
    ):
        categories.add("ik_failure")
    if any(word in lower for word in ("限位", "joint limit")):
        categories.add("joint_limit")
    if any(word in lower for word in ("belt", "砂带")) and any(
        word in lower
        for word in (
            "gripper",
            "clamp",
            "wp_proxy",
            "clearance",
            "干涉",
            "净距",
            "留量",
        )
    ):
        categories.add("belt_clamp_clearance")
    if any(
        word in lower
        for word in ("approach", "retreat", "retract", "接近", "退刀")
    ) and any(word in lower for word in ("collision", "干涉")):
        categories.add("approach_retreat_collision")
    if any(
        word in lower
        for word in ("跳变", "插值", "连续曲线", "continuity", "coverage")
    ):
        categories.add("segment_continuity")
    if any(
        word in lower
        for word in (
            "预算",
            "budget",
            "节点上限",
            "时间上限",
            "node limit",
            "time limit",
        )
    ):
        categories.add("budget_exhausted")
    if not categories:
        categories.add(
            "other_collision"
            if any(word in lower for word in ("干涉", "collision"))
            else "other_rejection"
        )
    return categories
