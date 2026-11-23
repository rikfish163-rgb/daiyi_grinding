"""Observation-only defect detectors with fixed development configurations.

Both candidates estimate excess material from RGB-D and a calibrated inspection
volume. Their rule scores describe geometry and are not calibrated probabilities.
"""

from copy import deepcopy

import cv2
import numpy as np
from scipy import ndimage

from .perception import Detection, detect, roi_mask, unproject

CANDIDATE_METHODS = ("multiscale", "robust_local")
CANDIDATE_CONFIGS = {
    "multiscale": {
        "opening_radii_mm": [3.0, 6.0],
        "support_height_mm": 0.20,
        "seed_height_mm": 0.40,
        "support_noise_multiplier": 3.0,
        "seed_noise_multiplier": 5.0,
        "closing_pixels": 2,
        "design_guard_margin_mm": 1.2,
        "geometry_fallback_noise_sigma_mm": 0.12,
    },
    "robust_local": {
        "smoothing_sigma_mm": 3.0,
        "positive_weight_scale_mm": 0.20,
        "reweight_iterations": 4,
        "support_height_mm": 0.20,
        "seed_height_mm": 0.40,
        "support_noise_multiplier": 3.0,
        "seed_noise_multiplier": 5.0,
        "closing_pixels": 2,
        "design_guard_margin_mm": 1.2,
        "geometry_fallback_noise_sigma_mm": 0.12,
    },
}
DEFAULT_INSPECTION_BOUNDS = (
    (-0.67, -0.27, 0.612),
    (-0.31, 0.03, 0.655),
)
CLASSIFICATION_CONFIG = {
    "seam_min_length_mm": 18.0,
    "seam_max_width_mm": 4.2,
    "seam_min_aspect": 5.0,
    "burr_max_length_mm": 14.0,
    "burr_max_width_mm": 10.0,
    "burr_min_height_mm": 0.85,
    "burr_min_pixels": 60,
    "review_residual_mm": 5.0,
    "review_surface_height_mm": 6.0,
}


def candidate_configuration(method):
    """Return a JSON-serializable independent copy of a frozen configuration."""
    if method not in CANDIDATE_CONFIGS:
        raise ValueError(f"Unknown detector candidate: {method}")
    return {
        "method": method,
        **deepcopy(CANDIDATE_CONFIGS[method]),
        "classification": deepcopy(CLASSIFICATION_CONFIG),
        "inspection_bounds_m": [
            list(bound) for bound in DEFAULT_INSPECTION_BOUNDS
        ],
    }


def candidate_configurations():
    """Return independent report configurations for all detector candidates."""
    return {
        method: candidate_configuration(method) for method in CANDIDATE_METHODS
    }


def _prepare_observation(rgb, depth, intrinsics, transform, inspection_bounds):
    if depth.ndim != 2 or rgb.shape[:2] != depth.shape:
        raise ValueError("RGB/depth shape mismatch")
    if (
        intrinsics.shape != (3, 3)
        or transform.shape != (4, 4)
        or not np.isfinite(intrinsics).all()
        or not np.isfinite(transform).all()
        or intrinsics[0, 0] <= 0
        or intrinsics[1, 1] <= 0
    ):
        raise ValueError("Invalid camera intrinsics or transform")
    bounds = np.asarray(inspection_bounds, dtype=float)
    if (
        bounds.shape != (2, 3)
        or not np.isfinite(bounds).all()
        or np.any(bounds[0] >= bounds[1])
    ):
        raise ValueError("Inspection bounds require finite ordered XYZ limits")
    valid = np.isfinite(depth) & (depth > 0.1) & (depth < 10.0)
    if valid.mean() < 0.7:
        raise ValueError("Too few valid depth measurements; reacquire")
    filled = depth.copy()
    if not valid.all():
        nearest = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled[~valid] = depth[tuple(nearest[:, ~valid])]
    xyz = unproject(filled, intrinsics, transform)
    surface = np.all((xyz > bounds[0]) & (xyz < bounds[1]), axis=-1)
    surface = ndimage.binary_erosion(surface, iterations=3)
    components, count = ndimage.label(surface)
    if count:
        areas = np.bincount(components.ravel())
        areas[0] = 0
        surface = components == int(areas.argmax())
    height = cv2.medianBlur(xyz[..., 2].astype(np.float32), 3)
    scale = (
        float(np.median(filled[surface]) / intrinsics[0, 0])
        if surface.any()
        else 0.0004
    )
    return xyz, height, surface, valid, scale


