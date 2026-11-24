"""Bounded segment repair, carried-object collision checks and safe transfers."""

from __future__ import annotations

import math
import time
from numbers import Integral, Real

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .workstation_model import (
    CELL_CONFIG,
    DEFAULT_WORKPIECE_PACKAGE,
    LOADING_POSITION,
)
from .workstation_motion import (
    NORMAL,
    PROCESS_RETREAT_M,
    bind_commands,
    preflight,
    transform,
    unexpected_contacts,
    workpiece_target,
)


class MotionBlocked(ValueError):
    """A concrete pose or connecting motion failed the cell's geometric gate."""


class PlanningBudgetExceeded(MotionBlocked):
    """A bounded search stopped before all required checks were completed."""


class PlanningBudget:
    """Check wall time and optional call limits at each expensive operation.

    One underlying IK/contact call may finish after the deadline. The result
    cannot be retained unless the subsequent checkpoint still has budget.
    """

    def __init__(
        self,
        seconds=None,
        *,
        max_ik_calls=None,
        max_collision_checks=None,
        now=None,
    ):
        """Validate the limits before starting their shared clock.

        Args:
            seconds: Finite nonnegative real seconds, or None for no deadline.
            max_ik_calls: Nonnegative integer IK limit, or None for no limit.
            max_collision_checks: Nonnegative integer collision-check limit,
                or None for no limit.
            now: Optional clock returning a monotonic timestamp in seconds.

        Raises:
            ValueError: A limit is negative, nonfinite or has an invalid type.
        """
        if seconds is not None and (
            isinstance(seconds, bool)
            or not isinstance(seconds, Real)
            or not 0 <= seconds < math.inf
        ):
            raise ValueError(
                "seconds must be a finite nonnegative number or None"
            )
        for name, limit in (
            ("max_ik_calls", max_ik_calls),
            ("max_collision_checks", max_collision_checks),
        ):
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, Integral)
                or limit < 0
            ):
                raise ValueError(
                    f"{name} must be a nonnegative integer or None"
                )
        self.now = now or time.monotonic
        self.started = self.now()
        self.seconds = seconds
        self.limits = {"ik": max_ik_calls, "collision": max_collision_checks}
        self.calls = {"ik": 0, "collision": 0}
        self.exhausted = False

    def check(self, operation=None):
        """Consume a call only if its shared segment budget permits it."""
        if (
            self.seconds is not None
            and self.now() - self.started >= self.seconds
        ):
            self.exhausted = True
            raise PlanningBudgetExceeded("规划预算耗尽：每段时间上限")
        if operation in self.calls:
            limit = self.limits[operation]
            if limit is not None and self.calls[operation] >= limit:
                self.exhausted = True
                raise PlanningBudgetExceeded(
                    f"规划预算耗尽：{operation} 调用上限"
                )
            self.calls[operation] += 1

    def summary(self):
        return {
            "seconds_limit": self.seconds,
            "elapsed_seconds": self.now() - self.started,
            "call_limits": self.limits,
            "calls": dict(self.calls),
            "exhausted": self.exhausted,
            "enforcement": "checkpoints before and after bounded underlying calls",
        }


