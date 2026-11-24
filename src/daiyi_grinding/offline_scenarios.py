"""Configuration-driven offline casting scenarios and changeover rehearsals.

The default command renders a small development suite serially. The full
matrix is planning-only unless a future caller explicitly expands it into
separate, resource-bounded runs. No robot commands or network messages are
produced. CAD 021 and Cao's public training base are interface rehearsals,
not silently renamed workstation changeovers.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import shlex
import shutil
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from runpy import run_path
from xml.etree import ElementTree

import numpy as np

from . import project
from .casting_defects import CASTING_CASES, casting_appearance
from .casting_profiles import PROFILE_FILES, load_profile, sensor_for_profile
from .team_data_reuse import sha256
from .workstation_model import write_json

CONFIG_FILE = project.ROOT / "config/offline_scenarios_20261123.json"
LOCK_FILE = project.RUNS / "enterprise_offline_20261123/mujoco.lock"
CASTING_AUDITOR = project.ROOT / "scripts/verify_casting_synthesis.py"
SPLITS = ("development", "scenario_evaluation")
SCENARIO_FIELDS = (
    "scenario_id",
    "part_id",
    "cad_id",
    "entity_seed",
    "entity_group_id",
    "split",
    "case",
    "casting_profile_key",
    "morphology",
    "size_regime",
    "root_region",
    "view_policy",
    "sensor",
    "occlusion",
    "clamping",
    "expected_failure",
    "source",
)
CHANGEOVER_FIELDS = (
    "part_id",
    "cad_id",
    "cad_path",
    "cad_sha256",
    "units",
    "coordinate_frame",
    "identity_confirmed",
    "manufacturing_stage_confirmed",
    "T_workpiece_gripper_m",
    "fixture_id",
    "gripper_id",
    "collision_proxy_manifest",
    "camera_registration",
    "machining_permission",
)
CHANGEOVER_BINDINGS = (
    "enterprise-confirmed CAD identity, manufacturing stage and datum",
    "registered T_workpiece_gripper in metres and verified clamp repeatability",
    "part-specific gripper, fixture and collision proxy geometry",
    "camera intrinsics, extrinsics and encoder registration for the fixture",
    "approved mold parting roots, machining zones and protected surfaces",
    "tool geometry, TCP, permitted contacts and whole-path collision checks",
    "physical commissioning and independently logged machine occupancy",
)


def _artifact_reference(path):
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def load_config(path=CONFIG_FILE):
    path = Path(path).resolve()
    configuration = json.loads(path.read_text())
    configuration["_config_path"] = str(path)
    return configuration


def repository_path(relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Repository-relative input required: {relative}")
    resolved = (project.ROOT / path).resolve()
    if not resolved.is_relative_to(project.ROOT):
        raise ValueError(f"Input escapes repository: {relative}")
    return resolved


def _split_seeds(configuration):
    registry = configuration["seed_registry"]
    return {split: set(registry[split]) for split in SPLITS}


def validate_seed_registry(configuration):
    registry = configuration["seed_registry"]
    split_seeds = _split_seeds(configuration)
    matrix = configuration["matrix"]
    all_seeds = {
        split: seeds | set(matrix[f"{split}_seeds"])
        for split, seeds in split_seeds.items()
    }
    if all_seeds["development"] & all_seeds["scenario_evaluation"]:
        raise ValueError("A seed cannot occur in both scenario splits")
    reserved = set(registry["excluded_seeds"])
    for split, seeds in all_seeds.items():
        overlap = seeds & reserved
        if overlap:
            raise ValueError(
                f"Historical/regression seeds forbidden in {split}: "
                f"{sorted(overlap)}"
            )
    for name in registry["historical_split_plans"]:
        plan = json.loads(repository_path(name).read_text())
        historical = set()
        for key in ("training_seeds", "validation_seeds", "test_seeds"):
            historical.update(plan.get(key, []))
        for split, seeds in all_seeds.items():
            if seeds & historical:
                raise ValueError(
                    f"{split} reuses an observed training/validation/test seed"
                )
    return {split: sorted(values) for split, values in split_seeds.items()}


def validate_parts(configuration):
    checked = {}
    for key, part in configuration["parts"].items():
        if not part["part_id"] or not part["cad_id"]:
            raise ValueError(f"Explicit part/cad identity required for {key}")
        path = repository_path(part["cad_path"])
        available = path.is_file()
        support = part["main_station_support"]
        if support in {"supported", "metadata_only_reject"} and not available:
            raise ValueError(f"Required CAD is missing: {path}")
        digest = sha256(path) if available else None
        if part.get("cad_sha256") and digest != part["cad_sha256"]:
            raise ValueError(f"CAD hash mismatch for {part['part_id']}")
        checked[key] = {
            **deepcopy(part),
            "cad_available": available,
            "observed_cad_sha256": digest,
        }
    return checked


def materialize_profiles(configuration, output):
    destination = Path(output) / "profiles"
    destination.mkdir(parents=True, exist_ok=True)
    records = {}
    for name, override in configuration["profile_overrides"].items():
        settings = deepcopy(load_profile(override["base_profile"]))
        settings["profile"] = f"enterprise_{name}_20261123"
        for family, changes in override.get("families", {}).items():
            for key, value in changes.items():
                if key == "remove_hotspots":
                    if value:
                        settings[family].pop("hotspots", None)
                else:
                    settings[family][key] = deepcopy(value)
        compact_filter = override.get("compact_filter")
        if compact_filter:
            rows = settings["compact_excess_prior"]["training_instances"]
            rows = [
                row
                for row in rows
                if row["optimizer_split"] == compact_filter["optimizer_split"]
                and row["semantic_code"] == compact_filter["semantic_code"]
                and row["height_mm"] <= compact_filter["height_mm_max"]
                and max(row["radii_uv_mm"])
                <= compact_filter["major_radius_mm_max"]
                and min(row["radii_uv_mm"])
                <= compact_filter["minor_radius_mm_max"]
            ]
            if len(rows) != compact_filter["eligible_training_instances"]:
                raise ValueError(f"Changed compact-prior filter count: {name}")
            settings["compact_excess_prior"]["training_instances"] = rows
        settings = load_profile(settings)
        path = destination / f"{name}.json"
        write_json(path, settings)
        digest = sha256(path)
        expected = override["sha256"]
        if expected and digest != expected:
            raise ValueError(f"Scenario profile hash mismatch: {name}")
        records[name] = {
            "path": path,
            "relative_path": str(path.relative_to(output)),
            "sha256": digest,
            "settings": settings,
        }
    return records


def _base_row(configuration, family, seed, split, profiles):
    part = configuration["parts"]["097"]
    override_name = family.get("profile_override")
    profile_key = (
        f"enterprise_{override_name}_20261123"
        if override_name
        else family["profile"]
    )
    row = {
        key: deepcopy(value)
        for key, value in family.items()
        if key not in {"profile", "profile_override", "sensor_override"}
    }
    row.update(
        row_id=f"{family['scenario_id']}_seed{seed}",
        part_id=part["part_id"],
        cad_id=part["cad_id"],
        cad_path=part["cad_path"],
        cad_sha256=part["cad_sha256"],
        entity_seed=seed,
        entity_group_id=f"synthetic097_seed{seed}",
        split=split,
        casting_profile_key=profile_key,
        profile_path=(
            profiles[override_name]["relative_path"] if override_name else None
        ),
        profile_sha256=(
            profiles[override_name]["sha256"] if override_name else None
        ),
        grasp_setup="original",
        views_deg=[0, 60, 120, 180, 240, 300],
        planned_view_count=6,
        source_kind="synthetic_team_prior_not_real_097_scan",
        trained_model_used=False,
        grinding_dynamics_executed=False,
    )
    if family.get("sensor_override"):
        row["sensor_override"] = deepcopy(family["sensor_override"])
    return row


def validate_rows(configuration, rows):
    validate_seed_registry(configuration)
    identifiers = set()
    groups = {}
    for row in rows:
        missing = [key for key in SCENARIO_FIELDS if key not in row]
        if missing:
            raise ValueError(f"Scenario row is missing {missing}")
        if row["row_id"] in identifiers:
            raise ValueError(f"Duplicate scenario row: {row['row_id']}")
        identifiers.add(row["row_id"])
        if row["split"] not in SPLITS:
            raise ValueError(f"Unknown scenario split: {row['split']}")
        if row["case"] not in CASTING_CASES:
            raise ValueError(f"Unknown casting case: {row['case']}")
        if row["part_id"] != "097" or row["cad_id"] != "097":
            raise ValueError("Renderable scenarios require explicit 097/097")
        group = (row["part_id"], row["cad_id"], row["entity_seed"])
        if row["entity_group_id"] != f"synthetic097_seed{row['entity_seed']}":
            raise ValueError("Entity group must include part/CAD/seed identity")
        previous = groups.setdefault(group, row["split"])
        if previous != row["split"]:
            raise ValueError(
                f"Same entity crosses data splits: {group}: "
                f"{previous} vs {row['split']}"
            )
        allowed = set(configuration["seed_registry"][row["split"]])
        allowed.update(configuration["matrix"][f"{row['split']}_seeds"])
        if row["entity_seed"] not in allowed:
            raise ValueError(
                f"Unregistered scenario seed for {row['split']}: "
                f"{row['entity_seed']}"
            )
        if row["views_deg"] != [0, 60, 120, 180, 240, 300]:
            raise ValueError("Only the registered fixed six views are allowed")
        if row.get("sensor_override"):
            if (
                not 0 < row["sensor_override"]["depth_noise_sigma_mm"] < 2
                or not 0 <= row["sensor_override"]["dropout_probability"] < 0.5
            ):
                raise ValueError("Invalid scenario sensor stress")
    return rows


def build_plan(configuration, output, suite="development"):
    if suite not in SPLITS:
        raise ValueError(f"Unknown suite {suite}")
    profiles = materialize_profiles(configuration, output)
    seed = configuration["seed_registry"][suite][0]
    rows = [
        _base_row(configuration, family, seed, suite, profiles)
        for family in configuration["scenario_families"]
    ]
    return validate_rows(configuration, rows)


def build_full_matrix(configuration, output):
    profiles = materialize_profiles(configuration, output)
    rows = []
    matrix = configuration["matrix"]
    sensors = {
        "team_depth_prior": None,
        "legacy_noisy": {
            "depth_noise_sigma_mm": 0.25,
            "dropout_probability": 0.025,
        },
        "unvalidated_specular_dropout_proxy": {
            "depth_noise_sigma_mm": 0.5,
            "dropout_probability": 0.08,
        },
    }
    split_keys = (
        ("development", "development_seeds"),
        ("scenario_evaluation", "scenario_evaluation_seeds"),
    )
    for split, key in split_keys:
        for seed in matrix[key]:
            for family in configuration["scenario_families"]:
                for clamping in matrix["clamping_setups"]:
                    for sensor in matrix["sensor_stresses"]:
                        row = _base_row(
                            configuration, family, seed, split, profiles
                        )
                        row["clamping"] = clamping
                        row["grasp_setup"] = (
                            "opposite_simulation"
                            if clamping.startswith("opposite")
                            else "original"
                        )
                        row["sensor"] = sensor
                        if sensors[sensor] is not None:
                            row["sensor_override"] = deepcopy(sensors[sensor])
                        else:
                            row.pop("sensor_override", None)
                        row["row_id"] = (
                            f"{family['scenario_id']}_seed{seed}_"
                            f"{row['grasp_setup']}_{sensor}"
                        )
                        row["execution_status"] = "dry_plan_only"
                        rows.append(row)
    return validate_rows(configuration, rows)


def inspect_station_boundary():
    from .workpiece_packages import WORKPIECE_DIRECTORY, load_workpiece_package

    model_file = project.SOURCE / "workstation_model.py"
    inspection_file = project.SOURCE / "workstation_inspection.py"
    packages_file = project.SOURCE / "workpiece_packages.py"
    mesh = (
        project.ROOT
        / "assets/ier220_workstation/meshes/workpiece/wp_097_c0.stl"
    )
    registered = []
    physical_certified = []
    if WORKPIECE_DIRECTORY.is_dir():
        for package_file in sorted(WORKPIECE_DIRECTORY.glob("wp_*.json")):
            workpiece_id = package_file.stem.removeprefix("wp_")
            if len(workpiece_id) == 3 and workpiece_id.isdigit():
                package = load_workpiece_package(workpiece_id)
                registered.append(package.workpiece_id)
                if package.declaration.get("physical_changeover_validated"):
                    physical_certified.append(package.workpiece_id)
    model = model_file.read_text()
    inspection = inspection_file.read_text()
    checks = {
        "registered_workpiece_packages": "097" in registered,
        "dynamic_package_model_loader": "load_workpiece_package" in model,
        "dynamic_coordinate_frame": "self.coordinate_frame" in inspection,
        "physical_changeover_unverified": not physical_certified,
    }
    if not all(checks.values()):
        raise ValueError(
            "Workstation CAD boundary changed; re-audit changeover"
        )
    return {
        "supported_part_cad": registered,
        "simulation_packages": registered,
        "physical_changeover_certified": physical_certified,
        "checks": checks,
        "source_sha256": {
            model_file.name: sha256(model_file),
            inspection_file.name: sha256(inspection_file),
            packages_file.name: sha256(packages_file),
        },
        "cad_097_sha256": sha256(mesh),
        "limitation": (
            "Registered workpiece packages are simulation-only. Real physical "
            "changeover, grasp calibration and fixture validation remain uncertified."
        ),
    }


def import_changeover(manifest):
    """Validate an import request, then enforce the current 097-only boundary."""
    missing = [key for key in CHANGEOVER_FIELDS if key not in manifest]
    if missing:
        return {
            "schema_valid": False,
            "accepted": False,
            "stage": "changeover_manifest",
            "missing_fields": missing,
            "reason": "A new part needs an explicit, complete changeover manifest",
        }
    if manifest["part_id"] == "097" and manifest["cad_id"] != "097":
        raise ValueError("Part/CAD identity mismatch; renaming is forbidden")
    path = Path(manifest["cad_path"])
    if not path.is_absolute():
        path = repository_path(path)
    if not path.is_file() or sha256(path) != manifest["cad_sha256"]:
        return {
            "schema_valid": True,
            "accepted": False,
            "stage": "cad_hash",
            "reason": "CAD file or declared SHA256 does not match",
        }
    transform = np.asarray(manifest["T_workpiece_gripper_m"], float)
    rigid = (
        transform.shape == (4, 4)
        and np.isfinite(transform).all()
        and np.allclose(transform[3], [0, 0, 0, 1])
        and np.allclose(
            transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-6
        )
        and np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-6)
    )
    if manifest["units"] != "m" or not rigid:
        return {
            "schema_valid": True,
            "accepted": False,
            "stage": "metric_transform",
            "reason": "Changeover requires metre units and a rigid 4x4 transform",
        }
    return {
        "schema_valid": True,
        "accepted": False,
        "stage": "main_station_capability",
        "reason": (
            "Current main station is 097-only even when import metadata is "
            "complete; implement and independently validate the CAD/grasp/"
            "fixture/collision/tool bindings before a real changeover"
        ),
        "main_station_executed": False,
    }


def changeover_manifest_template(part):
    """Return an explicit template; unknown changeover bindings stay pending."""
    return {
        "part_id": part["part_id"],
        "cad_id": part["cad_id"],
        "cad_path": part["cad_path"],
        "cad_sha256": part.get("observed_cad_sha256"),
        "units": "m",
        "coordinate_frame": "pending_enterprise_datum",
        "identity_confirmed": False,
        "manufacturing_stage_confirmed": False,
        "T_workpiece_gripper_m": None,
        "fixture_id": "pending",
        "gripper_id": "pending",
        "collision_proxy_manifest": "pending",
        "camera_registration": "pending",
        "machining_permission": "pending",
    }


def rehearse_changeovers(configuration):
    checked = validate_parts(configuration)
    records = []
    for key in ("021", "cao_box_train"):
        part = checked[key]
        manifest = {
            "part_id": part["part_id"],
            "cad_id": part["cad_id"],
            "cad_path": str(repository_path(part["cad_path"])),
        }
        started = time.monotonic()
        result = import_changeover(manifest)
        template = changeover_manifest_template(part)
        template_result = import_changeover(template)
        if result["accepted"] or template_result["accepted"]:
            raise ValueError(f"Unexpected station changeover acceptance: {key}")
        records.append(
            {
                "input_kind": key,
                **deepcopy(part),
                "import_result": result,
                "import_template": template,
                "import_template_result": template_result,
                "unverified_bindings": list(CHANGEOVER_BINDINGS),
                "software_import_check_s": time.monotonic() - started,
                "expected_rejection": True,
                "renamed_to_097": False,
                "main_station_executed": False,
                "new_holdout_claimed": False,
            }
        )
    return {
        "station": inspect_station_boundary(),
        "cases": records,
        "physical_machine_time": "pending",
        "physical_new_part_occupancy": "pending",
        "import_path": (
            "Fill the explicit template, run hash/unit/rigid-transform/schema "
            "checks, then keep rejection until main-station bindings are implemented"
        ),
    }


def _cao_input_paths(part):
    source = repository_path(part["source_metadata"])
    rendered = source.parent / "rendered"
    return {
        "metadata": source,
        "program_input": repository_path(part["program_input"]),
        "camera": rendered / "camera.json",
        "depth": rendered / "depth.npz",
        "rgb": rendered / "rgb.png",
    }


def inspect_cao_public_input(configuration):
    from PIL import Image

    part = configuration["parts"]["cao_box_train"]
    sources = _cao_input_paths(part)
    metadata = json.loads(sources["metadata"].read_text())
    if (
        metadata["base_id"] != "primitive_box_001"
        or metadata["scene"]["split"] != "train"
        or metadata["units"] != "mm"
        or metadata["coordinate_frame"] != "world"
    ):
        raise ValueError("Unexpected Cao train identity, split, units or frame")
    camera = json.loads(sources["camera"].read_text())
    with Image.open(sources["rgb"]) as image:
        rgb = np.asarray(image.convert("RGB"))
    with np.load(sources["depth"], allow_pickle=False) as depths:
        noisy_depth = depths["noisy_depth_mm"]
    with np.load(sources["program_input"], allow_pickle=False) as arrays:
        if (
            str(arrays["source_split"].item()) != "train"
            or str(arrays["optimizer_split"].item()) != "train"
            or str(arrays["units"].item()) != "mm"
            or str(arrays["coordinate_frame"].item()) != "world"
        ):
            raise ValueError("Cao program input provenance mismatch")
        trajectory = arrays["trajectory"]
        points = arrays["points"]
        roi = arrays["roi_mask"]
        xyz = arrays["xyz_camera_cv_mm"]
        uv = arrays["point_uv"]
        intrinsics = arrays["intrinsics"]
        camera_from_world = arrays["camera_from_world"]
        pixel_offset = float(arrays["pixel_center_offset"])
        u, v = uv.T
        if (
            pixel_offset != 0.5
            or pixel_offset != camera["pixel_center_offset"]
            or camera["world_units"] != "mm"
            or not np.array_equal(intrinsics, camera["intrinsics_K"])
            or rgb.shape[:2] != noisy_depth.shape
            or np.any((u < 0) | (u >= rgb.shape[1]))
            or np.any((v < 0) | (v >= rgb.shape[0]))
        ):
            raise ValueError("Cao public capture calibration did not match")
        camera_xyz = np.column_stack(
            [
                (uv[:, 0] + pixel_offset - intrinsics[0, 2])
                * xyz[:, 2]
                / intrinsics[0, 0],
                (uv[:, 1] + pixel_offset - intrinsics[1, 2])
                * xyz[:, 2]
                / intrinsics[1, 1],
                xyz[:, 2],
            ]
        )
        numeric_error = float(np.linalg.norm(xyz - camera_xyz, axis=1).max())
        world_from_camera = np.asarray(camera["world_from_camera_cv_mm"])
        if not np.allclose(
            world_from_camera, np.linalg.inv(camera_from_world), atol=1e-10
        ):
            raise ValueError("Cao camera JSON and program transform disagree")
        world_points = (
            xyz @ world_from_camera[:3, :3].T + world_from_camera[:3, 3]
        )
        frame_error = float(
            np.linalg.norm(points[:, :3] - world_points, axis=1).max()
        )
        if (
            trajectory.shape != (1024, 3)
            or points.ndim != 2
            or points.shape[1] != 6
            or roi.shape != (len(points),)
            or not np.isfinite(trajectory).all()
            or not np.isfinite(points).all()
            or not np.array_equal(xyz[:, 2], noisy_depth[v, u])
            or not np.allclose(points[:, 3:], rgb[v, u] / 255, atol=1e-7)
            or numeric_error > 0.01
            or frame_error > 0.01
        ):
            raise ValueError("Cao public program schema/numeric check failed")
        summary = {
            "point_rows": len(points),
            "roi_points": int(roi.sum()),
            "trajectory_rows": len(trajectory),
            "image_size_wh": list(camera["image_size_wh"]),
            "array_contract": "world-mm XYZ, normalized RGB, ROI mask and 1024-point world-mm trajectory",
            "sampled_noisy_depth_and_rgb_match_raw_files": True,
            "backprojection_numeric_max_mm": numeric_error,
            "world_frame_numeric_max_mm": frame_error,
            "pixel_center_offset": pixel_offset,
            "camera_transform_convention": (
                "The raw JSON world_from_camera_cv_mm maps camera XYZ to "
                "world. It equals the inverse of this release's program "
                "NPZ camera_from_world, and reproduces the exported world XYZ."
            ),
        }
    nominal_path = repository_path(part["cad_path"])
    return {
        "source": "Cao WeldGrind public synthetic train state",
        "source_sample_id": metadata["sample_id"],
        "source_base_id": metadata["base_id"],
        "source_split": "train",
        "source_is_new_holdout": False,
        "inputs": {
            role: _artifact_reference(path) for role, path in sources.items()
        },
        "nominal_mesh_path_declared_by_manifest": part["cad_path"],
        "nominal_mesh_available_in_release": nominal_path.is_file(),
        "nominal_mesh_limitation": (
            "The release retains analytic metadata and program inputs but not "
            "this raw mesh_target.ply; do not synthesize or rename a CAD stand-in"
        ),
        "readable_public_io": summary,
        "main_station_changeover_accepted": False,
        "rejection_reason": part["rejection_reason"],
        "trajectory_sent_to_robot": False,
    }


@contextmanager
def mujoco_lock(path=LOCK_FILE):
    """Serialize all model loading, rendering and turntable integration."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        stream.truncate()
        stream.write(f"pid={os.getpid()} acquired={time.time()}\n")
        stream.flush()
        try:
            yield {"path": str(path), "pid": os.getpid()}
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _settings_for_row(row, output):
    if row.get("profile_path"):
        path = output / row["profile_path"]
        if sha256(path) != row["profile_sha256"]:
            raise ValueError("Scenario profile input hash changed")
    else:
        original = PROFILE_FILES[row["casting_profile_key"]]
        frozen = output / "source_snapshot" / original.name
        path = frozen if frozen.is_file() else original
    settings = load_profile(json.loads(path.read_text()))
    if row.get("sensor_override"):
        sensor = settings["sensor_prior"]
        # A sampled mixture component otherwise supersedes both stress values.
        sensor.pop("components", None)
        sensor.update(row["sensor_override"])
        sensor.update(
            source_kind=row["source"],
            sampling="fixed synthetic parameters shared across this scenario",
            scenario_proxy=row["sensor"],
            real_camera_calibrated=False,
        )
    return settings


