"""Research-informed rooted casting excess on the supplied, unchanged CAD.

These meshes reproduce specified morphology, not a calibrated casting process.
Candidate parting curves come from supported CAD geometry, not mold drawings.
Root and stock metadata are private supervision and never perception inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import trimesh
from scipy.ndimage import gaussian_filter1d
from scipy.spatial import cKDTree

from . import project
from .cad_defects import supported_heights, top_support
from .casting_profiles import (
    BASE_PROFILE,
    load_profile,
    profile_provenance,
    sample_compact_prior,
)

CONFIG_FILE = project.ROOT / "config/casting_synthesis.json"
CONFIG = json.loads(CONFIG_FILE.read_text())
CASTING_CASES = (
    "casting_flash",
    "casting_interrupted",
    "casting_curled",
    "casting_torn",
    "casting_burr",
    "casting_mixed",
    "casting_normal",
)
CASE_LABELS = {
    "casting_flash": "变厚弯曲批缝",
    "casting_interrupted": "断续飞边",
    "casting_curled": "卷曲薄片飞边",
    "casting_torn": "破裂与缺口飞边",
    "casting_burr": "不规则局部毛刺",
    "casting_mixed": "研究合成混合缺陷",
    "casting_normal": "正常铸件：同材质对照",
}


def casting_appearance(seed):
    """Share appearance across all cases of an entity, including normal controls."""
    settings = CONFIG["appearance"]
    rng = np.random.default_rng(seed + settings["seed_offset"])
    gray = float(rng.uniform(*settings["base_gray"]))
    return {
        "rgba": [gray, gray, gray, 1.0],
        "specular": float(rng.uniform(*settings["specular"])),
        "shininess": float(rng.uniform(*settings["shininess"])),
        "calibrated_from_real_images": False,
    }


def _unit(vectors):
    return vectors / np.maximum(
        np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12
    )


def _field(rng, count, lower, upper, hotspot=None):
    """Spatially correlated dimensions rather than independent vertex noise."""
    controls = rng.uniform(lower, upper, 9)
    if hotspot is not None:
        selected = rng.choice(
            len(controls),
            max(1, round(len(controls) * hotspot["control_fraction"])),
            replace=False,
        )
        controls[selected] = upper * rng.uniform(
            *hotspot["scale"], size=len(selected)
        )
        controls = np.minimum(controls, hotspot["height_cap_mm"] / 1000)
    values = np.interp(
        np.linspace(0, 1, count), np.linspace(0, 1, len(controls)), controls
    )
    return gaussian_filter1d(values, max(1, count / 90), mode="nearest")


def _sample_mm(rng, family, name, settings):
    return rng.uniform(*settings[family][name]) / 1000


def _resample(points, spacing):
    distance = np.r_[
        0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))
    ]
    keep = np.r_[True, np.diff(distance) > 1e-10]
    distance, points = distance[keep], points[keep]
    samples = np.linspace(
        0, distance[-1], max(3, int(np.ceil(distance[-1] / spacing)) + 1)
    )
    return np.column_stack(
        [np.interp(samples, distance, points[:, axis]) for axis in range(3)]
    )


@lru_cache(maxsize=4)
def _candidate_loops(mesh_path, inset_mm):
    support = top_support(mesh_path)
    eligible = np.uint8(support["margin"] > inset_mm / 1000)
    contours, _ = cv2.findContours(
        eligible, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    spacing = 0.0005
    loops = []
    for contour in contours:
        if len(contour) < 200:
            continue
        pixels = contour[:, 0]
        xy = np.column_stack(
            [
                support["grid_x"][pixels[:, 0]],
                support["grid_y"][pixels[:, 1]],
            ]
        )
        xy = gaussian_filter1d(xy, 3, axis=0, mode="wrap")
        points = np.column_stack([xy, supported_heights(support, xy)])
        loop = _resample(np.vstack([points, points[0]]), spacing)
        loops.append(loop)
    if not loops:
        raise ValueError("CAD has no supported candidate top parting curve")
    return loops


@dataclass
class CadPatch:
    """A supported local surface and the root's moving tangent frame."""

    root: np.ndarray
    normals: np.ndarray
    region: str
    support: dict | None = None
    mesh: trimesh.Trimesh | None = None

    @property
    def lateral(self):
        return _unit(np.cross(self.normals, np.gradient(self.root, axis=0)))

    def project(self, vertices):
        if self.support is not None:
            base = vertices.copy()
            base[:, 2] = supported_heights(self.support, vertices[:, :2])
            return base, np.tile([0.0, 0.0, 1.0], (len(vertices), 1))
        bases, normals = [], []
        for begin in range(0, len(vertices), 128):
            points, _, faces = trimesh.proximity.closest_point_naive(
                self.mesh, vertices[begin : begin + 128]
            )
            bases.append(points)
            normals.append(self.mesh.face_normals[faces])
        base, normal = np.vstack(bases), np.vstack(normals)
        displacement = vertices - base
        tangent_error = displacement - (
            np.einsum("ij,ij->i", displacement, normal)[:, None] * normal
        )
        if np.linalg.norm(tangent_error, axis=1).max() > 0.00015:
            raise ValueError("Sheet crosses a local CAD patch boundary")
        return base, normal


