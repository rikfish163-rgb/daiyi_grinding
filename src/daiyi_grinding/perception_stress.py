"""Offline perception stress validation for the 097 casting-detection chain.

The suite reuses archived synthetic RGB-D as *known regression* inputs (it is
never presented as a new holdout), runs the same observation through the
geometric, learned and hybrid candidate interfaces, and scores them against
the archived evaluator labels. Labels reach scoring and annotation crops
only; they never reach any detector, candidate or path decision.

All distances inside this module are metres unless a name or field explicitly
ends in ``_mm``. The enterprise ±0.1 mm root-localization target is reported
as *pending real measurement*: synthetic root diagnostics are produced but are
never presented as real localization evidence.

Entry point for the root unified runner::

    from daiyi_grinding.perception_stress import run
    summary = run(output, seed=20261123)
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
import platform
import resource
import shlex
import sys
import time
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.optimize import linear_sum_assignment

from .learned_defects import (
    REGISTRY_FILE,
    LearnedDetector,
    registered_checkpoint,
)
from .project import ROOT, RUNS
from .thin_reference import THIN_REFERENCE_CONFIG, detect_thin_reference
from .workstation_evaluation import binary_metrics

OUTPUT_SCHEMA = "enterprise-perception-stress-v1"
INPUT_SCHEMA = "enterprise-perception-regression-input-v1"
DEFAULT_OUTPUT = RUNS / "enterprise_offline_20261123" / "perception"
REGRESSION_BATCH = RUNS / "data_reuse_20261121" / "team_prior_batch_final_02"
HYBRID_SOURCE = Path(__file__).with_name("workstation_inspection.py")
TARGET_MM = 0.1

# Nominal inspection volume and cell detector configuration copied from the
# current workstation so the offline reconstruction matches the production
# geometric interface without importing the simulation modules.
CELL_BOUNDS = ([-0.29, -0.23, 0.04], [0.23, 0.23, 0.41])
CELL_DETECTOR_CONFIG = replace(
    THIN_REFERENCE_CONFIG, minimum_pixels=6, component_join_mm=2.0
)
NORMAL_HEIGHT_QUANTILE = 0.8

FOREGROUND_LABELS = (1, 2)
KIND_BY_LABEL = {1: "seam", 2: "burr"}
SMALL_OBJECT_MAX_PIXELS = 64
MEDIUM_OBJECT_MAX_PIXELS = 400
DEFAULT_CASE_NAMES = (
    "casting_burr_seed9301",
    "casting_flash_seed9301",
    "casting_mixed_seed9301",
    "casting_normal_seed9301",
)
DEFAULT_PERTURBATION_CASES = ("casting_burr_seed9301",)
PERTURBATIONS = (
    {"kind": "depth_bias", "bias_mm": 0.30},
    {"kind": "depth_noise", "sigma_mm": 0.30},
    {"kind": "dropout", "fraction": 0.05},
    {
        "kind": "occlusion",
        "rows": [192, 320],
        "cols": [384, 640],
    },
    {
        "kind": "small_target_stress",
        "residual_seed_mm": 0.40,
        "dropout_fraction": 0.40,
        "maximum_component_pixels": SMALL_OBJECT_MAX_PIXELS,
    },
)
PUBLIC_KEYS = ("rgb", "depth", "reference_depth", "K", "T_workpiece_camera")
SOURCE_FILES = (
    "src/daiyi_grinding/perception_stress.py",
    "src/daiyi_grinding/perception.py",
    "src/daiyi_grinding/thin_reference.py",
    "src/daiyi_grinding/learned_defects.py",
    "src/daiyi_grinding/defect_network.py",
    "src/daiyi_grinding/workstation_evaluation.py",
    "src/daiyi_grinding/workstation_inspection.py",
    "config/workstation.json",
    "runs/learned_grinding_20261121/split_plan.json",
    "assets/ier220_workstation/meshes/workpiece/wp_097_c0.stl",
)


def sha256(path):
    """Hash a file without importing the simulation-backed report helpers."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path, **metadata):
    """Absolute file reference with its actual content hash."""
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path), **metadata}


def write_json(path, value):
    """Write strict JSON; non-finite metrics must be explicit nulls."""
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def stable_hash(value):
    """Deterministic SHA256 of a JSON-serializable value."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def array_hash(array):
    """Hash dtype, shape and raw array values for paired-input evidence."""
    array = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(stable_hash([array.dtype.str, list(array.shape)]).encode())
    digest.update(array.tobytes())
    return digest.hexdigest()


def load_observation(npz_path):
    """Load only measured/CAD inputs; reject evaluator fields in public data."""
    with np.load(npz_path, allow_pickle=False) as arrays:
        forbidden = {
            key
            for key in arrays.files
            if key in {"semantic", "instance", "ideal_depth_m"}
            or key.startswith("root_")
        }
        if forbidden:
            raise ValueError(
                f"Evaluator fields in public RGB-D: {sorted(forbidden)}"
            )
        missing = set(PUBLIC_KEYS) - set(arrays.files)
        if missing:
            raise ValueError(f"Missing public RGB-D fields: {sorted(missing)}")
        return {key: arrays[key] for key in PUBLIC_KEYS}


def load_labels(labels_path):
    """Load evaluator labels for scoring/display; never for detection."""
    with np.load(labels_path, allow_pickle=False) as arrays:
        return {key: arrays[key] for key in arrays.files}


def reconstruct_observation(public):
    """Run the cell geometric detector on one archived RGB-D frame.

    The reconstruction uses the archived registered nominal depth directly,
    so it never renders MuJoCo or reads evaluator labels. Every view is
    treated as a first view, i.e. with full image permission.
    """
    detections, observation = detect_thin_reference(
        public["rgb"],
        public["depth"],
        public["reference_depth"],
        public["K"],
        public["T_workpiece_camera"],
        inspection_bounds=CELL_BOUNDS,
        config=CELL_DETECTOR_CONFIG,
        allow_sloped_surface=True,
    )
    observation["normals"] = surface_normals(
        observation["reference_xyz"], public["T_workpiece_camera"]
    )
    observation["sensor_valid"] = (
        np.isfinite(public["depth"])
        & (public["depth"] > 0.05)
        & (public["depth"] < 10)
    )
    return detections, observation


def run_geometric(public):
    """Geometric candidates and measured observation for one frame."""
    detections, observation = reconstruct_observation(public)
    return detections, observation


def run_learned(detector, public, observation):
    """Learned candidates on the same reconstructed observation."""
    detections, _ = detector.detect(
        public["rgb"],
        public["depth"],
        public["reference_depth"],
        observation,
    )
    return detections


def _reference_hybrid_candidates(geometric, learned, shape):
    """Literal reference of the documented hybrid merge rule."""
    preferred = [d for d in geometric if d.kind in {"seam", "review"}]
    protected = np.zeros(shape, dtype=bool)
    for candidate in preferred:
        protected[tuple(candidate.pixels.T)] = True
    occupied = ndimage.binary_dilation(protected, iterations=1)
    candidates = preferred + [
        d for d in learned if not occupied[tuple(d.pixels.T)].any()
    ]
    return [replace(d, id=index) for index, d in enumerate(candidates, 1)]


def surface_normals(reference_xyz, camera):
    """Camera-oriented normals from the registered visible CAD surface."""
    down = np.gradient(reference_xyz, axis=0)
    right = np.gradient(reference_xyz, axis=1)
    normals = np.cross(right, down)
    lengths = np.linalg.norm(normals, axis=-1, keepdims=True)
    normals /= np.maximum(lengths, 1e-12)
    toward_camera = camera[:3, 3] - reference_xyz
    normals[np.einsum("ijk,ijk->ij", normals, toward_camera) < 0] *= -1
    return normals


def observed_normal_height_mm(
    detection, observation, quantile=NORMAL_HEIGHT_QUANTILE
):
    """Replay the cell's measured CAD-normal excess calculation on arrays."""
    rows, cols = detection.pixels.T
    displacement = (
        observation["xyz"][rows, cols]
        - observation["reference_xyz"][rows, cols]
    )
    normals = observation["normals"][rows, cols]
    lengths = np.linalg.norm(normals, axis=1)
    valid = (
        np.isfinite(displacement).all(axis=1)
        & np.isfinite(normals).all(axis=1)
        & (lengths > 0.9)
    )
    if not valid.any():
        return None
    heights = np.einsum(
        "ij,ij->i", displacement[valid], normals[valid] / lengths[valid, None]
    )
    positive = heights[heights > 0]
    return (
        float(np.quantile(positive, quantile) * 1000) if len(positive) else 0.0
    )


