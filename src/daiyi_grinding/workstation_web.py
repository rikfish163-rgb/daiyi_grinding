"""Export actual MuJoCo CAD geometry for the browser's live 3D view."""

from __future__ import annotations

import hashlib
from pathlib import Path

import mujoco
import numpy as np

from .workstation_model import COMPONENTS, component_for_geom, write_json


def export_scene(sim):
    """Write indexed geometry and local transforms, without approximate robot art."""
    model = sim.model
    chunks = []
    offset = 0
    geometries = {}
    meshes = []
    original_faces = 0
    exported_faces = 0
    for geom_id in range(model.ngeom):
        if (
            model.geom_group[geom_id] == 3
            or model.geom_rgba[geom_id, 3] == 0
            or model.geom_type[geom_id] == int(mujoco.mjtGeom.mjGEOM_PLANE)
        ):
            continue
        is_mesh = model.geom_type[geom_id] == int(mujoco.mjtGeom.mjGEOM_MESH)
        mesh_id = int(model.geom_dataid[geom_id]) if is_mesh else -geom_id - 1
        name = model.geom(geom_id).name
        component = component_for_geom(name)
        if mesh_id not in geometries:
            if is_mesh:
                vertex_start = model.mesh_vertadr[mesh_id]
                vertex_count = model.mesh_vertnum[mesh_id]
                face_start = model.mesh_faceadr[mesh_id]
                face_count = model.mesh_facenum[mesh_id]
                vertices = model.mesh_vert[
                    vertex_start : vertex_start + vertex_count
                ].copy()
                faces = model.mesh_face[
                    face_start : face_start + face_count
                ].copy()
            else:
                import trimesh

                size = model.geom_size[geom_id]
                kind = model.geom_type[geom_id]
                if kind == int(mujoco.mjtGeom.mjGEOM_BOX):
                    primitive = trimesh.creation.box(extents=size * 2)
                elif kind == int(mujoco.mjtGeom.mjGEOM_CYLINDER):
                    primitive = trimesh.creation.cylinder(
                        radius=size[0], height=size[1] * 2, sections=48
                    )
                elif kind == int(mujoco.mjtGeom.mjGEOM_SPHERE):
                    primitive = trimesh.creation.icosphere(
                        subdivisions=2, radius=size[0]
                    )
                else:
                    continue
                vertices, faces = primitive.vertices, primitive.faces
            original_faces += len(faces)
            budget = (
                len(faces)
                if component in {"workpiece", "defects", "tool"}
                else 5000
                if component == "fences"
                else 7000
            )
            if len(faces) > budget:
                try:
                    import fast_simplification

                    vertices, faces = fast_simplification.simplify(
                        vertices, faces, target_count=budget
                    )
                except ImportError:
                    pass
            vertex_bytes = np.asarray(vertices, dtype="<f4").tobytes()
            face_bytes = np.asarray(faces, dtype="<u4").tobytes()
            geometries[mesh_id] = {
                "id": mesh_id,
                "vertex_offset": offset,
                "vertex_count": len(vertices),
                "index_offset": offset + len(vertex_bytes),
                "index_count": int(np.asarray(faces).size),
                "mutable": component == "defects",
            }
            exported_faces += len(faces)
            chunks.extend([vertex_bytes, face_bytes])
            offset += len(vertex_bytes) + len(face_bytes)
        material = int(model.geom_matid[geom_id])
        color = (
            model.mat_rgba[material]
            if material >= 0
            else model.geom_rgba[geom_id]
        ).tolist()
        meshes.append(
            {
                "id": geom_id,
                "name": name,
                "geometry": mesh_id,
                "body": int(model.geom_bodyid[geom_id]),
                "component": component,
                "position": model.geom_pos[geom_id].tolist(),
                "quaternion": model.geom_quat[geom_id].tolist(),
                "color": color,
            }
        )
    target = Path(sim.output) / "model.bin"
    target.write_bytes(b"".join(chunks))
    manifest = {
        "schema_version": 1,
        "units": "meters",
        "up_axis": "Z",
        "robot": "ESTUN iER220-2700",
        "source": "supplied STEP assemblies imported into MuJoCo",
        "binary": "model.bin",
        "binary_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "geometries": list(geometries.values()),
        "meshes": meshes,
        "components": COMPONENTS,
        "workpiece_body": int(sim.workpiece),
        "gripper_body": int(sim.gripper),
        "tool": sim.tool.summary(),
        "original_triangle_count": original_faces,
        "display_triangle_count": exported_faces,
        "display_reduction_scope": "background and robot display only; original CAD retained in MuJoCo; workpiece, defects and abrasive wheel unreduced",
    }
    write_json(Path(sim.output) / "model.json", manifest)
    return manifest