def _generation_inputs():
    from .workstation_jobs import JOB_FILE
    from .workstation_model import (
        CELL_CONFIG_FILE,
        SOURCE_FILES,
        SOURCE_SCENE,
        WORKPIECE_MESH,
    )
    from .workstation_reuse import FILTER_MODULES, UPSTREAM

    return {
        *(project.SOURCE / name for name in SOURCE_FILES),
        project.SOURCE / "casting_synthesis.py",
        project.SOURCE / "workstation_replay.py",
        Path(__file__).resolve(),
        CASTING_AUDITOR,
        *PROFILE_FILES.values(),
        CELL_CONFIG_FILE,
        JOB_FILE,
        SOURCE_SCENE,
        WORKPIECE_MESH,
        *(UPSTREAM / "src/weldgrind_v1" / name for name in FILTER_MODULES),
    }


def _snapshot_sources(folder, paths):
    snapshot = folder / "source_snapshot"
    snapshot.mkdir()
    hashes = {}
    for path in sorted(paths):
        destination = snapshot / path.name
        if destination.exists():
            raise ValueError(f"Duplicate snapshot basename: {path.name}")
        shutil.copyfile(path, destination)
        hashes[path.name] = sha256(destination)
    return hashes


def _generate_one(configuration, row, output):
    from .casting_synthesis import LabelledInspection, macro_preview
    from .workstation_model import Workstation

    started = time.monotonic()
    folder = output / "observations" / row["row_id"]
    settings = _settings_for_row(row, output)
    sources = set((output / "source_snapshot").iterdir())
    if row.get("profile_path"):
        sources.add(output / row["profile_path"])
    generation_hashes = _snapshot_sources(folder, sources)
    sim = Workstation(
        folder,
        row["entity_seed"],
        row["case"],
        casting_profile=settings,
        grasp_setup=row["grasp_setup"],
    )
    try:
        inspection = LabelledInspection(sim, row["entity_seed"])
        _, paths, perception = inspection.scan(angles=row["views_deg"])
        objects = []
        for identity, item in enumerate(sim.private_defects, 1):
            macro = folder / "evaluator" / f"object_{identity:02d}_macro.png"
            macro_check = macro_preview(sim, item, macro)
            visible = [
                view["visible_object_pixels"][identity - 1]
                for view in inspection.label_views
            ]
            item["macro_preview"] = str(macro.relative_to(folder))
            item["macro_validation"] = macro_check
            item["visible_pixels_by_view"] = visible
            item["root_points_depth_consistent_some_view"] = int(
                inspection.root_seen[
                    inspection.root_instances == identity
                ].sum()
            )
            write_json(
                folder / "evaluator" / f"object_{identity:02d}.json", item
            )
            objects.append(
                {
                    key: item[key]
                    for key in (
                        "geom",
                        "morphology",
                        "kind",
                        "region",
                        "height_peak_mm",
                        "geometry_validation",
                        "macro_preview",
                        "macro_validation",
                        "visible_pixels_by_view",
                        "root_points_depth_consistent_some_view",
                    )
                }
            )
        protocol = {
            "schema_version": 1,
            "profile": settings,
            "seeds": [row["entity_seed"]],
            "cases": [row["case"]],
            "split": row["split"],
            "scenario_id": row["scenario_id"],
            "entity_group_id": row["entity_group_id"],
            "source_hashes": generation_hashes,
            "real_scans_used": False,
            "trained_model_used": False,
            "grinding_dynamics_executed": False,
            "turntable_servo_integrated": True,
            "normal_CAD_modified": False,
            "part_identity_rule": configuration["seed_registry"]["entity_rule"],
            "public": "fixed-camera RGB-D, reference depth, intrinsics, transforms and integrated encoder",
            "evaluator_only": "semantic/instance masks, complete roots, generator parameters and macro previews",
            "root_visibility_scope": "ideal raster depth consistency, not real-camera root observability",
            "sensor_scenario": row["sensor"],
            "requested_sensor_override": row.get("sensor_override"),
            "sensor_real_camera_calibrated": False,
        }
        record = {key: deepcopy(row[key]) for key in SCENARIO_FIELDS}
        record.update(
            row_id=row["row_id"],
            case=row["case"],
            seed=row["entity_seed"],
            casting_profile=settings["profile"],
            appearance=casting_appearance(row["entity_seed"]),
            synthetic_sensor_prior=sim.sensor_prior,
            sample_directory=".",
            objects=objects,
            view_count=len(inspection.label_views),
            views=inspection.label_views,
            visible_objects=sum(
                max(item["visible_pixels_by_view"]) >= 6 for item in objects
            ),
            root_points=len(inspection.root_points),
            root_points_depth_consistent_some_view=int(
                inspection.root_seen.sum()
            ),
            legacy_detector_metrics=perception["metrics"],
            legacy_predicted_paths=len(paths),
            inference_truth_used=False,
        )
        write_json(folder / "sample.json", record)
        write_json(
            folder / "manifest.json",
            {"protocol": protocol, "samples": [record]},
        )
        return {
            "row_id": row["row_id"],
            "seed": row["entity_seed"],
            "status": "rendered",
            "sample_directory": str(folder.relative_to(output)),
            "manifest": str((folder / "manifest.json").relative_to(output)),
            "seconds": time.monotonic() - started,
            "objects": len(objects),
            "visible_objects": record["visible_objects"],
            "views": record["view_count"],
            "root_points": record["root_points"],
            "depth_consistent_root_points": record[
                "root_points_depth_consistent_some_view"
            ],
            "legacy_detector_f1": perception["metrics"]["f1"],
            "legacy_predicted_paths": len(paths),
            "reproduction_command": (
                "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
                ".venv/bin/python -m daiyi_grinding.offline_scenarios "
                f"--output <new-directory> --suite {row['split']} "
                f"--scenarios {row['scenario_id']}"
            ),
        }
    finally:
        sim.close()


