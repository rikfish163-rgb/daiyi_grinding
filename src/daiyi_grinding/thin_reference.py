"""Registered-reference detector preserving one-pixel positive depth ridges.

The detector consumes sensor observations and a registered normal depth view.
It estimates depth-noise thresholds from paired observations, guards reference
discontinuities, and classifies complete components before permission cropping.
"""

from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from .perception import Detection, roi_mask, unproject


@dataclass(frozen=True)
class ThinReferenceConfig:
    """Metric thresholds and image connectivity chosen on development only."""

    support_mm: float = 0.16
    seed_mm: float = 0.40
    support_noise_multiplier: float = 2.5
    seed_noise_multiplier: float = 4.0
    minimum_pixels: int = 10
    component_join_mm: float = 0.0
    reference_jump_mm: float = 2.0
    reference_guard_pixels: int = 1
    closing_pixels: int = 1
    seam_min_length_mm: float = 16.0
    seam_max_width_mm: float = 4.5
    seam_min_aspect: float = 4.0
    burr_max_length_mm: float = 16.0
    burr_max_width_mm: float = 12.0
    review_height_mm: float = 5.0


THIN_REFERENCE_CONFIG = ThinReferenceConfig()
DEFAULT_BOUNDS = ((-0.67, -0.27, 0.612), (-0.31, 0.03, 0.655))


def configuration():
    """Return the independent JSON-serializable detector configuration."""
    return asdict(THIN_REFERENCE_CONFIG)


def _fill_for_geometry(depth):
    valid = np.isfinite(depth) & (depth > 0.05) & (depth < 10.0)
    if valid.mean() < 0.7:
        raise ValueError("Too few valid depth measurements; reacquire")
    filled = np.asarray(depth, dtype=np.float32).copy()
    if not valid.all():
        nearest = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled[~valid] = filled[tuple(nearest[:, ~valid])]
    return filled, valid


def _measure_region(points, pixels, component, residual, scale):
    centered = points - points.mean(axis=0)
    _, _, directions = np.linalg.svd(centered, full_matrices=False)
    limits = np.quantile(centered @ directions.T, [0.025, 0.975], axis=0)
    length_mm = float(max(limits[1, 0] - limits[0, 0], scale) * 1000)
    row_min, col_min = pixels.min(axis=0)
    row_max, col_max = pixels.max(axis=0) + 1
    local = np.pad(component[row_min:row_max, col_min:col_max], 1)
    distances = ndimage.distance_transform_edt(local)
    width_mm = float(2 * np.quantile(distances[local], 0.90) * scale * 1000)
    height_mm = float(np.quantile(residual[tuple(pixels.T)], 0.95) * 1000)
    return length_mm, width_mm, height_mm


