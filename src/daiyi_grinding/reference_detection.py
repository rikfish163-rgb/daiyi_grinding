"""Detect positive excess material against a registered nominal depth view."""

from dataclasses import asdict, dataclass

import cv2
import numpy as np
from scipy import ndimage

from .perception import Detection, roi_mask, unproject


@dataclass(frozen=True)
class ReferenceConfig:
    """Metric thresholds for a registered reference, independent of labels."""

    low_mm: float = 0.18
    high_mm: float = 0.45
    noise_multiplier: float = 4.0
    minimum_pixels: int = 10
    seam_min_length_mm: float = 16.0
    seam_max_width_mm: float = 4.5
    seam_min_aspect: float = 4.0
    burr_max_length_mm: float = 16.0
    burr_max_width_mm: float = 12.0
    review_height_mm: float = 5.0


REFERENCE_CONFIG = ReferenceConfig()
DEFAULT_BOUNDS = ((-0.67, -0.27, 0.612), (-0.31, 0.03, 0.655))


def configuration():
    """Return the configuration recorded before holdout evaluation."""
    return asdict(REFERENCE_CONFIG)


def _fill_depth(depth):
    valid = np.isfinite(depth) & (depth > 0.05) & (depth < 5.0)
    if valid.mean() < 0.7:
        raise ValueError("Too few valid depth measurements; reacquire")
    filled = np.asarray(depth, dtype=np.float32).copy()
    if not valid.all():
        nearest = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled[~valid] = filled[tuple(nearest[:, ~valid])]
    return filled, valid


def detect_reference(
    rgb,
    depth,
    reference_depth,
    K,
    T,
    roi=None,
    inspection_bounds=DEFAULT_BOUNDS,
    config=REFERENCE_CONFIG,
):
    """Find positive residual regions from registered RGB-D observations.

    Args:
        rgb: Captured RGB image used for overlays and shape validation.
        depth: Observed camera-forward depth in meters.
        reference_depth: Independently acquired nominal depth in the same
            camera geometry and registered workpiece pose. This is an extra
            input required by this detector, not an evaluation label.
        K: Pinhole camera intrinsics.
        T: Camera-to-world transform in meters.
        roi: Optional polygon in image pixel coordinates.
        inspection_bounds: Configured workpiece inspection volume in meters.
        config: Thresholds fixed using development observations.

    Returns:
        Detection regions and the measured geometry used by the planner.
    """
    if (
        depth.ndim != 2
        or rgb.shape[:2] != depth.shape
        or reference_depth.shape != depth.shape
    ):
        raise ValueError("RGB/depth/reference shape mismatch")
    filled, valid = _fill_depth(depth)
    reference, reference_valid = _fill_depth(reference_depth)
    xyz = unproject(filled, K, T)
    height = cv2.medianBlur(xyz[..., 2].astype(np.float32), 3)
    lower, upper = np.asarray(inspection_bounds, dtype=float)
    if lower.shape != (3,) or upper.shape != (3,) or (lower >= upper).any():
        raise ValueError("Inspection bounds must contain ordered xyz limits")
    surface = ((xyz > lower) & (xyz < upper)).all(axis=-1)
    surface = ndimage.binary_erosion(surface, iterations=2)
    selection = roi_mask(depth.shape, roi)
    residual = cv2.medianBlur((reference - filled).astype(np.float32), 3)
    paired_valid = surface & valid & reference_valid
    scale = (
        float(np.median(filled[surface]) / K[0, 0]) if surface.any() else 0.0004
    )
    if paired_valid.any():
        negative = residual[paired_valid & (residual <= 0)]
        noise_sigma = (
            float(np.median(np.abs(negative)) / 0.67449)
            if len(negative)
            else 0.0
        )
    else:
        noise_sigma = 0.0
    low_threshold = max(config.low_mm / 1000, 2.5 * noise_sigma)
    high_threshold = max(
        config.high_mm / 1000, config.noise_multiplier * noise_sigma
    )
    weak = (residual > low_threshold) & paired_valid
    strong = (residual > high_threshold) & paired_valid
    support = ndimage.binary_closing(weak, iterations=1) & surface
    components, count = ndimage.label(support)
    detections = []
    selected_mask = np.zeros(depth.shape, dtype=bool)
    for component_id in range(1, count + 1):
        component = components == component_id
        if not (component & strong).any():
            continue
        pixels = np.argwhere(component & paired_valid)
        if len(pixels) < config.minimum_pixels:
            continue
        points = xyz[tuple(pixels.T)]
        centered = points - points.mean(axis=0)
        _, _, directions = np.linalg.svd(centered, full_matrices=False)
        projected = centered @ directions.T
        bounds = np.quantile(projected, [0.025, 0.975], axis=0)
        length_mm = float(max(bounds[1, 0] - bounds[0, 0], scale) * 1000)
        distances = ndimage.distance_transform_edt(component)
        width_mm = float(
            2 * np.quantile(distances[component], 0.90) * scale * 1000
        )
        height_mm = float(np.quantile(residual[component], 0.95) * 1000)
        if height_mm > config.review_height_mm:
            kind = "review"
            reason = "positive reference residual exceeds deburring envelope"
        elif (
            length_mm > config.seam_min_length_mm
            and width_mm < config.seam_max_width_mm
            and length_mm / max(width_mm, 0.01) > config.seam_min_aspect
        ):
            kind = "seam"
            reason = "elongated positive residual against nominal CAD view"
        elif (
            length_mm < config.burr_max_length_mm
            and width_mm < config.burr_max_width_mm
        ):
            kind = "burr"
            reason = "localized positive residual against nominal CAD view"
        else:
            kind = "preserve"
            reason = "reference mismatch requires review of registration/design"
        chosen = selection[tuple(pixels.T)]
        if chosen.sum() < config.minimum_pixels:
            continue
        pixels, points = pixels[chosen], points[chosen]
        selected_mask[tuple(pixels.T)] = True
        detections.append(
            Detection(
                id=component_id,
                kind=kind,
                pixels=pixels,
                xyz=points,
                length_mm=length_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                confidence=float(
                    np.clip(height_mm / max(config.high_mm, 0.01), 0.4, 0.98)
                ),
                reason=reason,
            )
        )
    return detections, {
        "xyz": xyz,
        "height": height,
        "residual": residual,
        "mask": selected_mask,
        "surface": surface,
        "scale": scale,
        "selection": selection,
        "K": K,
        "T": T,
        "reference_required": True,
        "reference_registration": "same camera and workpiece pose required",
        "noise_sigma_mm": noise_sigma * 1000,
        "low_threshold_mm": low_threshold * 1000,
        "high_threshold_mm": high_threshold * 1000,
    }