def _audit_sensor_data(folder, row, sample, expected_sensor, public_fields):
    """Check the exported observation against the requested synthetic sensor."""
    if sample["view_count"] != len(row["views_deg"]):
        raise ValueError("Missing fixed-camera views")
    expected_sigma = expected_sensor["depth_noise_sigma_mm"]
    expected_dropout = expected_sensor["dropout_probability"]
    measurements = []
    fixed_camera = None
    for index, angle in enumerate(row["views_deg"]):
        with (
            np.load(folder / "public" / f"view_{index:02d}.npz") as raw,
            np.load(
                folder / "evaluator" / f"view_{index:02d}_labels.npz"
            ) as labels,
        ):
            if set(raw.files) != public_fields:
                raise ValueError(
                    f"Unexpected public RGB-D fields: view {index}"
                )
            ideal = labels["ideal_depth_m"]
            depth = raw["depth"]
            camera = raw["T_world_camera"]
            if fixed_camera is None:
                fixed_camera = camera
            if (
                raw["rgb"].shape != (*depth.shape, 3)
                or depth.shape != ideal.shape
                or ideal.shape != labels["semantic"].shape
                or not np.allclose(camera, fixed_camera, atol=1e-12, rtol=0)
                or not np.allclose(
                    raw["T_world_workpiece"] @ raw["T_workpiece_camera"],
                    camera,
                    atol=1e-10,
                    rtol=0,
                )
                or not np.isclose(
                    raw["encoder_angle_deg"], angle, atol=0.01, rtol=0
                )
            ):
                raise ValueError(
                    f"Invalid image, camera or encoder: view {index}"
                )
            if not np.isclose(
                raw["synthetic_noise_sigma_mm"], expected_sigma
            ) or not np.isclose(
                raw["synthetic_dropout_probability"], expected_dropout
            ):
                raise ValueError(
                    f"Requested sensor stress was not applied: view {index}"
                )
            ideal_valid = np.isfinite(ideal) & (ideal > 0)
            paired = ideal_valid & np.isfinite(depth)
            if not np.any(paired):
                raise ValueError(f"No valid depth pairs: view {index}")
            errors_mm = (depth[paired] - ideal[paired]) * 1000
            sigma = float(
                np.median(np.abs(errors_mm - np.median(errors_mm))) / 0.67448975
            )
            pixel_count = int(ideal_valid.sum())
            dropout = 1 - int(paired.sum()) / pixel_count
            # The sensor applies independent Bernoulli dropout per pixel.
            dropout_uncertainty = 6 * np.sqrt(
                expected_dropout * (1 - expected_dropout) / pixel_count
            )
            if abs(sigma - expected_sigma) >= 0.005:
                raise ValueError(
                    f"Exported noise MAD differs from the request: view {index}"
                )
            if abs(dropout - expected_dropout) > dropout_uncertainty:
                raise ValueError(
                    f"Exported dropout differs from the request: view {index}"
                )
            if row["case"] == "casting_normal" and (
                not set(np.unique(labels["semantic"])).issubset({0, 3})
                or np.any(labels["instance"])
                or len(labels["root_points_workpiece_m"])
            ):
                raise ValueError(
                    f"Defect labels in nominal control: view {index}"
                )
        measurements.append(
            {
                "index": index,
                "ideal_valid_pixels": pixel_count,
                "measured_mad_sigma_mm": sigma,
                "measured_dropout_fraction": dropout,
            }
        )
    return {
        "passed": True,
        "raw_labels_isolated": True,
        "fixed_camera_and_integrated_encoder_verified": True,
        "expected_noise_sigma_mm": expected_sigma,
        "expected_dropout_probability": expected_dropout,
        "views": measurements,
    }


