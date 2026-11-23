#!/usr/bin/env python3
"""Verify generated ESTUN iER220-2700 MuJoCo scenes, free-body physics, IK reachability, and render previews."""

from __future__ import annotations

import os

os.environ.setdefault("MUJOCO_GL", "egl")

from pathlib import Path

import mujoco
import numpy as np
from PIL import Image

OUT_DIR = Path("/home/dianrobot/daiyi_grinding/assets/ier220_workstation")


def solve_site_ik(model: mujoco.MjModel, data: mujoco.MjData, site_name: str, target_pos: np.ndarray, q_init: np.ndarray, n_iter: int = 120):
    data.qpos[:6] = q_init.copy()
    sid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, site_name)
    jacp = np.zeros((3, model.nv))
    for _ in range(n_iter):
        mujoco.mj_forward(model, data)
        err = target_pos - data.site_xpos[sid]
        if np.linalg.norm(err) < 2e-3:
            break
        mujoco.mj_jacSite(model, data, jacp, None, sid)
        J = jacp[:, :6]
        dq = J.T @ np.linalg.solve(J @ J.T + 1e-3 * np.eye(3), err)
        data.qpos[:6] = np.clip(
            data.qpos[:6] + 0.5 * dq,
            model.jnt_range[:6, 0] + 0.02,
            model.jnt_range[:6, 1] - 0.02,
        )
    mujoco.mj_forward(model, data)
    return data.qpos[:6].copy(), float(np.linalg.norm(target_pos - data.site_xpos[sid]))


def robot_obstacle_contacts(model: mujoco.MjModel, data: mujoco.MjData) -> list[tuple[str, str, float]]:
    hits = []
    for cidx in range(data.ncon):
        con = data.contact[cidx]
        g1 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, con.geom1) or str(con.geom1)
        g2 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, con.geom2) or str(con.geom2)
        if ("rob_" in g1 or "grip_" in g1) or ("rob_" in g2 or "grip_" in g2):
            hits.append((g1, g2, float(con.dist)))
    return hits


def main():
    for fname in ("scene.xml", "scene_workpiece_021.xml", "scene_grasp_sim.xml", "subassembly_tooling.xml"):
        m = mujoco.MjModel.from_xml_path(str(OUT_DIR / fname))
        d = mujoco.MjData(m)
        mujoco.mj_forward(m, d)
        print(f"Loaded {fname:26s} | nbody={m.nbody:2d} njnt={m.njnt} ngeom={m.ngeom:3d}")

    mg = mujoco.MjModel.from_xml_path(str(OUT_DIR / "scene_grasp_sim.xml"))
    dg = mujoco.MjData(mg)
    mujoco.mj_forward(mg, dg)
    wp_jnt_id = mujoco.mj_name2id(mg, mujoco.mjtObj.mjOBJ_JOINT, "workpiece_free")
    qadr = mg.jnt_qposadr[wp_jnt_id]
    z0 = float(dg.qpos[qadr + 2])
    for _ in range(500):
        mujoco.mj_step(mg, dg)
    z1 = float(dg.qpos[qadr + 2])
    print(f"scene_grasp_sim settling (1.0s): z_init={z0:.4f} -> z_settled={z1:.4f} (dz={z1 - z0:+.4f} m), ncon={dg.ncon}")

    m = mujoco.MjModel.from_xml_path(str(OUT_DIR / "scene.xml"))
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)

    waypoints = [
        ("loading_pregrasp", np.array([-0.40, -1.80, 1.22]), np.array([1.35, 0.20, 0.30, 0.0, 0.75, 0.0]), "loading_closeup"),
        ("belt_grinder_work", np.array([-0.68, 0.295, 1.22]), np.array([-0.15, 0.15, 0.45, 0.0, 0.65, 0.0]), "grinder_closeup"),
        ("stiff_spindle_work", np.array([-1.05, -1.125, 1.16]), np.array([0.65, 0.20, 0.45, 0.0, 0.60, 0.0]), "grinder_closeup"),
    ]

    r = mujoco.Renderer(m, 900, 1200)
    r.update_scene(d, camera="vision_scanner")
    img_scanner = r.render()

    panel_imgs = [img_scanner]
    for label, target_pos, q_seed, cam in waypoints:
        d_ik = mujoco.MjData(m)
        q_sol, ik_err = solve_site_ik(m, d_ik, "gripper_tcp", target_pos, q_seed)
        if label == "belt_grinder_work":
            q_mid = np.array([0.45, -0.35, 0.25, 0.0, 0.85, 0.0])
            q0 = d.qpos[:6].copy()
            for k in range(400):
                a = 0.5 * (1.0 - np.cos(np.pi * (k + 1) / 400))
                d.ctrl[:6] = (1.0 - a) * q0 + a * q_mid
                mujoco.mj_step(m, d)
        q_start = d.qpos[:6].copy()
        for k in range(600):
            a = 0.5 * (1.0 - np.cos(np.pi * (k + 1) / 600))
            d.ctrl[:6] = (1.0 - a) * q_start + a * q_sol
            mujoco.mj_step(m, d)
        for _ in range(150):
            mujoco.mj_step(m, d)
        tcp_actual = d.site("gripper_tcp").xpos.copy()
        hits = robot_obstacle_contacts(m, d)
        print(
            f"  {label:20s} | ik_err={ik_err*1000:5.2f} mm | tcp={np.round(tcp_actual,3)} "
            f"| track_err={np.max(np.abs(d.qpos[:6]-q_sol)):.4f} rad | robot_contacts={len(hits)}"
        )
        r.update_scene(d, camera=cam)
        panel_imgs.append(r.render())

    r.close()
    top = np.concatenate([panel_imgs[0], panel_imgs[1]], axis=1)
    bot = np.concatenate([panel_imgs[2], panel_imgs[3]], axis=1)
    Image.fromarray(np.concatenate([top, bot], axis=0)).save(str(OUT_DIR / "preview_stations_ik.png"))
    print(f"Saved {OUT_DIR / 'preview_stations_ik.png'}")


if __name__ == "__main__":
    main()