def apply_review_envelope(detections, observation):
    """Replay the cell's common review envelope before candidate scoring."""
    protected = []
    for detection in detections:
        height = observed_normal_height_mm(
            detection, observation, quantile=0.95
        )
        if (
            detection.kind in {"seam", "burr"}
            and height is not None
            and height > CELL_DETECTOR_CONFIG.review_height_mm
        ):
            detection = replace(
                detection,
                kind="review",
                reason=(
                    "measured CAD-normal excess (95th percentile) "
                    f"{height:.3f} mm exceeds the "
                    f"{CELL_DETECTOR_CONFIG.review_height_mm:.3f} mm "
                    "deburring review envelope; no automatic machining path"
                ),
            )
        protected.append(detection)
    return protected


def _normalized_function(node):
    """Compare executable function syntax without its name or docstring."""
    node.name = "array_replay"
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return ast.dump(node, include_attributes=False)


@lru_cache(maxsize=1)
def candidate_source_parity():
    """Verify array replays against source without importing MuJoCo.

    Any policy drift is a suite error. There is no silent substitute for a
    changed production merge or review rule.
    """
    source_text = HYBRID_SOURCE.read_text(encoding="utf-8")
    functions = {
        node.name: node
        for node in ast.parse(source_text).body
        if isinstance(node, ast.FunctionDef)
    }
    pairs = (
        ("hybrid_candidates", _reference_hybrid_candidates),
        ("surface_normals", surface_normals),
        ("observed_normal_height_mm", observed_normal_height_mm),
        ("apply_review_envelope", apply_review_envelope),
    )
    verified = {}
    for name, replay in pairs:
        if name not in functions:
            raise ValueError(f"Missing production candidate policy: {name}")
        production = _normalized_function(functions[name])
        local = _normalized_function(
            ast.parse(inspect.getsource(replay)).body[0]
        )
        if production != local:
            raise ValueError(f"Production candidate policy changed: {name}")
        verified[name] = hashlib.sha256(production.encode()).hexdigest()
    return {
        **file_record(HYBRID_SOURCE),
        "method": "equal function AST after removing names and docstrings",
        "verified_functions": verified,
        "mujoco_imported_for_parity": False,
    }


def hybrid_candidates_interface(geometric, learned, shape):
    """Run the source-verified array replay of the production hybrid merge."""
    candidate_source_parity()
    return (
        _reference_hybrid_candidates(geometric, learned, shape),
        "source_verified_array_replay.hybrid_candidates",
    )


def predicted_masks(detections, shape):
    """Binary and per-kind predicted masks; review is kept separate."""
    binary = np.zeros(shape, dtype=bool)
    per_kind = {
        kind: np.zeros(shape, dtype=bool)
        for kind in ("seam", "burr", "review", "preserve")
    }
    for detection in detections:
        if detection.kind not in per_kind:
            raise ValueError(f"Unexpected candidate kind: {detection.kind}")
        per_kind[detection.kind][tuple(detection.pixels.T)] = True
        if detection.kind in ("seam", "burr"):
            binary[tuple(detection.pixels.T)] = True
    return binary, per_kind


def _instance_kind(semantic_values):
    """Majority defect label of one instance, or None if hidden this view."""
    values, counts = np.unique(semantic_values, return_counts=True)
    usable = {
        label: int(counts[values == label][0])
        for label in FOREGROUND_LABELS
        if (values == label).any()
    }
    if not usable:
        return None
    return KIND_BY_LABEL[max(usable, key=usable.get)]


def instance_records(labels, minimum_pixels=6):
    """Visible instance records with an explicit pixel eligibility threshold."""
    semantic = labels["semantic"]
    instance = labels["instance"]
    records = []
    for identity in sorted(int(i) for i in np.unique(instance) if i):
        mask = instance == identity
        area = int(mask.sum())
        kind = _instance_kind(semantic[mask])
        if kind is None or area < minimum_pixels:
            continue
        records.append(
            {
                "instance_id": identity,
                "kind": kind,
                "area_pixels": area,
                "size_stratum": (
                    "small"
                    if area <= SMALL_OBJECT_MAX_PIXELS
                    else (
                        "medium"
                        if area <= MEDIUM_OBJECT_MAX_PIXELS
                        else "large"
                    )
                ),
            }
        )
    return records


def count_metrics(counts):
    """Metrics from pooled confusion counts, retaining null denominators."""
    tp, fp, fn = (int(counts[name]) for name in ("tp", "fp", "fn"))
    result = {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
    }
    if "tn" in counts:
        result["tn"] = int(counts["tn"])
    return result


def object_matching(detections, labels, observation=None, *, class_aware=True):
    """Score one-to-one visible instances independently from binary pixels.

    Six pixels is the existing cell's visible-instance eligibility threshold.
    Tiny instances remain in binary and size-stratum pixel scoring. Assignment
    maximizes match count first, then overlap; one merged detection cannot count
    as two successfully identified objects.
    """
    truth_records = instance_records(labels)
    kinds = {"seam", "burr"} if class_aware else {"seam", "burr", "review"}
    candidates = [item for item in detections if item.kind in kinds]
    overlap = np.zeros((len(truth_records), len(candidates)), dtype=int)
    eligible = np.zeros(overlap.shape, dtype=bool)
    for row, record in enumerate(truth_records):
        for col, candidate in enumerate(candidates):
            overlap[row, col] = np.count_nonzero(
                labels["instance"][tuple(candidate.pixels.T)]
                == record["instance_id"]
            )
            eligible[row, col] = (
                not class_aware or candidate.kind == record["kind"]
            ) and overlap[row, col] >= max(3, record["area_pixels"] * 0.2)
    assigned = {}
    used = set()
    if overlap.size:
        areas = np.array([item["area_pixels"] for item in truth_records])
        scores = eligible * (
            1 + overlap / areas[:, None] / (len(truth_records) + 1)
        )
        rows, cols = linear_sum_assignment(scores, maximize=True)
        for row, col in zip(rows, cols):
            if eligible[row, col]:
                assigned[int(row)] = int(col)
                used.add(int(col))
    records = []
    for row, record in enumerate(truth_records):
        col = assigned.get(row)
        candidate = candidates[col] if col is not None else None
        matched_overlap = int(overlap[row, col]) if col is not None else 0
        records.append(
            {
                **record,
                "overlap_pixels": matched_overlap,
                "overlap_fraction": matched_overlap / record["area_pixels"],
                "matched": candidate is not None,
                "detection_id": candidate.id if candidate else None,
                "predicted_kind": candidate.kind if candidate else None,
                "best_any_kind_overlap_pixels": (
                    int(overlap[row].max()) if len(candidates) else 0
                ),
            }
        )
    matched = len(assigned)
    report = {
        "records": records,
        "matched_objects": matched,
        "visible_objects": len(records),
        "predicted_objects": len(candidates),
        "counts": count_metrics(
            {
                "tp": matched,
                "fp": len(candidates) - matched,
                "fn": len(records) - matched,
            }
        ),
        "criterion": (
            "one-to-one; visible instance >=6 pixels; "
            + ("correct-kind " if class_aware else "class-agnostic ")
            + "overlap >= max(3, 0.2 * instance area)"
        ),
    }
    if class_aware:
        report["per_kind"] = {}
        for kind in ("seam", "burr"):
            true_count = sum(item["kind"] == kind for item in records)
            predicted_count = sum(item.kind == kind for item in candidates)
            tp = sum(
                item["matched"] and item["kind"] == kind for item in records
            )
            report["per_kind"][kind] = count_metrics(
                {"tp": tp, "fp": predicted_count - tp, "fn": true_count - tp}
            )
    return report


