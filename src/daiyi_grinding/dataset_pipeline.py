"""Multi-master casting samples with drafted features and sensor degradation.

The pipeline extracts candidate parting lines, sharp edges, hole rims and gate
attachment zones from any supplied triangle mesh, adds rooted flash, burr or
casting-root geometry, and renders local structured-light observations. The
sensor model is a physically motivated proxy for incidence-dependent axial
noise, grazing dropout, edge flying pixels, specular/oxide dropout patches and
bounded registration drift. It is not fitted to a real scanner until
``SensorDegradationProfile.calibrate_from_residuals`` receives measured
residuals with their provenance.

All generated labels, roots and provenance stay in the sample bundle. Nothing
here is a calibrated casting process, a metrology result or a robot command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage
from scipy.ndimage import gaussian_filter, gaussian_filter1d
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from . import project

SCHEMA = "daiyi-dataset-pipeline-v1"
BUNDLE_SCHEMA = "daiyi-casting-sample-bundle-v1"
OUTPUT_ROOT = project.RUNS / "enterprise_offline_20261123/p1_data_process_01"
MESH_DIR = project.ROOT / "assets/ier220_workstation/meshes/workpiece"
WORKPIECE_MESHES = {
    "097": MESH_DIR / "wp_097_c0.stl",
    "021": MESH_DIR / "wp_021_c0.stl",
}
SEMANTIC_LABELS = {
    "background": 0,
    "flash": 1,
    "burr": 2,
    "nominal_cad": 3,
    "review_excess": 4,
    "casting_root": 5,
    "intermediate_residual": 6,
}
DEFECT_SEMANTICS = {"flash": 1, "burr": 2, "casting_root": 5}
INTERMEDIATE_STATES = ("rough_residual", "thin_stock", "micro_overgrind")
SAMPLE_KINDS = ("overview", "local_fov")
REVIEW_HEIGHT_MM = 2.0
ROOT_EMBEDDING_MM = 0.08


def unit(vectors):
    """Return unit vectors without introducing non-finite values."""
    values = np.asarray(vectors, dtype=float)
    return values / np.maximum(
        np.linalg.norm(values, axis=-1, keepdims=True), 1e-12
    )


def sha256_file(path):
    """Return the SHA-256 digest of one local file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    """Write finite JSON with a trailing newline."""
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def workpiece_id_from_path(path):
    """Derive the part family from ``wp_<id>_c<variant>.stl`` or the stem."""
    stem = Path(path).stem
    match = re.fullmatch(r"wp_([A-Za-z0-9-]+)_c\d+", stem)
    if match:
        return match.group(1)
    token = re.split(r"[_\-. ]+", stem)[0]
    if not token:
        raise ValueError("Cannot derive a workpiece identifier from the mesh")
    return token


def load_mesh(path):
    """Load one STL/OBJ triangle mesh and check the shared contract."""
    mesh = trimesh.load_mesh(str(path), process=True)
    if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
        raise ValueError(f"{path} does not contain a triangle mesh")
    if not np.isfinite(mesh.vertices).all():
        raise ValueError(f"{path} contains non-finite vertices")
    if len(mesh.vertices) < 3 or len(mesh.faces) < 3:
        raise ValueError(f"{path} is too small to extract surface features")
    return mesh


def draft_rotation(direction):
    """Return a rotation mapping the draft direction onto +Z."""
    draft = unit(np.asarray(direction, dtype=float).reshape(3))
    if not np.isfinite(draft).all():
        raise ValueError("Draft direction must be finite")
    z_axis = np.array([0.0, 0.0, 1.0])
    if np.allclose(draft, z_axis, atol=1e-12):
        return np.eye(3)
    axis = np.cross(draft, z_axis)
    sine = float(np.linalg.norm(axis))
    if sine < 1e-12:
        return np.diag([1.0, -1.0, -1.0])
    angle = math.atan2(sine, float(draft @ z_axis))
    return Rotation.from_rotvec(unit(axis) * angle).as_matrix()


