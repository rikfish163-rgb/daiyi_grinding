"""Time-scaled, continuously stepped MuJoCo joint servo execution and auditing."""

import csv
import json
from pathlib import Path

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .planning import path_length


def waypoints(paths, home_xyz, home_R):
    result = [
        dict(
            xyz=np.array(home_xyz),
            R=home_R,
            speed=0.07,
            phase="home",
            detection_id=0,
        )
    ]
    for p in paths:
        R = np.array(p["rotation"])
        start = np.array(p["approach"])
        end = np.array(p["retract"])
        # Global clearance above all admitted coupon geometry and fixture clamps.
        old = result[-1]["xyz"].copy()
        old[2] = max(0.72, old[2])
        above = start.copy()
        above[2] = max(0.72, above[2])
        for point, phase in [
            (old, "transfer"),
            (above, "transfer"),
            (start, "approach"),
        ]:
            result.append(
                dict(
                    xyz=point,
                    R=R,
                    speed=0.07,
                    phase=phase,
                    detection_id=p["detection_id"],
                )
            )
        for point_index, pt in enumerate(p["xyz"]):
            result.append(
                dict(
                    xyz=np.array(pt),
                    R=R,
                    speed=p["recipe"]["feed_mm_s"] / 1000,
                    phase="approach" if point_index == 0 else "trace",
                    detection_id=p["detection_id"],
                )
            )
        result.append(
            dict(
                xyz=end,
                R=R,
                speed=0.04,
                phase="retract",
                detection_id=p["detection_id"],
            )
        )
    old = result[-1]["xyz"].copy()
    old[2] = max(0.72, old[2])
    result.append(
        dict(xyz=old, R=home_R, speed=0.06, phase="retract", detection_id=0)
    )
    result.append(
        dict(
            xyz=np.array(home_xyz),
            R=home_R,
            speed=0.07,
            phase="home",
            detection_id=0,
        )
    )
    return result


def make_commands(sim, paths):
    points = waypoints(
        paths,
        sim.data.site_xpos[sim.tcp].copy(),
        sim.data.site_xmat[sim.tcp].reshape(3, 3).copy(),
    )
    commands = []
    seed = sim.home.copy()
    now = 0.0
    max_p = 0.0
    max_r = 0.0
    for a, b in zip(points, points[1:]):
        dist = np.linalg.norm(b["xyz"] - a["xyz"])
        rdist = np.linalg.norm(
            Rotation.from_matrix(b["R"] @ a["R"].T).as_rotvec()
        )
        # Cartesian sampling <= 2 mm and <= 2 degrees before IK.
        n = max(1, int(np.ceil(max(dist / 0.002, rdist / np.deg2rad(2)))))
        rotations = Slerp([0, 1], Rotation.from_matrix([a["R"], b["R"]]))
        duration = max(dist / b["speed"], rdist / 0.25, 0.025)
        for i in range(1, n + 1):
            u = i / n
            p = a["xyz"] * (1 - u) + b["xyz"] * u
            R = rotations(u).as_matrix()
            q, pe, re = sim.ik(p, R, seed)
            # Further slow down if a joint would exceed the research limit.
            dt = max(
                duration / n, float(np.max(np.abs(q - seed))) / 0.45, 0.008
            )
            now += dt
            commands.append(
                dict(
                    t=now,
                    q=q.copy(),
                    xyz=p.copy(),
                    R=R,
                    phase=b["phase"],
                    detection_id=b["detection_id"],
                )
            )
            seed = q
            max_p = max(max_p, pe)
            max_r = max(max_r, re)
    return commands, dict(
        ik_max_mm=max_p * 1000,
        ik_max_deg=np.rad2deg(max_r),
        duration_s=now,
        sample_count=len(commands),
        trace_length_mm=path_length(paths) * 1000,
    )


def collision_pairs(sim, data):
    pairs = []
    for c in data.contact:
        a = mujoco.mj_id2name(
            sim.model, mujoco.mjtObj.mjOBJ_GEOM, int(c.geom1)
        ) or str(c.geom1)
        b = mujoco.mj_id2name(
            sim.model, mujoco.mjtObj.mjOBJ_GEOM, int(c.geom2)
        ) or str(c.geom2)
        if {a, b} == {"abrasive", "casting_surface"}:
            continue
        pairs.append((a, b, float(c.dist)))
    return pairs


def preflight(sim, commands):
    bad = []
    max_joint_delta = 0.0
    previous = sim.home
    for k, c in enumerate(commands):
        max_joint_delta = max(
            max_joint_delta, float(np.max(np.abs(c["q"] - previous)))
        )
        # Endpoints plus interpolation midpoint; poses are sampled <=2 mm above.
        for u in (0.5, 1.0):
            sim.scratch.qpos[:] = previous * (1 - u) + c["q"] * u
            mujoco.mj_forward(sim.model, sim.scratch)
            for a, b, d in collision_pairs(sim, sim.scratch):
                if d < -0.0001:
                    bad.append(
                        dict(index=k, pair=[a, b], penetration_mm=-d * 1000)
                    )
        previous = c["q"]
    return dict(
        passed=not bad,
        unexpected_contacts=bad[:25],
        contact_count=len(bad),
        max_joint_step_deg=np.rad2deg(max_joint_delta),
        scope="sampled checks using conservative primitive link proxies; not certified collision clearance",
    )