def size_strata_masks(labels):
    """Pixel masks of visible defect instances grouped by area."""
    semantic = labels["semantic"]
    masks = {
        name: np.zeros(semantic.shape, dtype=bool)
        for name in ("small", "medium", "large")
    }
    for record in instance_records(labels, minimum_pixels=1):
        masks[record["size_stratum"]] |= (
            labels["instance"] == record["instance_id"]
        )
    return masks


def measurable_root_mask(labels, observation):
    """Archived ideal visibility intersected with current raw depth validity.

    The archived visibility flags describe synthetic rendering. They are not
    measurements of real root error. Pressure dropout/occlusion must invalidate
    affected root pixels instead of reusing the unperturbed validity flag.
    """
    roots = labels.get("root_points_workpiece_m")
    if roots is None or not len(roots):
        return np.zeros(0, dtype=bool)
    measurable = (
        labels["root_in_image"]
        & labels["root_depth_consistent"]
        & labels["root_sensor_depth_valid"]
        & np.isfinite(roots).all(axis=1)
    )
    pixels = labels.get("root_pixels")
    if pixels is not None:
        pixels = np.asarray(pixels)
        height, width = observation["sensor_valid"].shape
        u, v = pixels.T
        inside = (u >= 0) & (u < width) & (v >= 0) & (v < height)
        current = np.zeros(len(roots), dtype=bool)
        current[inside] = observation["sensor_valid"][v[inside], u[inside]]
        measurable &= current
    return measurable


def testability(labels, observation):
    """Visible-pixel support and synthetic root visibility under current depth."""
    semantic = labels["semantic"]
    truth = np.isin(semantic, FOREGROUND_LABELS)
    measurable = observation["surface"] & observation["selection"]
    total = int(truth.sum())
    roots = labels.get("root_points_workpiece_m")
    root_result = {
        "total_root_points": int(len(roots)) if roots is not None else 0,
        "measurable_root_points": 0,
        "measurable_root_fraction": None,
        "basis": "archived ideal visibility and current raw sensor validity",
        "synthetic_visibility_only": True,
    }
    if roots is not None and len(roots):
        measurable_roots = int(measurable_root_mask(labels, observation).sum())
        root_result.update(
            measurable_root_points=measurable_roots,
            measurable_root_fraction=measurable_roots / len(roots),
        )
    tiny = [
        record
        for record in instance_records(labels, minimum_pixels=1)
        if record["area_pixels"] < 6
    ]
    return {
        "defect_pixels": total,
        "measurable_defect_pixels": (
            int((truth & measurable).sum()) if total else 0
        ),
        "measurable_defect_fraction": (
            int((truth & measurable).sum()) / total if total else None
        ),
        "tiny_instances_below_cell_eligibility": len(tiny),
        "tiny_defect_pixels": sum(item["area_pixels"] for item in tiny),
        "tiny_pixels_included_in_binary_score": True,
        "root_measurability": root_result,
    }


def synthetic_root_localization(detections, labels, observation):
    """Synthetic region-to-root distances; never real ±0.1 mm evidence.

    For every visible defect instance with at least one measurable root
    point, the closest correct-kind detection centroid is compared with the
    closest root point in the workpiece frame. Instances without a matching
    detection or measurable roots are reported as ``missing`` with a reason
    instead of receiving a fabricated distance.
    """
    records = []
    roots = labels.get("root_points_workpiece_m")
    for instance in instance_records(labels):
        instance_mask = labels["instance"] == instance["instance_id"]
        candidates = [
            detection
            for detection in detections
            if detection.kind == instance["kind"]
            and np.count_nonzero(instance_mask[tuple(detection.pixels.T)])
        ]
        if not candidates:
            records.append(
                {
                    "instance_id": instance["instance_id"],
                    "kind": instance["kind"],
                    "status": "missing",
                    "reason": "no correct-kind detection overlaps the instance",
                }
            )
            continue
        if roots is None or not len(roots):
            records.append(
                {
                    "instance_id": instance["instance_id"],
                    "kind": instance["kind"],
                    "status": "missing",
                    "reason": "no archived root points",
                }
            )
            continue
        instance_roots = (
            labels["root_instances"] == instance["instance_id"]
        ) & (measurable_root_mask(labels, observation))
        root_points = roots[instance_roots]
        if not len(root_points):
            records.append(
                {
                    "instance_id": instance["instance_id"],
                    "kind": instance["kind"],
                    "status": "missing",
                    "reason": "no depth-consistent measurable root point",
                }
            )
            continue
        best = None
        for detection in candidates:
            centroid = detection.xyz.mean(axis=0)
            distance = float(
                np.linalg.norm(root_points - centroid, axis=1).min() * 1000
            )
            if best is None or distance < best["region_root_error_mm"]:
                nominal_centroid = observation["reference_xyz"][
                    tuple(detection.pixels.T)
                ].mean(axis=0)
                best = {
                    "detection_id": detection.id,
                    "detection_pixels": len(detection.pixels),
                    "region_root_error_mm": distance,
                    "nominal_chart_region_root_error_mm": float(
                        np.linalg.norm(
                            root_points - nominal_centroid, axis=1
                        ).min()
                        * 1000
                    ),
                }
        best.update(
            instance_id=instance["instance_id"],
            kind=instance["kind"],
            status="computed",
            basis=(
                "detection centroid to nearest depth-consistent measurable "
                "root point, workpiece frame"
            ),
        )
        records.append(best)
    computed = [
        record["region_root_error_mm"]
        for record in records
        if record["status"] == "computed"
    ]
    return {
        "synthetic_only": True,
        "records": records,
        "computed_instances": int(len(computed)),
        "missing_instances": int(len(records) - len(computed)),
        "mean_region_root_error_mm": (
            float(np.mean(computed)) if computed else None
        ),
        "note": (
            "region-level synthetic diagnostic; not a point-level real "
            "localization result and not ±0.1 mm evidence"
        ),
        "real_localization_target_mm": TARGET_MM,
        "real_localization_target_status": "pending_real_measurement",
    }