def _audit_normal_geometry(folder, sample, cad_path):
    """Audit a nominal scene with an empty set of macro or root samples."""
    scene = ElementTree.parse(folder / "scene.xml")
    body = scene.find(".//body[@name='workpiece_097']")
    visual = [] if body is None else body.findall("geom[@group='1']")
    meshes = {
        element.get("name"): element.get("file")
        for element in scene.findall(".//asset/mesh")
    }
    if (
        not visual
        or any(meshes.get(item.get("mesh")) != str(cad_path) for item in visual)
        or any(
            item.get("name", "").startswith("added_defect_")
            for item in scene.findall(".//geom")
        )
        or sample["objects"]
        or sample["root_points"]
    ):
        raise ValueError("Nominal scene changed the 097 CAD or added defects")
    false_positives = sample["legacy_detector_metrics"]["fp"]
    paths = sample["legacy_predicted_paths"]
    if false_positives or paths:
        raise ValueError("Normal control produced selected pixels or paths")
    for index in range(sample["view_count"]):
        cloud_file = folder / "public" / f"view_{index:02d}_weldgrind_cloud.npz"
        with np.load(cloud_file, allow_pickle=False) as cloud:
            if len(cloud["points"]) or cloud["units"].item() != "mm":
                raise ValueError(
                    f"Invalid empty normal ROI cloud: view {index}"
                )
    return {
        "passed": True,
        "macro_min_visible_pixels": None,
        "counts": {
            "episodes": 1,
            "views": sample["view_count"],
            "objects": 0,
            "fragments": 0,
            "adapted_clouds_empty": sample["view_count"],
            "normal_false_positive_pixels": false_positives,
            "normal_predicted_paths": paths,
        },
    }