def execute(sim, commands, out, frame_callback=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    times = np.array([0.0, *[c["t"] for c in commands]])
    joints = np.vstack([sim.home, *[c["q"] for c in commands]])
    positions = np.vstack(
        [sim.data.site_xpos[sim.tcp].copy(), *[c["xyz"] for c in commands]]
    )
    i = 1
    step = 0
    next_frame = 0.0
    errors = []
    trace_errors = []
    orientation_errors = []
    bad = []
    forces = []
    history = []
    tool_contact_steps = 0
    max_force = 0.0
    rotations = Rotation.from_matrix(
        [
            sim.data.site_xmat[sim.tcp].reshape(3, 3).copy(),
            *[c["R"] for c in commands],
        ]
    )
    rotation_interp = Slerp(times, rotations)
    kp = sim.model.actuator_gainprm[:, 0]
    kv = -sim.model.actuator_biasprm[:, 2]
    dt = sim.model.opt.timestep
    end = times[-1] + 0.3
    while sim.data.time < end:
        t = min(float(sim.data.time), times[-1])
        while i < len(times) - 1 and times[i] < t:
            i += 1
        u = np.clip(
            (t - times[i - 1]) / max(times[i] - times[i - 1], 1e-9), 0, 1
        )
        q = joints[i - 1] * (1 - u) + joints[i] * u
        qdot = (
            (joints[i] - joints[i - 1]) / max(times[i] - times[i - 1], 1e-9)
            if t < times[-1]
            else np.zeros(6)
        )
        target = positions[i - 1] * (1 - u) + positions[i] * u
        c = commands[i - 1]
        sim.data.ctrl[:] = q + (kv + sim.model.dof_damping) * qdot / kp
        # Feed-forward model gravity, no qpos teleport during execution.
        sim.data.qfrc_applied[:] = sim.data.qfrc_bias[:]
        mujoco.mj_step(sim.model, sim.data)
        if not np.isfinite(sim.data.qpos).all():
            raise RuntimeError("Nonfinite simulation state")
        # Refresh post-integration pose AND compare to target at the same time.
        mujoco.mj_forward(sim.model, sim.data)
        sample_t = min(float(sim.data.time), times[-1])
        j = min(
            int(np.searchsorted(times, sample_t, side="left")), len(times) - 1
        )
        j = max(1, j)
        v = np.clip(
            (sample_t - times[j - 1]) / max(times[j] - times[j - 1], 1e-9), 0, 1
        )
        target = positions[j - 1] * (1 - v) + positions[j] * v
        c = commands[j - 1]
        force = 0.0
        contact_seen = False
        for ci, contact in enumerate(sim.data.contact):
            if sim.model.geom("abrasive").id in (contact.geom1, contact.geom2):
                f = np.zeros(6)
                mujoco.mj_contactForce(sim.model, sim.data, ci, f)
                force += max(0.0, f[0])
                contact_seen = True
        tool_contact_steps += int(contact_seen)
        max_force = max(max_force, float(force))
        for a, b, d in collision_pairs(sim, sim.data):
            if d < -0.0001:
                bad.append([float(sim.data.time), a, b, -d * 1000])
        step += 1
        if step % 5 == 0:
            actual = sim.data.site_xpos[sim.tcp].copy()
            err = float(np.linalg.norm(actual - target)) * 1000
            errors.append(err)
            if c["phase"] == "trace":
                trace_errors.append(err)
                R = sim.data.site_xmat[sim.tcp].reshape(3, 3)
                orientation_errors.append(
                    float(
                        np.rad2deg(
                            np.linalg.norm(
                                Rotation.from_matrix(
                                    rotation_interp(sample_t).as_matrix() @ R.T
                                ).as_rotvec()
                            )
                        )
                    )
                )
            forces.append(force)
            history.append(
                [
                    float(sim.data.time),
                    *actual,
                    *target,
                    err,
                    force,
                    c["phase"],
                    c["detection_id"],
                ]
            )
        if frame_callback is not None and sim.data.time >= next_frame:
            frame_callback(sim, c, float(sim.data.time), history)
            next_frame += 1 / 12
    with (out / "execution.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "time_s",
                "actual_x_m",
                "actual_y_m",
                "actual_z_m",
                "target_x_m",
                "target_y_m",
                "target_z_m",
                "error_mm",
                "contact_force_n",
                "phase",
                "detection_id",
            ]
        )
        w.writerows(history)
    err = np.asarray(trace_errors)
    report = dict(
        simulation_steps=step,
        simulated_seconds=float(sim.data.time),
        tracking_trace_rms_mm=float(np.sqrt(np.mean(err**2)))
        if len(err)
        else None,
        tracking_trace_p95_mm=float(np.quantile(err, 0.95))
        if len(err)
        else None,
        tracking_trace_max_mm=float(err.max()) if len(err) else None,
        tracking_trace_orientation_p95_deg=float(
            np.quantile(orientation_errors, 0.95)
        )
        if orientation_errors
        else None,
        unexpected_contact_count=len(bad),
        unexpected_contacts=bad[:20],
        max_contact_force_n=max_force,
        abrasive_contact_steps=tool_contact_steps,
        contact_sampling_ms=dt * 1000,
        logging="post-integration FK and contemporaneous interpolated target; TCP statistics sampled each 10ms",
        material_removal_simulated=False,
        force_control_validated=False,
        controller="MuJoCo position servo + desired-velocity and model gravity feedforward; not FR5 controller emulation",
    )
    (out / "execution.json").write_text(json.dumps(report, indent=2))
    return report
