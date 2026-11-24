"""Validated metric workpiece declarations, independent of the cell consumer.

Workpiece packages identify CAD, datum, simulation grasps and protected stock.
The shared cell package owns the abrasive surface and existing planning limits.
Neither package authorizes physical robot motion or certifies a real grasp.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import project

WORKPIECE_DIRECTORY = project.ROOT / "config/workpieces"
CELL_CONFIG_FILE = project.ROOT / "config/workstation.json"


@dataclass(frozen=True)
class CellPackage:
    """A shared cell configuration; lengths and tool poses resolve to metres."""

    path: Path
    config: dict

    def fixed_tool(self, key="belt"):
        """Resolve the declared belt while retaining the historical wheel API."""
        from .workstation_tooling import FixedTool, fixed_tool

        if key != "belt":
            return fixed_tool(key)
        belt = self.config["belt"]
        historical = fixed_tool("belt")
        return FixedTool(
            key=historical.key,
            label=historical.label,
            geom=historical.geom,
            contact=np.asarray(belt["contact_world_m"], dtype=float),
            normal=historical.normal.copy(),
            half_size=np.r_[
                belt["layer_thickness_mm"] / 2000,
                np.asarray(belt["abrasive_face_mm"], dtype=float) / 2000,
            ],
            source=belt["source"],
        )


@dataclass(frozen=True)
class GraspSetup:
    """One rigid CAD-to-gripper transform and its matching loading rotation."""

    name: str
    gripper_from_workpiece: np.ndarray
    loading_rotation: np.ndarray
    simulation_only: bool


@dataclass(frozen=True)
class ProtectedZone:
    """An axis-aligned datum region in the workpiece's own metric frame."""

    name: str
    bounds_m: np.ndarray


@dataclass(frozen=True)
class ContactSearch:
    """Bounded process redundancy and reachability-aware subdivision policy."""

    edge_margin_mm: float
    tilt_candidates_deg: tuple
    roll_refinement_deg: tuple
    overlap_mm: float
    minimum_segment_length_mm: float
    max_split_depth: int
    max_joint_jump_rad: float


@dataclass(frozen=True)
class WorkpiecePackage:
    """A registered CAD/fixture package, with no server or simulator dependency."""

    path: Path
    workpiece_id: str
    label: str
    coordinate_frame: str
    source_scene: Path
    mesh_path: Path
    body_name: str
    material_name: str
    fixture_id: str
    fixture_body_name: str
    loading_position: np.ndarray
    bounds_m: np.ndarray
    grasp_setups: tuple[GraspSetup, ...]
    protected_zones: tuple[ProtectedZone, ...]
    collision_slice_boundaries_m: tuple
    collision_angular_slices: int
    collision_cache: Path | None
    contact_search: ContactSearch
    cell: CellPackage
    declaration: dict

    def grasp_frames(self, name="original"):
        """Return independent frame arrays for a declared simulation setup."""
        for setup in self.grasp_setups:
            if setup.name == name:
                return (
                    setup.gripper_from_workpiece.copy(),
                    setup.loading_rotation.copy(),
                )
        raise ValueError("未登记的夹持配置")

    def summary(self):
        """Expose identity and assumptions without pretending to be calibrated."""
        return {
            "workpiece_id": self.workpiece_id,
            "coordinate_frame": self.coordinate_frame,
            "package_path": str(self.path),
            "cad_path": str(self.mesh_path),
            "source_scene": str(self.source_scene),
            "bounds_m": self.bounds_m.tolist(),
            "fixture_id": self.fixture_id,
            "grasp_setups": [setup.name for setup in self.grasp_setups],
            "cell_package_path": str(self.cell.path),
            "real_grasp_calibrated": False,
            "physical_changeover_validated": False,
        }