def _validate_episode(row, result, output, auditor):
    folder = output / result["sample_directory"]
    manifest = json.loads((output / result["manifest"]).read_text())
    protocol = manifest["protocol"]
    (sample,) = manifest["samples"]
    expected_sensor = row.get("sensor_override") or sensor_for_profile(
        _settings_for_row(row, output), row["entity_seed"]
    )
    geometry = {"passed": False, "status": "sensor_audit_precondition_failed"}
    try:
        sensor = _audit_sensor_data(
            folder, row, sample, expected_sensor, auditor["PUBLIC_FIELDS"]
        )
    except ValueError as error:
        sensor = {"passed": False, "message": str(error)}
    if sensor["passed"]:
        try:
            if sample["sample_directory"] != ".":
                raise ValueError("Child manifest must name its own directory")
            for name, digest in protocol["source_hashes"].items():
                snapshot = folder / "source_snapshot" / name
                if not snapshot.is_file() or sha256(snapshot) != digest:
                    raise ValueError(
                        f"Invalid frozen generation snapshot: {name}"
                    )
            if protocol["source_hashes"]["wp_097_c0.stl"] != row["cad_sha256"]:
                raise ValueError(
                    "Generated CAD snapshot differs from the registered 097"
                )
            if row["case"] == "casting_normal":
                geometry = _audit_normal_geometry(
                    folder, sample, repository_path(row["cad_path"])
                )
            else:
                if not sample["objects"]:
                    raise ValueError(
                        "No audited solids in the positive scenario"
                    )
                geometry = auditor["audit"](folder, frozen_generation=True)
        except ValueError as error:
            geometry = {"passed": False, "message": str(error)}
    passed = sensor["passed"] and geometry["passed"]
    validation_path = folder / "validation.json"
    write_json(
        validation_path,
        {"passed": passed, "geometry": geometry, "sensor": sensor},
    )
    result.update(
        validation=str(validation_path.relative_to(output)),
        status="generated_and_validated" if passed else "validation_failed",
        sensor_validated=sensor["passed"],
        validation_counts=geometry.get("counts", {}),
    )
    return [
        {"row_id": row["row_id"], "kind": kind, "message": checks["message"]}
        for kind, checks in (("sensor", sensor), ("geometry", geometry))
        if not checks["passed"] and "message" in checks
    ]