def _opening_residual(height, scale, radii_mm):
    residual = np.zeros_like(height)
    for radius_mm in radii_mm:
        radius_pixels = max(3, int(round(radius_mm / 1000 / scale)))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * radius_pixels + 1, 2 * radius_pixels + 1),
        )
        baseline = cv2.morphologyEx(height, cv2.MORPH_OPEN, kernel)
        residual = np.maximum(residual, height - baseline)
    return residual


def _robust_residual(height, surface, scale, config):
    sigma_pixels = max(2.0, config["smoothing_sigma_mm"] / 1000 / scale)
    weights = surface.astype(np.float32)
    weight_scale = config["positive_weight_scale_mm"] / 1000
    baseline = height.copy()
    for _ in range(config["reweight_iterations"] + 1):
        denominator = ndimage.gaussian_filter(weights, sigma_pixels)
        numerator = ndimage.gaussian_filter(height * weights, sigma_pixels)
        baseline = np.divide(
            numerator,
            denominator,
            out=height.copy(),
            where=denominator > 1e-6,
        )
        positive_residual = np.maximum(height - baseline, 0.0)
        weights = surface / (1.0 + (positive_residual / weight_scale) ** 4)
        weights = weights.astype(np.float32)
    return np.maximum(height - baseline, 0.0)


def _noise_sigma(raw_height, filtered_height, surface):
    highpass = raw_height - filtered_height
    samples = highpass[surface]
    center = np.median(samples)
    return max(0.000025, float(1.4826 * np.median(np.abs(samples - center))))


def _region_measurements(
    points, pixels, components, component_id, residual, scale
):
    centered = points[:, :2] - points[:, :2].mean(axis=0)
    _, _, principal_axes = np.linalg.svd(centered, full_matrices=False)
    projected = centered @ principal_axes.T
    limits = np.quantile(projected, [0.025, 0.975], axis=0)
    length_mm = float(max(limits[1, 0] - limits[0, 0], scale) * 1000)
    row_min, col_min = pixels.min(axis=0)
    row_max, col_max = pixels.max(axis=0) + 1
    region_mask = np.pad(
        components[row_min:row_max, col_min:col_max] == component_id, 1
    )
    distances = ndimage.distance_transform_edt(region_mask)
    width_mm = float(
        2 * np.quantile(distances[region_mask], 0.90) * scale * 1000
    )
    height_mm = float(np.quantile(residual[tuple(pixels.T)], 0.95) * 1000)
    return length_mm, width_mm, height_mm


def _classify_region(length_mm, width_mm, height_mm, pixel_count):
    config = CLASSIFICATION_CONFIG
    if (
        length_mm > config["seam_min_length_mm"]
        and width_mm < config["seam_max_width_mm"]
        and length_mm / width_mm > config["seam_min_aspect"]
    ):
        return "seam", "narrow ridge connected to measured high evidence"
    if (
        length_mm < config["burr_max_length_mm"]
        and width_mm < config["burr_max_width_mm"]
        and height_mm > config["burr_min_height_mm"]
        and pixel_count >= config["burr_min_pixels"]
    ):
        return "burr", "compact positive material above local surface"
    return "preserve", "broad/design-like or ambiguous full feature"