def _top_patch(mesh_path, rng, length, occupied, settings):
    support = top_support(mesh_path)
    loops = _candidate_loops(
        mesh_path, settings["candidate_top_curve_inset_mm"]
    )
    for _ in range(120):
        loop = loops[int(rng.integers(len(loops)))]
        count = max(3, int(length / 0.0005) + 1)
        begin = int(rng.integers(len(loop) - 1))
        points = loop[(begin + np.arange(count)) % (len(loop) - 1)]
        root = _resample(points, settings["root_sample_spacing_mm"] / 1000)
        tangents = _unit(np.gradient(root, axis=0))
        if np.min(np.einsum("ij,ij->i", tangents[:-1], tangents[1:])) < 0.998:
            continue
        if (
            occupied
            and cKDTree(np.vstack(occupied)).query(root)[0].min() < 0.014
        ):
            continue
        normal = np.tile([0.0, 0.0, 1.0], (len(root), 1))
        return CadPatch(root, normal, "top_flange", support=support)
    raise ValueError("Could not place a separated, smoothly supported top root")


def _side_patch(mesh_path, rng, length, occupied):
    mesh = top_support(mesh_path)["mesh"]
    centers, normals = mesh.triangles_center, mesh.face_normals
    radial = centers - [-0.028, 0, 0]
    radial[:, 2] = 0
    outward = np.einsum("ij,ij->i", radial, normals)
    candidates = np.flatnonzero(
        (centers[:, 2] > 0.18)
        & (centers[:, 2] < 0.30)
        & (np.abs(normals[:, 2]) < 0.7)
        & (outward > 0.09)
        & (mesh.area_faces > 1e-6)
    )
    for _ in range(80):
        index = rng.choice(
            candidates,
            p=mesh.area_faces[candidates] / mesh.area_faces[candidates].sum(),
        )
        origin, normal = centers[index], normals[index]
        tangent = _unit(np.cross(normal, [0.0, 0.0, 1.0]))
        near = (np.linalg.norm(centers - origin, axis=1) < length + 0.018) & (
            normals @ normal > 0.85
        )
        submesh = mesh.submesh(
            [np.flatnonzero(near)], append=True, repair=False
        )
        coordinates = np.linspace(
            -length / 2, length / 2, max(5, int(length / 0.0006) + 1)
        )
        proposed = origin + coordinates[:, None] * tangent
        roots, distance, faces = trimesh.proximity.closest_point_naive(
            submesh, proposed
        )
        if distance.max() > 0.0015:
            continue
        if (
            occupied
            and cKDTree(np.vstack(occupied)).query(roots)[0].min() < 0.014
        ):
            continue
        return CadPatch(
            roots, submesh.face_normals[faces], "outer_wall", mesh=submesh
        )
    raise ValueError("Could not find a supported outer-wall patch")


