"""Audit Cao's released training data and build a bounded casting prior."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import project
from .casting_profiles import BASE_PROFILE, TEAM_PROFILE, load_profile
from .workstation_model import write_json

CAO_REPOSITORY = "https://huggingface.co/datasets/charlesH777/WeldGrind-Dataset"
CAO_REVISION = "f422bf01fe770718a703ef6d4744cf2b9cea0b81"
CAO_ARCHIVE_SHA256 = (
    "30ff4e48f1ebde8a3b74845403ea06df5822def7a2ee01607811b71d888e228a"
)
YANG_DOCUMENT = project.ROOT / (
    "feishu_docs/飞边异常数据合成__docx-BsOsdBM5collgPxMkdAcaywfnZf.md"
)
YANG_UPDATE = project.ROOT / (
    "feishu_docs/飞边缺陷合成__wiki-LcFzw9XUoiiPGrkdFzTc9TT0n5e.md"
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid package-relative path: {relative}")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError(f"Missing or escaping source file: {relative}")
    return target


def distribution(values):
    values = np.asarray(values, float)
    if not len(values):
        return {"count": 0}
    return {
        "count": len(values),
        "min": float(values.min()),
        "p05": float(np.quantile(values, 0.05)),
        "median": float(np.median(values)),
        "p95": float(np.quantile(values, 0.95)),
        "max": float(values.max()),
    }


def collect_training_instances(root):
    """Use the optimizer train manifest, including correctly remapped replay."""
    manifest = package_file(root, "program_inputs/train.jsonl")
    rows = [
        json.loads(line) for line in manifest.read_text().splitlines() if line
    ]
    if len(rows) != 2260:
        raise ValueError("Expected all 2260 released optimizer-training states")
    seen_ids, compact, dimensions, noise_rows = set(), [], [], []
    family_examples = {}
    cohorts = Counter()
    for row in rows:
        identity = row["sample_id"]
        if row.get("optimizer_split") != "train" or identity in seen_ids:
            raise ValueError("Changed train split or duplicate sample")
        seen_ids.add(identity)
        cohorts[row["cohort"]] += 1
        base = f"raw/{row['cohort']}/{row['source_split']}/{identity}"
        metadata_file = package_file(root, f"{base}/metadata.json")
        metadata = json.loads(metadata_file.read_text())
        if (
            metadata["sample_id"] != identity
            or metadata["units"] != "mm"
            or metadata["coordinate_frame"] != "world"
        ):
            raise ValueError("Raw sample identity/frame/unit mismatch")
        scene = metadata["scene"]
        for instance in scene["instances"]:
            if not instance.get("grindable") or instance["height_mm"] <= 0:
                continue
            item = {
                "sample_id": identity,
                "base_id": row["base_id"],
                "optimizer_split": "train",
                "source_split": row["source_split"],
                "cohort": row["cohort"],
                "shape": row["shape"],
                "semantic_code": instance["semantic_code"],
                "height_mm": instance["height_mm"],
                "radii_uv_mm": instance["radii_uv_mm"],
                "profile": instance["profile"],
                "source_metadata": f"{base}/metadata.json",
                "source_metadata_sha256": sha256(metadata_file),
            }
            dimensions.append(item)
            # 602 is a compact additive proxy. Long 502 weld ridges must not
            # redefine casting-flash width or a mold's parting line.
            if row["cohort"] == "new" and instance["semantic_code"] == 602:
                compact.append(item)
        examples = family_examples.setdefault(row["shape"], [])
        if len(examples) < 3:
            examples.append((row, base))
        camera_file = package_file(root, f"{base}/rendered/camera.json")
        camera = json.loads(camera_file.read_text())
        noise_rows.append(
            {
                "sample_id": identity,
                "shape": row["shape"],
                "sigma_mm": camera["depth_noise_sigma_mm"],
            }
        )
    if cohorts != Counter({"old": 1000, "new": 1260}) or not compact:
        raise ValueError("Unexpected training cohorts or no compact prior")
    return {
        "manifest_sha256": sha256(manifest),
        "sample_ids": sorted(seen_ids),
        "train_cohorts": dict(cohorts),
        "compact": compact,
        "all_dimensions": dimensions,
        "family_examples": family_examples,
        "declared_noise": noise_rows,
    }


def measure_training_sensor(root, examples):
    """Measure provided clean/noisy RGB-D pairs, never use held-out captures."""
    measurements = []
    for family, captures in sorted(examples.items()):
        for row, base in captures:
            camera = json.loads(
                package_file(root, f"{base}/rendered/camera.json").read_text()
            )
            depth_file = package_file(root, f"{base}/rendered/depth.npz")
            with np.load(depth_file, allow_pickle=False) as arrays:
                clean, noisy = arrays["depth_mm"], arrays["noisy_depth_mm"]
                clean_valid = np.isfinite(clean) & (clean > 0)
                paired = clean_valid & np.isfinite(noisy) & (noisy > 0)
                differences = noisy[paired].astype(float) - clean[
                    paired
                ].astype(float)
            bias = float(np.median(differences))
            mad_sigma = float(
                np.median(np.abs(differences - bias)) / 0.67448975
            )
            dropout = float(1 - paired.sum() / clean_valid.sum())
            measurements.append(
                {
                    "sample_id": row["sample_id"],
                    "optimizer_split": "train",
                    "family": family,
                    "image_size_wh": camera["image_size_wh"],
                    "declared_sigma_mm": camera["depth_noise_sigma_mm"],
                    "measured_mad_sigma_mm": mad_sigma,
                    "depth_bias_mm": bias,
                    "valid_clean_pixels": int(clean_valid.sum()),
                    "missing_fraction_on_clean_foreground": dropout,
                    "source_sha256": sha256(depth_file),
                }
            )
    return measurements


def make_profile(training, measurements, receipt_sha256):
    settings = deepcopy(load_profile(BASE_PROFILE))
    settings.update(
        schema_version=2,
        profile=TEAM_PROFILE,
        calibration_status="team documents and verified synthetic training data; real 097 scan fit pending",
        real_scan_fitted=False,
    )
    settings["variable_flash"].update(
        height_mm=[0.3, 5.0],
        full_width_mm=[0.6, 1.6],
        hotspots={
            "control_fraction": 0.25,
            "scale": [1.0, 1.4],
            "height_cap_mm": 6.0,
        },
    )
    settings["interrupted_flash"].update(
        height_mm=[0.3, 5.0],
        full_width_mm=[0.6, 1.6],
        hotspots=deepcopy(settings["variable_flash"]["hotspots"]),
    )
    settings["compact_excess_prior"] = {
        "source_kind": "synthetic training metadata",
        "semantic_scope": "602 attached compact additive proxy; no real casting class equivalence",
        "sampling": "one joint height/radii record; no independent marginal sampling",
        "training_instances": training["compact"],
    }
    noise_groups = {}
    for item in measurements:
        noise_groups.setdefault(item["declared_sigma_mm"], []).append(item)
    components = [
        {
            "depth_noise_sigma_mm": float(
                np.median([item["measured_mad_sigma_mm"] for item in group])
            ),
            "dropout_probability": float(
                np.median(
                    [
                        item["missing_fraction_on_clean_foreground"]
                        for item in group
                    ]
                )
            ),
            "weight": len(group) / len(measurements),
            "captures": [item["sample_id"] for item in group],
        }
        for _, group in sorted(noise_groups.items())
    ]
    settings["sensor_prior"] = {
        "source_kind": "Cao synthetic clean/noisy depth pairs; not ALSON calibration",
        "depth_noise_sigma_mm": float(
            np.median([item["measured_mad_sigma_mm"] for item in measurements])
        ),
        "dropout_probability": float(
            np.median(
                [
                    item["missing_fraction_on_clean_foreground"]
                    for item in measurements
                ]
            )
        ),
        "components": components,
        "sampling": "one component per synthetic entity seed, shared across all defect cases",
        "seed_offset": 39147,
        "current_camera_geometry_preserved": True,
        "fit_split": "train",
    }
    settings["dataset_binding"] = {
        "repository": CAO_REPOSITORY,
        "revision": CAO_REVISION,
        "archive_sha256": CAO_ARCHIVE_SHA256,
        "train_manifest_sha256": training["manifest_sha256"],
        "verification_receipt_sha256": receipt_sha256,
        "optimizer_training_samples": len(training["sample_ids"]),
        "compact_training_instances": len(training["compact"]),
        "validation_or_test_used_to_fit": False,
    }
    settings["parameter_sources"] = {
        "flash_height_width_hotspots": {
            "kind": "Yang documented generation settings; not measurements",
            "document": str(YANG_DOCUMENT.relative_to(project.ROOT)),
            "sha256": sha256(YANG_DOCUMENT),
            "bmin_bmax_mm": [0.3, 5.0],
            "half_thickness_mm": [0.3, 0.8],
            "cap_scope": "6 mm exploration bound from Nov19 document; no empirical percentile",
        },
        "compact_height_and_footprint": {
            "kind": "Cao paired synthetic train instances",
            "filter": "cohort=new, optimizer_split=train, semantic_code=602, positive grindable",
        },
        "depth_noise": {
            "kind": "measured differences of provided synthetic depth pairs",
            "captured_samples": len(measurements),
        },
        "curled_and_torn_sheet_dimensions": {
            "kind": "retained v1 assumptions; no measured curl/thickness data",
        },
        "root_placement_length_gaps_appearance": {
            "kind": "retained CAD support and v1 assumptions",
            "true_mold_line_known": False,
        },
    }
    settings["sources"].extend(
        [
            CAO_REPOSITORY,
            str(YANG_DOCUMENT.relative_to(project.ROOT)),
            str(YANG_UPDATE.relative_to(project.ROOT)),
        ]
    )
    return load_profile(settings)


def analyze_dataset(root, output, profile_output):
    root, out = Path(root).resolve(), Path(output).resolve()
    receipt_file = root.parent / "local_verification.json"
    receipt = json.loads(receipt_file.read_text())
    if (
        not receipt.get("passed")
        or not receipt.get("hashes_verified")
        or receipt.get("archive_sha256") != CAO_ARCHIVE_SHA256
    ):
        raise ValueError(
            "Full archive and released file hashes must be verified"
        )
    out.mkdir(parents=True, exist_ok=False)
    training = collect_training_instances(root)
    measurements = measure_training_sensor(root, training["family_examples"])
    settings = make_profile(training, measurements, sha256(receipt_file))
    write_json(profile_output, settings)
    write_json(out / "profile.json", settings)
    summary = {
        "source": settings["dataset_binding"],
        "source_kind": "synthetic",
        "real_097_scan_used": False,
        "trained_model_used": False,
        "training_cohorts": training["train_cohorts"],
        "shape_counts": dict(
            Counter(item["shape"] for item in training["all_dimensions"])
        ),
        "semantic_counts": dict(
            Counter(
                str(item["semantic_code"])
                for item in training["all_dimensions"]
            )
        ),
        "compact_height_mm": distribution(
            [item["height_mm"] for item in training["compact"]]
        ),
        "compact_major_diameter_mm": distribution(
            [2 * max(item["radii_uv_mm"]) for item in training["compact"]]
        ),
        "compact_minor_diameter_mm": distribution(
            [2 * min(item["radii_uv_mm"]) for item in training["compact"]]
        ),
        "sensor_measurements": measurements,
        "profile_file": str(Path(profile_output).resolve()),
        "profile_sha256": sha256(profile_output),
        "held_out_data_used_for_fitting": False,
    }
    write_json(out / "analysis.json", summary)
    write_json(
        out / "calibration_training_ids.json",
        {"optimizer_split": "train", "sample_ids": training["sample_ids"]},
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--profile-output",
        type=Path,
        default=project.ROOT / "config/casting_team_prior_20261121.json",
    )
    args = parser.parse_args()
    analyze_dataset(args.root, args.output, args.profile_output)


if __name__ == "__main__":
    main()
