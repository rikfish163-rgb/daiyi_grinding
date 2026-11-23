"""Generate attached casting defects and isolated RGB-D truth on CAD bases.

CAD versions 097 and 021 are identifiers supplied with the assembly. Their
manufacturing stage and any pre-existing defects have not been established.
The clean reference uses the same pose and an ideal, configured camera.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt, map_coordinates
from scipy.spatial.transform import Rotation

from . import project

CASES = ("normal", "seam", "burr", "mixed", "thin", "coarse", "noisy")
DEFAULT_MESH = (
    project.ROOT / "assets/ier220_workstation/meshes/workpiece/wp_097_c0.stl"
)
GENERATOR_CONFIG = {
    "schema_version": 1,
    "grid_spacing_m": 0.0005,
    "surface_z_m": 0.361,
    "support_height_tolerance_m": 0.0003,
    "embedded_base_m": 0.00008,
    "seam_height_m": [0.0015, 0.0030],
    "thin_height_m": [0.00035, 0.0010],
    "burr_height_m": [0.0015, 0.0035],
    "review_height_m": [0.007, 0.011],
    "depth_noise_mm": 0.06,
    "dropout": 0.003,
    "stress_noise_mm": 0.25,
    "stress_dropout": 0.025,
    "camera_width": 1024,
    "camera_height": 768,
    "camera_fovy_deg": 44.0,
    "top_inspection_below_m": 0.030,
    "top_inspection_above_m": 0.025,
    "pose_xy_jitter_m": 0.008,
    "pose_roll_pitch_deg": 0.4,
    "pose_yaw_deg": 4.0,
    "world_support_height_m": 0.6,
    "reference": "ideal CAD reference rendered at exactly the observed pose",
    "truth_labels": {
        "0": "background",
        "1": "added parting seam",
        "2": "added local burr",
        "3": "visible baseline CAD surface",
        "4": "added excess requiring review",
    },
}


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def camera_axes(position, target):
    """Return MuJoCo camera axes with its negative Z looking at target."""
    camera_z = np.asarray(position) - np.asarray(target)
    camera_z = camera_z / np.linalg.norm(camera_z)
    camera_x = np.cross([0.0, 0.0, 1.0], camera_z)
    if np.linalg.norm(camera_x) < 0.01:
        camera_x = np.array([1.0, 0.0, 0.0])
    camera_x /= np.linalg.norm(camera_x)
    camera_y = np.cross(camera_z, camera_x)
    return " ".join(str(value) for value in np.r_[camera_x, camera_y])


@lru_cache(maxsize=2)
def top_support(mesh_path):
    """Rasterize top-facing CAD triangles, retaining real holes and steps."""
    import trimesh

    mesh = trimesh.load_mesh(mesh_path, process=True)
    spacing = GENERATOR_CONFIG["grid_spacing_m"]
    grid_x = np.arange(mesh.bounds[0, 0], mesh.bounds[1, 0] + spacing, spacing)
    grid_y = np.arange(mesh.bounds[0, 1], mesh.bounds[1, 1] + spacing, spacing)
    heights = np.full((len(grid_y), len(grid_x)), np.nan)
    for triangle in mesh.triangles[mesh.face_normals[:, 2] > 0.9]:
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
        weight_third = 1 - weight_first - weight_second
        valid = (
            (weight_first >= -1e-5)
            & (weight_second >= -1e-5)
            & (weight_third >= -1e-5)
        )
        proposed = (
            weight_first * first[2]
            + weight_second * second[2]
            + weight_third * third[2]
        )
        patch = heights[row_start:row_end, col_start:col_end]
        patch[valid] = np.fmax(patch[valid], proposed[valid])
    support = np.isfinite(heights) & (
        np.abs(heights - GENERATOR_CONFIG["surface_z_m"])
        < GENERATOR_CONFIG["support_height_tolerance_m"]
    )
    if support.sum() < 500:
        raise ValueError("CAD top plane has insufficient supported area")
    return {
        "mesh": mesh,
        "grid_x": grid_x,
        "grid_y": grid_y,
        "height": heights,
        "mask": support,
        "margin": distance_transform_edt(support) * spacing,
    }


def supported_heights(support, points):
    """Interpolate actual CAD surface heights for attached defect vertices."""
    spacing = GENERATOR_CONFIG["grid_spacing_m"]
    grid_coords = np.array(
        [
            (points[:, 1] - support["grid_y"][0]) / spacing,
            (points[:, 0] - support["grid_x"][0]) / spacing,
        ]
    )
    heights = map_coordinates(
        support["height"], grid_coords, order=1, mode="nearest"
    )
    margin = map_coordinates(
        support["margin"], grid_coords, order=1, mode="constant", cval=0
    )
    if not np.isfinite(heights).all() or np.any(margin < 0.0005):
        raise ValueError("A defect vertex crosses unsupported CAD geometry")
    return heights


def seam_mesh(support, rng, thin=False):
    """Create a varying, embedded ridge on a supported planar flange band."""
    import trimesh

    eligible = support["margin"] > 0.008
    choices = []
    for row_index, row in enumerate(eligible):
        padded = np.r_[False, row, False]
        transitions = np.flatnonzero(padded[1:] != padded[:-1])
        for col_start, col_end in zip(transitions[::2], transitions[1::2]):
            if col_end - col_start > 150:
                choices.append((row_index, col_start, col_end))
    if not choices:
        raise ValueError("No supported CAD band can contain a parting seam")
    row_index, col_start, col_end = choices[int(rng.integers(len(choices)))]
    grid_x = support["grid_x"]
    available_start = grid_x[col_start] + 0.002
    available_end = grid_x[col_end - 1] - 0.002
    length = min(rng.uniform(0.075, 0.115), available_end - available_start)
    begin = rng.uniform(available_start, available_end - length)
    samples = np.linspace(begin, begin + length, int(length / 0.00065) + 1)
    center_y = support["grid_y"][row_index]
    phase = rng.uniform(-np.pi, np.pi)
    wave = 0.0008 * np.sin((samples - begin) * 80 + phase)
    widths = rng.uniform(0.0006, 0.00095) * (
        1 + 0.16 * np.sin((samples - begin) * 180 + phase)
    )
    range_key = "thin_height_m" if thin else "seam_height_m"
    height = rng.uniform(*GENERATOR_CONFIG[range_key])
    ridge_height = height * (
        0.82 + 0.18 * np.sin((samples - begin) * 150 + phase)
    )
    ridge_height += 0.00002 * np.sin((samples - begin) * 700)
    taper = np.minimum(
        1.0,
        np.minimum(
            (samples - begin) / 0.004, (begin + length - samples) / 0.004
        ),
    )
    ridge_height *= np.maximum(0.0, taper)
    strips = []
    for lateral, profile in ((-1.0, 0.0), (0.0, 1.0), (1.0, 0.0)):
        xy = np.column_stack([samples, center_y + wave + lateral * widths])
        base_z = supported_heights(support, xy)
        strips.append(
            np.column_stack(
                [
                    xy,
                    base_z
                    - GENERATOR_CONFIG["embedded_base_m"]
                    + ridge_height * profile,
                ]
            )
        )
    vertices = np.stack(strips, axis=1).reshape(-1, 3)
    faces = []
    for index in range(len(samples) - 1):
        left = 3 * index
        right = 3 * (index + 1)
        for offset in (0, 1):
            faces.extend(
                (
                    [left + offset, right + offset, right + offset + 1],
                    [left + offset, right + offset + 1, left + offset + 1],
                )
            )
        faces.extend(([left, left + 2, right + 2], [left, right + 2, right]))
    faces.extend(
        ([0, 1, 2], [3 * (len(samples) - 1) + offset for offset in (2, 1, 0)])
    )
    mesh = trimesh.Trimesh(vertices, np.array(faces), process=False)
    return mesh, {
        "kind": "seam",
        "label": 1,
        "height_peak_mm": float(ridge_height.max() * 1000),
        "length_mm": float(length * 1000),
        "width_mm": float(2 * widths.max() * 1000),
        "base_embedding_mm": GENERATOR_CONFIG["embedded_base_m"] * 1000,
    }


def burr_mesh(support, rng, centers, review=False):
    """Create a radially irregular dome embedded into the measured CAD plane."""
    import trimesh

    radius = (
        rng.uniform(0.006, 0.008) if review else rng.uniform(0.0025, 0.0045)
    )
    candidates = np.argwhere(support["margin"] > radius * 1.4)
    rng.shuffle(candidates)
    center = None
    for row_index, col_index in candidates:
        proposed = np.array(
            [support["grid_x"][col_index], support["grid_y"][row_index]]
        )
        if all(
            np.linalg.norm(proposed - previous) > 0.025 for previous in centers
        ):
            center = proposed
            break
    if center is None:
        raise ValueError(
            "Cannot place separate burrs on supported CAD surfaces"
        )
    centers.append(center)
    range_key = "review_height_m" if review else "burr_height_m"
    peak = rng.uniform(*GENERATOR_CONFIG[range_key])
    angle = np.arange(40) * 2 * np.pi / 40
    phase = rng.uniform(-np.pi, np.pi)
    irregular_radius = radius * (
        1 + 0.13 * np.sin(5 * angle + phase) + 0.06 * np.sin(9 * angle)
    )
    vertices = [
        np.r_[center, supported_heights(support, center[None])[0] + peak]
    ]
    fractions = np.linspace(0.10, 1.0, 10)
    for fraction in fractions:
        radial = irregular_radius * fraction
        xy = center + np.column_stack(
            [radial * np.cos(angle), radial * np.sin(angle)]
        )
        heights = (
            supported_heights(support, xy)
            - GENERATOR_CONFIG["embedded_base_m"]
            + peak * np.maximum(0.0, 1 - fraction) ** 0.7
        )
        vertices.extend(np.column_stack([xy, heights]))
    bottom_index = len(vertices)
    vertices.append(
        np.r_[
            center,
            supported_heights(support, center[None])[0]
            - GENERATOR_CONFIG["embedded_base_m"],
        ]
    )
    faces = []
    for index in range(len(angle)):
        following = (index + 1) % len(angle)
        faces.append([0, 1 + index, 1 + following])
        for ring_index in range(len(fractions) - 1):
            inner = 1 + ring_index * len(angle)
            outer = inner + len(angle)
            faces.extend(
                (
                    [inner + index, outer + index, outer + following],
                    [inner + index, outer + following, inner + following],
                )
            )
        outer = 1 + (len(fractions) - 1) * len(angle)
        faces.append([bottom_index, outer + following, outer + index])
    mesh = trimesh.Trimesh(vertices, np.array(faces), process=False)
    return mesh, {
        "kind": "review" if review else "burr",
        "label": 4 if review else 2,
        "height_peak_mm": float(peak * 1000),
        "width_mm": float(2 * irregular_radius.max() * 1000),
        "base_embedding_mm": GENERATOR_CONFIG["embedded_base_m"] * 1000,
    }


def generate_defects(mesh_path, seed, case):
    """Return added geometries and private generator metadata for a CAD case."""
    if case not in CASES:
        raise ValueError(f"Unknown CAD defect case {case}")
    rng = np.random.default_rng(seed)
    support = top_support(str(Path(mesh_path).resolve()))
    defects = []
    if case in ("mixed", "seam", "thin", "coarse", "noisy"):
        defects.append(seam_mesh(support, rng, thin=case == "thin"))
    if case in ("mixed", "burr", "noisy"):
        centers = []
        for _ in range(2):
            defects.append(burr_mesh(support, rng, centers))
    if case == "coarse":
        defects.append(burr_mesh(support, rng, [], review=True))
    return defects


def pose_for_seed(seed):
    """Return small casting pose variation around a fixed inspection station."""
    rng = np.random.default_rng(seed + 10091)
    translation = np.r_[rng.uniform(-0.008, 0.008, 2), 0.6]
    angles = np.deg2rad(np.r_[rng.uniform(-0.4, 0.4, 2), rng.uniform(-4, 4)])
    rotation = Rotation.from_euler("xyz", angles)
    quaternion = rotation.as_quat()[[3, 0, 1, 2]]
    return translation, rotation.as_matrix(), quaternion


def sample_xml(mesh_path, defect_paths, translation, quaternion):
    """Build one inspection scene without changing the original workstation."""
    root = ET.Element("mujoco", model="CAD casting defect inspection")
    ET.SubElement(root, "compiler", angle="radian")
    visual = ET.SubElement(root, "visual")
    ET.SubElement(visual, "global", offwidth="1024", offheight="768")
    ET.SubElement(visual, "map", znear="0.01", zfar="10")
    assets = ET.SubElement(root, "asset")
    ET.SubElement(
        assets, "mesh", name="cad_mesh", file=str(mesh_path), inertia="shell"
    )
    for index, path in enumerate(defect_paths):
        ET.SubElement(
            assets,
            "mesh",
            name=f"defect_mesh_{index}",
            file=str(path),
            inertia="shell",
        )
    world = ET.SubElement(root, "worldbody")
    ET.SubElement(world, "light", pos="0.6 -0.8 2.2", diffuse="0.8 0.8 0.8")
    ET.SubElement(world, "light", pos="-0.7 0.6 1.8", diffuse="0.4 0.4 0.4")
    ET.SubElement(
        world,
        "geom",
        name="table",
        type="box",
        pos="0 0 0.58",
        size="0.50 0.45 0.02",
        rgba="0.18 0.22 0.27 1",
        group="0",
    )
    camera_position = [-0.025, -0.22, 1.50]
    camera_target = [-0.025, 0.0, 0.85]
    ET.SubElement(
        world,
        "camera",
        name="inspection",
        pos=" ".join(map(str, camera_position)),
        xyaxes=camera_axes(camera_position, camera_target),
        fovy="44",
    )
    overview_position = [0.65, -0.85, 1.33]
    ET.SubElement(
        world,
        "camera",
        name="overview",
        pos=" ".join(map(str, overview_position)),
        xyaxes=camera_axes(overview_position, camera_target),
        fovy="40",
    )
    casting = ET.SubElement(
        world,
        "body",
        name="casting",
        pos=" ".join(map(str, translation)),
        quat=" ".join(map(str, quaternion)),
    )
    ET.SubElement(
        casting,
        "geom",
        name="cad_surface",
        type="mesh",
        mesh="cad_mesh",
        rgba="0.53 0.55 0.58 1",
        contype="0",
        conaffinity="0",
        group="1",
    )
    for index in range(len(defect_paths)):
        ET.SubElement(
            casting,
            "geom",
            name=f"added_defect_{index}",
            type="mesh",
            mesh=f"defect_mesh_{index}",
            rgba="0.53 0.55 0.58 1",
            contype="0",
            conaffinity="0",
            group="2",
        )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


def camera_info(model, data, camera="inspection"):
    """Return configured pinhole intrinsics and OpenCV camera-to-world pose."""
    width = GENERATOR_CONFIG["camera_width"]
    height = GENERATOR_CONFIG["camera_height"]
    camera_id = model.camera(camera).id
    focal = 0.5 * height / np.tan(np.deg2rad(model.cam_fovy[camera_id]) / 2)
    intrinsics = np.array(
        [[focal, 0, (width - 1) / 2], [0, focal, (height - 1) / 2], [0, 0, 1.0]]
    )
    extrinsics = np.eye(4)
    extrinsics[:3, :3] = data.cam_xmat[camera_id].reshape(3, 3) @ np.diag(
        [1, -1, -1]
    )
    extrinsics[:3, 3] = data.cam_xpos[camera_id]
    return intrinsics, extrinsics


def render_sample(output, seed, case, mesh_path=DEFAULT_MESH):
    """Save public observations and separate evaluator-only labels for one case.

    observation.npz contains rgb, depth, reference_depth, K, T_world_camera,
    inspection_bounds_min and inspection_bounds_max. evaluator/truth.npz holds
    segmentation-derived truth, ideal_depth, trueK and trueT. It must not be
    provided to a detector. Returned metadata reports file paths and counts.
    """
    os.environ.setdefault("MUJOCO_GL", "egl")
    import mujoco

    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    mesh_path = Path(mesh_path).resolve()
    defects = generate_defects(mesh_path, seed, case)
    defect_paths = []
    for index, (mesh, metadata) in enumerate(defects):
        path = out / f"added_defect_{index}.stl"
        mesh.export(path)
        defect_paths.append(path)
    translation, rotation, quaternion = pose_for_seed(seed)
    scene_source = sample_xml(mesh_path, defect_paths, translation, quaternion)
    (out / "scene.xml").write_text(scene_source, encoding="utf-8")
    model = mujoco.MjModel.from_xml_string(scene_source)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    render_options = mujoco.MjvOption()
    renderer = mujoco.Renderer(
        model,
        GENERATOR_CONFIG["camera_height"],
        GENERATOR_CONFIG["camera_width"],
    )
    try:
        render_options.geomgroup[2] = 0
        renderer.update_scene(
            data, camera="inspection", scene_option=render_options
        )
        reference_rgb = renderer.render().copy()
        renderer.enable_depth_rendering()
        renderer.update_scene(
            data, camera="inspection", scene_option=render_options
        )
        reference_depth = renderer.render().copy()
        renderer.disable_depth_rendering()
        render_options.geomgroup[2] = 1
        renderer.update_scene(
            data, camera="inspection", scene_option=render_options
        )
        rgb = renderer.render().copy()
        renderer.enable_depth_rendering()
        renderer.update_scene(
            data, camera="inspection", scene_option=render_options
        )
        ideal_depth = renderer.render().copy()
        renderer.disable_depth_rendering()
        renderer.update_scene(
            data, camera="overview", scene_option=render_options
        )
        overview = renderer.render().copy()
        intrinsics, extrinsics = camera_info(model, data)
        noisy = case == "noisy"
        noise_mm = GENERATOR_CONFIG[
            "stress_noise_mm" if noisy else "depth_noise_mm"
        ]
        dropout = GENERATOR_CONFIG["stress_dropout" if noisy else "dropout"]
        rng = np.random.default_rng(seed + 12117)
        depth = ideal_depth + rng.normal(0, noise_mm / 1000, ideal_depth.shape)
        depth[rng.random(depth.shape) < dropout] = np.nan
        base_vertices = top_support(str(mesh_path))["mesh"].vertices
        world_vertices = base_vertices @ rotation.T + translation
        lower = world_vertices.min(axis=0) - [0.015, 0.015, 0.01]
        upper = world_vertices.max(axis=0) + [0.015, 0.015, 0.03]
        nominal_top = world_vertices[:, 2].max()
        lower[2] = nominal_top - GENERATOR_CONFIG["top_inspection_below_m"]
        upper[2] = nominal_top + GENERATOR_CONFIG["top_inspection_above_m"]
        np.savez_compressed(
            out / "observation.npz",
            rgb=rgb,
            depth=depth.astype(np.float32),
            reference_depth=reference_depth,
            K=intrinsics,
            T_world_camera=extrinsics,
            inspection_bounds_min=lower,
            inspection_bounds_max=upper,
        )
        Image.fromarray(rgb).save(out / "camera_rgb.png")
        Image.fromarray(reference_rgb).save(out / "reference_rgb.png")
        Image.fromarray(overview).save(out / "overview.png")
        renderer.enable_segmentation_rendering()
        renderer.update_scene(
            data, camera="inspection", scene_option=render_options
        )
        segmentation = renderer.render().copy()
        renderer.disable_segmentation_rendering()
        truth = np.zeros(ideal_depth.shape, np.uint8)
        baseline_id = model.geom("cad_surface").id
        is_geom = segmentation[:, :, 1] == int(mujoco.mjtObj.mjOBJ_GEOM)
        truth[is_geom & (segmentation[:, :, 0] == baseline_id)] = 3
        for index, (_, metadata) in enumerate(defects):
            geom_id = model.geom(f"added_defect_{index}").id
            selected = is_geom & (segmentation[:, :, 0] == geom_id)
            truth[selected] = metadata["label"]
            metadata["visible_pixels"] = int(selected.sum())
        evaluator = out / "evaluator"
        evaluator.mkdir()
        np.savez_compressed(
            evaluator / "truth.npz",
            truth=truth,
            ideal_depth=ideal_depth,
            trueK=intrinsics,
            trueT=extrinsics,
        )
        private_metadata = {
            "seed": seed,
            "case": case,
            "added_defects": [item[1] for item in defects],
            "visible_label_pixels": {
                str(label): int((truth == label).sum()) for label in range(5)
            },
            "truth_origin": "rendered visible geometry IDs, after public observation export",
            "base_model_identity": "CAD version identifier only; manufacture state unconfirmed",
        }
        write_json(evaluator / "generator_metadata.json", private_metadata)
        color_table = np.array(
            [
                [30, 35, 45],
                [35, 224, 187],
                [255, 179, 68],
                [105, 125, 145],
                [241, 75, 96],
            ],
            dtype=np.uint8,
        )
        Image.fromarray(color_table[truth]).save(
            evaluator / "truth_preview.png"
        )
        highlighted = rgb.copy()
        for label in (1, 2, 4):
            selected = truth == label
            highlighted[selected] = (
                highlighted[selected] * 0.25 + color_table[label] * 0.75
            ).astype(np.uint8)
        Image.fromarray(highlighted).save(evaluator / "annotated_preview.png")
        for _, metadata in defects:
            if metadata["visible_pixels"] == 0:
                raise ValueError(
                    "An added defect is invisible to the inspection camera"
                )
        metadata = {
            "seed": seed,
            "case": case,
            "sample_directory": str(out),
            "observation": str(out / "observation.npz"),
            "evaluator_truth": str(evaluator / "truth.npz"),
            "noise_mm": noise_mm,
            "dropout": dropout,
            "visible_label_pixels": private_metadata["visible_label_pixels"],
        }
        write_json(out / "sample.json", metadata)
        return metadata
    finally:
        renderer.close()


def generate_dataset(
    output,
    seeds=(7007, 7008),
    cases=CASES,
    mesh_path=DEFAULT_MESH,
    split="development",
):
    """Freeze a CAD generation protocol, then render deterministic sample pairs."""
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    mesh_path = Path(mesh_path).resolve()
    protocol = {
        "split": split,
        "seeds": list(seeds),
        "cases": list(cases),
        "generator_config": GENERATOR_CONFIG,
        "mesh_path": str(mesh_path),
        "mesh_sha256": hashlib.sha256(mesh_path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "synthetic_defects": True,
        "reference_pose_known": True,
        "schema": {
            "public": "observation.npz: rgb/depth/reference_depth/K/T_world_camera/inspection_bounds_min/inspection_bounds_max",
            "evaluator_only": "evaluator/truth.npz: truth/ideal_depth/trueK/trueT",
        },
    }
    write_json(out / "protocol.json", protocol)
    records = []
    for case in cases:
        for seed in seeds:
            sample = render_sample(
                out / f"{case}_seed{seed}", seed, case, mesh_path
            )
            records.append(sample)
            print(json.dumps(sample, ensure_ascii=False), flush=True)
    write_json(
        out / "manifest.json", {"protocol": protocol, "samples": records}
    )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seeds", nargs="+", type=int, default=[7007, 7008])
    parser.add_argument(
        "--cases", nargs="+", choices=CASES, default=list(CASES)
    )
    parser.add_argument("--mesh", default=str(DEFAULT_MESH))
    parser.add_argument(
        "--split", choices=["development", "holdout"], default="development"
    )
    args = parser.parse_args()
    generate_dataset(args.output, args.seeds, args.cases, args.mesh, args.split)


if __name__ == "__main__":
    main()