def detect_candidate(
    rgb, depth, K, T, method="multiscale", roi=None, inspection_bounds=None
):
    """Detect local protrusions without scene IDs, reference labels or CAD truth.

    Args:
        rgb: Observed color image with the same pixel dimensions as depth.
        depth: Calibrated camera-forward depth in meters.
        K: Three-by-three camera intrinsics.
        T: Four-by-four camera-to-inspection-frame transform.
        method: Candidate name from CANDIDATE_METHODS.
        roi: Optional permitted image-pixel polygon, applied after classification.
        inspection_bounds: Optional minimum/maximum XYZ inspection limits in
            meters. The default is the original FR5 inspection volume.

    Returns:
        A list of Detection objects and planning-compatible observation fields.
    """
    if method not in CANDIDATE_CONFIGS:
        raise ValueError(f"Unknown detector candidate: {method}")
    config = CANDIDATE_CONFIGS[method]
    bounds = (
        DEFAULT_INSPECTION_BOUNDS
        if inspection_bounds is None
        else inspection_bounds
    )
    intrinsics = np.asarray(K, dtype=float)
    transform = np.asarray(T, dtype=float)
    xyz, height, surface, valid, scale = _prepare_observation(
        rgb, depth, intrinsics, transform, bounds
    )
    selection = roi_mask(depth.shape, roi)
    observation = dict(
        xyz=xyz,
        height=height,
        residual=np.zeros_like(height),
        mask=np.zeros_like(surface),
        scale=scale,
        selection=selection,
        K=intrinsics,
        T=transform,
        method=method,
    )
    if not surface.any():
        return [], observation
    sigma = _noise_sigma(xyz[..., 2], height, surface & valid)
    if sigma * 1000 > config["geometry_fallback_noise_sigma_mm"]:
        detections, fallback = detect(
            rgb,
            depth,
            intrinsics,
            transform,
            method="geometry",
            roi=roi,
            inspection_bounds=bounds,
        )
        fallback.update(
            method=method,
            selected_backend="geometry",
            noise_sigma_mm=sigma * 1000,
        )
        return detections, fallback
    support_threshold = max(
        config["support_height_mm"] / 1000,
        config["support_noise_multiplier"] * sigma,
    )
    seed_threshold = max(
        config["seed_height_mm"] / 1000,
        config["seed_noise_multiplier"] * sigma,
    )
    reference_height = float(np.median(height[surface]))
    recessed = height < reference_height - 0.004
    guard_pixels = max(3, int(round(0.003 / scale)))
    surface = surface & ~ndimage.binary_dilation(
        recessed, iterations=guard_pixels
    )
    if method == "multiscale":
        extrapolation = ndimage.distance_transform_edt(
            ~surface, return_distances=False, return_indices=True
        )
        extended_height = height.copy()
        extended_height[~surface] = height[tuple(extrapolation[:, ~surface])]
        residual = _opening_residual(
            extended_height, scale, config["opening_radii_mm"]
        )
    else:
        residual = _robust_residual(height, surface, scale, config)
    support = (residual > support_threshold) & surface & valid
    seeds = (residual > seed_threshold) & support
    mask = ndimage.binary_propagation(seeds, mask=support)
    mask = (
        ndimage.binary_closing(mask, iterations=config["closing_pixels"])
        & surface
        & valid
    )
    components, count = ndimage.label(mask)
    full_features, _ = detect(
        rgb, depth, intrinsics, transform, inspection_bounds=bounds
    )
    preserved = np.zeros_like(surface)
    for feature in full_features:
        if feature.kind == "review" or (
            feature.kind == "preserve"
            and feature.width_mm > 4.2
            and feature.length_mm > 14.0
        ):
            preserved[tuple(feature.pixels.T)] = True
    protected = ndimage.binary_dilation(
        preserved,
        iterations=max(
            1, int(round(config["design_guard_margin_mm"] / 1000 / scale))
        ),
    )
    detections = []
    for component_id in range(1, count + 1):
        pixels = np.argwhere(components == component_id)
        if len(pixels) < 10:
            continue
        points = xyz[tuple(pixels.T)]
        length_mm, width_mm, height_mm = _region_measurements(
            points, pixels, components, component_id, residual, scale
        )
        kind, reason = _classify_region(
            length_mm, width_mm, height_mm, len(pixels)
        )
        surface_height_mm = (
            np.quantile(points[:, 2], 0.95) - reference_height
        ) * 1000
        if (
            kind == "preserve"
            and length_mm < 30.0
            and surface_height_mm
            > CLASSIFICATION_CONFIG["review_surface_height_mm"]
        ):
            kind = "review"
            reason = "compact protrusion exceeds the light-deburring envelope"
        if kind in ("seam", "burr") and (
            height_mm > CLASSIFICATION_CONFIG["review_residual_mm"]
            or surface_height_mm
            > CLASSIFICATION_CONFIG["review_surface_height_mm"]
        ):
            kind = "review"
            reason = "candidate exceeds the light-deburring height envelope"
        permitted = selection[tuple(pixels.T)] & ~protected[tuple(pixels.T)]
        if permitted.sum() < 10:
            continue
        detections.append(
            Detection(
                component_id,
                kind,
                pixels[permitted],
                points[permitted],
                length_mm,
                width_mm,
                height_mm,
                min(0.98, max(0.4, height_mm / 2.5)),
                f"{method}: {reason}",
            )
        )
    observation.update(
        residual=residual,
        mask=mask,
        surface=surface,
        noise_sigma_mm=sigma * 1000,
        support_threshold_mm=support_threshold * 1000,
        seed_threshold_mm=seed_threshold * 1000,
    )
    for feature in full_features:
        if feature.kind not in ("preserve", "review"):
            continue
        permitted = selection[tuple(feature.pixels.T)]
        if permitted.sum() < 10:
            continue
        detections.append(
            Detection(
                count + feature.id,
                feature.kind,
                feature.pixels[permitted],
                feature.xyz[permitted],
                feature.length_mm,
                feature.width_mm,
                feature.height_mm,
                feature.confidence,
                f"{method}: high-contrast full geometry guard",
            )
        )
    observation["preserved_full_features"] = preserved
    return detections, observation
