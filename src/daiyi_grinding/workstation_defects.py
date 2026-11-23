"""Seeded excess on multiple real CAD faces, isolated from perception inputs."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from .cad_defects import CASES, generate_defects
from .casting_defects import CASTING_CASES, generate_casting_defects
from .casting_profiles import BASE_PROFILE

WORKSTATION_CASES = (*CASES, *CASTING_CASES)


@lru_cache(maxsize=2)
def _cad(path):
    import trimesh

    return trimesh.load_mesh(path, process=True)


def _side_defect(mesh, rng, seam=False):
    """Attach a ridge/dome to an outward-facing CAD patch without closing holes."""
    import trimesh

    centers = mesh.triangles_center
    radial = centers - [-0.028, 0, 0]
    radial[:, 2] = 0
    normals = mesh.face_normals
    outward = np.einsum("ij,ij->i", radial, normals)
    candidates = np.flatnonzero(
        (centers[:, 2] > 0.17)
        & (centers[:, 2] < 0.29)
        & (np.abs(normals[:, 2]) < 0.85)
        & (outward > 0.08)
        & (mesh.area_faces > 1e-6)
    )
    if not len(candidates):
        return None
    index = rng.choice(
        candidates,
        p=mesh.area_faces[candidates] / mesh.area_faces[candidates].sum(),
    )
    origin = centers[index]
    normal = normals[index]
    tangent = np.cross(normal, [0, 0, 1])
    tangent /= np.linalg.norm(tangent)
    other = np.cross(normal, tangent)
    if seam:
        first = np.linspace(-0.022, 0.022, 65)
        second = np.array([-0.0007, 0, 0.0007])
        u, v = np.meshgrid(first, second, indexing="ij")
        profile = np.cos(v / 0.0007 * np.pi / 2) ** 2
        profile *= np.minimum(1, (0.022 - np.abs(u)) / 0.003)
        peak = rng.uniform(0.0012, 0.0020)
    else:
        coordinates = np.linspace(-0.0038, 0.0038, 21)
        u, v = np.meshgrid(coordinates, coordinates, indexing="ij")
        radius = np.sqrt(u**2 + v**2) / 0.0038
        profile = np.maximum(0, 1 - radius**2) ** 1.3
        peak = rng.uniform(0.0015, 0.0028)
    plane_points = (
        origin + u.reshape(-1, 1) * tangent + v.reshape(-1, 1) * other
    )
    # Restrict nearest-surface queries to the same local outward CAD patch.
    near = np.linalg.norm(centers - origin, axis=1) < 0.065
    near &= normals @ normal > 0.75
    submesh = mesh.submesh([np.flatnonzero(near)], append=True, repair=False)
    support, distance, faces = trimesh.proximity.closest_point_naive(
        submesh, plane_points
    )
    if distance.max() > 0.008:
        return None
    base_normals = submesh.face_normals[faces]
    vertices = support + base_normals * (
        peak * profile.reshape(-1, 1) - 0.00008
    )
    rows, cols = u.shape
    triangles = []
    for row in range(rows - 1):
        for col in range(cols - 1):
            a = row * cols + col
            triangles.extend(
                [[a, a + cols, a + cols + 1], [a, a + cols + 1, a + 1]]
            )
    defect = trimesh.Trimesh(vertices, triangles, process=False)
    return defect, {
        "kind": "seam" if seam else "burr",
        "label": 1 if seam else 2,
        "region": "outer_wall",
        "height_peak_mm": float(peak * 1000),
        "surface_normal": normal.tolist(),
        "original_vertices": vertices.tolist(),
        "base_vertices": support.tolist(),
        "vertex_normals": base_normals.tolist(),
        "scope": "synthetic excess embedded on supplied nominal CAD; not a real defect scan",
    }


def generate_cell_defects(mesh_path, seed, case, casting_profile=BASE_PROFILE):
    """Keep legacy top cases and add visible side patches for full-cell testing."""
    if case in CASTING_CASES:
        return generate_casting_defects(mesh_path, seed, case, casting_profile)
    defects = generate_defects(mesh_path, seed, case)
    for _, metadata in defects:
        metadata["region"] = "top_flange"
        metadata["surface_normal"] = [0, 0, 1]
        metadata["nominal_plane_z_m"] = 0.361
    if case in {"mixed", "noisy", "burr"}:
        rng = np.random.default_rng(seed + 29011)
        mesh = _cad(str(mesh_path))
        for seam in (False, True) if case != "burr" else (False,):
            item = _side_defect(mesh, rng, seam)
            if item is not None:
                defects.append(item)
    return defects