def score_view(detections, labels, observation):
    """Score one interface on one frame; evaluator labels stay here."""
    semantic = labels["semantic"]
    if (
        semantic.shape != observation["surface"].shape
        or labels["instance"].shape != semantic.shape
    ):
        raise ValueError("Evaluator masks do not match the public RGB-D shape")
    truth = np.isin(semantic, FOREGROUND_LABELS)
    valid = observation["surface"] | truth
    binary, per_kind = predicted_masks(detections, semantic.shape)
    anomaly = binary | per_kind["review"]
    metrics = {
        "binary": binary_metrics(truth, binary, valid),
        "anomaly_binary": binary_metrics(truth, anomaly, valid),
        "currently_measurable_binary": binary_metrics(
            truth, binary, observation["surface"] & observation["selection"]
        ),
        "per_kind": {
            kind: binary_metrics(semantic == label, per_kind[kind], valid)
            for label, kind in KIND_BY_LABEL.items()
        },
        "review_pixels": int(per_kind["review"].sum()),
        "review_defect_pixels": int((per_kind["review"] & truth).sum()),
        "preserve_pixels": int(per_kind["preserve"].sum()),
        "detections": len(detections),
    }
    strata = size_strata_masks(labels)
    matching = object_matching(detections, labels, observation)
    metrics["size_strata"] = {}
    for stratum_name, stratum_mask in strata.items():
        pixel_count = int((truth & stratum_mask).sum())
        stratum = {
            "defect_pixels": pixel_count,
            "binary_detected_pixels": int(
                (binary & truth & stratum_mask).sum()
            ),
            "anomaly_detected_pixels": int(
                (anomaly & truth & stratum_mask).sum()
            ),
            "per_kind": {},
        }
        for prefix in ("binary", "anomaly"):
            stratum[f"{prefix}_recall"] = (
                stratum[f"{prefix}_detected_pixels"] / pixel_count
                if pixel_count
                else None
            )
        for label, kind in KIND_BY_LABEL.items():
            stratum_truth = (semantic == label) & stratum_mask
            total = int(stratum_truth.sum())
            detected = int((stratum_truth & per_kind[kind]).sum())
            stratum["per_kind"][kind] = {
                "defect_pixels": total,
                "correct_kind_pixels": detected,
                "recall": detected / total if total else None,
            }
        objects = [
            item
            for item in matching["records"]
            if item["size_stratum"] == stratum_name
        ]
        stratum["eligible_instance_observations"] = len(objects)
        stratum["correct_kind_instance_observations"] = sum(
            item["matched"] for item in objects
        )
        metrics["size_strata"][stratum_name] = stratum
    metrics["object_matching"] = matching
    metrics["anomaly_object_matching"] = object_matching(
        detections, labels, observation, class_aware=False
    )
    metrics["testability"] = testability(labels, observation)
    metrics["synthetic_root_localization"] = synthetic_root_localization(
        detections, labels, observation
    )
    return metrics


def small_residual_mask(observation, spec):
    """Select small measured components without using evaluator labels."""
    positive = np.asarray(observation["residual"]) > (
        spec["residual_seed_mm"] / 1000
    )
    components, _ = ndimage.label(positive, structure=np.ones((3, 3), bool))
    counts = np.bincount(components.ravel())
    small = counts <= spec.get(
        "maximum_component_pixels", SMALL_OBJECT_MAX_PIXELS
    )
    small[0] = False
    return small[components]


def perturb_depth(depth, spec, rng, observation=None):
    """Apply one deterministic stress effect to an archived depth frame.

    Effects modify the measured depth only; the registered reference depth
    and labels are never changed. ``small_target_stress`` concentrates
    dropout on small positive residual pixels using the unperturbed
    observation, not any label.
    """
    modified = np.array(depth, dtype=np.float32).copy()
    valid = np.isfinite(modified) & (modified > 0.05)
    kind = spec["kind"]
    if kind == "depth_bias":
        modified[valid] += spec["bias_mm"] / 1000
    elif kind == "depth_noise":
        modified[valid] += rng.normal(
            0.0, spec["sigma_mm"] / 1000, int(valid.sum())
        )
    elif kind == "dropout":
        drop = rng.random(modified.shape) < spec["fraction"]
        modified[drop & valid] = np.nan
    elif kind == "occlusion":
        rows = slice(spec["rows"][0], spec["rows"][1])
        cols = slice(spec["cols"][0], spec["cols"][1])
        modified[rows, cols] = np.nan
    elif kind == "small_target_stress":
        if observation is None:
            raise ValueError(
                "small_target_stress requires the unperturbed observation"
            )
        small_positive = small_residual_mask(observation, spec)
        drop = (
            (rng.random(modified.shape) < spec["dropout_fraction"])
            & small_positive
            & valid
        )
        modified[drop] = np.nan
    else:
        raise ValueError(f"Unknown perturbation kind: {kind}")
    return modified


def _case_views(case_name):
    if Path(case_name).name != case_name:
        raise ValueError(
            "Regression case must be a single archive directory name"
        )
    case_dir = REGRESSION_BATCH / case_name
    views = sorted(case_dir.glob("public/view_[0-9][0-9].npz"))
    labels_paths = [
        case_dir / "evaluator" / path.name.replace(".npz", "_labels.npz")
        for path in views
    ]
    # Missing labels are errors, rather than a way to drop difficult frames.
    return case_dir, list(zip(views, labels_paths))


def _candidate_comparison(public, detector):
    """Produce all predictions before loading any evaluator arrays."""
    geometric, observation = run_geometric(public)
    learned = run_learned(detector, public, observation)
    hybrid, hybrid_backend = hybrid_candidates_interface(
        geometric, learned, public["depth"].shape
    )
    candidates = {
        name: apply_review_envelope(items, observation)
        for name, items in (
            ("geometric", geometric),
            ("learned", learned),
            ("hybrid", hybrid),
        )
    }
    return candidates, observation, hybrid_backend


def _score_candidates(candidates, labels, observation):
    return {
        name: score_view(items, labels, observation)
        for name, items in candidates.items()
    }


def _evaluation_context(labels):
    return {
        "visible_instance_ids": [
            item["instance_id"] for item in instance_records(labels)
        ],
        "archived_root_instance_ids": [
            int(value)
            for value in np.unique(labels.get("root_instances", []))
            if value
        ],
        "tiny_visible_instances": [
            item
            for item in instance_records(labels, minimum_pixels=1)
            if item["area_pixels"] < 6
        ],
        "root_point_count": len(labels.get("root_points_workpiece_m", [])),
    }


def _backends(hybrid_backend):
    return {
        "geometric": "thin_reference.detect_thin_reference",
        "learned": "learned_defects.LearnedDetector",
        "hybrid": hybrid_backend,
        "common_review_envelope": "source_verified_array_replay.apply_review_envelope",
    }


def _observation_hashes(public):
    return {name: array_hash(public[name]) for name in PUBLIC_KEYS}


def evaluate_frame(view_path, labels_path, detector):
    """Compare all three interfaces on one archived regression frame."""
    started = time.monotonic()
    public = load_observation(view_path)
    candidates, observation, hybrid_backend = _candidate_comparison(
        public, detector
    )
    labels = load_labels(labels_path)
    results = _score_candidates(candidates, labels, observation)
    return {
        "status": "evaluated",
        "view": view_path.name,
        "case": view_path.parent.parent.name,
        "dataset_role": "known_regression",
        "public_input": file_record(view_path, source=str(REGRESSION_BATCH)),
        "labels_input": file_record(
            labels_path,
            used_for="scoring and annotation display only",
        ),
        "paired_observation_hashes": _observation_hashes(public),
        "actual_backends": _backends(hybrid_backend),
        "detector_config": {
            "bounds_m": [list(limit) for limit in CELL_BOUNDS],
            "geometric": asdict(CELL_DETECTOR_CONFIG),
            "probability_threshold": detector.threshold,
        },
        "evaluation_context": _evaluation_context(labels),
        "observed_sensor": {
            name: observation[name]
            for name in ("noise_sigma_mm", "bias_mm", "seed_threshold_mm")
        },
        "wall_seconds": time.monotonic() - started,
        "results": results,
    }


