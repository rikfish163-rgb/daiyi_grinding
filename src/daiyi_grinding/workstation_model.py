"""The supplied ESTUN cell, with separate tooling and CAD grasp frames.

All dimensions are meters. CAD visual meshes are preserved; collision meshes
are convex pieces derived from the same CAD, not controller-certified geometry.
"""

from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from . import project
from .cad_defects import camera_axes
from .casting_defects import CASTING_CASES, casting_appearance
from .casting_defects import CONFIG_FILE as CASTING_CONFIG_FILE
from .casting_profiles import (
    BASE_PROFILE,
    load_profile,
    profile_hashes,
    sensor_for_profile,
)
from .learned_defects import registered_model_hashes
from .workstation_jobs import JOB_FILE
from .workstation_replay import ReplayRecorder
from .workpiece_packages import load_cell_package, load_workpiece_package

ASSETS = project.ROOT / "assets/ier220_workstation"
SOURCE_SCENE = ASSETS / "scene.xml"
WORKPIECE_MESH = ASSETS / "meshes/workpiece/wp_097_c0.stl"
MODEL_VERSION = 3
CELL_CONFIG_FILE = project.ROOT / "config/workstation.json"
CELL_PACKAGE = load_cell_package(CELL_CONFIG_FILE)
CELL_CONFIG = CELL_PACKAGE.config
DEFAULT_WORKPIECE_PACKAGE = load_workpiece_package()
SOURCE_FILES = (
    "workstation_model.py",
    "workpiece_packages.py",
    "workstation_motion.py",
    "workstation_run.py",
    "workstation_benchmark.py",
    "workstation_web.py",
    "cad_defects.py",
    "thin_reference.py",
    "planning.py",
    "perception.py",
    "workstation_inspection.py",
    "workstation_planning.py",
    "workstation_quality.py",
    "workstation_regrind.py",
    "workstation_jobs.py",
    "workstation_delivery.py",
    "workstation_transfers.py",
    "workstation_evaluation.py",
    "workstation_reuse.py",
    "workstation_defects.py",
    "casting_defects.py",
    "casting_profiles.py",
    "team_data_reuse.py",
    "casting_scan_calibration.py",
    "workstation_tooling.py",
    "learned_defects.py",
    "defect_network.py",
    "defect_training.py",
)

# Public default-frame constants remain the 097 package's CAD datum frames.
GRIPPER_FROM_WORKPIECE, LOADING_ROTATION = DEFAULT_WORKPIECE_PACKAGE.grasp_frames()
WORKPIECE_FROM_GRIPPER = np.linalg.inv(GRIPPER_FROM_WORKPIECE)
LOADING_POSITION = DEFAULT_WORKPIECE_PACKAGE.loading_position.copy()
WHEEL_CENTER = np.array([-1.651, -1.125, 1.156])
WHEEL_RADIUS = 0.0225
WHEEL_HALF_WIDTH = 0.009


GRASP_SETUPS = tuple(setup.name for setup in DEFAULT_WORKPIECE_PACKAGE.grasp_setups)
CAMERA_SETUPS = ("original", "low_side_simulation")


def grasp_frames(name, workpiece_id="097"):
    """Resolve declared grasp frames while retaining the default 097 call."""
    return load_workpiece_package(workpiece_id).grasp_frames(name)


def inspection_camera_config(name="original", *, cell_package=None):
    """Resolve one fixed virtual camera placement independently of defects.

    The lower placement observes outer CAD walls below the upper flange. Both
    placements keep the configured target, optics, sensor noise and table views.
    These positions are simulation assumptions pending real-camera calibration.

    Args:
        name: ``original`` or ``low_side_simulation``.

    Returns:
        This run's camera settings in world coordinates and meters.

    Raises:
        ValueError: The named placement is not registered.
    """
    if name not in CAMERA_SETUPS:
        raise ValueError("未登记的离线相机配置")
    camera = (cell_package or CELL_PACKAGE).config["camera"]
    return {
        **camera,
        "position_world_m": [
            *camera["position_world_m"][:2],
            1.200
            if name == "low_side_simulation"
            else camera["position_world_m"][2],
        ],
        "target_world_m": list(camera["target_world_m"]),
    }


COMPONENTS = [
    {
        "id": "robot",
        "label": "ESTUN iER220-2700 机械臂",
        "role": "六轴运动机构",
        "color": "#ec7d21",
    },
    {
        "id": "robot_base",
        "label": "机械臂安装底座",
        "role": "固定机械臂与安装板，不是待打磨工件",
        "color": "#7894ae",
    },
    {
        "id": "gripper",
        "label": "6102152200 底座打磨抓手",
        "role": "夹持并搬运工件，不是打磨工具",
        "color": "#478eb8",
    },
    {
        "id": "fixture",
        "label": "6102152210 底座定位夹具",
        "role": "上料与定位，固定在上料台",
        "color": "#7e72a6",
    },
    {
        "id": "workpiece",
        "label": "待打磨底座铸件 · CAD 097",
        "role": "加工对象；批缝和毛刺只附着在该工件上",
        "color": "#d6ad67",
    },
    {
        "id": "defects",
        "label": "工件批缝 / 局部毛刺",
        "role": "本次生成的缺陷几何；检测从相机深度计算",
        "color": "#e25161",
    },
    {
        "id": "tool",
        "label": "固定砂带加工面",
        "role": "当前参与加工的固定磨具；与抓手、工件分离",
        "color": "#50aa97",
    },
    {
        "id": "spindle",
        "label": "固定主轴与支架",
        "role": "支撑打磨轮，位置来自整站 CAD",
        "color": "#90a2b3",
    },
    {
        "id": "belt_grinder",
        "label": "砂带机",
        "role": "支撑固定砂带加工面，位置保持整站 CAD 布置",
        "color": "#6f8498",
    },
    {
        "id": "loading",
        "label": "上料台",
        "role": "定位夹具的支撑设备",
        "color": "#597da8",
    },
    {
        "id": "sensor",
        "label": "固定 3D 相机与视觉传感器",
        "role": "固定在世界坐标中；按转台编码器角度采集 RGB-D",
        "color": "#5aaec0",
    },
    {
        "id": "turntable",
        "label": "六视角采集转台",
        "role": "独立旋转机构；60° 分度、编码器读数，尺寸为仿真配置",
        "color": "#478ea0",
    },
    {
        "id": "environment",
        "label": "立柱、工具台与电柜",
        "role": "整站静态环境与避障对象",
        "color": "#8a95a1",
    },
    {
        "id": "fences",
        "label": "围栏与人员模型",
        "role": "整站布局；可隐藏以查看加工过程",
        "color": "#a6acb1",
    },
]


