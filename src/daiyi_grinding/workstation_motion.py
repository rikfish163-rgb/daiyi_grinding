"""Carry a CAD casting to the fixed tool and audit actual MuJoCo feedback."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from numbers import Integral
from pathlib import Path

import mujoco
import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation, Slerp

from .workstation_model import (
    LOADING_POSITION,
    WHEEL_CENTER,
    WHEEL_HALF_WIDTH,
    write_json,
)

NORMAL = np.array([-1.0, 0.0, 0.0])
CONTACT = WHEEL_CENTER + np.array([WHEEL_HALF_WIDTH, 0.0, 0.0])
PROCESS_RETREAT_M = 0.06
PREFLIGHT_JOINT_STEP_RAD = 0.012
PREFLIGHT_SAMPLE_BUDGET = 200_000


def transform(rotation, position):
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = position
    return result


def unexpected_contacts(sim, data, phase):
    """Separate tool contact and intentional fixture support from obstacles."""
    contacts = []
    for contact in data.contact:
        if contact.dist >= -0.0004:
            continue
        a, b = (
            sim.model.geom(int(contact.geom1)).name,
            sim.model.geom(int(contact.geom2)).name,
        )
        workpiece_pair = a.startswith("wp_") or b.startswith("wp_")
        if workpiece_pair and sim.tool.geom in (a, b):
            if (
                phase in {"trace", "approach", "retract"}
                and contact.dist > -0.0008
            ):
                continue
        if workpiece_pair and (a.startswith("fix_") or b.startswith("fix_")):
            if phase in {"home", "grasp", "lift", "return", "release"}:
                continue
        contacts.append(
            {"pair": [a, b], "penetration_mm": -float(contact.dist) * 1000}
        )
    return contacts


def workpiece_target(point, rotation, gap_m, contact=CONTACT):
    """Inverse workpiece motion: a local surface point meets a fixed tool."""
    return transform(rotation, contact - NORMAL * gap_m - rotation @ point)


def choose_process_orientation(sim, paths):
    """Search wrist roll and verify actual cell geometry at observed regions."""
    samples = []
    for path in paths:
        points = np.asarray(path.get("nominal_xyz", path["xyz"]))
        samples.extend([points[0], points[len(points) // 2], points[-1]])
    samples = np.asarray(samples)
    # Keep the entire casting on the robot side of the wheel's end face. This
    # configured abrasive face is explicit in the process contract.
    align = Rotation.from_euler("y", -90, degrees=True)
    best = None
    for yaw, contact in (
        (yaw, contact)
        for contact in sim.tool.candidates()
        for yaw in (
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
        )
    ):
        rotation = (
            align * Rotation.from_euler("z", yaw, degrees=True)
        ).as_matrix()
        seed = np.array([0.7, 0.2, 0.4, -1.0, 0.8, 1.5])
        failed = []
        solutions = []
        for point in samples:
            pose = workpiece_target(point, rotation, 0.003, contact)
            try:
                q, _, _ = sim.ik(pose @ sim.workpiece_from_gripper, seed)
            except ValueError as error:
                failed.append({"reason": str(error)})
                break
            sim.set_scratch_pose(q, attached=True)
            failed.extend(unexpected_contacts(sim, sim.scratch, "approach"))
            if failed:
                break
            seed = q
            solutions.append(q)
        if best is None or len(failed) < len(best[0]):
            best = (failed, yaw)
        if not failed:
            sim.process_contact = contact
            return rotation, {
                "passed": True,
                "workpiece_roll_deg": yaw,
                "surface_normal_world": NORMAL.tolist(),
                "tool_contact_world": contact.tolist(),
                "tool_surface": "configured front abrasive face",
                "checked_surface_poses": len(samples),
                "scope": "CAD-derived convex pieces and published imported joint limits",
            }
    return None, {
        "passed": False,
        "closest_roll_deg": best[1] if best else None,
        "unexpected_contacts": best[0] if best else [],
        "reason": "未找到所有选中区域均可达且无障碍碰撞的工件姿态",
    }


def make_commands(sim, paths, process_rotation, mode="removal"):
    """Sample grasp, transfer, processing and return in the gripper frame."""
    commands = []
    previous_pose = sim.body_transform(sim.gripper)
    previous_q = sim.data.qpos[sim.q_index].copy()
    clock = 0.0
    attached = False
    largest_ik_error = 0.0
    contact = sim.process_contact

    def leg(wp_pose, phase, speed=0.15, event=None, point=None, detection_id=0):
        nonlocal previous_pose, previous_q, clock, attached, largest_ik_error
        target = wp_pose @ sim.workpiece_from_gripper
        distance = float(np.linalg.norm(target[:3, 3] - previous_pose[:3, 3]))
        angle = float(
            np.linalg.norm(
                Rotation.from_matrix(
                    target[:3, :3] @ previous_pose[:3, :3].T
                ).as_rotvec()
            )
        )
        count = max(
            1, int(np.ceil(max(distance / 0.008, angle / np.deg2rad(4))))
        )
        rotations = Slerp(
            [0, 1],
            Rotation.from_matrix([previous_pose[:3, :3], target[:3, :3]]),
        )
        minimum_duration = max(distance / speed, angle / 0.35, 0.12)
        start = previous_pose.copy()
        for index in range(1, count + 1):
            amount = index / count
            pose = transform(
                rotations(amount).as_matrix(),
                start[:3, 3] * (1 - amount) + target[:3, 3] * amount,
            )
            q, pe, _ = sim.ik(pose, previous_q)
            duration = max(
                minimum_duration / count,
                float(np.max(np.abs(q - previous_q))) / 0.35,
                0.02,
            )
            clock += duration
            commands.append(
                {
                    "t": clock,
                    "q": q,
                    "pose": pose,
                    "phase": phase,
                    "attached": attached,
                    "point": point,
                    "detection_id": detection_id,
                }
            )
            largest_ik_error = max(largest_ik_error, pe)
            previous_q = q
        previous_pose = target
        if event:
            clock += 0.6
            commands.append(
                {
                    "t": clock,
                    "q": previous_q.copy(),
                    "pose": target.copy(),
                    "phase": phase,
                    "attached": attached,
                    "event": event,
                    "point": point,
                    "detection_id": detection_id,
                }
            )
            attached = event == "grasp"

    loading = transform(sim.loading_rotation, LOADING_POSITION)
    lift = transform(sim.loading_rotation, LOADING_POSITION + [0, 0, 0.38])
    staging = transform(sim.loading_rotation, [-0.6, -0.65, 1.10])
    process_staging = transform(process_rotation, [-0.6, -0.65, 1.10])
    leg(loading, "grasp", speed=0.08, event="grasp")
    leg(lift, "lift", speed=0.10)
    leg(staging, "transfer")
    leg(process_staging, "transfer", speed=0.10)
    for path in paths:
        points = np.asarray(
            path["nominal_xyz"] if mode == "removal" else path["xyz"]
        )
        gap = 0.00005 if mode == "removal" else 0.003
        first = workpiece_target(points[0], process_rotation, gap, contact)
        retreat = first.copy()
        retreat[:3, 3] -= NORMAL * PROCESS_RETREAT_M
        leg(retreat, "transfer")
        leg(
            first,
            "approach",
            speed=0.035,
            point=points[0],
            detection_id=path["detection_id"],
        )
        for point in points[1:]:
            leg(
                workpiece_target(point, process_rotation, gap, contact),
                "trace",
                speed=path["recipe"]["feed_mm_s"] / 1000,
                point=point,
                detection_id=path["detection_id"],
            )
        last = workpiece_target(points[-1], process_rotation, gap, contact)
        last[:3, 3] -= NORMAL * PROCESS_RETREAT_M
        leg(last, "retract", speed=0.05)
    leg(process_staging, "return")
    leg(staging, "return")
    leg(lift, "return")
    leg(loading, "release", speed=0.08, event="release")
    leg(
        transform(sim.loading_rotation, LOADING_POSITION + [0, 0, 0.35]),
        "home",
        speed=0.10,
    )
    return commands, {
        "duration_s": clock,
        "sample_count": len(commands),
        "ik_max_mm": largest_ik_error * 1000,
        "joint_speed_limit_deg_s": float(np.rad2deg(0.35)),
    }


def joint_curve(sim, commands, start_time=0):
    """A C1 joint curve shared by preflight and the simulated servo."""
    times = np.r_[start_time, [start_time + item["t"] for item in commands]]
    positions = np.vstack(
        [sim.data.qpos[sim.q_index], [item["q"] for item in commands]]
    )
    if (
        not np.isfinite(times).all()
        or not np.isfinite(positions).all()
        or positions.shape != (len(times), 6)
        or np.any(np.diff(times) <= 0)
    ):
        raise ValueError("命令时间须严格递增，六轴关节位置须为有限数")
    return times, PchipInterpolator(times, positions, axis=0)


def motion_binding(sim):
    """Bind a plan to the actual fixture, frames, scene and motion sources."""
    source = Path(__file__).parent
    paths = [
        source / name
        for name in (
            "workstation_motion.py",
            "workstation_planning.py",
            "workstation_transfers.py",
            "workstation_model.py",
            "workstation_tooling.py",
            "workpiece_packages.py",
        )
    ]
    paths.append(source.parents[1] / "config" / "workstation.json")
    package = getattr(sim, "workpiece_package", None)
    package_path = getattr(package, "path", None)
    if package_path is not None and Path(package_path).is_file():
        paths.append(Path(package_path))
    scene = getattr(sim, "scene_file", None)
    geometry = hashlib.sha256()
    for key in (
        "geom_type",
        "geom_size",
        "geom_pos",
        "geom_quat",
        "geom_bodyid",
        "geom_dataid",
        "geom_contype",
        "geom_conaffinity",
        "mesh_vert",
        "mesh_face",
        "mesh_pos",
        "mesh_quat",
        "body_pos",
        "body_quat",
        "jnt_pos",
        "jnt_axis",
        "jnt_range",
    ):
        if hasattr(sim.model, key):
            array = np.ascontiguousarray(getattr(sim.model, key))
            geometry.update(f"{key}:{array.dtype}:{array.shape}".encode())
            geometry.update(memoryview(array))
    payload = {
        "schema_version": 1,
        **(
            {"workpiece_id": package.workpiece_id}
            if package is not None and getattr(package, "workpiece_id", None)
            else {}
        ),
        "grasp_setup": getattr(sim, "grasp_setup", None),
        "frames": {
            key: np.asarray(getattr(sim, key)).tolist()
            for key in (
                "gripper_from_workpiece",
                "workpiece_from_gripper",
                "loading_rotation",
            )
            if hasattr(sim, key)
        },
        "joint_limits": {
            key: np.asarray(getattr(sim, key)).tolist()
            for key in ("lower", "upper")
            if hasattr(sim, key)
        },
        "tool": {
            "key": getattr(sim.tool, "key", None),
            "geom": sim.tool.geom,
        },
        "scene_sha256": hashlib.sha256(Path(scene).read_bytes()).hexdigest()
        if scene is not None
        else None,
        "collision_model_sha256": geometry.hexdigest(),
        "source_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
    }
    return {
        **payload,
        "sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def bind_commands(sim, commands):
    """Stamp the first command while retaining the existing command schema."""
    if not commands:
        return commands
    return [
        {**commands[0], "motion_binding": motion_binding(sim)},
        *commands[1:],
    ]


def binding_failure(sim, commands):
    """Reject a stamped plan after a fixture/frame/model/source change."""
    expected = commands[0].get("motion_binding") if commands else None
    if expected is not None:
        actual = motion_binding(sim)
        if expected.get("sha256") != actual["sha256"]:
            return {
                "reason": "装夹、坐标、场景或运动源码已变化，旧计划哈希失效",
                "expected_binding_sha256": expected.get("sha256"),
                "actual_binding_sha256": actual["sha256"],
            }
    return None


def curve_interval_speed_bound(curve, index, duration):
    """Exact absolute derivative bound for each cubic coordinate interval."""
    cubic, quadratic, linear = curve.c[:3, index]
    end = 3 * cubic * duration**2 + 2 * quadratic * duration + linear
    bound = np.maximum(np.abs(linear), np.abs(end))
    vertex = np.divide(
        -quadratic,
        3 * cubic,
        out=np.zeros_like(cubic),
        where=cubic != 0,
    )
    inside = (cubic != 0) & (vertex > 0) & (vertex < duration)
    peak = np.abs(3 * cubic * vertex**2 + 2 * quadratic * vertex + linear)
    return np.where(inside, np.maximum(bound, peak), bound)


def preflight(
    sim,
    commands,
    *,
    sampling_policy="bounded_joint",
    sample_budget=PREFLIGHT_SAMPLE_BUDGET,
    budget=None,
):
    """Check the servo curve with bounded joint spacing and bounded work.

    The 0.012 rad spacing matches the existing Cartesian-leg edge checker.
    It strengthens temporal sampling; it is not a proof of continuous mesh
    clearance or a substitute for measured tracking and profile acceptance.
    ``legacy_four`` is retained only for explicit same-input ablations.

    Args:
        sim: Workstation or analytical fixture used for collision checks.
        commands: Existing joint commands with time, phase and attachment.
        sampling_policy: Bounded joint sampling or the legacy ablation.
        sample_budget: Nonnegative integer sample limit; floats and booleans
            are rejected before reading commands or checking the geometry.
        budget: Optional shared planning deadline and collision-call budget.

    Raises:
        ValueError: The sample budget or sampling policy is invalid.
    """
    if (
        isinstance(sample_budget, bool)
        or not isinstance(sample_budget, Integral)
        or sample_budget < 0
    ):
        raise ValueError("sample_budget must be a nonnegative integer")
    empty = {
        "passed": False,
        "checked_commands": 0,
        "contact_count": 0,
        "unexpected_contacts": [],
    }
    if not commands:
        return empty
    if sampling_policy not in {"bounded_joint", "legacy_four"}:
        raise ValueError("未知连续曲线采样策略")
    invalid_binding = binding_failure(sim, commands)
    if invalid_binding:
        return {**empty, **invalid_binding, "stale_plan": True}
    try:
        times, curve = joint_curve(sim, commands)
    except ValueError as error:
        return {**empty, "reason": str(error), "invalid_commands": True}
    positions = np.asarray([command["q"] for command in commands])
    if hasattr(sim, "lower") and (
        np.any(positions < sim.lower) or np.any(positions > sim.upper)
    ):
        return {
            **empty,
            "reason": "命令超出已发布的六轴关节限位",
            "joint_limits_failed": True,
        }
    counts = []
    for index, duration in enumerate(np.diff(times)):
        speed = curve_interval_speed_bound(curve, index, duration)
        count = (
            4
            if sampling_policy == "legacy_four"
            else max(
                4,
                int(
                    np.ceil(np.max(speed) * duration / PREFLIGHT_JOINT_STEP_RAD)
                ),
            )
        )
        counts.append(count)
    required_samples = sum(counts) + (
        len(commands) if sampling_policy == "bounded_joint" else 0
    )
    if required_samples > sample_budget:
        return {
            **empty,
            "reason": "连续曲线预检达到采样预算，整条命令拒绝执行",
            "budget_exhausted": True,
            "required_samples": required_samples,
            "sample_budget": sample_budget,
        }
    bad, contact_count = [], 0
    for index, command in enumerate(commands):
        first = 1 if sampling_policy == "legacy_four" else 0
        for step in range(first, counts[index] + 1):
            if budget is not None:
                budget.check("collision")
            fraction = step / counts[index]
            sim.set_scratch_pose(
                curve(
                    times[index] * (1 - fraction) + times[index + 1] * fraction
                ),
                command["attached"],
            )
            for collision in unexpected_contacts(
                sim, sim.scratch, command["phase"]
            ):
                contact_count += 1
                if len(bad) < 30:
                    bad.append(
                        {
                            "command": index,
                            "phase": command["phase"],
                            "fraction": fraction,
                            **collision,
                        }
                    )
    if budget is not None:
        budget.check()
    return {
        "passed": contact_count == 0,
        "contact_count": contact_count,
        "unexpected_contacts": bad,
        "checked_commands": len(commands),
        "samples_per_command": 4,
        "sampling_policy": sampling_policy,
        "max_samples_per_command": max(counts),
        "checked_interpolated_samples": required_samples,
        "joint_step_bound_rad": PREFLIGHT_JOINT_STEP_RAD
        if sampling_policy == "bounded_joint"
        else None,
        "scope": "the same C1 joint curve used by the servo, sampled with CAD-derived collision pieces; bounded joint spacing is not continuous collision certification or real-controller certification",
    }


def clip_excess(vertices, base, normals, pose, tool):
    """Clip only positive stock hit by the measured workpiece/tool transform."""
    world = vertices @ pose[:3, :3].T + pose[:3, 3]
    surface_x, inside = tool.stock_intersection(world)
    base_world = base @ pose[:3, :3].T + pose[:3, 3]
    world_normals = normals @ pose[:3, :3].T
    height = np.einsum("ij,ij->i", vertices - base, normals)
    height_limit = np.maximum(
        0.0,
        (surface_x - base_world[:, 0])
        / np.where(
            np.abs(world_normals[:, 0]) > 0.9, world_normals[:, 0], -1.0
        ),
    )
    cutting = (
        inside & (world_normals[:, 0] < -0.9) & (height > height_limit + 1e-8)
    )
    clipped = vertices.copy()
    clipped[cutting] = (
        base[cutting] + normals[cutting] * height_limit[cutting, None]
    )
    return clipped, cutting


class GeometricRemoval:
    """A swept-tool stock model driven by actual, not commanded, body poses.

    A configured rate limits normal removal during actual footprint engagement.
    The rate is a synthetic plant assumption; force and material are uncalibrated.
    Only excess meshes change.
    """

    def __init__(self, sim, rate_mm_s=None):
        self.sim = sim
        self.rate_mm_s = rate_mm_s
        self.parts = []
        self.changed = False
        for item in sim.private_defects:
            geom = sim.model.geom(item["geom"]).id
            mesh = int(sim.model.geom_dataid[geom])
            address, count = (
                sim.model.mesh_vertadr[mesh],
                sim.model.mesh_vertnum[mesh],
            )
            rotation = Rotation.from_quat(
                sim.model.geom_quat[geom][[1, 2, 3, 0]]
            ).as_matrix()
            origin = sim.model.geom_pos[geom].copy()
            vertices = sim.model.mesh_vert[address : address + count].astype(
                float
            )
            local = vertices @ rotation.T + origin
            if "base_vertices" in item:
                original = np.asarray(item["original_vertices"])
                _, indices = cKDTree(original).query(local)
                base = np.asarray(item["base_vertices"])[indices]
                normals = np.asarray(item["vertex_normals"])[indices]
            else:
                base = local.copy()
                base[:, 2] = item["nominal_plane_z_m"]
                normals = np.tile([0, 0, 1], (count, 1))
            self.parts.append(
                {
                    "name": item["geom"],
                    "kind": item["kind"],
                    "region": item.get("region"),
                    "geom": geom,
                    "mesh": mesh,
                    "address": address,
                    "count": count,
                    "rotation": rotation,
                    "origin": origin,
                    "vertices": local,
                    "initial": local.copy(),
                    "base": base,
                    "normals": normals,
                }
            )

    def cut(self, duration_s=0.0):
        """Remove swept stock, bounded by this interval's engaged duration."""
        pose = self.sim.body_transform(self.sim.workpiece)
        for part in self.parts:
            vertices, cutting = clip_excess(
                part["vertices"],
                part["base"],
                part["normals"],
                pose,
                self.sim.tool,
            )
            if not cutting.any():
                continue
            if self.rate_mm_s is not None:
                depth = np.einsum(
                    "ij,ij->i",
                    part["vertices"] - vertices,
                    part["normals"],
                )
                depth = np.minimum(depth, self.rate_mm_s * duration_s / 1000)
                if not np.any(depth > 0):
                    continue
                vertices = part["vertices"] - part["normals"] * depth[:, None]
            part["vertices"] = vertices
            compiled = (vertices - part["origin"]) @ part["rotation"]
            self.sim.model.mesh_vert[
                part["address"] : part["address"] + part["count"]
            ] = compiled
            self.changed = True

    def updates(self):
        updates = []
        for part in self.parts:
            positions = self.sim.model.mesh_vert[
                part["address"] : part["address"] + part["count"]
            ].round(8)
            cached_positions = part.get("replay_positions")
            if cached_positions is None or not np.array_equal(
                positions, cached_positions
            ):
                part["replay_positions"] = positions
                part["replay_update"] = {
                    "id": part["mesh"],
                    "positions": positions.tolist(),
                }
            updates.append(part["replay_update"])
        return updates

    def upload(self):
        if not self.changed:
            return
        for renderer in self.sim.renderers.values():
            if renderer._gl_context:
                renderer._gl_context.make_current()
            for part in self.parts:
                mujoco.mjr_uploadMesh(
                    self.sim.model, renderer._mjr_context, part["mesh"]
                )
        self.changed = False

    def summary(self):
        initial = (
            np.concatenate(
                [
                    np.maximum(
                        0,
                        np.einsum(
                            "ij,ij->i",
                            part["initial"] - part["base"],
                            part["normals"],
                        ),
                    )
                    for part in self.parts
                ]
            )
            if self.parts
            else np.zeros(1)
        )
        final = (
            np.concatenate(
                [
                    np.maximum(
                        0,
                        np.einsum(
                            "ij,ij->i",
                            part["vertices"] - part["base"],
                            part["normals"],
                        ),
                    )
                    for part in self.parts
                ]
            )
            if self.parts
            else np.zeros(1)
        )
        eligible = initial > 0.0003
        regions = []
        for part in self.parts:
            height = np.maximum(
                0,
                np.einsum(
                    "ij,ij->i", part["vertices"] - part["base"], part["normals"]
                ),
            )
            regions.append(
                {
                    "name": part["name"],
                    "kind": part["kind"],
                    "region": part["region"],
                    "remaining_peak_mm": float(height.max() * 1000),
                    "vertices_above_0_3mm": int(
                        np.count_nonzero(height > 0.0003)
                    ),
                }
            )
        # Evaluation can exclude a now-flat excess mesh despite rasterization
        # error at a coincident nominal surface. Inference never reads this.
        self.sim.evaluation_stock = {
            item["name"]: item["remaining_peak_mm"] for item in regions
        }
        return {
            "initial_peak_mm": float(initial.max() * 1000),
            "remaining_peak_mm": float(final.max() * 1000),
            "modified_vertices": int(np.count_nonzero(final < initial - 1e-8)),
            "vertex_completion_rate": float(np.mean(final[eligible] <= 0.0003))
            if eligible.any()
            else None,
            "residual_regions": regions,
            "nominal_cad_modified": False,
            "rate_mm_s": self.rate_mm_s,
            "rate_calibrated": False,
            "scope": "generated excess removed inside the actual swept tool footprint along CAD surface normals; a configured synthetic rate limits depth per engaged second; vertex statistics do not measure material volume, force or surface finish",
        }


