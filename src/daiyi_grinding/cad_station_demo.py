"""Capture added casting defects on the existing ESTUN workstation.

The demo records static RGB-D inspection and projected surface traces. Robot
motion and gripper transport are outside this capture experiment. The reference
view uses the same CAD pose with only the added defect meshes hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from . import project
from .cad_defects import camera_axes
from .evaluation import score
from .perception import overlay
from .planning import path_length, plan
from .reference_detection import configuration, detect_reference

DEFAULT_OUTPUT = project.RUNS / "defect_study_20261118/cad_station_demo"
STATION_SOURCE = project.ROOT / "assets/ier220_workstation/scene.xml"
ADDED_SOURCE = (
    project.RUNS
    / "defect_study_20261118/cad_dataset/development/mixed_seed7007"
)


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def build_scene(output):
    """Copy the existing station configuration and attach reused CAD defects."""
    root = ET.parse(STATION_SOURCE).getroot()
    compiler = root.find("compiler")
    original_meshdir = STATION_SOURCE.parent / compiler.attrib.pop("meshdir")
    assets = root.find("asset")
    for mesh in assets.findall("mesh"):
        mesh.set("file", str(original_meshdir / mesh.attrib["file"]))
    workpiece = root.find(".//body[@name='workpiece_097']")
    if workpiece is None:
        raise ValueError("Existing station does not contain CAD version 097")
    defect_files = sorted(ADDED_SOURCE.glob("added_defect_*.stl"))
    if len(defect_files) != 3:
        raise ValueError("Mixed development sample must have three additions")
    for index, source in enumerate(defect_files):
        target = output / source.name
        shutil.copyfile(source, target)
        ET.SubElement(
            assets,
            "mesh",
            name=f"station_added_mesh_{index}",
            file=str(target),
            inertia="shell",
        )
        ET.SubElement(
            workpiece,
            "geom",
            name=f"station_added_defect_{index}",
            type="mesh",
            mesh=f"station_added_mesh_{index}",
            material="wp_097_mat0",
            contype="0",
            conaffinity="0",
            group="4",
        )
    world = root.find("worldbody")
    camera_position = [-0.425, -1.93, 1.55]
    camera_target = [-0.425, -1.80, 0.94]
    ET.SubElement(
        world,
        "camera",
        name="inspection",
        pos=" ".join(map(str, camera_position)),
        xyaxes=camera_axes(camera_position, camera_target),
        fovy="44",
    )
    overview_position = [1.05, -3.6, 3.6]
    overview_target = [-0.6, -0.4, 0.8]
    ET.SubElement(
        world,
        "camera",
        name="inspection_context",
        pos=" ".join(map(str, overview_position)),
        xyaxes=camera_axes(overview_position, overview_target),
        fovy="46",
    )
    ET.indent(root, space="  ")
    scene_file = output / "scene.xml"
    scene_file.write_text(ET.tostring(root, encoding="unicode") + "\n")
    return scene_file, defect_files


def capture(output=DEFAULT_OUTPUT, width=1024, height=768):
    """Render and evaluate one full-workstation static inspection demo."""
    os.environ.setdefault("MUJOCO_GL", "egl")
    import mujoco

    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    scene_file, defect_files = build_scene(out)
    model = mujoco.MjModel.from_xml_path(str(scene_file))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    renderer = mujoco.Renderer(model, height=height, width=width)
    options = mujoco.MjvOption()
    options.sitegroup[:] = 0
    snapshots = out / "source_snapshot"
    snapshots.mkdir()
    for name in (
        "reference_detection.py",
        "planning.py",
        "cad_station_demo.py",
    ):
        shutil.copyfile(project.SOURCE / name, snapshots / name)
    hashes = {
        "original_station": hashlib.sha256(
            STATION_SOURCE.read_bytes()
        ).hexdigest(),
        "addition_files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in defect_files
        },
        "source": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in snapshots.glob("*.py")
        },
    }
    try:
        options.geomgroup[4] = 0
        renderer.update_scene(data, camera="inspection", scene_option=options)
        reference_rgb = renderer.render().copy()
        renderer.enable_depth_rendering()
        renderer.update_scene(data, camera="inspection", scene_option=options)
        reference_depth = renderer.render().copy()
        renderer.disable_depth_rendering()
        options.geomgroup[4] = 1
        renderer.update_scene(data, camera="inspection", scene_option=options)
        rgb = renderer.render().copy()
        renderer.enable_depth_rendering()
        renderer.update_scene(data, camera="inspection", scene_option=options)
        ideal_depth = renderer.render().copy()
        renderer.disable_depth_rendering()
        renderer.update_scene(
            data, camera="inspection_context", scene_option=options
        )
        overview = renderer.render().copy()
        camera_id = model.camera("inspection").id
        focal = 0.5 * height / np.tan(np.deg2rad(model.cam_fovy[camera_id]) / 2)
        intrinsics = np.array(
            [
                [focal, 0, (width - 1) / 2],
                [0, focal, (height - 1) / 2],
                [0, 0, 1.0],
            ]
        )
        extrinsics = np.eye(4)
        extrinsics[:3, :3] = data.cam_xmat[camera_id].reshape(3, 3) @ np.diag(
            [1, -1, -1]
        )
        extrinsics[:3, 3] = data.cam_xpos[camera_id]
        rng = np.random.default_rng(7007 + 15191)
        depth = ideal_depth + rng.normal(0, 0.00006, ideal_depth.shape)
        depth[rng.random(depth.shape) < 0.003] = np.nan
        lower = np.array([-0.658, -2.00, 0.941])
        upper = np.array([-0.195, -1.60, 0.996])
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
        detections, observation = detect_reference(
            rgb,
            depth,
            reference_depth,
            intrinsics,
            extrinsics,
            inspection_bounds=(lower, upper),
        )
        paths = plan(detections, observation, clearance_mm=3.0)
        annotated = overlay(rgb, detections, paths, intrinsics, extrinsics)
        Image.fromarray(annotated).save(out / "detection_paths.png")
        write_json(
            out / "detections.json", [item.summary() for item in detections]
        )
        write_json(out / "paths.json", paths)
        renderer.enable_segmentation_rendering()
        renderer.update_scene(data, camera="inspection", scene_option=options)
        segmentation = renderer.render().copy()
        renderer.disable_segmentation_rendering()
        truth = np.zeros(ideal_depth.shape, np.uint8)
        is_geom = segmentation[:, :, 1] == int(mujoco.mjtObj.mjOBJ_GEOM)
        truth[is_geom & (segmentation[:, :, 0] == model.geom("wp_0").id)] = 3
        for index in range(3):
            geom_id = model.geom(f"station_added_defect_{index}").id
            truth[is_geom & (segmentation[:, :, 0] == geom_id)] = (
                1 if index == 0 else 2
            )
        evaluator = out / "evaluator"
        evaluator.mkdir()
        np.savez_compressed(
            evaluator / "truth.npz",
            truth=truth,
            ideal_depth=ideal_depth,
            trueK=intrinsics,
            trueT=extrinsics,
        )
        metrics = score(
            detections, truth, paths, intrinsics, extrinsics, ideal_depth
        )
        result = {
            "method": "registered_reference",
            "case": "CAD version 097 with synthetic mixed additions",
            "scene_source": str(STATION_SOURCE),
            "defect_source": str(ADDED_SOURCE),
            "source_hashes": hashes,
            "configuration": configuration(),
            "static_capture_only": True,
            "robot_executed": False,
            "material_removal_simulated": False,
            "reference": "same pose and camera, only added defect geometry hidden",
            "camera": "additional configured inspection camera inside the cell",
            "width": width,
            "height": height,
            "original_model_physics_parameters_changed": False,
            "perception": metrics,
            "path_count": len(paths),
            "path_length_mm": path_length(paths) * 1000,
            "path_scope": "surface traces with nominal 3 mm ball-tool footprint; ESTUN carries workpiece and execution adapter remains pending",
            "visible_label_pixels": {
                str(label): int((truth == label).sum()) for label in range(5)
            },
        }
        write_json(evaluator / "score.json", result)
        write_json(out / "result.json", result)
        write_json(
            out / "status.json", {"stage": "complete", "result": "result.json"}
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    finally:
        renderer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=768)
    args = parser.parse_args()
    capture(args.output, args.width, args.height)


if __name__ == "__main__":
    main()
