"""Sensor-only signed reinspection of registered nominal-surface anchors."""

from numbers import Real

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .perception import project

MAX_ANCHORS = 96
REFERENCE_MATCH_M = 0.0015
MIN_VIEW_COSINE = 0.25
MIN_NORMAL_LENGTH = 0.9


def _validated_targets(targets):
    validated = []
    for target in targets:
        points = np.asarray(target["points"], float)
        normals = np.asarray(target["normals"], float)
        if (
            points.ndim != 2
            or points.shape[1] != 3
            or normals.shape != points.shape
            or not len(points)
            or not np.isfinite(points).all()
            or not np.isfinite(normals).all()
            or np.any(np.linalg.norm(normals, axis=1) <= MIN_NORMAL_LENGTH)
        ):
            raise ValueError("Targets require finite paired XYZ and normals")
        validated.append(
            {
                **target,
                "points": points,
                "normals": normals / np.linalg.norm(normals, axis=1)[:, None],
            }
        )
    return validated


def _observation_shape_or_raise(observation):
    required = (
        "T",
        "K",
        "surface",
        "selection",
        "reference_xyz",
        "xyz",
        "noise_sigma_mm",
    )
    missing = [name for name in required if name not in observation]
    if missing:
        raise ValueError(f"Observation is missing {', '.join(missing)}")
    camera = np.asarray(observation["T"], float)
    intrinsics = np.asarray(observation["K"], float)
    surface = np.asarray(observation["surface"])
    selection = np.asarray(observation["selection"])
    reference = np.asarray(observation["reference_xyz"], float)
    measured = np.asarray(observation["xyz"], float)
    noise = observation["noise_sigma_mm"]
    if (
        camera.shape != (4, 4)
        or intrinsics.shape != (3, 3)
        or not np.isfinite(camera).all()
        or not np.isfinite(intrinsics).all()
        or not np.allclose(camera[3], [0, 0, 0, 1])
        or not np.allclose(
            camera[:3, :3].T @ camera[:3, :3], np.eye(3), atol=1e-6
        )
        or not np.isclose(np.linalg.det(camera[:3, :3]), 1.0, atol=1e-6)
        or not np.allclose(intrinsics[2], [0, 0, 1])
        or intrinsics[0, 0] <= 0
        or intrinsics[1, 1] <= 0
        or surface.shape != selection.shape
        or surface.ndim != 2
        or surface.dtype != np.bool_
        or selection.dtype != np.bool_
        or reference.shape != measured.shape
        or reference.ndim != 3
        or reference.shape[:2] != surface.shape
        or reference.shape[2] != 3
        or isinstance(noise, (bool, np.bool_))
        or not isinstance(noise, Real)
        or not np.isfinite(noise)
        or noise < 0
    ):
        raise ValueError("Observation schema, camera or noise is invalid")
    return camera, intrinsics, surface.shape


def surface_anchors(detection, observation):
    """Save referred CAD samples, independent of future detector outputs."""
    rows, cols = detection.pixels.T
    points = observation["reference_xyz"][rows, cols]
    normals = observation["normals"][rows, cols]
    valid = (
        np.isfinite(points).all(axis=1)
        & np.isfinite(normals).all(axis=1)
        & (np.linalg.norm(normals, axis=1) > MIN_NORMAL_LENGTH)
    )
    points, normals = points[valid], normals[valid]
    if len(points) > MAX_ANCHORS:
        keep = np.linspace(0, len(points) - 1, MAX_ANCHORS, dtype=int)
        points, normals = points[keep], normals[keep]
    return points, normals


def add_targets(targets, candidates):
    """Keep independent newly observed regions in subsequent scan rounds."""
    saved = list(targets)
    for candidate in candidates:
        points = candidate["points"]
        if not len(points):
            continue
        already_registered = any(
            len(target["points"])
            and np.mean(cKDTree(target["points"]).query(points)[0] <= 0.002)
            >= 0.75
            for target in saved
        )
        if not already_registered:
            saved.append(
                {
                    **candidate,
                    "id": max((t["id"] for t in saved), default=0) + 1,
                }
            )
    return saved


