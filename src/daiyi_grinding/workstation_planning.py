"""Bounded segment repair, carried-object collision checks and safe transfers."""

from __future__ import annotations

import time

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .workstation_model import (
    CELL_CONFIG,
    LOADING_POSITION,
)
from .workstation_motion import (
    NORMAL,
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
            self.clock += float(duration) * CELL_CONFIG["planning"].get(
                "trajectory_time_scale", 1.0
            )
            self.items.append(
                {
                    "t": self.clock,
                    "q": q.copy(),
                    "pose": pose.copy(),
                    "phase": phase,
                    "attached": self.attached,
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
                    "event": event,
                    "point": point,
                    "detection_id": detection_id,
                    "segment_id": segment_id,
                }
            )
            self.attached = event == "grasp"


def segment_candidates(path, tool):
    normal = np.asarray(path["normal"])
    alignment = Rotation.align_vectors([NORMAL], [normal])[0]
    for roll in (
        45,
        60,
        30,
        90,
        120,
        150,
        180,
        210,
        0,
        240,
        270,
        300,
        330,
        75,
        105,
        135,
        165,
        195,
        225,
        255,
        285,
        315,
        345,
        15,
    ):
        rotation = (
            Rotation.from_rotvec(NORMAL * np.deg2rad(roll)) * alignment
        ).as_matrix()
        for contact in tool.candidates():
            yield rotation, contact, roll


def build_plan(sim, paths, mode="removal", publish=None):
    """Repair each region independently; never execute rejected connecting legs."""
    publish = publish or (lambda record: None)
    builder = Commands(sim)
    loading = transform(sim.loading_rotation, LOADING_POSITION)
    lift = transform(sim.loading_rotation, LOADING_POSITION + [0, 0, 0.38])
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
    gap = (
        CELL_CONFIG["planning"]["material_allowance_mm"] / 1000
        if mode == "removal"
        else 0.003
    )
    for path in paths:
        start_search = time.monotonic()
        builder.budget = PlanningBudget(
            CELL_CONFIG["planning"]["max_search_seconds_per_segment"]
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
        for rotation, contact, roll in segment_candidates(path, sim.tool):
            try:
                builder.budget.check()
            except PlanningBudgetExceeded as error:
                failures.append(str(error))
                break
            attempts += 1
            builder.restore(saved)
            try:
                # Gate sparse endpoints before spending time on connecting legs.
                seed = builder.q.copy()
                for point in points[[0, len(points) // 2, -1]]:
                    pose = workpiece_target(point, rotation, gap, contact)
                    q, _, _ = builder.solve_ik(
                        pose @ sim.workpiece_from_gripper, seed
                    )
                    collisions = builder.collision(q, "trace")
                    if collisions:
                        raise MotionBlocked(
                            "加工姿态干涉："
                            + " / ".join(collisions[0]["pair"])
                            + f"（{collisions[0]['penetration_mm']:.3f} mm）"
                        )
                    seed = q
                process_staging = transform(rotation, [-0.6, -0.65, 1.10])
                builder.leg(process_staging, "transfer", speed=0.1)
                first = workpiece_target(points[0], rotation, gap, contact)
                retreat = first.copy()
                retreat[:3, 3] -= NORMAL * 0.12
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
                last[:3, 3] -= NORMAL * 0.12
                builder.leg(last, "retract", speed=0.04)
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
                    coverage=1.0,
                    reason="整段及进出段通过六轴可达性和携件碰撞检查",
                )
                accepted.append(path)
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
                    sim.loading_rotation, LOADING_POSITION + [0, 0, 0.35]
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