def execute(
    sim,
    commands,
    mode,
    publish,
    realtime_factor=2.0,
    removal=None,
    prefix="",
    stop_requested=None,
):
    """Continuously integrate the servo and free workpiece with grasp constraints."""
    invalid_binding = binding_failure(sim, commands)
    if invalid_binding:
        raise ValueError(invalid_binding["reason"])
    removal = GeometricRemoval(sim) if removal is None else removal
    start_sim = float(sim.data.time)
    times, curve = joint_curve(sim, commands, start_sim)
    kp = sim.model.actuator_gainprm[sim.arm_actuators, 0]
    kv = -sim.model.actuator_biasprm[sim.arm_actuators, 2]
    start_wall = time.monotonic()
    index = 1
    event_done = set()
    history = []
    replay = sim.replay_recording
    trace_errors = []
    surface_errors = []
    grasp_errors = []
    orientation_errors = []
    unexpected = []
    max_force = 0.0
    contact_steps = 0
    next_publish = start_sim
    next_log = start_sim
    step = 0
    interrupted = False
    last_phase = None
    end = times[-1] + 0.4
    while sim.data.time < end:
        t = min(float(sim.data.time), times[-1])
        while index < len(times) - 1 and times[index] < t:
            completed = commands[index - 1]
            if completed.get("event") and index not in event_done:
                # Events occur after a stationary settling command. The free
                # body's qpos is never teleported during execution.
                if completed["event"] == "grasp":
                    expected = (
                        sim.body_transform(sim.gripper)
                        @ sim.gripper_from_workpiece
                    )
                    error = np.linalg.norm(
                        expected[:3, 3] - sim.data.xpos[sim.workpiece]
                    )
                    if error > 0.001:
                        raise RuntimeError(
                            f"夹持对齐误差 {error * 1000:.2f} mm 超出门槛"
                        )
                    sim.data.eq_active[sim.fixture_weld] = 0
                    sim.data.eq_active[sim.gripper_weld] = 1
                else:
                    sim.data.eq_active[sim.gripper_weld] = 0
                    sim.data.eq_active[sim.fixture_weld] = 1
                event_done.add(index)
            index += 1
        command = commands[index - 1]
        target_q = curve(t)
        velocity = curve(t, 1) if t < times[-1] else np.zeros(6)
        acceleration = curve(t, 2) if t < times[-1] else np.zeros(6)
        sim.data.ctrl[sim.arm_actuators] = (
            target_q
            + (kv + sim.model.dof_damping[sim.dof_index]) * velocity / kp
        )
        sim.data.ctrl[sim.model.actuator("spindle_speed").id] = (
            180
            if sim.tool.key == "wheel"
            and command["phase"] in {"approach", "trace", "retract"}
            else 0
        )
        sim.apply_motion_feedforward(acceleration)
        mujoco.mj_step(sim.model, sim.data)
        mujoco.mj_forward(sim.model, sim.data)
        if not np.isfinite(sim.data.qpos).all():
            raise RuntimeError("MuJoCo 状态出现非有限数")
        step += 1
        if (
            mode == "removal"
            and command["phase"] in {"trace", "approach"}
            and step % 5 == 0
        ):
            removal.cut(duration_s=5 * sim.model.opt.timestep)
        collisions = unexpected_contacts(sim, sim.data, command["phase"])
        if collisions:
            unexpected.extend(
                {"time_s": float(sim.data.time), **item} for item in collisions
            )
            if any(item["penetration_mm"] > 2.0 for item in collisions):
                raise RuntimeError(
                    f"运动中检测到障碍接触：{collisions[0]['pair']}"
                )
        force = 0.0
        for contact_index, contact in enumerate(sim.data.contact):
            if sim.model.geom(sim.tool.geom).id in (
                contact.geom1,
                contact.geom2,
            ):
                wrench = np.zeros(6)
                mujoco.mj_contactForce(
                    sim.model, sim.data, contact_index, wrench
                )
                force += max(0.0, float(wrench[0]))
        max_force = max(max_force, force)
        contact_steps += int(force > 0)
        actual = sim.body_transform(sim.gripper)
        sample_t = min(float(sim.data.time), times[-1])
        desired = sim.desired_gripper_pose(curve(sample_t))
        target_position = desired[:3, 3]
        error = float(np.linalg.norm(actual[:3, 3] - target_position) * 1000)
        if sim.data.time >= next_log:
            surface_error = grasp_error = None
            if command["phase"] == "trace":
                trace_errors.append(error)
                actual_workpiece = sim.body_transform(sim.workpiece)
                desired_workpiece = desired @ sim.gripper_from_workpiece
                point = np.asarray(command["point"])
                actual_surface = (
                    actual_workpiece[:3, :3] @ point + actual_workpiece[:3, 3]
                )
                desired_surface = (
                    desired_workpiece[:3, :3] @ point + desired_workpiece[:3, 3]
                )
                surface_error = float(
                    np.linalg.norm(actual_surface - desired_surface) * 1000
                )
                surface_errors.append(surface_error)
                held_workpiece = actual @ sim.gripper_from_workpiece
                grasp_error = float(
                    np.linalg.norm(
                        actual_workpiece[:3, :3] @ point
                        + actual_workpiece[:3, 3]
                        - held_workpiece[:3, :3] @ point
                        - held_workpiece[:3, 3]
                    )
                    * 1000
                )
                grasp_errors.append(grasp_error)
                orientation_errors.append(
                    float(
                        np.rad2deg(
                            np.linalg.norm(
                                Rotation.from_matrix(
                                    desired[:3, :3] @ actual[:3, :3].T
                                ).as_rotvec()
                            )
                        )
                    )
                )
            history.append(
                [
                    float(sim.data.time),
                    *sim.data.qpos[sim.q_index],
                    *actual[:3, 3],
                    *target_position,
                    error,
                    force,
                    command["phase"],
                    command["detection_id"],
                    surface_error,
                    grasp_error,
                ]
            )
            next_log += 0.01
        last_phase = command["phase"]
        due_publish = sim.data.time >= next_publish
        if due_publish:
            next_publish += 0.10
        stopped = (
            not due_publish and stop_requested is not None and stop_requested()
        )
        if due_publish or stopped:
            state = sim.state(
                command["phase"],
                progress=min(
                    1.0,
                    (float(sim.data.time) - start_sim)
                    / (times[-1] - start_sim),
                ),
                tracking_error_mm=round(error, 4),
                detection_id=command["detection_id"],
                contact_force_n=round(force, 3),
                mesh_updates=removal.updates(),
            )
            publish(state)
            replay.append(state)
            if due_publish and stop_requested is not None and stop_requested():
                stopped = True
        if stopped:
            interrupted = True
            break
        if due_publish and realtime_factor > 0:
            delay = (float(sim.data.time) - start_sim) / realtime_factor - (
                time.monotonic() - start_wall
            )
            if delay > 0:
                time.sleep(min(delay, 0.05))
    with (sim.output / f"{prefix}execution.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "time_s",
                *[f"q{i}_rad" for i in range(1, 7)],
                "actual_x_m",
                "actual_y_m",
                "actual_z_m",
                "target_x_m",
                "target_y_m",
                "target_z_m",
                "error_mm",
                "tool_force_n",
                "phase",
                "detection_id",
                "surface_tracking_mm",
                "grasp_surface_deflection_mm",
            ]
        )
        writer.writerows(history)
    # Replay uses measured body transforms from integration, not planned poses.
    write_json(
        sim.output / "replay.json",
        replay.document(
            source="post-integration MuJoCo body feedback",
            sample_interval_s=0.1,
        ),
    )
    removal.upload()
    errors = np.asarray(trace_errors)
    result = {
        "simulation_steps": step,
        "simulated_seconds": float(sim.data.time) - start_sim,
        "simulation_start_s": start_sim,
        "tracking_trace_rms_mm": float(np.sqrt(np.mean(errors**2)))
        if len(errors)
        else None,
        "tracking_trace_p95_mm": float(np.quantile(errors, 0.95))
        if len(errors)
        else None,
        "tracking_trace_max_mm": float(errors.max()) if len(errors) else None,
        "tracking_trace_orientation_p95_deg": float(
            np.quantile(orientation_errors, 0.95)
        )
        if orientation_errors
        else None,
        "tracking_reference": "forward kinematics of the exact desired C1 joint curve at the feedback timestamp",
        "unexpected_contact_count": len(unexpected),
        "unexpected_contacts": unexpected[:20],
        "tool_contact_steps": contact_steps,
        "contact_duration_s": float(contact_steps * sim.model.opt.timestep),
        "max_contact_force_n": max_force,
        "interrupted": interrupted,
        "last_phase": last_phase,
        "rigid_grasp_constraint": True,
        "joint_positions_teleported_during_execution": False,
        "material_removal_simulated": mode == "removal",
        "material_removal_model": (
            f"finite-rate swept-{sim.tool.key} normal-depth removal"
            if removal.rate_mm_s is not None
            else f"geometric swept-{sim.tool.key} clipping of generated excess"
        )
        if mode == "removal"
        else None,
        "tool": sim.tool.summary(),
        "force_control_validated": False,
        "controller": "C1 joint interpolation, position servo, velocity and inverse-dynamics acceleration feedforward for robot/held payload; free-workpiece gravity remains active; imported approximate inertia",
        "surface_tracking_p95_mm": float(np.quantile(surface_errors, 0.95))
        if surface_errors
        else None,
        "surface_tracking_max_mm": float(np.max(surface_errors))
        if surface_errors
        else None,
        "grasp_surface_deflection_p95_mm": float(
            np.quantile(grasp_errors, 0.95)
        )
        if grasp_errors
        else None,
        "surface_tracking_scope": "actual nominal workpiece point against FK of the desired joint curve, including gripper and grasp orientation error",
        "removal": removal.summary(),
    }
    write_json(sim.output / f"{prefix}execution.json", result)
    return result, removal
