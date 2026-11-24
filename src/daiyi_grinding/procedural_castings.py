"""Parametric industrial casting CAD generators for multi-master training.

These meshes are procedural proxy castings (240-400 mm scale) built with pure
NumPy and ``trimesh`` so the defect-synthesis pipeline can train across many
distinct geometric topologies while keeping real factory CADs (such as ``021``)
strictly held out for unseen-CAD evaluation. Every exported package marks
``cad_source = "procedural_parametric_casting"`` and
``physical_changeover_validated = False``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import trimesh

PROVENANCE_NOTE = (
    "Procedural parametric casting CAD for offline multi-master perception "
    "training and unseen-CAD validation only. Not a factory workpiece CAD."
)


def _heightfield_solid(xs, ys, z_top, mask, z_bottom=0.0):
    """Build a closed triangle mesh from a 2D masked heightfield over (xs, ys)."""
    ny, nx = mask.shape
    gx, gy = np.meshgrid(xs, ys)
    z_bot = np.full_like(z_top, float(z_bottom), dtype=float)

    # Create corner vertices on an (ny+1, nx+1) grid; each occupied cell (r, c)
    # uses corners (r, c), (r, c+1), (r+1, c+1), (r+1, c).
    dx = float(xs[1] - xs[0])
    dy = float(ys[1] - ys[0])
    cx = np.concatenate([xs - 0.5 * dx, [xs[-1] + 0.5 * dx]])
    cy = np.concatenate([ys - 0.5 * dy, [ys[-1] + 0.5 * dy]])
    cgx, cgy = np.meshgrid(cx, cy)

    # Corner heights = max of incident occupied cells so stepped walls stay crisp.
    corner_z = np.full((ny + 1, nx + 1), -np.inf, dtype=float)
    z_occ = np.where(mask, z_top, -np.inf)
    for dr in (0, 1):
        for dc in (0, 1):
            corner_z[dr : dr + ny, dc : dc + nx] = np.maximum(
                corner_z[dr : dr + ny, dc : dc + nx], z_occ
            )
    corner_z = np.where(np.isfinite(corner_z), corner_z, float(z_bottom))

    # Build per-cell flat top/bottom quads + boundary/step vertical walls so
    # sharp edges and hole rims have crisp 90-degree dihedral angles.
    verts = []
    faces = []

    def add_quad(p0, p1, p2, p3):
        base = len(verts)
        verts.extend((p0, p1, p2, p3))
        faces.append((base, base + 1, base + 2))
        faces.append((base, base + 2, base + 3))

    occ_r, occ_c = np.nonzero(mask)
    for r, c in zip(occ_r, occ_c, strict=True):
        x0, x1 = cx[c], cx[c + 1]
        y0, y1 = cy[r], cy[r + 1]
        zt = float(z_top[r, c])
        zb = float(z_bot[r, c])
        if zt <= zb + 1e-6:
            continue
        # Top (+Z) and bottom (-Z)
        add_quad((x0, y0, zt), (x1, y0, zt), (x1, y1, zt), (x0, y1, zt))
        add_quad((x0, y0, zb), (x0, y1, zb), (x1, y1, zb), (x1, y0, zb))

        # Four side neighbors: add vertical wall where neighbor is empty or lower.
        neighbors = (
            (r - 1, c, (x0, y0), (x1, y0)),  # south (-Y)
            (r + 1, c, (x1, y1), (x0, y1)),  # north (+Y)
            (r, c - 1, (x0, y1), (x0, y0)),  # west (-X)
            (r, c + 1, (x1, y0), (x1, y1)),  # east (+X)
        )
        for nr, nc, (ax, ay), (bx, by) in neighbors:
            if 0 <= nr < ny and 0 <= nc < nx and mask[nr, nc]:
                zn = float(z_top[nr, nc])
                if zn < zt - 1e-5:
                    add_quad(
                        (ax, ay, zn),
                        (bx, by, zn),
                        (bx, by, zt),
                        (ax, ay, zt),
                    )
            else:
                add_quad(
                    (ax, ay, zb),
                    (bx, by, zb),
                    (bx, by, zt),
                    (ax, ay, zt),
                )

    del gx, gy, cgx, cgy, corner_z
    mesh = trimesh.Trimesh(
        vertices=np.asarray(verts, dtype=float),
        faces=np.asarray(faces, dtype=int),
        process=True,
    )
    return mesh


def _grid(half_x, half_y, step=0.004):
    nx = int(round(2 * half_x / step))
    ny = int(round(2 * half_y / step))
    xs = np.linspace(-half_x, half_x, nx)
    ys = np.linspace(-half_y, half_y, ny)
    gx, gy = np.meshgrid(xs, ys)
    return xs, ys, gx, gy


def _finalize(mesh):
    bounds = mesh.bounds
    shift = np.array(
        [
            -0.5 * (bounds[0, 0] + bounds[1, 0]),
            -0.5 * (bounds[0, 1] + bounds[1, 1]),
            -bounds[0, 2],
        ]
    )
    mesh.apply_translation(shift)
    return mesh


def _flange_mesh():
    """Circular bolted flange with central bore, 6 bolt holes, hub & radial ribs."""
    xs, ys, gx, gy = _grid(0.175, 0.175, step=0.004)
    r = np.hypot(gx, gy)
    theta = np.arctan2(gy, gx)
    mask = (r <= 0.168) & (r >= 0.052)
    for k in range(6):
        ang = k * math.pi / 3
        mask &= (
            np.hypot(gx - 0.125 * math.cos(ang), gy - 0.125 * math.sin(ang))
            >= 0.015
        )
    z = np.full_like(gx, 0.032)
    z = np.where(r <= 0.088, 0.108, z)
    # Six radial ribs between hub and rim
    for k in range(6):
        ang = k * math.pi / 3 + math.pi / 6
        ux, uy = math.cos(ang), math.sin(ang)
        proj = gx * ux + gy * uy
        perp = np.abs(-gx * uy + gy * ux)
        rib = (proj >= 0.080) & (proj <= 0.156) & (perp <= 0.009)
        z = np.where(rib, np.maximum(z, 0.072), z)
    del theta
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _housing_mesh():
    """Stepped rectangular gearbox housing with internal pocket, bosses & ribs."""
    xs, ys, gx, gy = _grid(0.195, 0.150, step=0.004)
    mask = (np.abs(gx) <= 0.186) & (np.abs(gy) <= 0.140)
    # Rounded corners
    corner_r = np.hypot(
        np.maximum(np.abs(gx) - 0.164, 0.0), np.maximum(np.abs(gy) - 0.118, 0.0)
    )
    mask &= corner_r <= 0.022
    # Central bore and four corner mounting holes
    mask &= np.hypot(gx, gy) >= 0.030
    for sx in (-1, 1):
        for sy in (-1, 1):
            mask &= np.hypot(gx - sx * 0.148, gy - sy * 0.106) >= 0.015
    z = np.full_like(gx, 0.030)
    wall = (
        (np.abs(gx) <= 0.132)
        & (np.abs(gy) <= 0.096)
        & ~((np.abs(gx) <= 0.096) & (np.abs(gy) <= 0.064))
    )
    z = np.where(wall, 0.118, z)
    # Cross ribs inside pocket + corner bosses
    ribs = ((np.abs(gx) <= 0.098) & (np.abs(gy) <= 0.010)) | (
        (np.abs(gy) <= 0.066) & (np.abs(gx) <= 0.010)
    )
    z = np.where(ribs, np.maximum(z, 0.068), z)
    for sx in (-1, 1):
        for sy in (-1, 1):
            boss = np.hypot(gx - sx * 0.148, gy - sy * 0.106) <= 0.028
            z = np.where(boss, np.maximum(z, 0.062), z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _bracket_mesh():
    """L-shaped structural casting bracket with gusset webs and mounting bores."""
    xs, ys, gx, gy = _grid(0.185, 0.145, step=0.004)
    mask = (np.abs(gx) <= 0.176) & (np.abs(gy) <= 0.136)
    for x in (-0.112, 0.0, 0.112):
        mask &= np.hypot(gx - x, gy + 0.078) >= 0.016
    mask &= np.hypot(gx, gy - 0.010) >= 0.028
    z = np.full_like(gx, 0.030)
    step = (np.abs(gx) <= 0.142) & (gy >= -0.034) & (gy <= 0.068)
    z = np.where(step, 0.056, z)
    upright = (np.abs(gx) <= 0.166) & (gy >= 0.068) & (gy <= 0.128)
    z = np.where(upright, 0.138, z)
    for x in (-0.118, 0.0, 0.118):
        gusset = (np.abs(gx - x) <= 0.011) & (gy >= -0.026) & (gy <= 0.072)
        ramp = 0.056 + 0.072 * np.clip((gy + 0.026) / 0.098, 0.0, 1.0)
        z = np.where(gusset, np.maximum(z, ramp), z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _manifold_mesh():
    """Twin-port curved manifold casting with stepped saddle and bolt collar."""
    xs, ys, gx, gy = _grid(0.205, 0.125, step=0.004)
    r_left = np.hypot(gx + 0.098, gy)
    r_right = np.hypot(gx - 0.098, gy)
    mask = (
        ((np.abs(gx) <= 0.105) & (np.abs(gy) <= 0.108))
        | (r_left <= 0.108)
        | (r_right <= 0.108)
    )
    mask &= (r_left >= 0.036) & (r_right >= 0.036)
    for x in (-0.168, 0.0, 0.168):
        for y in (-0.074, 0.074):
            if (
                np.hypot(x + 0.098, y) > 0.055
                and np.hypot(x - 0.098, y) > 0.055
            ):
                mask &= np.hypot(gx - x, gy - y) >= 0.013
    z = np.full_like(gx, 0.030)
    z = np.where(r_left <= 0.064, 0.116, z)
    z = np.where(r_right <= 0.064, 0.100, z)
    # Curved arched bridge between twin ports
    arch = (np.abs(gx) <= 0.056) & (np.abs(gy) <= 0.048)
    arch_z = 0.074 + 0.018 * np.cos(
        np.clip(gy / 0.048, -1.0, 1.0) * math.pi * 0.5
    )
    z = np.where(arch, np.maximum(z, arch_z), z)
    rails = (np.abs(gx) <= 0.078) & (np.abs(np.abs(gy) - 0.058) <= 0.009)
    z = np.where(rails, np.maximum(z, 0.064), z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _hub_mesh():
    """Stepped bearing/wheel hub with recessed dish, lug bosses and outer rim."""
    xs, ys, gx, gy = _grid(0.180, 0.180, step=0.004)
    r = np.hypot(gx, gy)
    mask = (r <= 0.174) & (r >= 0.042)
    for k in range(5):
        ang = k * 2 * math.pi / 5
        mask &= (
            np.hypot(gx - 0.110 * math.cos(ang), gy - 0.110 * math.sin(ang))
            >= 0.015
        )
    z = np.full_like(gx, 0.028)
    z = np.where(r >= 0.142, 0.088, z)
    z = np.where(r <= 0.078, 0.108, z)
    for k in range(5):
        ang = k * 2 * math.pi / 5
        lug = (
            np.hypot(gx - 0.110 * math.cos(ang), gy - 0.110 * math.sin(ang))
            <= 0.028
        )
        z = np.where(lug, np.maximum(z, 0.066), z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _cover_mesh():
    """Oval pump cover casting with rib grid, bolt ears and central dome."""
    xs, ys, gx, gy = _grid(0.205, 0.140, step=0.004)
    in_body = (
        (np.hypot(gx + 0.082, gy) <= 0.104)
        | (np.hypot(gx - 0.082, gy) <= 0.104)
        | ((np.abs(gx) <= 0.082) & (np.abs(gy) <= 0.104))
    )
    ears = (
        (-0.172, 0.0),
        (0.172, 0.0),
        (-0.072, 0.104),
        (0.072, 0.104),
        (-0.072, -0.104),
        (0.072, -0.104),
    )
    mask = in_body.copy()
    for ex, ey in ears:
        mask |= np.hypot(gx - ex, gy - ey) <= 0.028
    for ex, ey in ears:
        mask &= np.hypot(gx - ex, gy - ey) >= 0.013
    mask &= np.hypot(gx, gy) >= 0.026
    z = np.full_like(gx, 0.028)
    dome_region = (
        (np.hypot(gx + 0.052, gy) <= 0.072)
        | (np.hypot(gx - 0.052, gy) <= 0.072)
        | ((np.abs(gx) <= 0.052) & (np.abs(gy) <= 0.072))
    )
    z = np.where(dome_region, 0.076, z)
    ribs = ((np.abs(gx) <= 0.120) & (np.abs(gy) <= 0.009)) | (
        (np.abs(gy) <= 0.066)
        & ((np.abs(gx + 0.062) <= 0.009) | (np.abs(gx - 0.062) <= 0.009))
    )
    z = np.where(ribs & dome_region, 0.102, z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _yoke_mesh():
    """Forked U-shaped clevis casting with twin arms, bridge & bore rims."""
    xs, ys, gx, gy = _grid(0.185, 0.155, step=0.004)
    outer = (np.abs(gx) <= 0.176) & (gy >= -0.132) & (gy <= 0.146)
    slot = (np.abs(gx) <= 0.066) & (gy >= -0.018)
    mask = outer & ~slot
    for x, y, rad in (
        (-0.122, 0.086, 0.024),
        (0.122, 0.086, 0.024),
        (0.0, -0.076, 0.028),
        (-0.126, -0.078, 0.015),
        (0.126, -0.078, 0.015),
    ):
        mask &= np.hypot(gx - x, gy - y) >= rad
    z = np.full_like(gx, 0.032)
    arms = (
        (np.abs(gx) >= 0.078)
        & (np.abs(gx) <= 0.166)
        & (gy >= -0.014)
        & (gy <= 0.136)
    )
    z = np.where(arms, 0.098, z)
    bridge = (np.abs(gx) <= 0.148) & (gy >= -0.118) & (gy <= -0.030)
    z = np.where(bridge, 0.072, z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


def _valve_mesh():
    """T-junction valve body casting with triple flanged pads & central dome."""
    xs, ys, gx, gy = _grid(0.195, 0.185, step=0.004)
    stem_h = (np.abs(gx) <= 0.186) & (np.abs(gy) <= 0.072)
    stem_v = (np.abs(gx) <= 0.074) & (gy >= -0.056) & (gy <= 0.176)
    mask = stem_h | stem_v
    for x, y, rad in (
        (-0.142, 0.0, 0.022),
        (0.142, 0.0, 0.022),
        (0.0, 0.132, 0.022),
        (0.0, 0.0, 0.030),
    ):
        mask &= np.hypot(gx - x, gy - y) >= rad
    z = np.full_like(gx, 0.032)
    z = np.where(np.hypot(gx, gy) <= 0.068, 0.118, z)
    for x, y in ((-0.142, 0.0), (0.142, 0.0), (0.0, 0.132)):
        z = np.where(np.hypot(gx - x, gy - y) <= 0.044, np.maximum(z, 0.078), z)
    fins = ((np.abs(gx) <= 0.112) & (np.abs(gy + 0.046) <= 0.009)) | (
        (np.abs(gx) <= 0.009) & (gy >= 0.040) & (gy <= 0.100)
    )
    z = np.where(fins, np.maximum(z, 0.066), z)
    return _finalize(_heightfield_solid(xs, ys, z, mask))


CASTING_BUILDERS = {
    "p01": ("proc_flange_01", _flange_mesh),
    "p02": ("proc_housing_02", _housing_mesh),
    "p03": ("proc_bracket_03", _bracket_mesh),
    "p04": ("proc_manifold_04", _manifold_mesh),
    "p05": ("proc_hub_05", _hub_mesh),
    "p06": ("proc_cover_06", _cover_mesh),
    "p07": ("proc_yoke_07", _yoke_mesh),
    "p08": ("proc_valve_08", _valve_mesh),
}


def materialize_procedural_castings(output_dir, ids=None):
    """Export procedural casting STLs and package manifests into ``output_dir``."""
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    selected = list(CASTING_BUILDERS) if ids is None else list(ids)
    records = []
    for key in selected:
        if key not in CASTING_BUILDERS:
            raise ValueError(f"Unknown procedural casting key: {key}")
        name, builder = CASTING_BUILDERS[key]
        mesh = builder()
        stl_path = target / f"wp_{key}_c0.stl"
        mesh.export(str(stl_path))
        package_path = target / f"wp_{key}.json"
        payload = {
            "workpiece_id": key,
            "package_id": name,
            "mesh_path": str(stl_path),
            "units": "m",
            "cad_source": "procedural_parametric_casting",
            "physical_changeover_validated": False,
            "description": PROVENANCE_NOTE,
            "protected_zones": [],
        }
        package_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        records.append(
            {
                "workpiece_id": key,
                "package_id": name,
                "mesh_path": str(stl_path),
                "package_path": str(package_path),
                "vertices": int(len(mesh.vertices)),
                "faces": int(len(mesh.faces)),
                "extents_mm": ((mesh.bounds[1] - mesh.bounds[0]) * 1000.0)
                .round(2)
                .tolist(),
            }
        )
    return records