def evaluate_perturbation(view_path, labels_path, detector, spec, seed):
    """Run all three interfaces on one perturbed regression frame."""
    started = time.monotonic()
    public = load_observation(view_path)
    _, base_observation = run_geometric(public)
    public_input = file_record(view_path)
    actual_seed = int(stable_hash([seed, public_input["sha256"], spec]), 16) % (
        2**32
    )
    rng = np.random.default_rng(actual_seed)
    depth = perturb_depth(
        public["depth"], spec, rng, observation=base_observation
    )
    modified = dict(public)
    modified["depth"] = depth
    if spec["kind"] == "occlusion":
        # A fixed image block masks both sensor channels. This is an array
        # occlusion stress, not a rerendered physical occluder.
        modified["rgb"] = public["rgb"].copy()
        rows = slice(*spec["rows"])
        cols = slice(*spec["cols"])
        modified["rgb"][rows, cols] = 128
    candidates, observation, hybrid_backend = _candidate_comparison(
        modified, detector
    )
    labels = load_labels(labels_path)
    record = {
        "status": "evaluated",
        "view": view_path.name,
        "case": view_path.parent.parent.name,
        "dataset_role": "known_regression_perturbed",
        "perturbation": dict(spec),
        "perturbation_sha256": stable_hash(spec),
        "original_input": public_input,
        "labels_input": file_record(
            labels_path, used_for="scoring and annotation display only"
        ),
        "paired_observation_hashes": _observation_hashes(modified),
        "seed": seed,
        "actual_rng_seed": actual_seed,
        "perturbation_effect": {
            "added_missing_depth_pixels": int(
                (
                    np.isfinite(public["depth"])
                    & ~np.isfinite(modified["depth"])
                ).sum()
            ),
            "small_measured_residual_pixels": (
                int(small_residual_mask(base_observation, spec).sum())
                if spec["kind"] == "small_target_stress"
                else None
            ),
            "rgb_masked": spec["kind"] == "occlusion",
            "labels_modified": False,
            "truth_denominator": "original archived visible defect pixels",
        },
        "actual_backends": _backends(hybrid_backend),
        "evaluation_context": _evaluation_context(labels),
        "observed_sensor": {
            name: observation[name]
            for name in ("noise_sigma_mm", "bias_mm", "seed_threshold_mm")
        },
        "results": _score_candidates(candidates, labels, observation),
        "wall_seconds": time.monotonic() - started,
    }
    return record


def write_annotation_crop(public_path, labels_path, output_path):
    """Render archived synthetic labels after scoring for annotation review."""
    public = load_observation(public_path)
    labels = load_labels(labels_path)
    semantic = labels["semantic"]
    selected = np.zeros(semantic.shape, dtype=bool)
    for record in instance_records(labels):
        selected |= labels["instance"] == record["instance_id"]
    if not selected.any():
        raise ValueError("No visible labelled instance for annotation crop")
    rows, cols = np.argwhere(selected).T
    row_min, row_max = rows.min(), rows.max()
    col_min, col_max = cols.min(), cols.max()
    margin = 24
    crop = (
        slice(
            max(0, row_min - margin), min(semantic.shape[0], row_max + margin)
        ),
        slice(
            max(0, col_min - margin), min(semantic.shape[1], col_max + margin)
        ),
    )
    original = public["rgb"][crop].copy()
    annotated = original.copy()
    colors = {1: (255, 80, 80), 2: (80, 120, 255), 3: (120, 120, 120)}
    for label, color in colors.items():
        mask = semantic[crop] == label
        if mask.any() and label != 3:
            annotated[mask] = (
                original[mask] * 0.55 + np.asarray(color) * 0.45
            ).astype(np.uint8)
    image = Image.fromarray(annotated)
    draw = ImageDraw.Draw(image)
    pixels = labels.get("root_pixels", [])
    visible = labels.get("root_depth_consistent", np.zeros(len(pixels), bool))
    for point, seen in zip(pixels, visible):
        col, row = (int(value) for value in point)
        col -= crop[1].start
        row -= crop[0].start
        if 0 <= col < image.width and 0 <= row < image.height:
            color = (32, 245, 98) if seen else (205, 112, 240)
            draw.ellipse((col - 1, row - 1, col + 1, row + 1), fill=color)
    panel = Image.new("RGB", (2 * image.width, image.height))
    panel.paste(Image.fromarray(original), (0, 0))
    panel.paste(image, (image.width, 0))
    panel.save(output_path)
    return output_path


def _objects_across_views(records, interface):
    """Deduplicate instance IDs within a case; never count views as parts."""
    objects = {}
    for record in records:
        context = record["evaluation_context"]
        for identity in context["archived_root_instance_ids"]:
            objects.setdefault(
                (record["case"], identity),
                {"case": record["case"], "instance_id": identity, "views": []},
            )
        for item in record["results"][interface]["object_matching"]["records"]:
            obj = objects.setdefault(
                (record["case"], item["instance_id"]),
                {
                    "case": record["case"],
                    "instance_id": item["instance_id"],
                    "views": [],
                },
            )
            obj["kind"] = item["kind"]
            obj["views"].append(
                {
                    "view": record["view"],
                    "correct_kind_matched": item["matched"],
                }
            )
    entries = list(objects.values())
    for obj in entries:
        obj["visible_at_cell_eligibility"] = bool(obj["views"])
        obj["correct_kind_matched_in_any_view"] = any(
            item["correct_kind_matched"] for item in obj["views"]
        )
    visible = sum(item["visible_at_cell_eligibility"] for item in entries)
    matched = sum(item["correct_kind_matched_in_any_view"] for item in entries)
    return {
        "records": entries,
        "archived_objects": len(entries),
        "visible_objects": visible,
        "correct_kind_matched_objects": matched,
        "not_visible_at_cell_eligibility": len(entries) - visible,
        "scope": "case-instance IDs deduplicated across selected camera views",
    }


def _aggregate_comparison(records):
    """Pool pixels and instance observations with separate support summaries."""
    aggregate = {}
    for interface in ("geometric", "learned", "hybrid"):
        pixel_names = (
            "binary",
            "anomaly_binary",
            "currently_measurable_binary",
        )
        pixels = {
            name: dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
            for name in pixel_names
        }
        kinds = {
            kind: dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
            for kind in ("seam", "burr")
        }
        instance_counts = {
            name: dict.fromkeys(("tp", "fp", "fn"), 0)
            for name in ("object_matching", "anomaly_object_matching")
        }
        class_instance_counts = {
            kind: dict.fromkeys(("tp", "fp", "fn"), 0)
            for kind in ("seam", "burr")
        }
        strata = {
            size: {
                **dict.fromkeys(
                    (
                        "defect_pixels",
                        "binary_detected_pixels",
                        "anomaly_detected_pixels",
                        "eligible_instance_observations",
                        "correct_kind_instance_observations",
                    ),
                    0,
                ),
                "per_kind": {
                    kind: {"defect_pixels": 0, "correct_kind_pixels": 0}
                    for kind in ("seam", "burr")
                },
            }
            for size in ("small", "medium", "large")
        }
        binary_f1s = []
        review_pixels = preserve_pixels = computed = missing = 0
        root_errors, nominal_errors = [], []
        testable = dict.fromkeys(
            (
                "defect_pixels",
                "measurable_defect_pixels",
                "tiny_instances_below_cell_eligibility",
                "tiny_defect_pixels",
                "root_point_observations",
                "measurable_root_point_observations",
            ),
            0,
        )
        for record in records:
            metrics = record["results"][interface]
            for name in pixel_names:
                for key in pixels[name]:
                    pixels[name][key] += metrics[name][key]
            for key in ("tp", "fp", "fn", "tn"):
                for kind, item in metrics["per_kind"].items():
                    kinds[kind][key] += item[key]
            if metrics["binary"]["f1"] is not None:
                binary_f1s.append(metrics["binary"]["f1"])
            for name, counts in instance_counts.items():
                for key in counts:
                    counts[key] += metrics[name]["counts"][key]
            for kind, counts in class_instance_counts.items():
                for key in counts:
                    counts[key] += metrics["object_matching"]["per_kind"][kind][
                        key
                    ]
            for size, stratum in strata.items():
                current = metrics["size_strata"][size]
                for key in stratum:
                    if key != "per_kind":
                        stratum[key] += current[key]
                for kind, counts in stratum["per_kind"].items():
                    for key in counts:
                        counts[key] += current["per_kind"][kind][key]
            review_pixels += metrics["review_pixels"]
            preserve_pixels += metrics["preserve_pixels"]
            visibility = metrics["testability"]
            for key in (
                "defect_pixels",
                "measurable_defect_pixels",
                "tiny_instances_below_cell_eligibility",
                "tiny_defect_pixels",
            ):
                testable[key] += visibility[key]
            root_support = visibility["root_measurability"]
            testable["root_point_observations"] += root_support[
                "total_root_points"
            ]
            testable["measurable_root_point_observations"] += root_support[
                "measurable_root_points"
            ]
            root = metrics["synthetic_root_localization"]
            computed += root["computed_instances"]
            missing += root["missing_instances"]
            for item in root["records"]:
                if item["status"] == "computed":
                    root_errors.append(item["region_root_error_mm"])
                    nominal_errors.append(
                        item["nominal_chart_region_root_error_mm"]
                    )
        for stratum in strata.values():
            total = stratum["defect_pixels"]
            for prefix in ("binary", "anomaly"):
                stratum[f"{prefix}_recall"] = (
                    stratum[f"{prefix}_detected_pixels"] / total
                    if total
                    else None
                )
            for counts in stratum["per_kind"].values():
                counts["recall"] = (
                    counts["correct_kind_pixels"] / counts["defect_pixels"]
                    if counts["defect_pixels"]
                    else None
                )

        aggregate[interface] = {
            "binary_pooled": count_metrics(pixels["binary"]),
            "anomaly_binary_pooled": count_metrics(pixels["anomaly_binary"]),
            "currently_measurable_binary_pooled": count_metrics(
                pixels["currently_measurable_binary"]
            ),
            "per_kind_pooled": {
                kind: count_metrics(kinds[kind]) for kind in ("seam", "burr")
            },
            "binary_mean_f1": (
                float(np.mean(binary_f1s)) if binary_f1s else None
            ),
            "object_matching": {
                "matched_objects": instance_counts["object_matching"]["tp"],
                "visible_objects": (
                    instance_counts["object_matching"]["tp"]
                    + instance_counts["object_matching"]["fn"]
                ),
                "counts": count_metrics(instance_counts["object_matching"]),
                "per_kind": {
                    kind: count_metrics(counts)
                    for kind, counts in class_instance_counts.items()
                },
                "scope": "one-to-one instance observations, not independent parts",
            },
            "anomaly_object_matching": count_metrics(
                instance_counts["anomaly_object_matching"]
            ),
            "objects_across_views": _objects_across_views(records, interface),
            "size_strata": strata,
            "review_pixels": review_pixels,
            "preserve_pixels": preserve_pixels,
            "testability": testable,
            "synthetic_root": {
                "computed_instances": int(computed),
                "missing_instances": int(missing),
                "mean_region_root_error_mm": (
                    float(np.mean(root_errors)) if root_errors else None
                ),
                "mean_nominal_chart_region_root_error_mm": (
                    float(np.mean(nominal_errors)) if nominal_errors else None
                ),
                "scope": "region centroid proxy; not point-level root localization",
                "real_target_status": "pending_real_measurement",
            },
        }
    aggregate["frame_count"] = len(records)
    return aggregate


