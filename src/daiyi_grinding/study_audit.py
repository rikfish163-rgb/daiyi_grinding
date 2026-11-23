"""Independently recompute saved defect experiments and execution evidence."""

import argparse
import ast
import csv
import hashlib
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np

DEFAULT_STUDY = (
    Path(__file__).resolve().parents[2] / "runs/defect_study_20261118"
)
OBSERVATION_KEYS = {
    "rgb",
    "depth",
    "reference_depth",
    "K",
    "T_world_camera",
    "inspection_bounds",
}
CAD_OBSERVATION_KEYS = {
    "rgb",
    "depth",
    "reference_depth",
    "K",
    "T_world_camera",
    "inspection_bounds_min",
    "inspection_bounds_max",
}
COUNT_FIELDS = (
    "tp",
    "fp",
    "fn",
    "normal_structure_selected_pixels",
    "unsupported_stump_selected_pixels",
    "selected_pixels",
    "wrong_type_pixels",
)
DETECTOR_FILES = (
    "perception.py",
    "detector_candidates.py",
    "reference_detection.py",
)


def read_json(path):
    """Read a saved JSON artifact without importing the experiment code."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    """Return the digest of the current file bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def array_digest(array):
    """Include array type and dimensions in a content digest."""
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode())
    digest.update(json.dumps(list(array.shape)).encode())
    digest.update(np.ascontiguousarray(array).tobytes())
    return digest.hexdigest()


def validate_masks(prediction, truth):
    """Require two paired integer masks with declared class IDs."""
    if prediction.ndim != 2 or truth.ndim != 2:
        raise ValueError("Prediction and truth must be two-dimensional")
    if prediction.shape != truth.shape:
        raise ValueError("Prediction and truth shapes differ")
    if not np.issubdtype(prediction.dtype, np.integer):
        raise ValueError("Prediction class IDs must be integers")
    if not np.issubdtype(truth.dtype, np.integer):
        raise ValueError("Truth class IDs must be integers")
    if not np.isin(prediction, (0, 1, 2)).all():
        raise ValueError("Prediction contains undeclared class IDs")
    if not np.isin(truth, (0, 1, 2, 3, 4)).all():
        raise ValueError("Truth contains undeclared class IDs")


def pixel_counts(prediction, truth):
    """Count all visible pixels directly, including type and guard errors."""
    prediction = np.asarray(prediction)
    truth = np.asarray(truth)
    validate_masks(prediction, truth)
    selected = prediction != 0
    target = (truth == 1) | (truth == 2)
    true_positive = int(np.count_nonzero(selected & target))
    false_positive = int(np.count_nonzero(selected & ~target))
    false_negative = int(np.count_nonzero(~selected & target))
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "selected_pixels": int(np.count_nonzero(selected)),
        "target_pixels": int(np.count_nonzero(target)),
        "normal_structure_selected_pixels": int(
            np.count_nonzero(selected & (truth == 3))
        ),
        "unsupported_stump_selected_pixels": int(
            np.count_nonzero(selected & (truth == 4))
        ),
        "wrong_type_pixels": int(
            np.count_nonzero(selected & target & (prediction != truth))
        ),
        "precision": (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        ),
        "recall": (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        ),
        "f1": (
            2
            * true_positive
            / (2 * true_positive + false_positive + false_negative)
            if 2 * true_positive + false_positive + false_negative
            else 0.0
        ),
    }


def projected_coverage(truth, ideal_depth, intrinsics, transform, paths):
    """Recompute 3 mm XY coverage with direct distances to saved vertices.

    This deliberately uses neither the experiment's unproject function nor its
    spatial tree. The metric measures saved path vertex footprints, rather than
    material removal or coverage of interpolated continuous tool motion.
    """
    rows, columns = np.nonzero((truth == 1) | (truth == 2))
    if not len(rows):
        return None
    if not paths:
        return 0.0
    depths = np.asarray(ideal_depth)[rows, columns]
    if not np.isfinite(depths).all() or (depths <= 0).any():
        raise ValueError("Invalid ideal depths at evaluated target pixels")
    camera_points = np.column_stack(
        (
            (columns - intrinsics[0, 2]) * depths / intrinsics[0, 0],
            (rows - intrinsics[1, 2]) * depths / intrinsics[1, 1],
            depths,
        )
    )
    world_points = (transform[:3, :3] @ camera_points.T).T + transform[:3, 3]
    vertices = np.concatenate(
        [np.asarray(path["xyz"], dtype=float) for path in paths], axis=0
    )
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        raise ValueError("Saved paths require XYZ vertices")
    if not np.isfinite(vertices).all():
        raise ValueError("Saved path contains nonfinite vertices")
    covered = 0
    for start in range(0, len(world_points), 256):
        coordinates = world_points[start : start + 256, :2]
        squared_distances = np.sum(
            (coordinates[:, None, :] - vertices[None, :, :2]) ** 2,
            axis=2,
        )
        covered += int(
            np.count_nonzero(np.min(squared_distances, axis=1) <= 0.003**2)
        )
    return covered / len(world_points)


def compare_fields(expected, actual, names, context):
    """Return specific mismatches without suppressing missing values."""
    issues = []
    for name in names:
        if name not in actual:
            issues.append(f"{context}: missing {name}")
            continue
        first, second = expected[name], actual[name]
        if first is None or isinstance(first, (str, bool, int)):
            equal = first == second
        elif isinstance(first, float):
            equal = isinstance(second, (float, int)) and np.isclose(
                first, second, atol=1e-9, rtol=1e-9
            )
        else:
            equal = first == second
        if not equal:
            issues.append(
                f"{context}: {name} expected {first!r}, recorded {second!r}"
            )
    return issues