def _split_summary(rows):
    counts = defaultdict(lambda: {"scenarios": 0, "entity_groups": set()})
    for row in rows:
        counts[row["split"]]["scenarios"] += 1
        counts[row["split"]]["entity_groups"].add(row["entity_group_id"])
    return {
        split: {
            "scenarios": value["scenarios"],
            "entity_groups": len(value["entity_groups"]),
        }
        for split, value in counts.items()
    }


def _write_report(output, summary):
    lines = [
        "# Enterprise offline scenario run",
        "",
        "This run uses synthetic 097 observations and the existing geometry detector.",
        "",
        f"- Rendered {summary['metrics']['generated_episodes']} of {summary['metrics']['planned_scenarios']} scenarios and {summary['metrics']['generated_frames']} fixed-camera frames.",
        f"- Validated {summary['metrics']['objects']} rooted objects across {summary['metrics']['fragments']} closed fragments.",
        f"- Software errors {len(summary['failures'])}. Validation failures {len(summary['validation_failures'])}.",
        f"- The expansion matrix records {summary['metrics']['full_matrix_dry_plan_rows']} planning rows.",
        f"- Scenario-evaluation registrations awaiting rendering {summary['metrics']['evaluation_registered_not_rendered']}.",
        "",
        "| Scenario | Seed | Objects / visible | Frames | Validation | Legacy F1 | Candidate paths |",
        "|---|---:|---:|---:|---|---:|---:|",
    ]
    for row in summary["results"]:
        f1 = row["legacy_detector_f1"]
        status = row["status"].replace("_", " ")
        lines.append(
            f"| {row['row_id']} | {row['seed']} "
            f"| {row['objects']} / {row['visible_objects']} | {row['views']} "
            f"| {status} | {'—' if f1 is None else f'{f1 * 100:.2f}%'} "
            f"| {row['legacy_predicted_paths']} |"
        )
    (Path(output) / "report.md").write_text("\n".join(lines) + "\n")