def _load_frozen_detector(checkpoint):
    """Explicit CPU-only inference; importing the suite never loads torch."""
    import cv2
    import torch

    frozen = registered_checkpoint()
    if frozen is None:
        raise FileNotFoundError(
            "No locally registered frozen detector checkpoint"
        )
    selected = Path(checkpoint).resolve() if checkpoint else frozen
    expected_hash = sha256(frozen)
    if sha256(selected) != expected_hash:
        raise ValueError("Checkpoint must match the registered frozen weights")
    interop_note = None
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError as error:
        # A parent Python runner may already have initialized interop workers.
        # Record its actual count instead of concealing that resource setting.
        interop_note = str(error)
    detector = LearnedDetector(selected, device="cpu")
    torch.set_num_threads(1)  # LearnedDetector's general default is four.
    cv2.setNumThreads(1)
    registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    if detector.threshold != registry["probability_threshold"]:
        raise ValueError("Checkpoint threshold differs from frozen registry")
    return detector, {
        "device": detector.device,
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "torch_interop_threads": torch.get_num_interop_threads(),
        "torch_interop_setting_note": interop_note,
        "opencv_threads": cv2.getNumThreads(),
        "parameter_count": sum(p.numel() for p in detector.model.parameters()),
        "model_training": detector.model.training,
        "checkpoint": file_record(selected),
        "checkpoint_hash_verified": True,
        "probability_threshold": detector.threshold,
        "weights_updated": False,
    }


def _failure(scope, error, **context):
    return {
        "scope": scope,
        **context,
        "exception_type": type(error).__name__,
        "reason": f"{type(error).__name__}: {error}",
    }


def _resource_report(started, usage_before, runtime, modules_before):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    added_mujoco = sorted(
        name
        for name in set(sys.modules) - modules_before
        if name == "mujoco" or name.startswith("mujoco.")
    )
    return {
        "wall_seconds": time.monotonic() - started,
        "process_cpu_seconds": (
            usage.ru_utime
            + usage.ru_stime
            - usage_before.ru_utime
            - usage_before.ru_stime
        ),
        "process_peak_rss_kib": int(usage.ru_maxrss),
        "process_peak_rss_mib": float(usage.ru_maxrss / 1024),
        "peak_rss_scope": "whole process high-water mark, not a delta",
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "environment": {
            name: os.environ.get(name)
            for name in (
                "MUJOCO_GL",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
            )
        },
        "mujoco_modules_added_by_suite": added_mujoco,
        "mujoco_present_before_suite": "mujoco" in modules_before,
        "learned_inference": runtime,
    }


def _annotation_tasks(records):
    """Separate evaluation findings from unfilled real annotation requirements."""
    tasks = []
    for record in records:
        tasks.append(
            {
                "case": record["case"],
                "view": record["view"],
                "labels_input": record["labels_input"],
                "tiny_visible_instances": record["evaluation_context"][
                    "tiny_visible_instances"
                ],
                "class_instance_misses": {
                    name: [
                        item["instance_id"]
                        for item in record["results"][name]["object_matching"][
                            "records"
                        ]
                        if not item["matched"]
                    ]
                    for name in ("geometric", "learned", "hybrid")
                },
                "synthetic_root_diagnostic_missing": {
                    name: [
                        item
                        for item in record["results"][name][
                            "synthetic_root_localization"
                        ]["records"]
                        if item["status"] == "missing"
                    ]
                    for name in ("geometric", "learned", "hybrid")
                },
            }
        )
    return {
        "schema": "enterprise-perception-annotation-todo-v1",
        "labels_modified": False,
        "labels_used_for": "evaluation and annotation review only",
        "synthetic_review_tasks": tasks,
        "real_097_annotation_status": "pending",
        "real_collection_tasks": [
            "same-part 097 RGB-D / original point cloud with identity, units and hashes",
            "confirmed defect kind and instance masks, including tiny/occluded objects",
            "point-level roots in a calibrated workpiece frame with uncertainty",
            "normal-region registration anchors separate from defect roots",
            "process-confirmed roots and allowed / protected surface regions",
            "independent validation of the ±0.1 mm root-localization target",
        ],
        "crop_legend": (
            "left: archived RGB; right: synthetic seam red / burr blue; "
            "green roots: ideal-depth consistent; purple: hidden/inconsistent. "
            "These colors do not show real root accuracy or machining permission."
        ),
    }


def _percent(value):
    return f"{value * 100:.2f}%" if value is not None else "undefined"