class Reinspection:
    """Measure saved CAD regions across views against signed tolerance.

    Positive residual is material above the nominal surface; negative residual
    is overgrinding. Each view contributes its extreme residual and an
    uncalibrated +/-2 reported-sigma interval. Missing anchors always hold the
    region rather than being inferred from neighbouring measurements.
    """

    def __init__(self, targets):
        self.targets = _validated_targets(targets)
        self.lower = [
            np.full(len(row["points"]), np.inf) for row in self.targets
        ]
        self.upper = [
            np.full(len(row["points"]), -np.inf) for row in self.targets
        ]
        self.heights = [values.copy() for values in self.upper]
        self.troughs = [values.copy() for values in self.lower]
        self.views = [set() for _ in self.targets]
        self.last_camera = None
        self.invalid_observations = []

    def observe(self, observation, view_index):
        """Absorb one sensor array view and return measured anchor count."""
        try:
            camera, intrinsics, image_shape = _observation_shape_or_raise(
                observation
            )
        except ValueError as error:
            self.invalid_observations.append(
                {"view": view_index, "reason": str(error)}
            )
            return 0
        width, height = image_shape[::-1]
        self.last_camera = camera
        measured_count = 0
        for index, target in enumerate(self.targets):
            points, normals = target["points"], target["normals"]
            facing = camera[:3, 3] - points
            directions = facing / np.maximum(
                np.linalg.norm(facing, axis=1, keepdims=True), 1e-12
            )
            with np.errstate(divide="ignore", invalid="ignore"):
                uv = project(points, intrinsics, camera)
            valid = (
                np.isfinite(uv).all(axis=1)
                & (uv[:, 0] >= 0)
                & (uv[:, 0] < width - 0.5)
                & (uv[:, 1] >= 0)
                & (uv[:, 1] < height - 0.5)
                & (facing @ camera[:3, 2] < 0)
                & (
                    np.einsum("ij,ij->i", normals, directions)
                    >= MIN_VIEW_COSINE
                )
            )
            indices = np.flatnonzero(valid)
            u, v = np.rint(uv[indices]).astype(int).T
            reference = observation["reference_xyz"][v, u]
            selected_measured = observation["xyz"][v, u]
            paired = (
                observation["surface"][v, u]
                & observation["selection"][v, u]
                & np.isfinite(reference).all(axis=1)
                & np.isfinite(selected_measured).all(axis=1)
                & (
                    np.linalg.norm(reference - points[indices], axis=1)
                    <= REFERENCE_MATCH_M
                )
            )
            indices = indices[paired]
            u, v = u[paired], v[paired]
            displacement = selected_measured[paired] - reference[paired]
            residual = (
                np.einsum("ij,ij->i", displacement, normals[indices]) * 1000
            )
            finite = np.isfinite(residual)
            indices, residual = indices[finite], residual[finite]
            if len(indices):
                self.views[index].add(view_index)
                self.troughs[index][indices] = np.minimum(
                    self.troughs[index][indices], residual
                )
                self.heights[index][indices] = np.maximum(
                    self.heights[index][indices], residual
                )
                self.lower[index][indices] = np.minimum(
                    self.lower[index][indices],
                    residual - 2 * observation["noise_sigma_mm"],
                )
                self.upper[index][indices] = np.maximum(
                    self.upper[index][indices],
                    residual + 2 * observation["noise_sigma_mm"],
                )
                measured_count += len(indices)
        return measured_count

    def supplementary_angle(self, current_angle, choices):
        """Prefer a view facing the largest number of unmeasured anchors."""
        if self.last_camera is None:
            return None
        unseen_targets = []
        for target, lower, upper in zip(self.targets, self.lower, self.upper):
            unseen = ~(np.isfinite(lower) | np.isfinite(upper))
            unseen_targets.append((target, unseen))
        counts = {}
        for angle in choices:
            rotation = Rotation.from_euler(
                "z", current_angle - angle, degrees=True
            )
            camera_position = rotation.apply(self.last_camera[:3, 3])
            score = 0
            for target, unseen in unseen_targets:
                toward = camera_position - target["points"][unseen]
                lengths = np.maximum(np.linalg.norm(toward, axis=1), 1e-12)
                cosine = (
                    np.einsum("ij,ij->i", target["normals"][unseen], toward)
                    / lengths
                )
                score += int((cosine >= MIN_VIEW_COSINE).sum())
            counts[angle] = score
        best = max(choices, key=counts.get, default=None)
        return best if best is not None and counts[best] else None

    def report(self, tolerance_mm, *, whole_workpiece_scan=True):
        """Return signed per-region bounds, coverage and hold decisions."""
        if (
            isinstance(tolerance_mm, (bool, np.bool_))
            or not isinstance(tolerance_mm, Real)
            or not np.isfinite(tolerance_mm)
            or tolerance_mm <= 0
        ):
            raise ValueError(
                "Signed tolerance must be a positive finite number"
            )
        upper_limit_mm = float(tolerance_mm)
        lower_limit_mm = -abs(upper_limit_mm)
        rows = []
        for target, lower, upper, heights, troughs, views in zip(
            self.targets,
            self.lower,
            self.upper,
            self.heights,
            self.troughs,
            self.views,
        ):
            measured = np.isfinite(upper) | np.isfinite(lower)
            peak = float(heights[measured].max()) if measured.any() else None
            trough = float(troughs[measured].min()) if measured.any() else None
            upper_peak = (
                float(upper[measured].max()) if measured.any() else None
            )
            lower_peak = (
                float(lower[measured].min()) if measured.any() else None
            )
            upper_excess = peak is not None and peak > upper_limit_mm
            lower_excess = trough is not None and trough < lower_limit_mm
            upper_uncertain = (
                upper_peak is not None and upper_peak > upper_limit_mm
            )
            lower_uncertain = (
                lower_peak is not None and lower_peak < lower_limit_mm
            )
            if upper_excess or lower_excess:
                state = "needs_rework"
                reason = "overgrind" if lower_excess else "positive_excess"
            elif not len(upper) or not measured.all():
                state = "insufficient_observation"
                reason = "missing_anchor"
            elif upper_uncertain or lower_uncertain:
                state = "uncertain_measurement"
                reason = (
                    "upper_uncertainty"
                    if upper_uncertain
                    else "lower_uncertainty"
                )
            else:
                state = "verified"
                reason = "signed_interval_within_limits"
            signed_peak = None
            if peak is not None and trough is not None:
                signed_peak = peak if abs(peak) >= abs(trough) else trough
            anchor_bounds = [
                [float(lower[index]), float(upper[index])]
                if np.isfinite(lower[index]) and np.isfinite(upper[index])
                else [None, None]
                for index in range(len(upper))
            ]
            rows.append(
                {
                    "id": target["id"],
                    "kind": target["kind"],
                    "status": state,
                    "status_reason": reason,
                    "positive_excess": bool(upper_excess),
                    "overgrind": bool(lower_excess),
                    "negative_limit_risk": bool(lower_uncertain),
                    "observations": sorted(views),
                    "anchors": len(upper),
                    "observed_anchors": int(measured.sum()),
                    "anchor_coverage": (
                        float(measured.mean()) if len(upper) else 0.0
                    ),
                    "peak_mm": peak,
                    "trough_mm": trough,
                    "signed_peak_mm": signed_peak,
                    "lower_peak_mm": lower_peak,
                    "upper_peak_mm": upper_peak,
                    "anchor_bounds_mm": anchor_bounds,
                }
            )
        pending = [r["status"] for r in rows if r["status"] != "verified"]
        uncertain = pending.count("uncertain_measurement")
        overgrind = sum(row["overgrind"] for row in rows)
        negative_limit_risk = sum(row["negative_limit_risk"] for row in rows)
        return {
            "regions": rows,
            "verified": len(rows) - len(pending),
            "needs_rework": pending.count("needs_rework"),
            "uncertain": uncertain,
            "overgrind": overgrind,
            "negative_limit_risk": negative_limit_risk,
            "automatic_regrind_allowed": not negative_limit_risk
            and not self.invalid_observations,
            "insufficient": len(pending)
            - pending.count("needs_rework")
            - uncertain,
            "coverage_scope": "six_view"
            if whole_workpiece_scan
            else "limited_view_or_roi",
            "signed_limits_mm": {
                "lower": lower_limit_mm,
                "upper": upper_limit_mm,
            },
            "uncertainty_model": {
                "sigma_multiplier": 2,
                "source": "reported sensor noise only",
                "calibrated": False,
            },
            "invalid_observations": self.invalid_observations,
            "truth_used_for_decision": False,
            "observational_release": bool(rows)
            and not pending
            and not self.invalid_observations
            and whole_workpiece_scan,
        }