def audit_dataset(study):
    """Check generation hashes, paired inputs and disjoint scene seeds."""
    study = Path(study)
    entries = read_json(study / "coupon_dataset/index.json")
    protocol = read_json(study / "coupon_dataset/generation_protocol.json")
    issues = []
    identities = Counter(entry["id"] for entry in entries)
    repeated_ids = sorted(key for key, count in identities.items() if count > 1)
    if repeated_ids:
        issues.append(f"Duplicate sample IDs: {repeated_ids}")
    seeds_by_split = defaultdict(set)
    observation_hash_splits = defaultdict(set)
    depth_hash_splits = defaultdict(set)
    reference_hash_splits = defaultdict(set)
    target_counts = defaultdict(list)
    for entry in entries:
        sample = study / entry["sample"]
        split = entry["split"]
        seeds_by_split[split].add(entry["seed"])
        if entry["seed"] not in protocol["splits"].get(split, []):
            issues.append(f"{entry['id']}: seed outside recorded split")
        if entry["case"] not in protocol["cases"]:
            issues.append(f"{entry['id']}: undeclared scenario")
        if entry["id"] != f"{split}/{entry['case']}_{entry['seed']}":
            issues.append(f"{entry['id']}: inconsistent sample identity")
        for basename, key in (
            ("observation.npz", "observation_sha256"),
            ("truth.npz", "truth_sha256"),
        ):
            if sha256(sample / basename) != entry[key]:
                issues.append(f"{entry['id']}: {basename} hash changed")
        observation_hash_splits[entry["observation_sha256"]].add(split)
        with np.load(sample / "observation.npz", allow_pickle=False) as public:
            unexpected = sorted(set(public.files) - OBSERVATION_KEYS)
            if unexpected:
                issues.append(
                    f"{entry['id']}: unexpected detector inputs {unexpected}"
                )
            if not {"rgb", "depth", "K", "T_world_camera"}.issubset(
                public.files
            ):
                issues.append(f"{entry['id']}: missing public observation")
            depth_hash_splits[array_digest(public["depth"])].add(split)
            reference_hash_splits[array_digest(public["reference_depth"])].add(
                split
            )
            if public["depth"].shape != public["reference_depth"].shape:
                issues.append(f"{entry['id']}: reference shape mismatch")
            if np.array_equal(
                public["depth"], public["reference_depth"], equal_nan=True
            ):
                issues.append(f"{entry['id']}: observed/reference copies equal")
            observation_shape = public["depth"].shape
        with np.load(sample / "truth.npz", allow_pickle=False) as evaluator:
            truth = evaluator["truth"]
            validate_masks(np.zeros(observation_shape, dtype=np.uint8), truth)
            count = int(np.count_nonzero((truth == 1) | (truth == 2)))
            target_counts[entry["case"]].append(count)
            if entry["case"] == "normal" and count:
                issues.append(f"{entry['id']}: normal has target labels")
            if entry["case"] != "normal" and count == 0:
                issues.append(f"{entry['id']}: defect scenario has no targets")
    split_names = sorted(seeds_by_split)
    for first_index, first in enumerate(split_names):
        for second in split_names[first_index + 1 :]:
            shared_seeds = seeds_by_split[first] & seeds_by_split[second]
            if shared_seeds:
                issues.append(f"Shared split seeds: {sorted(shared_seeds)}")
    duplicate_cross_split = {}
    for label, digests in (
        ("observation_files", observation_hash_splits),
        ("depth_arrays", depth_hash_splits),
        ("reference_arrays", reference_hash_splits),
    ):
        duplicate_cross_split[label] = sum(
            len(splits) > 1 for splits in digests.values()
        )
        if duplicate_cross_split[label]:
            issues.append(f"Cross-split duplicate {label}")
    for split, declared_seeds in protocol["splits"].items():
        expected = {
            f"{split}/{case}_{seed}"
            for case in protocol["cases"]
            for seed in declared_seeds
        }
        observed = {entry["id"] for entry in entries if entry["split"] == split}
        if observed != expected:
            issues.append(f"{split}: missing or additional sample pairs")
    return {
        "samples": len(entries),
        "samples_per_split": dict(Counter(entry["split"] for entry in entries)),
        "independent_base_seeds_per_split": {
            split: len(seeds) for split, seeds in seeds_by_split.items()
        },
        "same_seed_cases_share_nominal_geometry": True,
        "cross_split_duplicate_counts": duplicate_cross_split,
        "target_pixels_by_case": {
            case: {"minimum": min(counts), "maximum": max(counts)}
            for case, counts in target_counts.items()
        },
        "issues": issues,
    }


def replay_prediction(method, observation, shape):
    """Re-run a detector with read-only public arrays and blocked label loads."""
    from .defect_study import predict

    original_load = np.load

    def load_without_labels(path, *args, **kwargs):
        if "truth" in str(path).lower() or "evaluator" in str(path).lower():
            raise RuntimeError("Detector attempted to read evaluation labels")
        return original_load(path, *args, **kwargs)

    copied = {key: value.copy() for key, value in observation.items()}
    for value in copied.values():
        value.setflags(write=False)
    before = {key: array_digest(value) for key, value in copied.items()}
    if "inspection_bounds_min" in copied:
        copied["inspection_bounds"] = np.stack(
            [copied["inspection_bounds_min"], copied["inspection_bounds_max"]]
        )
        copied["inspection_bounds"].setflags(write=False)
    with mock.patch.object(np, "load", side_effect=load_without_labels):
        if method == "thin_reference":
            from .thin_reference import detect_thin_reference

            detections, _ = detect_thin_reference(
                copied["rgb"],
                copied["depth"],
                copied["reference_depth"],
                copied["K"],
                copied["T_world_camera"],
                inspection_bounds=copied["inspection_bounds"],
            )
        else:
            detections, _ = predict(method, copied)
    after = {key: array_digest(copied[key]) for key in before}
    if before != after:
        raise RuntimeError("Detector mutated its shared observation")
    prediction = np.zeros(shape, dtype=np.uint8)
    for detection in detections:
        if detection.kind in ("seam", "burr"):
            prediction[tuple(detection.pixels.T)] = (
                1 if detection.kind == "seam" else 2
            )
    return prediction