class Commands:
    """Build only motions that pass both pose and connecting-motion checks."""

    def __init__(self, sim, budget=None):
        self.sim = sim
        self.config = getattr(sim, "cell_config", CELL_CONFIG)
        self.search = getattr(sim, "workpiece_package", DEFAULT_WORKPIECE_PACKAGE).contact_search
        self.budget = budget
        self.items = []
        self.pose = sim.body_transform(sim.gripper)
        self.q = sim.data.qpos[sim.q_index].copy()
        self.attached = False
        self.clock = 0.0
        self.max_error = 0.0
        self.rrt_repairs = 0

    def saved(self):
        return (
            len(self.items),
            self.pose.copy(),
            self.q.copy(),
            self.attached,
            self.clock,
        )

    def restore(self, value):
        length, self.pose, self.q, self.attached, self.clock = value
        del self.items[length:]

    def collision(self, q, phase):
        if self.budget is not None:
            self.budget.check("collision")
        self.sim.set_scratch_pose(q, self.attached)
        contacts = unexpected_contacts(self.sim, self.sim.scratch, phase)
        if self.budget is not None:
            self.budget.check()
        return contacts

    def solve_ik(self, pose, seed):
        if self.budget is not None:
            self.budget.check("ik")
        result = self.sim.ik(pose, seed)
        if self.budget is not None:
            self.budget.check()
        return result

    def edge(self, first, second, phase, step=0.025):
        count = max(1, int(np.ceil(np.max(np.abs(second - first)) / step)))
        for amount in np.linspace(0, 1, count + 1)[1:]:
            if self.collision(first * (1 - amount) + second * amount, phase):
                return False
        return True

    def joint_route(self, goal, phase):
        """Deterministic bounded RRT with collision checks along every edge."""
        if self.collision(goal, phase):
            raise MotionBlocked("目标姿态与整站障碍干涉")
        if self.edge(self.q, goal, phase):
            return [self.q.copy(), goal]
        rng = np.random.default_rng(19019 + len(self.items))
        nodes, parents = [self.q.copy()], [-1]
        begin = time.monotonic()
        scale = self.sim.upper - self.sim.lower
        for attempt in range(220):
            if self.budget is not None:
                self.budget.check()
            if time.monotonic() - begin > 3:
                break
            sample = (
                goal
                if attempt % 4 == 0
                else rng.uniform(self.sim.lower, self.sim.upper)
            )
            distances = np.linalg.norm(
                (np.asarray(nodes) - sample) / scale, axis=1
            )
            nearest = int(distances.argmin())
            delta = sample - nodes[nearest]
            proposal = nodes[nearest] + delta * min(
                1, 0.25 / max(np.linalg.norm(delta), 1e-9)
            )
            if not self.edge(nodes[nearest], proposal, phase):
                continue
            nodes.append(proposal)
            parents.append(nearest)
            if self.edge(proposal, goal, phase):
                route = [goal, proposal]
                previous = parents[-1]
                while previous >= 0:
                    route.append(nodes[previous])
                    previous = parents[previous]
                self.rrt_repairs += 1
                return route[::-1]
        raise MotionBlocked("连接段避障搜索达到时间/节点上限")

    def leg(
        self,
        wp_pose,
        phase,
        speed=0.12,
        event=None,
        point=None,
        detection_id=0,
        segment_id=0,
    ):
        goal = wp_pose @ self.sim.workpiece_from_gripper
        start_pose, start_q = self.pose.copy(), self.q.copy()
        distance = float(np.linalg.norm(goal[:3, 3] - start_pose[:3, 3]))
        angle = float(
            np.linalg.norm(
                Rotation.from_matrix(
                    goal[:3, :3] @ start_pose[:3, :3].T
                ).as_rotvec()
            )
        )
        count = max(
            1, int(np.ceil(max(distance / 0.008, angle / np.deg2rad(4))))
        )
        curve = Slerp(
            [0, 1], Rotation.from_matrix([start_pose[:3, :3], goal[:3, :3]])
        )
        samples = []
        q = start_q.copy()
        minimum_duration = max(distance / speed, angle / 0.3, 0.10)
        try:
            for index in range(1, count + 1):
                amount = index / count
                pose = transform(
                    curve(amount).as_matrix(),
                    start_pose[:3, 3] * (1 - amount) + goal[:3, 3] * amount,
                )
                candidate, error, _ = self.solve_ik(pose, q)
                if phase == "trace" and np.max(np.abs(candidate - q)) > self.search.max_joint_jump_rad:
                    raise MotionBlocked("加工轨迹关节跳变，需独立切段进退刀")
                if not self.edge(q, candidate, phase, step=0.012):
                    raise MotionBlocked("段间插值与整站障碍干涉")
                duration = max(
                    minimum_duration / count,
                    np.max(np.abs(candidate - q)) / 0.3,
                    0.03,
                )
                samples.append((candidate, pose, duration))
                self.max_error = max(self.max_error, error)
                q = candidate
        except PlanningBudgetExceeded:
            raise
        except ValueError:
            if phase not in {"transfer", "return", "home"}:
                raise
            goal_q = self.solve_ik(goal, start_q)[0]
            route = self.joint_route(goal_q, phase)
            samples = []
            q = start_q
            for first, last in zip(route[:-1], route[1:]):
                subdivisions = max(
                    1, int(np.ceil(np.max(np.abs(last - first)) / 0.008))
                )
                for amount in np.linspace(0, 1, subdivisions + 1)[1:]:
                    if self.budget is not None:
                        self.budget.check()
                    candidate = first * (1 - amount) + last * amount
                    self.sim.set_scratch_pose(candidate, self.attached)
                    pose = self.sim.body_transform(
                        self.sim.gripper, self.sim.scratch
                    )
                    duration = max(
                        float(np.max(np.abs(candidate - q))) / 0.22, 0.035
                    )
                    samples.append((candidate, pose, duration))
                    q = candidate
        if self.budget is not None:
            self.budget.check()
        for q, pose, duration in samples:
            self.clock += float(duration) * self.config["planning"].get(
                "trajectory_time_scale", 1.0
            )
            self.items.append(
                {
                    "t": self.clock,
                    "q": q.copy(),
                    "pose": pose.copy(),
                    "phase": phase,
                    "attached": self.attached,
                    "grasp_setup": getattr(self.sim, "grasp_setup", "original"),
                    "point": point,
                    "detection_id": detection_id,
                    "segment_id": segment_id,
                }
            )
        self.pose, self.q = goal.copy(), q.copy()
        if event:
            self.clock += 0.6
            self.items.append(
                {
                    "t": self.clock,
                    "q": self.q.copy(),
                    "pose": self.pose.copy(),
                    "phase": phase,
                    "attached": self.attached,
                    "grasp_setup": getattr(self.sim, "grasp_setup", "original"),
                    "event": event,
                    "point": point,
                    "detection_id": detection_id,
                    "segment_id": segment_id,
                }
            )
            self.attached = event == "grasp"