def write_json(path, value):
    """Atomically publish a state file while the browser is polling it."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def source_hashes(workpiece_id="097"):
    package = load_workpiece_package(workpiece_id)
    return {
        **{
            name: hashlib.sha256(
                (project.SOURCE / name).read_bytes()
            ).hexdigest()
            for name in SOURCE_FILES
            if (project.SOURCE / name).exists()
        },
        package.source_scene.name: hashlib.sha256(package.source_scene.read_bytes()).hexdigest(),
        package.mesh_path.name: hashlib.sha256(package.mesh_path.read_bytes()).hexdigest(),
        package.path.name: hashlib.sha256(package.path.read_bytes()).hexdigest(),
        "workstation.json": hashlib.sha256(
            CELL_CONFIG_FILE.read_bytes()
        ).hexdigest(),
        "workstation_jobs.json": hashlib.sha256(
            JOB_FILE.read_bytes()
        ).hexdigest(),
        "casting_synthesis.json": hashlib.sha256(
            CASTING_CONFIG_FILE.read_bytes()
        ).hexdigest(),
        **profile_hashes(),
        **registered_model_hashes(),
    }


def component_for_geom(name):
    """A single semantic mapping shared by rendering, picking and auditing."""
    if name.startswith("added_defect_"):
        return "defects"
    if name.startswith("wp_"):
        return "workpiece"
    if name.startswith("fix_"):
        return "fixture"
    if name.startswith("turntable_"):
        return "turntable"
    if name.startswith("camera_"):
        return "sensor"
    if name.startswith(("rob_base_", "st_pedestal_plate_")):
        return "robot_base"
    if name.startswith("rob_"):
        return "robot"
    if name.startswith(("grip_", "gripper_proxy_")):
        return "gripper"
    if name.startswith("belt_abrasive") or name == "abrasive_belt":
        return "tool"
    if name in {"st_stiff_spindle_8", "abrasive_wheel"}:
        return "spindle"
    if name.startswith("belt_front_proxy"):
        return "belt_grinder"
    if name.startswith(("st_stiff_spindle_", "spindle_proxy_")):
        return "spindle"
    if name.startswith(("st_belt_grinder_", "belt_grinder_")):
        return "belt_grinder"
    if name.startswith(("st_loading_station_", "loading_")):
        return "loading"
    if name.startswith("st_vision_sensor_"):
        return "sensor"
    if name.startswith(("st_safety_fences_", "st_operator_dummy_")):
        return "fences"
    return "environment"


def _numbers(values):
    return " ".join(f"{float(value):.9g}" for value in values)


def _quaternion(matrix):
    return Rotation.from_matrix(matrix).as_quat()[[3, 0, 1, 2]]


def _convex_proxy(points, path):
    """Export a small convex collision piece, retaining its CAD coordinates."""
    import trimesh
    from scipy.spatial import ConvexHull, QhullError

    # STL stores float32 coordinates. Reject slices that collapse to a plane
    # after that serialization, before giving MuJoCo an invalid collision mesh.
    points = np.unique(
        np.asarray(points, dtype=np.float32).astype(float).reshape(-1, 3),
        axis=0,
    )
    if len(points) < 4 or np.prod(np.ptp(points, axis=0)) < 2e-8:
        return False
    try:
        hull = ConvexHull(points)
    except QhullError:
        return False
    if hull.volume < 1e-12:
        return False
    proxy = trimesh.Trimesh(points, hull.simplices, process=False)
    proxy.fix_normals()
    proxy.export(path)
    return True


def _clip_triangles(triangles, normal, origin):
    """Clip CAD triangles to a half-space without filling holes in the casting."""
    normal = np.asarray(normal)
    distances = (triangles - origin) @ normal
    inside = distances >= -1e-10
    complete = triangles[inside.all(axis=1)]
    crossing = triangles[inside.any(axis=1) & ~inside.all(axis=1)]
    clipped = []
    for triangle in crossing:
        polygon = []
        for a, b in zip(triangle, np.roll(triangle, -1, axis=0)):
            da, db = np.dot(a - origin, normal), np.dot(b - origin, normal)
            if da >= -1e-10:
                polygon.append(a)
            if (da >= -1e-10) != (db >= -1e-10):
                polygon.append(a + (b - a) * da / (da - db))
        for index in range(1, len(polygon) - 1):
            clipped.append([polygon[0], polygon[index], polygon[index + 1]])
    if clipped:
        return np.concatenate([complete, np.asarray(clipped)])
    return complete


def prepare_collision_assets():
    """Replace the cavity-filling gripper box with separate CAD convex pieces."""
    import trimesh

    cache = ASSETS / "collision_v2"
    manifest_file = cache / "manifest.json"
    if manifest_file.is_file():
        return json.loads(manifest_file.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    manifest = {"gripper": [], "spindle": [], "workpiece": []}
    for group, paths in (
        ("gripper", sorted((ASSETS / "meshes/gripper").glob("*.stl"))),
        (
            "spindle",
            [
                ASSETS / f"meshes/station/stiff_spindle_c{i}.stl"
                for i in (0, 1, 2)
            ],
        ),
    ):
        for path in paths:
            mesh = trimesh.load_mesh(path, process=True)
            pieces = trimesh.graph.connected_components(
                mesh.face_adjacency,
                nodes=np.arange(len(mesh.faces)),
                engine="scipy",
            )
            for faces in pieces:
                if len(faces) < 20:
                    continue
                points = mesh.vertices[mesh.faces[faces]].reshape(-1, 3)
                if np.max(np.ptp(points, axis=0)) < 0.025:
                    continue
                chunks = [mesh.triangles[faces]]
                # The spindle's housing and narrow shaft share vertices. A
                # single hull would invent a tapered solid around that shaft.
                if group == "spindle" and np.ptp(points[:, 0]) > 0.15:
                    bounds = np.linspace(
                        points[:, 0].min() - 1e-7,
                        points[:, 0].max() + 1e-7,
                        int(np.ceil(np.ptp(points[:, 0]) / 0.035)) + 1,
                    )
                    chunks = []
                    for low, high in zip(bounds[:-1], bounds[1:]):
                        chunk = _clip_triangles(
                            mesh.triangles[faces], [1, 0, 0], [low, 0, 0]
                        )
                        chunks.append(
                            _clip_triangles(chunk, [-1, 0, 0], [high, 0, 0])
                        )
                for chunk in chunks:
                    target = cache / f"{group}_{len(manifest[group])}.stl"
                    if _convex_proxy(chunk.reshape(-1, 3), target):
                        manifest[group].append(str(target.relative_to(ASSETS)))

    # Angular and height cuts preserve the casting's hollow center much better
    # than the previous solid cylinder. Each clipped piece uses a convex hull.
    mesh = trimesh.load_mesh(WORKPIECE_MESH, process=True)
    center = np.array([-0.028, 0.0, 0.0])
    for angle in np.linspace(0, 2 * np.pi, 13)[:-1]:
        first = np.array([-np.sin(angle), np.cos(angle), 0])
        end = angle + 2 * np.pi / 12
        second = np.array([np.sin(end), -np.cos(end), 0])
        wedge = _clip_triangles(mesh.triangles, first, center)
        wedge = _clip_triangles(wedge, second, center)
        for low, high in ((0.07, 0.17), (0.17, 0.31), (0.31, 0.371)):
            piece = _clip_triangles(wedge, [0, 0, 1], [0, 0, low])
            piece = _clip_triangles(piece, [0, 0, -1], [0, 0, high])
            target = cache / f"workpiece_{len(manifest['workpiece'])}.stl"
            if _convex_proxy(piece.reshape(-1, 3), target):
                manifest["workpiece"].append(str(target.relative_to(ASSETS)))
    write_json(manifest_file, manifest)
    return manifest


def prepare_belt_collision_assets():
    """Resolve the old invented roller proxy against the supplied front CAD.

    Spatially clipped hulls retain the two separated grinding heads and their
    openings. The rear cabinet/base proxies from the importer stay in place.
    """
    import trimesh

    cache = ASSETS / "collision_belt_v4"
    manifest_file = cache / "manifest.json"
    source = ASSETS / "meshes/station/belt_grinder_c0.stl"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if manifest_file.exists():
        saved = json.loads(manifest_file.read_text())
        if saved["source_sha256"] == digest:
            return saved
    cache.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.load_mesh(source, process=True)
    parts = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), engine="scipy"
    )
    files = []
    for faces in parts:
        if len(faces) < 10:
            continue
        chunks = [mesh.triangles[faces]]
        for axis in range(3):
            refined = []
            for chunk in chunks:
                points = chunk.reshape(-1, 3)
                low, high = points[:, axis].min(), points[:, axis].max()
                if high - low < 0.35:
                    refined.append(chunk)
                    continue
                breaks = np.linspace(
                    low - 1e-7,
                    high + 1e-7,
                    int(np.ceil((high - low) / 0.12)) + 1,
                )
                normal = np.eye(3)[axis]
                for first, last in zip(breaks[:-1], breaks[1:]):
                    piece = _clip_triangles(chunk, normal, normal * first)
                    piece = _clip_triangles(piece, -normal, normal * last)
                    if len(piece):
                        refined.append(piece)
            chunks = refined
        for piece in chunks:
            target = cache / f"front_{len(files)}.stl"
            if _convex_proxy(piece.reshape(-1, 3), target):
                files.append(str(target.relative_to(ASSETS)))
    record = {
        "source": str(source.relative_to(ASSETS)),
        "source_sha256": digest,
        "pieces": files,
        "scope": "individual connected CAD solids; long solids clipped into 120 mm cells before convexification",
    }
    write_json(manifest_file, record)
    return record


def prepare_workpiece_collision_assets(workpiece_package=None, output=None):
    """Preserve the selected CAD steps; new packages keep proxies in run storage."""
    import trimesh

    package = load_workpiece_package(workpiece_package or "097")
    cache = package.collision_cache
    if cache is None:
        if output is None:
            raise ValueError("新工件碰撞网格需要独立运行输出目录")
        cache = Path(output) / f"collision_workpiece_{package.workpiece_id}_v3"
    manifest_file = cache / "manifest.json"
    digest = hashlib.sha256(package.mesh_path.read_bytes()).hexdigest()
    if manifest_file.is_file():
        saved = json.loads(manifest_file.read_text())
        if (
            saved["source_sha256"] == digest
            and saved.get("schema_version") == 3
        ):
            return saved
    cache.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.load_mesh(package.mesh_path, process=True)
    horizontal = (np.abs(mesh.face_normals[:, 2]) > 0.999999) & (
        np.ptp(mesh.triangles[:, :, 2], axis=1) < 1e-7
    )
    heights = mesh.triangles[horizontal, :, 2].mean(axis=1)
    rounded, groups = np.unique(np.round(heights, 6), return_inverse=True)
    areas = np.bincount(groups, weights=mesh.area_faces[horizontal])
    major_planes = [
        float(np.median(heights[groups == index]))
        for index, height in enumerate(rounded)
        if areas[index] > mesh.area * 0.01 and height > mesh.bounds[:, 2].mean()
    ]
    levels = sorted({*package.collision_slice_boundaries_m, *major_planes})
    center = np.r_[mesh.bounds[:, :2].mean(axis=0), 0]
    files = []
    for angle in np.linspace(0, 2 * np.pi, package.collision_angular_slices + 1)[:-1]:
        end = angle + 2 * np.pi / package.collision_angular_slices
        wedge = _clip_triangles(
            mesh.triangles, [-np.sin(angle), np.cos(angle), 0], center
        )
        wedge = _clip_triangles(wedge, [np.sin(end), -np.cos(end), 0], center)
        for low, high in zip(levels[:-1], levels[1:]):
            piece = _clip_triangles(wedge, [0, 0, 1], [0, 0, low])
            piece = _clip_triangles(piece, [0, 0, -1], [0, 0, high])
            # A large flange face belongs to the lower slice. Including it in
            # the raised-boss slice would recreate the very wedge we removed.
            if low in major_planes:
                coplanar = np.max(np.abs(piece[:, :, 2] - low), axis=1) < 1e-7
                piece = piece[~coplanar]
            if not len(piece):
                continue
            chunks = [piece]
            if major_planes and low >= min(major_planes):
                cell_size = (
                    0.005
                    if len(major_planes) > 1 and low >= major_planes[-2]
                    else 0.03
                )
                for axis in (0, 1):
                    refined = []
                    normal = np.eye(3)[axis]
                    for chunk in chunks:
                        minimum = chunk[:, :, axis].min()
                        maximum = chunk[:, :, axis].max()
                        if maximum - minimum <= cell_size * 1.5:
                            refined.append(chunk)
                            continue
                        boundaries = np.linspace(
                            minimum - 1e-7,
                            maximum + 1e-7,
                            int(np.ceil((maximum - minimum) / cell_size)) + 1,
                        )
                        for first, last in zip(boundaries[:-1], boundaries[1:]):
                            cell = _clip_triangles(
                                chunk, normal, normal * first
                            )
                            cell = _clip_triangles(cell, -normal, normal * last)
                            if len(cell):
                                refined.append(cell)
                    chunks = refined
            for chunk in chunks:
                clipped = trimesh.Trimesh(
                    vertices=chunk.reshape(-1, 3),
                    faces=np.arange(len(chunk) * 3).reshape(-1, 3),
                    process=True,
                )
                solids = trimesh.graph.connected_components(
                    clipped.face_adjacency,
                    nodes=np.arange(len(clipped.faces)),
                    engine="scipy",
                )
                for faces in solids:
                    target = cache / f"workpiece_{len(files)}.stl"
                    points = clipped.triangles[faces].reshape(-1, 3)
                    if _convex_proxy(points, target):
                        files.append(str(target.relative_to(ASSETS)) if target.is_relative_to(ASSETS) else str(target.resolve()))
    record = {
        "schema_version": 3,
        "source": str(package.mesh_path.relative_to(ASSETS)),
        "source_sha256": digest,
        "major_horizontal_cad_planes_m": major_planes,
        "slice_boundaries_m": levels,
        "upper_cell_size_m": 0.03,
        "raised_rim_cell_size_m": 0.005,
        "pieces": files,
        "scope": "angular slices split at dominant horizontal CAD planes; upper solids partitioned into 30 mm cells and the raised rim into 5 mm cells; coplanar flange faces excluded from raised-boss hulls",
    }
    write_json(manifest_file, record)
    return record


def build_scene(
    output,
    seed=7007,
    case="mixed",
    tool_key="belt",
    casting_profile=BASE_PROFILE,
    grasp_setup="original",
    camera_config=None,
    *,
    workpiece_id="097",
    workpiece_package=None,
    cell_package=None,
):
    """Compose the declared workpiece, shared cell and independent excess meshes."""
    package = load_workpiece_package(workpiece_package or workpiece_id)
    cell = cell_package or package.cell
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    gripper_from_workpiece, loading_rotation = package.grasp_frames(grasp_setup)
    root = ET.parse(package.source_scene).getroot()
    root.set("model", "ESTUN_iER220_current_grinding_cell")
    compiler = root.find("compiler")
    meshdir = package.source_scene.parent / compiler.attrib.pop("meshdir")
    assets = root.find("asset")
    if case in CASTING_CASES:
        appearance = casting_appearance(seed)
        material = assets.find(f"./material[@name='{package.material_name}']")
        material.set("rgba", _numbers(appearance["rgba"]))
        material.set("specular", str(appearance["specular"]))
        material.set("shininess", str(appearance["shininess"]))
    for mesh in assets.findall("mesh"):
        mesh.set("file", str(meshdir / mesh.attrib["file"]))
    world = root.find("worldbody")
    bodies = {}
    # Station meshes were previously all attached to world. Give each assembly
    # its own body without moving its baked world-coordinate geometry.
    for geom in list(world.findall("geom")):
        name = geom.get("name", "")
        if not name.startswith("st_"):
            continue
        key = component_for_geom(name)
        if name == "st_stiff_spindle_8":
            continue
        body = bodies.setdefault(key, None)
        if body is None:
            body = ET.SubElement(world, "body", name=f"cell_{key}")
            bodies[key] = body
        world.remove(geom)
        body.append(geom)
    wheel = ET.SubElement(
        world, "body", name="grinding_wheel", pos=_numbers(WHEEL_CENTER)
    )
    ET.SubElement(
        wheel,
        "joint",
        name="spindle_rotation",
        axis="1 0 0",
        limited="false",
        damping="0.01",
        armature="0.001",
        frictionloss="0",
    )
    ET.SubElement(
        wheel,
        "inertial",
        pos="0 0 0",
        mass="0.2",
        diaginertia="0.00005 0.00003 0.00003",
    )
    visual = world.find("geom[@name='st_stiff_spindle_8']")
    world.remove(visual)
    visual.set("pos", _numbers(-WHEEL_CENTER))
    wheel.append(visual)
    ET.SubElement(
        wheel,
        "geom",
        name="abrasive_wheel",
        type="cylinder",
        size=_numbers([WHEEL_RADIUS, WHEEL_HALF_WIDTH]),
        quat="0.707106781 0 0.707106781 0",
        rgba="0 0 0 0",
        group="3",
        contype="4",
        conaffinity="10",
        mass="0",
    )
    ET.SubElement(
        world,
        "site",
        name="tool_contact",
        pos=_numbers(WHEEL_CENTER + [0, 0, -WHEEL_RADIUS]),
        size="0.003",
        rgba="0.2 0.9 0.7 1",
    )
    ET.SubElement(
        root.find("actuator"),
        "velocity",
        name="spindle_speed",
        joint="spindle_rotation",
        kv="0.05",
        ctrlrange="-250 250",
        forcerange="-5 5",
    )
    tool = cell.fixed_tool(tool_key)
    belt = cell.fixed_tool("belt")
    surface = ET.SubElement(world, "body", name="fixed_abrasive_belt")
    ET.SubElement(
        surface,
        "geom",
        name="abrasive_belt",
        type="box",
        pos=_numbers(belt.contact - [belt.half_size[0], 0, 0]),
        size=_numbers(belt.half_size),
        rgba="0.16 0.52 0.43 1",
        contype="4",
        conaffinity="10",
        mass="0",
    )
    for offset in np.linspace(-0.065, 0.065, 12):
        ET.SubElement(
            surface,
            "geom",
            name=f"belt_abrasive_stripe_{offset:.4f}",
            type="box",
            pos=_numbers(belt.contact + [0.00005, 0, offset]),
            size="0.00004 0.037 0.0003",
            rgba="0.1 0.3 0.25 1",
            contype="0",
            conaffinity="0",
            mass="0",
        )
    for name in (
        "belt_grinder_wheel_col",
        "belt_grinder_col_col",
        "belt_grinder_base_col",
    ):
        world.remove(world.find(f"geom[@name='{name}']"))
    for index, path in enumerate(prepare_belt_collision_assets()["pieces"]):
        name = f"belt_front_proxy_{index}"
        ET.SubElement(
            assets, "mesh", name=name, file=str(ASSETS / path), inertia="shell"
        )
        ET.SubElement(
            bodies["belt_grinder"],
            "geom",
            name=name,
            type="mesh",
            mesh=name,
            contype="1",
            conaffinity="11",
            group="3",
            rgba="0 0 0 0",
            mass="0",
        )
    contact_site = world.find("site[@name='tool_contact']")
    contact_site.set("pos", _numbers(tool.contact))

    gripper = root.find(".//body[@name='grinding_gripper']")
    ET.SubElement(
        gripper,
        "site",
        name="gripper_frame",
        pos="0 0 0",
        size="0.003",
        rgba="0.3 0.6 0.9 1",
    )
    fixture = root.find(f".//body[@name='{package.fixture_body_name}']")
    workpiece = root.find(f".//body[@name='{package.body_name}']")
    # The original static importer assigned no fixture orientation. This setup
    # faces the grip toward the robot, avoiding the rear loading gantry.
    for body in (fixture, workpiece):
        body.set("pos", _numbers(package.loading_position))
        body.set("quat", _numbers(_quaternion(loading_rotation)))
    table = ET.SubElement(
        world,
        "body",
        name="inspection_turntable",
        pos=_numbers(package.loading_position),
    )
    ET.SubElement(
        table,
        "joint",
        name="turntable_angle",
        axis="0 0 1",
        limited="false",
        damping="3",
        armature="0.1",
    )
    ET.SubElement(
        table,
        "geom",
        name="turntable_disk",
        type="cylinder",
        size=_numbers([cell.config["turntable"]["radius_m"], 0.014]),
        pos="0 0 -0.025",
        rgba="0.16 0.4 0.48 1",
        mass="8",
        contype="0",
        conaffinity="0",
    )
    ET.SubElement(
        root.find("actuator"),
        "position",
        name="turntable_servo",
        joint="turntable_angle",
        kp="4000",
        kv="120",
        ctrllimited="false",
        forcerange="-800 800",
    )
    ET.SubElement(workpiece, "freejoint", name="workpiece_free")
    for body in root.findall(".//body"):
        if "gravcomp" in body.attrib:
            body.set("gravcomp", "0")

    proxies = prepare_collision_assets()
    proxies["workpiece"] = prepare_workpiece_collision_assets(package, out)["pieces"]
    for owner, names in (
        (gripper, ("grip_board_col", "grip_frame_col")),
        (world, ("stiff_spindle_stand_col", "stiff_spindle_head_col")),
        (workpiece, ("wp_flange_col", "wp_hub_col")),
    ):
        for name in names:
            owner.remove(owner.find(f"geom[@name='{name}']"))
    for key, owner, prefix, ctype, affinity in (
        ("gripper", gripper, "gripper_proxy", 2, 3),
        ("spindle", bodies["spindle"], "spindle_proxy", 1, 3),
        ("workpiece", workpiece, "wp_proxy", 8, 7),
    ):
        for index, path in enumerate(proxies[key]):
            name = f"{prefix}_{index}"
            ET.SubElement(
                assets,
                "mesh",
                name=name,
                file=str(ASSETS / path),
                inertia="shell",
            )
            ET.SubElement(
                owner,
                "geom",
                name=name,
                type="mesh",
                mesh=name,
                contype=str(ctype),
                conaffinity=str(affinity),
                group="3",
                rgba="0 0 0 0",
                mass="0",
            )

    contact = root.find("contact")
    ET.SubElement(
        contact, "exclude", body1="grinding_gripper", body2=package.body_name
    )
    equality = ET.SubElement(root, "equality")
    # High weld impedance approximates the declared rigid fixture/grasp. It is
    # a simulation assumption, rather than measured gripper compliance.
    ET.SubElement(
        equality,
        "weld",
        name="fixture_hold",
        body1="inspection_turntable",
        body2=package.body_name,
        relpose=_numbers(np.r_[np.zeros(3), _quaternion(loading_rotation)]),
        active="true",
        solref="0.002 1",
        solimp="0.9999 0.9999 0.001",
    )
    ET.SubElement(
        equality,
        "weld",
        name="gripper_hold",
        body1="grinding_gripper",
        body2=package.body_name,
        relpose=_numbers(
            np.r_[
                gripper_from_workpiece[:3, 3],
                _quaternion(gripper_from_workpiece[:3, :3]),
            ]
        ),
        active="false",
        solref="0.002 1",
        solimp="0.9999 0.9999 0.001",
    )

    private = []
    from .workstation_defects import generate_cell_defects

    for index, (mesh, metadata) in enumerate(
        generate_cell_defects(package.mesh_path, seed, case, casting_profile)
    ):
        target = out / f"added_defect_{index}.stl"
        mesh.export(target)
        name = f"added_defect_{index}"
        ET.SubElement(
            assets, "mesh", name=name, file=str(target), inertia="shell"
        )
        ET.SubElement(
            workpiece,
            "geom",
            name=name,
            type="mesh",
            mesh=name,
            material=package.material_name,
            group="4",
            contype="0",
            conaffinity="0",
            mass="0",
        )
        private.append({"geom": name, **metadata})

    camera = cell.config["camera"] if camera_config is None else camera_config
    ET.SubElement(
        world,
        "camera",
        name="inspection",
        pos=_numbers(camera["position_world_m"]),
        xyaxes=camera_axes(
            camera["position_world_m"], camera["target_world_m"]
        ),
        fovy=str(camera["fovy_deg"]),
    )
    sensor = ET.SubElement(
        world,
        "body",
        name="fixed_inspection_camera",
        pos=_numbers(
            np.asarray(camera["position_world_m"])
            + 0.07
            * (
                np.asarray(camera["position_world_m"])
                - np.asarray(camera["target_world_m"])
            )
            / np.linalg.norm(
                np.asarray(camera["position_world_m"])
                - np.asarray(camera["target_world_m"])
            )
        ),
    )
    ET.SubElement(
        sensor,
        "geom",
        name="camera_housing",
        type="box",
        size="0.05 0.035 0.025",
        rgba="0.13 0.38 0.46 1",
        contype="0",
        conaffinity="0",
    )
    for name, position, target, fovy in (
        ("cell_overview", [2.4, -3.9, 3.5], [-0.6, -0.6, 1.0], 48),
        ("process_closeup", [-0.8, -1.7, 1.9], [-1.5, -1.1, 1.16], 38),
    ):
        ET.SubElement(
            world,
            "camera",
            name=name,
            pos=_numbers(position),
            xyaxes=camera_axes(position, target),
            fovy=str(fovy),
        )
    ET.indent(root, space="  ")
    path = out / "scene.xml"
    path.write_text(ET.tostring(root, encoding="unicode") + "\n")
    return path, private


class Workstation:
    """MuJoCo state for the actual cell, including a free casting and grasp weld."""

    def __init__(
        self,
        output,
        seed=7007,
        case="mixed",
        tool="belt",
        casting_profile=BASE_PROFILE,
        grasp_setup="original",
        camera_setup="original",
        *,
        workpiece_id="097",
        workpiece_package=None,
        cell_package=None,
    ):
        self.workpiece_package = load_workpiece_package(workpiece_package or workpiece_id)
        self.workpiece_id = self.workpiece_package.workpiece_id
        self.cell_package = cell_package or self.workpiece_package.cell
        self.cell_config = self.cell_package.config
        self.loading_position = self.workpiece_package.loading_position.copy()
        self.seed = seed
        self.camera_setup = camera_setup
        self.camera_config = inspection_camera_config(camera_setup)
        self.grasp_setup = grasp_setup
        self.gripper_from_workpiece, self.loading_rotation = self.workpiece_package.grasp_frames(grasp_setup)
        if self.workpiece_id != "097" and case in CASTING_CASES:
            raise ValueError("该工件尚未登记 casting 工况先验，不能借用 097 身份")
        self.workpiece_from_gripper = np.linalg.inv(self.gripper_from_workpiece)
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.case = case
        self.casting_profile = (
            load_profile(casting_profile) if case in CASTING_CASES else None
        )
        self.sensor_prior = (
            sensor_for_profile(self.casting_profile, seed)
            if self.casting_profile is not None
            else None
        )
        self.tool = self.cell_package.fixed_tool(tool)
        self.scene_file, self.private_defects = build_scene(
            self.output,
            seed,
            case,
            tool,
            casting_profile,
            grasp_setup,
            self.camera_config,
            workpiece_package=self.workpiece_package,
            cell_package=self.cell_package,
        )
        self.model = mujoco.MjModel.from_xml_path(str(self.scene_file))
        self.data = mujoco.MjData(self.model)
        self.scratch = mujoco.MjData(self.model)
        self.joints = np.array(
            [self.model.joint(f"j{i}").id for i in range(1, 7)]
        )
        self.q_index = self.model.jnt_qposadr[self.joints]
        self.dof_index = self.model.jnt_dofadr[self.joints]
        self.arm_actuators = np.array(
            [self.model.actuator(f"act_j{i}").id for i in range(1, 7)]
        )
        self.lower = self.model.jnt_range[self.joints, 0] + 0.005
        self.upper = self.model.jnt_range[self.joints, 1] - 0.005
        self.gripper = self.model.body("grinding_gripper").id
        self.workpiece = (
            self.model.body("workpiece_097")
            if self.workpiece_id == "097"
            else self.model.body(self.workpiece_package.body_name)
        ).id
        self.tcp = self.model.site("gripper_frame").id
        self.wp_qadr = self.model.joint("workpiece_free").qposadr[0]
        self.fixture_weld = self.model.equality("fixture_hold").id
        self.gripper_weld = self.model.equality("gripper_hold").id
        self.table_joint = self.model.joint("turntable_angle").id
        self.table_qadr = int(self.model.jnt_qposadr[self.table_joint])
        self.table_dof = int(self.model.jnt_dofadr[self.table_joint])
        self.table_actuator = self.model.actuator("turntable_servo").id
        self.replay_recording = ReplayRecorder()
        self.replay_frames = self.replay_recording.frames
        self.renderers = {}
        self.payload_jacobian = np.zeros((3, self.model.nv))
        self.payload_rotation_jacobian = np.zeros((3, self.model.nv))
        self.payload_jacobian_dot = np.zeros((3, self.model.nv))
        self.payload_rotation_jacobian_dot = np.zeros((3, self.model.nv))
        self.desired_acceleration = np.zeros(self.model.nv)
        self.inertial_force = np.zeros(self.model.nv)
        mujoco.mj_forward(self.model, self.data)
        home_wp = np.eye(4)
        home_wp[:3, :3] = self.loading_rotation
        home_wp[:3, 3] = self.loading_position + [0, 0, 0.35]
        home_grip = home_wp @ self.workpiece_from_gripper
        self.home = self.ik(
            home_grip, np.array([1.19, -0.1, 0.7, 2.5, 0.7, -2.7])
        )[0]
        self.data.qpos[self.q_index] = self.home
        self.data.ctrl[self.arm_actuators] = self.home
        mujoco.mj_forward(self.model, self.data)

    def ik(self, transform, seed):
        """Solve all six axes for the gripper frame; preserve published limits."""
        target_position = transform[:3, 3]
        target_rotation = transform[:3, :3]

        def residual(q):
            self.scratch.qpos[self.q_index] = q
            # IK needs kinematics, not full contact/constraint dynamics.
            mujoco.mj_kinematics(self.model, self.scratch)
            mujoco.mj_comPos(self.model, self.scratch)
            return np.r_[
                12 * (self.scratch.site_xpos[self.tcp] - target_position),
                Rotation.from_matrix(
                    target_rotation
                    @ self.scratch.site_xmat[self.tcp].reshape(3, 3).T
                ).as_rotvec(),
            ]

        jac_position = np.zeros((3, self.model.nv))
        jac_rotation = np.zeros((3, self.model.nv))

        def jacobian(q):
            error = residual(q)[3:]
            mujoco.mj_jacSite(
                self.model, self.scratch, jac_position, jac_rotation, self.tcp
            )
            x, y, z = error
            skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
            angle = np.linalg.norm(error)
            coefficient = (
                1 / 12
                if angle < 1e-4
                else 1 / angle**2
                - (1 + np.cos(angle)) / (2 * angle * np.sin(angle))
            )
            right_inverse = np.eye(3) + 0.5 * skew + coefficient * skew @ skew
            return np.vstack(
                [
                    12 * jac_position[:, self.dof_index],
                    -right_inverse @ jac_rotation[:, self.dof_index],
                ]
            )

        solution = least_squares(
            residual,
            np.clip(seed, self.lower, self.upper),
            jac=jacobian,
            bounds=(self.lower, self.upper),
            max_nfev=90,
            ftol=1e-9,
            xtol=1e-9,
            gtol=1e-9,
        )
        error = residual(solution.x)
        if np.linalg.norm(error) > 0.005:
            for alternative in (
                np.array([0.5, 0.3, -0.1, 0, 1.3, 0.5]),
                np.array([0.4, 0.3, 0.3, 1.1, 1.0, -0.4]),
            ):
                candidate = least_squares(
                    residual,
                    alternative,
                    jac=jacobian,
                    bounds=(self.lower, self.upper),
                    max_nfev=90,
                    ftol=1e-9,
                    xtol=1e-9,
                    gtol=1e-9,
                )
                candidate_error = residual(candidate.x)
                if np.linalg.norm(candidate_error) < np.linalg.norm(error):
                    solution, error = candidate, candidate_error
                if np.linalg.norm(error) < 0.005:
                    break
        position_error = float(np.linalg.norm(error[:3]) / 12)
        orientation_error = float(np.linalg.norm(error[3:]))
        if position_error > 0.0004 or orientation_error > np.deg2rad(0.6):
            raise ValueError(
                f"ESTUN 姿态不可达：位置 {position_error * 1000:.2f} mm，姿态 {np.rad2deg(orientation_error):.2f}°"
            )
        return solution.x, position_error, orientation_error

    def body_transform(self, body, data=None):
        data = self.data if data is None else data
        transform = np.eye(4)
        transform[:3, :3] = data.xmat[body].reshape(3, 3)
        transform[:3, 3] = data.xpos[body]
        return transform

    def set_scratch_pose(self, q, attached=False):
        self.scratch.qpos[self.q_index] = q
        mujoco.mj_forward(self.model, self.scratch)
        transform = (
            self.body_transform(self.gripper, self.scratch)
            @ self.gripper_from_workpiece
            if attached
            else np.block(
                [
                    [self.loading_rotation, self.loading_position[:, None]],
                    [np.zeros((1, 3)), np.ones((1, 1))],
                ]
            )
        )
        self.scratch.qpos[self.wp_qadr : self.wp_qadr + 7] = np.r_[
            transform[:3, 3], _quaternion(transform[:3, :3])
        ]
        mujoco.mj_forward(self.model, self.scratch)

    def desired_gripper_pose(self, q):
        """FK of the exact desired joint curve, without touching integrated state."""
        self.scratch.qpos[self.q_index] = q
        mujoco.mj_kinematics(self.model, self.scratch)
        return self.body_transform(self.gripper, self.scratch)

    def apply_gravity_feedforward(self):
        """Compensate the robot and held payload; leave free-body gravity active."""
        self.apply_motion_feedforward(np.zeros(6))

    def apply_motion_feedforward(self, acceleration):
        """Apply inverse dynamics only to the six robot DOFs, including held stock."""
        self.data.qfrc_applied[:] = 0
        self.desired_acceleration[:] = 0
        self.desired_acceleration[self.dof_index] = acceleration
        mujoco.mj_mulM(
            self.model,
            self.data,
            self.inertial_force,
            self.desired_acceleration,
        )
        self.data.qfrc_applied[self.dof_index] = (
            self.data.qfrc_bias[self.dof_index]
            + self.inertial_force[self.dof_index]
        )
        if self.data.eq_active[self.gripper_weld]:
            mujoco.mj_jac(
                self.model,
                self.data,
                self.payload_jacobian,
                self.payload_rotation_jacobian,
                self.data.xipos[self.workpiece],
                self.gripper,
            )
            mujoco.mj_jacDot(
                self.model,
                self.data,
                self.payload_jacobian_dot,
                self.payload_rotation_jacobian_dot,
                self.data.xipos[self.workpiece],
                self.gripper,
            )
            velocity = self.data.qvel[self.dof_index]
            linear_acceleration = (
                self.payload_jacobian[:, self.dof_index] @ acceleration
                + self.payload_jacobian_dot[:, self.dof_index] @ velocity
            )
            angular_acceleration = (
                self.payload_rotation_jacobian[:, self.dof_index] @ acceleration
                + self.payload_rotation_jacobian_dot[:, self.dof_index]
                @ velocity
            )
            angular_velocity = (
                self.payload_rotation_jacobian[:, self.dof_index] @ velocity
            )
            inertial_rotation = self.data.ximat[self.workpiece].reshape(3, 3)
            inertia = (
                inertial_rotation
                @ np.diag(self.model.body_inertia[self.workpiece])
                @ inertial_rotation.T
            )
            support_force = self.model.body_mass[self.workpiece] * (
                linear_acceleration - self.model.opt.gravity
            )
            support_torque = inertia @ angular_acceleration + np.cross(
                angular_velocity, inertia @ angular_velocity
            )
            self.data.qfrc_applied[self.dof_index] += (
                self.payload_jacobian[:, self.dof_index].T @ support_force
                + self.payload_rotation_jacobian[:, self.dof_index].T
                @ support_torque
            )

    def render(
        self,
        camera="inspection",
        width=1024,
        height=768,
        mode="rgb",
        reference=False,
    ):
        """Render RGB for display and pixel-center rays for depth and labels."""
        measurement = mode in {"depth", "segmentation"}
        key = (width, height, measurement)
        if key not in self.renderers:
            samples = self.model.vis.quality.offsamples
            if measurement:
                # Multisample depth resolves different subpixel rays at edges;
                # those samples do not match the calibrated pixel-center ray.
                self.model.vis.quality.offsamples = 0
            try:
                self.renderers[key] = mujoco.Renderer(
                    self.model, height=height, width=width
                )
            finally:
                self.model.vis.quality.offsamples = samples
        renderer = self.renderers[key]
        renderer.disable_depth_rendering()
        renderer.disable_segmentation_rendering()
        if mode == "depth":
            renderer.enable_depth_rendering()
        elif mode == "segmentation":
            renderer.enable_segmentation_rendering()
        options = mujoco.MjvOption()
        options.sitegroup[:] = 0
        options.geomgroup[3] = 0
        options.geomgroup[4] = int(not reference)
        renderer.update_scene(self.data, camera=camera, scene_option=options)
        return renderer.render().copy()

    def camera_info(self, width=1024, height=768, frame="workpiece"):
        camera = self.model.camera("inspection").id
        focal = (
            0.5 * height / np.tan(np.deg2rad(self.model.cam_fovy[camera]) / 2)
        )
        intrinsics = np.array(
            [
                [focal, 0, (width - 1) / 2],
                [0, focal, (height - 1) / 2],
                [0, 0, 1],
            ]
        )
        transform = np.eye(4)
        transform[:3, :3] = self.data.cam_xmat[camera].reshape(3, 3) @ np.diag(
            [1, -1, -1]
        )
        transform[:3, 3] = self.data.cam_xpos[camera]
        if frame == "workpiece":
            transform = (
                np.linalg.inv(self.body_transform(self.workpiece)) @ transform
            )
        return intrinsics, transform

    def state(self, phase="inspection", **extra):
        return {
            "schema_version": 1,
            "robot": "ESTUN iER220-2700",
            "workpiece_id": self.workpiece_id,
            "workpiece_package": self.workpiece_package.summary(),
            "grasp_setup": self.grasp_setup,
            "simulation_time_s": float(self.data.time),
            "phase": phase,
            "joint_angles_deg": np.rad2deg(self.data.qpos[self.q_index])
            .round(4)
            .tolist(),
            "joint_velocity_deg_s": np.rad2deg(self.data.qvel[self.dof_index])
            .round(4)
            .tolist(),
            "bodies": np.column_stack([self.data.xpos, self.data.xquat])
            .round(7)
            .tolist(),
            "workpiece_transform": self.body_transform(self.workpiece).tolist(),
            "gripper_transform": self.body_transform(self.gripper).tolist(),
            "grasp_attached": bool(self.data.eq_active[self.gripper_weld]),
            "fixture_attached": bool(self.data.eq_active[self.fixture_weld]),
            "turntable_angle_deg": float(
                np.rad2deg(self.data.qpos[self.table_qadr])
            ),
            "camera_transform_world": self.camera_info(frame="world")[
                1
            ].tolist(),
            "camera_fixed": True,
            "camera_setup": self.camera_setup,
            "camera_config": self.camera_config,
            "tool": self.tool.summary(),
            **extra,
        }

    def close(self):
        for renderer in self.renderers.values():
            renderer.close()
