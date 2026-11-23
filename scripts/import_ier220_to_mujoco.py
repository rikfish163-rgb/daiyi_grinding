#!/usr/bin/env python3
"""Convert ESTUN iER220-2700 workstation, gripper, fixture, and workpieces from STEP to MuJoCo MJCF."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool
from OCP.collections import Sequence_TDF_Label

ROOT = Path("/home/dianrobot/daiyi_grinding")
OUT_DIR = ROOT / "assets" / "ier220_workstation"
MESH_DIR = OUT_DIR / "meshes"

LAYOUT_STP = ROOT / "华科实验平台布局图.stp"
TOOLING_STP = ROOT / "机器人底座打磨定位工装和抓手.stp"
OFFICIAL_ROBOT_STEP_ZIP = (
    ROOT
    / "047393B 1-iER220-2700 Model(1)"
    / "047393B 1-iER220-2700 Model"
    / "iER220-2700 Model(STEP).zip"
)
MANIFEST_PATH = OUT_DIR / "manifest.json"

ROBOT_BASE_CAD = np.array([8860.1343, 723.0, 16494.9073], dtype=np.float64)
FLOOR_TOP_Y_CAD = 753.0
R_STATION_TO_MJ = np.array(
    [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, -1.0, 0.0],
    ],
    dtype=np.float64,
)


def station_pt_transform() -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] = R_STATION_TO_MJ * 0.001
    offset_cad = np.array([ROBOT_BASE_CAD[0], FLOOR_TOP_Y_CAD, ROBOT_BASE_CAD[2]], dtype=np.float64)
    mat[:3, 3] = -(R_STATION_TO_MJ @ offset_cad) * 0.001
    return mat


def robot_link_transform(origin_mm: tuple[float, float, float]) -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] *= 0.001
    mat[:3, 3] = -np.asarray(origin_mm, dtype=np.float64) * 0.001
    return mat


FIXTURE_DATUM_CAD = np.array([2640.0, 588.0, -3332.5], dtype=np.float64)
R_TOOLING_TO_MJ = np.array(
    [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
        [0.0, 1.0, 0.0],
    ],
    dtype=np.float64,
)


def fixture_transform() -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] = R_TOOLING_TO_MJ * 0.001
    mat[:3, 3] = -(R_TOOLING_TO_MJ @ FIXTURE_DATUM_CAD) * 0.001
    return mat


GRIPPER_FLANGE_CAD = np.array([2730.0, 1009.0, -3022.5], dtype=np.float64)
R_GRIPPER_TO_FLANGE = np.array(
    [
        [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
    ],
    dtype=np.float64,
)


def gripper_flange_transform() -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] = R_GRIPPER_TO_FLANGE * 0.001
    mat[:3, 3] = -(R_GRIPPER_TO_FLANGE @ GRIPPER_FLANGE_CAD) * 0.001
    return mat


def get_name(label: TDF_Label) -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def trsf_to_mat(trsf) -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    for r in range(3):
        for c in range(4):
            mat[r, c] = trsf.Value(r + 1, c + 1)
    return mat


def load_step_xcaf(path: Path):
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    status = reader.ReadFile(str(path))
    if status != 1:
        raise RuntimeError(f"Failed to read STEP file {path}: status={status}")
    reader.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    return doc, st, ct


def lookup_color(shape, color_tool, fallback):
    col = Quantity_Color()
    for ctype in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
        if color_tool.GetColor(shape, ctype, col):
            return (round(col.Red(), 2), round(col.Green(), 2), round(col.Blue(), 2))
    return fallback


def collect_colored_tris(
    label: TDF_Label,
    shape_tool,
    color_tool,
    parent_loc: TopLoc_Location,
    parent_rgb: tuple[float, float, float],
    out_buckets: dict[tuple[float, float, float], list[np.ndarray]],
    world_mat: np.ndarray,
    lin_defl: float = 0.4,
    ang_defl: float = 0.5,
):
    ref = TDF_Label()
    if shape_tool.IsReference_s(label):
        shape_tool.GetReferredShape_s(label, ref)
        loc = parent_loc.Multiplied(shape_tool.GetLocation_s(label))
        target = ref
    else:
        loc = parent_loc
        target = label

    raw_shape = shape_tool.GetShape_s(target)
    cur_rgb = lookup_color(raw_shape, color_tool, parent_rgb)

    comps = Sequence_TDF_Label()
    shape_tool.GetComponents_s(target, comps)
    if comps.Length() > 0:
        for i in range(1, comps.Length() + 1):
            collect_colored_tris(
                comps.Value(i),
                shape_tool,
                color_tool,
                loc,
                cur_rgb,
                out_buckets,
                world_mat,
                lin_defl=lin_defl,
                ang_defl=ang_defl,
            )
        return

    if raw_shape.IsNull():
        return

    BRepMesh_IncrementalMesh(raw_shape, lin_defl, False, ang_defl, True)
    loc_mat = trsf_to_mat(loc.Transformation())

    exp = TopExp_Explorer(raw_shape, TopAbs_FACE)
    while exp.More():
        face = exp.Current()
        face_rgb = lookup_color(face, color_tool, cur_rgb)
        floc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(TopoDS.Face(face), floc)
        if tri is not None and tri.NbTriangles() > 0:
            f_mat = trsf_to_mat(floc.Transformation())
            full_mat = world_mat @ loc_mat @ f_mat
            n_nodes = tri.NbNodes()
            pts = np.empty((n_nodes, 3), dtype=np.float64)
            for idx in range(1, n_nodes + 1):
                nd = tri.Node(idx)
                pts[idx - 1, 0] = nd.X()
                pts[idx - 1, 1] = nd.Y()
                pts[idx - 1, 2] = nd.Z()
            pts = pts @ full_mat[:3, :3].T + full_mat[:3, 3]
            n_tris = tri.NbTriangles()
            t_idx = np.empty((n_tris, 3), dtype=np.int32)
            rev = face.Orientation() == TopAbs_REVERSED
            if np.linalg.det(full_mat[:3, :3]) < 0:
                rev = not rev
            for t in range(1, n_tris + 1):
                a, b, c = tri.Triangle(t).Get()
                if rev:
                    t_idx[t - 1] = (a - 1, c - 1, b - 1)
                else:
                    t_idx[t - 1] = (a - 1, b - 1, c - 1)
            out_buckets.setdefault(face_rgb, []).append(pts[t_idx])
        exp.Next()


def write_binary_stl(path: Path, tris: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    tris = tris.astype(np.float32, copy=False)
    n = len(tris)
    v1 = tris[:, 1] - tris[:, 0]
    v2 = tris[:, 2] - tris[:, 0]
    norms = np.cross(v1, v2)
    lens = np.linalg.norm(norms, axis=1, keepdims=True)
    norms = np.divide(norms, lens, out=np.zeros_like(norms), where=lens > 1e-12)
    rec = np.zeros(
        n,
        dtype=[
            ("n", "<f4", (3,)),
            ("v0", "<f4", (3,)),
            ("v1", "<f4", (3,)),
            ("v2", "<f4", (3,)),
            ("attr", "<u2"),
        ],
    )
    rec["n"] = norms
    rec["v0"] = tris[:, 0]
    rec["v1"] = tris[:, 1]
    rec["v2"] = tris[:, 2]
    with open(path, "wb") as f:
        f.write(b"Binary STL converted from STEP for MuJoCo".ljust(80, b" "))
        f.write(struct.pack("<I", n))
        f.write(rec.tobytes())


def export_buckets(buckets: dict, folder: Path, prefix: str, min_tris: int = 1):
    results = []
    items = []
    for rgb, arrs in buckets.items():
        tris = np.concatenate(arrs, axis=0)
        if len(tris) >= min_tris:
            items.append((rgb, tris))
    items.sort(key=lambda x: -len(x[1]))
    for idx, (rgb, tris) in enumerate(items):
        fname = f"{prefix}_c{idx}.stl"
        fpath = folder / fname
        write_binary_stl(fpath, tris)
        results.append((fname, fpath, rgb, len(tris), tris.reshape(-1, 3)))
    return results


def classify_station_group(comp_name: str) -> str | None:
    if "IER220" in comp_name:
        return None
    if comp_name == "地面":
        return "floor"
    if comp_name == "机器人安装板":
        return "pedestal_plate"
    if "上料台" in comp_name:
        return "loading_station"
    if "砂带机" in comp_name:
        return "belt_grinder"
    if "stiffspindle" in comp_name:
        return "stiff_spindle"
    if "工具台" in comp_name:
        return "tool_tables"
    if "cloumn" in comp_name:
        return "columns"
    if "AT-S1000" in comp_name:
        return "vision_sensor"
    if "电柜" in comp_name or "Cabinet" in comp_name:
        return "cabinets"
    if "网片" in comp_name or "立柱" in comp_name or "safetylock" in comp_name:
        return "safety_fences"
    if "DUMMY" in comp_name:
        return "operator_dummy"
    return "misc"


def build_mjcf(
    station_assets: dict,
    robot_assets: dict,
    gripper_assets: list,
    fixture_assets: list,
    workpiece_assets: dict,
):
    def sub(parent, tag, **attrs):
        return ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})

    def camera_axes(pos, target):
        z = np.asarray(pos, dtype=float) - np.asarray(target, dtype=float)
        z = z / np.linalg.norm(z)
        x = np.cross([0.0, 0.0, 1.0], z)
        if np.linalg.norm(x) < 0.05:
            x = np.array([1.0, 0.0, 0.0])
        x /= np.linalg.norm(x)
        y = np.cross(z, x)
        return " ".join(f"{v:.5f}" for v in np.r_[x, y])

    def add_mesh_geoms(body, items, mat_prefix, contype=0, conaffinity=0, group=1, name_prefix="g"):
        for idx, _ in enumerate(items):
            sub(
                body,
                "geom",
                name=f"{name_prefix}_{idx}",
                type="mesh",
                mesh=f"{mat_prefix}_m{idx}",
                material=f"{mat_prefix}_mat{idx}",
                contype=contype,
                conaffinity=conaffinity,
                group=group,
            )

    def register_meshes(asset_el, items, mat_prefix, rel_dir):
        for idx, item in enumerate(items):
            fname, rgb = item[0], item[2]
            sub(asset_el, "mesh", name=f"{mat_prefix}_m{idx}", file=f"{rel_dir}/{fname}", inertia="shell")
            sub(
                asset_el,
                "material",
                name=f"{mat_prefix}_mat{idx}",
                rgba=f"{rgb[0]:.2f} {rgb[1]:.2f} {rgb[2]:.2f} 1",
                specular="0.35",
                shininess="0.3",
            )

    def write_xml(path: Path, root: ET.Element):
        ET.indent(root, space="  ")
        path.write_text('<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")

    joints = [
        ("link1", "j1", (0.0, 0.0, 0.0), (0, 0, 1), (-math.pi, math.pi), 450.0, 90000, 9000, 25000),
        ("link2", "j2", (0.285, 0.0, 0.688), (0, 1, 0), (math.radians(-60), math.radians(80)), 222.0, 140000, 14000, 35000),
        ("link3", "j3", (0.0, 0.0, 1.150), (0, 1, 0), (math.radians(-180), math.radians(80)), 140.0, 100000, 10000, 25000),
        ("link4", "j4", (0.0, 0.0, 0.230), (1, 0, 0), (math.radians(-250), math.radians(250)), 125.0, 35000, 3000, 10000),
        ("link5", "j5", (1.265, 0.0, 0.0), (0, 1, 0), (math.radians(-125), math.radians(125)), 86.0, 25000, 2000, 8000),
        ("link6", "j6", (0.240, 0.0, 0.0), (1, 0, 0), (math.radians(-360), math.radians(360)), 50.0, 15000, 1200, 5000),
    ]

    link_collisions = {
        "link1": ("cylinder", "0.06 0 0.56", "0.35 0.32", None),
        "link2": ("capsule", None, "0.18", "0 0 0 0 0 1.150"),
        "link3": ("box", "0.10 0 0.15", "0.24 0.25 0.24", None),
        "link4": ("capsule", None, "0.14", "0.35 0 0 1.265 0 0"),
        "link5": ("capsule", None, "0.11", "0 0 0 0.20 0 0"),
        "link6": ("cylinder", "-0.014 0 0", "0.085 0.015", None),
    }

    def build_full_scene(wp_key: str, out_xml: Path, free_workpiece: bool = False):
        suffix = "_GraspSim" if free_workpiece else ""
        root = ET.Element("mujoco", model=f"ESTUN_iER220_2700_Workstation_{wp_key}{suffix}")
        sub(root, "compiler", angle="radian", autolimits="true", meshdir="meshes")
        sub(root, "option", timestep="0.002", integrator="implicitfast", iterations="50", gravity="0 0 -9.81")
        sub(root, "size", memory="128M")
        vis = sub(root, "visual")
        sub(vis, "global", offwidth="1600", offheight="1200")
        sub(vis, "quality", shadowsize="2048")
        sub(vis, "map", znear="0.02", zfar="25")

        dflt = sub(root, "default")
        sub(dflt, "joint", damping="40.0", armature="1.5", frictionloss="2.0")
        sub(dflt, "geom", friction="0.7 0.01 0.002", solref="0.015 1")

        asset = sub(root, "asset")
        for grp, items in station_assets.items():
            register_meshes(asset, items, f"st_{grp}", "station")
        for lname, items in robot_assets.items():
            register_meshes(asset, items, f"rob_{lname}", "robot")
        register_meshes(asset, gripper_assets, "grip", "gripper")
        register_meshes(asset, fixture_assets, "fix", "fixture")
        register_meshes(asset, workpiece_assets[wp_key], f"wp_{wp_key}", "workpiece")

        wb = sub(root, "worldbody")
        sub(wb, "light", pos="-1.5 -2.5 5.0", dir="0.2 0.3 -1", diffuse="0.95 0.94 0.92", castshadow="true")
        sub(wb, "light", pos="2.5 2.5 4.0", dir="-0.4 -0.4 -1", diffuse="0.45 0.5 0.55", castshadow="false")

        sub(wb, "camera", name="overview", pos="2.6 -3.8 2.9", xyaxes="0.80 0.60 0 -0.26 0.35 0.90", fovy="48")
        sub(wb, "camera", name="workstation_front", pos="-4.2 -3.2 2.5", xyaxes="0.62 -0.78 0 0.25 0.20 0.95", fovy="46")
        sub(wb, "camera", name="loading_closeup", pos="0.65 -2.95 1.55", xyaxes="0.75 0.66 0 -0.30 0.34 0.89", fovy="42")
        sub(wb, "camera", name="robot_side", pos="0.0 -3.9 1.65", xyaxes="1 0 0 0 0 1", fovy="44")
        sub(wb, "camera", name="grinder_closeup", pos="-0.1 -1.85 1.95", xyaxes=camera_axes([-0.1, -1.85, 1.95], [-1.7, -0.3, 1.15]), fovy="46")
        sub(wb, "camera", name="vision_scanner", pos="-0.48 -2.14 1.24", xyaxes=camera_axes([-0.48, -2.14, 1.24], [-0.40, -1.80, 0.78]), fovy="52")

        sub(wb, "geom", name="physics_floor", type="plane", pos="0 0 0", size="6 6 0.1", rgba="0.22 0.24 0.28 1", contype="1", conaffinity="3")
        for grp, items in station_assets.items():
            add_mesh_geoms(wb, items, f"st_{grp}", contype=0, conaffinity=0, group=1, name_prefix=f"st_{grp}")

        # Station obstacle collision proxies (group=3) and functional station sites
        sub(wb, "geom", name="loading_pallet_col", type="box", pos="-0.400 -1.800 0.5935", size="0.275 0.275 0.0075", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="loading_base_col", type="box", pos="-0.400 -2.200 0.295", size="0.695 0.675 0.295", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="loading_gantry_col", type="box", pos="-0.400 -2.200 0.890", size="0.695 0.060 0.300", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="belt_grinder_base_col", type="box", pos="-1.825 0.295 0.430", size="0.575 0.512 0.420", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="belt_grinder_col_col", type="box", pos="-1.975 0.295 1.290", size="0.425 0.430 0.440", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="belt_grinder_wheel_col", type="cylinder", pos="-1.250 0.295 1.150", quat="0.70710678 0.70710678 0 0", size="0.140 0.120", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="stiff_spindle_stand_col", type="box", pos="-1.970 -1.125 0.625", size="0.195 0.140 0.625", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="stiff_spindle_head_col", type="cylinder", pos="-1.710 -1.125 1.135", quat="0.70710678 0 0.70710678 0", size="0.095 0.070", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="tool_tables_col", type="box", pos="0.029 1.327 0.437", size="0.882 0.250 0.437", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="columns_col", type="box", pos="-1.708 -2.192 0.690", size="0.452 0.472 0.690", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wb, "geom", name="cabinets_col", type="box", pos="0.576 1.997 1.052", size="0.964 0.392 1.052", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")

        sub(wb, "site", name="loading_pallet_center", pos="-0.400 -1.800 0.601", size="0.020", rgba="0.2 0.8 1.0 0.9")
        sub(wb, "site", name="belt_grinder_contact", pos="-1.150 0.295 1.150", size="0.025", rgba="1.0 0.5 0.1 0.9")
        sub(wb, "site", name="stiff_spindle_tip", pos="-1.642 -1.125 1.135", size="0.022", rgba="1.0 0.85 0.1 0.9")

        rob = sub(wb, "body", name="ier220_base", pos="0 0 0.030", euler="0 0 3.14159265")
        sub(rob, "inertial", pos="0 0 0.14", mass="247.0", diaginertia="10 10 10")
        add_mesh_geoms(rob, robot_assets["base"], "rob_base", contype=0, conaffinity=0, group=1, name_prefix="rob_base")
        sub(rob, "geom", name="rob_base_col", type="cylinder", pos="0 0 0.14", size="0.38 0.14", rgba="0 0 0 0", group="3", contype="2", conaffinity="3")

        parent = rob
        for lname, jname, pos, axis, rng, mass, kp, kv, fr in joints:
            b = sub(parent, "body", name=f"ier220_{lname}", pos=f"{pos[0]:.4f} {pos[1]:.4f} {pos[2]:.4f}", gravcomp="1")
            sub(b, "joint", name=jname, type="hinge", axis=f"{axis[0]} {axis[1]} {axis[2]}", range=f"{rng[0]:.5f} {rng[1]:.5f}")
            sub(b, "inertial", pos="0 0 0", mass=f"{mass:.1f}", diaginertia="5 5 5")
            add_mesh_geoms(b, robot_assets[lname], f"rob_{lname}", contype=0, conaffinity=0, group=1, name_prefix=f"rob_{lname}")
            ctype, cpos, csize, cfromto = link_collisions[lname]
            ckw = dict(name=f"rob_{lname}_col", type=ctype, size=csize, rgba="0 0 0 0", group="3", contype="2", conaffinity="3")
            if cpos is not None:
                ckw["pos"] = cpos
            if cfromto is not None:
                ckw["fromto"] = cfromto
            if lname == "link6":
                ckw["quat"] = "0.70710678 0 0.70710678 0"
            sub(b, "geom", **ckw)
            parent = b

        sub(parent, "site", name="flange", pos="0 0 0", quat="0 0.70710678 0 0.70710678", size="0.025", rgba="0.1 0.9 0.9 0.8")

        grip = sub(parent, "body", name="grinding_gripper", pos="0 0 0", quat="0 0.70710678 0 0.70710678", gravcomp="1")
        sub(grip, "inertial", pos="-0.08 0 0.30", mass="42.0", diaginertia="1.2 1.2 1.2")
        add_mesh_geoms(grip, gripper_assets, "grip", contype=0, conaffinity=0, group=1, name_prefix="grip")
        sub(grip, "geom", name="grip_board_col", type="box", pos="0 0 0.015", size="0.11 0.13 0.016", rgba="0 0 0 0", group="3", contype="2", conaffinity="3")
        sub(grip, "geom", name="grip_frame_col", type="box", pos="-0.01 0.03 0.28", size="0.09 0.18 0.25", rgba="0 0 0 0", group="3", contype="2", conaffinity="3")
        sub(grip, "site", name="gripper_tcp", pos="-0.171 -0.140 0.310", size="0.020", rgba="1 0.2 0.2 0.9")
        sub(grip, "camera", name="wrist_cam", pos="0.11 0 0.08", xyaxes="0 1 0 -0.8 0 0.6", fovy="65")

        fix = sub(wb, "body", name="base_fixture", pos="-0.400 -1.800 0.601")
        add_mesh_geoms(fix, fixture_assets, "fix", contype=0, conaffinity=0, group=1, name_prefix="fix")
        sub(fix, "geom", name="fix_plate_col", type="box", pos="-0.0675 0 0.008", size="0.398 0.350 0.008", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(fix, "geom", name="fix_ring_col", type="cylinder", pos="0 0 0.042", size="0.12 0.030", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")

        wp = sub(wb, "body", name=f"workpiece_{wp_key}", pos="-0.400 -1.800 0.601")
        if free_workpiece:
            sub(wp, "freejoint", name="workpiece_free")
        sub(wp, "inertial", pos="-0.02 0 0.20", mass="65.0", diaginertia="1.5 1.5 1.5")
        add_mesh_geoms(wp, workpiece_assets[wp_key], f"wp_{wp_key}", contype=0, conaffinity=0, group=1, name_prefix="wp")
        sub(wp, "geom", name="wp_flange_col", type="box", pos="-0.03 0 0.107", size="0.215 0.185 0.035", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wp, "geom", name="wp_hub_col", type="cylinder", pos="-0.03 0 0.255", size="0.165 0.115", rgba="0 0 0 0", group="3", contype="1", conaffinity="3")
        sub(wp, "site", name="workpiece_datum", pos="0 0 0.071", size="0.015", rgba="0.2 1 0.3 0.9")
        sub(wp, "site", name="workpiece_top", pos="-0.03 0 0.370", size="0.015", rgba="0.2 1 0.3 0.9")

        contact = sub(root, "contact")
        for b1, b2 in [
            ("ier220_base", "ier220_link1"),
            ("ier220_link1", "ier220_link2"),
            ("ier220_link2", "ier220_link3"),
            ("ier220_link3", "ier220_link4"),
            ("ier220_link4", "ier220_link5"),
            ("ier220_link5", "ier220_link6"),
            ("ier220_link6", "grinding_gripper"),
        ]:
            sub(contact, "exclude", body1=b1, body2=b2)

        act = sub(root, "actuator")
        for _, jname, _, _, rng, _, kp, kv, fr in joints:
            sub(act, "position", name=f"act_{jname}", joint=jname, kp=f"{kp}", kv=f"{kv}", forcerange=f"-{fr} {fr}", ctrlrange=f"{rng[0]:.5f} {rng[1]:.5f}")

        write_xml(out_xml, root)

    def build_tooling_scene(out_xml: Path):
        root = ET.Element("mujoco", model="Base_Gripper_Fixture_Workpieces_Inspection")
        sub(root, "compiler", angle="radian", autolimits="true", meshdir="meshes")
        sub(root, "option", timestep="0.002", gravity="0 0 -9.81")
        vis = sub(root, "visual")
        sub(vis, "global", offwidth="1600", offheight="1200")
        sub(vis, "map", znear="0.01", zfar="10")

        asset = sub(root, "asset")
        register_meshes(asset, gripper_assets, "grip", "gripper")
        register_meshes(asset, fixture_assets, "fix", "fixture")
        for wk in ("097", "021", "surface"):
            register_meshes(asset, workpiece_assets[wk], f"wp_{wk}", "workpiece")

        wb = sub(root, "worldbody")
        sub(wb, "light", pos="1.5 -1.5 2.5", dir="-0.4 0.4 -0.8", diffuse="0.95 0.95 0.95", castshadow="true")
        sub(wb, "light", pos="-1.5 1.5 2.0", dir="0.4 -0.4 -0.8", diffuse="0.5 0.55 0.6", castshadow="false")
        sub(wb, "camera", name="iso_assembly", pos="0.85 -1.35 0.85", xyaxes="0.82 0.57 0 -0.22 0.31 0.92", fovy="44")
        sub(wb, "camera", name="side_compare", pos="0.20 -2.15 0.45", xyaxes="1 0 0 0 0 1", fovy="38")
        sub(wb, "geom", name="table_plane", type="plane", pos="0 0 0", size="3 3 0.1", rgba="0.18 0.21 0.25 1")

        asm = sub(wb, "body", name="assembled_station", pos="-0.60 0 0")
        add_mesh_geoms(asm, fixture_assets, "fix", name_prefix="fix")
        add_mesh_geoms(asm, workpiece_assets["097"], "wp_097", name_prefix="wp097_asm")
        grip_in_fix = sub(asm, "body", name="gripper_mated", pos="0.090 -0.310 0.421", quat="0.5 0.5 0.5 -0.5")
        add_mesh_geoms(grip_in_fix, gripper_assets, "grip", name_prefix="grip_asm")

        b021 = sub(wb, "body", name="workpiece_021_standalone", pos="0.25 0 0")
        add_mesh_geoms(b021, workpiece_assets["021"], "wp_021", name_prefix="wp021")
        bsurf = sub(wb, "body", name="workpiece_surface_standalone", pos="0.85 0 0")
        add_mesh_geoms(bsurf, workpiece_assets["surface"], "wp_surface", name_prefix="wpsurf")

        write_xml(out_xml, root)

    build_full_scene("097", OUT_DIR / "scene.xml")
    build_full_scene("021", OUT_DIR / "scene_workpiece_021.xml")
    build_full_scene("097", OUT_DIR / "scene_grasp_sim.xml", free_workpiece=True)
    build_tooling_scene(OUT_DIR / "subassembly_tooling.xml")


def save_manifest(station_assets, robot_assets, gripper_assets, fixture_assets, workpiece_assets):
    def ser(items):
        return [[x[0], str(x[1]), list(x[2]), int(x[3])] for x in items]

    payload = {
        "station": {k: ser(v) for k, v in station_assets.items()},
        "robot": {k: ser(v) for k, v in robot_assets.items()},
        "gripper": ser(gripper_assets),
        "fixture": ser(fixture_assets),
        "workpiece": {k: ser(v) for k, v in workpiece_assets.items()},
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manifest():
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    def deser(items):
        return [(x[0], Path(x[1]), tuple(x[2]), x[3], None) for x in items]
    return (
        {k: deser(v) for k, v in data["station"].items()},
        {k: deser(v) for k, v in data["robot"].items()},
        deser(data["gripper"]),
        deser(data["fixture"]),
        {k: deser(v) for k, v in data["workpiece"].items()},
    )


def export_part10_into_buckets(robot_buckets: dict):
    if not OFFICIAL_ROBOT_STEP_ZIP.exists():
        return
    tmp = Path("/tmp/ier220_part10_tmp.step")
    try:
        with zipfile.ZipFile(OFFICIAL_ROBOT_STEP_ZIP) as zf:
            tmp.write_bytes(zf.read("iER220-2700 Model.STEP"))
        _, st_off, ct_off = load_step_xcaf(tmp)
        roots = Sequence_TDF_Label()
        st_off.GetFreeShapes(roots)
        comps = Sequence_TDF_Label()
        st_off.GetComponents_s(roots.Value(1), comps)
        for i in range(1, comps.Length() + 1):
            cl = comps.Value(i)
            ref = TDF_Label()
            st_off.GetReferredShape_s(cl, ref)
            if "iER220-2700-10" in get_name(ref):
                collect_colored_tris(
                    cl,
                    st_off,
                    ct_off,
                    TopLoc_Location(),
                    (1.0, 1.0, 1.0),
                    robot_buckets["link1"],
                    robot_link_transform((0.0, 0.0, 0.0)),
                    lin_defl=0.30,
                )
                print("  Added iER220-2700-10 into robot/link1")
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Convert STEP workstation & tooling into MuJoCo MJCF")
    parser.add_argument("--xml-only", action="store_true", help="Regenerate XMLs from manifest.json without re-meshing STEPs")
    args = parser.parse_args()

    if args.xml_only and MANIFEST_PATH.exists():
        station_assets, robot_assets, gripper_assets, fixture_assets, workpiece_assets = load_manifest()
        build_mjcf(station_assets, robot_assets, gripper_assets, fixture_assets, workpiece_assets)
        print(f"Regenerated MJCFs from {MANIFEST_PATH}")
        return

    t_start = time.time()
    raw_dir = MESH_DIR / "robot_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)

    print("1/3 Reading 华科实验平台布局图.stp...")
    _, st1, ct1 = load_step_xcaf(LAYOUT_STP)
    roots1 = Sequence_TDF_Label()
    st1.GetFreeShapes(roots1)
    comps1 = Sequence_TDF_Label()
    st1.GetComponents_s(roots1.Value(1), comps1)

    st_mat = station_pt_transform()
    station_buckets: dict[str, dict] = {}
    robot_buckets: dict[str, dict] = {k: {} for k in ("base", "link1", "link2", "link3", "link4", "link5", "link6")}

    robot_part_map = {
        "IER220-2700-01": ("base", (0.0, 0.0, 0.0)),
        "IER220-2700-02": ("link1", (0.0, 0.0, 0.0)),
        "IER220-2700-04": ("link1", (0.0, 0.0, 0.0)),
        "IER220-2700-05": ("link1", (0.0, 0.0, 0.0)),
        "IER220-2700-03": ("link2", (285.0, 0.0, 688.0)),
        "IER220-2700-11": ("link2", (285.0, 0.0, 688.0)),
        "IER220-2700-12": ("link2", (285.0, 0.0, 688.0)),
        "IER220-2700-06": ("link3", (285.0, 0.0, 1838.0)),
        "IER220-2700-07": ("link4", (285.0, 0.0, 2068.0)),
        "IER220-2700-08": ("link5", (1550.0, 0.0, 2068.0)),
        "IER220-2700-09": ("link6", (1790.0, 0.0, 2068.0)),
    }

    for i in range(1, comps1.Length() + 1):
        cl = comps1.Value(i)
        cname = get_name(cl)
        ref = TDF_Label()
        st1.GetReferredShape_s(cl, ref)
        rname = get_name(ref)
        if "IER220" in rname or "IER220" in cname:
            sub1 = Sequence_TDF_Label()
            st1.GetComponents_s(ref, sub1)
            s1 = sub1.Value(1)
            r1_lbl = TDF_Label()
            st1.GetReferredShape_s(s1, r1_lbl)
            loc1 = st1.GetLocation_s(s1)
            sub2 = Sequence_TDF_Label()
            st1.GetComponents_s(r1_lbl, sub2)
            for m in range(1, sub2.Length() + 1):
                s2 = sub2.Value(m)
                r2_lbl = TDF_Label()
                st1.GetReferredShape_s(s2, r2_lbl)
                pname = get_name(r2_lbl)
                matched = None
                for key, val in robot_part_map.items():
                    if key in pname:
                        matched = val
                        break
                if matched is None:
                    continue
                link_name, orig_mm = matched
                collect_colored_tris(
                    s2,
                    st1,
                    ct1,
                    loc1,
                    (0.85, 0.45, 0.10),
                    robot_buckets[link_name],
                    robot_link_transform(orig_mm),
                    lin_defl=0.30,
                )
            export_part10_into_buckets(robot_buckets)
        else:
            grp = classify_station_group(cname)
            if grp is None:
                continue
            bkt = station_buckets.setdefault(grp, {})
            defl = 1.0 if grp in ("floor", "safety_fences", "cabinets", "operator_dummy") else 0.5
            collect_colored_tris(
                cl,
                st1,
                ct1,
                TopLoc_Location(),
                (0.65, 0.68, 0.72),
                bkt,
                st_mat,
                lin_defl=defl,
            )

    print("2/3 Exporting station & robot meshes...")
    station_assets = {grp: export_buckets(bkt, MESH_DIR / "station", grp) for grp, bkt in station_buckets.items() if bkt}
    robot_assets = {lname: export_buckets(bkt, MESH_DIR / "robot", lname) for lname, bkt in robot_buckets.items()}
    for grp, items in station_assets.items():
        print(f"  station/{grp}: {sum(x[3] for x in items)} tris across {len(items)} color meshes")
    for lname, items in robot_assets.items():
        print(f"  robot/{lname}: {sum(x[3] for x in items)} tris across {len(items)} color meshes")

    print("3/3 Reading 机器人底座打磨定位工装和抓手.stp...")
    _, st2, ct2 = load_step_xcaf(TOOLING_STP)
    roots2 = Sequence_TDF_Label()
    st2.GetFreeShapes(roots2)
    comps2 = Sequence_TDF_Label()
    st2.GetComponents_s(roots2.Value(1), comps2)

    gripper_bkt: dict = {}
    fixture_bkt: dict = {}
    wp_bkts: dict[str, dict] = {"021": {}, "097": {}, "surface": {}}

    fix_mat = fixture_transform()
    grip_mat = gripper_flange_transform()

    for i in range(1, comps2.Length() + 1):
        cl = comps2.Value(i)
        cname = get_name(cl)
        if "抓手" in cname:
            collect_colored_tris(cl, st2, ct2, TopLoc_Location(), (0.55, 0.58, 0.62), gripper_bkt, grip_mat, lin_defl=0.25)
        elif "夹具" in cname:
            collect_colored_tris(cl, st2, ct2, TopLoc_Location(), (0.45, 0.50, 0.56), fixture_bkt, fix_mat, lin_defl=0.25)
        elif "工件" in cname:
            ref = TDF_Label()
            st2.GetReferredShape_s(cl, ref)
            loc_wp = st2.GetLocation_s(cl)
            sub1 = Sequence_TDF_Label()
            st2.GetComponents_s(ref, sub1)
            for j in range(1, sub1.Length() + 1):
                sc1 = sub1.Value(j)
                r1 = TDF_Label()
                st2.GetReferredShape_s(sc1, r1)
                loc_base = loc_wp.Multiplied(st2.GetLocation_s(sc1))
                sub2 = Sequence_TDF_Label()
                st2.GetComponents_s(r1, sub2)
                for k in range(1, sub2.Length() + 1):
                    sc2 = sub2.Value(k)
                    pname = get_name(sc2)
                    r2 = TDF_Label()
                    st2.GetReferredShape_s(sc2, r2)
                    r2name = get_name(r2)
                    tag = f"{pname} {r2name}"
                    if "021" in tag:
                        wk, defcol = "021", (0.60, 0.60, 0.60)
                    elif "097" in tag:
                        wk, defcol = "097", (0.95, 0.55, 0.12)
                    else:
                        wk, defcol = "surface", (0.25, 0.75, 0.85)
                    collect_colored_tris(sc2, st2, ct2, loc_base, defcol, wp_bkts[wk], fix_mat, lin_defl=0.20)

    gripper_assets = export_buckets(gripper_bkt, MESH_DIR / "gripper", "gripper")
    fixture_assets = export_buckets(fixture_bkt, MESH_DIR / "fixture", "fixture")
    workpiece_assets = {wk: export_buckets(bkt, MESH_DIR / "workpiece", f"wp_{wk}") for wk, bkt in wp_bkts.items()}
    print(f"  gripper: {sum(x[3] for x in gripper_assets)} tris across {len(gripper_assets)} color meshes")
    print(f"  fixture: {sum(x[3] for x in fixture_assets)} tris across {len(fixture_assets)} color meshes")
    for wk, items in workpiece_assets.items():
        print(f"  workpiece/{wk}: {sum(x[3] for x in items)} tris across {len(items)} color meshes")

    save_manifest(station_assets, robot_assets, gripper_assets, fixture_assets, workpiece_assets)
    build_mjcf(station_assets, robot_assets, gripper_assets, fixture_assets, workpiece_assets)
    print(f"Done in {time.time() - t_start:.1f}s -> {OUT_DIR}")


if __name__ == "__main__":
    main()