def _input_artifacts(configuration, generation_inputs):
    paths = generation_inputs | {
        Path(configuration["_config_path"]),
        project.ROOT / "config/enterprise_requirements_20261123.json",
        project.ROOT / "docs/team_data_reuse_20261121.md",
        project.ROOT / "docs/workstation_full_chain_20261122.md",
        *_cao_input_paths(configuration["parts"]["cao_box_train"]).values(),
    }
    for part in configuration["parts"].values():
        for key in ("cad_path", "source_metadata", "program_input"):
            if part.get(key):
                path = repository_path(part[key])
                if path.is_file():
                    paths.add(path)
    registry = configuration["seed_registry"]
    paths.update(
        repository_path(name) for name in registry["historical_split_plans"]
    )
    paths.update(
        repository_path(name) for name in registry["known_regression_evidence"]
    )
    return [_artifact_reference(path) for path in sorted(paths)]


def run(
    configuration,
    output,
    matrix="small",
    dry_plan=False,
    suite="development",
    scenarios=None,
):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(f"Output must be a new directory: {output}")
    if matrix == "full" and not dry_plan:
        raise ValueError("The full matrix requires explicit --dry-plan")
    output.mkdir(parents=True, exist_ok=False)
    os.environ["MUJOCO_GL"] = "egl"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    config_path = Path(configuration["_config_path"])
    shutil.copyfile(config_path, output / "scenario_config.json")
    checked_parts = validate_parts(configuration)
    if matrix == "full":
        rows = build_full_matrix(configuration, output)
    else:
        rows = build_plan(configuration, output, suite)
    if scenarios:
        unknown = set(scenarios) - {row["scenario_id"] for row in rows}
        if unknown:
            raise ValueError(f"Unknown scenario IDs: {sorted(unknown)}")
        rows = [row for row in rows if row["scenario_id"] in scenarios]
    validate_rows(configuration, rows)
    evaluation_rows = build_plan(configuration, output, "scenario_evaluation")
    evaluation_requested = {
        row["row_id"]
        for row in rows
        if not dry_plan and row["split"] == "scenario_evaluation"
    }
    write_json(
        output / "scenario_manifest.json",
        {
            "schema_version": 1,
            "matrix": matrix,
            "dry_plan": dry_plan,
            "suite": suite,
            "scenarios": rows,
        },
    )
    with (output / "scenario_manifest.csv").open("w", newline="") as stream:
        columns = [
            "row_id",
            *SCENARIO_FIELDS,
            "profile_sha256",
            "grasp_setup",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})
    write_json(
        output / "evaluation_plan.json",
        {
            "status": "render_requested"
            if evaluation_requested
            else "registered_only",
            "warning": configuration["seed_registry"][
                "not_a_new_holdout_claim"
            ],
            "scenarios": evaluation_rows,
        },
    )
    if matrix == "full":
        full_rows = rows
    else:
        full_rows = build_full_matrix(configuration, output)
        write_json(
            output / "full_matrix_dry_plan.json",
            {
                "status": "dry_plan_only",
                "scenario_count": len(full_rows),
                "scenarios": full_rows,
            },
        )
    changeovers = rehearse_changeovers(configuration)
    cao = inspect_cao_public_input(configuration)
    write_json(output / "changeover_rehearsal.json", changeovers)
    write_json(output / "cao_public_interface.json", cao)
    failures = []
    validation_failures = []
    results = []
    lock_record = None
    generation_inputs = _generation_inputs()
    inputs = []
    auditor = None
    if dry_plan:
        inputs = _input_artifacts(configuration, generation_inputs)
    if not dry_plan:
        with mujoco_lock() as lock_record:
            try:
                frozen_hashes = _snapshot_sources(
                    output,
                    generation_inputs | {output / "scenario_config.json"},
                )
                # Load the auditor and its generation dependencies before the
                # first render so every row uses one shared source revision.
                auditor = run_path(
                    str(output / "source_snapshot" / CASTING_AUDITOR.name)
                )
                inputs = _input_artifacts(configuration, generation_inputs)
                changed = [
                    path.name
                    for path in generation_inputs
                    if frozen_hashes[path.name] != sha256(path)
                ]
                if changed:
                    raise ValueError(
                        f"Generation sources changed while freezing: {sorted(changed)}"
                    )
            except Exception as error:
                failures.append(
                    {
                        "stage": "freeze_inputs",
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
            else:
                (output / "observations").mkdir()
                for row in rows:
                    folder = output / "observations" / row["row_id"]
                    folder.mkdir()
                    try:
                        result = _generate_one(configuration, row, output)
                        results.append(result)
                        print(
                            json.dumps(result, ensure_ascii=False), flush=True
                        )
                    except Exception as error:
                        failure = {
                            "row_id": row["row_id"],
                            "stage": "generation",
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        failures.append(failure)
                        print(
                            json.dumps(failure, ensure_ascii=False), flush=True
                        )
        planned = {row["row_id"]: row for row in rows}
        for result in results:
            try:
                validation_failures.extend(
                    _validate_episode(
                        planned[result["row_id"]], result, output, auditor
                    )
                )
            except Exception as error:
                result["status"] = "validation_error"
                failure = {
                    "row_id": result["row_id"],
                    "stage": "validation",
                    "type": type(error).__name__,
                    "message": str(error),
                }
                failures.append(failure)
                print(json.dumps(failure, ensure_ascii=False), flush=True)
    counts = Counter()
    for result in results:
        counts.update(result.get("validation_counts", {}))
    evaluated = {row["row_id"] for row in results} & evaluation_requested
    validated_sensor = [row for row in results if row.get("sensor_validated")]
    summary = {
        "schema": "enterprise-offline-scenario-evidence-v1",
        "task": "offline enterprise scenario and changeover experience library",
        "scope": {
            "offline_simulation_only": True,
            "part_cad_rendered": ["097", "097"],
            "real_artifact_data_used": False,
            "training_executed": False,
            "frozen_weights_used": False,
            "default_configs_overwritten": False,
            "grinding_dynamics_executed": False,
            "robot_commands_sent": False,
            "external_messages_sent": False,
            "cross_cad_generalization_claimed": False,
        },
        "commands": [
            shlex.join([sys.executable, "-m", __spec__.name, *sys.argv[1:]]),
            "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python -m daiyi_grinding.offline_scenarios --output <new-directory>",
            "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python -m daiyi_grinding.offline_scenarios --output <new-plan-directory> --matrix full --dry-plan",
        ],
        "inputs": inputs,
        "parts": checked_parts,
        "metrics": {
            "planned_scenarios": len(rows),
            "generated_episodes": len(results),
            "generated_frames": sum(row["views"] for row in results),
            "full_matrix_dry_plan_rows": len(full_rows),
            "evaluation_registered_not_rendered": len(evaluation_rows)
            - len(evaluated),
            "evaluation_rendered_episodes": len(evaluated),
            "sensor_validated_episodes": len(validated_sensor),
            "sensor_validated_frames": sum(
                row["views"] for row in validated_sensor
            ),
            "geometry_validated_episodes": counts["episodes"],
            "objects": counts["objects"],
            "fragments": counts["fragments"],
            "visible_objects": counts["visible_objects"],
            "root_samples": counts["root_samples"],
            "depth_consistent_root_samples": counts[
                "depth_consistent_root_samples"
            ],
            "normal_false_positive_pixels": counts[
                "normal_false_positive_pixels"
            ],
            "normal_predicted_paths": counts["normal_predicted_paths"],
            "adapted_clouds_executed": counts["adapted_clouds_executed"],
            "adapted_clouds_empty": counts["adapted_clouds_empty"],
            "split_entity_counts": _split_summary(rows),
            "unique_entity_groups": len(
                {row["entity_group_id"] for row in rows}
            ),
        },
        "failures": failures,
        "validation_failures": validation_failures,
        "real_data_pending": configuration["real_data_pending"],
        "seed_registry": configuration["seed_registry"],
        "resource": {
            "mujoco_lock": lock_record,
            "mujoco_processes": 0 if dry_plan else 1,
            "serial_generation": True,
            "torch_loaded": "torch" in sys.modules,
            "environment": {
                key: os.environ.get(key)
                for key in ("MUJOCO_GL", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
            },
        },
        "changeover": changeovers,
        "cao_public_interface": cao,
        "data_limitations": [
            "Only synthetic 097 CAD scenarios were rendered; 021 and Cao public train states were not renamed or inserted into the station.",
            "Yang's real point cloud was not obtained; roots, morphology and dimensions are not measured 097 distributions.",
            "Cao dimensions/noise come from synthetic train data; public train observations are not a new holdout.",
            "Specular/dropout scenarios are unvalidated sensor stresses, not a calibrated reflectance or ALSON model.",
            "Occluded roots remain hidden; ideal depth consistency is not metrology accuracy or machining permission.",
            "Legacy detector scores are diagnostic development observations, not a new holdout or frozen-weight regression.",
        ],
        "results": results,
    }
    summary["passed"] = (
        not failures
        and not validation_failures
        and (dry_plan or counts["episodes"] == len(rows))
    )
    _write_report(output, summary)
    summary["artifacts"] = [
        _artifact_reference(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    write_json(output / "evidence.json", summary)
    if not summary["passed"]:
        raise RuntimeError(
            f"Scenario run logged {len(failures)} software errors and "
            f"{len(validation_failures)} validation failures"
        )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--matrix", choices=("small", "full"), default="small")
    parser.add_argument("--dry-plan", action="store_true")
    parser.add_argument("--suite", choices=SPLITS, default="development")
    parser.add_argument("--scenarios", nargs="+")
    args = parser.parse_args(argv)
    os.environ.setdefault("MUJOCO_GL", "egl")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    try:
        result = run(
            load_config(args.config),
            args.output,
            matrix=args.matrix,
            dry_plan=args.dry_plan,
            suite=args.suite,
            scenarios=args.scenarios,
        )
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps({"output": str(args.output), **result["metrics"]}))


if __name__ == "__main__":
    main()