def segment_candidates(path, tool, search=None, *, max_roll_candidates=None):
    """Search planar contact offsets, normal roll and small process tilts.

    Belt coordinates remain inside the declared abrasive face with a fixed
    edge margin. Coarse normal-aligned candidates precede bounded refinements;
    every yielded pose still requires CAD pressure, IK and full curve checks.
    """
    search = search or DEFAULT_WORKPIECE_PACKAGE.contact_search
    normal = np.asarray(path["normal"], dtype=float)
    alignment = Rotation.align_vectors([NORMAL], [normal])[0]
    rolls = (
        45, 60, 30, 90, 120, 150, 180, 210, 0, 240, 270, 300,
        330, 75, 105, 135, 165, 195, 225, 255, 285, 315, 345, 15,
    )[:max_roll_candidates if max_roll_candidates is not None else CELL_CONFIG["planning"].get("max_roll_candidates", 24)]
    contacts = _belt_contacts(tool, search) if tool.key == "belt" and hasattr(tool, "half_size") else list(tool.candidates())
    for refinement in search.roll_refinement_deg:
        for tilt in search.tilt_candidates_deg:
            for axis in ((1,) if tilt == 0 else (1, 2)):
                deviation = Rotation.from_rotvec(np.eye(3)[axis] * np.deg2rad(tilt))
                for roll in rolls:
                    rotation = (
                        deviation * Rotation.from_rotvec(NORMAL * np.deg2rad(roll + refinement)) * alignment
                    ).as_matrix()
                    for contact in contacts:
                        yield rotation, contact, roll + refinement


def _belt_contacts(tool, search):
    half = np.asarray(tool.half_size[1:], dtype=float) - search.edge_margin_mm / 1000
    if np.any(half <= 0):
        raise MotionBlocked("砂带留边耗尽有效磨削面")
    width, length = half
    offsets = [
        (0, 0), (width, 0), (-width, 0), (0, length), (0, -length),
        (width, length), (-width, length), (width, -length), (-width, -length),
        *[tuple(np.asarray(point)[1:] - tool.contact[1:]) for point in tool.candidates()],
        *[(y, z) for z in np.linspace(-length, length, 5) for y in np.linspace(-width, width, 5)],
    ]
    return [
        tool.contact + [0, y, z]
        for y, z in dict.fromkeys(offsets)
        if abs(y) <= width + 1e-12 and abs(z) <= length + 1e-12
    ]


