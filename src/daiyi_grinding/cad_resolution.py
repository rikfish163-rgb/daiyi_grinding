"""Render paired CAD defect observations at two camera resolutions.

The frozen CAD generator is imported without modifying its source. Only image
dimensions and offscreen framebuffer dimensions change in the driver process.
Paired samples represent the same physical CAD pose and defect geometries.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import cad_defects, project

DEFAULT_OUTPUT = project.RUNS / "defect_study_20261118/cad_resolution_dataset"
RESOLUTIONS = {1024: (1024, 768), 1920: (1920, 1440)}
SPLITS = {"development": (8207, 8208), "holdout": tuple(range(8307, 8313))}
FROZEN_GENERATOR_SHA256 = (
    "2005a5b5f22e3f98bdbccad0dd46470c2aaf0f9caf06e985d5ff167b0c42747c"
)


def source_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def effective_resolution(width, height):
    """Override render dimensions in memory and restore the frozen generator."""
    original_config = copy.deepcopy(cad_defects.GENERATOR_CONFIG)
    original_sample_xml = cad_defects.sample_xml

    def sample_xml_with_framebuffer(*args, **kwargs):
        root = ET.fromstring(original_sample_xml(*args, **kwargs))
        framebuffer = root.find("visual/global")
        if framebuffer is None:
            raise ValueError("CAD generator scene has no framebuffer settings")
        framebuffer.set("offwidth", str(width))
        framebuffer.set("offheight", str(height))
        ET.indent(root, space="  ")
        return ET.tostring(root, encoding="unicode")

    cad_defects.GENERATOR_CONFIG["camera_width"] = width
    cad_defects.GENERATOR_CONFIG["camera_height"] = height
    cad_defects.sample_xml = sample_xml_with_framebuffer
    try:
        yield copy.deepcopy(cad_defects.GENERATOR_CONFIG)
    finally:
        cad_defects.GENERATOR_CONFIG.clear()
        cad_defects.GENERATOR_CONFIG.update(original_config)
        cad_defects.sample_xml = original_sample_xml


def generate_split(output, resolution, split):
    """Freeze effective settings before generating one resolution/split group."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    width, height = RESOLUTIONS[resolution]
    generator_source = Path(cad_defects.__file__).resolve()
    generator_hash = source_sha256(generator_source)
    if generator_hash != FROZEN_GENERATOR_SHA256:
        raise ValueError("Frozen CAD generator source has changed")
    driver_hash = source_sha256(__file__)
    with effective_resolution(width, height) as settings:
        protocol = {
            "schema_version": 1,
            "split": split,
            "seeds": list(SPLITS[split]),
            "cases": list(cad_defects.CASES),
            "resolution": [width, height],
            "generator_config": settings,
            "effective_generator_config": settings,
            "generator_sha256": generator_hash,
            "generator_source": str(generator_source),
            "driver_sha256": driver_hash,
            "driver_source": str(Path(__file__).resolve()),
            "mesh_path": str(cad_defects.DEFAULT_MESH),
            "mesh_sha256": source_sha256(cad_defects.DEFAULT_MESH),
            "runtime_overrides": {
                "generator_config_fields": ["camera_width", "camera_height"],
                "scene_xml_fields": [
                    "visual/global@offwidth",
                    "visual/global@offheight",
                ],
            },
            "paired_observation": "same case and seed across resolutions has identical added meshes and workpiece pose",
            "independent_workpieces": False,
            "reference_pose_known": True,
            "synthetic_defects": True,
            "noise_pairing": "same RNG seed but different sensor pixel lattices; arrays are not matched per physical point",
            "evaluation_scope": "pixel detection only; no robot motion",
            "schema": {
                "public": "observation.npz: rgb/depth/reference_depth/K/T_world_camera/inspection_bounds_min/inspection_bounds_max",
                "evaluator_only": "evaluator/truth.npz: truth/ideal_depth/trueK/trueT",
            },
        }
        cad_defects.write_json(output / "protocol.json", protocol)
        records = []
        for case in cad_defects.CASES:
            for seed in SPLITS[split]:
                sample = cad_defects.render_sample(
                    output / f"{case}_seed{seed}", seed, case
                )
                records.append(sample)
                with (output / "samples.jsonl").open(
                    "a", encoding="utf-8"
                ) as log:
                    log.write(json.dumps(sample, ensure_ascii=False) + "\n")
                print(
                    json.dumps(
                        {
                            "resolution": resolution,
                            "split": split,
                            "case": case,
                            "seed": seed,
                            "visible_label_pixels": sample[
                                "visible_label_pixels"
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        cad_defects.write_json(
            output / "manifest.json", {"protocol": protocol, "samples": records}
        )
    if source_sha256(generator_source) != generator_hash:
        raise ValueError("Frozen generator changed during capture")
    return records


def xml_physical_signature(path):
    """Normalize asset paths and framebuffer settings for geometry comparison."""
    root = ET.parse(path).getroot()
    global_settings = root.find("visual/global")
    global_settings.attrib.pop("offwidth", None)
    global_settings.attrib.pop("offheight", None)
    for mesh in root.findall("asset/mesh"):
        mesh.set("file", Path(mesh.attrib["file"]).name)
    return ET.tostring(root, encoding="unicode")


def verify_pairs(output=DEFAULT_OUTPUT):
    """Compare actual mesh files, pose matrices, camera parameters and schemas."""
    output = Path(output).resolve()
    rows = []
    for split, seeds in SPLITS.items():
        low_manifest = json.loads(
            (output / "resolution_1024" / split / "manifest.json").read_text()
        )
        high_manifest = json.loads(
            (output / "resolution_1920" / split / "manifest.json").read_text()
        )
        low_protocol = low_manifest["protocol"]
        high_protocol = high_manifest["protocol"]
        low_settings = copy.deepcopy(low_protocol["effective_generator_config"])
        high_settings = copy.deepcopy(
            high_protocol["effective_generator_config"]
        )
        for key in ("camera_width", "camera_height"):
            low_settings.pop(key)
            high_settings.pop(key)
        if low_settings != high_settings:
            raise ValueError(
                "Paired effective configurations differ beyond dimensions"
            )
        for case in cad_defects.CASES:
            for seed in seeds:
                low_dir = (
                    output / "resolution_1024" / split / f"{case}_seed{seed}"
                )
                high_dir = (
                    output / "resolution_1920" / split / f"{case}_seed{seed}"
                )
                low_meshes = {
                    path.name: source_sha256(path)
                    for path in low_dir.glob("added_defect_*.stl")
                }
                high_meshes = {
                    path.name: source_sha256(path)
                    for path in high_dir.glob("added_defect_*.stl")
                }
                low_xml = ET.parse(low_dir / "scene.xml").getroot()
                high_xml = ET.parse(high_dir / "scene.xml").getroot()
                low_casting = low_xml.find("worldbody/body[@name='casting']")
                high_casting = high_xml.find("worldbody/body[@name='casting']")
                row = {
                    "split": split,
                    "case": case,
                    "seed": seed,
                    "defect_meshes_bitwise_equal": low_meshes == high_meshes,
                    "same_cad_mesh": low_protocol["mesh_sha256"]
                    == high_protocol["mesh_sha256"],
                    "same_source_generator": low_protocol["generator_sha256"]
                    == high_protocol["generator_sha256"],
                    "same_effective_parameters_except_resolution": low_settings
                    == high_settings,
                    "same_all_scene_geometry_and_physics": xml_physical_signature(
                        low_dir / "scene.xml"
                    )
                    == xml_physical_signature(high_dir / "scene.xml"),
                    "same_numeric_pose": all(
                        np.array_equal(
                            np.fromstring(low_casting.attrib[key], sep=" "),
                            np.fromstring(high_casting.attrib[key], sep=" "),
                        )
                        for key in ("pos", "quat")
                    ),
                    "mesh_sha256": low_meshes,
                }
                with (
                    np.load(low_dir / "observation.npz") as low,
                    np.load(high_dir / "observation.npz") as high,
                ):
                    row["same_camera_extrinsics"] = bool(
                        np.array_equal(
                            low["T_world_camera"], high["T_world_camera"]
                        )
                    )
                    row["same_inspection_bounds"] = all(
                        np.array_equal(low[key], high[key])
                        for key in (
                            "inspection_bounds_min",
                            "inspection_bounds_max",
                        )
                    )
                    row["intrinsics_scale_correctly"] = bool(
                        np.allclose(
                            high["K"][[0, 1], [0, 1]],
                            low["K"][[0, 1], [0, 1]] * 1920 / 1024,
                            rtol=0,
                            atol=1e-10,
                        )
                    )
                    row["principal_points_correct"] = bool(
                        np.array_equal(
                            low["K"][:2, 2], [(1024 - 1) / 2, (768 - 1) / 2]
                        )
                        and np.array_equal(
                            high["K"][:2, 2], [(1920 - 1) / 2, (1440 - 1) / 2]
                        )
                    )
                    row["no_truth_in_public_observation"] = not any(
                        {"truth", "segmentation"}.intersection(
                            observation.files
                        )
                        for observation in (low, high)
                    )
                    row["expected_array_shapes"] = bool(
                        low["rgb"].shape == (768, 1024, 3)
                        and high["rgb"].shape == (1440, 1920, 3)
                        and low["depth"].shape
                        == low["reference_depth"].shape
                        == (768, 1024)
                        and high["depth"].shape
                        == high["reference_depth"].shape
                        == (1440, 1920)
                    )
                row["pair_passed"] = all(
                    value for value in row.values() if isinstance(value, bool)
                )
                rows.append(row)
    result = {
        "pair_count": len(rows),
        "observation_count": len(rows) * 2,
        "generator_sha256": source_sha256(cad_defects.__file__),
        "driver_sha256": source_sha256(__file__),
        "all_pairs_passed": all(row["pair_passed"] for row in rows),
        "pairs": rows,
        "interpretation": "56 physical scene states rendered twice; 112 observations are paired, not independent workpieces",
        "detection_evaluated": False,
    }
    cad_defects.write_json(output / "pair_verification.json", result)
    if not result["all_pairs_passed"]:
        raise ValueError(
            "At least one resolution pair failed numeric equality checks"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--resolution", type=int, choices=list(RESOLUTIONS))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if args.verify_only:
        result = verify_pairs(output)
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "pairs"},
                ensure_ascii=False,
            )
        )
        return
    output.mkdir(parents=True, exist_ok=True)
    selected = [args.resolution] if args.resolution else list(RESOLUTIONS)
    for resolution in selected:
        for split in SPLITS:
            generate_split(
                output / f"resolution_{resolution}" / split, resolution, split
            )
    if args.resolution is None:
        verify_pairs(output)


if __name__ == "__main__":
    main()