def detect_thin_reference(
    rgb,
    depth,
    reference_depth,
    K,
    T,
    inspection_bounds=DEFAULT_BOUNDS,
    roi=None,
    config=THIN_REFERENCE_CONFIG,
    reference_object_mask=None,
    selection_mask=None,
    allow_sloped_surface=False,
):
    """Extract measurable thin protrusions against a normal registered view.

    Args:
        rgb: Observed RGB image.
        depth: Observed forward camera depth in meters.
        reference_depth: Registered nominal depth with matching camera and pose.
        K: Camera intrinsics, three-by-three matrix.
        T: Camera-to-inspection-frame transform, in meters.
        inspection_bounds: Lower and upper metric XYZ inspection limits.
        roi: Optional permitted polygon in image pixel coordinates.
        config: Frozen development thresholds and geometry classifications.

    Returns:
        Detection objects and planning-compatible measured observation fields.
        The residual retains unsmoothed positive depth differences so a ridge
        narrower than a three-pixel median kernel can remain measurable.
    """
    if (
        depth.ndim != 2
        or rgb.shape[:2] != depth.shape
        or reference_depth.shape != depth.shape
    ):
        raise ValueError("RGB/depth/reference shape mismatch")
    intrinsics = np.asarray(K, dtype=float)
    transform = np.asarray(T, dtype=float)
    limits = np.asarray(inspection_bounds, dtype=float)
    if (
        intrinsics.shape != (3, 3)
        or transform.shape != (4, 4)
        or not np.isfinite(intrinsics).all()
        or not np.isfinite(transform).all()
        or intrinsics[0, 0] <= 0
        or intrinsics[1, 1] <= 0
    ):
        raise ValueError("Invalid camera intrinsics or transform")
    if (
        limits.shape != (2, 3)
        or not np.isfinite(limits).all()
        or np.any(limits[0] >= limits[1])
    ):
        raise ValueError("Inspection bounds require ordered finite XYZ limits")
    filled, valid = _fill_for_geometry(depth)
    reference, reference_valid = _fill_for_geometry(reference_depth)
    xyz = unproject(filled, intrinsics, transform)
    reference_xyz = unproject(reference, intrinsics, transform)
    surface = np.all(
        (reference_xyz > limits[0]) & (reference_xyz < limits[1]), axis=-1
    )
    if reference_object_mask is not None:
        object_mask = np.asarray(reference_object_mask, dtype=bool)
        if object_mask.shape != depth.shape:
            raise ValueError("Reference object mask shape mismatch")
        surface &= object_mask
    local_range = ndimage.maximum_filter(reference, size=3) - (
        ndimage.minimum_filter(reference, size=3)
    )
    discontinuities = local_range > config.reference_jump_mm / 1000
    if allow_sloped_surface:
        # Inverse depth is affine on a perspective image of a plane. Its
        # second difference detects a real edge without rejecting every pixel
        # of a steep, otherwise smooth, camera-facing CAD wall.
        inverse = 1 / np.maximum(reference, 1e-5)
        horizontal = np.pad(
            np.abs(np.diff(inverse, n=2, axis=1)), ((0, 0), (1, 1))
        )
        vertical = np.pad(
            np.abs(np.diff(inverse, n=2, axis=0)), ((1, 1), (0, 0))
        )
        discontinuities = (
            horizontal + vertical
        ) * reference**2 > config.reference_jump_mm / 1000
    if config.reference_guard_pixels:
        discontinuities = ndimage.binary_dilation(
            discontinuities, iterations=config.reference_guard_pixels
        )
    paired = surface & valid & reference_valid & ~discontinuities
    selection = roi_mask(depth.shape, roi)
    if selection_mask is not None:
        extra_selection = np.asarray(selection_mask, dtype=bool)
        if extra_selection.shape != depth.shape:
            raise ValueError("Selection mask shape mismatch")
        selection &= extra_selection
    scale = (
        float(np.median(filled[paired]) / intrinsics[0, 0])
        if paired.any()
        else 0.0004
    )
    signed_difference = (reference - filled).astype(np.float32)
    bias = float(np.median(signed_difference[paired])) if paired.any() else 0.0
    signed_difference -= bias
    negative = signed_difference[paired & (signed_difference <= 0)]
    noise_sigma = (
        float(np.median(np.abs(negative)) / 0.67449) if len(negative) else 0.0
    )
    support_threshold = max(
        config.support_mm / 1000,
        config.support_noise_multiplier * noise_sigma,
    )
    seed_threshold = max(
        config.seed_mm / 1000,
        config.seed_noise_multiplier * noise_sigma,
    )
    support = (signed_difference > support_threshold) & paired
    seeds = (signed_difference > seed_threshold) & paired
    # Keep weak measured fragments until metric connectivity can associate
    # them with a strong seed. No samples are added across the gaps.
    connected = support.copy()
    if config.closing_pixels:
        connected = ndimage.binary_closing(
            connected,
            iterations=config.closing_pixels,
            structure=np.ones((3, 3), bool),
        )
    connected &= paired & support
    components, count = ndimage.label(
        connected, structure=np.ones((3, 3), np.uint8)
    )
    if config.component_join_mm > 0 and count > 1:
        # Join nearby measured positive components, without inventing material
        # in intervening missing pixels or changing the registered CAD.
        pixels = np.argwhere(connected)
        identities = components[tuple(pixels.T)]
        neighbors = cKDTree(reference_xyz[tuple(pixels.T)]).query_pairs(
            config.component_join_mm / 1000, output_type="ndarray"
        )
        parent = np.arange(count + 1)

        def representative(identity):
            while parent[identity] != identity:
                parent[identity] = parent[parent[identity]]
                identity = parent[identity]
            return identity

        for first, second in np.unique(identities[neighbors], axis=0):
            a, b = representative(first), representative(second)
            if a != b:
                parent[b] = a
        joined = np.array([representative(index) for index in range(count + 1)])
        roots, dense = np.unique(joined[1:], return_inverse=True)
        mapping = np.r_[0, dense + 1]
        components = mapping[components]
        count = len(roots)
    seeded_components = np.unique(components[seeds & connected])
    mapping = np.zeros(count + 1, dtype=np.intp)
    mapping[seeded_components] = np.arange(1, len(seeded_components) + 1)
    components = mapping[components]
    count = len(seeded_components)
    residual = np.maximum(signed_difference, 0.0)
    detections = []
    unresolved_components = []
    selected_mask = np.zeros(depth.shape, bool)
    for component_id in range(1, count + 1):
        component = components == component_id
        pixels = np.argwhere(component)
        if len(pixels) < config.minimum_pixels:
            permitted = selection[tuple(pixels.T)]
            selected = pixels[permitted]
            if len(selected):
                values = residual[tuple(pixels.T)] * 1000
                unresolved_components.append(
                    {
                        "component_id": component_id,
                        "reason": "strong_seed_insufficient_support",
                        "pixels": selected.tolist(),
                        "nominal_xyz": reference_xyz[
                            tuple(selected.T)
                        ].tolist(),
                        "seed_count": int((seeds & component).sum()),
                        "support_count": len(pixels),
                        "permitted_pixel_count": len(selected),
                        "minimum_pixels": config.minimum_pixels,
                        "optical_residual_range_mm": [
                            float(values.min()),
                            float(values.max()),
                        ],
                    }
                )
            continue
        points = xyz[tuple(pixels.T)]
        length_mm, width_mm, height_mm = _measure_region(
            points, pixels, component, residual, scale
        )
        if height_mm > config.review_height_mm:
            kind = "review"
            reason = "positive material exceeds the deburring height envelope"
        elif (
            length_mm > config.seam_min_length_mm
            and width_mm < config.seam_max_width_mm
            and length_mm / max(width_mm, 0.01) > config.seam_min_aspect
        ):
            kind = "seam"
            reason = (
                "thin positive connected ridge against registered reference"
            )
        elif (
            length_mm < config.burr_max_length_mm
            and width_mm < config.burr_max_width_mm
        ):
            kind = "burr"
            reason = "compact positive material against registered reference"
        else:
            kind = "preserve"
            reason = "broad mismatch needs registration or design inspection"
        permitted = selection[tuple(pixels.T)]
        if permitted.sum() < config.minimum_pixels:
            continue
        chosen_pixels = pixels[permitted]
        selected_mask[tuple(chosen_pixels.T)] = True
        detections.append(
            Detection(
                id=component_id,
                kind=kind,
                pixels=chosen_pixels,
                xyz=points[permitted],
                length_mm=length_mm,
                width_mm=width_mm,
                height_mm=height_mm,
                confidence=float(np.clip(height_mm / 2.5, 0.4, 0.98)),
                reason=reason,
            )
        )
    return detections, {
        "xyz": xyz,
        "reference_xyz": reference_xyz,
        "height": xyz[..., 2].astype(np.float32),
        "residual": residual,
        "mask": selected_mask,
        "surface": paired,
        "scale": scale,
        "selection": selection,
        "K": intrinsics,
        "T": transform,
        "reference_required": True,
        "reference_registration": "same camera and workpiece pose required",
        "noise_sigma_mm": noise_sigma * 1000,
        "bias_mm": bias * 1000,
        "support_threshold_mm": support_threshold * 1000,
        "seed_threshold_mm": seed_threshold * 1000,
        "reference_discontinuity_guard": discontinuities,
        "unresolved_components": unresolved_components,
    }