def separate_nominal_tool(builder, points, rotation, contact, gap, mode):
    """Move the tool plane outward until the nominal CAD stops pressing it.

    The contact depth comes from the current carried CAD collision pieces.
    This leaves more positive stock; it cannot exceed the existing remaining
    height target. The corrected poses still need IK and full curve preflight.
    """
    initial_gap = gap
    initial_penetration = 0.0
    if (
        mode == "removal"
        and gap * 1000 > builder.config["planning"]["target_remaining_height_mm"]
    ):
        raise MotionBlocked(
            f"名义工件脱离砂带所需留量超过剩余高度目标：{gap * 1000:.3f} mm"
        )
    for adjustment in range(3):
        seed = builder.q.copy()
        separation = 0.0
        for point in points[[0, len(points) // 2, -1]]:
            pose = workpiece_target(point, rotation, gap, contact)
            q, _, _ = builder.solve_ik(
                pose @ builder.sim.workpiece_from_gripper, seed
            )
            collisions = builder.collision(q, "trace")
            if collisions:
                raise MotionBlocked(
                    "加工姿态干涉："
                    + " / ".join(collisions[0]["pair"])
                    + f"（{collisions[0]['penetration_mm']:.3f} mm）"
                )
            for candidate in builder.sim.scratch.contact:
                if candidate.dist >= 0:
                    continue
                first, second = (
                    builder.sim.model.geom(int(candidate.geom1)).name,
                    builder.sim.model.geom(int(candidate.geom2)).name,
                )
                if builder.sim.tool.geom not in (first, second) or not (
                    first.startswith("wp_") or second.startswith("wp_")
                ):
                    continue
                penetration = -float(candidate.dist)
                if adjustment == 0:
                    initial_penetration = max(initial_penetration, penetration)
                # A larger gap translates the carried workpiece along +X.
                component = (
                    float(candidate.frame[0])
                    if first == builder.sim.tool.geom
                    else -float(candidate.frame[0])
                )
                separation = max(
                    separation,
                    penetration / component if component > 0 else math.inf,
                )
            seed = q
        if mode != "removal" or separation == 0:
            return gap, {
                **(
                    {
                        "requested_material_allowance_mm": initial_gap * 1000,
                        "material_allowance_mm": gap * 1000,
                    }
                    if mode == "removal"
                    else {"clearance_mm": gap * 1000}
                ),
                "nominal_tool_penetration_mm": initial_penetration * 1000,
                "tool_clearance_adjustments": adjustment,
            }
        # Round an outward correction up to a micrometre to avoid retaining
        # a contact merely because the solved pose landed at its boundary.
        gap += (
            math.ceil(separation * 1e6) / 1e6
            if separation < math.inf
            else math.inf
        )
        if gap * 1000 > builder.config["planning"]["target_remaining_height_mm"]:
            raise MotionBlocked(
                f"名义工件脱离砂带所需留量超过剩余高度目标：{gap * 1000:.3f} mm"
            )
    raise MotionBlocked("名义工件与砂带接触未在局部修复预算内脱离")


def _build_grasp_cycle(sim, paths, mode="removal", publish=None):
    """Repair each region independently; never execute rejected connecting legs."""
    publish = publish or (lambda record: None)
    builder = Commands(sim)
    loading_position = getattr(sim, "loading_position", LOADING_POSITION)
    loading = transform(sim.loading_rotation, loading_position)
    lift = transform(sim.loading_rotation, loading_position + [0, 0, 0.38])
    staging = transform(sim.loading_rotation, [-0.6, -0.65, 1.10])
    rows, accepted = [], []
    begin = time.monotonic()
    if not paths:
        return (
            [],
            [],
            {
                "passed": True,
                "segment_count": 0,
                "admitted_count": 0,
                "blocked_count": 0,
                "rows": [],
                "seconds": 0,
            },
        )
    try:
        builder.leg(loading, "grasp", speed=0.07, event="grasp")
        builder.leg(lift, "lift", speed=0.08)
        builder.leg(staging, "transfer")
    except ValueError as error:
        return (
            [],
            [],
            {
                "passed": False,
                "segment_count": len(paths),
                "admitted_count": 0,
                "blocked_count": len(paths),
                "rows": [
                    {
                        "segment_id": path["segment_id"],
                        "detection_id": path["detection_id"],
                        "status": "blocked",
                        "reason": f"抓取/抬升/转场：{error}",
                    }
                    for path in paths
                ],
                "seconds": time.monotonic() - begin,
            },
        )
    for path in paths:
        start_search = time.monotonic()
        builder.budget = PlanningBudget(
            builder.config["planning"]["max_search_seconds_per_segment"]
        )
        points = np.asarray(path["nominal_xyz"])
        failures = []
        saved = builder.saved()
        row = {
            "segment_id": path["segment_id"],
            "detection_id": path["detection_id"],
            "kind": path["kind"],
            "view_index": path.get("view_index"),
            "point_count": len(points),
            "status": "blocked",
        }
        attempts = 0
        for rotation, contact, roll in segment_candidates(
            path, sim.tool, builder.search,
            max_roll_candidates=builder.config["planning"].get("max_roll_candidates", 24),
        ):
            try:
                builder.budget.check()
            except PlanningBudgetExceeded as error:
                failures.append(str(error))
                break
            attempts += 1
            builder.restore(saved)
            try:
                # Resolve nominal tool pressure before connecting the poses.
                gap, clearance = separate_nominal_tool(
                    builder,
                    points,
                    rotation,
                    contact,
                    path.get(
                        "material_allowance_mm",
                        builder.config["planning"]["material_allowance_mm"],
                    )
                    / 1000
                    if mode == "removal"
                    else 0.003,
                    mode,
                )
                process_staging = transform(rotation, [-0.6, -0.65, 1.10])
                builder.leg(process_staging, "transfer", speed=0.1)
                first = workpiece_target(points[0], rotation, gap, contact)
                retreat = first.copy()
                retreat[:3, 3] -= NORMAL * PROCESS_RETREAT_M
                builder.leg(retreat, "transfer")
                builder.leg(
                    first,
                    "approach",
                    speed=0.025,
                    point=points[0],
                    detection_id=path["detection_id"],
                    segment_id=path["segment_id"],
                )
                for point in points[1:]:
                    builder.leg(
                        workpiece_target(point, rotation, gap, contact),
                        "trace",
                        speed=path["recipe"]["feed_mm_s"] / 1000,
                        point=point,
                        detection_id=path["detection_id"],
                        segment_id=path["segment_id"],
                    )
                last = workpiece_target(points[-1], rotation, gap, contact)
                last[:3, 3] -= NORMAL * PROCESS_RETREAT_M
                builder.leg(
                    last, "retract", speed=0.04, point=points[-1],
                    detection_id=path["detection_id"], segment_id=path["segment_id"],
                )
                builder.leg(process_staging, "return")
                builder.leg(staging, "return")
                # Sparse poses and linear edges do not guarantee the actual
                # PCHIP curve. Reject this candidate before retaining its path;
                # otherwise a single bad trace cancels every accepted segment.
                curve_check = preflight(
                    sim, builder.items, budget=builder.budget
                )
                if not curve_check["passed"]:
                    collisions = curve_check["unexpected_contacts"]
                    if not collisions:
                        raise MotionBlocked(
                            curve_check.get("reason", "候选连续曲线预检未完成")
                        )
                    collision = collisions[0]
                    raise MotionBlocked(
                        "候选连续曲线干涉："
                        + " / ".join(collision["pair"])
                        + f"（{collision['penetration_mm']:.3f} mm）"
                    )
                row.update(
                    status="passed",
                    roll_deg=roll,
                    attempts=attempts,
                    tool_contact_world=contact.tolist(),
                    workpiece_rotation_world=rotation.tolist(),
                    tool=sim.tool.key,
                    grasp_setup=getattr(sim, "grasp_setup", "original"),
                    surface_normal_world=(rotation @ np.asarray(path["normal"])).tolist(),
                    normal_deviation_deg=float(np.rad2deg(np.arccos(np.clip(np.dot(rotation @ np.asarray(path["normal"]), NORMAL), -1, 1)))),
                    belt_edge_margin_mm=getattr(sim, "workpiece_package", DEFAULT_WORKPIECE_PACKAGE).contact_search.edge_margin_mm,
                    coverage=1.0,
                    coverage_ratio=1.0,
                    **clearance,
                    reason="整段及进出段通过六轴可达性和携件碰撞检查",
                )
                accepted.append(
                    {**path, "material_allowance_mm": gap * 1000}
                    if mode == "removal"
                    else path
                )
                break
            except PlanningBudgetExceeded as error:
                failures.append(str(error))
                break
            except ValueError as error:
                failures.append(str(error))
        if row["status"] != "passed":
            builder.restore(saved)
            row.update(
                reason=failures[-1] if failures else "没有可行加工姿态",
                coverage=0,
                attempts=attempts,
            )
        row["seconds"] = time.monotonic() - start_search
        row["failure_reasons"] = list(dict.fromkeys(failures))[:5]
        row["budget"] = builder.budget.summary()
        builder.budget = None
        rows.append(row)
        publish(
            {
                "rows": rows,
                "admitted_count": len(accepted),
                "segment_count": len(paths),
            }
        )
    if not accepted:
        builder.items = []
        checks = {"passed": False, "reason": "所有加工段均被安全门拒绝"}
    else:
        try:
            builder.leg(lift, "return")
            builder.leg(loading, "release", speed=0.07, event="release")
            builder.leg(
                transform(
                    sim.loading_rotation, loading_position + [0, 0, 0.35]
                ),
                "home",
            )
            builder.items = bind_commands(sim, builder.items)
            checks = preflight(sim, builder.items)
        except ValueError as error:
            builder.items = []
            checks = {"passed": False, "reason": f"回位段失败：{error}"}
    if not checks["passed"]:
        accepted = []
        builder.items = []
        for row in rows:
            if row["status"] == "passed":
                row["status"] = "blocked"
                row["coverage"] = 0
                row["reason"] = (
                    "局部姿态通过，但完整插值/回位检查失败，整条命令拒绝执行"
                )
    return (
        builder.items,
        accepted,
        {
            **checks,
            "rows": rows,
            "segment_count": len(paths),
            "admitted_count": len(accepted) if checks["passed"] else 0,
            "blocked_count": len(paths)
            - (len(accepted) if checks["passed"] else 0),
            "complete_segment_rate": len(accepted) / len(paths)
            if checks["passed"]
            else 0,
            "all_paths_passed": bool(accepted)
            and len(accepted) == len(paths)
            and checks["passed"],
            "seconds": time.monotonic() - begin,
            "duration_s": builder.clock if builder.items else 0.0,
            "sample_count": len(builder.items),
            "motion_binding": builder.items[0].get("motion_binding")
            if builder.items
            else None,
            "ik_max_mm": builder.max_error * 1000,
            "connecting_motion_repairs": builder.rrt_repairs,
            "workpiece_normal_strategy": "each observed region is oriented against the fixed tool separately",
            "source_strategy": "Wang Shiyu: screen, reason-specific repair, local repair, full acceptance",
        },
    )


def build_plan(sim, paths, mode="removal", publish=None):
    """Admit whole paths first, then bounded overlapping local grasp cycles.

    Each repaired subsegment has its own verified grasp, approach, trace,
    retreat and return cycle. Subdivision never joins different source paths
    or interpolates across a declared permission gap. Coverage is the union of
    admitted source intervals, not the sum of overlapping subsegment lengths.
    """
    publish = publish or (lambda record: None)
    if not paths:
        return _build_grasp_cycle(sim, paths, mode, publish)
    begin = time.monotonic()
    package = getattr(sim, "workpiece_package", None)
    search = (package or DEFAULT_WORKPIECE_PACKAGE).contact_search
    valid, invalid = [], []
    for path in paths:
        reason = _path_rejection(path, package, sim.tool)
        if reason is not None:
            invalid.append({"segment_id": path["segment_id"], "detection_id": path["detection_id"], "status": "blocked", "reason": reason, "coverage": 0.0, "coverage_ratio": 0.0})
            continue
        length = float(_path_arc(path)[-1] * 1000)
        valid.append({
            **path,
            "source_segment_id": path.get("source_segment_id", path.get("split_from", path["segment_id"])),
            "source_length_mm": path.get("source_length_mm", length),
            "source_interval_mm": path.get("source_interval_mm", [0.0, length]),
        })
    commands, accepted, checks = _build_grasp_cycle(sim, valid, mode)
    rows = [*checks["rows"], *invalid]
    cycles = [commands] if commands else []
    next_id = max(path["segment_id"] for path in paths) + 1
    attempted_leaves = sum(row["status"] == "passed" for row in rows)
    for path in valid:
        row = next(row for row in rows if row["segment_id"] == path["segment_id"])
        if row["status"] == "passed":
            continue
        pending = [(path, 0, row)]
        local_rows = []
        while pending:
            current, depth, current_row = pending.pop(0)
            length = float(_path_arc(current)[-1] * 1000)
            failures = [current_row.get("reason", ""), *current_row.get("failure_reasons", [])]
            if (
                depth >= search.max_split_depth
                or current_row.get("reason", "").startswith("抓取/抬升/转场")
                or length < 2 * search.minimum_segment_length_mm
                or not any(any(word in failure for word in ("不可达", "干涉", "跳变", "插值")) for failure in failures)
            ):
                attempted_leaves += 1
                continue
            midpoint = length / 2
            half_overlap = search.overlap_mm / 2
            for low, high in ((0, midpoint + half_overlap), (midpoint - half_overlap, length)):
                child = slice_path(current, low, high, segment_id=next_id, overlap_mm=search.overlap_mm)
                next_id += 1
                motion, admitted, child_checks = _build_grasp_cycle(sim, [child], mode)
                child_row = {**child_checks["rows"][0], "split_from": child["split_from"], "source_interval_mm": child["source_interval_mm"], "coverage_ratio": child["coverage_ratio"], "split_depth": depth + 1}
                local_rows.append(child_row)
                if admitted:
                    cycles.append(motion)
                    accepted.extend(admitted)
                    attempted_leaves += 1
                else:
                    pending.append((child, depth + 1, child_row))
            publish({"rows": rows, "admitted_count": len(accepted), "segment_count": len(paths)})
        row["subsegments"] = local_rows
    assembled = []
    clock = 0.0
    for cycle in cycles:
        assembled.extend({**item, "t": item["t"] + clock} for item in cycle)
        clock = assembled[-1]["t"]
    # Only the assembled cycle has an authoritative binding. Intermediate
    # stamps belong to identical local cycles and are removed before rechecking.
    assembled = [{key: value for key, value in item.items() if key != "motion_binding"} for item in assembled]
    if assembled:
        assembled = bind_commands(sim, assembled)
        final = preflight(sim, assembled)
    else:
        final = {"passed": False, "reason": "所有加工段均被安全门拒绝"}
    if not final["passed"]:
        assembled, accepted = [], []
    covered_lengths = []
    complete = 0
    for path in valid:
        row = next(row for row in rows if row["segment_id"] == path["segment_id"])
        matching = [item for item in accepted if item["source_segment_id"] == path["source_segment_id"]]
        covered = interval_coverage([item["source_interval_mm"] for item in matching], path["source_interval_mm"])
        length = path["source_interval_mm"][1] - path["source_interval_mm"][0]
        coverage = covered / length if length > 0 else 0.0
        covered_lengths.append((covered, length))
        complete += coverage >= 1 - 1e-8
        row.update(
            source_segment_id=path["source_segment_id"],
            source_interval_mm=path["source_interval_mm"],
            source_length_mm=path["source_length_mm"],
            coverage=coverage,
            coverage_ratio=coverage,
            grasp_setup=getattr(sim, "grasp_setup", "original"),
            status="passed" if coverage >= 1 - 1e-8 else "partial" if coverage > 0 else "blocked",
        )
        if coverage > 0 and row.get("subsegments"):
            row["reason"] = "已保留通过独立进退刀与连续预检的局部子段；其余区间保持受阻"
        if not final["passed"] and row.get("subsegments"):
            row["reason"] = "完整组合曲线预检失败，局部子段均拒绝执行"
    for item in accepted:
        interval = item["source_interval_mm"]
        item["coverage_ratio"] = (interval[1] - interval[0]) / item["source_length_mm"]
        item["grasp_setup"] = getattr(sim, "grasp_setup", "original")
    total_length = sum(length for _, length in covered_lengths)
    return assembled, accepted, {
        **checks, **final,
        "rows": rows,
        "grasp_setup": getattr(sim, "grasp_setup", "original"),
        "regrasp_checkpoints": [],
        "segment_count": len(paths),
        "admitted_count": len(accepted),
        "complete_source_segment_count": int(complete),
        "blocked_count": len(paths) - int(complete),
        "complete_segment_rate": complete / len(paths),
        "source_coverage_ratio": sum(covered for covered, _ in covered_lengths) / total_length if total_length else 0.0,
        "admitted_subsegment_count": sum("split_from" in item for item in accepted),
        "subsegment_admission_rate": len(accepted) / attempted_leaves if attempted_leaves else 0.0,
        "admitted_subsegment_coverage_ratio": 1.0 if accepted else 0.0,
        "all_paths_passed": complete == len(paths) and final["passed"],
        "sample_count": len(assembled),
        "duration_s": assembled[-1]["t"] if assembled else 0.0,
        "motion_binding": assembled[0].get("motion_binding") if assembled else None,
        "seconds": time.monotonic() - begin,
        "split_overlap_mm": search.overlap_mm,
        "split_max_depth": search.max_split_depth,
        "split_cycle_policy": "independent verified grasp/approach/trace/retract/return cycle per repaired subsegment",
    }


def _path_rejection(path, package, tool):
    points = np.asarray(path["nominal_xyz"], dtype=float)
    normal = np.asarray(path["normal"], dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 2 or not np.isfinite(points).all() or normal.shape != (3,) or not np.isfinite(normal).all() or not np.isclose(np.linalg.norm(normal), 1, atol=1e-3):
        return "轨迹须有有限的米制点与单位表面法向"
    if np.any(np.linalg.norm(np.diff(points, axis=0), axis=1) > 0.005):
        return "轨迹跨越已有许可支持缺口；不得插值切段或衔接"
    if path.get("kind") not in {"burr", "seam"}:
        return "该区域未获准自动磨削"
    if package is None:
        return None
    if path.get("coordinate_frame", package.coordinate_frame) != package.coordinate_frame:
        return "轨迹 CAD 坐标身份与工件包不一致"
    for zone in package.protected_zones:
        distance = np.linalg.norm(np.maximum(np.maximum(zone.bounds_m[0] - points, points - zone.bounds_m[1]), 0), axis=1)
        if np.any(distance <= tool.footprint_radius + 0.001):
            return f"工具足迹进入声明保护区：{zone.name}"
    return None


def _path_arc(path):
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(path["nominal_xyz"], axis=0), axis=1))]