def _write_report(path, summary):
    metrics = summary["metrics"]
    lines = [
        "# 097 archived perception stress\n",
        "Input role: known regression; seeds 9301/9302 were used for training. "
        "This is array replay of synthetic data from one CAD.\n",
        f"Compared {metrics['known_regression_frames']} original frames and "
        f"{metrics['perturbation_count']} pressure observations; "
        f"{len(summary['failures'])} suite errors. "
        f"Status: {summary['status']}.\n",
        "Candidate binary counts seam/burr; anomaly binary also counts review. "
        "Correct-kind instance assignment is one-to-one with the existing "
        ">=6-pixel eligibility and >=max(3,20% area) overlap. "
        "Smaller instances remain in binary and size-stratum recall.\n",
        "| Interface | Candidate binary F1 | Anomaly binary F1 | Seam F1 | "
        "Burr F1 | Correct-kind instance observations |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in ("geometric", "learned", "hybrid"):
        item = metrics["comparison"][name]
        matched = item["object_matching"]
        lines.append(
            f"| {name} | {_percent(item['binary_pooled']['f1'])} | "
            f"{_percent(item['anomaly_binary_pooled']['f1'])} | "
            f"{_percent(item['per_kind_pooled']['seam']['f1'])} | "
            f"{_percent(item['per_kind_pooled']['burr']['f1'])} | "
            f"{matched['matched_objects']}/{matched['visible_objects']} |"
        )
    lines.extend(
        [
            "\nPressure uses the original archived truth denominator. "
            "The same modified RGB-D is passed to all interfaces; "
            "missing pixels are retained as misses in the original-visible score. "
            "Currently measurable scoring and support are reported separately.\n",
            "| Pressure | Frames | Geometric F1 | Learned F1 | Hybrid F1 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, group in metrics["perturbations_by_kind"].items():
        scores = [
            _percent(group[interface]["binary_pooled"]["f1"])
            for interface in ("geometric", "learned", "hybrid")
        ]
        lines.append(
            f"| {name} | {group['frame_count']} | {' | '.join(scores)} |"
        )
    resources = summary["resources"]
    lines.extend(
        [
            "\nGeometry calls the actual registered-reference detector. "
            "Hybrid merge, normals and review policy are source-verified array "
            "replays without importing workstation simulation. Bounds reconstruct "
            "the nominal chart; the archive has no nominal CAD identity mask, "
            "so this is not a replay of the full live inspection/action pipeline.\n",
            "Root fields are synthetic candidate-centroid-to-nearest-root "
            "diagnostics, not point-level root estimates. Root visibility uses "
            "archived ideal depth and current raw measured validity. "
            "Real 097 ±0.1 mm localization, process permissions and real "
            "annotations remain pending; see annotation_todo.json.\n",
            f"CPU wall time {resources['wall_seconds']:.3f} s; "
            f"process peak RSS {resources['process_peak_rss_mib']:.2f} MiB. "
            "No MuJoCo loading, rendering, integration, robot commands or "
            "external messages are performed.\n",
            "No weights or thresholds are updated. This run does not reproduce "
            "SAM/action weights, prove real accuracy or test cross-CAD "
            "generalization.\n",
        ]
    )
    if summary["failures"]:
        lines.append("Suite errors:\n")
        lines.extend(f"- {item['reason']}" for item in summary["failures"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_chart(path, comparison):
    """Standalone comparison artifact; no server or browser is started."""
    colors = ("#6b7280", "#0284c7", "#0d9488")
    bars = []
    for index, name in enumerate(("geometric", "learned", "hybrid")):
        f1 = comparison[name]["binary_pooled"]["f1"]
        width = (f1 or 0) * 430
        y = 32 + index * 52
        bars.append(
            f'<text x="0" y="{y + 18}">{name}</text>'
            f'<rect x="110" y="{y}" width="{width:.3f}" height="27" '
            f'rx="3" fill="{colors[index]}"/>'
            f'<text x="{120 + width:.3f}" y="{y + 19}">{_percent(f1)}</text>'
        )
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>097 known regression perception</title>"
        "<style>body{font:16px system-ui;max-width:850px;margin:36px auto;"
        "padding:20px;color:#1f2937}svg{width:100%;height:auto}"
        "p{line-height:1.6}h1{font-size:25px}</style>"
        "<h1>097 known regression · candidate binary F1</h1>"
        f"<p>{comparison['frame_count']} archived synthetic camera frames. "
        "Development/training seeds; one CAD. Frozen weights, CPU inference. "
        "This comparison evaluates candidate pixels, not machining success.</p>"
        '<svg viewBox="0 0 670 200" role="img" '
        'aria-label="Candidate binary F1 comparison">'
        + "".join(bars)
        + "</svg><p>Review anomalies, correct-kind instances, small-target "
        "recall and synthetic root diagnostics are reported separately. "
        "Real 097 ±0.1 mm root localization remains pending.</p></html>",
        encoding="utf-8",
    )


def run(
    output,
    seed=20261123,
    case_names=DEFAULT_CASE_NAMES,
    perturbation_cases=DEFAULT_PERTURBATION_CASES,
    view_limit=None,
    checkpoint=None,
    command=None,
):
    """Run CPU single-thread array replay and preserve all suite errors.

    Low metric scores are reported as results. Missing inputs, changed policies
    or unexpected exceptions mark the summary failed, and the CLI returns nonzero.
    Existing nonempty directories are never overwritten.
    """
    if view_limit is not None and view_limit < 1:
        raise ValueError("view_limit must be positive")
    if not case_names or len(set(case_names)) != len(case_names):
        raise ValueError("Select unique regression cases")
    if len(set(perturbation_cases)) != len(perturbation_cases):
        raise ValueError("Select unique perturbation cases")
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(
            "Output is not empty; choose a new perception directory"
        )
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    modules_before = set(sys.modules)
    detector = None
    runtime, parity = {}, {}
    comparison_records, perturbation_records, failures, inputs = [], [], [], []
    pairs = {}
    for case_name in dict.fromkeys([*case_names, *perturbation_cases]):
        try:
            _, views = _case_views(case_name)
            pairs[case_name] = views[:view_limit] if view_limit else views
            if not views:
                raise FileNotFoundError("no archived public RGB-D views found")
        except Exception as error:
            pairs[case_name] = []
            failures.append(_failure("regression_input", error, case=case_name))
    try:
        candidate_source_parity.cache_clear()
        parity = candidate_source_parity()
        detector, runtime = _load_frozen_detector(checkpoint)
        inputs = [
            file_record(
                REGRESSION_BATCH / "manifest.json",
                role="known_regression_batch_manifest",
                provenance="archived synthetic training/development seeds 9301/9302",
            ),
            file_record(
                REGISTRY_FILE, role="detector_registry", read_only=True
            ),
            file_record(detector.path, role="frozen_learned_checkpoint"),
            *[
                file_record(ROOT / name, role="source_or_provenance")
                for name in SOURCE_FILES
            ],
        ]
    except Exception as error:
        failures.append(_failure("suite_setup", error))
        detector = None
    if detector is not None:
        for case_name in case_names:
            for view_path, labels_path in pairs[case_name]:
                try:
                    comparison_records.append(
                        evaluate_frame(view_path, labels_path, detector)
                    )
                except Exception as error:
                    failures.append(
                        _failure(
                            "comparison",
                            error,
                            case=case_name,
                            view=view_path.name,
                            public_path=str(view_path),
                            labels_path=str(labels_path),
                        )
                    )
        for case_name in perturbation_cases:
            for view_path, labels_path in pairs[case_name]:
                for spec in PERTURBATIONS:
                    try:
                        record = evaluate_perturbation(
                            view_path, labels_path, detector, spec, seed
                        )
                    except Exception as error:
                        record = {
                            "status": "failed",
                            "case": case_name,
                            "view": view_path.name,
                            "perturbation": dict(spec),
                            **_failure("perturbation", error),
                        }
                        failures.append(dict(record))
                    perturbation_records.append(record)
        if sha256(detector.path) != runtime["checkpoint"]["sha256"]:
            failures.append(
                _failure(
                    "frozen_checkpoint",
                    ValueError("weights changed during suite"),
                )
            )
    for record in [*comparison_records, *perturbation_records]:
        for key in ("public_input", "original_input", "labels_input"):
            if key in record:
                inputs.append(record[key])
    inputs = list({item["path"]: item for item in inputs}.values())
    evaluated_pressure = [
        record
        for record in perturbation_records
        if record["status"] == "evaluated"
    ]
    aggregate = _aggregate_comparison(comparison_records)
    metrics = {
        "comparison": aggregate,
        "by_case": {
            case: _aggregate_comparison(
                [
                    record
                    for record in comparison_records
                    if record["case"] == case
                ]
            )
            for case in case_names
        },
        "perturbation_baseline": _aggregate_comparison(
            [
                record
                for record in comparison_records
                if record["case"] in perturbation_cases
            ]
        ),
        "perturbations_by_kind": {
            spec["kind"]: _aggregate_comparison(
                [
                    record
                    for record in evaluated_pressure
                    if record["perturbation"]["kind"] == spec["kind"]
                ]
            )
            for spec in PERTURBATIONS
        },
        "known_regression_frames": len(comparison_records),
        "expected_regression_frames": sum(
            len(pairs[name]) for name in case_names
        ),
        "perturbation_count": len(perturbation_records),
        "expected_perturbation_count": (
            sum(len(pairs[name]) for name in perturbation_cases)
            * len(PERTURBATIONS)
        ),
        "perturbation_failed": len(perturbation_records)
        - len(evaluated_pressure),
        "selected_cases": list(case_names),
        "perturbation_cases": list(perturbation_cases),
        "views_per_case_limit": view_limit,
        "size_strata_pixels": {
            "small": [1, SMALL_OBJECT_MAX_PIXELS],
            "medium": [SMALL_OBJECT_MAX_PIXELS + 1, MEDIUM_OBJECT_MAX_PIXELS],
            "large": [MEDIUM_OBJECT_MAX_PIXELS + 1, None],
        },
        "metric_scope": (
            "candidate binary seam/burr; anomaly binary also includes review; "
            "original-visible truth retained under pressure; correct-kind "
            "one-to-one eligible instance observations; size recall includes "
            "1–5 pixel instances; views/cases sharing a seed are correlated"
        ),
    }
    annotation_status = "skipped_no_visible_labelled_instance"
    annotation_file = output / "annotation_crop.png"
    annotation_view = next(
        (
            record
            for record in comparison_records
            if record["evaluation_context"]["visible_instance_ids"]
        ),
        None,
    )
    if annotation_view:
        try:
            write_annotation_crop(
                Path(annotation_view["public_input"]["path"]),
                Path(annotation_view["labels_input"]["path"]),
                annotation_file,
            )
            annotation_status = "written"
        except Exception as error:
            annotation_status = "failed"
            failures.append(_failure("annotation_display", error))
    metrics["annotation_status"] = annotation_status
    resources = _resource_report(started, usage_before, runtime, modules_before)
    if resources["mujoco_modules_added_by_suite"]:
        failures.append(
            _failure("offline_scope", RuntimeError("MuJoCo was imported"))
        )
    scope = {
        "offline_software_only": True,
        "array_archive_replay_only": True,
        "mujoco_processes_started": 0,
        "mujoco_models_loaded": 0,
        "mujoco_imported_by_suite": bool(
            resources["mujoco_modules_added_by_suite"]
        ),
        "real_scan_used": False,
        "robot_control": False,
        "paths_or_actions_generated": False,
        "external_messages": False,
        "evaluator_separation": (
            "all three candidate predictions precede evaluator loading; "
            "labels only score and annotate"
        ),
        "nominal_mask_limitation": (
            "public archive has no nominal CAD identity mask; "
            "nominal chart uses registered XYZ inspection bounds"
        ),
        "not_sam_or_action_model_reproduction": True,
        "cross_cad_generalization_evaluated": False,
    }
    pending = {
        "real_097_rgbd_and_point_cloud": True,
        "confirmed_defect_roots_with_processing_permission": True,
        "real_root_localization_within_0_1mm": True,
        "real_root_target_mm": TARGET_MM,
        "note": "centroid-to-root synthetic diagnostics do not validate point-level real roots",
    }
    summary = {
        "schema": OUTPUT_SCHEMA,
        "task": "same-RGB-D geometric/learned/hybrid archived perception stress",
        "status": "failed" if failures else "complete",
        "exit_code": 1 if failures else 0,
        "source_kind": "synthetic_archived_rgbd",
        "dataset_role": "known_regression",
        "simulation_only": True,
        "hardware_controlled": False,
        "seed": seed,
        "scope": scope,
        "metrics": metrics,
        "resources": resources,
        "failures": failures,
        "real_data_pending": pending,
    }
    files = {
        "comparison.json": comparison_records,
        "perturbations.json": perturbation_records,
        "annotation_todo.json": _annotation_tasks(comparison_records),
        "resources.json": resources,
        "candidate_source_parity.json": parity,
        "summary.json": summary,
    }
    for name, content in files.items():
        write_json(output / name, content)
    _write_report(output / "report.md", summary)
    _write_chart(output / "comparison.html", aggregate)
    artifacts = [
        file_record(output / name)
        for name in [*files, "report.md", "comparison.html"]
    ]
    if annotation_file.is_file():
        artifacts.append(file_record(annotation_file))
    reproduce = [
        sys.executable,
        "-m",
        "daiyi_grinding.perception_stress",
        "--output",
        str(output),
        "--seed",
        str(seed),
        "--cases",
        ",".join(case_names),
        "--perturbation-cases",
        ",".join(perturbation_cases),
    ]
    if view_limit is not None:
        reproduce.extend(["--limit-views", str(view_limit)])
    if detector is not None:
        reproduce.extend(["--checkpoint", str(detector.path)])
    evidence = {
        "schema": "enterprise-offline-perception-evidence-v1",
        "task": summary["task"],
        "status": summary["status"],
        "scope": scope,
        "commands": [
            {
                "executed": command
                or "daiyi_grinding.perception_stress.run(...)",
                "entry": "cli" if command else "python_api",
                "reproduction": (
                    "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
                    + shlex.join(reproduce)
                ),
                "exit_code": summary["exit_code"],
                "output_policy": "choose a new empty directory for every reproduction",
            }
        ],
        "inputs": inputs,
        "metrics": metrics,
        "resources": resources,
        "failures": failures,
        "artifacts": artifacts,
        "real_data_pending": pending,
    }
    write_json(output / "evidence.json", evidence)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="explicit frozen checkpoint; must hash-match the local registry",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="comma-separated regression case names; default covers "
        "burr, flash, mixed and normal",
    )
    parser.add_argument(
        "--perturbation-cases",
        type=str,
        default=None,
        help="comma-separated cases for perturbation runs",
    )
    parser.add_argument(
        "--limit-views",
        type=int,
        default=None,
        help="restrict evaluated views per case for quick smoke runs",
    )
    args = parser.parse_args(argv)
    case_names = (
        tuple(name for name in args.cases.split(",") if name)
        if args.cases
        else DEFAULT_CASE_NAMES
    )
    perturbation_cases = (
        tuple(name for name in args.perturbation_cases.split(",") if name)
        if args.perturbation_cases is not None
        else DEFAULT_PERTURBATION_CASES
    )
    output = args.output or DEFAULT_OUTPUT / f"stress_{time.time_ns()}"
    try:
        report = run(
            output,
            seed=args.seed,
            case_names=case_names,
            perturbation_cases=perturbation_cases,
            view_limit=args.limit_views,
            checkpoint=args.checkpoint,
            command=shlex.join(
                [
                    sys.executable,
                    "-m",
                    "daiyi_grinding.perception_stress",
                    *(sys.argv[1:] if argv is None else argv),
                ]
            ),
        )
    except Exception as error:
        print(
            json.dumps(_failure("cli", error), ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "status": report["status"],
                "known_regression_frames": report["metrics"][
                    "known_regression_frames"
                ],
                "perturbation_runs": report["metrics"]["perturbation_count"],
                "failures": len(report["failures"]),
            },
            ensure_ascii=False,
        )
    )
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