def load_cell_package(path=CELL_CONFIG_FILE):
    """Validate the existing cell's finite abrasive dimensions and planning limit."""
    path = _file(path, "cell configuration")
    config = json.loads(path.read_text())
    try:
        belt = config["belt"]
        _array(belt["contact_world_m"], (3,), "belt.contact_world_m")
        face = _array(belt["abrasive_face_mm"], (2,), "belt.abrasive_face_mm")
        if np.any(face <= 0) or not 0 < belt["layer_thickness_mm"] < np.inf:
            raise ValueError("砂带有效面与厚度须为正有限值")
        planning = config["planning"]
        for key in (
            "max_search_seconds_per_segment",
            "material_allowance_mm",
            "target_remaining_height_mm",
            "transfer_clearance_mm",
        ):
            if not 0 <= planning[key] < np.inf:
                raise ValueError(f"Invalid planning.{key}")
    except (KeyError, TypeError) as error:
        raise ValueError(f"工位配置缺少有效字段：{error}") from error
    return CellPackage(path, config)


def load_workpiece_package(workpiece_id="097", *, package_path=None):
    """Load one declaration, rejecting missing CAD, frame mismatch or bad grasps.

    Args:
        workpiece_id: Registered three-digit CAD identity, or an already loaded
            package. Explicit file loading still requires matching CAD identity.
        package_path: Optional declaration file; CAD paths remain project-relative.

    Returns:
        A validated workpiece package referencing a separately validated cell.

    Raises:
        ValueError: The package is unregistered, incomplete or not metric/rigid.
    """
    if isinstance(workpiece_id, WorkpiecePackage):
        return workpiece_id
    if not isinstance(workpiece_id, str) or not re.fullmatch(
        r"[0-9]{3}", workpiece_id
    ):
        raise ValueError("未登记的工件包")
    path = (
        Path(package_path)
        if package_path is not None
        else WORKPIECE_DIRECTORY / f"wp_{workpiece_id}.json"
    )
    if not path.is_file():
        raise ValueError("未登记的工件包")
    path = path.resolve()
    row = json.loads(path.read_text())
    try:
        if (
            row["schema_version"] != 1
            or row["workpiece_id"] != workpiece_id
            or row["units"] != "m"
            or row["coordinate_frame"] != f"workpiece_{workpiece_id}"
            or row["body_name"] != row["coordinate_frame"]
        ):
            raise ValueError("工件包的 CAD 身份、米制单位或坐标系不一致")
        mesh = _file(row["cad_path"], "CAD mesh")
        scene = _file(row["scene_path"], "source scene")
        source = ET.parse(scene).getroot()
        body = source.find(f".//body[@name='{row['body_name']}']")
        if body is None or body.find("geom[@name='wp_0']") is None:
            raise ValueError("工件包的实体未出现在声明场景中")
        visual = body.find("geom[@name='wp_0']")
        asset = source.find(f"./asset/mesh[@name='{visual.get('mesh')}']")
        meshdir = source.find("compiler").get("meshdir", "")
        if (
            asset is None
            or (scene.parent / meshdir / asset.get("file")).resolve() != mesh
        ):
            raise ValueError("工件包 CAD 与场景可视网格不一致")
        if visual.get("material") != row["material_name"]:
            raise ValueError("工件包材质与场景不一致")
        loading = _array(
            row["loading_position_world_m"], (3,), "loading_position_world_m"
        )
        bounds = _bounds(row["bounds_m"], "bounds_m")
        grasps = tuple(
            GraspSetup(
                setup["name"],
                _rigid(setup["gripper_from_workpiece"], setup["name"]),
                _rotation(setup["loading_rotation_world"], setup["name"]),
                setup["simulation_only"],
            )
            for setup in row["grasp_setups"]
        )
        names = [setup.name for setup in grasps]
        if "original" not in names or len(set(names)) != len(names):
            raise ValueError("工件包须有唯一命名的 original 与候选夹持")
        zones = tuple(
            ProtectedZone(zone["name"], _bounds(zone["bounds_m"], zone["name"]))
            for zone in row["protected_zones"]
        )
        collision = row["collision"]
        levels = _array(
            collision["slice_boundaries_m"],
            (len(collision["slice_boundaries_m"]),),
            "collision.slice_boundaries_m",
        )
        if (
            len(levels) < 2
            or np.any(np.diff(levels) <= 0)
            or levels[0] > bounds[0, 2]
            or levels[-1] < bounds[1, 2]
            or type(collision["angular_slices"]) is not int
            or collision["angular_slices"] < 4
        ):
            raise ValueError("碰撞切片须完整覆盖声明 CAD 高度")
        settings = row["contact_search"]
        tilts = tuple(settings["tilt_candidates_deg"])
        refinements = tuple(settings["roll_refinement_deg"])
        if (
            not tilts
            or not refinements
            or not np.isfinite([*tilts, *refinements]).all()
            or max(abs(value) for value in tilts)
            > settings["max_process_tilt_deg"]
            or not 0 <= settings["max_process_tilt_deg"] <= 5
            or not 0 < settings["edge_margin_mm"] < np.inf
            or not 2 <= settings["overlap_mm"] <= 4
            or not settings["overlap_mm"]
            < settings["minimum_segment_length_mm"]
            < np.inf
            or type(settings["max_split_depth"]) is not int
            or not 0 <= settings["max_split_depth"] <= 6
            or not 0 < settings["max_joint_jump_rad"] <= np.pi
        ):
            raise ValueError("接触冗余或受控切段参数无效")
        cell = load_cell_package(
            _file(row["cell_config_path"], "cell configuration")
        )
        if 2 * settings["edge_margin_mm"] >= min(
            cell.config["belt"]["abrasive_face_mm"]
        ):
            raise ValueError("砂带留边耗尽有效磨削面")
        return WorkpiecePackage(
            path=path,
            workpiece_id=workpiece_id,
            label=row["label"],
            coordinate_frame=row["coordinate_frame"],
            source_scene=scene,
            mesh_path=mesh,
            body_name=row["body_name"],
            material_name=row["material_name"],
            fixture_id=row["fixture_id"],
            fixture_body_name=row["fixture_body_name"],
            loading_position=loading,
            bounds_m=bounds,
            grasp_setups=grasps,
            protected_zones=zones,
            collision_slice_boundaries_m=tuple(levels),
            collision_angular_slices=collision["angular_slices"],
            collision_cache=(project.ROOT / collision["cache_path"]).resolve()
            if collision["cache_path"] is not None
            else None,
            contact_search=ContactSearch(
                settings["edge_margin_mm"],
                tilts,
                refinements,
                settings["overlap_mm"],
                settings["minimum_segment_length_mm"],
                settings["max_split_depth"],
                settings["max_joint_jump_rad"],
            ),
            cell=cell,
            declaration=row,
        )
    except (KeyError, TypeError) as error:
        raise ValueError(f"工件包缺少有效字段：{error}") from error


def _file(value, name):
    path = Path(value)
    path = (
        (project.ROOT / path).resolve()
        if not path.is_absolute()
        else path.resolve()
    )
    if not path.is_file():
        raise ValueError(f"工件包缺少 {name}：{path}")
    return path


def _array(value, shape, name):
    result = np.asarray(value, dtype=float)
    if result.shape != shape or not np.isfinite(result).all():
        raise ValueError(f"{name} 须为有限的 {shape} 数组")
    return result


def _rotation(value, name):
    rotation = _array(value, (3, 3), name)
    if not np.allclose(
        rotation.T @ rotation, np.eye(3), atol=1e-8
    ) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-8):
        raise ValueError(f"{name} 须为刚体旋转")
    return rotation


def _rigid(value, name):
    matrix = _array(value, (4, 4), name)
    _rotation(matrix[:3, :3], name)
    if not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-10):
        raise ValueError(f"{name} 须为米制刚体变换")
    return matrix


def _bounds(value, name):
    bounds = _array(value, (2, 3), name)
    if np.any(bounds[1] <= bounds[0]):
        raise ValueError(f"{name} 须为有序三维包围盒")
    return bounds
