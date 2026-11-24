"""Geometry-only defect injection on normal RGB-D scans and saved-run mining.

Normal and injected siblings retain the same ``base_scan_group`` for splitting.
Their texture, scanner noise and CAD-registration errors come from the same
scan; no semantic tint or independently rendered defect domain is introduced.
The caller supplies an assumed defect-free, registered scan. Synthetic axial
height profiles and saved reinspection candidates are not real-defect labels
or calibrated material-removal measurements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class InjectionResult:
    """RGB, bundle arrays and provenance from one normal-scan augmentation."""

    arrays: dict[str, np.ndarray]
    rgb: np.ndarray
    metadata: dict


@dataclass(frozen=True)
class InjectedDefect:
    """One localized axial protrusion; sizes and height are in millimetres."""

    instance: int
    kind: str
    center_xy: tuple[int, int]
    height_mm: float
    length_mm: float
    width_mm: float
    angle_rad: float

    def record(self) -> dict:
        """Return JSON-safe geometry, independent of RGB material colours."""
        return {
            "instance": self.instance,
            "kind": self.kind,
            "center_xy": list(self.center_xy),
            "height_mm": self.height_mm,
            "length_mm": self.length_mm,
            "width_mm": self.width_mm,
            "angle_rad": self.angle_rad,
        }


def inject_synthetic_defects_on_scan(
    rgb,
    depth_m,
    reference_depth_m,
    intrinsics,
    *,
    seed,
    defect_kinds=("flash", "burr"),
    count=2,
    base_scan_source="unverified_normal_scan",
) -> InjectionResult:
    """Add rooted flash ridges or burr bumps to an assumed normal scan.

    Placement uses measured/reference depth gradients, curvature and surface
    boundaries, never RGB or labels. Heights are axial, towards the camera:
    flash 0.35--1.8 mm and burr 0.30--1.4 mm. RGB is modulated only by the ratio
    of class-independent Lambertian shading before/after the normal change.

    ``residual_mm`` records the injected protrusion, not an existing normal-scan
    registration/noise residual. ``depth_ideal_m`` is reference minus injected
    height; observed depth retains the input scan's error. Invalid depth pixels
    are untouched. ``count=0`` produces the matching normal hard negative.

    Args:
        rgb: H x W x 3 uint8 RGB scan.
        depth_m: H x W observed axial depth in metres, with invalid sentinels.
        reference_depth_m: Registered defect-free depth, in the same frame.
        intrinsics: Finite, zero-skew 3 x 3 pinhole camera matrix.
        seed: Nonnegative integer controlling geometry and illumination.
        defect_kinds: Sequence cycled over the requested instances.
        count: Number of non-overlapping instances, or zero for a control.
        base_scan_source: Caller-declared origin; no factory status is inferred.

    Raises:
        ValueError: Shapes, calibration, kinds or surface support are invalid.
    """
    rgb = np.asarray(rgb)
    depth = np.asarray(depth_m, dtype=np.float32)
    nominal = np.asarray(reference_depth_m, dtype=np.float32)
    camera = np.asarray(intrinsics, dtype=np.float64)
    _validate_scan(rgb, depth, nominal, camera)
    if (
        not isinstance(seed, (int, np.integer))
        or isinstance(seed, bool)
        or seed < 0
    ):
        raise ValueError("seed must be a nonnegative integer")
    if (
        not isinstance(count, (int, np.integer))
        or isinstance(count, bool)
        or not 0 <= count <= np.iinfo(np.uint16).max
    ):
        raise ValueError("count must fit a nonnegative uint16 instance count")
    if not isinstance(defect_kinds, (list, tuple)) or not defect_kinds:
        raise ValueError("defect_kinds must contain flash or burr")
    if any(kind not in {"flash", "burr"} for kind in defect_kinds):
        raise ValueError("only flash and burr injection is supported")
    if not isinstance(base_scan_source, str) or not base_scan_source.strip():
        raise ValueError("base_scan_source must declare the scan origin")

    from .dataset_pipeline import SEMANTIC_LABELS

    surface = (
        np.isfinite(depth)
        & (depth > 0.05)
        & (depth < 10)
        & np.isfinite(nominal)
        & (nominal > 0.05)
        & (nominal < 10)
    )
    if not surface.any():
        raise ValueError("scan has no valid registered surface")
    rng = np.random.default_rng(int(seed))
    score, gx, gy = _placement_geometry(depth, nominal, surface)
    interior = cv2.erode(
        surface.astype(np.uint8), np.ones((3, 3), np.uint8), borderValue=0
    ).astype(bool)
    candidates = (interior if interior.any() else surface) & (depth > 0.052)
    weights = np.where(candidates, score, 0.0).ravel().astype(np.float64)
    rows, columns = np.indices(depth.shape, dtype=np.float32)
    protrusion = np.zeros(depth.shape, dtype=np.float32)
    semantic = np.where(
        surface, SEMANTIC_LABELS["nominal_cad"], SEMANTIC_LABELS["background"]
    ).astype(np.uint8)
    instance = np.zeros(depth.shape, dtype=np.uint16)
    defects = []

    for identity in range(1, int(count) + 1):
        kind = defect_kinds[(identity - 1) % len(defect_kinds)]
        height = float(
            rng.uniform(0.35, 1.8)
            if kind == "flash"
            else rng.uniform(0.30, 1.4)
        )
        length = float(
            rng.uniform(10.0, 28.0)
            if kind == "flash"
            else rng.uniform(2.4, 5.5)
        )
        width = float(
            rng.uniform(0.8, 1.8)
            if kind == "flash"
            else length * rng.uniform(0.85, 1.15)
        )
        for _ in range(128):
            if weights.sum() == 0:
                raise ValueError(
                    "insufficient surface for non-overlapping defects"
                )
            center = int(rng.choice(weights.size, p=weights / weights.sum()))
            cy, cx = np.unravel_index(center, depth.shape)
            angle = (
                float(np.arctan2(gy[cy, cx], gx[cy, cx]) + np.pi / 2)
                if kind == "flash" and np.hypot(gx[cy, cx], gy[cy, cx]) > 1e-7
                else float(rng.uniform(0, np.pi))
            )
            defect = InjectedDefect(
                identity, kind, (int(cx), int(cy)), height, length, width, angle
            )
            profile = _defect_profile(
                defect, rows, columns, depth, nominal, surface, camera
            )
            footprint = profile > 0
            weights[center] = 0
            if np.any(footprint & (instance > 0)):
                continue
            protrusion += profile
            semantic[footprint] = SEMANTIC_LABELS[kind]
            instance[footprint] = identity
            weights[footprint.ravel()] = 0
            defects.append(defect.record())
            break
        else:
            raise ValueError("could not place non-overlapping defect profiles")

    observed = depth.copy()
    observed[surface] -= protrusion[surface] / 1000.0
    residual = np.full(depth.shape, np.nan, dtype=np.float32)
    residual[surface] = (depth[surface] - observed[surface]) * 1000.0
    ideal = nominal.copy()
    ideal[surface] -= residual[surface] / 1000.0
    raised = instance > 0
    protection = np.zeros(depth.shape, dtype=bool)
    if raised.any():
        distance_px = cv2.distanceTransform(
            (~raised).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
        )
        distance_mm = (
            distance_px * nominal * 1000 / np.sqrt(camera[0, 0] * camera[1, 1])
        )
        protection = surface & ~raised & (distance_mm <= 2.0)
    shaded = (
        _modulate_shading(rgb, nominal, ideal, surface, camera, rng)
        if count
        else rgb.copy()
    )
    return InjectionResult(
        arrays={
            "depth_m": observed,
            "depth_ideal_m": ideal,
            "depth_nominal_m": nominal.copy(),
            "semantic": semantic,
            "instance": instance,
            "residual_mm": residual,
            "protection": protection,
            "surface": surface,
        },
        rgb=shaded,
        metadata={
            "base_scan_source": base_scan_source,
            "base_scan_group": base_scan_source,
            "normal_status": "assumed defect-free by caller",
            "synthetic_defect_injected": bool(count),
            "real_defect_scan": False,
            "injection_seed": int(seed),
            "defects": defects,
            "camera": {
                "K": camera.tolist(),
                "width": depth.shape[1],
                "height": depth.shape[0],
            },
            "placement_source": "measured and registered-reference depth geometry only",
            "rgb_source": "base RGB with class-independent scalar shading from induced normals",
            "depth_valid_fraction": float(surface.mean()),
            "peak_residual_mm": float(np.nanmax(residual)),
            "supervision": {
                "residual_mm": "injected axial protrusion; existing scan noise/drift retained in observed depth",
                "depth_ideal_m": "registered nominal depth minus injected axial height, not a measured ideal scan",
                "protection": "untouched valid surface within approximately 2 mm of injected pixels",
            },
            "split_policy": "keep normal and injected siblings in the same base_scan_group and split",
            "scope": "synthetic augmentation on a saved normal scan; no confirmed real-defect or metrology evidence",
        },
    )


def _validate_scan(rgb, depth, nominal, camera):
    if depth.ndim != 2 or min(depth.shape, default=0) < 3:
        raise ValueError("depth must be a two-dimensional scan at least 3 x 3")
    if nominal.shape != depth.shape or rgb.shape != (*depth.shape, 3):
        raise ValueError("RGB, depth and reference must have matching shapes")
    if rgb.dtype != np.uint8:
        raise ValueError("RGB must be uint8 without implicit colour rescaling")
    if (
        camera.shape != (3, 3)
        or not np.isfinite(camera).all()
        or camera[0, 0] <= 0
        or camera[1, 1] <= 0
        or not np.allclose(camera[2], (0, 0, 1))
        or not np.allclose((camera[0, 1], camera[1, 0]), 0)
    ):
        raise ValueError("intrinsics must be a finite zero-skew pinhole matrix")


def _placement_geometry(depth, nominal, surface):
    weight = cv2.GaussianBlur(surface.astype(np.float32), (5, 5), 0)
    smooth = [
        cv2.GaussianBlur(np.where(surface, value, 0), (5, 5), 0)
        / np.maximum(weight, 1e-6)
        for value in (depth, nominal)
    ]
    gy, gx = np.gradient((smooth[0] + smooth[1]) / 2)
    curvature = np.abs(np.gradient(gx, axis=1)) + np.abs(
        np.gradient(gy, axis=0)
    )
    gradient = np.hypot(gx, gy)
    score = np.ones(depth.shape, dtype=np.float32)
    for value in (gradient, curvature):
        scale = float(np.percentile(value[surface], 95))
        if scale > 1e-8:
            score += 3 * np.clip(value / scale, 0, 2)
    distance = cv2.distanceTransform(surface.astype(np.uint8), cv2.DIST_L2, 3)
    score += 2 * np.exp(-distance / 3)
    return score, gx, gy


def _defect_profile(defect, rows, columns, depth, nominal, surface, camera):
    cx, cy = defect.center_xy
    dx = (columns - cx) * nominal[cy, cx] * 1000 / camera[0, 0]
    dy = (rows - cy) * nominal[cy, cx] * 1000 / camera[1, 1]
    along = (np.cos(defect.angle_rad) * dx + np.sin(defect.angle_rad) * dy) / (
        defect.length_mm / 2
    )
    across = (
        -np.sin(defect.angle_rad) * dx + np.cos(defect.angle_rad) * dy
    ) / (defect.width_mm / 2)
    if defect.kind == "flash":
        profile = (
            np.maximum(1 - along**2, 0) ** 2 * np.maximum(1 - across**2, 0) ** 2
        )
    else:
        profile = np.maximum(1 - along**2 - across**2, 0) ** 2
    profile = (profile * defect.height_mm).astype(np.float32)
    # Keep the local profile on the same depth sheet at occlusion boundaries.
    supported = surface & (np.abs(nominal - nominal[cy, cx]) < 0.012)
    return np.where(
        supported & (profile >= 0.03) & (depth - profile / 1000 > 0.05),
        profile,
        0,
    ).astype(np.float32)


def _surface_normals(depth, surface, camera):
    filled = np.where(surface, depth, np.median(depth[surface]))
    rows, columns = np.indices(depth.shape, dtype=np.float32)
    xyz = np.stack(
        (
            (columns - camera[0, 2]) * filled / camera[0, 0],
            (rows - camera[1, 2]) * filled / camera[1, 1],
            filled,
        ),
        axis=-1,
    )
    normals = np.cross(np.gradient(xyz, axis=0), np.gradient(xyz, axis=1))
    return normals / np.maximum(
        np.linalg.norm(normals, axis=-1, keepdims=True), 1e-12
    )


def _modulate_shading(rgb, nominal, ideal, surface, camera, rng):
    light = np.array([rng.uniform(-0.6, 0.6), rng.uniform(-0.6, 0.6), -1.0])
    light /= np.linalg.norm(light)
    before = _surface_normals(nominal, surface, camera)
    after = _surface_normals(ideal, surface, camera)
    brightness = [
        0.55 + 0.45 * np.maximum(normal @ light, 0)
        for normal in (before, after)
    ]
    gain = np.where(surface, brightness[1] / brightness[0], 1.0)
    return np.clip(
        np.rint(rgb.astype(np.float32) * gain[..., None]), 0, 255
    ).astype(np.uint8)


def write_injected_bundle(
    output_dir,
    sample_id,
    result: InjectionResult,
    *,
    workpiece_id,
    split="train",
    seed=0,
) -> dict:
    """Write a fresh bundle and return its ``scaled_training`` manifest row.

    No global manifest or historical sample is overwritten. Callers append the
    returned row to their dataset's ``samples`` and split by ``base_scan_group``
    before augmentation, not independently by injected sample seed.
    """
    if (
        not isinstance(sample_id, str)
        or not sample_id
        or Path(sample_id).name != sample_id
        or sample_id in {".", ".."}
    ):
        raise ValueError("sample_id must be a single nonempty directory name")
    if split not in {"train", "val", "test"}:
        raise ValueError("split must be train, val or test")
    if not str(workpiece_id):
        raise ValueError("workpiece_id must be declared")

    from .dataset_pipeline import _write_sample

    kinds = {defect["kind"] for defect in result.metadata["defects"]}
    row = {
        "sample_id": sample_id,
        "directory": f"samples/{sample_id}",
        "workpiece_id": str(workpiece_id),
        "split": split,
        "seed": int(seed),
        "sample_kind": "local_fov" if kinds else "normal_hard_negative",
        "defect_kind": next(iter(kinds))
        if len(kinds) == 1
        else "mixed"
        if kinds
        else None,
        "regime": "normal_scan_injection" if kinds else "normal_scan_control",
        "base_scan_source": result.metadata["base_scan_source"],
        "base_scan_group": result.metadata["base_scan_group"],
        "synthetic_defect_injected": bool(kinds),
        "real_defect_scan": False,
        "has_point_cloud": False,
        "point_count": 0,
        "depth_valid_fraction": result.metadata["depth_valid_fraction"],
        "peak_residual_mm": result.metadata["peak_residual_mm"],
    }
    _write_sample(
        Path(output_dir) / row["directory"],
        result.arrays,
        result.rgb,
        None,
        None,
        {**result.metadata, **row},
    )
    return row


HARD_EXAMPLE_WEIGHTS = {
    "persistent_positive_residual": 3.0,
    "review_region": 2.0,
    "operator_false_positive": 2.5,
    "operator_intervention": 1.5,
}


def harvest_closed_loop_hard_examples(
    run_dir, output_dir, *, interventions=None
) -> dict:
    """Mine a finished saved workstation run without instantiating a simulator.

    Positive signed-anchor reports from every post-grind inspection retain
    their round, observations and final resolution. Review detections retain
    inclusive ``bbox_xyxy`` in their best view. Operator masks label only their
    localized region as class 0; other measurements remain review candidates,
    not automatic positive labels. Parameter edits also retain the existing
    ``interventions_to_hard_examples`` synthesis requests.

    Args:
        run_dir: Directory containing result/status or a saved session state.
        output_dir: Destination for a new ``hard_examples_manifest.json``.
        interventions: Log records, a JSON/JSONL path, or None to read the run's
            ``interventions.jsonl`` when present.

    Returns:
        Manifest with stable categorical weights, normalized probabilities,
        relative observation paths and JSON pointers to the measured evidence.

    Raises:
        ValueError: No finished run is saved, or intervention rows are invalid.
        FileExistsError: The destination manifest already exists.
    """
    run = Path(run_dir).resolve()
    result = _read_json(run / "result.json", {})
    state = _read_json(run / "session_state.json", {})
    status = _read_json(run / "status.json", {})
    if any(not isinstance(value, dict) for value in (result, state, status)):
        raise ValueError(
            "saved result, session state and status must be mappings"
        )
    if not result and not state:
        raise ValueError("run has no saved result or session state")
    if status and status.get("stage") != "complete":
        raise ValueError("run must be finished before harvesting")
    if not result and state.get("stage") not in {
        "DONE",
        "ABORTED",
        "STOPPED_SAFETY",
    }:
        raise ValueError(
            "session must be finished or safety-stopped before harvesting"
        )
    receipt = _read_json(run / "cycle_receipt.json", {})
    registered = _read_json(
        run / "registered_regions.json", state.get("registered_regions", [])
    )
    anchors = {str(region["id"]): region for region in registered}
    initial = _read_json(run / "detections.json", [])
    initial_regions = {str(region["id"]): region for region in initial}
    initial_regions.update(
        {str(region["id"]): region for region in state.get("regions", [])}
    )
    segments = {
        str(segment["segment_id"]): segment
        for segment in state.get("segments", [])
    }
    reports = result.get("reinspections", [])
    if not reports and result.get("reinspection"):
        reports = [result["reinspection"]]
    if not reports and state.get("reinspection"):
        reports = [{"region_reinspection": state["reinspection"]}]
    final_regions = (
        {
            str(region["id"]): region
            for region in reports[-1]
            .get("region_reinspection", {})
            .get("regions", [])
        }
        if reports
        else {}
    )
    examples = []

    for index, report in enumerate(reports):
        prefix = f"rescan_{index + 1:02d}_"
        regions = report.get("region_reinspection", {}).get("regions", [])
        review_detections = _read_json(run / f"{prefix}detections.json", [])
        for region_index, region in enumerate(regions):
            positive = (
                region.get("positive_excess") is True
                or region.get("status_reason") == "positive_excess"
            )
            if not positive:
                continue
            if region.get("kind") == "review" and any(
                row.get("kind") == "review" for row in review_detections
            ):
                continue
            identity = str(region["id"])
            kind = (
                "review_region"
                if region.get("kind") == "review"
                else "persistent_positive_residual"
            )
            final = final_regions.get(identity, {})
            examples.append(
                {
                    "example_id": f"{run.name}:{prefix}region_{identity}",
                    "example_kind": kind,
                    "region_id": region["id"],
                    "defect_kind": _training_kind(region.get("kind")),
                    "source": {
                        "file": "result.json"
                        if result
                        else "session_state.json",
                        "json_pointer": f"/reinspections/{index}/region_reinspection/regions/{region_index}"
                        if result.get("reinspections")
                        else f"/reinspection/region_reinspection/regions/{region_index}"
                        if result
                        else f"/reinspection/regions/{region_index}",
                        "rescan_index": index + 1,
                        "observation_files": _observation_files(
                            run, prefix, region.get("observations")
                        ),
                    },
                    "region": _region_evidence(region, anchors.get(identity)),
                    "final_status": final.get("status"),
                    "resolved_by_final_scan": final.get("status") == "verified",
                    "supervision": "measured residual candidate; requires defect-label review",
                    "training_ready": False,
                }
            )

    detection_files = [
        run / "detections.json",
        *sorted(run.glob("rescan_*_detections.json")),
    ]
    if len(detection_files) == 1 and (run / "after_detections.json").is_file():
        detection_files.append(run / "after_detections.json")
    for path in detection_files:
        prefix = path.name.removesuffix("detections.json")
        for index, region in enumerate(_read_json(path, [])):
            if region.get("kind") != "review":
                continue
            examples.append(
                {
                    "example_id": f"{run.name}:{path.stem}:review_{index}",
                    "example_kind": "review_region",
                    "region_id": region.get("id"),
                    "defect_kind": None,
                    "source": {
                        "file": path.name,
                        "json_pointer": f"/{index}",
                        "observation_files": _observation_files(
                            run, prefix, [region.get("best_view", 0)]
                        ),
                    },
                    "region": _region_evidence(region),
                    "supervision": "review-held region; not a flash/burr ground-truth label",
                    "training_ready": False,
                }
            )

    entries = _intervention_rows(run, interventions)
    requests = []
    if entries:
        from .dataset_pipeline import interventions_to_hard_examples

        requests = [
            {**request, "source_record_index": index}
            for index, entry in enumerate(entries)
            for request in interventions_to_hard_examples([entry])["requests"]
        ]
        for index, request in enumerate(requests):
            source = request["source"]
            entry = entries[request["source_record_index"]]
            masked = source["action"] == "mask_false_positive"
            segment = segments.get(str(source.get("target_id")), {})
            identity = (
                str(source.get("target_id"))
                if source["action"]
                in {"mask_false_positive", "add_manual_region"}
                else str(segment.get("region_id"))
            )
            region = initial_regions.get(identity, entry.get("region", {}))
            evidence = _region_evidence(region, anchors.get(identity))
            if entry.get("roi_spec") or entry.get("after", {}).get("roi_spec"):
                evidence["roi_spec"] = entry.get(
                    "roi_spec", entry.get("after", {}).get("roi_spec")
                )
            if masked:
                request = {
                    **request,
                    "requested_defect_kind": None,
                    "target_class": 0,
                }
                requests[index] = request
            examples.append(
                {
                    "example_id": f"{run.name}:intervention_{request['source_record_index']}",
                    "example_kind": "operator_false_positive"
                    if masked
                    else "operator_intervention",
                    "region_id": region.get("id", segment.get("region_id")),
                    "defect_kind": None
                    if masked
                    else request["requested_defect_kind"],
                    "source": {
                        **source,
                        "observation_files": _operator_observations(
                            run, state, region
                        ),
                    },
                    "region": evidence,
                    "synthesis_request": request,
                    "target_class": 0 if masked else None,
                    "supervision": "operator-negative label inside the masked region only"
                    if masked
                    else "operator recipe edit or review; no automatic pixel label",
                    "training_ready": False,
                }
            )

    # Older runs may save RGB-D reinspection frames without signed-anchor JSON.
    if not reports:
        examples.extend(_saved_depth_candidates(run, result))
    total_weight = sum(
        HARD_EXAMPLE_WEIGHTS[row["example_kind"]] for row in examples
    )
    weighted = [
        {
            **row,
            "sampling_weight": HARD_EXAMPLE_WEIGHTS[row["example_kind"]],
            "sampling_probability": HARD_EXAMPLE_WEIGHTS[row["example_kind"]]
            / total_weight,
            "truth_used_for_control": False,
        }
        for row in examples
    ]
    manifest = {
        "schema": "daiyi-closed-loop-hard-examples-v1",
        "run_dir": str(run),
        "workpiece_id": result.get("cad_workpiece", state.get("workpiece_id")),
        "run_status": result.get("task_status", state.get("stage")),
        "evidence_scope": receipt.get(
            "evidence_scope",
            "simulation"
            if result.get("material_removal_simulated")
            else "unverified_saved_run",
        ),
        "real_defect_scan": False,
        "examples": weighted,
        "intervention_requests": requests,
        "sampling_policy": {
            "weights": HARD_EXAMPLE_WEIGHTS,
            "normalization": "weight divided by the sum of all harvested example weights",
            "ordering": "inspection order, sorted detection filenames, then intervention order",
        },
        "training_policy": "localize and review candidate labels before materializing scaled bundles; operator negatives apply only inside the masked region",
        "truth_used_for_control": False,
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "hard_examples_manifest.json").open(
        "x", encoding="utf-8"
    ) as stream:
        stream.write(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n"
        )
    return manifest


def _read_json(path, default):
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else default
    )


def _training_kind(kind):
    return (
        "flash"
        if kind in {"seam", "flash"}
        else "burr"
        if kind == "burr"
        else None
    )


def _region_evidence(region, registered=None):
    keys = (
        "kind",
        "status",
        "status_reason",
        "positive_excess",
        "peak_mm",
        "signed_peak_mm",
        "lower_peak_mm",
        "upper_peak_mm",
        "height_mm",
        "normal_height_mm",
        "target_remaining_height_mm",
        "observations",
        "anchors",
        "observed_anchors",
        "anchor_coverage",
        "anchor_bounds_mm",
        "anchor_estimates_mm",
        "anchor_points_m",
        "anchor_observation_counts",
        "length_mm",
        "width_mm",
        "confidence",
        "reason",
        "roi_spec",
    )
    evidence = {key: region[key] for key in keys if key in region}
    if "bbox" in region:
        evidence["bbox_xyxy"] = region["bbox"]
        evidence["bbox_convention"] = "inclusive pixel bounds in best_view"
        evidence["best_view"] = region.get("best_view", 0)
    if registered and "points" in registered:
        evidence["reference_points_m"] = registered["points"]
        evidence["coordinate_frame"] = "workpiece"
    return evidence


def _observation_files(run, prefix, indices=None):
    selected = set(indices) if indices is not None else None
    return [
        str(path.relative_to(run))
        for path in sorted(run.glob(f"{prefix}view_*_observation.npz"))
        if selected is None or int(path.stem.split("_")[-2]) in selected
    ]


def _operator_observations(run, state, region):
    if region.get("source_observation"):
        return [region["source_observation"]]
    if state.get("observation_snapshots"):
        return state["observation_snapshots"]
    return _observation_files(run, "", [region.get("best_view", 0)])


def _intervention_rows(run, interventions):
    source = (
        interventions
        if interventions is not None
        else run / "interventions.jsonl"
    )
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            if interventions is None:
                return []
            raise ValueError(f"intervention file does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        entries = (
            [json.loads(line) for line in text.splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else json.loads(text)
        )
    else:
        entries = source
    if not isinstance(entries, (list, tuple)) or any(
        not isinstance(entry, dict) for entry in entries
    ):
        raise ValueError("interventions must be a list of log records")
    return [{**entry, "diff": entry.get("diff", {})} for entry in entries]


def _saved_depth_candidates(run, result):
    paths = sorted(run.glob("rescan_*_view_*_observation.npz"))
    if not paths:
        paths = sorted(run.glob("after_view_*_observation.npz"))
    examples = []
    target = result.get("controller_stopping", {}).get(
        "target_remaining_height_mm", 0.3
    )
    for path in paths:
        with np.load(path, allow_pickle=False) as arrays:
            depth = arrays["depth"]
            reference = arrays["reference_depth"]
            surface = (
                arrays["reference_object_mask"].astype(bool)
                if "reference_object_mask" in arrays
                else arrays["surface"].astype(bool)
                if "surface" in arrays
                else np.ones(depth.shape, dtype=bool)
            )
            sigma = (
                float(arrays["synthetic_noise_sigma_mm"])
                if "synthetic_noise_sigma_mm" in arrays
                else 0.0
            )
        valid = (
            surface
            & np.isfinite(depth)
            & np.isfinite(reference)
            & (depth > 0.05)
            & (depth < 10)
            & (reference > 0.05)
            & (reference < 10)
        )
        residual = np.where(valid, (reference - depth) * 1000, 0).astype(
            np.float32
        )
        positive = cv2.medianBlur(residual, 3) > max(float(target), 3 * sigma)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (positive & valid).astype(np.uint8), 8
        )
        for identity in range(1, count):
            x, y, width, height, pixels = (
                int(value) for value in stats[identity]
            )
            if pixels < 6:
                continue
            selected = residual[labels == identity]
            examples.append(
                {
                    "example_id": f"{run.name}:{path.stem}:component_{identity}",
                    "example_kind": "persistent_positive_residual",
                    "region_id": identity,
                    "defect_kind": None,
                    "source": {
                        "file": path.name,
                        "observation_files": [path.name],
                    },
                    "region": {
                        "bbox_xyxy": [x, y, x + width - 1, y + height - 1],
                        "bbox_convention": "inclusive pixel bounds in the source observation",
                        "pixel_count": pixels,
                        "peak_mm": float(selected.max()),
                        "median_mm": float(np.median(selected)),
                        "threshold_mm": max(float(target), 3 * sigma),
                        "residual_frame": "camera axial depth; not signed surface-normal metrology",
                    },
                    "supervision": "post-grind depth-residual candidate; requires registration and defect-label review",
                    "training_ready": False,
                }
            )
    return examples