@dataclass(frozen=True)
class FeatureConfig:
    """Thresholds for automatic feature extraction in the draft frame."""

    draft_direction: tuple = (0.0, 0.0, 1.0)
    heightmap_spacing_m: float = 0.0015
    support_normal_dot: float = 0.5
    sharp_angle_deg: float = 35.0
    min_sharp_length_m: float = 0.012
    min_contour_perimeter_m: float = 0.02
    min_contour_area_m2: float = 3e-5
    contour_spacing_m: float = 0.0008
    gate_zone_length_m: float = 0.022
    gate_zone_count: int = 2
    gate_min_top_access: float = 0.6
    gate_max_height_span_mm: float = 3.0
    gate_min_hole_clearance_m: float = 0.012
    contour_smoothing_px: float = 2.0

    def __post_init__(self):
        """Reject thresholds that cannot produce a well-defined feature."""
        if (
            not isinstance(self.draft_direction, tuple)
            or len(self.draft_direction) != 3
            or not np.isfinite(self.draft_direction).all()
            or np.linalg.norm(self.draft_direction) < 1e-9
        ):
            raise ValueError("Draft direction must be a nonzero 3-vector")
        for name in (
            "heightmap_spacing_m",
            "min_sharp_length_m",
            "min_contour_perimeter_m",
            "min_contour_area_m2",
            "contour_spacing_m",
            "gate_zone_length_m",
            "gate_min_hole_clearance_m",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a positive finite length")
        if not 0.0 < self.support_normal_dot < 1.0:
            raise ValueError("support_normal_dot must lie between zero and one")
        if not 0.0 < self.sharp_angle_deg < 180.0:
            raise ValueError("sharp_angle_deg must lie in (0, 180)")
        if type(self.gate_zone_count) is not int or self.gate_zone_count < 0:
            raise ValueError("gate_zone_count must be a nonnegative integer")


@dataclass(frozen=True)
class FeatureLine:
    """One extracted polyline with its CAD support and provenance."""

    points_m: np.ndarray
    normals_m: np.ndarray
    kind: str
    source: str
    closed: bool
    length_m: float
    metadata: dict = field(default_factory=dict)

    def record(self):
        """Return a JSON record without losing the geometric provenance."""
        return {
            "kind": self.kind,
            "source": self.source,
            "closed": bool(self.closed),
            "length_mm": float(self.length_m * 1000),
            "point_count": int(len(self.points_m)),
            "points_m": self.points_m.round(7).tolist(),
            "normals": self.normals_m.round(6).tolist(),
            **self.metadata,
        }


@dataclass(frozen=True)
class FeatureLineSet:
    """Automatic feature extraction for one CAD mesh."""

    workpiece_id: str
    mesh_path: str
    cad_sha256: str
    draft_direction: tuple
    lines: tuple
    config: FeatureConfig

    def by_kind(self, kind):
        """Return lines of one kind in extraction order."""
        return [line for line in self.lines if line.kind == kind]

    def summary(self):
        """Summarize counts and total lengths by feature kind."""
        rows = {}
        for kind in (
            "parting_candidate",
            "sharp_edge",
            "hole_rim",
            "flat_surface",
        ):
            selected = self.by_kind(kind)
            rows[kind] = {
                "count": len(selected),
                "total_length_mm": float(
                    sum(line.length_m for line in selected) * 1000
                ),
                "max_length_mm": float(
                    max((line.length_m for line in selected), default=0.0)
                    * 1000
                ),
            }
        zones = self.by_kind("gate_root_zone")
        rows["gate_root_zone"] = {
            "count": len(zones),
            "scores": [line.metadata.get("score") for line in zones],
        }
        return {
            "workpiece_id": self.workpiece_id,
            "mesh_path": self.mesh_path,
            "cad_sha256": self.cad_sha256,
            "draft_direction": list(self.draft_direction),
            "heightmap_spacing_mm": self.config.heightmap_spacing_m * 1000,
            "support_normal_dot": self.config.support_normal_dot,
            "sharp_angle_deg": self.config.sharp_angle_deg,
            "features": rows,
        }


@dataclass(frozen=True)
class DraftHeightmap:
    """Maximum supported height over a grid aligned with the draft direction."""

    heights_m: np.ndarray
    grid_x_m: np.ndarray
    grid_y_m: np.ndarray
    spacing_m: float
    rotation: np.ndarray

    @property
    def support(self):
        return np.isfinite(self.heights_m)

    def filled(self):
        """Fill unsupported cells by their nearest supported height."""
        heights = self.heights_m.copy()
        missing = ~np.isfinite(heights)
        if missing.any():
            indices = ndimage.distance_transform_edt(
                missing, return_distances=False, return_indices=True
            )
            heights[missing] = heights[tuple(indices[:, missing])]
        return heights

    def sample(self, xy_m):
        """Nearest-cell height at draft-frame XY points."""
        heights = self.filled()
        col = np.clip(
            np.rint((xy_m[..., 0] - self.grid_x_m[0]) / self.spacing_m),
            0,
            len(self.grid_x_m) - 1,
        ).astype(int)
        row = np.clip(
            np.rint((xy_m[..., 1] - self.grid_y_m[0]) / self.spacing_m),
            0,
            len(self.grid_y_m) - 1,
        ).astype(int)
        return heights[row, col]

    def curvature(self):
        """Return |Laplacian| of the filled height in inverse metres."""
        heights = gaussian_filter(self.filled(), 1.5)
        return np.abs(ndimage.laplace(heights)) / self.spacing_m**2


def top_surface_heightmap(mesh, config):
    """Rasterize draft-facing triangles into a max-height grid.

    Grid coordinates are in the draft frame where the configured draft
    direction is +Z; heights are metre Z values in that same frame.
    """
    rotation = draft_rotation(config.draft_direction)
    vertices = mesh.vertices @ rotation.T
    normals = mesh.face_normals @ rotation.T
    triangles = mesh.triangles @ rotation.T
    selected = normals[:, 2] >= config.support_normal_dot
    if not selected.any():
        raise ValueError(
            "No surface faces the configured draft direction; check the mesh"
        )
    spacing = config.heightmap_spacing_m
    grid_x = np.arange(
        vertices[:, 0].min(), vertices[:, 0].max() + spacing, spacing
    )
    grid_y = np.arange(
        vertices[:, 1].min(), vertices[:, 1].max() + spacing, spacing
    )
    heights = np.full((len(grid_y), len(grid_x)), np.nan)
    for triangle in triangles[selected]:
        row_start = max(
            0, int(np.floor((triangle[:, 1].min() - grid_y[0]) / spacing))
        )
        row_end = min(
            len(grid_y),
            int(np.ceil((triangle[:, 1].max() - grid_y[0]) / spacing)) + 1,
        )
        col_start = max(
            0, int(np.floor((triangle[:, 0].min() - grid_x[0]) / spacing))
        )
        col_end = min(
            len(grid_x),
            int(np.ceil((triangle[:, 0].max() - grid_x[0]) / spacing)) + 1,
        )
        if row_end <= row_start or col_end <= col_start:
            continue
        sample_x, sample_y = np.meshgrid(
            grid_x[col_start:col_end], grid_y[row_start:row_end]
        )
        first, second, third = triangle
        determinant = (second[1] - third[1]) * (first[0] - third[0]) + (
            third[0] - second[0]
        ) * (first[1] - third[1])
        if abs(determinant) < 1e-15:
            continue
        weight_first = (
            (second[1] - third[1]) * (sample_x - third[0])
            + (third[0] - second[0]) * (sample_y - third[1])
        ) / determinant
        weight_second = (
            (third[1] - first[1]) * (sample_x - third[0])
            + (first[0] - third[0]) * (sample_y - third[1])
        ) / determinant
        weight_third = 1.0 - weight_first - weight_second
        inside = (
            (weight_first >= -1e-5)
            & (weight_second >= -1e-5)
            & (weight_third >= -1e-5)
        )
        if not inside.any():
            continue
        proposed = (
            weight_first * first[2]
            + weight_second * second[2]
            + weight_third * third[2]
        )
        patch = heights[row_start:row_end, col_start:col_end]
        patch[inside] = np.fmax(patch[inside], proposed[inside])
    return DraftHeightmap(heights, grid_x, grid_y, spacing, rotation)


def _resample_closed(xy_m, spacing_m):
    """Resample a closed planar polyline at uniform arc length."""
    closed = np.vstack([xy_m, xy_m[0]])
    distances = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    keep = np.r_[True, distances > 1e-12]
    closed = closed[keep]
    distances = np.r_[
        0.0, np.cumsum(np.linalg.norm(np.diff(closed, axis=0), axis=1))
    ]
    count = max(8, int(np.ceil(distances[-1] / spacing_m)))
    samples = np.linspace(0.0, distances[-1], count, endpoint=False)
    return np.column_stack(
        [np.interp(samples, distances, closed[:, axis]) for axis in (0, 1)]
    )


def _heightmap_normals(heightmap, xy_m):
    """Return draft-frame normals from the filled height gradient."""
    heights = gaussian_filter(heightmap.filled(), 1.0)
    d_height_dy, d_height_dx = np.gradient(
        heights, heightmap.spacing_m, heightmap.spacing_m
    )
    col = np.clip(
        np.rint((xy_m[:, 0] - heightmap.grid_x_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_x_m) - 1,
    ).astype(int)
    row = np.clip(
        np.rint((xy_m[:, 1] - heightmap.grid_y_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_y_m) - 1,
    ).astype(int)
    normals = np.column_stack(
        [
            -d_height_dx[row, col],
            -d_height_dy[row, col],
            np.ones(len(xy_m)),
        ]
    )
    return unit(normals)


def _heightmap_curvature(heightmap, xy_m):
    """Sample the absolute height Laplacian at draft-frame XY points."""
    curvature = heightmap.curvature()
    col = np.clip(
        np.rint((xy_m[:, 0] - heightmap.grid_x_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_x_m) - 1,
    ).astype(int)
    row = np.clip(
        np.rint((xy_m[:, 1] - heightmap.grid_y_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_y_m) - 1,
    ).astype(int)
    return curvature[row, col]


def _edge_components(edge_ids, edges):
    """Group edges into connected components without quadratic scans."""
    parent = {int(edge_id): int(edge_id) for edge_id in edge_ids}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first, second):
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[root_second] = root_first

    first_edge_by_vertex = {}
    for edge_id in edge_ids:
        for vertex in edges[edge_id]:
            vertex = int(vertex)
            if vertex in first_edge_by_vertex:
                union(int(edge_id), first_edge_by_vertex[vertex])
            else:
                first_edge_by_vertex[vertex] = int(edge_id)
    groups = {}
    for edge_id in edge_ids:
        groups.setdefault(find(int(edge_id)), []).append(int(edge_id))
    return list(groups.values())


def _order_chain(edge_ids, edges):
    """Order one connected edge set into a polyline, closing loops exactly."""
    adjacency = {}
    for edge_id in edge_ids:
        first, second = edges[edge_id]
        adjacency.setdefault(int(first), []).append((int(second), edge_id))
        adjacency.setdefault(int(second), []).append((int(first), edge_id))
    if not adjacency:
        return []
    endpoints = [
        vertex for vertex, links in adjacency.items() if len(links) == 1
    ]
    start = endpoints[0] if endpoints else min(adjacency)
    ordered = [start]
    visited = set()
    current = start
    previous = None
    while True:
        candidates = None
        for neighbour, edge_id in adjacency[current]:
            if edge_id in visited or neighbour == previous:
                continue
            candidates = (neighbour, edge_id)
            break
        if candidates is None:
            break
        next_vertex, edge_id = candidates
        visited.add(edge_id)
        ordered.append(next_vertex)
        previous, current = current, next_vertex
        if current == start:
            break
    return ordered


def _vertex_chains(edge_ids, edges):
    """Split an edge set into connected ordered chains."""
    chains = []
    for component in _edge_components(edge_ids, edges):
        ordered = _order_chain(component, edges)
        if len(ordered) >= 2:
            chains.append(ordered)
    return chains


def _chain_length(points_m):
    if len(points_m) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(points_m, axis=0), axis=1).sum())


def _nearest_face_normals(mesh, face_ids, points):
    """Use nearby support-face normals along an extracted polyline."""
    face_ids = np.asarray(face_ids, dtype=int)
    tree = cKDTree(mesh.triangles_center[face_ids])
    _, nearest = tree.query(points)
    return mesh.face_normals[face_ids[nearest]]


def _sharp_lines(mesh, config):
    """Extract polylines where the dihedral angle jumps."""
    angles = mesh.face_adjacency_angles
    selected = np.flatnonzero(np.degrees(angles) >= config.sharp_angle_deg)
    if not len(selected):
        return []
    edges = mesh.face_adjacency_edges[selected]
    adjacent = mesh.face_adjacency[selected]
    draft = unit(np.asarray(config.draft_direction, dtype=float))
    lines = []
    for chain in _vertex_chains(np.arange(len(edges)), edges):
        points = mesh.vertices[np.asarray(chain)]
        length = _chain_length(points)
        if length < config.min_sharp_length_m:
            continue
        incident = np.unique(
            adjacent[np.isin(edges, np.asarray(chain)).any(axis=1)]
        )
        if not len(incident):
            continue
        normals = _nearest_face_normals(mesh, incident, points)
        draft_dots = mesh.face_normals[incident] @ draft
        lines.append(
            FeatureLine(
                points_m=points,
                normals_m=normals,
                kind="sharp_edge",
                source="dihedral",
                closed=bool(chain[0] == chain[-1]),
                length_m=length,
                metadata={
                    "max_dihedral_deg": float(
                        np.degrees(
                            angles[selected][
                                np.isin(edges, np.asarray(chain)).any(axis=1)
                            ]
                        ).max()
                    ),
                    "draft_dot_min": float(draft_dots.min()),
                    "draft_dot_max": float(draft_dots.max()),
                },
            )
        )
    return lines


def _inset_contour(heightmap, mask, xy_m, source, inset_m=0.008):
    """Offset a silhouette candidate inward so a flash chart fits the face.

    The draft-silhouette contour sits exactly on the top edge; a tangent-plane
    chart centred there puts half of its nodes on the outer wall. The inset
    candidate stays a valid flash attachment while making the local residual
    chart representable on the top surface. The offset is recorded.
    """
    distance = gaussian_filter(
        ndimage.distance_transform_edt(mask).astype(float), 1.5
    )
    d_row, d_col = np.gradient(distance)
    col = np.clip(
        np.rint((xy_m[:, 0] - heightmap.grid_x_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_x_m) - 1,
    ).astype(int)
    row = np.clip(
        np.rint((xy_m[:, 1] - heightmap.grid_y_m[0]) / heightmap.spacing_m),
        0,
        len(heightmap.grid_y_m) - 1,
    ).astype(int)
    gradient = np.column_stack([d_col[row, col], d_row[row, col]])
    gradient /= np.maximum(
        np.linalg.norm(gradient, axis=1, keepdims=True), 1e-12
    )
    moved = xy_m + gradient * inset_m
    moved = gaussian_filter1d(moved, 2.0, axis=0, mode="wrap")
    moved = _resample_closed(moved, 0.0008)
    heights = heightmap.sample(moved)
    rotation = heightmap.rotation
    draft_normals = _heightmap_normals(heightmap, moved)
    return FeatureLine(
        points_m=np.column_stack([moved, heights]) @ rotation,
        normals_m=draft_normals @ rotation,
        kind="parting_candidate",
        source="draft_silhouette_inset",
        closed=True,
        length_m=float(source.length_m),
        metadata={
            "inset_mm": float(inset_m * 1000),
            "source_length_mm": float(source.length_m * 1000),
            "mean_height_mm": float(np.mean(heights) * 1000),
            "height_span_mm": float(np.ptp(heights) * 1000),
            "mean_top_access": float(np.mean(np.abs(draft_normals[:, 2]))),
        },
    )


def extract_features(mesh, workpiece_id, mesh_path, config):
    """Extract parting, sharp, hole-rim and gate-zone candidates."""
    heightmap = top_surface_heightmap(mesh, config)
    rotation = heightmap.rotation
    mask = heightmap.support.astype(np.uint8)
    contours, hierarchy = cv2.findContours(
        mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE
    )
    if hierarchy is None:
        raise ValueError("The draft-facing support map has no contours")
    hierarchy = hierarchy[0]
    spacing = heightmap.spacing_m
    supported_heights = heightmap.heights_m[heightmap.support]
    top_band_mm = float(np.percentile(supported_heights, 95) * 1000)
    lines = []
    outer = []
    rims = []
    draft = np.asarray(config.draft_direction, dtype=float) / np.linalg.norm(
        config.draft_direction
    )
    for index, contour in enumerate(contours):
        if len(contour) < 20:
            continue
        perimeter = float(cv2.arcLength(contour, True) * spacing)
        area = float(abs(cv2.contourArea(contour)) * spacing**2)
        if (
            perimeter < config.min_contour_perimeter_m
            or area < config.min_contour_area_m2
        ):
            continue
        pixels = contour[:, 0].astype(float)
        xy = np.column_stack(
            [
                heightmap.grid_x_m[0] + pixels[:, 0] * spacing,
                heightmap.grid_y_m[0] + pixels[:, 1] * spacing,
            ]
        )
        xy = gaussian_filter1d(
            xy, config.contour_smoothing_px, axis=0, mode="wrap"
        )
        xy = _resample_closed(xy, config.contour_spacing_m)
        heights = heightmap.sample(xy)
        draft_normals = _heightmap_normals(heightmap, xy)
        points = np.column_stack([xy, heights]) @ rotation
        normals = draft_normals @ rotation
        kind = "parting_candidate" if hierarchy[index][3] < 0 else "hole_rim"
        curve = FeatureLine(
            points_m=points,
            normals_m=normals,
            kind=kind,
            source="draft_silhouette",
            closed=True,
            length_m=perimeter,
            metadata={
                "area_mm2": area * 1e6,
                "mean_height_mm": float(np.mean(heights) * 1000),
                "height_span_mm": float(np.ptp(heights) * 1000),
                "mean_top_access": float(
                    np.mean(np.abs(draft_normals @ draft))
                ),
                "mean_curvature_inv_m": float(
                    np.mean(_heightmap_curvature(heightmap, xy))
                ),
            },
        )
        lines.append(curve)
        if kind == "parting_candidate":
            outer.append(curve)
            inset = _inset_contour(heightmap, mask, xy, curve)
            if (
                inset.metadata["mean_top_access"] >= 0.9
                and inset.metadata["mean_height_mm"] >= top_band_mm - 12.0
            ):
                lines.append(inset)
        else:
            rims.append(curve)

    sharp = _sharp_lines(mesh, config)
    lines.extend(sharp)
    for line in sharp:
        if (
            line.metadata["draft_dot_max"] >= 0.55
            and line.metadata["draft_dot_min"] <= 0.35
            and line.length_m >= 0.02
        ):
            lines.append(
                FeatureLine(
                    points_m=line.points_m,
                    normals_m=line.normals_m,
                    kind="parting_candidate",
                    source="dihedral_transition",
                    closed=line.closed,
                    length_m=line.length_m,
                    metadata={
                        "draft_dot_min": line.metadata["draft_dot_min"],
                        "draft_dot_max": line.metadata["draft_dot_max"],
                        "max_dihedral_deg": line.metadata["max_dihedral_deg"],
                    },
                )
            )

    boundary = np.bincount(mesh.edges_unique_inverse) == 1
    if boundary.any():
        boundary_edges = mesh.edges_unique[boundary]
        for chain in _vertex_chains(
            np.arange(len(boundary_edges)), boundary_edges
        ):
            points = mesh.vertices[np.asarray(chain)]
            length = _chain_length(points)
            if chain[0] != chain[-1] or length < config.min_contour_perimeter_m:
                continue
            incident = np.flatnonzero(np.isin(mesh.faces, chain).any(axis=1))
            normals = (
                _nearest_face_normals(mesh, incident, points)
                if len(incident)
                else np.tile([0.0, 0.0, 1.0], (len(points), 1))
            )
            lines.append(
                FeatureLine(
                    points_m=points,
                    normals_m=normals,
                    kind="hole_rim",
                    source="mesh_boundary",
                    closed=True,
                    length_m=length,
                    metadata={"boundary_loop": True},
                )
            )
    lines.extend(_gate_zones(outer, rims, heightmap, config, top_band_mm))
    lines.extend(_flat_surface_points(heightmap, config))
    return FeatureLineSet(
        workpiece_id=workpiece_id,
        mesh_path=str(Path(mesh_path).resolve()),
        cad_sha256=sha256_file(mesh_path),
        draft_direction=tuple(float(value) for value in config.draft_direction),
        lines=tuple(lines),
        config=config,
    )


def _gate_zones(outer, rims, heightmap, config, top_band_mm):
    """Score draft-rim windows as candidate gate attachment zones."""
    if not outer:
        return []
    rim_xy = (
        np.vstack([line.points_m @ heightmap.rotation.T for line in rims])[
            :, :2
        ]
        if rims
        else np.empty((0, 2))
    )
    rim_tree = cKDTree(rim_xy) if len(rim_xy) else None
    rotation = heightmap.rotation
    candidates = []
    for line in outer:
        points = line.points_m
        closed = np.vstack([points, points[0]])
        arc = np.r_[
            0.0,
            np.cumsum(np.linalg.norm(np.diff(closed, axis=0), axis=1)),
        ]
        arc = arc / arc[-1]
        window = config.gate_zone_length_m / max(line.length_m, 1e-9)
        starts = np.arange(0.0, 1.0 - window, max(window / 2, 0.05))
        for start in starts:
            active = (arc >= start) & (arc <= start + window)
            if active.sum() < 8:
                continue
            window_points = closed[active]
            xy = (window_points @ rotation.T)[:, :2]
            heights = heightmap.sample(xy)
            normals = _heightmap_normals(heightmap, xy)
            top_access = float(np.mean(normals[:, 2]))
            span_mm = float(np.ptp(heights) * 1000)
            mean_height_mm = float(np.mean(heights) * 1000)
            curvature = float(np.mean(_heightmap_curvature(heightmap, xy)))
            clearance = (
                float(rim_tree.query(xy)[0].min())
                if rim_tree is not None
                else math.inf
            )
            if (
                top_access < config.gate_min_top_access
                or span_mm > config.gate_max_height_span_mm
                or clearance < config.gate_min_hole_clearance_m
                or mean_height_mm < top_band_mm - 12.0
            ):
                continue
            score = (
                top_access
                + 0.2 * min(clearance / 0.05, 1.0)
                - 0.05 * span_mm
                - 0.001 * curvature
            )
            candidates.append(
                (
                    score,
                    FeatureLine(
                        points_m=window_points,
                        normals_m=normals @ rotation,
                        kind="gate_root_zone",
                        source="draft_silhouette",
                        closed=False,
                        length_m=_chain_length(window_points),
                        metadata={
                            "score": float(score),
                            "top_access": top_access,
                            "height_span_mm": span_mm,
                            "hole_clearance_mm": clearance * 1000,
                            "mean_curvature_inv_m": curvature,
                        },
                    ),
                )
            )
    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = []
    for _, candidate in candidates:
        center = candidate.points_m.mean(axis=0)
        if any(
            np.linalg.norm(center - other.points_m.mean(axis=0)) < 0.03
            for other in chosen
        ):
            continue
        chosen.append(candidate)
        if len(chosen) >= config.gate_zone_count:
            break
    return chosen


def _flat_surface_points(heightmap, config, count=6):
    """Choose low-curvature interior top-surface points for compact defects.

    Placing a compact burr on a sharp edge makes the tangent-plane footprint
    straddle two faces, so the solid can sit inside the casting. Interior flat
    support points keep the radial chart single-valued.
    """
    support = heightmap.support
    distance = ndimage.distance_transform_edt(support) * heightmap.spacing_m
    curvature = heightmap.curvature()
    supported_heights = heightmap.heights_m[support]
    top_band_mm = float(np.percentile(supported_heights, 95) * 1000)
    candidate = (
        support
        & (distance >= 0.025)
        & (heightmap.heights_m * 1000 >= top_band_mm - 12.0)
    )
    if not candidate.any():
        return []
    rows, cols = np.nonzero(candidate)
    scores = curvature[rows, cols]
    order = np.argsort(scores)
    rotation = heightmap.rotation
    draft = unit(np.asarray(config.draft_direction, dtype=float))
    chosen = []
    for index in order:
        xy = np.array(
            [
                heightmap.grid_x_m[0] + cols[index] * heightmap.spacing_m,
                heightmap.grid_y_m[0] + rows[index] * heightmap.spacing_m,
            ]
        )
        if any(np.linalg.norm(xy - other) < 0.025 for other in chosen):
            continue
        height = float(heightmap.sample(xy[None, :])[0])
        normal = _heightmap_normals(heightmap, xy[None, :])[0]
        point = rotation.T @ np.array([xy[0], xy[1], height])
        world_normal = rotation.T @ normal
        tangent = unit(np.cross(world_normal, [0.0, 0.0, 1.0]))
        if np.linalg.norm(tangent) < 1e-6:
            tangent = np.array([1.0, 0.0, 0.0])
        chosen.append(xy)
        yield FeatureLine(
            points_m=np.array(
                [point, point + tangent * 0.001, point + tangent * 0.002]
            ),
            normals_m=np.tile(world_normal, (3, 1)),
            kind="flat_surface",
            source="curvature",
            closed=False,
            length_m=0.002,
            metadata={
                "draft_dot_max": float(draft @ world_normal),
                "curvature_inv_m": float(curvature[rows[index], cols[index]]),
                "distance_to_support_edge_mm": float(
                    distance[rows[index], cols[index]] * 1000
                ),
            },
        )
        if len(chosen) >= count:
            break


@dataclass(frozen=True)
class SplitAssignment:
    """Unseen-CAD split with an explicit no-leak invariant."""

    train: tuple
    val: tuple
    test: tuple
    seed: int
    fractions: tuple

    def mapping(self):
        """Return the workpiece-to-split lookup used by every sample."""
        return {
            workpiece_id: split
            for split, identifiers in (
                ("train", self.train),
                ("val", self.val),
                ("test", self.test),
            )
            for workpiece_id in identifiers
        }

    def record(self):
        return {
            "train": list(self.train),
            "val": list(self.val),
            "test": list(self.test),
            "seed": self.seed,
            "fractions": list(self.fractions),
            "rule": (
                "all views, styles, crops and intermediate states of one "
                "workpiece_id stay in one split"
            ),
        }


def unseen_cad_split(workpiece_ids, fractions=(0.5, 0.25, 0.25), seed=0):
    """Assign whole workpiece families to train/val/test without leakage."""
    identifiers = sorted({str(value) for value in workpiece_ids})
    if len(identifiers) < 2:
        raise ValueError(
            "Unseen-CAD splitting requires at least two workpieces"
        )
    if (
        len(fractions) != 3
        or any(not math.isfinite(value) or value < 0 for value in fractions)
        or abs(sum(fractions) - 1.0) > 1e-9
    ):
        raise ValueError("Split fractions must be nonnegative and sum to one")
    if type(seed) is not int or seed < 0:
        raise ValueError("Split seed must be a nonnegative integer")
    order = list(identifiers)
    np.random.default_rng(seed).shuffle(order)
    count = len(order)
    train_count = max(1, int(round(fractions[0] * count)))
    test_count = max(1, int(round(fractions[2] * count)))
    if train_count + test_count > count:
        test_count = max(1, count - train_count)
    val_count = max(0, count - train_count - test_count)
    train = tuple(order[:train_count])
    val = tuple(order[train_count : train_count + val_count])
    test = tuple(order[train_count + val_count :])
    assignment = SplitAssignment(train, val, test, seed, tuple(fractions))
    validate_unseen_cad_split(assignment)
    return assignment


def assign_splits(workpiece_ids, fractions=(0.5, 0.25, 0.25), seed=0):
    """Assign splits, marking single-CAD runs as development-only."""
    identifiers = sorted({str(value) for value in workpiece_ids})
    if len(identifiers) >= 2:
        return unseen_cad_split(identifiers, fractions, seed)
    if not identifiers:
        raise ValueError("At least one workpiece is required")
    return SplitAssignment(
        train=tuple(identifiers),
        val=(),
        test=(),
        seed=seed,
        fractions=tuple(fractions),
    )


def validate_unseen_cad_split(assignment):
    """Raise if a workpiece identifier appears in more than one split."""
    groups = [set(assignment.train), set(assignment.val), set(assignment.test)]
    if sum(len(group) for group in groups) != len(set().union(*groups)):
        raise ValueError("A workpiece identifier appears in two splits")
    if not assignment.train or not assignment.test:
        raise ValueError("Train and test splits must each contain a workpiece")
    return True


def assert_samples_respect_split(samples):
    """Raise if any sample of one workpiece is assigned to two splits."""
    mapping = {}
    for sample in samples:
        workpiece = str(sample["workpiece_id"])
        split = str(sample["split"])
        if mapping.setdefault(workpiece, split) != split:
            raise ValueError(
                f"Workpiece {workpiece} leaks across {mapping[workpiece]} "
                f"and {split}"
            )
    return mapping


@dataclass(frozen=True)
class DraftSurfaceIndex:
    """Exact 2.5D projection on the draft-facing CAD surface.

    Large CAD faces make nearest-vertex heuristics unusable: a point inside a
    centimetre-scale flat triangle is far from every vertex. This index buckets
    draft-facing triangles into XY cells and solves the exact barycentric
    height per query, so roots and charts lie on the supplied mesh rather than
    on an approximation.
    """

    rotation: np.ndarray
    triangles: np.ndarray
    face_normals: np.ndarray
    face_ids: np.ndarray
    origin_m: np.ndarray
    cell_size_m: float
    shape: tuple
    offsets: np.ndarray
    indices: np.ndarray
    buckets: np.ndarray

    @classmethod
    def build(cls, mesh, direction, min_normal_dot=0.5, cell_size_m=0.0025):
        rotation = draft_rotation(direction)
        normals = mesh.face_normals @ rotation.T
        selected = np.flatnonzero(normals[:, 2] >= min_normal_dot)
        if not len(selected):
            raise ValueError("No draft-facing triangles for the surface index")
        triangles = mesh.triangles[selected] @ rotation.T
        xy = triangles[:, :, :2].reshape(-1, 2)
        origin = xy.min(axis=0) - cell_size_m
        upper = xy.max(axis=0) + cell_size_m
        shape = tuple(
            int(np.ceil((upper[axis] - origin[axis]) / cell_size_m)) + 1
            for axis in range(2)
        )
        rows, cols = shape
        counts = np.zeros(rows * cols, dtype=np.int64)
        cell_lists = []
        for triangle in triangles:
            row_start = max(
                0,
                int(np.floor((triangle[:, 1].min() - origin[1]) / cell_size_m)),
            )
            row_end = min(
                rows - 1,
                int(np.ceil((triangle[:, 1].max() - origin[1]) / cell_size_m)),
            )
            col_start = max(
                0,
                int(np.floor((triangle[:, 0].min() - origin[0]) / cell_size_m)),
            )
            col_end = min(
                cols - 1,
                int(np.ceil((triangle[:, 0].max() - origin[0]) / cell_size_m)),
            )
            if row_end < row_start or col_end < col_start:
                cell_lists.append(np.empty(0, dtype=np.int64))
                continue
            cell_rows = np.arange(row_start, row_end + 1)
            cell_cols = np.arange(col_start, col_end + 1)
            cells = (cell_rows[:, None] * cols + cell_cols[None, :]).ravel()
            counts[cells] += 1
            cell_lists.append(cells)
        offsets = np.r_[0, np.cumsum(counts)]
        indices = np.empty(int(offsets[-1]), dtype=np.int64)
        cursor = np.zeros(rows * cols, dtype=np.int64)
        for triangle_id, cells in enumerate(cell_lists):
            if not len(cells):
                continue
            position = offsets[cells] + cursor[cells]
            indices[position] = triangle_id
            cursor[cells] += 1
        width = min(int(counts.max()) if len(counts) else 0, 64)
        buckets = np.full((rows * cols, max(width, 1)), -1, dtype=np.int64)
        if width:
            cell_ids = np.repeat(np.arange(rows * cols), counts)
            rank = np.arange(len(indices)) - np.repeat(offsets[:-1], counts)
            keep = rank < width
            buckets[cell_ids[keep], rank[keep]] = indices[keep]
        return cls(
            rotation=rotation,
            triangles=triangles,
            face_normals=normals[selected],
            face_ids=selected,
            origin_m=origin,
            cell_size_m=cell_size_m,
            shape=shape,
            offsets=offsets,
            indices=indices,
            buckets=buckets,
        )

    def project(self, points_m, batch=512):
        """Return base points, normals, distances and a support mask."""
        points = np.asarray(points_m, dtype=float).reshape(-1, 3)
        draft = points @ self.rotation.T
        bases = np.empty_like(points)
        normals = np.empty_like(points)
        distances = np.empty(len(points))
        supported = np.zeros(len(points), bool)
        rows, cols = self.shape
        col_index = np.floor(
            (draft[:, 0] - self.origin_m[0]) / self.cell_size_m
        ).astype(int)
        row_index = np.floor(
            (draft[:, 1] - self.origin_m[1]) / self.cell_size_m
        ).astype(int)
        inside = (
            (col_index >= 0)
            & (col_index < cols)
            & (row_index >= 0)
            & (row_index < rows)
        )
        cells = np.where(inside, row_index * cols + col_index, 0)
        for start in range(0, len(points), batch):
            chunk = draft[start : start + batch]
            chunk_cells = cells[start : start + batch]
            chunk_inside = inside[start : start + batch]
            candidates = self.buckets[chunk_cells]
            valid = candidates >= 0
            safe = np.where(valid, candidates, 0)
            triangles = self.triangles[safe]
            best_height = np.full(len(chunk), -np.inf)
            best_distance = np.full(len(chunk), np.inf)
            best_triangle = np.zeros(len(chunk), dtype=np.int64)
            first = triangles[:, :, 0, :]
            second = triangles[:, :, 1, :]
            third = triangles[:, :, 2, :]
            determinant = (second[:, :, 1] - third[:, :, 1]) * (
                first[:, :, 0] - third[:, :, 0]
            ) + (third[:, :, 0] - second[:, :, 0]) * (
                first[:, :, 1] - third[:, :, 1]
            )
            safe_determinant = np.where(
                np.abs(determinant) < 1e-15, 1.0, determinant
            )
            weight_first = (
                (second[:, :, 1] - third[:, :, 1])
                * (chunk[:, None, 0] - third[:, :, 0])
                + (third[:, :, 0] - second[:, :, 0])
                * (chunk[:, None, 1] - third[:, :, 1])
            ) / safe_determinant
            weight_second = (
                (third[:, :, 1] - first[:, :, 1])
                * (chunk[:, None, 0] - third[:, :, 0])
                + (first[:, :, 0] - third[:, :, 0])
                * (chunk[:, None, 1] - third[:, :, 1])
            ) / safe_determinant
            weight_third = 1.0 - weight_first - weight_second
            contains = (
                valid
                & (np.abs(determinant) >= 1e-15)
                & (weight_first >= -1e-6)
                & (weight_second >= -1e-6)
                & (weight_third >= -1e-6)
            )
            height = (
                weight_first * first[:, :, 2]
                + weight_second * second[:, :, 2]
                + weight_third * third[:, :, 2]
            )
            distance = np.abs(height - chunk[:, None, 2])
            distance = np.where(contains, distance, np.inf)
            best = np.argmin(distance, axis=1)
            best_distance = distance[np.arange(len(chunk)), best]
            best_height = height[np.arange(len(chunk)), best]
            best_triangle = safe[np.arange(len(chunk)), best]
            found = (
                chunk_inside
                & np.isfinite(best_distance)
                & (best_distance <= 0.005)
            )
            supported[start : start + batch] = found
            bases[start : start + batch, 0] = chunk[:, 0]
            bases[start : start + batch, 1] = chunk[:, 1]
            bases[start : start + batch, 2] = np.where(
                found, best_height, chunk[:, 2]
            )
            normals[start : start + batch] = self.face_normals[best_triangle]
            distances[start : start + batch] = best_distance
        bases = bases @ self.rotation
        normals = normals @ self.rotation
        return bases, normals, distances, supported


@dataclass(frozen=True)
class MeshNeighbourhood:
    """Approximate closest-point projection on a triangle mesh.

    The candidate faces come from the nearest vertices' incident faces, so a
    projection costs a KD-tree query plus a small number of triangle distance
    evaluations instead of a scan over every face. The bounded error is set by
    the local triangulation of the supplied CAD.
    """

    mesh: trimesh.Trimesh
    vertex_tree: cKDTree
    candidate_faces: np.ndarray
    neighbours: int = 3

    @classmethod
    def build(cls, mesh, neighbours=3):
        faces = mesh.faces
        counts = np.bincount(faces.ravel(), minlength=len(mesh.vertices))
        width = max(int(counts.max()), 1)
        candidate = np.full((len(mesh.vertices), width), -1, dtype=np.int64)
        order = np.argsort(faces.ravel(), kind="stable")
        sorted_vertices = faces.ravel()[order]
        sorted_faces = np.repeat(np.arange(len(faces)), 3)[order]
        starts = np.r_[0, np.cumsum(counts)[:-1]]
        within = np.arange(len(faces) * 3) - np.repeat(starts, counts)
        candidate[sorted_vertices, within] = sorted_faces
        return cls(mesh, cKDTree(mesh.vertices), candidate, neighbours)

    def project(self, points_m, batch=256):
        """Return CAD base points, face normals and projection distances."""
        points = np.asarray(points_m, dtype=float).reshape(-1, 3)
        if not len(points):
            return (
                np.empty((0, 3)),
                np.empty((0, 3)),
                np.empty(0),
            )
        _, nearest = self.vertex_tree.query(points, k=self.neighbours)
        nearest = np.asarray(nearest).reshape(len(points), self.neighbours)
        bases = np.empty_like(points)
        normals = np.empty_like(points)
        distances = np.empty(len(points))
        for start in range(0, len(points), batch):
            chunk = points[start : start + batch]
            vertex_ids = nearest[start : start + batch]
            face_ids = self.candidate_faces[vertex_ids].reshape(len(chunk), -1)
            valid = face_ids >= 0
            safe = np.where(valid, face_ids, 0)
            triangles = self.mesh.triangles[safe]
            flat_triangles = triangles.reshape(-1, 3, 3)
            repeated = np.repeat(chunk, safe.shape[1], axis=0)
            closest = trimesh.triangles.closest_point(flat_triangles, repeated)
            offset = np.linalg.norm(closest - repeated, axis=1).reshape(
                len(chunk), -1
            )
            offset[~valid] = np.inf
            best = np.argmin(offset, axis=1)
            rows = np.arange(len(chunk))
            bases[start : start + batch] = closest.reshape(len(chunk), -1, 3)[
                rows, best
            ]
            distances[start : start + batch] = offset[rows, best]
            normals[start : start + batch] = self.mesh.face_normals[
                safe[rows, best]
            ]
        return bases, normals, distances


@dataclass
class LocalChart:
    """Projection onto the nominal CAD with a draft-surface exact fallback."""

    mesh: trimesh.Trimesh
    neighbourhood: MeshNeighbourhood
    surface_index: DraftSurfaceIndex | None
    center_m: np.ndarray
    radius_m: float
    draft_direction: tuple = (0.0, 0.0, 1.0)

    def project(self, points_m):
        """Return CAD base points, face normals and projection distances."""
        points = np.asarray(points_m, dtype=float).reshape(-1, 3)
        if self.surface_index is None:
            return self.neighbourhood.project(points)
        bases, normals, distances, supported = self.surface_index.project(
            points
        )
        if not supported.all():
            fallback = ~supported
            exact_bases, exact_normals, exact_distance = (
                self.neighbourhood.project(points[fallback])
            )
            bases[fallback] = exact_bases
            normals[fallback] = exact_normals
            distances[fallback] = exact_distance
        return bases, normals, distances


def build_local_chart(
    mesh,
    points_m,
    margin_m=0.035,
    neighbourhood=None,
    surface_index=None,
    draft_direction=(0.0, 0.0, 1.0),
):
    """Bind a feature to the nominal CAD without leaving the supplied mesh."""
    points = np.asarray(points_m, dtype=float)
    center = points.mean(axis=0)
    span = float(np.linalg.norm(points - center, axis=1).max())
    radius = span + margin_m
    return LocalChart(
        mesh,
        neighbourhood
        if neighbourhood is not None
        else MeshNeighbourhood.build(mesh),
        surface_index
        if surface_index is not None
        else DraftSurfaceIndex.build(mesh, draft_direction),
        center,
        radius,
        tuple(float(value) for value in draft_direction),
    )


def closed_shell(upper, lower):
    """Cap a two-sided parameter grid into a consistently wound solid."""
    rows, cols = upper.shape[:2]
    count = rows * cols
    vertices = np.vstack([upper.reshape(-1, 3), lower.reshape(-1, 3)])
    faces = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            index = row * cols + col
            right, below, diagonal = index + cols, index + cols + 1, index + 1
            faces.extend(
                [
                    [index, right, below],
                    [index, below, diagonal],
                    [index + count, below + count, right + count],
                    [index + count, diagonal + count, below + count],
                ]
            )
    edges = []
    for row in range(rows - 1):
        edges.extend(
            [
                [row * cols, (row + 1) * cols],
                [(row + 1) * cols + cols - 1, row * cols + cols - 1],
            ]
        )
    for col in range(cols - 1):
        edges.extend(
            [
                [col + 1, col],
                [(rows - 1) * cols + col, (rows - 1) * cols + col + 1],
            ]
        )
    for first, second in edges:
        faces.extend(
            [
                [first, first + count, second + count],
                [first, second + count, second],
            ]
        )
    shell = trimesh.Trimesh(vertices, faces, process=False)
    shell.fix_normals(multibody=True)
    return shell


@dataclass(frozen=True)
class ChartGrid:
    """Signed residual height field over a surface-following local chart."""

    u_m: np.ndarray
    v_m: np.ndarray
    base_m: np.ndarray
    normal_m: np.ndarray
    height_mm: np.ndarray
    origin_m: np.ndarray
    tangent: np.ndarray
    lateral: np.ndarray
    valid: np.ndarray

    def surface(self, height_mm=None):
        """Return chart surface points with an optional replacement height."""
        heights = self.height_mm if height_mm is None else height_mm
        return (
            self.base_m
            + self.normal_m * (np.asarray(heights) / 1000)[..., None]
        )

    def surface_mesh(self, height_mm=None):
        """Build a triangle strip over valid top-facing chart cells only."""
        points = self.surface(height_mm)
        rows, cols = points.shape[:2]
        vertices = points.reshape(-1, 3)
        faces = []
        for row in range(rows - 1):
            for col in range(cols - 1):
                if not (
                    self.valid[row, col]
                    and self.valid[row, col + 1]
                    and self.valid[row + 1, col]
                    and self.valid[row + 1, col + 1]
                ):
                    continue
                index = row * cols + col
                faces.extend(
                    [
                        [index, index + cols, index + cols + 1],
                        [index, index + cols + 1, index + 1],
                    ]
                )
        if not faces:
            raise ValueError("The chart has no valid top-facing cells")
        mesh = trimesh.Trimesh(vertices, np.asarray(faces, int), process=False)
        reference = self.normal_m[self.valid].mean(axis=0)
        if float(mesh.face_normals.mean(axis=0) @ reference) < 0:
            mesh.faces = mesh.faces[:, ::-1]
        return mesh


@dataclass
class DefectInstance:
    """One synthesized rooted defect plus its chart and provenance."""

    kind: str
    style: str
    mesh: trimesh.Trimesh
    vertex_residual_mm: np.ndarray
    roots_m: list
    root_tangents: list
    root_normals: list
    boundaries_m: list
    chart: ChartGrid
    peak_height_mm: float
    parameters: dict
    geometry_validation: dict

    def record(self):
        return {
            "kind": self.kind,
            "style": self.style,
            "peak_height_mm": self.peak_height_mm,
            "volume_mm3": float(self.mesh.volume * 1e9),
            "root_curve_count": len(self.roots_m),
            "root_length_mm": float(
                sum(_chain_length(root) for root in self.roots_m) * 1000
            ),
            "parameters": self.parameters,
            "geometry_validation": self.geometry_validation,
            "review_envelope_exceeded": bool(
                self.peak_height_mm > REVIEW_HEIGHT_MM
            ),
        }


def _chart_grid(chart, root, tangents, normals, half_width_m, margin_m=0.014):
    """Sample a surface-following chart around a root polyline."""
    root = np.asarray(root, dtype=float)
    tangents = unit(np.asarray(tangents, dtype=float))
    normals = unit(np.asarray(normals, dtype=float))
    distances = np.r_[
        0.0, np.cumsum(np.linalg.norm(np.diff(root, axis=0), axis=1))
    ]
    length = float(distances[-1])
    # Centering v on zero keeps the ridge centreline in the chart; an
    # off-centre 0.6 mm grid can miss a 0.3 mm half-width peak.
    u = np.linspace(
        -margin_m,
        length + margin_m,
        max(8, int(np.ceil((length + 2 * margin_m) / 0.0008)) + 1),
    )
    half_span = half_width_m + margin_m
    v = np.linspace(
        -half_span,
        half_span,
        2 * max(4, int(np.ceil(half_span / 0.0004))) + 1,
    )
    root_at_u = np.column_stack(
        [
            np.interp(np.clip(u, 0.0, length), distances, root[:, axis])
            for axis in range(3)
        ]
    )
    tangent_at_u = unit(
        np.column_stack(
            [
                np.interp(np.clip(u, 0.0, length), distances, tangents[:, axis])
                for axis in range(3)
            ]
        )
    )
    normal_at_u = unit(
        np.column_stack(
            [
                np.interp(np.clip(u, 0.0, length), distances, normals[:, axis])
                for axis in range(3)
            ]
        )
    )
    lateral_at_u = unit(np.cross(normal_at_u, tangent_at_u))
    plane = (
        root_at_u[:, None, :] + lateral_at_u[:, None, :] * v[None, :, None]
    ).reshape(-1, 3)
    bases, base_normals, distance = chart.project(plane)
    draft = unit(np.asarray(chart.draft_direction, dtype=float))
    valid = (distance <= 0.002) & ((base_normals @ draft) >= 0.5)
    return ChartGrid(
        u_m=u,
        v_m=v,
        base_m=bases.reshape(len(u), len(v), 3),
        normal_m=base_normals.reshape(len(u), len(v), 3),
        height_mm=np.zeros((len(u), len(v))),
        origin_m=root[len(root) // 2].copy(),
        tangent=tangent_at_u[len(u) // 2].copy(),
        lateral=lateral_at_u[len(u) // 2].copy(),
        valid=valid.reshape(len(u), len(v)),
    )


def _smooth_field(rng, count, lower, upper, smooth=8.0):
    """A spatially correlated field rather than independent per-vertex noise."""
    controls = rng.uniform(lower, upper, max(3, min(count, 9)))
    values = np.interp(
        np.linspace(0, 1, count),
        np.linspace(0, 1, len(controls)),
        controls,
    )
    return gaussian_filter1d(values, smooth, mode="nearest")


def _root_positions(root):
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(root, axis=0), axis=1))]


def _ridge_chart(
    chart_grid, root, height_mm, half_width_mm, gap, margin_m=0.014
):
    """Evaluate the ridge profile on the padded chart grid."""
    distances = _root_positions(root)
    length = float(distances[-1])
    values = np.zeros_like(chart_grid.height_mm)
    for row, sample in enumerate(chart_grid.u_m):
        if sample < 0.0 or sample > length:
            continue
        width = float(np.interp(sample, distances, half_width_mm)) / 1000
        lift = float(np.interp(sample, distances, height_mm))
        interrupted = float(np.interp(sample, distances, gap.astype(float)))
        if interrupted > 0.5:
            continue
        values[row] = (
            lift
            * np.cos(
                np.clip(chart_grid.v_m / max(width, 1e-6), -1.0, 1.0)
                * np.pi
                / 2
            )
            ** 2
        )
    return values


def _ridge_instance(chart, root, rng, style):
    """Build a variable or interrupted rooted flash ridge."""
    root = np.asarray(root, dtype=float)
    bases, normals, distance = chart.project(root)
    if distance.max() > 0.002:
        raise ValueError("The selected ridge root is far from the nominal CAD")
    root_snap_mm = float(distance.max() * 1000)
    root = bases
    tangents = unit(np.gradient(root, axis=0))
    lateral = unit(np.cross(normals, tangents))
    count = len(root)
    height_mm = _smooth_field(rng, count, 0.55, 2.6)
    half_width_mm = _smooth_field(rng, count, 0.3, 0.85)
    gap = np.zeros(count, dtype=bool)
    if style == "interrupted":
        for _ in range(2):
            center = rng.uniform(0.2, 0.8)
            width = rng.uniform(0.05, 0.11)
            gap |= np.abs(np.linspace(0, 1, count) - center) < width
    lift_mm = height_mm * (~gap)
    q = np.linspace(-1.0, 1.0, 17)
    span = (
        bases[:, None, :]
        + lateral[:, None, :]
        * (half_width_mm[:, None] * q[None, :] / 1000)[..., None]
    )
    span_base, span_normals, _ = chart.project(span.reshape(-1, 3))
    span_base = span_base.reshape(count, len(q), 3)
    span_normals = span_normals.reshape(count, len(q), 3)
    upper = (
        span_base
        + span_normals
        * (
            lift_mm[:, None] * np.cos(q * np.pi / 2)[None, :] ** 2
            - ROOT_EMBEDDING_MM
        )[..., None]
        / 1000
    )
    lower = span_base - span_normals * (ROOT_EMBEDDING_MM / 1000)
    upper += span_normals * 0.000015
    mesh = closed_shell(upper, lower)
    chart_grid = _chart_grid(
        chart,
        root,
        tangents,
        normals,
        float(half_width_mm.max()) / 1000,
        margin_m=0.005,
    )
    chart_grid.height_mm[...] = _ridge_chart(
        chart_grid, root, height_mm, half_width_mm, gap
    )
    boundaries = [
        chart.project(root + lateral * sign * half_width_mm[:, None] / 1000)[0]
        for sign in (-1.0, 1.0)
    ]
    return (
        mesh,
        [root],
        [tangents],
        [normals],
        boundaries,
        chart_grid,
        float(height_mm.max()),
        {
            "height_range_mm": [float(height_mm.min()), float(height_mm.max())],
            "half_width_mm": [
                float(half_width_mm.min()),
                float(half_width_mm.max()),
            ],
            "interrupted_gap_fraction": float(gap.mean()),
            "root_embedding_mm": ROOT_EMBEDDING_MM,
            "root_snap_mm": root_snap_mm,
        },
    )


def _sheet_instance(chart, root, rng, style):
    """Build a bent sheet flash and its visible top-down residual chart."""
    root = np.asarray(root, dtype=float)
    bases, normals, distance = chart.project(root)
    if distance.max() > 0.002:
        raise ValueError("The selected sheet root is far from the nominal CAD")
    root_snap_mm = float(distance.max() * 1000)
    root = bases
    tangents = unit(np.gradient(root, axis=0))
    lateral = unit(np.cross(normals, tangents))
    count = len(root)
    thickness_mm = _smooth_field(rng, count, 0.22, 0.6)
    bend = _smooth_field(rng, count, 1.4, 2.4)
    arc_mm = _smooth_field(rng, count, 3.5, 6.0)
    gap = np.zeros(count, dtype=bool)
    if style == "torn":
        for center in rng.uniform(0.1, 0.9, 3):
            gap |= np.abs(np.linspace(0, 1, count) - center) < rng.uniform(
                0.03, 0.06
            )
    arc_mm = arc_mm * (~gap)
    q = np.linspace(0.0, 1.0, 25)
    angle = bend[:, None] * q[None, :]
    radius = arc_mm[:, None] / bend[:, None]
    across = radius * (1.0 - np.cos(angle))
    above = radius * np.sin(angle)
    center = (
        bases[:, None, :]
        + lateral[:, None, :] * (across / 1000)[..., None]
        + normals[:, None, :] * (above / 1000)[..., None]
    )
    sheet_normal = (
        lateral[:, None, :] * np.cos(angle)[..., None]
        - normals[:, None, :] * np.sin(angle)[..., None]
    )
    half = thickness_mm[:, None, None] / 2000
    upper = center + half * sheet_normal
    lower = center - half * sheet_normal
    lower -= normals[:, None, :] * (ROOT_EMBEDDING_MM / 1000)
    mesh = closed_shell(upper, lower)
    visible = (above + thickness_mm[:, None] / 2 * np.cos(angle)) * (~gap)[
        :, None
    ]
    half_width = float((across.max() + thickness_mm.max() / 2) / 1000)
    chart_grid = _chart_grid(
        chart, root, tangents, normals, half_width, margin_m=0.005
    )
    total = _chain_length(root)
    sample_positions = np.linspace(0.0, total, count)
    for row, sample in enumerate(chart_grid.u_m):
        if sample < 0.0 or sample > total:
            continue
        lower = int(
            np.clip(np.searchsorted(sample_positions, sample) - 1, 0, count - 1)
        )
        upper = min(lower + 1, count - 1)
        span = max(sample_positions[upper] - sample_positions[lower], 1e-12)
        weight = float(
            np.clip((sample - sample_positions[lower]) / span, 0.0, 1.0)
        )
        row_across = (1.0 - weight) * across[lower] + weight * across[upper]
        row_visible = (1.0 - weight) * visible[lower] + weight * visible[upper]
        chart_grid.height_mm[row] = np.interp(
            chart_grid.v_m * 1000,
            row_across,
            row_visible,
            left=0.0,
            right=0.0,
        )
    boundaries = [
        chart.project(root + lateral * sign * thickness_mm[:, None] / 2000)[0]
        for sign in (-1.0, 1.0)
    ]
    return (
        mesh,
        [root],
        [tangents],
        [normals],
        boundaries,
        chart_grid,
        float(visible.max()),
        {
            "thickness_mm": [
                float(thickness_mm.min()),
                float(thickness_mm.max()),
            ],
            "sheet_arc_mm": [float(arc_mm.min()), float(arc_mm.max())],
            "bend_radians": [float(bend.min()), float(bend.max())],
            "free_edge_notches": 3 if style == "torn" else 0,
            "root_embedding_mm": ROOT_EMBEDDING_MM,
            "root_snap_mm": root_snap_mm,
        },
    )


def _compact_instance(chart, center, rng, kind):
    """Build a burr or a gate-root stub from a radial chart profile."""
    center = np.asarray(center, dtype=float)
    base, outward, _ = chart.project(center[None, :])
    center = base[0]
    normal = unit(outward[0])
    reference = np.array([0.0, 0.0, 1.0])
    if abs(float(normal @ reference)) > 0.9:
        reference = np.array([0.0, 1.0, 0.0])
    tangent = unit(np.cross(normal, reference))
    lateral = unit(np.cross(normal, tangent))
    if kind == "burr":
        diameter_mm = rng.uniform(4.0, 8.0)
        height_mm = rng.uniform(0.7, 2.8)
        plateau = 0.0
    else:
        diameter_mm = rng.uniform(7.0, 12.0)
        height_mm = rng.uniform(2.5, 4.5)
        plateau = 0.55
    radius = diameter_mm / 2000
    extent = radius + 0.008
    u = np.arange(-extent, extent + 0.0006, 0.0006)
    v = np.arange(-extent, extent + 0.0006, 0.0006)
    grid_u, grid_v = np.meshgrid(u, v, indexing="ij")
    radial = np.sqrt(grid_u**2 + grid_v**2) / radius
    envelope = np.clip(1.0 - radial, 0.0, 1.0)
    profile = height_mm * (
        plateau + (1.0 - plateau) * np.maximum(0.0, 1.0 - radial**2) ** 0.65
    )
    if kind == "burr":
        shape = 0.45 + 0.15 * (grid_u / radius + 1.0)
        for _ in range(3):
            lobe = rng.uniform(-0.5, 0.5, 2)
            spread = rng.uniform(0.18, 0.4)
            shape += rng.uniform(0.4, 1.0) * np.exp(
                -(
                    (grid_u / radius - lobe[0]) ** 2
                    + (grid_v / radius - lobe[1]) ** 2
                )
                / spread**2
            )
        profile = profile * shape / max(shape.max(), 1e-9) * 1.15
    else:
        profile += rng.normal(0.0, 0.05, profile.shape) * (radial < 0.7)
    profile[envelope <= 0.0] = 0.0
    plane = (
        center[None, None, :]
        + tangent[None, None, :] * grid_u[..., None]
        + lateral[None, None, :] * grid_v[..., None]
    ).reshape(-1, 3)
    bases, base_normals, base_distance = chart.project(plane)
    bases = bases.reshape(grid_u.shape + (3,))
    base_normals = base_normals.reshape(grid_u.shape + (3,))
    draft = unit(np.asarray(chart.draft_direction, dtype=float))
    chart_valid = (base_distance.reshape(grid_u.shape) <= 0.002) & (
        (base_normals @ draft) >= 0.5
    )
    upper = (
        bases
        + base_normals
        * np.maximum(profile, -ROOT_EMBEDDING_MM + 0.015)[..., None]
        / 1000
    )
    lower = bases - base_normals * (ROOT_EMBEDDING_MM / 1000)
    mesh = closed_shell(upper, lower)
    chart_grid = ChartGrid(
        u_m=u,
        v_m=v,
        base_m=bases,
        normal_m=base_normals,
        height_mm=profile,
        origin_m=center.copy(),
        tangent=tangent,
        lateral=lateral,
        valid=chart_valid.reshape(grid_u.shape),
    )
    footprint_angle = np.linspace(0.0, 2 * np.pi, 121)
    footprint = chart.project(
        center
        + tangent * (radius * np.cos(footprint_angle))[:, None]
        + lateral * (radius * np.sin(footprint_angle))[:, None]
    )[0]
    return (
        mesh,
        [footprint],
        [unit(np.gradient(footprint, axis=0))],
        [unit(np.tile(normal, (len(footprint), 1)))],
        [footprint],
        chart_grid,
        float(profile.max()),
        {
            "footprint_diameter_mm": float(diameter_mm),
            "plateau_fraction": plateau,
            "root_embedding_mm": ROOT_EMBEDDING_MM,
        },
    )


def _best_flat_segment(points, length_m, span_limit_mm=3.0):
    """Choose the flattest start offset for a long support candidate."""
    points = np.asarray(points, dtype=float)
    closed = np.vstack([points, points[0]])
    distances = np.r_[
        0.0, np.cumsum(np.linalg.norm(np.diff(closed, axis=0), axis=1))
    ]
    total = float(distances[-1])
    length = min(length_m, max(total - 0.005, 0.005))
    starts = np.arange(0.0, max(total - length, 0.0) + 1e-9, 0.001)
    if not len(starts):
        return 0.0
    best_start, best_span = 0.0, math.inf
    for start in starts:
        active = (distances >= start) & (distances <= start + length)
        if active.sum() < 3:
            continue
        span = float(np.ptp(closed[active, 2]))
        if span < best_span:
            best_start, best_span = float(start), span
    if best_span * 1000 > span_limit_mm:
        raise ValueError("No flat support window on the candidate curve")
    return best_start


def _sub_curve(points, normals, offset_m, length_m, *, closed=True):
    """Return a resampled, contiguous sub-polyline of the requested length."""
    base = (
        np.vstack([points, points[0]])
        if closed
        else np.asarray(points, dtype=float)
    )
    base_normals = (
        np.vstack([normals, normals[:1]])
        if closed
        else np.asarray(normals, dtype=float)
    )
    distances = np.r_[
        0.0, np.cumsum(np.linalg.norm(np.diff(base, axis=0), axis=1))
    ]
    start = min(offset_m, max(distances[-1] - length_m, 0.0))
    stop = min(start + length_m, distances[-1])
    if stop - start < 0.004:
        raise ValueError("Selected root curve is too short")
    count = max(6, int(np.ceil((stop - start) / 0.0008)) + 1)
    samples = np.linspace(start, stop, count)
    selected = np.column_stack(
        [np.interp(samples, distances, base[:, axis]) for axis in range(3)]
    )
    selected_normals = unit(
        np.column_stack(
            [
                np.interp(samples, distances, base_normals[:, axis])
                for axis in range(3)
            ]
        )
    )
    return selected, selected_normals


def _exact_support_mm(mesh, root_points, sample_count=24):
    """Check sampled root points with an exact full-mesh projection."""
    points = np.asarray(root_points, dtype=float)
    selected = points[
        np.linspace(
            0, len(points) - 1, min(sample_count, len(points)), dtype=int
        )
    ]
    _, distance, _ = trimesh.proximity.closest_point_naive(mesh, selected)
    return float(distance.max() * 1000), int(len(selected))


def _validate_defect(mesh, chart, cad_chart, roots):
    """Check the closed-solid and CAD-root invariants."""
    if not mesh.is_watertight or not mesh.is_winding_consistent:
        raise ValueError(
            "Generated defect is not a closed, consistently wound solid"
        )
    if mesh.volume <= 0:
        raise ValueError("Generated defect has non-positive volume")
    root_points = np.vstack(roots)
    _, _, distance = cad_chart.project(root_points)
    error_mm = float(distance.max() * 1000)
    if error_mm > 0.05:
        raise ValueError("A generated root does not lie on the nominal CAD")
    exact_mm, exact_samples = _exact_support_mm(cad_chart.mesh, root_points)
    if exact_mm > 0.1:
        raise ValueError("Exact root projection leaves the nominal CAD")
    return {
        "watertight": True,
        "winding_consistent": True,
        "positive_volume": True,
        "positive_volume_mm3": float(mesh.volume * 1e9),
        "root_max_cad_distance_mm": error_mm,
        "exact_support_max_mm": exact_mm,
        "exact_support_samples": exact_samples,
        "chart_height_min_mm": float(chart.height_mm.min()),
        "chart_height_max_mm": float(chart.height_mm.max()),
    }


def _defect_entries(kind, feature_set):
    """Candidate support curves long enough for one defect kind."""
    if kind == "flash":
        minimum = 0.032
        inset = [
            line
            for line in feature_set.by_kind("parting_candidate")
            if line.source == "draft_silhouette_inset" and line.length_m >= 0.05
        ]
        entries = inset or (
            feature_set.by_kind("gate_root_zone")
            + [
                line
                for line in feature_set.by_kind("parting_candidate")
                if line.length_m >= 0.05
                and line.metadata.get("mean_top_access", 0.0) >= 0.6
            ]
        )
    elif kind == "burr":
        minimum = 0.001
        interior = feature_set.by_kind("flat_surface")
        entries = interior or (
            [
                line
                for line in feature_set.by_kind("sharp_edge")
                if line.length_m >= 0.008
                and line.metadata.get("draft_dot_max", 0.0) >= 0.8
            ]
            + feature_set.by_kind("gate_root_zone")
        )
    elif kind == "casting_root":
        minimum = 0.001
        interior = feature_set.by_kind("flat_surface")
        entries = interior or feature_set.by_kind("gate_root_zone") + [
            line
            for line in feature_set.by_kind("parting_candidate")
            if line.length_m >= 0.03
        ]
    else:
        raise ValueError(f"Unknown defect kind: {kind}")
    return [line for line in entries if line.length_m >= minimum]


def synthesize_defect(
    kind,
    feature_set,
    mesh,
    seed,
    occupied,
    style=None,
    neighbourhood=None,
    surface_index=None,
):
    """Create one rooted defect on the CAD using extracted features."""
    entries = _defect_entries(kind, feature_set)
    if not entries:
        raise ValueError(
            f"CAD {feature_set.workpiece_id} has no support for {kind}"
        )
    digest = hashlib.sha256(
        f"{feature_set.workpiece_id}:{seed}:{kind}".encode()
    )
    rng = np.random.default_rng(int.from_bytes(digest.digest()[:8], "big"))
    errors = []
    for _ in range(30):
        entry = entries[int(rng.integers(len(entries)))]
        try:
            if kind == "flash":
                chart = build_local_chart(
                    mesh,
                    entry.points_m,
                    neighbourhood=neighbourhood,
                    surface_index=surface_index,
                    draft_direction=feature_set.config.draft_direction,
                )
                length = rng.uniform(0.03, min(0.07, entry.length_m))
                offset = _best_flat_segment(entry.points_m, length)
                root, _ = _sub_curve(
                    entry.points_m,
                    entry.normals_m,
                    offset,
                    length,
                    closed=entry.closed,
                )
                chosen_style = style or str(
                    rng.choice(["variable", "curled", "interrupted"])
                )
                if chosen_style in {"variable", "interrupted"}:
                    result = _ridge_instance(
                        chart,
                        root,
                        rng,
                        "interrupted"
                        if chosen_style == "interrupted"
                        else "variable",
                    )
                elif chosen_style in {"curled", "torn"}:
                    result = _sheet_instance(
                        chart,
                        root,
                        rng,
                        "torn" if chosen_style == "torn" else "curled",
                    )
                else:
                    raise ValueError(f"Unknown flash style: {chosen_style}")
                chosen_style = (
                    "variable_ridge"
                    if chosen_style == "variable"
                    else "thin_curled_sheet"
                    if chosen_style == "curled"
                    else chosen_style
                )
            elif kind == "burr":
                chart = build_local_chart(
                    mesh,
                    entry.points_m,
                    neighbourhood=neighbourhood,
                    surface_index=surface_index,
                    draft_direction=feature_set.config.draft_direction,
                )
                center = entry.points_m[int(rng.integers(len(entry.points_m)))]
                result = _compact_instance(chart, center, rng, "burr")
                chosen_style = "asymmetric_lobed"
            else:
                chart = build_local_chart(
                    mesh,
                    entry.points_m,
                    neighbourhood=neighbourhood,
                    surface_index=surface_index,
                    draft_direction=feature_set.config.draft_direction,
                )
                if entry.kind == "flat_surface":
                    center = entry.points_m[len(entry.points_m) // 2]
                else:
                    root, _ = _sub_curve(
                        entry.points_m,
                        entry.normals_m,
                        rng.uniform(0.0, max(0.0, entry.length_m * 0.4)),
                        rng.uniform(
                            0.012, min(0.024, max(entry.length_m * 0.9, 0.012))
                        ),
                        closed=entry.closed,
                    )
                    center = root[len(root) // 2]
                result = _compact_instance(chart, center, rng, "casting_root")
                chosen_style = "gate_stub"
            (
                defect_mesh,
                roots,
                tangents,
                normals,
                boundaries,
                chart_grid,
                peak,
                parameters,
            ) = result
            validation = _validate_defect(defect_mesh, chart_grid, chart, roots)
            bases = chart_grid.base_m.reshape(-1, 3)
            normals_flat = chart_grid.normal_m.reshape(-1, 3)
            _, nearest = cKDTree(bases).query(defect_mesh.vertices)
            residual = (
                np.einsum(
                    "ij,ij->i",
                    defect_mesh.vertices - bases[nearest],
                    normals_flat[nearest],
                )
                * 1000
            )
            candidate = DefectInstance(
                kind=kind,
                style=chosen_style,
                mesh=defect_mesh,
                vertex_residual_mm=residual,
                roots_m=[np.asarray(value, dtype=float) for value in roots],
                root_tangents=[
                    np.asarray(value, dtype=float) for value in tangents
                ],
                root_normals=[
                    np.asarray(value, dtype=float) for value in normals
                ],
                boundaries_m=[
                    np.asarray(value, dtype=float) for value in boundaries
                ],
                chart=chart_grid,
                peak_height_mm=float(peak),
                parameters=parameters,
                geometry_validation=validation,
            )
            if any(
                np.linalg.norm(
                    candidate.roots_m[0].mean(axis=0)
                    - other.roots_m[0].mean(axis=0)
                )
                < 0.025
                for other in occupied
            ):
                errors.append("overlaps an already generated defect")
                continue
            occupied.append(candidate)
            return candidate
        except (ValueError, RuntimeError) as error:
            errors.append(str(error))
            continue
    raise ValueError(
        f"Could not construct a supported {kind} on CAD "
        f"{feature_set.workpiece_id}: {errors[-1] if errors else 'no candidate'}"
    )


def generate_defects(mesh, feature_set, seed, kinds=tuple(DEFECT_SEMANTICS)):
    """Generate one instance of each requested defect kind."""
    occupied = []
    instances = []
    neighbourhood = MeshNeighbourhood.build(mesh)
    surface_index = DraftSurfaceIndex.build(
        mesh, feature_set.config.draft_direction
    )
    for kind in kinds:
        instances.append(
            synthesize_defect(
                kind,
                feature_set,
                mesh,
                seed,
                occupied,
                neighbourhood=neighbourhood,
                surface_index=surface_index,
            )
        )
    return instances


def intermediate_height(chart, state, rng):
    """Return a signed residual field for one machining intermediate state."""
    heights = np.asarray(chart.height_mm, dtype=float).copy()
    valid = np.asarray(chart.valid, dtype=bool)
    peak = float(heights[valid].max()) if valid.any() else 0.0
    if peak <= 0.05:
        raise ValueError("Intermediate state requires a positive excess chart")
    if state == "rough_residual":
        fraction = gaussian_filter(rng.uniform(0.38, 0.55, heights.shape), 2.0)
        result = heights * (1.0 - fraction)
    elif state == "thin_stock":
        cap = rng.uniform(0.10, 0.22)
        smoothed = gaussian_filter(heights, 1.5)
        result = np.minimum(np.maximum(smoothed, 0.0), cap)
    elif state == "micro_overgrind":
        rows, cols = np.indices(heights.shape)
        centre_u = (len(chart.u_m) - 1) / 2
        centre_v = (len(chart.v_m) - 1) / 2
        extent_u = max(centre_u * 0.6, 1.0)
        extent_v = max(centre_v * 0.5, 1.0)
        radial = np.sqrt(
            ((rows - centre_u) / extent_u) ** 2
            + ((cols - centre_v) / extent_v) ** 2
        )
        taper = np.clip((radial - 0.55) / 0.45, 0.0, 1.0)
        weight = 1.0 - taper * taper * (3.0 - 2.0 * taper)
        weight = gaussian_filter(weight, 1.5)
        overshoot_mm = rng.uniform(0.04, 0.12)
        # Blend to a shallow negative residual in the band core; the band edge
        # returns to the untouched chart height instead of subtracting a peak
        # uniformly across a thin flash.
        result = (1.0 - weight) * heights - weight * overshoot_mm
    else:
        raise ValueError(f"Unknown intermediate state: {state}")
    return np.where(valid, result, 0.0)


@dataclass(frozen=True)
class SensorDegradationProfile:
    """Configurable structured-light degradation for synthetic observations.

    ``incidence_sigma_gain_mm`` adds axial sigma as the viewing angle
    approaches grazing. ``edge_flying_fraction`` mixes discontinuity pixels
    between the two adjacent surfaces. Specular and oxide patches produce
    connected dropout; oxide also lifts the surface slightly. Registration
    drift is a bounded sub-millimetre camera-frame perturbation.
    """

    base_depth_sigma_mm: float = 0.05
    range_sigma_mm_per_m: float = 0.35
    incidence_sigma_gain_mm: float = 0.9
    incidence_power: float = 1.6
    base_dropout: float = 0.004
    incidence_dropout_gain: float = 0.22
    grazing_angle_deg: float = 78.0
    max_dropout: float = 0.95
    edge_gradient_mm: float = 1.0
    edge_band_px: int = 1
    edge_flying_fraction: float = 0.25
    specular_patches_per_m2: float = 24.0
    specular_radius_mm: tuple = (1.5, 5.0)
    specular_dropout: float = 0.85
    oxide_patches_per_m2: float = 10.0
    oxide_radius_mm: tuple = (2.0, 7.0)
    oxide_dropout: float = 0.55
    oxide_bias_mm: float = 0.10
    registration_translation_mm: float = 0.15
    registration_rotation_mrad: float = 0.6
    normal_bias_mm: float = 0.03
    normal_bias_pattern_mm: float = 0.02
    frames_averaged: int = 1
    calibrated_from_real_scans: bool = False

    def __post_init__(self):
        for name in (
            "base_depth_sigma_mm",
            "range_sigma_mm_per_m",
            "incidence_sigma_gain_mm",
            "base_dropout",
            "incidence_dropout_gain",
            "edge_gradient_mm",
            "edge_flying_fraction",
            "specular_patches_per_m2",
            "specular_dropout",
            "oxide_patches_per_m2",
            "oxide_dropout",
            "oxide_bias_mm",
            "registration_translation_mm",
            "registration_rotation_mrad",
            "normal_bias_mm",
            "normal_bias_pattern_mm",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a nonnegative finite value")
        if not 0.0 < self.grazing_angle_deg < 90.0:
            raise ValueError("grazing_angle_deg must lie in (0, 90)")
        if not 0.0 <= self.max_dropout <= 1.0:
            raise ValueError("max_dropout must lie in [0, 1]")
        if type(self.edge_band_px) is not int or self.edge_band_px < 1:
            raise ValueError("edge_band_px must be a positive integer")
        if type(self.frames_averaged) is not int or self.frames_averaged < 1:
            raise ValueError("frames_averaged must be a positive integer")
        for name in ("specular_radius_mm", "oxide_radius_mm"):
            pair = getattr(self, name)
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or pair[0] < 0
                or pair[1] <= pair[0]
            ):
                raise ValueError(f"{name} must be an increasing pair")

    def record(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def draw(self, rng):
        """Draw one concrete degradation parameter set for a sample."""
        return SensorDegradationSample(
            base_sigma_mm=float(self.base_depth_sigma_mm),
            range_sigma_mm_per_m=float(self.range_sigma_mm_per_m),
            incidence_gain_mm=float(
                self.incidence_sigma_gain_mm * rng.uniform(0.8, 1.2)
            ),
            incidence_power=float(self.incidence_power),
            base_dropout=float(self.base_dropout),
            incidence_dropout=float(
                self.incidence_dropout_gain * rng.uniform(0.8, 1.2)
            ),
            grazing_angle_deg=float(self.grazing_angle_deg),
            max_dropout=float(self.max_dropout),
            edge_gradient_mm=float(self.edge_gradient_mm),
            edge_band_px=self.edge_band_px,
            edge_flying_fraction=float(
                self.edge_flying_fraction * rng.uniform(0.7, 1.3)
            ),
            specular_rate=float(self.specular_patches_per_m2),
            specular_radius_mm=tuple(
                float(value) for value in self.specular_radius_mm
            ),
            specular_dropout=float(self.specular_dropout),
            oxide_rate=float(self.oxide_patches_per_m2),
            oxide_radius_mm=tuple(
                float(value) for value in self.oxide_radius_mm
            ),
            oxide_dropout=float(self.oxide_dropout),
            oxide_bias_mm=float(self.oxide_bias_mm),
            registration_translation_mm=tuple(
                float(value)
                for value in rng.uniform(
                    -self.registration_translation_mm,
                    self.registration_translation_mm,
                    3,
                )
            ),
            registration_rotation_mrad=tuple(
                float(value)
                for value in rng.uniform(
                    -self.registration_rotation_mrad,
                    self.registration_rotation_mrad,
                    3,
                )
            ),
            normal_bias_mm=float(self.normal_bias_mm),
            normal_bias_pattern_mm=float(self.normal_bias_pattern_mm),
            frames_averaged=int(self.frames_averaged),
        )

    @classmethod
    def calibrate_from_residuals(
        cls, residuals_mm, incidence_deg, ranges_m, *, missing=None
    ):
        """Fit degradation parameters from measured signed residuals.

        Args:
            residuals_mm: Signed depth residual per valid pixel, in millimetres.
            incidence_deg: Surface-to-view incidence angle per valid pixel.
            ranges_m: Camera range per valid pixel, in metres.
            missing: Optional boolean mask over the same rows marking dropout.

        Returns:
            A profile with fitted base noise, incidence gain, range gain,
            dropout slope and offset, plus a calibration report. The result is
            a fit to the supplied residuals; it is not an identified scanner
            model until the residuals come from a calibrated artifact.
        """
        residuals = np.asarray(residuals_mm, dtype=float).reshape(-1)
        angles = np.asarray(incidence_deg, dtype=float).reshape(-1)
        ranges = np.asarray(ranges_m, dtype=float).reshape(-1)
        if not (
            len(residuals) == len(angles) == len(ranges)
            and len(residuals) >= 32
            and np.isfinite(residuals).all()
            and np.isfinite(angles).all()
            and np.isfinite(ranges).all()
            and (angles >= 0).all()
            and (angles < 90).all()
        ):
            raise ValueError(
                "Calibration requires matched finite residuals, incidence and "
                "range"
            )
        # A 2D angle x range grid is needed to identify the intercept and the
        # range slope separately; a 1D angle grid makes them collinear.
        angle_bins = np.digitize(
            angles, [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0]
        )
        range_edges = np.quantile(ranges, [1.0 / 3.0, 2.0 / 3.0])
        if np.ptp(ranges) <= 1e-6:
            range_bins = np.zeros(len(ranges), dtype=int)
        else:
            range_bins = np.digitize(ranges, range_edges)
        rows = []
        for angle_bin in np.unique(angle_bins):
            for range_bin in np.unique(range_bins):
                selected = (angle_bins == angle_bin) & (range_bins == range_bin)
                if selected.sum() < 24:
                    continue
                centered = residuals[selected] - np.median(residuals[selected])
                sigma = 1.4826 * np.median(np.abs(centered))
                rows.append(
                    (
                        float(np.median(angles[selected])),
                        float(np.median(ranges[selected])),
                        float(sigma),
                        int(selected.sum()),
                    )
                )
        if len(rows) < 4:
            raise ValueError(
                "Insufficient angle/range cells for a degradation fit"
            )
        design = []
        target = []
        for angle, distance, sigma, _ in rows:
            secant = 1.0 / max(math.cos(math.radians(angle)), 0.05) - 1.0
            design.append([1.0, distance, secant])
            target.append(sigma)
        coefficients = np.linalg.lstsq(
            np.asarray(design), np.asarray(target), rcond=None
        )[0]
        base = max(float(coefficients[0]), 0.0)
        range_gain = max(float(coefficients[1]), 0.0)
        incidence_gain = max(float(coefficients[2]), 0.0)
        bias = float(np.median(residuals))
        dropout_gain = 0.0
        base_dropout = 0.0
        if missing is not None:
            mask = np.asarray(missing, dtype=bool).reshape(-1)
            if len(mask) != len(residuals):
                raise ValueError(
                    "Dropout mask does not match the residual rows"
                )
            base_dropout = float(mask.mean())
            rates = []
            for angle in (10.0, 25.0, 40.0, 55.0, 68.0):
                near = np.abs(angles - angle) < 5.0
                if near.sum() >= 16:
                    rates.append((angle, float(mask[near].mean())))
            if len(rates) >= 2:
                fitted_angles, values = zip(*rates)
                secant = 1.0 / np.cos(np.radians(fitted_angles)) - 1.0
                dropout_gain = max(float(np.polyfit(secant, values, 1)[0]), 0.0)
        profile = cls(
            base_depth_sigma_mm=base,
            range_sigma_mm_per_m=range_gain,
            incidence_sigma_gain_mm=incidence_gain,
            base_dropout=base_dropout,
            incidence_dropout_gain=dropout_gain,
            normal_bias_mm=abs(bias),
            normal_bias_pattern_mm=0.0,
            calibrated_from_real_scans=False,
        )
        return profile, {
            "source_rows": int(len(residuals)),
            "angle_bins_deg": [row[0] for row in rows],
            "range_bins_m": [row[1] for row in rows],
            "sigma_per_cell_mm": [row[2] for row in rows],
            "cell_counts": [row[3] for row in rows],
            "fitted_base_sigma_mm": base,
            "fitted_range_gain_mm_per_m": range_gain,
            "fitted_incidence_gain_mm": incidence_gain,
            "fitted_base_dropout": base_dropout,
            "fitted_dropout_gain": dropout_gain,
            "signed_median_bias_mm": bias,
            "residual_mad_mm": float(
                1.4826 * np.median(np.abs(residuals - np.median(residuals)))
            ),
            "calibration_status": "fitted_to_supplied_residuals",
        }


@dataclass(frozen=True)
class SensorDegradationSample:
    """One concrete draw of the degradation parameters."""

    base_sigma_mm: float
    range_sigma_mm_per_m: float
    incidence_gain_mm: float
    incidence_power: float
    base_dropout: float
    incidence_dropout: float
    grazing_angle_deg: float
    max_dropout: float
    edge_gradient_mm: float
    edge_band_px: int
    edge_flying_fraction: float
    specular_rate: float
    specular_radius_mm: tuple
    specular_dropout: float
    oxide_rate: float
    oxide_radius_mm: tuple
    oxide_dropout: float
    oxide_bias_mm: float
    registration_translation_mm: tuple
    registration_rotation_mrad: tuple
    normal_bias_mm: float
    normal_bias_pattern_mm: float
    frames_averaged: int

    def record(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class CameraModel:
    """Pinhole camera with +Z optical axis and image rows increasing in +Y."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    position_m: np.ndarray
    rotation: np.ndarray
    fov_deg: float = 40.0

    def project(self, points_m):
        """Project workpiece points to pixels and camera-frame coordinates."""
        points = np.asarray(points_m, dtype=float).reshape(-1, 3)
        camera = (points - self.position_m) @ self.rotation
        safe = np.maximum(camera[:, 2], 1e-9)
        pixels = np.column_stack(
            [
                camera[:, 0] / safe * self.fx + self.cx,
                camera[:, 1] / safe * self.fy + self.cy,
            ]
        )
        return pixels, camera

    def unproject(self, depth_m):
        """Unproject a camera-frame depth image into workpiece points."""
        rows, cols = np.indices(depth_m.shape)
        camera = np.stack(
            [
                (cols - self.cx) / self.fx * depth_m,
                (rows - self.cy) / self.fy * depth_m,
                depth_m,
            ],
            axis=-1,
        )
        return camera @ self.rotation.T + self.position_m

    def pixel_scale_m(self, depth_m):
        return np.asarray(depth_m) / self.fx

    def record(self):
        return {
            "width": self.width,
            "height": self.height,
            "K": [
                [self.fx, 0.0, self.cx],
                [0.0, self.fy, self.cy],
                [0.0, 0.0, 1.0],
            ],
            "T_camera_workpiece": np.vstack(
                [
                    np.column_stack([self.rotation, self.position_m]),
                    [0, 0, 0, 1],
                ]
            ).tolist(),
            "position_m": self.position_m.tolist(),
            "fov_deg": self.fov_deg,
            "convention": "+Z optical axis, image rows along +Y",
        }


def look_at(position_m, target_m, width, height, fov_deg):
    """Build a camera looking at a target with image rows along camera +Y."""
    position = np.asarray(position_m, dtype=float)
    forward = unit(np.asarray(target_m, dtype=float) - position)
    reference = np.array([0.0, 0.0, 1.0])
    if abs(float(forward @ reference)) > 0.95:
        reference = np.array([0.0, 1.0, 0.0])
    right = unit(np.cross(forward, reference))
    down = unit(np.cross(forward, right))
    rotation = np.column_stack([right, down, forward])
    focal = (width / 2) / math.tan(math.radians(fov_deg) / 2)
    return CameraModel(
        width=width,
        height=height,
        fx=float(focal),
        fy=float(focal),
        cx=(width - 1) / 2,
        cy=(height - 1) / 2,
        position_m=position,
        rotation=rotation,
        fov_deg=float(fov_deg),
    )


def rasterize_mesh(mesh, camera, face_values=None, near_m=0.03):
    """Z-buffer triangle rasterization returning depth and face identifiers.

    Args:
        mesh: Triangle mesh in the workpiece frame.
        camera: Camera model whose +Z axis is the optical axis.
        face_values: Optional per-face value sampled into an image.
        near_m: Minimum camera-frame depth retained by the rasterizer.

    Returns:
        Mapping with depth, face identifier, surface mask and optional values.
    """
    vertices = (mesh.vertices - camera.position_m) @ camera.rotation
    safe_depth = np.maximum(vertices[:, 2], 1e-9)
    projected = np.column_stack(
        [
            vertices[:, 0] / safe_depth * camera.fx + camera.cx,
            vertices[:, 1] / safe_depth * camera.fy + camera.cy,
        ]
    )
    view = mesh.triangles_center - camera.position_m
    facing = np.einsum("ij,ij->i", mesh.face_normals, view) < 0
    face_depth = vertices[mesh.faces][:, :, 2]
    usable = facing & (face_depth > near_m).all(axis=1)
    candidates = np.flatnonzero(usable)
    if len(candidates):
        triangle_pixels = projected[mesh.faces[candidates]]
        left = triangle_pixels[:, :, 0]
        top = triangle_pixels[:, :, 1]
        inside_frame = ~(
            (left.max(axis=1) < 0)
            | (left.min(axis=1) > camera.width - 1)
            | (top.max(axis=1) < 0)
            | (top.min(axis=1) > camera.height - 1)
        )
        candidates = candidates[inside_frame]
    depth = np.full((camera.height, camera.width), np.inf)
    face_image = np.full((camera.height, camera.width), -1, dtype=np.int64)
    for face in candidates:
        corners = mesh.faces[face]
        uv = projected[corners]
        col_start = max(0, int(math.floor(uv[:, 0].min())))
        col_end = min(camera.width - 1, int(math.ceil(uv[:, 0].max())))
        row_start = max(0, int(math.floor(uv[:, 1].min())))
        row_end = min(camera.height - 1, int(math.ceil(uv[:, 1].max())))
        if col_end < col_start or row_end < row_start:
            continue
        sample_x, sample_y = np.meshgrid(
            np.arange(col_start, col_end + 1),
            np.arange(row_start, row_end + 1),
        )
        first_x, first_y = uv[0]
        second_x, second_y = uv[1]
        third_x, third_y = uv[2]
        determinant = (second_y - third_y) * (first_x - third_x) + (
            third_x - second_x
        ) * (first_y - third_y)
        if abs(determinant) < 1e-12:
            continue
        weight_first = (
            (second_y - third_y) * (sample_x - third_x)
            + (third_x - second_x) * (sample_y - third_y)
        ) / determinant
        weight_second = (
            (third_y - first_y) * (sample_x - third_x)
            + (first_x - third_x) * (sample_y - third_y)
        ) / determinant
        weight_third = 1.0 - weight_first - weight_second
        inside = (
            (weight_first >= -1e-6)
            & (weight_second >= -1e-6)
            & (weight_third >= -1e-6)
        )
        if not inside.any():
            continue
        z = (
            weight_first * vertices[corners[0], 2]
            + weight_second * vertices[corners[1], 2]
            + weight_third * vertices[corners[2], 2]
        )
        patch = depth[row_start : row_end + 1, col_start : col_end + 1]
        closer = inside & (z < patch)
        patch[closer] = z[closer]
        face_patch = face_image[
            row_start : row_end + 1, col_start : col_end + 1
        ]
        face_patch[closer] = face
    surface = np.isfinite(depth)
    result = {
        "depth_m": np.where(surface, depth, np.nan),
        "face_index": np.where(surface, face_image, -1),
        "surface": surface,
    }
    if face_values is not None:
        values = np.asarray(face_values)
        result["face_value"] = np.where(
            surface, values[np.clip(face_image, 0, len(values) - 1)], np.nan
        )
    return result


def _filled_depth(depth):
    """Fill invalid depth by the nearest valid value."""
    valid = np.isfinite(depth)
    filled = depth.copy()
    if valid.any() and (~valid).any():
        indices = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled[~valid] = depth[tuple(indices[:, ~valid])]
    return filled


def _depth_normals(points):
    """Camera-facing normals from local depth derivatives."""
    rows = np.gradient(points, axis=0)
    cols = np.gradient(points, axis=1)
    normals = unit(np.cross(cols, rows))
    normals[np.einsum("ijk,ijk->ij", normals, points) > 0] *= -1
    return normals


def _reproject_depth(points_m, camera, shape):
    """Z-buffer a camera-frame point set back into an image."""
    uv, camera_points = camera.project(points_m)
    columns = np.rint(uv[:, 0]).astype(int)
    rows = np.rint(uv[:, 1]).astype(int)
    valid = (
        np.isfinite(uv).all(axis=1)
        & (camera_points[:, 2] > 0.0)
        & (columns >= 0)
        & (columns < shape[1])
        & (rows >= 0)
        & (rows < shape[0])
    )
    depth = np.full(shape, np.inf)
    columns, rows = columns[valid], rows[valid]
    values = camera_points[valid, 2]
    order = np.argsort(-values)
    depth[rows[order], columns[order]] = values[order]
    return np.where(np.isfinite(depth), depth, np.nan)


def _dropout_patches(
    surface, depth, camera, rate, radius_pair, probability, rng
):
    """Draw connected patch dropout and return both patch and dropped masks."""
    patch = np.zeros(depth.shape, bool)
    dropped = np.zeros(depth.shape, bool)
    rows, cols = np.indices(depth.shape)
    visible = np.flatnonzero(surface.reshape(-1))
    area_m2 = float(np.nansum(depth[surface] ** 2) / (camera.fx * camera.fy))
    count = int(round(rate * area_m2))
    for _ in range(count):
        if not len(visible):
            break
        pick = int(visible[int(rng.integers(len(visible)))])
        row, col = divmod(pick, depth.shape[1])
        if not surface[row, col]:
            continue
        radius_mm = rng.uniform(*radius_pair)
        radius_px = max(
            1.0,
            radius_mm
            / 1000
            / max(float(camera.pixel_scale_m(depth[row, col])), 1e-6),
        )
        inside = (
            (cols - col) ** 2 + (rows - row) ** 2 <= radius_px**2
        ) & surface
        drawn = rng.random(depth.shape) < probability
        patch |= inside
        dropped |= inside & drawn
    return patch, dropped


def degrade_depth(ideal_depth_m, camera, profile_sample, rng):
    """Render one degraded structured-light observation.

    Args:
        ideal_depth_m: Noiseless camera-frame depth from the same camera.
        camera: Camera model matching the depth image.
        profile_sample: Concrete degradation parameters from a profile.
        rng: Numpy generator used for noise and patch placement.

    Returns:
        Mapping with observed depth, per-pixel sigma, incidence, masks, the
        unprojected observed points and the registration drift applied.
    """
    depth = np.asarray(ideal_depth_m, dtype=float)
    surface = np.isfinite(depth) & (depth > 0.0)
    if not surface.any():
        raise ValueError("Ideal depth contains no surface")
    observed_surface = surface.copy()
    filled = _filled_depth(depth)
    filled_points = camera.unproject(filled)
    rows, cols = np.indices(depth.shape)
    direction = unit(filled_points - camera.position_m)
    normals = _depth_normals(filled_points)
    cosine = np.clip(
        np.abs(np.einsum("ijk,ijk->ij", normals, direction)), 0.05, 1.0
    )
    incidence = np.degrees(np.arccos(cosine))
    range_m = np.where(surface, depth, np.nan)
    sigma = (
        profile_sample.base_sigma_mm
        + profile_sample.range_sigma_mm_per_m * range_m
        + profile_sample.incidence_gain_mm
        * (1.0 / cosine - 1.0) ** profile_sample.incidence_power
    )
    sigma = np.where(surface, np.clip(sigma, 0.0, 3.0), np.nan)
    noise = rng.normal(0.0, 1.0, depth.shape) * np.nan_to_num(sigma)
    observed = depth + noise / 1000.0

    specular_patch, specular_dropped = _dropout_patches(
        surface,
        depth,
        camera,
        profile_sample.specular_rate,
        profile_sample.specular_radius_mm,
        profile_sample.specular_dropout,
        rng,
    )
    oxide_patch, oxide_dropped = _dropout_patches(
        surface,
        depth,
        camera,
        profile_sample.oxide_rate,
        profile_sample.oxide_radius_mm,
        profile_sample.oxide_dropout,
        rng,
    )
    if oxide_patch.any():
        inside = ndimage.distance_transform_edt(oxide_patch)
        observed[oxide_patch] += (
            profile_sample.oxide_bias_mm
            / 1000.0
            * np.clip(inside[oxide_patch] / 2.0, 0.0, 1.0)
        )

    gradient = np.hypot(*np.gradient(filled))
    edge = gradient * 1000 > profile_sample.edge_gradient_mm
    if profile_sample.edge_band_px > 1:
        edge = ndimage.binary_dilation(
            edge, iterations=profile_sample.edge_band_px
        )
    edge &= surface
    window = 2 * profile_sample.edge_band_px + 1
    local_max = ndimage.maximum_filter(filled, size=window)
    local_min = ndimage.minimum_filter(filled, size=window)
    flying = edge & (
        rng.random(depth.shape) < profile_sample.edge_flying_fraction
    )
    observed[flying] = local_min[flying] + rng.random(int(flying.sum())) * (
        local_max[flying] - local_min[flying]
    )

    pattern = gaussian_filter(rng.normal(0.0, 1.0, depth.shape), 8.0)
    pattern = pattern / max(np.abs(pattern[surface]).max(), 1e-9)
    offset_mm = (
        profile_sample.normal_bias_mm
        + profile_sample.normal_bias_pattern_mm * pattern
    )
    observed += offset_mm / 1000 * normals[:, :, 2]

    dropout_probability = np.clip(
        profile_sample.base_dropout
        + profile_sample.incidence_dropout * (1.0 / cosine - 1.0),
        0.0,
        profile_sample.max_dropout,
    )
    dropout = surface & (rng.random(depth.shape) < dropout_probability)
    dropout |= specular_dropped | oxide_dropped
    observed[dropout] = np.nan

    rotation = Rotation.from_rotvec(
        np.asarray(profile_sample.registration_rotation_mrad) * 1e-3
    ).as_matrix()
    translation = np.asarray(profile_sample.registration_translation_mm) / 1000
    valid_points = np.isfinite(observed)
    points = camera.unproject(np.where(valid_points, observed, np.nan))
    drift_points = points[valid_points] @ rotation.T + translation
    drifted = _reproject_depth(drift_points, camera, depth.shape)
    observed = drifted
    surface = np.isfinite(observed)
    return {
        "depth_m": observed,
        "depth_ideal_m": depth,
        "sigma_mm": np.where(surface, sigma, np.nan),
        "incidence_deg": np.where(surface, incidence, np.nan),
        "surface": surface,
        "dropout": dropout,
        "specular": specular_patch & observed_surface,
        "oxide": oxide_patch & observed_surface,
        "flying": flying & observed_surface,
        "points_m": camera.unproject(np.where(surface, observed, np.nan)),
        "normals": normals,
        "drift_translation_mm": np.asarray(
            profile_sample.registration_translation_mm
        ).tolist(),
        "drift_rotation_mrad": np.asarray(
            profile_sample.registration_rotation_mrad
        ).tolist(),
    }


def render_observation(scene, camera, profile_sample, rng):
    """Rasterize a scene and degrade the ideal depth in one call."""
    raster = rasterize_mesh(scene, camera)
    degraded = degrade_depth(raster["depth_m"], camera, profile_sample, rng)
    degraded["face_index"] = np.where(
        degraded["surface"], raster["face_index"], -1
    )
    return degraded


def _synthetic_rgb(observation, semantic=None):
    """Return geometry-only shaded RGB; labels never affect appearance.

    ``semantic`` is accepted for call-site compatibility and deliberately
    ignored. The image uses one cast-metal base colour, Lambertian shading from
    the observed normals, the broad specular lobe, and the measured specular /
    oxide / dropout masks. Changing supervision labels therefore cannot change
    a single RGB pixel.
    """
    normals = observation["normals"]
    surface = observation["surface"]
    light = unit(np.array([-0.4, -0.7, -0.55]))
    diffuse = np.clip(np.einsum("ijk,k->ij", normals, light), 0.0, 1.0)
    image = np.empty(normals.shape[:2] + (3,), dtype=float)
    image[:] = np.array([126.0, 128.0, 130.0])
    image *= (0.32 + 0.68 * diffuse)[..., None]
    highlight = np.clip(diffuse, 0.0, 1.0) ** 24
    image += 55.0 * highlight[..., None]
    specular = observation.get("specular")
    if specular is not None:
        image[specular] = np.minimum(image[specular] + 70.0, 255.0)
    oxide = observation.get("oxide")
    if oxide is not None:
        image[oxide] *= 0.55
        image[oxide] += np.array([18.0, 10.0, 4.0])
    dropout = observation.get("dropout")
    if dropout is not None:
        image[dropout] *= 0.6
    image[~surface] = 20.0
    return np.clip(image, 0, 255).astype(np.uint8)


@dataclass(frozen=True)
class SceneAssembly:
    """Base CAD plus rooted defects with per-face supervision."""

    mesh: trimesh.Trimesh
    vertex_residual_mm: np.ndarray
    face_semantic: np.ndarray
    face_instance: np.ndarray
    base_face_count: int
    defects: tuple

    def face_residual_mm(self):
        """Return the mean signed residual of each face's three vertices."""
        return self.vertex_residual_mm[self.mesh.faces].mean(axis=1)


def assemble_scene(base_mesh, defects):
    """Concatenate the base CAD and defects without welding face identity."""
    vertices = [np.asarray(base_mesh.vertices, dtype=float)]
    residual = [np.zeros(len(base_mesh.vertices))]
    faces = [np.asarray(base_mesh.faces, dtype=int)]
    semantic = [
        np.full(
            len(base_mesh.faces), SEMANTIC_LABELS["nominal_cad"], dtype=np.uint8
        )
    ]
    instance = [np.zeros(len(base_mesh.faces), dtype=int)]
    offset = len(base_mesh.vertices)
    for identity, defect in enumerate(defects, 1):
        vertices.append(np.asarray(defect.mesh.vertices, dtype=float))
        residual.append(np.asarray(defect.vertex_residual_mm, dtype=float))
        faces.append(np.asarray(defect.mesh.faces, dtype=int) + offset)
        semantic.append(
            np.full(len(defect.mesh.faces), DEFECT_SEMANTICS[defect.kind])
        )
        instance.append(np.full(len(defect.mesh.faces), identity))
        offset += len(defect.mesh.vertices)
    mesh = trimesh.Trimesh(np.vstack(vertices), np.vstack(faces), process=False)
    return SceneAssembly(
        mesh=mesh,
        vertex_residual_mm=np.concatenate(residual),
        face_semantic=np.concatenate(semantic),
        face_instance=np.concatenate(instance),
        base_face_count=len(base_mesh.faces),
        defects=tuple(defects),
    )


def _root_arrays(defects, identities=None):
    """Concatenate roots, tangents and normals with explicit instance ids."""
    defect_list = list(defects)
    if identities is None:
        identity_list = list(range(1, len(defect_list) + 1))
    else:
        identity_list = [int(value) for value in identities]
        if len(identity_list) != len(defect_list):
            raise ValueError("Root identities must match the defect count")
    points, tangents, normals, offsets, instances = [], [], [], [0], []
    for identity, defect in zip(identity_list, defect_list):
        for root, tangent, normal in zip(
            defect.roots_m, defect.root_tangents, defect.root_normals
        ):
            points.append(root)
            tangents.append(tangent)
            normals.append(normal)
            instances.extend([identity] * len(root))
            offsets.append(offsets[-1] + len(root))
    if not points:
        return (
            np.empty((0, 3)),
            np.empty((0, 3)),
            np.empty((0, 3)),
            np.asarray(offsets, dtype=np.int32),
            np.empty(0, dtype=int),
        )
    return (
        np.vstack(points),
        np.vstack(tangents),
        np.vstack(normals),
        np.asarray(offsets, dtype=np.int32),
        np.asarray(instances, dtype=int),
    )


def sample_dense_cloud(
    scene, camera, center_m, radius_m, spacing_m, profile_sample, rng
):
    """Sample a high-density local cloud with pointwise sensor degradation.

    The local spacing stays below one millimetre on purpose: the bundle must
    not pre-subsample peaks into a coarse voxel grid before training.
    """
    face_centers = scene.mesh.triangles_center
    local = np.asarray(
        cKDTree(face_centers).query_ball_point(center_m, radius_m)
    )
    if len(local) < 4:
        local = np.arange(len(scene.mesh.faces))
    area_m2 = float(scene.mesh.area_faces[local].sum())
    count = int(min(40000, max(400, round(area_m2 / spacing_m**2))))
    weights = np.zeros(len(scene.mesh.faces))
    weights[local] = scene.mesh.area_faces[local]
    points, face_index, barycentric = trimesh.sample.sample_surface(
        scene.mesh,
        count,
        face_weight=weights,
        seed=int(rng.integers(2**31)),
        return_barycentric=True,
    )
    triangle_residual = scene.vertex_residual_mm[scene.mesh.faces[face_index]]
    residual = np.einsum("ij,ij->i", barycentric, triangle_residual)
    normals = scene.mesh.face_normals[face_index]
    direction = unit(points - camera.position_m)
    incidence = np.degrees(
        np.arccos(
            np.clip(
                np.abs(np.einsum("ij,ij->i", normals, direction)), 0.05, 1.0
            )
        )
    )
    range_m = np.linalg.norm(points - camera.position_m, axis=1)
    sigma = (
        profile_sample.base_sigma_mm
        + profile_sample.range_sigma_mm_per_m * range_m
        + profile_sample.incidence_gain_mm
        * (1.0 / np.clip(np.cos(np.radians(incidence)), 0.05, 1.0) - 1.0)
        ** profile_sample.incidence_power
    )
    points = (
        points
        + direction
        * (rng.normal(0.0, 1.0, len(points)) * sigma / 1000)[:, None]
    )
    dropout_probability = np.clip(
        profile_sample.base_dropout
        + profile_sample.incidence_dropout
        * (1.0 / np.clip(np.cos(np.radians(incidence)), 0.05, 1.0) - 1.0),
        0.0,
        profile_sample.max_dropout,
    )
    keep = rng.random(len(points)) >= dropout_probability
    points, face_index = points[keep], face_index[keep]
    residual, normals = residual[keep], normals[keep]
    incidence, sigma = incidence[keep], sigma[keep]
    root_points, root_tangents, _, _, _ = _root_arrays(scene.defects)
    if len(root_points):
        distance, nearest = cKDTree(root_points).query(points)
        near_tangent = root_tangents[nearest]
    else:
        distance = np.full(len(points), np.inf)
        near_tangent = np.zeros_like(points)
    protection = (np.abs(residual) <= 0.05) & (distance <= 0.02)
    return {
        "points_m": points,
        "normals": normals,
        "semantic": scene.face_semantic[face_index],
        "instance": scene.face_instance[face_index],
        "residual_mm": residual,
        "root_distance_mm": distance * 1000,
        "root_tangent": near_tangent,
        "incidence_deg": incidence,
        "sigma_mm": sigma,
        "protection": protection,
        "spacing_mm": float(spacing_m * 1000),
        "local_area_mm2": area_m2 * 1e6,
        "count": int(len(points)),
    }


def _orbit(vector, angle_deg, draft):
    """Rotate a camera offset around the draft axis like a turntable."""
    return Rotation.from_rotvec(
        np.asarray(draft) / np.linalg.norm(draft) * math.radians(angle_deg)
    ).apply(vector)


def _feature_frame(defect, draft):
    center = np.vstack(defect.roots_m).mean(axis=0)
    normal = unit(np.vstack(defect.root_normals).mean(axis=0))
    tangent = unit(np.vstack(defect.root_tangents).mean(axis=0))
    tangent = unit(tangent - normal * (tangent @ normal))
    lateral = unit(np.cross(normal, tangent))
    if abs(float(lateral @ _orbit(lateral, 0.0, draft))) < 1e-9:
        lateral = unit(np.cross(normal, [0.0, 1.0, 0.0]))
    return center, normal, tangent, lateral


def _defect_extent_m(defect):
    """Bounding extent of the valid chart support, in metres."""
    valid = np.asarray(defect.chart.valid, dtype=bool).reshape(-1)
    points = defect.chart.base_m.reshape(-1, 3)[valid]
    if len(points) < 4:
        points = np.vstack(defect.roots_m)
    return max(
        float(np.linalg.norm(points.max(axis=0) - points.min(axis=0))),
        0.008,
    )


def _sample_cameras(defect, draft, encoder_angle_deg, rng, image_size):
    width, height = image_size
    center, normal, tangent, lateral = _feature_frame(defect, draft)
    lateral = unit(_orbit(lateral, encoder_angle_deg, draft))
    tangent = unit(_orbit(tangent, encoder_angle_deg, draft))
    overview_offset = unit(0.80 * normal + 0.42 * lateral + 0.16 * tangent)
    overview = look_at(
        center + 0.50 * overview_offset, center, width, height, 40.0
    )
    alignment_mm = rng.uniform(0.0, 1.2)
    alignment_direction = unit(
        rng.normal(size=3) - normal * (np.dot(rng.normal(size=3), normal))
    )
    target = center + alignment_direction * alignment_mm / 1000
    yaw_mrad = rng.uniform(-0.8, 0.8)
    local_offset = unit(0.62 * normal + 0.72 * lateral + 0.20 * tangent)
    extent = _defect_extent_m(defect)
    local_distance = float(
        np.clip(0.62 * extent / math.tan(math.radians(15.0)), 0.05, 0.30)
    )
    local_position = target + local_distance * local_offset
    local_position = center + Rotation.from_rotvec(
        np.asarray(draft) / np.linalg.norm(draft) * yaw_mrad / 1000
    ).apply(local_position - center)
    local = look_at(local_position, target, width, height, 30.0)
    return {
        "overview": (overview, 0.0, {"alignment_mm": 0.0, "yaw_mrad": 0.0}),
        "local_fov": (
            local,
            alignment_mm,
            {"alignment_mm": alignment_mm, "yaw_mrad": yaw_mrad},
        ),
    }


def _protection_mask(semantic, surface, radius_px, defect_labels=None):
    """Mark untouched nominal surface next to the machined band."""
    labels = (
        tuple(DEFECT_SEMANTICS.values())
        if defect_labels is None
        else tuple(defect_labels)
    )
    defect_pixels = np.isin(semantic, labels)
    if not defect_pixels.any():
        return np.zeros_like(defect_pixels)
    dilated = ndimage.binary_dilation(defect_pixels, iterations=radius_px)
    return (dilated & ~defect_pixels & surface).astype(bool)


def _render_sample(
    scene,
    camera,
    profile,
    profile_sample,
    rng,
    *,
    sample_kind,
    encoder_angle_deg,
    weak_alignment,
    defect_identity,
    defect_center_m,
    supervision,
    defect_records,
    cloud_spacing_m,
    nominal_mesh=None,
):
    """Render one scene sample and return its arrays and metadata."""
    degraded = render_observation(scene.mesh, camera, profile_sample, rng)
    nominal_depth = (
        rasterize_mesh(nominal_mesh, camera)["depth_m"]
        if nominal_mesh is not None
        else np.full_like(degraded["depth_ideal_m"], np.nan)
    )
    face_index = np.clip(degraded["face_index"], 0, None)
    semantic = np.where(
        degraded["surface"] & (degraded["face_index"] >= 0),
        scene.face_semantic[face_index],
        0,
    ).astype(np.uint8)
    instance = np.where(
        degraded["surface"] & (degraded["face_index"] >= 0),
        scene.face_instance[face_index],
        0,
    ).astype(np.uint16)
    residual = np.where(
        degraded["surface"],
        scene.face_residual_mm()[face_index],
        np.nan,
    )
    mean_depth = float(np.nanmean(degraded["depth_m"]))
    radius_px = max(
        2,
        int(round(0.004 / max(float(camera.pixel_scale_m(mean_depth)), 1e-6))),
    )
    protection = _protection_mask(semantic, degraded["surface"], radius_px)
    rgb = _synthetic_rgb(degraded, semantic)
    cloud = sample_dense_cloud(
        scene,
        camera,
        defect_center_m,
        0.045,
        cloud_spacing_m,
        profile_sample,
        rng,
    )
    arrays = {
        "depth_m": degraded["depth_m"].astype(np.float32),
        "depth_ideal_m": degraded["depth_ideal_m"].astype(np.float32),
        "depth_nominal_m": nominal_depth.astype(np.float32),
        "depth_sigma_mm": degraded["sigma_mm"].astype(np.float32),
        "incidence_deg": degraded["incidence_deg"].astype(np.float32),
        "semantic": semantic,
        "instance": instance,
        "residual_mm": residual.astype(np.float32),
        "protection": protection,
        "surface": degraded["surface"],
        "dropout": degraded["dropout"],
        "specular": degraded["specular"],
        "oxide": degraded["oxide"],
        "flying": degraded["flying"],
    }
    metadata = {
        "sample_kind": sample_kind,
        "instance": int(defect_identity),
        "encoder_angle_deg": float(encoder_angle_deg),
        "weak_alignment": weak_alignment,
        "camera": camera.record(),
        "sensor_sample": profile_sample.record(),
        "sensor_profile": profile.record(),
        "defect": defect_records[defect_identity - 1],
        "supervision": supervision,
        "point_count": cloud["count"],
        "mean_sigma_mm": float(
            np.nanmean(degraded["sigma_mm"][degraded["surface"]])
        ),
        "depth_valid_fraction": float(degraded["surface"].mean()),
        "incidence_p95_deg": float(
            np.nanpercentile(degraded["incidence_deg"], 95)
        ),
        "dropout_fraction": float(
            degraded["dropout"].sum() / max(degraded["surface"].sum(), 1)
        ),
        "specular_pixels": int(degraded["specular"].sum()),
        "oxide_pixels": int(degraded["oxide"].sum()),
        "flying_pixels": int(degraded["flying"].sum()),
        "peak_residual_mm": float(np.nanmax(residual))
        if np.isfinite(residual).any()
        else 0.0,
        "visible_instances": sorted(
            int(value) for value in np.unique(instance) if value
        ),
        "rgb_source": "geometry-only Lambertian cast-metal shading",
    }
    return arrays, rgb, cloud, metadata


def _write_sample(folder, arrays, rgb, cloud, roots, metadata):
    """Write one self-describing multimodal sample bundle."""
    folder.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(folder / "arrays.npz", **arrays)
    Image.fromarray(rgb).save(folder / "rgb.png")
    Image.fromarray(arrays["semantic"]).save(folder / "semantic.png")
    Image.fromarray(arrays["protection"]).save(folder / "protection.png")
    np.savez_compressed(folder / "points.npz", **cloud)
    np.savez_compressed(folder / "roots.npz", **roots)
    write_json(folder / "meta.json", {**metadata, "schema": BUNDLE_SCHEMA})
    return folder


def _root_bundle(defects, identities=None):
    points, tangents, normals, offsets, instances = _root_arrays(
        defects, identities
    )
    boundaries = [
        boundary for defect in defects for boundary in defect.boundaries_m
    ]
    boundary_points = np.vstack(boundaries) if boundaries else np.empty((0, 3))
    return {
        "root_points_m": points,
        "root_tangent": tangents,
        "root_normal": normals,
        "root_offsets": offsets,
        "root_instance": instances,
        "boundary_points_m": boundary_points,
        "frame": "workpiece",
    }


def protection_for_cloud(
    points, residual_mm, band_threshold_mm=0.03, reach_mm=3.0
):
    """Mark nominal chart nodes within reach of the machined residual band."""
    residual = np.asarray(residual_mm, dtype=float)
    nominal = np.abs(residual) <= 0.05
    band = np.abs(residual) > band_threshold_mm
    if not band.any() or not nominal.any():
        return np.zeros(len(residual), dtype=bool)
    distance, _ = cKDTree(np.asarray(points, dtype=float)[band]).query(
        np.asarray(points, dtype=float)[nominal]
    )
    selected = np.zeros(len(residual), dtype=bool)
    selected[np.flatnonzero(nominal)] = distance * 1000 <= reach_mm
    return selected


def _intermediate_bundle(
    feature_set,
    defect,
    identity,
    state,
    split,
    seed,
    profile,
    profile_sample,
    image_size,
    rng,
):
    """Build one local residual-chart sample for a machining intermediate."""
    heights = intermediate_height(defect.chart, state, rng)
    surface_mesh = defect.chart.surface_mesh(heights)
    nominal_mesh = defect.chart.surface_mesh(np.zeros_like(heights))
    center = defect.chart.base_m.reshape(-1, 3).mean(axis=0)
    normal = unit(defect.chart.normal_m.mean(axis=(0, 1)))
    tangent = unit(defect.chart.tangent)
    lateral = unit(np.cross(normal, tangent))
    valid_nodes = np.asarray(defect.chart.valid, dtype=bool).reshape(-1)
    support = defect.chart.base_m.reshape(-1, 3)[valid_nodes]
    if len(support) < 4:
        support = defect.chart.base_m.reshape(-1, 3)
    extent = max(
        float(np.linalg.norm(support.max(axis=0) - support.min(axis=0))),
        0.008,
    )
    distance = float(
        np.clip(0.62 * extent / math.tan(math.radians(12.0)), 0.04, 0.26)
    )
    position = center + distance * unit(
        0.8 * normal + 0.5 * lateral + 0.2 * tangent
    )
    camera = look_at(position, center, image_size[0], image_size[1], 24.0)
    raster = rasterize_mesh(surface_mesh, camera)
    degraded = degrade_depth(raster["depth_m"], camera, profile_sample, rng)
    nominal = rasterize_mesh(nominal_mesh, camera)["depth_m"]
    # surface_mesh.faces index the flattened vertex array, not the (U, V) grid.
    face_heights = heights.reshape(-1)[surface_mesh.faces].mean(axis=1)
    face_index = np.where(degraded["surface"], raster["face_index"], -1)
    valid_face = face_index >= 0
    semantic_values = np.where(
        np.abs(face_heights) > 0.03,
        SEMANTIC_LABELS["intermediate_residual"],
        SEMANTIC_LABELS["nominal_cad"],
    ).astype(np.uint8)
    semantic = np.zeros(camera.height * camera.width, dtype=np.uint8)
    residual_flat = np.full(camera.height * camera.width, np.nan)
    instance_flat = np.zeros(camera.height * camera.width, dtype=np.uint16)
    semantic[valid_face.reshape(-1)] = semantic_values[face_index[valid_face]]
    residual_flat[valid_face.reshape(-1)] = face_heights[face_index[valid_face]]
    instance_flat[valid_face.reshape(-1)] = identity
    semantic = semantic.reshape(camera.height, camera.width)
    residual_image = residual_flat.reshape(camera.height, camera.width)
    instance = instance_flat.reshape(camera.height, camera.width)
    mean_depth = float(np.nanmean(degraded["depth_m"]))
    protection_radius = max(
        4,
        int(round(0.002 / max(float(camera.pixel_scale_m(mean_depth)), 1e-6))),
    )
    protection = _protection_mask(
        semantic,
        degraded["surface"],
        protection_radius,
        defect_labels=(SEMANTIC_LABELS["intermediate_residual"],),
    )
    rgb = _synthetic_rgb(degraded, semantic)
    arrays = {
        "depth_m": degraded["depth_m"].astype(np.float32),
        "depth_ideal_m": raster["depth_m"].astype(np.float32),
        "depth_nominal_m": nominal.astype(np.float32),
        "depth_sigma_mm": degraded["sigma_mm"].astype(np.float32),
        "incidence_deg": degraded["incidence_deg"].astype(np.float32),
        "semantic": semantic,
        "instance": instance.astype(np.uint16),
        "residual_mm": residual_image.astype(np.float32),
        "protection": protection,
        "surface": degraded["surface"],
        "dropout": degraded["dropout"],
        "specular": degraded["specular"],
        "oxide": degraded["oxide"],
        "flying": degraded["flying"],
    }
    valid_nodes = np.asarray(defect.chart.valid, dtype=bool).reshape(-1)
    points = defect.chart.surface(heights).reshape(-1, 3)[valid_nodes]
    normals = defect.chart.normal_m.reshape(-1, 3)[valid_nodes]
    residual = heights.reshape(-1)[valid_nodes]
    root_points, root_tangents, _, _, _ = _root_arrays(
        [defect], identities=[identity]
    )
    distance, nearest = cKDTree(root_points).query(points)
    view = unit(points - camera.position_m)
    incidence = np.degrees(
        np.arccos(
            np.clip(np.abs(np.einsum("ij,ij->i", normals, view)), 0.05, 1.0)
        )
    )
    ranges = np.linalg.norm(points - camera.position_m, axis=1)
    sigma = (
        profile_sample.base_sigma_mm
        + profile_sample.range_sigma_mm_per_m * ranges
        + profile_sample.incidence_gain_mm
        * (1.0 / np.clip(np.cos(np.radians(incidence)), 0.05, 1.0) - 1.0)
        ** profile_sample.incidence_power
    )
    cloud = {
        "points_m": points,
        "normals": normals,
        "semantic": np.where(
            np.abs(residual) > 0.03,
            SEMANTIC_LABELS["intermediate_residual"],
            SEMANTIC_LABELS["nominal_cad"],
        ).astype(np.uint8),
        "instance": np.full(len(points), identity, dtype=int),
        "residual_mm": residual,
        "root_distance_mm": distance * 1000,
        "root_tangent": root_tangents[nearest],
        "incidence_deg": incidence,
        "sigma_mm": sigma,
        "protection": protection_for_cloud(points, residual),
        "spacing_mm": float(
            np.median(np.abs(np.diff(defect.chart.u_m))) * 1000
        ),
        "local_area_mm2": float(np.sum(surface_mesh.area_faces) * 1e6),
        "count": int(len(points)),
    }
    metadata = {
        "sample_kind": "intermediate_state",
        "intermediate_state": state,
        "workpiece_id": feature_set.workpiece_id,
        "split": split,
        "seed": seed,
        "defect": defect.record(),
        "camera": camera.record(),
        "sensor_sample": profile_sample.record(),
        "sensor_profile": profile.record(),
        "encoder_angle_deg": 0.0,
        "weak_alignment": {"alignment_mm": 0.0, "yaw_mrad": 0.0},
        "supervision": {
            "residual_mm": "signed height above the local nominal CAD chart",
            "semantic": "6 where |residual| exceeds 0.03 mm, else 3",
            "overgrind_pixels": int((residual_image < -0.03).sum()),
        },
        "point_count": int(len(points)),
        "mean_sigma_mm": float(np.nanmean(degraded["sigma_mm"])),
        "depth_valid_fraction": float(degraded["surface"].mean()),
        "incidence_p95_deg": float(
            np.nanpercentile(degraded["incidence_deg"], 95)
        ),
        "dropout_fraction": float(
            degraded["dropout"].sum() / max(degraded["surface"].sum(), 1)
        ),
        "specular_pixels": int(degraded["specular"].sum()),
        "oxide_pixels": int(degraded["oxide"].sum()),
        "flying_pixels": int(degraded["flying"].sum()),
        "peak_residual_mm": float(np.max(heights)),
        "minimum_residual_mm": float(np.min(heights)),
        "scope": (
            "local chart observation; not a re-meshed solid and not a "
            "calibrated material-removal result"
        ),
        "rgb_source": "deterministic Lambertian shading from synthetic normals",
    }
    return arrays, rgb, cloud, metadata


@dataclass(frozen=True)
class WorkpiecePackageView:
    """Minimal P0-B package contract consumed by this pipeline."""

    workpiece_id: str
    package_id: str
    mesh_path: Path
    units: str
    cad_sha256: str | None
    protection_regions: tuple
    metadata: dict


def _json_record(value):
    """Convert package objects and numpy values into JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(key): _json_record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_record(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def _zone_records(zones):
    """Normalize P0-B ``ProtectedZone`` objects or mapping zone records."""
    if not isinstance(zones, (list, tuple)):
        raise ValueError("protected zones must be a list or tuple")
    records = []
    for zone in zones:
        if isinstance(zone, dict):
            records.append(_json_record(zone))
            continue
        record = {"name": str(getattr(zone, "name", "zone"))}
        bounds = getattr(zone, "bounds_m", None)
        if bounds is not None:
            record["bounds_m"] = np.asarray(bounds, dtype=float).tolist()
        records.append(record)
    return tuple(records)


def _resolve_mesh_path(value, bases, key):
    """Resolve a declared CAD path against the package location then the repo."""
    path = Path(str(value))
    candidates = (
        [path] if path.is_absolute() else [base / path for base in bases]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(
        f"WorkpiecePackage {key} does not exist; tried "
        f"{', '.join(str(candidate) for candidate in candidates)}"
    )


def _view_from_object(source):
    """Adapt a P0-B ``WorkpiecePackage`` dataclass without a hard import."""
    workpiece_id = str(getattr(source, "workpiece_id", ""))
    if not workpiece_id:
        raise ValueError("WorkpiecePackage requires a workpiece_id")
    mesh_path = Path(getattr(source, "mesh_path"))
    if not mesh_path.is_file():
        raise ValueError(f"WorkpiecePackage mesh does not exist: {mesh_path}")
    declaration = getattr(source, "declaration", {}) or {}
    units = declaration.get("units", "m")
    if units not in {"m", "mm"}:
        raise ValueError("WorkpiecePackage units must be explicitly m or mm")
    bounds = getattr(source, "bounds_m", None)
    return WorkpiecePackageView(
        workpiece_id=workpiece_id,
        package_id=str(getattr(source, "package_id", workpiece_id)),
        mesh_path=mesh_path.resolve(),
        units=units,
        cad_sha256=getattr(source, "cad_sha256", None),
        protection_regions=_zone_records(
            getattr(source, "protected_zones", ())
        ),
        metadata={
            "label": declaration.get("label"),
            "coordinate_frame": declaration.get("coordinate_frame"),
            "source_scene": str(getattr(source, "source_scene", "")),
            "fixture_id": declaration.get("fixture_id"),
            "bounds_m": _json_record(bounds),
            "package_path": str(getattr(source, "path", "")),
        },
    )


def load_workpiece_package(source, *, base_directory=None):
    """Normalize a P0-B ``WorkpiecePackage`` object, JSON path or mapping.

    Accepted inputs:
        * A loaded ``workpiece_packages.WorkpiecePackage`` dataclass instance.
        * ``config/workpieces/wp_<id>.json`` or any mapping with
          ``workpiece_id`` and either ``cad_path`` (P0-B) or ``mesh_path``.
        * A mapping with ``units`` (or ``declaration.units``) and
          ``protected_zones`` (P0-B) or ``protection_regions``.

    Relative CAD paths are tried against the declaration's own directory, then
    the repository root. Protection zones may be ``ProtectedZone`` objects,
    dicts with ``name``/``bounds_m``, and either a list or a tuple.
    """
    if not isinstance(source, (str, Path, dict)):
        if not hasattr(source, "workpiece_id") or not hasattr(
            source, "mesh_path"
        ):
            raise ValueError(
                "WorkpiecePackage must be a package object, JSON path or mapping"
            )
        return _view_from_object(source)
    if isinstance(source, (str, Path)):
        path = Path(source)
        value = json.loads(path.read_text(encoding="utf-8"))
        bases = [path.parent, project.ROOT, Path.cwd()]
    else:
        value = source
        bases = (
            [Path(base_directory), Path.cwd(), project.ROOT]
            if base_directory is not None
            else [Path.cwd(), project.ROOT]
        )
    if not isinstance(value, dict):
        raise ValueError("WorkpiecePackage must be a mapping")
    workpiece_id = str(value.get("workpiece_id", ""))
    if not workpiece_id:
        raise ValueError("WorkpiecePackage requires a workpiece_id")
    key = "mesh_path" if value.get("mesh_path") else "cad_path"
    if not value.get(key):
        raise ValueError("WorkpiecePackage requires mesh_path or cad_path")
    mesh_path = _resolve_mesh_path(value[key], bases, key)
    declaration = value.get("declaration", {}) or {}
    units = value.get("units", declaration.get("units", "m"))
    if units not in {"m", "mm"}:
        raise ValueError("WorkpiecePackage units must be explicitly m or mm")
    zones = value.get("protected_zones", value.get("protection_regions", ()))
    return WorkpiecePackageView(
        workpiece_id=workpiece_id,
        package_id=str(value.get("package_id", workpiece_id)),
        mesh_path=mesh_path,
        units=units,
        cad_sha256=value.get("cad_sha256"),
        protection_regions=_zone_records(zones),
        metadata={
            "label": value.get("label"),
            "coordinate_frame": value.get("coordinate_frame"),
            "source_scene": value.get("scene_path"),
            "fixture_id": value.get("fixture_id"),
            "bounds_m": _json_record(value.get("bounds_m")),
            "declared_status": value.get("protected_zone_status"),
        },
    )


DEMO_PROFILE = SensorDegradationProfile(
    specular_patches_per_m2=120.0,
    oxide_patches_per_m2=60.0,
)
ENCODER_ANGLES_DEG = (0.0, 60.0, 120.0, 180.0, 240.0, 300.0)


def _derived_seed(text):
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _visible_cloud_points(points, camera, ideal_depth, tolerance_m=0.0015):
    """Keep only points on the visible side of the rendered surface."""
    pixels, camera_points = camera.project(points)
    columns = np.rint(pixels[:, 0]).astype(int)
    rows = np.rint(pixels[:, 1]).astype(int)
    valid = (
        np.isfinite(pixels).all(axis=1)
        & (columns >= 0)
        & (columns < camera.width)
        & (rows >= 0)
        & (rows < camera.height)
        & (camera_points[:, 2] > 0)
    )
    reference = np.full(len(points), np.inf)
    reference[valid] = ideal_depth[rows[valid], columns[valid]]
    keep = (
        valid
        & np.isfinite(reference)
        & (np.abs(camera_points[:, 2] - reference) <= tolerance_m)
    )
    return keep


def build_workpiece_samples(
    package,
    output,
    *,
    split,
    seed,
    profile=DEMO_PROFILE,
    config=FeatureConfig(),
    cloud_spacing_m=0.00035,
    image_size=(320, 240),
):
    """Extract features, synthesize defects and export every sample bundle.

    Args:
        package: Validated ``WorkpiecePackageView`` for one CAD family.
        output: New run directory that receives ``features``, ``defects``,
            ``samples`` and the manifest.
        split: Unseen-CAD split label for every sample of this workpiece.
        seed: Nonnegative development seed; all derived RNGs are deterministic.
        profile: Sensor degradation profile.
        config: Feature extraction thresholds.
        cloud_spacing_m: Target local point spacing; must stay below 1 mm.
        image_size: (width, height) of every rendered observation.

    Returns:
        Mapping with feature summary, defect records and sample records.
    """
    if package.units != "m":
        raise ValueError("The pipeline expects a metre-valued CAD mesh")
    if not 0.0 < cloud_spacing_m <= 0.001:
        raise ValueError("cloud_spacing_m must be at most 1 mm")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    mesh = load_mesh(package.mesh_path)
    observed_id = workpiece_id_from_path(package.mesh_path)
    if str(package.workpiece_id) != observed_id:
        raise ValueError(
            f"Package id {package.workpiece_id} does not match mesh {observed_id}"
        )
    features = extract_features(
        mesh, package.workpiece_id, package.mesh_path, config
    )
    defects = generate_defects(mesh, features, seed)
    scene = assemble_scene(mesh, defects)
    defect_records = [defect.record() for defect in defects]
    samples = []
    for identity, defect in enumerate(defects, 1):
        center = np.vstack(defect.roots_m).mean(axis=0)
        for kind_index, sample_kind in enumerate(SAMPLE_KINDS):
            sample_seed = _derived_seed(
                f"{package.workpiece_id}:{seed}:{sample_kind}:{identity}"
            )
            rng = np.random.default_rng(sample_seed)
            profile_sample = profile.draw(rng)
            encoder_angle = ENCODER_ANGLES_DEG[
                (identity * 2 + kind_index) % len(ENCODER_ANGLES_DEG)
            ]
            cameras = _sample_cameras(
                defect, config.draft_direction, encoder_angle, rng, image_size
            )
            camera, alignment, weak = cameras[sample_kind]
            arrays, rgb, cloud, metadata = _render_sample(
                scene,
                camera,
                profile,
                profile_sample,
                rng,
                sample_kind=sample_kind,
                encoder_angle_deg=encoder_angle,
                weak_alignment=weak,
                defect_identity=identity,
                defect_center_m=center,
                supervision={
                    "semantic": SEMANTIC_LABELS,
                    "instance": f"{identity} labels the current defect",
                    "residual_mm": "face-mean signed height above nominal CAD",
                    "protection": "untouched nominal pixels next to the defect",
                    "root": "3D root polyline, tangent and normal per instance",
                },
                defect_records=defect_records,
                cloud_spacing_m=cloud_spacing_m,
                nominal_mesh=mesh,
            )
            visible_instances = metadata.get("visible_instances") or [identity]
            roots = _root_bundle(
                [defects[value - 1] for value in visible_instances],
                identities=visible_instances,
            )
            visible = _visible_cloud_points(
                cloud["points_m"], camera, arrays["depth_ideal_m"]
            )
            cloud = {
                key: value[visible] if key in CLOUD_POINT_FIELDS else value
                for key, value in cloud.items()
            }
            cloud["count"] = int(len(cloud["points_m"]))
            sample_id = (
                f"{package.workpiece_id}_{sample_kind}_d{identity:02d}"
                f"_seed{seed}"
            )
            folder = output / "samples" / sample_id
            metadata = {
                **metadata,
                "sample_id": sample_id,
                "workpiece_id": package.workpiece_id,
                "package_id": package.package_id,
                "split": split,
                "seed": seed,
                "cad_sha256": features.cad_sha256,
                "provenance": _provenance(package, features, seed, sample_kind),
            }
            _write_sample(folder, arrays, rgb, cloud, roots, metadata)
            samples.append(
                {
                    "sample_id": sample_id,
                    "directory": str(folder.relative_to(output)),
                    "workpiece_id": package.workpiece_id,
                    "split": split,
                    "sample_kind": sample_kind,
                    "instance": identity,
                    "defect_kind": defect.kind,
                    "defect_style": defect.style,
                    "intermediate_state": None,
                    "point_count": cloud["count"],
                    "mean_sigma_mm": metadata["mean_sigma_mm"],
                    "depth_valid_fraction": metadata["depth_valid_fraction"],
                    "peak_residual_mm": metadata["peak_residual_mm"],
                }
            )
        for state in INTERMEDIATE_STATES:
            sample_seed = _derived_seed(
                f"{package.workpiece_id}:{seed}:{state}:{identity}"
            )
            rng = np.random.default_rng(sample_seed)
            profile_sample = profile.draw(rng)
            arrays, rgb, cloud, metadata = _intermediate_bundle(
                features,
                defect,
                identity,
                state,
                split,
                seed,
                profile,
                profile_sample,
                image_size,
                rng,
            )
            sample_id = (
                f"{package.workpiece_id}_intermediate_d{identity:02d}"
                f"_{state}_seed{seed}"
            )
            folder = output / "samples" / sample_id
            metadata = {
                **metadata,
                "sample_id": sample_id,
                "package_id": package.package_id,
                "cad_sha256": features.cad_sha256,
                "provenance": _provenance(
                    package, features, seed, "intermediate_state"
                ),
            }
            _write_sample(
                folder,
                arrays,
                rgb,
                cloud,
                _root_bundle([defect], identities=[identity]),
                metadata,
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "directory": str(folder.relative_to(output)),
                    "workpiece_id": package.workpiece_id,
                    "split": split,
                    "sample_kind": "intermediate_state",
                    "instance": identity,
                    "defect_kind": defect.kind,
                    "defect_style": defect.style,
                    "intermediate_state": state,
                    "point_count": cloud["count"],
                    "mean_sigma_mm": metadata["mean_sigma_mm"],
                    "depth_valid_fraction": metadata["depth_valid_fraction"],
                    "peak_residual_mm": metadata["peak_residual_mm"],
                }
            )
    write_json(
        output / "features" / f"{package.workpiece_id}.json",
        {
            "summary": features.summary(),
            "lines": [line.record() for line in features.lines],
        },
    )
    write_json(
        output / "defects" / f"{package.workpiece_id}.json",
        {"defects": defect_records},
    )
    return {
        "workpiece_id": package.workpiece_id,
        "package_id": package.package_id,
        "mesh_path": str(package.mesh_path),
        "feature_summary": features.summary(),
        "defects": defect_records,
        "samples": samples,
    }


CLOUD_POINT_FIELDS = (
    "points_m",
    "normals",
    "semantic",
    "instance",
    "residual_mm",
    "root_distance_mm",
    "root_tangent",
    "incidence_deg",
    "sigma_mm",
    "protection",
)


def _provenance(package, features, seed, sample_kind):
    return {
        "generator": "daiyi_grinding.dataset_pipeline",
        "schema": SCHEMA,
        "sample_kind": sample_kind,
        "seed": seed,
        "cad_path": str(package.mesh_path),
        "cad_sha256": features.cad_sha256,
        "package_id": package.package_id,
        "protection_regions": list(package.protection_regions),
        "package_metadata": package.metadata,
        "mujoco_used": False,
        "real_scans_used": False,
        "truth_used_for_control": False,
        "labels_are_supervision_only": True,
        "calibrated_casting_physics": False,
        "calibrated_sensor_model": False,
    }


def run(
    output,
    *,
    workpiece_paths=None,
    packages=None,
    seeds=(20261124,),
    split_seed=0,
    profile=DEMO_PROFILE,
    config=FeatureConfig(),
    cloud_spacing_m=0.00035,
    image_size=(320, 240),
):
    """Run the demonstration pipeline over the supplied CAD families."""
    target = Path(output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    for folder in ("features", "defects", "samples"):
        (target / folder).mkdir()
    views = []
    if packages is not None:
        views.extend(load_workpiece_package(item) for item in packages)
    if workpiece_paths is not None:
        for path in workpiece_paths:
            views.append(
                load_workpiece_package(
                    {
                        "workpiece_id": workpiece_id_from_path(path),
                        "mesh_path": str(Path(path).resolve()),
                        "units": "m",
                    }
                )
            )
    if not views:
        raise ValueError("Supply at least one workpiece mesh or package")
    identifiers = [view.workpiece_id for view in views]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Each workpiece family may appear only once")
    assignment = assign_splits(identifiers, seed=split_seed)
    split_map = assignment.mapping()
    records = []
    for view in views:
        for seed in seeds:
            records.append(
                build_workpiece_samples(
                    view,
                    target,
                    split=split_map[view.workpiece_id],
                    seed=seed,
                    profile=profile,
                    config=config,
                    cloud_spacing_m=cloud_spacing_m,
                    image_size=image_size,
                )
            )
    samples = [sample for record in records for sample in record["samples"]]
    assert_samples_respect_split(samples)
    manifest = {
        "schema": SCHEMA,
        "task": "P1-A multi-master casting samples with real sensor degradation",
        "seeds": list(seeds),
        "split": {
            **assignment.record(),
            "unseen_cad_evaluation_possible": bool(assignment.test),
        },
        "sensor_profile": profile.record(),
        "feature_config": {
            "draft_direction": list(config.draft_direction),
            "heightmap_spacing_mm": config.heightmap_spacing_m * 1000,
            "sharp_angle_deg": config.sharp_angle_deg,
            "gate_zone_length_mm": config.gate_zone_length_m * 1000,
        },
        "cloud_spacing_mm": cloud_spacing_m * 1000,
        "image_size": list(image_size),
        "workpieces": [
            {
                "workpiece_id": record["workpiece_id"],
                "package_id": record["package_id"],
                "mesh_path": record["mesh_path"],
                "features": record["feature_summary"],
                "defects": record["defects"],
            }
            for record in records
        ],
        "samples": samples,
        "scope": {
            "offline_only": True,
            "mujoco_used": False,
            "real_scans_used": False,
            "robot_commands": False,
            "truth_used_for_control": False,
            "labels_are_supervision_only": True,
            "calibrated_casting_physics": False,
            "calibrated_sensor_model": False,
            "point_spacing_max_mm": cloud_spacing_m * 1000,
            "no_coarse_voxel_downsampling": True,
        },
        "degradation_fields": {
            "incidence_noise": "base + range + incidence_gain*(sec(theta)-1)^p",
            "flying_pixels": "edge-band depth mixed between adjacent surfaces",
            "specular_oxide_dropout": "connected random-radius patches",
            "registration_drift": "bounded camera-frame rigid perturbation",
            "normal_bias": "small systematic offset along the surface normal",
        },
    }
    write_json(target / "manifest.json", manifest)
    (target / "report.md").write_text(_report(manifest), encoding="utf-8")
    return manifest


def _report(manifest):
    """Render the Chinese result summary for one pipeline run."""
    rows = []
    for workpiece in manifest["workpieces"]:
        features = workpiece["features"]
        defects = workpiece["defects"]
        rows.append(
            "| {id} | {parting} | {sharp} | {rims} | {zones} | {defects} |".format(
                id=workpiece["workpiece_id"],
                parting=features["features"]["parting_candidate"]["count"],
                sharp=features["features"]["sharp_edge"]["count"],
                rims=features["features"]["hole_rim"]["count"],
                zones=features["features"]["gate_root_zone"]["count"],
                defects=len(defects),
            )
        )
    defect_rows = []
    for workpiece in manifest["workpieces"]:
        for defect in workpiece["defects"]:
            defect_rows.append(
                "| {id} | {kind} | {style} | {peak:.3f} | {volume:.2f} | "
                "{root:.2f} | {watertight} |".format(
                    id=workpiece["workpiece_id"],
                    kind=defect["kind"],
                    style=defect["style"],
                    peak=defect["peak_height_mm"],
                    volume=defect["volume_mm3"],
                    root=defect["geometry_validation"][
                        "root_max_cad_distance_mm"
                    ],
                    watertight="是"
                    if defect["geometry_validation"]["watertight"]
                    else "否",
                )
            )
    split = manifest["split"]
    sample_rows = [
        "| {id} | {kind} | {state} | {points} | {sigma:.3f} | {valid:.3f} |".format(
            id=sample["sample_id"],
            kind=sample["sample_kind"],
            state=sample["intermediate_state"] or "—",
            points=sample["point_count"],
            sigma=sample["mean_sigma_mm"],
            valid=sample["depth_valid_fraction"],
        )
        for sample in manifest["samples"][:24]
    ]
    lines = [
        "# 多母件合成数据管线",
        "",
        "本运行从 097/021 的原始三角网格自动提取分型候选线、锐棱、孔口边缘"
        "与浇口附着区，并在 CAD 上生成有根部的 `flash`、`burr`、`casting_root`。"
        "所有样本使用同一高密多模态格式；真实工件、真实扫描和 MuJoCo 均未参与。",
        "",
        f"unseen-CAD 划分：train={list(split['train'])}，"
        f"val={list(split['val'])}，test={list(split['test'])}。"
        "同一工件的全部视角、风格与中间态均只进入一个划分。",
        "",
        "## 自动特征",
        "",
        "| 工件 | 分型候选 | 锐棱 | 孔口边缘 | 浇口附着区 | 合成缺陷 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *rows,
        "",
        "## 合成缺陷",
        "",
        "| 工件 | 类别 | 形态 | 峰值 mm | 体积 mm³ | 根部最大 CAD 距离 mm | 闭合 |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
        *defect_rows,
        "",
        "## 退化模型",
        "",
        f"- 基础轴向噪声 {manifest['sensor_profile']['base_depth_sigma_mm']:.3f} mm，"
        f"距离项 {manifest['sensor_profile']['range_sigma_mm_per_m']:.3f} mm/m，"
        f"入射角增益 {manifest['sensor_profile']['incidence_sigma_gain_mm']:.3f} mm。",
        f"- 飞点比例 {manifest['sensor_profile']['edge_flying_fraction']:.3f}，"
        f"高反光斑 {manifest['sensor_profile']['specular_patches_per_m2']:.0f} 个/m²，"
        f"氧化皮斑 {manifest['sensor_profile']['oxide_patches_per_m2']:.0f} 个/m²。",
        f"- 配准漂移 ±{manifest['sensor_profile']['registration_translation_mm']:.2f} mm"
        f" / ±{manifest['sensor_profile']['registration_rotation_mrad']:.2f} mrad；"
        f"法向系统偏置 {manifest['sensor_profile']['normal_bias_mm']:.3f} mm。",
        "",
        "## 样本",
        "",
        f"共 {len(manifest['samples'])} 个样本；局部点云目标间距 "
        f"{manifest['cloud_spacing_mm']:.2f} mm，未使用粗体素下采样。"
        "每样本保存 `arrays.npz`（观测/参考深度、σ、入射角、语义、实例、"
        "有符号余量、保护区、丢点/飞点/高反光/氧化皮掩码）、`points.npz`"
        "（高密点云与逐点标签）、`roots.npz`（3D 根部骨架、切向、法向）、"
        "`rgb.png`、`semantic.png`、`protection.png` 与 `meta.json`。",
        "",
        "| 样本 | 类型 | 中间态 | 点数 | 平均 σ mm | 有效深度比例 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
        *sample_rows,
        "",
        "样本列表仅显示前 24 条，完整记录见 `manifest.json`。",
        "",
        "## 复现",
        "",
        "```bash",
        "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
        "OPENBLAS_NUM_THREADS=1 \\",
        "  .venv/bin/python -m daiyi_grinding.dataset_pipeline \\",
        f"  --output {OUTPUT_ROOT.name}/dataset_demo_new \\",
        "  --workpieces 097 021 --seeds 20261124",
        "```",
        "",
        "## 范围与限制",
        "",
        "- 分型线是 CAD 拔模轮廓与二面角突变的候选，不是已确认的模具分型线。",
        "- 浇口附着区是按可达性、平坦度与孔口距离打分的候选窗口，真实浇口"
        "位置需要铸造工艺资料。",
        "- 传感器模型为物理代理；`SensorDegradationProfile."
        "calibrate_from_residuals` 可在取得军山实采残差后反推参数，"
        "当前参数不是标定值。",
        "- 语义、实例、余量与根部是监督标签，不参与任何控制决定。",
        "",
    ]
    return "\n".join(lines)


INTERVENTION_PARAMETER_KEYS = (
    "feed_scale",
    "normal_offset_mm",
    "passes",
    "dwell_scale",
)


def interventions_to_hard_examples(interventions):
    """Map P0-A intervention rows to synthetic hard-sample requests.

    ``interventions.jsonl`` entries from ``WorkstationSession._log`` carry
    ``before``/``after`` segment parameters and their ``diff``. A reduced feed
    scale or added passes indicates a hard region; a normal-offset change
    indicates a residual/allowance mismatch. The returned requests are
    arguments for ``synthesize_defect`` and ``intermediate_height`` with a new
    seed, never control commands.
    """
    if not isinstance(interventions, (list, tuple)):
        raise ValueError("interventions must be a list of log records")
    action_kinds = {
        "mask_false_positive": ("false_positive_review", "medium"),
        "skip_segment": ("skipped_segment_review", "medium"),
        "add_manual_region": ("manual_roi", "medium"),
        "split_segment": ("split_segment_review", "low"),
        "switch_grasp": ("grasp_switch_review", "low"),
    }
    requests = []
    for entry in interventions:
        if not isinstance(entry, dict):
            continue
        diff = entry.get("diff")
        if not isinstance(diff, dict):
            continue
        changed = {
            key: value
            for key, value in diff.items()
            if key in INTERVENTION_PARAMETER_KEYS and isinstance(value, dict)
        }
        if not changed:
            action = entry.get("action")
            if action not in action_kinds:
                continue
            requested, severity = action_kinds[action]
            state = None
        else:
            requested = "nominal_replay"
            state = None
            severity = "nominal"
            if "feed_scale" in changed:
                after = changed["feed_scale"].get("after")
                if isinstance(after, (int, float)) and after < 1.0:
                    requested, state, severity = (
                        "over_removal_risk",
                        "thin_stock",
                        "high",
                    )
                elif isinstance(after, (int, float)) and after > 1.0:
                    requested, state, severity = (
                        "under_removal_risk",
                        "rough_residual",
                        "high",
                    )
            if "passes" in changed:
                after = changed["passes"].get("after")
                if isinstance(after, (int, float)) and after > 1:
                    requested, state, severity = (
                        "thick_stock",
                        "rough_residual",
                        "high",
                    )
            if "normal_offset_mm" in changed and requested == "nominal_replay":
                requested, state, severity = (
                    "allowance_offset",
                    "thin_stock",
                    "medium",
                )
        requests.append(
            {
                "source": {
                    "timestamp": entry.get("timestamp"),
                    "stage": entry.get("stage"),
                    "action": entry.get("action"),
                    "target_id": entry.get("target_id"),
                    "reason": entry.get("reason", ""),
                },
                "changed": changed,
                "requested_kind": requested,
                "requested_intermediate_state": state,
                "requested_defect_kind": "flash",
                "severity": severity,
                "truth_used_for_control": False,
            }
        )
    return {
        "schema": "daiyi-intervention-hard-examples-v1",
        "requests": requests,
        "usage": (
            "pass requested_defect_kind and an unused seed to synthesize_defect; "
            "pass requested_intermediate_state to intermediate_height"
        ),
        "truth_used_for_control": False,
    }


def _output_path(value):
    """Require a fresh directory under the P1 experiment root."""
    root = OUTPUT_ROOT.resolve()
    requested = Path(value)
    if not requested.is_absolute():
        candidate = (root / requested).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"--output must stay under {root}")
        return candidate
    requested = requested.resolve()
    if not requested.is_relative_to(root):
        raise ValueError(f"--output must stay under {root}")
    return requested


def main(argv=None):
    """Run the multi-master sample demonstration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="new directory under runs/enterprise_offline_20261123/"
        "p1_data_process_01",
    )
    parser.add_argument(
        "--workpieces",
        nargs="+",
        default=list(WORKPIECE_MESHES),
        choices=sorted(WORKPIECE_MESHES),
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=[20261124])
    parser.add_argument("--split-seed", type=int, default=0)
    parser.add_argument("--cloud-spacing-mm", type=float, default=0.35)
    parser.add_argument("--image-width", type=int, default=320)
    parser.add_argument("--image-height", type=int, default=240)
    args = parser.parse_args(argv)
    output = _output_path(args.output)
    manifest = run(
        output,
        workpiece_paths=[WORKPIECE_MESHES[name] for name in args.workpieces],
        seeds=tuple(args.seeds),
        split_seed=args.split_seed,
        cloud_spacing_m=args.cloud_spacing_mm / 1000,
        image_size=(args.image_width, args.image_height),
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "workpieces": [
                    workpiece["workpiece_id"]
                    for workpiece in manifest["workpieces"]
                ],
                "samples": len(manifest["samples"]),
                "split": manifest["split"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