def slice_path(path, low_mm, high_mm, *, segment_id, overlap_mm=0.0):
    """Restrict a contiguous permitted polyline without extrapolating its ends."""
    arc = _path_arc(path) * 1000
    if not 0 <= low_mm < high_mm <= arc[-1] + 1e-8:
        raise ValueError("子段区间须完全落在原许可轨迹内")
    samples = np.unique(np.r_[low_mm, arc[(arc > low_mm) & (arc < high_mm)], high_mm])
    points = np.asarray(path["nominal_xyz"])
    local = np.column_stack([np.interp(samples, arc, points[:, axis]) for axis in range(3)])
    offset = path.get("source_interval_mm", [0.0, arc[-1]])[0]
    source_length = path.get("source_length_mm", float(arc[-1]))
    row = {
        **path,
        "segment_id": segment_id,
        "split_from": path.get("split_from", path["segment_id"]),
        "source_segment_id": path.get("source_segment_id", path.get("split_from", path["segment_id"])),
        "source_interval_mm": [offset + low_mm, offset + high_mm],
        "source_length_mm": source_length,
        "nominal_xyz": local.tolist(),
        "xyz": local.tolist(),
        "coverage_ratio": (high_mm - low_mm) / source_length,
        "overlap_mm": overlap_mm,
    }
    observed = np.asarray(path.get("observed_xyz", []))
    if observed.shape == points.shape:
        row["observed_xyz"] = np.column_stack([np.interp(samples, arc, observed[:, axis]) for axis in range(3)]).tolist()
    return row


def interval_coverage(intervals, boundary):
    """Return union length inside one source interval, counting overlap once."""
    covered, end = 0.0, boundary[0]
    for low, high in sorted(intervals):
        low, high = max(low, boundary[0]), min(high, boundary[1])
        covered += max(0.0, high - max(low, end))
        end = max(end, high)
    return covered