def pooled_summary(rows):
    """Aggregate independent counts, with undefined normal-scene rates null."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["method"], row["case"])].append(row)
    result = []
    for (method, case), samples in sorted(grouped.items()):
        counts = {
            field: sum(sample[field] for sample in samples)
            for field in COUNT_FIELDS
        }
        true_positive = counts["tp"]
        false_positive = counts["fp"]
        false_negative = counts["fn"]
        coverage = [
            row["defect_footprint_coverage_3mm"]
            for row in samples
            if row["defect_footprint_coverage_3mm"] is not None
        ]
        result.append(
            {
                "method": method,
                "case": case,
                "samples": len(samples),
                **counts,
                "precision": (
                    true_positive / (true_positive + false_positive)
                    if true_positive + false_positive
                    else None
                ),
                "recall": (
                    true_positive / (true_positive + false_negative)
                    if true_positive + false_negative
                    else None
                ),
                "f1": (
                    2
                    * true_positive
                    / (2 * true_positive + false_positive + false_negative)
                    if true_positive + false_negative
                    else None
                ),
                "no_action_samples": sum(
                    row["auto_regions"] == 0 for row in samples
                ),
                "review_samples": sum(
                    row["review_regions"] > 0 for row in samples
                ),
                "mean_coverage_3mm": (
                    sum(coverage) / len(coverage) if coverage else None
                ),
                "requires_reference": method
                in ("cad_reference", "thin_reference"),
            }
        )
    return result


def audit_coupon_split(study, split, replay=False):
    """Verify every prediction, metric row, paired record and pooled total."""
    study = Path(study)
    comparison = study / "coupon_comparison" / split
    recorded_summary = read_json(comparison / "summary.json")
    protocol = read_json(comparison / "protocol.json")
    methods = protocol["methods"]
    entries = [
        entry
        for entry in read_json(study / "coupon_dataset/index.json")
        if entry["split"] == split
    ]
    issues = []
    rows = []
    hashes_by_id = defaultdict(set)
    snapshot = comparison / "source_snapshot"
    for filename, digest in protocol["source_hashes"].items():
        if sha256(snapshot / filename) != digest:
            issues.append(f"{split}: frozen {filename} hash changed")
    forbidden_imports = []
    for filename in DETECTOR_FILES:
        syntax = ast.parse((snapshot / filename).read_text())
        for node in ast.walk(syntax):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module.split(".")[-1] in {
                    "evaluation",
                    "geometry",
                    "sim",
                    "cad_defects",
                }:
                    forbidden_imports.append(f"{filename}: {module}")
    if forbidden_imports:
        issues.append(f"Detector imports labels/generator: {forbidden_imports}")
    for entry in entries:
        sample = study / entry["sample"]
        with np.load(sample / "truth.npz", allow_pickle=False) as evaluator:
            truth = evaluator["truth"].copy()
            ideal_depth = evaluator["ideal_depth"].copy()
            intrinsics = evaluator["K"].copy()
            transform = evaluator["T_world_camera"].copy()
        observation = None
        if replay:
            with np.load(
                sample / "observation.npz", allow_pickle=False
            ) as data:
                observation = {name: data[name] for name in data.files}
        for method in methods:
            destination = (
                comparison / method / f"{entry['case']}_{entry['seed']}"
            )
            recorded = read_json(destination / "metrics.json")
            with np.load(
                destination / "prediction.npz", allow_pickle=False
            ) as data:
                prediction = data["pred"]
            independently = pixel_counts(prediction, truth)
            detections = read_json(destination / "detections.json")
            paths = read_json(destination / "paths.json")
            independently.update(
                id=entry["id"],
                split=split,
                case=entry["case"],
                seed=entry["seed"],
                method=method,
                auto_regions=sum(
                    detection["kind"] in ("seam", "burr")
                    for detection in detections
                ),
                review_regions=sum(
                    detection["kind"] == "review" for detection in detections
                ),
                path_count=len(paths),
                defect_footprint_coverage_3mm=projected_coverage(
                    truth, ideal_depth, intrinsics, transform, paths
                ),
            )
            fields = tuple(independently.keys() - {"target_pixels"})
            issues.extend(
                compare_fields(
                    independently, recorded, fields, f"{entry['id']} {method}"
                )
            )
            if method == "threshold" and paths:
                issues.append(f"{entry['id']}: threshold has undeclared paths")
            if replay:
                reproduced = replay_prediction(method, observation, truth.shape)
                if not np.array_equal(reproduced, prediction):
                    issues.append(f"{entry['id']} {method}: replay mismatch")
            hashes_by_id[entry["id"]].add(sha256(sample / "observation.npz"))
            rows.append(independently)
    recorded_rows = [
        json.loads(line)
        for line in (comparison / "episodes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    keyed_rows = {(row["id"], row["method"]): row for row in recorded_rows}
    expected_pairs = {
        (entry["id"], method) for entry in entries for method in methods
    }
    if (
        len(recorded_rows) != len(expected_pairs)
        or set(keyed_rows) != expected_pairs
    ):
        issues.append(f"{split}: duplicate/missing/unpaired episode records")
    for row in rows:
        recorded = keyed_rows.get((row["id"], row["method"]), {})
        issues.extend(
            compare_fields(
                row,
                recorded,
                tuple(row.keys() - {"target_pixels"}),
                f"episodes {row['id']} {row['method']}",
            )
        )
    summaries = pooled_summary(rows)
    stored_groups = {
        (row["method"], row["case"]): row for row in recorded_summary["summary"]
    }
    if len(stored_groups) != len(summaries):
        issues.append(f"{split}: summary group count mismatch")
    for row in summaries:
        stored = stored_groups.get((row["method"], row["case"]), {})
        compared_names = tuple(
            row.keys() - {"selected_pixels", "wrong_type_pixels"}
        )
        issues.extend(
            compare_fields(
                row,
                stored,
                compared_names,
                f"summary {split}/{row['method']}/{row['case']}",
            )
        )
    if recorded_summary["completed"] != len(rows):
        issues.append(f"{split}: completed record count mismatch")
    return {
        "split": split,
        "samples": len(entries),
        "methods": methods,
        "recomputed_prediction_records": len(rows),
        "paired_observations_per_sample": all(
            len(hashes) == 1 for hashes in hashes_by_id.values()
        ),
        "prediction_replay_performed": replay,
        "detector_forbidden_imports": forbidden_imports,
        "summary": summaries,
        "issues": issues,
    }


def audit_cad_dataset(dataset):
    """Check the actual CAD observation schema, labels, poses and manifest."""
    dataset = Path(dataset)
    manifest = read_json(dataset / "manifest.json")
    protocol = read_json(dataset / "protocol.json")
    entries = manifest["samples"]
    issues = []
    if manifest["protocol"] != protocol:
        issues.append("CAD manifest protocol differs from generation protocol")
    expected = {
        (case, seed) for case in protocol["cases"] for seed in protocol["seeds"]
    }
    observed = Counter((row["case"], row["seed"]) for row in entries)
    if set(observed) != expected or any(
        count != 1 for count in observed.values()
    ):
        issues.append("CAD manifest has duplicate/missing/undeclared samples")
    mesh_path = Path(protocol["mesh_path"])
    if sha256(mesh_path) != protocol["mesh_sha256"]:
        issues.append("CAD mesh differs from the recorded generation mesh")
    generator = Path(__file__).parent / "cad_defects.py"
    if sha256(generator) != protocol["generator_sha256"]:
        issues.append("CAD generator differs from the recorded frozen source")
    if "driver_sha256" in protocol:
        if sha256(protocol["driver_source"]) != protocol["driver_sha256"]:
            issues.append("CAD resolution driver differs from frozen source")
    files = []
    pixel_counts_by_case = defaultdict(list)
    depths = {}
    references = {}
    for entry in entries:
        sample = dataset / f"{entry['case']}_seed{entry['seed']}"
        if Path(entry["sample_directory"]).resolve() != sample.resolve():
            issues.append(f"{sample.name}: sample directory differs from ID")
        public_path = sample / "observation.npz"
        truth_path = sample / "evaluator/truth.npz"
        metadata = read_json(sample / "evaluator/generator_metadata.json")
        with np.load(public_path, allow_pickle=False) as public:
            if set(public.files) != CAD_OBSERVATION_KEYS:
                issues.append(f"{sample.name}: unexpected public input keys")
            shape = public["depth"].shape
            if (
                public["rgb"].shape != (*shape, 3)
                or public["reference_depth"].shape != shape
            ):
                issues.append(f"{sample.name}: observation shapes do not match")
            bounds = np.stack(
                [
                    public["inspection_bounds_min"],
                    public["inspection_bounds_max"],
                ]
            )
            if not np.isfinite(bounds).all() or (bounds[0] >= bounds[1]).any():
                issues.append(f"{sample.name}: invalid inspection bounds")
            transform = public["T_world_camera"]
            if not np.allclose(
                transform[:3, :3].T @ transform[:3, :3], np.eye(3)
            ):
                issues.append(f"{sample.name}: nonrigid camera rotation")
            if not np.isclose(np.linalg.det(transform[:3, :3]), 1.0):
                issues.append(f"{sample.name}: camera rotation reverses axes")
            depths[sample.name] = array_digest(public["depth"])
            references[sample.name] = array_digest(public["reference_depth"])
            with np.load(truth_path, allow_pickle=False) as evaluator:
                truth = evaluator["truth"]
                validate_masks(np.zeros(shape, dtype=np.uint8), truth)
                target = (truth == 1) | (truth == 2)
                count = int(target.sum())
                pixel_counts_by_case[entry["case"]].append(count)
                counts = {
                    str(label): int(np.count_nonzero(truth == label))
                    for label in range(5)
                }
                if counts != entry["visible_label_pixels"]:
                    issues.append(
                        f"{sample.name}: manifest label counts changed"
                    )
                if counts != metadata["visible_label_pixels"]:
                    issues.append(
                        f"{sample.name}: private label counts changed"
                    )
                if entry["case"] == "normal" and count:
                    issues.append(
                        f"{sample.name}: normal includes target labels"
                    )
                if entry["case"] != "normal" and not count:
                    issues.append(
                        f"{sample.name}: defect scenario has no targets"
                    )
                if not np.array_equal(evaluator["trueK"], public["K"]):
                    issues.append(f"{sample.name}: evaluator intrinsics differ")
                if not np.array_equal(evaluator["trueT"], transform):
                    issues.append(f"{sample.name}: evaluator extrinsics differ")
                if target.any():
                    differences = (
                        public["reference_depth"][target]
                        - evaluator["ideal_depth"][target]
                    )
                    if float(np.median(differences)) <= 0:
                        issues.append(
                            f"{sample.name}: targets lack positive depth excess"
                        )
        files.append(
            {
                "sample": sample.name,
                "observation_sha256_current": sha256(public_path),
                "truth_sha256_current": sha256(truth_path),
            }
        )
    return {
        "dataset": str(dataset),
        "split": protocol["split"],
        "samples": len(entries),
        "independent_base_seeds": len(protocol["seeds"]),
        "seeds": protocol["seeds"],
        "generator_sha256": protocol["generator_sha256"],
        "mesh_sha256": protocol["mesh_sha256"],
        "label_3_meaning": "all visible nominal CAD surface, not an annotated normal-rib subset",
        "target_pixels_by_case": {
            case: {"minimum": min(values), "maximum": max(values)}
            for case, values in pixel_counts_by_case.items()
        },
        "current_file_hashes": files,
        "depth_array_digests": depths,
        "reference_array_digests": references,
        "hash_evidence_limit": "prior per-observation acquisition hashes were not saved; model and generator hashes are checked",
        "issues": issues,
    }


def audit_cad_comparison(dataset, comparison, replay=False, plain_ids=False):
    """Independently verify every CAD prediction and pooled summary row."""
    dataset, comparison = Path(dataset), Path(comparison)
    manifest = read_json(dataset / "manifest.json")
    recorded_summary = read_json(comparison / "summary.json")
    protocol = read_json(comparison / "protocol.json")
    split = manifest["protocol"]["split"]
    methods = protocol["methods"]
    entries = manifest["samples"]
    issues = []
    if protocol["dataset_protocol"] != manifest["protocol"]:
        issues.append(f"{comparison}: comparison dataset protocol changed")
    if list(recorded_summary["methods"]) != list(methods):
        issues.append(f"{comparison}: summary declares different methods")
    snapshot = comparison / "source_snapshot"
    for filename, digest in protocol["source_hashes"].items():
        if sha256(snapshot / filename) != digest:
            issues.append(f"{comparison}: frozen {filename} hash changed")
    checked_detector_files = [
        name for name in DETECTOR_FILES if name in protocol["source_hashes"]
    ]
    if "thin_reference" in methods:
        checked_detector_files.append("thin_reference.py")
    forbidden_imports = []
    for filename in checked_detector_files:
        syntax = ast.parse((snapshot / filename).read_text())
        for node in ast.walk(syntax):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                if module.split(".")[-1] in {
                    "evaluation",
                    "geometry",
                    "sim",
                    "cad_defects",
                }:
                    forbidden_imports.append(f"{filename}: {module}")
    if forbidden_imports:
        issues.append(
            f"CAD detector imports labels/generator: {forbidden_imports}"
        )
    integrity = read_json(comparison / "source_integrity.json")
    for filename in checked_detector_files:
        if (
            integrity["source_hashes_after"].get(filename)
            != protocol["source_hashes"][filename]
        ):
            issues.append(
                f"{comparison}: detector changed during evaluation: {filename}"
            )
        if (
            replay
            and sha256(Path(__file__).parent / filename)
            != protocol["source_hashes"][filename]
        ):
            issues.append(f"{comparison}: replay detector differs: {filename}")
    rows = []
    hashes_by_id = defaultdict(set)
    for entry in entries:
        sample = dataset / f"{entry['case']}_seed{entry['seed']}"
        sample_id = f"{entry['case']}_{entry['seed']}"
        record_id = sample_id if plain_ids else f"{split}/{sample_id}"
        with np.load(
            sample / "evaluator/truth.npz", allow_pickle=False
        ) as evaluator:
            truth = evaluator["truth"].copy()
            ideal_depth = evaluator["ideal_depth"].copy()
            intrinsics = evaluator["trueK"].copy()
            transform = evaluator["trueT"].copy()
        observation = None
        if replay:
            with np.load(
                sample / "observation.npz", allow_pickle=False
            ) as public:
                observation = {name: public[name] for name in public.files}
        for method in methods:
            destination = comparison / method / sample_id
            recorded = read_json(destination / "metrics.json")
            with np.load(
                destination / "prediction.npz", allow_pickle=False
            ) as predicted:
                prediction = predicted["pred"]
            independently = pixel_counts(prediction, truth)
            detections = read_json(destination / "detections.json")
            paths = read_json(destination / "paths.json")
            independently.update(
                id=record_id,
                split=split,
                case=entry["case"],
                seed=entry["seed"],
                method=method,
                auto_regions=sum(
                    region["kind"] in ("seam", "burr") for region in detections
                ),
                review_regions=sum(
                    region["kind"] == "review" for region in detections
                ),
                path_count=len(paths),
                defect_footprint_coverage_3mm=projected_coverage(
                    truth, ideal_depth, intrinsics, transform, paths
                ),
            )
            if plain_ids:
                independently["resolution"] = recorded_summary["resolution"]
            issues.extend(
                compare_fields(
                    independently,
                    recorded,
                    tuple(independently.keys() - {"target_pixels"}),
                    f"CAD {record_id} {method}",
                )
            )
            if method == "threshold" and paths:
                issues.append(
                    f"CAD {record_id}: threshold has undeclared paths"
                )
            if replay:
                reproduced = replay_prediction(method, observation, truth.shape)
                if not np.array_equal(reproduced, prediction):
                    issues.append(f"CAD {record_id} {method}: replay mismatch")
            hashes_by_id[record_id].add(sha256(sample / "observation.npz"))
            rows.append(independently)
    recorded_rows = [
        json.loads(line)
        for line in (comparison / "episodes.jsonl").read_text().splitlines()
        if line.strip()
    ]
    keyed_rows = {(row["id"], row["method"]): row for row in recorded_rows}
    expected_pairs = {(row["id"], row["method"]) for row in rows}
    if (
        len(recorded_rows) != len(expected_pairs)
        or set(keyed_rows) != expected_pairs
    ):
        issues.append(f"{comparison}: duplicate/missing/unpaired episode rows")
    for row in rows:
        issues.extend(
            compare_fields(
                row,
                keyed_rows.get((row["id"], row["method"]), {}),
                tuple(row.keys() - {"target_pixels"}),
                f"CAD episodes {row['id']} {row['method']}",
            )
        )
    summaries = pooled_summary(rows)
    stored = {
        (row["method"], row["case"]): row for row in recorded_summary["summary"]
    }
    if len(stored) != len(summaries) or len(stored) != len(
        recorded_summary["summary"]
    ):
        issues.append(f"{comparison}: duplicate/missing summary groups")
    for row in summaries:
        issues.extend(
            compare_fields(
                row,
                stored.get((row["method"], row["case"]), {}),
                tuple(row.keys() - {"selected_pixels", "wrong_type_pixels"}),
                f"CAD summary {row['method']}/{row['case']}",
            )
        )
    if recorded_summary["completed"] != len(rows):
        issues.append(f"{comparison}: completed record count differs")
    return {
        "comparison": str(comparison),
        "split": split,
        "samples": len(entries),
        "methods": methods,
        "recomputed_prediction_records": len(rows),
        "prediction_replay_performed": replay,
        "paired_observations_per_sample": all(
            len(hashes) == 1 for hashes in hashes_by_id.values()
        ),
        "detector_forbidden_imports": forbidden_imports,
        "label_3_meaning": "all visible original CAD surface",
        "summary": summaries,
        "issues": issues,
    }


def audit_cad_split(study, split, replay=False):
    """Audit one original CAD split using its independent dataset and masks."""
    study = Path(study)
    dataset = study / "cad_dataset" / split
    return {
        "dataset": audit_cad_dataset(dataset),
        "comparison": audit_cad_comparison(
            dataset, study / "cad_comparison" / split, replay
        ),
    }


def audit_resolution_pairs(study):
    """Check actual paired files and camera geometry across resolutions."""
    root = Path(study) / "cad_resolution_dataset"
    rows = []
    issues = []
    for split in ("development", "holdout"):
        low_protocol = read_json(
            root / "resolution_1024" / split / "protocol.json"
        )
        high_protocol = read_json(
            root / "resolution_1920" / split / "protocol.json"
        )
        low_settings = dict(low_protocol["effective_generator_config"])
        high_settings = dict(high_protocol["effective_generator_config"])
        for key in ("camera_width", "camera_height"):
            low_settings.pop(key)
            high_settings.pop(key)
        if low_settings != high_settings:
            issues.append(
                "Resolution protocols change geometry beyond image dimensions"
            )
        if set(low_protocol["seeds"]) != set(high_protocol["seeds"]):
            issues.append("Resolution protocols declare different scene seeds")
        for case in low_protocol["cases"]:
            for seed in low_protocol["seeds"]:
                first = root / "resolution_1024" / split / f"{case}_seed{seed}"
                second = root / "resolution_1920" / split / f"{case}_seed{seed}"
                first_meshes = {
                    path.name: sha256(path)
                    for path in first.glob("added_defect_*.stl")
                }
                second_meshes = {
                    path.name: sha256(path)
                    for path in second.glob("added_defect_*.stl")
                }
                signature = []
                for directory in (first, second):
                    xml = ET.parse(directory / "scene.xml").getroot()
                    framebuffer = xml.find("visual/global")
                    framebuffer.attrib.pop("offwidth", None)
                    framebuffer.attrib.pop("offheight", None)
                    for mesh in xml.findall("asset/mesh"):
                        mesh.set("file", Path(mesh.attrib["file"]).name)
                    signature.append(ET.tostring(xml, encoding="unicode"))
                with (
                    np.load(first / "observation.npz") as low,
                    np.load(second / "observation.npz") as high,
                ):
                    checks = {
                        "same_defect_mesh_bytes": first_meshes == second_meshes,
                        "same_scene_physics_geometry": signature[0]
                        == signature[1],
                        "same_camera_transform": np.array_equal(
                            low["T_world_camera"], high["T_world_camera"]
                        ),
                        "same_bounds": all(
                            np.array_equal(low[key], high[key])
                            for key in (
                                "inspection_bounds_min",
                                "inspection_bounds_max",
                            )
                        ),
                        "correct_focal_scale": np.allclose(
                            high["K"][[0, 1], [0, 1]],
                            low["K"][[0, 1], [0, 1]] * (1920 / 1024),
                            atol=1e-10,
                            rtol=0,
                        ),
                        "correct_principal_points": np.array_equal(
                            low["K"][:2, 2], [511.5, 383.5]
                        )
                        and np.array_equal(high["K"][:2, 2], [959.5, 719.5]),
                        "correct_image_shapes": low["depth"].shape
                        == (768, 1024)
                        and high["depth"].shape == (1440, 1920),
                        "no_label_fields_in_detector_input": set(low.files)
                        == set(high.files)
                        == CAD_OBSERVATION_KEYS,
                    }
                row = {
                    "split": split,
                    "case": case,
                    "seed": seed,
                    "checks": {
                        key: bool(value) for key, value in checks.items()
                    },
                }
                failed = [key for key, value in checks.items() if not value]
                if failed:
                    issues.append(
                        f"{split}/{case}_{seed}: resolution pair failed {failed}"
                    )
                rows.append(row)
    return {
        "scene_state_pairs": len(rows),
        "observation_count": 2 * len(rows),
        "same_scene_rendered_twice": True,
        "not_independent_workpieces": True,
        "pair_checks": rows,
        "issues": issues,
    }


def audit_refinement_selection(study, refinement):
    """Recompute the reference method choice from development pixels only."""
    study = Path(study)
    chosen_path = study / "cad_refinement/selection.json"
    chosen = read_json(chosen_path)
    methods = defaultdict(list)
    issues = []
    for resolution, splits in refinement.items():
        for row in splits["development"]["comparison"]["summary"]:
            if row["method"] != "geometry":
                methods[row["method"]].append(row)
        development = read_json(
            study / "cad_refinement" / resolution / "development/protocol.json"
        )
        holdout = read_json(
            study / "cad_refinement" / resolution / "holdout/protocol.json"
        )
        for field in (
            "reference_configuration",
            "thin_reference_configuration",
        ):
            if development[field] != holdout[field]:
                issues.append(
                    f"{resolution}: detector configuration changed before holdout"
                )
        for filename in (
            "perception.py",
            "reference_detection.py",
            "thin_reference.py",
            "planning.py",
        ):
            if (
                development["source_hashes"][filename]
                != holdout["source_hashes"][filename]
            ):
                issues.append(
                    f"{resolution}: relevant source changed before holdout: {filename}"
                )
        holdout_time = datetime.fromisoformat(
            holdout["frozen_before_evaluation_utc"]
        ).timestamp()
        choice_time = datetime.fromisoformat(chosen["selected_utc"]).timestamp()
        if (
            chosen_path.stat().st_mtime > holdout_time
            or choice_time > holdout_time
        ):
            issues.append(
                f"{resolution}: reference selection postdates holdout"
            )
    for filename, digest in chosen["development_summary_sha256"].items():
        if sha256(filename) != digest:
            issues.append(
                f"Reference selection uses changed development summary: {filename}"
            )
    ranked = []
    for method, records in methods.items():
        normal = [row for row in records if row["case"] == "normal"]
        thin = [row for row in records if row["case"] == "thin"]
        coarse = [row for row in records if row["case"] == "coarse"]
        true_positive = sum(row["tp"] for row in thin)
        false_positive = sum(row["fp"] for row in thin)
        false_negative = sum(row["fn"] for row in thin)
        eligible = all(
            row["no_action_samples"] == row["samples"]
            and row["review_samples"] == 0
            for row in normal
        ) and all(
            row["unsupported_stump_selected_pixels"] == 0 for row in coarse
        )
        ranked.append(
            {
                "method": method,
                "eligible": eligible,
                "pooled_thin_f1": 2
                * true_positive
                / max(2 * true_positive + false_positive + false_negative, 1),
                "pooled_thin_recall": true_positive
                / max(true_positive + false_negative, 1),
            }
        )
    candidates = [row for row in ranked if row["eligible"]]
    selected = (
        max(candidates, key=lambda row: row["pooled_thin_f1"])["method"]
        if candidates
        else None
    )
    if selected != chosen["selected_reference_method"]:
        issues.append(
            "Reference method differs from independent development ranking"
        )
    recorded_rank = {row["method"]: row for row in chosen["ranked"]}
    for row in ranked:
        issues.extend(
            compare_fields(
                row,
                recorded_rank.get(row["method"], {}),
                tuple(row),
                "Reference selection",
            )
        )
    return {
        "selected_reference_method_recomputed": selected,
        "ranked": ranked,
        "pooled_pixel_weighting": "same scene at two resolutions; high-resolution view contributes more pixels",
        "issues": issues,
    }


def render_saved_inspection(directory, geom_labels, defect_group=2):
    """Independently re-render stored scene depth and visible geometry labels."""
    os.environ.setdefault("MUJOCO_GL", "egl")
    import mujoco

    directory = Path(directory)
    with np.load(directory / "observation.npz") as public:
        height, width = public["depth"].shape
    model = mujoco.MjModel.from_xml_path(str(directory / "scene.xml"))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    options = mujoco.MjvOption()
    options.sitegroup[:] = 0
    renderer = mujoco.Renderer(model, height=height, width=width)
    try:
        options.geomgroup[defect_group] = 0
        renderer.enable_depth_rendering()
        renderer.update_scene(data, camera="inspection", scene_option=options)
        reference = renderer.render().copy()
        options.geomgroup[defect_group] = 1
        renderer.update_scene(data, camera="inspection", scene_option=options)
        ideal = renderer.render().copy()
        renderer.disable_depth_rendering()
        renderer.enable_segmentation_rendering()
        renderer.update_scene(data, camera="inspection", scene_option=options)
        segmentation = renderer.render().copy()
        truth = np.zeros((height, width), dtype=np.uint8)
        for name, label in geom_labels.items():
            selected = (segmentation[..., 0] == model.geom(name).id) & (
                segmentation[..., 1] == int(mujoco.mjtObj.mjOBJ_GEOM)
            )
            truth[selected] = label
        return reference, ideal, truth
    finally:
        renderer.close()


def audit_saved_station(directory, rerender=True):
    """Verify actual full-station geometry, replayed detections and metrics."""
    import mujoco

    directory = Path(directory)
    result = read_json(directory / "result.json")
    issues = []
    original_path = Path(result["scene_source"])
    original_xml = ET.parse(original_path).getroot()
    rendered_xml = ET.parse(directory / "scene.xml").getroot()
    if sha256(original_path) != result["source_hashes"]["original_station"]:
        issues.append("Station original XML changed since capture")
    original_model = mujoco.MjModel.from_xml_path(str(original_path))
    scene_model = mujoco.MjModel.from_xml_path(str(directory / "scene.xml"))
    compiled_properties = (
        "jnt_type",
        "jnt_pos",
        "jnt_axis",
        "jnt_range",
        "body_mass",
        "body_inertia",
        "body_ipos",
        "body_iquat",
        "dof_damping",
        "dof_armature",
        "actuator_gainprm",
        "actuator_biasprm",
        "actuator_forcerange",
    )
    equal_physics = all(
        np.array_equal(
            getattr(original_model, field), getattr(scene_model, field)
        )
        for field in compiled_properties
    )
    if not equal_physics:
        issues.append("Station compiled robot physics differs from original")
    original_body = original_xml.find(".//body[@name='workpiece_097']")
    captured_body = rendered_xml.find(".//body[@name='workpiece_097']")
    same_workpiece_pose = original_body.attrib == captured_body.attrib
    if not same_workpiece_pose:
        issues.append("Station workpiece pose differs from the original")
    original_geoms = {
        item.attrib["name"]: item.attrib
        for item in original_xml.findall(".//geom")
        if "name" in item.attrib
    }
    captured_geoms = {
        item.attrib["name"]: item.attrib
        for item in rendered_xml.findall(".//geom")
        if "name" in item.attrib
    }
    retained_geoms = all(
        captured_geoms.get(name) == attributes
        for name, attributes in original_geoms.items()
    )
    if not retained_geoms:
        issues.append("Station changes original visual/collision geometries")
    for filename, digest in result["source_hashes"]["source"].items():
        if sha256(directory / "source_snapshot" / filename) != digest:
            issues.append(f"Station frozen source differs: {filename}")
    for filename, digest in result["source_hashes"]["addition_files"].items():
        if (
            sha256(directory / filename) != digest
            or sha256(Path(result["defect_source"]) / filename) != digest
        ):
            issues.append(f"Station defect mesh changed: {filename}")
    with np.load(directory / "observation.npz") as saved:
        public = {key: saved[key] for key in saved.files}
    if set(public) != CAD_OBSERVATION_KEYS:
        issues.append("Station public observation contains unexpected fields")
    with np.load(directory / "evaluator/truth.npz") as saved:
        evaluator = {key: saved[key] for key in saved.files}
    truth = evaluator["truth"]
    prediction = replay_prediction("cad_reference", public, truth.shape)
    independently = pixel_counts(prediction, truth)
    paths = read_json(directory / "paths.json")
    from .reference_detection import configuration, detect_reference

    detectors, _ = detect_reference(
        public["rgb"],
        public["depth"],
        public["reference_depth"],
        public["K"],
        public["T_world_camera"],
        inspection_bounds=np.stack(
            [public["inspection_bounds_min"], public["inspection_bounds_max"]]
        ),
    )
    if result["configuration"] != configuration():
        issues.append("Station replay uses different reference configuration")
    if [region.summary() for region in detectors] != read_json(
        directory / "detections.json"
    ):
        issues.append("Station detections differ on replay")
    independently.update(
        auto_regions=sum(
            region.kind in ("seam", "burr") for region in detectors
        ),
        review_regions=sum(region.kind == "review" for region in detectors),
        defect_footprint_coverage_3mm=projected_coverage(
            truth,
            evaluator["ideal_depth"],
            evaluator["trueK"],
            evaluator["trueT"],
            paths,
        ),
    )
    issues.extend(
        compare_fields(
            independently,
            result["perception"],
            tuple(independently.keys() - {"target_pixels"}),
            "Station perception",
        )
    )
    labels = {str(label): int((truth == label).sum()) for label in range(5)}
    if labels != result["visible_label_pixels"]:
        issues.append("Station saved visible label counts differ")
    length_mm = (
        sum(
            float(
                np.linalg.norm(
                    np.diff(np.asarray(path["xyz"]), axis=0), axis=1
                ).sum()
            )
            for path in paths
        )
        * 1000
    )
    issues.extend(
        compare_fields(
            {"path_count": len(paths), "path_length_mm": length_mm},
            result,
            ("path_count", "path_length_mm"),
            "Station paths",
        )
    )
    recreated = {}
    if rerender:
        reference, ideal, rendered_truth = render_saved_inspection(
            directory,
            {
                "wp_0": 3,
                "station_added_defect_0": 1,
                "station_added_defect_1": 2,
                "station_added_defect_2": 2,
            },
            defect_group=4,
        )
        recreated = {
            "same_pose_reference_depth_reproduced": bool(
                np.allclose(
                    reference, public["reference_depth"], atol=1e-6, rtol=0
                )
            ),
            "ideal_depth_reproduced": bool(
                np.allclose(ideal, evaluator["ideal_depth"], atol=1e-6, rtol=0)
            ),
            "visible_truth_reproduced": bool(
                np.array_equal(rendered_truth, truth)
            ),
        }
        for field, matched in recreated.items():
            if not matched:
                issues.append(f"Station re-render does not reproduce {field}")
    return {
        "directory": str(directory),
        "equal_compiled_robot_physics": equal_physics,
        "equal_workpiece_pose": same_workpiece_pose,
        "original_geometries_retained": retained_geoms,
        "reference_depth": "same-pose rendered nominal CAD, ideal camera assumed",
        "nominal_surface_selected_pixels": independently[
            "normal_structure_selected_pixels"
        ],
        "label_3_meaning": "all original visible CAD surface",
        "recomputed_perception": independently,
        "recomputed_path_length_mm": length_mm,
        "static_capture_consistent": result["static_capture_only"]
        and not result["robot_executed"],
        "rerender": recreated,
        "issues": issues,
    }


def independent_selection(study, development_audit):
    """Recompute the selected method without calling the selection code."""
    study = Path(study)
    selection = read_json(study / "selection.json")
    by_method = defaultdict(dict)
    for row in development_audit["summary"]:
        by_method[row["method"]][row["case"]] = row
    ranked = []
    for method, cases in by_method.items():
        normal = cases["normal"]
        eligible = (
            method not in ("threshold", "cad_reference")
            and all(
                row["normal_structure_selected_pixels"] == 0
                for row in cases.values()
            )
            and normal["no_action_samples"] == normal["samples"]
            and normal["review_samples"] == 0
            and cases["coarse"]["unsupported_stump_selected_pixels"] == 0
        )
        ranked.append(
            {
                "method": method,
                "eligible": eligible,
                "thin_recall": cases["thin"]["recall"],
                "thin_f1": cases["thin"]["f1"],
                "mixed_f1": cases["mixed"]["f1"],
                "reference_required": method == "cad_reference",
            }
        )
    eligible = [row for row in ranked if row["eligible"]]
    selected = (
        max(
            eligible,
            key=lambda row: (
                row["thin_f1"],
                row["thin_recall"],
                row["mixed_f1"],
            ),
        )["method"]
        if eligible
        else None
    )
    issues = []
    if selection["chosen_on_split"] != "development":
        issues.append("Selected method does not declare development split")
    if selection["selected_observation_method"] != selected:
        issues.append("Selected method differs from independent dev ranking")
    recorded_ranked = {row["method"]: row for row in selection["ranked"]}
    for row in ranked:
        issues.extend(
            compare_fields(
                row,
                recorded_ranked.get(row["method"], {}),
                tuple(row),
                f"selection {row['method']}",
            )
        )
    holdout_protocol_path = study / "coupon_comparison/holdout/protocol.json"
    created_before_holdout = None
    if holdout_protocol_path.exists():
        holdout = read_json(holdout_protocol_path)
        evaluation_time = datetime.fromisoformat(
            holdout["frozen_before_evaluation_utc"]
        ).timestamp()
        created_before_holdout = (
            study / "selection.json"
        ).stat().st_mtime <= evaluation_time
        if not created_before_holdout:
            issues.append("Selection file was modified after holdout started")
        development = read_json(
            study / "coupon_comparison/development/protocol.json"
        )
        for key in ("candidate_configurations", "reference_configuration"):
            if development[key] != holdout[key]:
                issues.append(f"Configs changed before holdout: {key}")
        for filename in DETECTOR_FILES:
            if (
                development["source_hashes"][filename]
                != holdout["source_hashes"][filename]
            ):
                issues.append(
                    f"Detector source changed before holdout: {filename}"
                )
    return {
        "recomputed_selected_observation_method": selected,
        "ranked": ranked,
        "selection_file_predates_holdout": created_before_holdout,
        "chronology_evidence": "saved selection file mtime versus protocol UTC",
        "issues": issues,
    }


def audit_default_detector(study):
    """Compare current default seed-7 decisions to the frozen baseline."""
    from .perception import detect

    study = Path(study)
    baseline_path = study / "frozen_baseline/src/daiyi_grinding/perception.py"
    name = "_daiyi_frozen_perception_audit"
    specification = importlib.util.spec_from_file_location(name, baseline_path)
    baseline = importlib.util.module_from_spec(specification)
    sys.modules[name] = baseline
    specification.loader.exec_module(baseline)
    with np.load(study / "reproduction/observation.npz") as data:
        inputs = [data[key] for key in ("rgb", "depth", "K", "T_world_camera")]
    expected, _ = baseline.detect(*inputs)
    actual, _ = detect(*inputs)
    matches = len(expected) == len(actual) and all(
        first.summary() == second.summary()
        and np.array_equal(first.pixels, second.pixels)
        and np.array_equal(first.xyz, second.xyz)
        for first, second in zip(expected, actual)
    )
    return {
        "default_geometry_matches_frozen_seed7": bool(matches),
        "detections": len(actual),
        "reproduced_detection_metrics": read_json(
            study / "reproduction/result.json"
        )["perception"],
        "issues": [] if matches else ["Default geometry decisions changed"],
    }


def audit_execution(run_directory, study):
    """Verify saved feedback, planned commands, masks and all video frames."""
    import imageio_ffmpeg
    from PIL import Image

    run_directory, study = Path(run_directory), Path(study)
    result = read_json(run_directory / "result.json")
    execution = read_json(run_directory / "execution.json")
    issues = []
    with (run_directory / "execution.csv").open() as stream:
        feedback = list(csv.DictReader(stream))
    if not feedback:
        raise ValueError("Execution has no saved feedback")
    times = np.array([float(row["time_s"]) for row in feedback])
    actual = np.array(
        [[float(row[f"actual_{axis}_m"]) for axis in "xyz"] for row in feedback]
    )
    target = np.array(
        [[float(row[f"target_{axis}_m"]) for axis in "xyz"] for row in feedback]
    )
    saved_errors = np.array([float(row["error_mm"]) for row in feedback])
    recomputed_errors = np.linalg.norm(actual - target, axis=1) * 1000
    trace = np.array([row["phase"] == "trace" for row in feedback])
    if not np.isfinite(np.column_stack((times, actual, target))).all():
        issues.append("Execution feedback contains nonfinite values")
    if not np.all(np.diff(times) > 0):
        issues.append("Execution feedback timestamps are not increasing")
    if not np.allclose(recomputed_errors, saved_errors, atol=1e-8, rtol=1e-8):
        issues.append("Recorded error differs from actual-target distance")
    if not trace.any():
        issues.append("Execution has no trace feedback")
        independently = {}
    else:
        independently = {
            "tracking_trace_rms_mm": float(
                np.sqrt(np.mean(recomputed_errors[trace] ** 2))
            ),
            "tracking_trace_p95_mm": float(
                np.quantile(recomputed_errors[trace], 0.95)
            ),
            "tracking_trace_max_mm": float(recomputed_errors[trace].max()),
        }
        issues.extend(
            compare_fields(
                independently, execution, tuple(independently), "execution"
            )
        )
    if result["execution"] != execution:
        issues.append("Result execution differs from execution.json")
    expected_feedback_end = (
        execution["simulated_seconds"]
        - (execution["simulation_steps"] % 5)
        * execution["contact_sampling_ms"]
        / 1000
    )
    if not np.isclose(times[-1], expected_feedback_end, atol=1e-8):
        issues.append("Feedback end time differs from simulated duration")
    with np.load(run_directory / "joint_commands.npz") as commands:
        command_time = commands["time_s"]
        if not np.all(np.diff(command_time) > 0):
            issues.append("Planned command timestamps are not increasing")
        if not all(np.isfinite(commands[key]).all() for key in commands.files):
            issues.append("Planned commands contain nonfinite values")
        if len(command_time) != result["planning"]["sample_count"]:
            issues.append("Planned command count differs from result")
    paths = read_json(run_directory / "paths.json")
    length_mm = sum(
        float(
            np.linalg.norm(
                np.diff(np.asarray(path["xyz"]), axis=0), axis=1
            ).sum()
        )
        * 1000
        for path in paths
    )
    issues.extend(
        compare_fields(
            {"path_count": len(paths), "path_length_mm": length_mm},
            result,
            ("path_count", "path_length_mm"),
            "execution paths",
        )
    )
    expected_gate = (
        execution["unexpected_contact_count"] == 0
        and execution["abrasive_contact_steps"] == 0
        and execution["tracking_trace_p95_mm"] < 1.0
        and execution["tracking_trace_max_mm"] < 2.0
        and execution["tracking_trace_orientation_p95_deg"] < 1.0
    )
    if result["motion_gate_passed"] != expected_gate or not expected_gate:
        issues.append("Execution motion gate is not passed consistently")
    method, case, seed = result["method"], result["case"], result["seed"]
    sample = study / "coupon_dataset/holdout" / f"{case}_{seed}"
    comparison = study / "coupon_comparison/holdout" / method / f"{case}_{seed}"
    if sample.exists() and comparison.exists():
        if sha256(run_directory / "observation.npz") != sha256(
            sample / "observation.npz"
        ):
            issues.append("Execution observation differs from paired sample")
        with np.load(comparison / "prediction.npz") as predicted:
            prediction = predicted["pred"]
        with np.load(sample / "truth.npz") as evaluator:
            independently_perception = pixel_counts(
                prediction, evaluator["truth"]
            )
        issues.extend(
            compare_fields(
                independently_perception,
                result["perception"],
                (*COUNT_FIELDS, "precision", "recall", "f1"),
                "execution perception",
            )
        )
        if read_json(run_directory / "detections.json") != read_json(
            comparison / "detections.json"
        ):
            issues.append(
                "Execution detections differ from holdout predictions"
            )
    video = imageio_ffmpeg.read_frames(str(run_directory / "demo.mp4"))
    metadata = next(video)
    frame_count = 0
    last_frame = None
    for frame in video:
        frame_count += 1
        last_frame = frame
    if not frame_count or last_frame is None:
        issues.append("Execution video has no decoded frames")
        last_frame_path = None
    else:
        width, height = metadata["size"]
        last_frame_path = run_directory / "audited_last_frame.png"
        Image.fromarray(
            np.frombuffer(last_frame, dtype=np.uint8).reshape(height, width, 3)
        ).save(last_frame_path)
        video_duration = frame_count / metadata["fps"]
        if abs(video_duration - execution["simulated_seconds"]) > 0.2:
            issues.append("Decoded video duration differs from simulation")
    return {
        "run": str(run_directory),
        "method": method,
        "case": case,
        "seed": seed,
        "feedback_rows": len(feedback),
        "trace_rows": int(trace.sum()),
        "recomputed_tracking": independently,
        "path_count": len(paths),
        "path_length_mm": length_mm,
        "video_fully_decoded_frames": frame_count,
        "video_size": list(metadata["size"]),
        "video_fps": metadata["fps"],
        "last_frame": str(last_frame_path) if last_frame_path else None,
        "motion_gate_passed": expected_gate,
        "orientation_recomputed_from_feedback": False,
        "orientation_note": "CSV contains XYZ and no actual rotations",
        "issues": issues,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    parser.add_argument("--replay-predictions", action="store_true")
    parser.add_argument("--execution", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cad-only", action="store_true")
    args = parser.parse_args()
    dataset = audit_dataset(args.study) if not args.cad_only else None
    splits = {}
    if not args.cad_only:
        for split in ("development", "holdout"):
            if (
                args.study / "coupon_comparison" / split / "summary.json"
            ).exists():
                splits[split] = audit_coupon_split(
                    args.study, split, args.replay_predictions
                )
    selection = None
    if "development" in splits and (args.study / "selection.json").exists():
        selection = independent_selection(args.study, splits["development"])
    default_detector = (
        audit_default_detector(args.study) if not args.cad_only else None
    )
    executions = [audit_execution(path, args.study) for path in args.execution]
    issues = []
    if dataset:
        issues.extend(dataset["issues"])
    if default_detector:
        issues.extend(default_detector["issues"])
    for audited in splits.values():
        issues.extend(audited["issues"])
    if selection:
        issues.extend(selection["issues"])
    for execution in executions:
        issues.extend(execution["issues"])
    cad_splits = {}
    for split in ("development", "holdout"):
        if (args.study / "cad_comparison" / split / "summary.json").exists():
            cad_splits[split] = audit_cad_split(
                args.study, split, args.replay_predictions
            )
            issues.extend(cad_splits[split]["dataset"]["issues"])
            issues.extend(cad_splits[split]["comparison"]["issues"])
    if set(cad_splits) == {"development", "holdout"}:
        first, second = [
            cad_splits[split]["dataset"] for split in ("development", "holdout")
        ]
        if set(first["seeds"]) & set(second["seeds"]):
            issues.append("CAD development and holdout reuse generation seeds")
        for field in ("depth_array_digests", "reference_array_digests"):
            if set(first[field].values()) & set(second[field].values()):
                issues.append(f"CAD development/holdout has copied {field}")
    stations = {}
    for name in ("cad_station_demo", "cad_station_demo_highres"):
        if (args.study / name / "result.json").exists():
            stations[name] = audit_saved_station(args.study / name)
            issues.extend(stations[name]["issues"])
    refinement = {}
    refinement_selection = None
    pairs = None
    refinement_requested = (args.study / "cad_refinement").exists()
    if refinement_requested:
        for resolution in ("resolution_1024", "resolution_1920"):
            refined_splits = {}
            for split in ("development", "holdout"):
                comparison = args.study / "cad_refinement" / resolution / split
                if (comparison / "summary.json").exists():
                    cad_dataset = (
                        args.study
                        / "cad_resolution_dataset"
                        / resolution
                        / split
                    )
                    refined_splits[split] = {
                        "dataset": audit_cad_dataset(cad_dataset),
                        "comparison": audit_cad_comparison(
                            cad_dataset,
                            comparison,
                            args.replay_predictions,
                            plain_ids=True,
                        ),
                    }
                    issues.extend(refined_splits[split]["dataset"]["issues"])
                    issues.extend(refined_splits[split]["comparison"]["issues"])
            refinement[resolution] = refined_splits
        if all(
            set(refined_splits) == {"development", "holdout"}
            for refined_splits in refinement.values()
        ):
            pairs = audit_resolution_pairs(args.study)
            issues.extend(pairs["issues"])
            refinement_selection = audit_refinement_selection(
                args.study, refinement
            )
            issues.extend(refinement_selection["issues"])
    incomplete = (
        (
            not args.cad_only
            and (set(splits) != {"development", "holdout"} or selection is None)
        )
        or set(cad_splits) != {"development", "holdout"}
        or (refinement_requested and refinement_selection is None)
    )
    audit = {
        "audited_utc": datetime.now(timezone.utc).isoformat(),
        "independent_auditor_sha256": sha256(Path(__file__)),
        "comparison_complete": not incomplete,
        "passed": not issues and not incomplete,
        "dataset": dataset,
        "default_detector": default_detector,
        "splits": splits,
        "selection": selection,
        "executions": executions,
        "cad_splits": cad_splits,
        "station_captures": stations,
        "cad_refinement": refinement,
        "cad_refinement_selection": refinement_selection,
        "resolution_pairs": pairs,
        "issues": issues,
        "limitations": [
            "CAD reference assumes exact pose registration and an extra input",
            "Repeated seed cases share the same synthetic nominal workpiece",
            "Selection chronology verified from file metadata and protocol UTC",
            "CSV cannot independently reconstruct orientation/contact events",
            "CAD label 3 represents all visible nominal surface",
            "Prior CAD observations lack acquisition-time hashes; replay and manifests verify current evidence",
            "Resolution pairs share physical scene states and are not independent workpieces",
        ],
    }
    output = args.output or args.study / "independent_audit.json"
    output.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": audit["passed"],
                "comparison_complete": audit["comparison_complete"],
                "prediction_records": sum(
                    split["recomputed_prediction_records"]
                    for split in splits.values()
                )
                + sum(
                    split["comparison"]["recomputed_prediction_records"]
                    for split in cad_splits.values()
                )
                + sum(
                    split["comparison"]["recomputed_prediction_records"]
                    for refined_splits in refinement.values()
                    for split in refined_splits.values()
                ),
                "issues": issues,
            },
            ensure_ascii=False,
        )
    )
    if issues:
        raise SystemExit(1)
    if incomplete:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
