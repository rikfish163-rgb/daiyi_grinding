"""MuJoCo sensing and FR5 dynamics. No real-robot networking exists here."""

import os

os.environ.setdefault("MUJOCO_GL", "egl")
import xml.etree.ElementTree as ET

import mujoco
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from . import assets, robot_model


def camera_axes(pos, target):
    z = np.asarray(pos) - np.asarray(target)
    z = z / np.linalg.norm(z)
    x = np.cross([0, 0, 1], z)
    if np.linalg.norm(x) < 0.01:
        x = np.array([1.0, 0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return robot_model.s(np.r_[x, y])


def scene_xml(coupon):
    root = ET.Element("mujoco", model="FR5 cast-flange deburring research")
    robot_model.sub(root, "compiler", angle="radian", autolimits="true")
    robot_model.sub(
        root,
        "option",
        timestep=".002",
        integrator="implicitfast",
        iterations="60",
        gravity="0 0 -9.81",
    )
    robot_model.sub(root, "size", memory="96M")
    vis = robot_model.sub(root, "visual")
    robot_model.sub(vis, "global", offwidth="960", offheight="720")
    robot_model.sub(vis, "quality", shadowsize="2048")
    robot_model.sub(vis, "map", znear=".002", zfar="8")
    d = robot_model.sub(root, "default")
    robot_model.sub(d, "joint", damping="2.5", armature=".04")
    robot_model.sub(d, "geom", friction=".6 .005 .001", solref=".015 1")
    asset = robot_model.sub(root, "asset")
    md = assets.fetch()
    for name in assets.NAMES:
        robot_model.sub(asset, "mesh", name=name, file=str(md / f"{name}.STL"))
    robot_model.sub(
        asset,
        "material",
        name="alloy",
        rgba=".48 .51 .54 1",
        specular=".45",
        shininess=".35",
    )
    ny, nx = coupon.height.shape
    robot_model.sub(
        asset,
        "hfield",
        name="casting",
        nrow=ny,
        ncol=nx,
        size=robot_model.s([*coupon.halfsize, 0.05, 0.015]),
    )
    w = robot_model.sub(root, "worldbody")
    robot_model.sub(
        w,
        "light",
        pos="-.2 -.8 1.7",
        dir="-.2 .5 -1",
        diffuse=".9 .88 .85",
        castshadow="true",
    )
    robot_model.sub(
        w, "light", pos="-.9 .4 1.3", diffuse=".4 .5 .6", castshadow="false"
    )
    robot_model.sub(
        w,
        "geom",
        name="floor",
        type="plane",
        size="2 2 .1",
        rgba=".14 .18 .24 1",
    )
    robot_model.sub(
        w,
        "geom",
        name="table",
        type="box",
        pos="-.43 -.07 .515",
        size=".72 .61 .035",
        rgba=".31 .35 .40 1",
    )
    robot_model.sub(
        w,
        "geom",
        name="pedestal",
        type="box",
        pos="0 0 .635",
        size=".095 .095 .085",
        rgba=".24 .28 .34 1",
        contype="0",
        conaffinity="0",
    )
    center = coupon.center
    robot_model.sub(
        w,
        "geom",
        name="fixture",
        type="box",
        pos=robot_model.s([*center[:2], 0.575]),
        size=".145 .11 .025",
        rgba=".19 .24 .31 1",
    )
    part = robot_model.sub(w, "body", name="part", pos=robot_model.s(center))
    robot_model.sub(
        part,
        "geom",
        name="casting_surface",
        type="hfield",
        hfield="casting",
        material="alloy",
    )
    for x in (-0.15, 0.15):
        robot_model.sub(
            w,
            "geom",
            name=f"clamp_{x}",
            type="box",
            pos=robot_model.s([center[0] + x, center[1], 0.614]),
            size=".010 .024 .014",
            rgba=".22 .30 .40 1",
        )
    # Fixed inspection station, matching the independently solved calibration.
    # Parts move between episodes; the camera does not follow their hidden pose.
    cam_pos = [-0.486997708801488, -0.12046686877673, 1.13]
    robot_model.sub(
        w,
        "camera",
        name="inspection",
        pos=robot_model.s(cam_pos),
        xyaxes="1 0 0 0 1 0",
        fovy="26",
    )
    for name, pos, target, fovy in [
        ("overview", [-1.30, -1.4, 1.48], [-0.30, -0.02, 0.81], 49),
        ("closeup", [-0.91, -0.79, 1.01], [*center[:2], 0.63], 32),
    ]:
        robot_model.sub(
            w,
            "camera",
            name=name,
            pos=robot_model.s(pos),
            xyaxes=camera_axes(pos, target),
            fovy=fovy,
        )
    b = robot_model.sub(w, "body", name="base_link", pos="0 0 .72")
    for i, name in enumerate(assets.NAMES):
        if i:
            j, pos, rpy, lim = robot_model.JOINTS[i - 1]
            b = robot_model.sub(
                b,
                "body",
                name=name,
                pos=robot_model.s(pos),
                euler=robot_model.s(rpy),
            )
            robot_model.sub(
                b, "joint", name=j, axis="0 0 1", range=robot_model.s(lim)
            )
        robot_model.sub(
            b,
            "inertial",
            pos="0 0 0",
            mass=robot_model.LINK_MASS[i],
            diaginertia=".01 .01 .01",
        )
        robot_model.sub(
            b,
            "geom",
            name=name + "_visual",
            type="mesh",
            mesh=name,
            rgba=".13 .42 .75 1" if i in (1, 4) else ".86 .89 .93 1",
            contype="0",
            conaffinity="0",
            group="1",
        )
        typ, size, pos = robot_model.COLLISION[i]
        kw = dict(
            name=name + "_collision",
            type=typ,
            size=size,
            pos=pos,
            rgba="0 0 0 0",
            mass="0",
            contype="2",
            conaffinity="3",
            group="3",
        )
        if i in (2, 3):
            kw.pop("pos")
            kw["fromto"] = "0 0 0 -.42 0 0" if i == 2 else "0 0 0 -.39 0 0"
        robot_model.sub(b, "geom", **kw)
    tool = robot_model.sub(b, "body", name="spindle", pos="0 0 .015")
    robot_model.sub(
        tool,
        "geom",
        name="spindle_body",
        type="cylinder",
        size=".024 .048",
        pos="0 0 .050",
        mass=".65",
        rgba=".22 .25 .29 1",
    )
    robot_model.sub(
        tool,
        "geom",
        name="shaft",
        type="cylinder",
        size=".005 .025",
        pos="0 0 .12",
        mass=".06",
        rgba=".68 .7 .74 1",
    )
    robot_model.sub(
        tool,
        "geom",
        name="abrasive",
        type="sphere",
        size=".003",
        pos="0 0 .148",
        mass=".01",
        rgba=".62 .40 .19 1",
    )
    # TCP is the tip of a ball abrasive; this is a nominal tool assumption.
    robot_model.sub(
        tool, "site", name="tcp", pos="0 0 .151", size=".002", rgba=".1 .9 .8 1"
    )
    robot_model.sub(
        tool,
        "camera",
        name="wrist",
        pos=".045 0 .065",
        xyaxes="1 0 0 0 -1 0",
        fovy="70",
    )
    # The fixed base is welded to world by MuJoCo, so automatic parent filtering
    # does not exclude the shoulder/base joint's intentionally overlapping proxies.
    contact = robot_model.sub(root, "contact")
    robot_model.sub(
        contact, "exclude", body1="base_link", body2="shoulder_link"
    )
    act = robot_model.sub(root, "actuator")
    for i in range(6):
        robot_model.sub(
            act,
            "position",
            joint=f"j{i + 1}",
            name=f"arm{i}",
            kp="1800" if i < 3 else "700",
            kv="120" if i < 3 else "45",
            forcerange="-150 150" if i < 3 else "-28 28",
        )
    return ET.tostring(root, encoding="unicode")


class Simulation:
    def __init__(self, coupon):
        self.xml = scene_xml(coupon)
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self.model.hfield_data[:] = coupon.height.ravel() / 0.05
        self.data = mujoco.MjData(self.model)
        self.scratch = mujoco.MjData(self.model)
        self.tcp = self.model.site("tcp").id
        self.renderers = {}
        self.lower = self.model.jnt_range[:, 0] + 0.02
        self.upper = self.model.jnt_range[:, 1] - 0.02
        mujoco.mj_forward(self.model, self.data)
        # A documented initial fixture-relative pose, not a defect location.
        self.home = self.ik(
            np.array([-0.35, 0.13, 0.90]), np.diag([1, -1, -1]), np.zeros(6)
        )[0]
        self.data.qpos[:] = self.home
        mujoco.mj_forward(self.model, self.data)

    def ik(self, xyz, R, seed):
        def fun(q):
            self.scratch.qpos[:] = q
            mujoco.mj_forward(self.model, self.scratch)
            rot = self.scratch.site_xmat[self.tcp].reshape(3, 3)
            return np.r_[
                12 * (self.scratch.site_xpos[self.tcp] - xyz),
                Rotation.from_matrix(R @ rot.T).as_rotvec(),
            ]

        sol = least_squares(
            fun,
            np.clip(seed, self.lower, self.upper),
            bounds=(self.lower, self.upper),
            max_nfev=100,
            ftol=1e-9,
            xtol=1e-9,
            gtol=1e-9,
        )
        e = fun(sol.x)
        pe = float(np.linalg.norm(e[:3]) / 12)
        re = float(np.linalg.norm(e[3:]))
        if pe > 0.0003 or re > np.deg2rad(0.6):
            raise ValueError(
                f"IK failure: {pe * 1000:.3f} mm, {np.rad2deg(re):.3f} deg"
            )
        return sol.x, pe, re

    def render(
        self,
        camera="inspection",
        width=768,
        height=576,
        depth=False,
        paths=None,
    ):
        key = (width, height)
        if key not in self.renderers:
            self.renderers[key] = mujoco.Renderer(
                self.model, height=height, width=width
            )
        r = self.renderers[key]
        if depth:
            r.enable_depth_rendering()
        else:
            r.disable_depth_rendering()
        r.update_scene(self.data, camera=camera)
        if paths is not None and not depth:
            for path in paths:
                pts = np.asarray(path["xyz"])
                for a, b in zip(pts[:-1], pts[1:]):
                    if r.scene.ngeom >= r.scene.maxgeom:
                        break
                    g = r.scene.geoms[r.scene.ngeom]
                    mujoco.mjv_initGeom(
                        g,
                        mujoco.mjtGeom.mjGEOM_CAPSULE,
                        np.zeros(3),
                        np.zeros(3),
                        np.eye(3).ravel(),
                        np.array([1.0, 0.26, 0.5, 0.8]),
                    )
                    mujoco.mjv_connector(
                        g, mujoco.mjtGeom.mjGEOM_CAPSULE, 0.00045, a, b
                    )
                    r.scene.ngeom += 1
        return r.render().copy()

    def camera_info(self, camera="inspection", width=768, height=576):
        c = self.model.camera(camera).id
        f = 0.5 * height / np.tan(np.deg2rad(self.model.cam_fovy[c]) / 2)
        K = np.array(
            [[f, 0, (width - 1) / 2], [0, f, (height - 1) / 2], [0, 0, 1.0]]
        )
        T = np.eye(4)
        # MuJoCo camera -Z forward/+Y up -> OpenCV +Z forward/+Y down.
        T[:3, :3] = self.data.cam_xmat[c].reshape(3, 3) @ np.diag([1, -1, -1])
        T[:3, 3] = self.data.cam_xpos[c]
        return K, T

    def close(self):
        for r in self.renderers.values():
            r.close()