def _closed_grid(upper, lower):
    """Cap a two-sided sheet including its root, free edge and both ends."""
    rows, cols = upper.shape[:2]
    count = rows * cols
    vertices = np.vstack([upper.reshape(-1, 3), lower.reshape(-1, 3)])
    faces = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            a = row * cols + col
            b, c, d = a + cols, a + cols + 1, a + 1
            faces.extend(
                [
                    [a, b, c],
                    [a, c, d],
                    [a + count, c + count, b + count],
                    [a + count, d + count, c + count],
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
    for a, b in edges:
        faces.extend([[a, a + count, b + count], [a, b + count, b]])
    mesh = trimesh.Trimesh(vertices, faces, process=False)
    mesh.fix_normals(multibody=True)
    return mesh


def _fragments(rng, count, family, length, settings):
    gaps = [
        _sample_mm(rng, family, "gap_mm", settings) / length for _ in range(2)
    ]
    centers = [rng.uniform(0.27, 0.36), rng.uniform(0.63, 0.72)]
    spans = [
        (0, centers[0] - gaps[0] / 2),
        (centers[0] + gaps[0] / 2, centers[1] - gaps[1] / 2),
        (centers[1] + gaps[1] / 2, 1),
    ]
    ranges = []
    for begin, end in spans:
        start, stop = (
            int(np.ceil(begin * (count - 1))),
            int(np.floor(end * (count - 1))) + 1,
        )
        if stop - start < 4:
            raise ValueError("Broken flash fragment has too few root samples")
        ranges.append((start, stop))
    return ranges


def _finish(
    mesh, patch, morphology, kind, roots, boundaries, parameters, settings
):
    if (
        not mesh.is_watertight
        or not mesh.is_winding_consistent
        or mesh.volume <= 0
    ):
        raise ValueError("Generated excess is not a consistently closed volume")
    base, normals = patch.project(mesh.vertices)
    heights = np.einsum("ij,ij->i", mesh.vertices - base, normals)
    if heights.min() > -0.00002:
        raise ValueError("Generated excess has no root embedded into CAD")
    root_points = np.vstack(roots)
    root_base, _ = patch.project(root_points)
    error = float(np.linalg.norm(root_points - root_base, axis=1).max())
    if error > 0.00001:
        raise ValueError("Generated root does not lie on nominal CAD")
    return mesh, {
        "profile": settings["profile"],
        "morphology": morphology,
        "kind": kind,
        "label": 1 if kind == "seam" else 2,
        "region": patch.region,
        "height_peak_mm": float(heights.max() * 1000),
        "length_mm": float(
            sum(
                np.linalg.norm(np.diff(root, axis=0), axis=1).sum()
                for root in roots
            )
            * 1000
        ),
        "base_embedding_mm": settings["root_embedding_mm"],
        "surface_normal": _unit(np.mean(patch.normals, axis=0)).tolist(),
        "original_vertices": mesh.vertices.tolist(),
        "base_vertices": base.tolist(),
        "vertex_normals": normals.tolist(),
        "root_curves_m": [root.tolist() for root in roots],
        "removal_boundary_curves_m": [
            boundary.tolist() for boundary in boundaries
        ],
        "parameters": parameters,
        "geometry_validation": {
            "watertight": True,
            "winding_consistent": True,
            "positive_volume_mm3": float(mesh.volume * 1e9),
            "root_max_CAD_distance_mm": error * 1000,
            "connected_rooted_fragments": len(roots),
        },
        "scope": "synthetic morphology on actual CAD; parameter provenance is explicit and casting physics is not validated",
        "parting_curve_status": settings["parting_curve_status"],
        "casting_profile": profile_provenance(settings),
        "truth_for_inference": False,
    }


def _ridge(patch, rng, settings, interrupted=False):
    family = "interrupted_flash" if interrupted else "variable_flash"
    count = len(patch.root)
    width_limits = np.array(settings[family]["full_width_mm"]) / 2000
    height_limits = np.array(settings[family]["height_mm"]) / 1000
    half_width = _field(rng, count, *width_limits)
    height = _field(
        rng, count, *height_limits, hotspot=settings[family].get("hotspots")
    )
    # A shared smooth field couples thickness and height along the root.
    height *= 0.85 + 0.15 * (half_width - width_limits[0]) / np.ptp(
        width_limits
    )
    length = np.linalg.norm(np.diff(patch.root, axis=0), axis=1).sum()
    ranges = (
        _fragments(rng, count, family, length, settings)
        if interrupted
        else [(0, count)]
    )
    pieces, roots, boundaries = [], [], []
    q = np.linspace(-1, 1, 9)
    for begin, end in ranges:
        root = patch.root[begin:end]
        lateral = patch.lateral[begin:end]
        width = half_width[begin:end]
        span = len(root)
        taper = np.minimum(
            1, np.minimum(np.arange(span), np.arange(span)[::-1]) / 4
        )
        lift = np.maximum(height[begin:end] * taper, 0.000025)
        plane = (
            root[:, None] + lateral[:, None] * (width[:, None] * q)[..., None]
        )
        base, base_normal = patch.project(plane.reshape(-1, 3))
        base = base.reshape(span, len(q), 3)
        base_normal = base_normal.reshape(span, len(q), 3)
        upper = (
            base
            + base_normal
            * (
                lift[:, None] * np.cos(q * np.pi / 2)[None, :] ** 2
                - settings["root_embedding_mm"] / 1000
            )[..., None]
        )
        lower = base - base_normal * settings["root_embedding_mm"] / 1000
        # Both outer edges remain embedded; a thin closed base avoids open strips.
        upper += base_normal * 0.000015
        pieces.append(_closed_grid(upper, lower))
        roots.append(root)
        edges = []
        for sign in (-1, 1):
            boundary, _ = patch.project(
                root + lateral * (sign * width[:, None])
            )
            edges.append(boundary)
        boundaries.append(np.vstack([edges[0], edges[1][::-1], edges[0][0]]))
    mesh = trimesh.util.concatenate(pieces)
    parameters = {
        "height_range_mm": (height_limits * 1000).tolist(),
        "actual_half_width_mm": [
            float(half_width.min() * 1000),
            float(half_width.max() * 1000),
        ],
        "dimensions_spatially_correlated": True,
        "root_spans": [[begin, end] for begin, end in ranges],
        "actual_gaps_mm": [
            float(
                np.linalg.norm(patch.root[right[0]] - patch.root[left[1] - 1])
                * 1000
            )
            for left, right in zip(ranges[:-1], ranges[1:])
        ],
    }
    return _finish(
        mesh, patch, family, "seam", roots, boundaries, parameters, settings
    )


def _sheet(patch, rng, settings, torn=False):
    """An extruded thin plate bent by integrated tangent angles, with CAD roots."""
    family = "torn_flash" if torn else "curled_flash"
    count = len(patch.root)
    arc = _field(
        rng, count, *(np.array(settings[family]["sheet_arc_mm"]) / 1000)
    )
    thickness = _field(
        rng, count, *(np.array(settings[family]["thickness_mm"]) / 1000)
    )
    beta = _field(rng, count, *settings[family]["bend_radians"])
    if torn:
        s = np.linspace(0, 1, count)
        for center in rng.uniform(0.05, 0.95, 5):
            arc *= 1 - rng.uniform(0.25, 0.6) * np.maximum(
                0, 1 - np.abs(s - center) / rng.uniform(0.025, 0.055)
            )
    if np.any(arc / beta <= thickness * 0.6):
        raise ValueError("Sheet thickness exceeds the supported bend radius")
    length = np.linalg.norm(np.diff(patch.root, axis=0), axis=1).sum()
    ranges = (
        _fragments(rng, count, family, length, settings)
        if torn
        else [(0, count)]
    )
    pieces, roots, boundaries = [], [], []
    q = np.linspace(0, 1, 23)
    for begin, end in ranges:
        root = patch.root[begin:end]
        normal, lateral = patch.normals[begin:end], patch.lateral[begin:end]
        angle = beta[begin:end, None] * q[None, :]
        radius = arc[begin:end, None] / beta[begin:end, None]
        across = radius * (1 - np.cos(angle))
        above = radius * np.sin(angle) - settings["root_embedding_mm"] / 1000
        center = (
            root[:, None]
            + lateral[:, None] * across[..., None]
            + normal[:, None] * above[..., None]
        )
        sheet_normal = (
            lateral[:, None] * np.cos(angle)[..., None]
            - normal[:, None] * np.sin(angle)[..., None]
        )
        half = thickness[begin:end, None, None] / 2
        pieces.append(
            _closed_grid(
                center + half * sheet_normal, center - half * sheet_normal
            )
        )
        roots.append(root)
        edges = []
        for sign in (-1, 1):
            edge, _ = patch.project(
                root + lateral * (sign * thickness[begin:end, None] / 2)
            )
            edges.append(edge)
        boundaries.append(np.vstack([edges[0], edges[1][::-1], edges[0][0]]))
    mesh = trimesh.util.concatenate(pieces)
    parameters = {
        "sheet_arc_length_mm": [
            float(arc.min() * 1000),
            float(arc.max() * 1000),
        ],
        "thickness_mm": [
            float(thickness.min() * 1000),
            float(thickness.max() * 1000),
        ],
        "bend_radians": [float(beta.min()), float(beta.max())],
        "has_downward_curled_free_edge": bool(np.any(beta > np.pi / 2)),
        "root_spans": [[begin, end] for begin, end in ranges],
        "actual_gaps_mm": [
            float(
                np.linalg.norm(patch.root[right[0]] - patch.root[left[1] - 1])
                * 1000
            )
            for left, right in zip(ranges[:-1], ranges[1:])
        ],
        "free_edge_notches": 5 if torn else 0,
        "minimum_inner_bend_radius_mm": float(
            np.min(arc / beta - thickness / 2) * 1000
        ),
    }
    return _finish(
        mesh, patch, family, "seam", roots, boundaries, parameters, settings
    )


def _burr(patch, rng, settings):
    """Asymmetric compact multi-lobe excess with a closed, attached footprint."""
    root = patch.root[len(patch.root) // 2]
    normal = patch.normals[len(patch.root) // 2]
    first = patch.lateral[len(patch.root) // 2]
    second = _unit(np.cross(normal, first))
    prior = sample_compact_prior(settings, rng)
    diameter = (
        max(prior["radii_uv_mm"]) * 2 / 1000
        if prior is not None
        else _sample_mm(
            rng, "irregular_burr", "footprint_diameter_mm", settings
        )
    )
    radius = diameter / 2
    coordinate = np.linspace(-1, 1, 25)
    u, v = np.meshgrid(coordinate, coordinate, indexing="ij")
    aspect = (
        min(prior["radii_uv_mm"]) / max(prior["radii_uv_mm"])
        if prior is not None
        else rng.uniform(0.65, 0.95)
    )
    plane = (
        root
        + first * (u * radius)[..., None]
        + second * (v * radius * aspect)[..., None]
    )
    base, normals = patch.project(plane.reshape(-1, 3))
    base, normals = base.reshape(25, 25, 3), normals.reshape(25, 25, 3)
    radial = np.sqrt(u**2 + v**2)
    envelope = np.maximum(0, 1 - radial**2) ** 0.8
    shape = 0.35 + 0.1 * (u + 1)
    lobes = []
    for _ in range(3):
        center = rng.uniform(-0.55, 0.55, 2)
        spread = rng.uniform(0.12, 0.32)
        amplitude = rng.uniform(0.4, 1.1)
        shape += amplitude * np.exp(
            -((u - center[0]) ** 2 + (v - center[1]) ** 2) / spread**2
        )
        lobes.append({"center": center.tolist(), "spread": float(spread)})
    profile = envelope * shape
    profile /= profile.max()
    peak = (
        prior["height_mm"] / 1000
        if prior is not None
        else _sample_mm(rng, "irregular_burr", "height_mm", settings)
    )
    # Source height is excess above the nominal surface, not solid thickness.
    # Compensate the embedded base only for the new data-derived prior.
    profile_peak = (
        peak + settings["root_embedding_mm"] / 1000 - 0.000015
        if prior is not None
        else peak
    )
    upper = (
        base
        + normals
        * (
            profile * profile_peak
            - settings["root_embedding_mm"] / 1000
            + 0.000015
        )[..., None]
    )
    lower = base - normals * settings["root_embedding_mm"] / 1000
    angle = np.linspace(0, 2 * np.pi, 81)
    footprint = (
        root
        + first * (radius * np.cos(angle))[:, None]
        + second * (radius * aspect * np.sin(angle))[:, None]
    )
    footprint, _ = patch.project(footprint)
    return _finish(
        _closed_grid(upper, lower),
        patch,
        "irregular_burr",
        "burr",
        [footprint],
        [footprint],
        {
            "footprint_diameter_mm": float(diameter * 1000),
            "footprint_aspect": float(aspect),
            "lobes": lobes,
            "nominal_peak_mm": float(peak * 1000),
            "root_representation": "closed footprint boundary",
            "compact_prior_instance": prior,
            "height_reference": (
                "sampled visible normal height above nominal CAD"
                if prior is not None
                else "legacy profile amplitude before root embedding"
            ),
            "shape_scope": "asymmetric casting proxy; sampled dimensions do not reproduce the source weld/spatter semantics",
        },
        settings,
    )


def generate_casting_defects(mesh_path, seed, case, profile=BASE_PROFILE):
    """Return reproducible rooted solids with private root/stock supervision."""
    if case not in CASTING_CASES:
        raise ValueError(f"Unknown research casting case {case}")
    if case == "casting_normal":
        return []
    settings = load_profile(profile)
    mesh_path = str(Path(mesh_path).resolve())
    rng = np.random.default_rng(seed + settings["seed_offset"])
    specifications = {
        "casting_flash": [("variable_flash", "top")],
        "casting_interrupted": [("interrupted_flash", "top")],
        "casting_curled": [("curled_flash", "top")],
        "casting_torn": [("torn_flash", "top")],
        "casting_burr": [("irregular_burr", "top"), ("irregular_burr", "side")],
        "casting_mixed": [
            ("variable_flash", "top"),
            ("curled_flash", "top"),
            ("torn_flash", "top"),
            ("interrupted_flash", "side"),
            ("irregular_burr", "side"),
        ],
    }[case]
    defects, occupied = [], []
    for family, region in specifications:
        last_error = None
        for _ in range(40):
            try:
                length = (
                    0.009
                    if family == "irregular_burr"
                    else _sample_mm(rng, family, "length_mm", settings)
                )
                patch = (
                    _top_patch(mesh_path, rng, length, occupied, settings)
                    if region == "top"
                    else _side_patch(
                        mesh_path, rng, min(length, 0.043), occupied
                    )
                )
                if family in {"variable_flash", "interrupted_flash"}:
                    item = _ridge(
                        patch, rng, settings, family == "interrupted_flash"
                    )
                elif family in {"curled_flash", "torn_flash"}:
                    item = _sheet(patch, rng, settings, family == "torn_flash")
                else:
                    item = _burr(patch, rng, settings)
                defects.append(item)
                occupied.append(patch.root)
                break
            except ValueError as error:
                last_error = error
                continue
        else:
            raise ValueError(
                f"Could not construct CAD-supported {family}"
            ) from last_error
    return defects
