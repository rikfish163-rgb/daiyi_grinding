"""Sensor-only signed reinspection of registered nominal-surface anchors."""

from numbers import Integral, Real

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .perception import project

MAX_ANCHORS = 96
MAX_BASELINE_PER_SIDE = 24
MAX_PROTECTED_ANCHORS = 24
REFERENCE_MATCH_M = 0.0015
MIN_VIEW_COSINE = 0.25
MIN_NORMAL_LENGTH = 0.9


def _target_binormal(points, normals):
    """Return a unit cross-band axis orthogonal to the mean normal and tangent."""
    mean_normal = normals.mean(axis=0)
    normal_norm = np.linalg.norm(mean_normal)
    if normal_norm <= 1e-9:
        return np.array([1.0, 0.0, 0.0])
    mean_normal = mean_normal / normal_norm
    centered = points - points.mean(axis=0)
    if len(points) >= 2 and np.linalg.norm(centered) > 1e-9:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        tangent = vt[0]
        tangent = tangent - np.dot(tangent, mean_normal) * mean_normal
        tangent_norm = np.linalg.norm(tangent)
        if tangent_norm > 1e-9:
            tangent = tangent / tangent_norm
            binormal = np.cross(mean_normal, tangent)
            binormal_norm = np.linalg.norm(binormal)
            if binormal_norm > 1e-9:
                return binormal / binormal_norm
    fallback = np.array([1.0, 0.0, 0.0])
    if abs(np.dot(fallback, mean_normal)) > 0.9:
        fallback = np.array([0.0, 1.0, 0.0])
    axis = np.cross(mean_normal, fallback)
    return axis / np.linalg.norm(axis)


def _append_spaced_anchor(bucket, anchor, *, limit, min_spacing_m):
    if len(bucket) >= limit:
        return
    point = np.asarray(anchor["point_m"], dtype=float)
    for existing in bucket:
        if np.linalg.norm(point - existing["point_m"]) < min_spacing_m:
            return
    bucket.append(anchor)


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
    if not isinstance(observation, dict):
        raise ValueError("Observation must be an object")
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
    numeric = {
        name: np.asarray(observation[name])
        for name in ("T", "K", "reference_xyz", "xyz")
    }
    if any(array.dtype.kind not in "iuf" for array in numeric.values()):
        raise ValueError("Observation numeric arrays require real numbers")
    camera = np.asarray(numeric["T"], float)
    intrinsics = np.asarray(numeric["K"], float)
    surface = np.asarray(observation["surface"])
    selection = np.asarray(observation["selection"])
    reference = np.asarray(numeric["reference_xyz"], float)
    measured = np.asarray(numeric["xyz"], float)
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
    sample_counts = np.asarray(
        observation.get("depth_sample_counts", np.ones(surface.shape, int))
    )
    frame_count = observation.get("depth_frame_count", 1)
    if (
        isinstance(frame_count, (bool, np.bool_))
        or not isinstance(frame_count, Integral)
        or frame_count < 1
        or sample_counts.shape != surface.shape
        or sample_counts.dtype.kind not in "iu"
        or np.any(sample_counts < 0)
        or np.any(sample_counts > frame_count)
    ):
        raise ValueError("Repeated-depth frame and sample counts are invalid")
    return {
        **observation,
        "T": camera,
        "K": intrinsics,
        "surface": surface,
        "selection": selection,
        "reference_xyz": reference,
        "xyz": measured,
        "noise_sigma_mm": float(noise),
        "depth_sample_counts": sample_counts,
        "depth_frame_count": int(frame_count),
    }


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
    uncalibrated +/-2 interval after projecting reported depth noise onto the
    anchor normal. Fixed-pixel repeat means arrive with their effective sigma;
    measurements from different views retain their extreme bounds. Missing
    anchors always hold rather than being inferred from neighbours.
    """

    def __init__(
        self,
        targets,
        *,
        protected_zones=(),
        coordinate_frame="workpiece_097",
        exclusion_radius_m=0.015,
        baseline_outer_radius_m=0.060,
    ):
        self.targets = _validated_targets(targets)
        self.protected_zones = tuple(protected_zones)
        self.coordinate_frame = str(coordinate_frame)
        self.exclusion_radius_m = float(exclusion_radius_m)
        self.baseline_outer_radius_m = float(baseline_outer_radius_m)
        self.lower = [
            np.full(len(row["points"]), np.inf) for row in self.targets
        ]
        self.upper = [
            np.full(len(row["points"]), -np.inf) for row in self.targets
        ]
        self.heights = [values.copy() for values in self.upper]
        self.troughs = [values.copy() for values in self.lower]
        self.views = [set() for _ in self.targets]
        self.sample_counts = [{} for _ in self.targets]
        self.positive_lower = [values.copy() for values in self.upper]
        self.positive_estimates = [values.copy() for values in self.upper]
        self.positive_counts = [
            np.zeros(len(row["points"]), dtype=int) for row in self.targets
        ]
        self.positive_views = [
            [None] * len(row["points"]) for row in self.targets
        ]
        self._target_points = (
            np.vstack([row["points"] for row in self.targets])
            if self.targets
            else np.empty((0, 3), dtype=float)
        )
        self._target_binormals = (
            np.vstack(
                [
                    np.repeat(
                        _target_binormal(row["points"], row["normals"])[
                            None, :
                        ],
                        len(row["points"]),
                        axis=0,
                    )
                    for row in self.targets
                ]
            )
            if self.targets
            else np.empty((0, 3), dtype=float)
        )
        self._baseline_by_side = {-1: [], 1: []}
        self._protected_anchors = []
        self.last_camera = None
        self.invalid_observations = []

    def _observe_local_baseline(self, observation, excluded_mask):
        """Collect untouched outside-band bilateral nominal anchors from depth."""
        raw_normals = observation.get("normals")
        if raw_normals is None or not len(self._target_points):
            return
        normals_map = np.asarray(raw_normals, dtype=float)
        surface = observation["surface"]
        if normals_map.shape != (*surface.shape, 3):
            return
        valid = (
            surface
            & observation["selection"]
            & (observation["depth_sample_counts"] > 0)
            & np.isfinite(observation["reference_xyz"]).all(axis=2)
            & np.isfinite(observation["xyz"]).all(axis=2)
            & np.isfinite(normals_map).all(axis=2)
        )
        if excluded_mask is not None:
            excluded = np.asarray(excluded_mask, dtype=bool)
            if excluded.shape == surface.shape:
                valid = valid & ~excluded
        rows, cols = np.nonzero(valid)
        if not len(rows):
            return
        if len(rows) > 2400:
            keep = np.linspace(0, len(rows) - 1, 2400, dtype=int)
            rows, cols = rows[keep], cols[keep]
        ref = observation["reference_xyz"][rows, cols]
        meas = observation["xyz"][rows, cols]
        normals = normals_map[rows, cols]
        normal_len = np.linalg.norm(normals, axis=1)
        good_n = normal_len > MIN_NORMAL_LENGTH
        if not good_n.any():
            return
        rows, cols = rows[good_n], cols[good_n]
        ref, meas = ref[good_n], meas[good_n]
        normals = normals[good_n] / normal_len[good_n][:, None]
        camera, intrinsics = observation["T"], observation["K"]
        facing = camera[:3, 3] - ref
        facing_len = np.maximum(
            np.linalg.norm(facing, axis=1, keepdims=True), 1e-12
        )
        cosine = np.einsum("ij,ij->i", normals, facing / facing_len)
        good_v = cosine >= MIN_VIEW_COSINE
        if not good_v.any():
            return
        rows, cols = rows[good_v], cols[good_v]
        ref, meas, normals = ref[good_v], meas[good_v], normals[good_v]
        tree = cKDTree(self._target_points)
        dist, nearest = tree.query(ref)
        rays = (
            np.column_stack(
                (
                    (cols - intrinsics[0, 2]) / intrinsics[0, 0],
                    (rows - intrinsics[1, 2]) / intrinsics[1, 1],
                    np.ones(len(cols)),
                )
            )
            @ camera[:3, :3].T
        )
        residual_mm = np.einsum("ij,ij->i", meas - ref, normals) * 1000.0
        sigma_mm = np.maximum(
            observation["noise_sigma_mm"]
            * np.abs(np.einsum("ij,ij->i", rays, normals)),
            1e-4,
        )
        band = (
            (dist >= self.exclusion_radius_m)
            & (dist <= self.baseline_outer_radius_m)
            & np.isfinite(residual_mm)
            & np.isfinite(sigma_mm)
        )
        for idx in np.flatnonzero(band):
            if (
                len(self._baseline_by_side[-1]) >= MAX_BASELINE_PER_SIDE
                and len(self._baseline_by_side[1]) >= MAX_BASELINE_PER_SIDE
            ):
                break
            delta = ref[idx] - self._target_points[nearest[idx]]
            lateral = float(np.dot(delta, self._target_binormals[nearest[idx]]))
            if abs(lateral) < 0.004:
                continue
            side = 1 if lateral > 0 else -1
            _append_spaced_anchor(
                self._baseline_by_side[side],
                {
                    "height_mm": float(residual_mm[idx]),
                    "sigma_mm": float(sigma_mm[idx]),
                    "side": side,
                    "point_m": ref[idx].tolist(),
                },
                limit=MAX_BASELINE_PER_SIDE,
                min_spacing_m=0.006,
            )
        if (
            self.protected_zones
            and len(self._protected_anchors) < MAX_PROTECTED_ANCHORS
        ):
            outside = (
                (dist >= self.exclusion_radius_m)
                & np.isfinite(residual_mm)
                & np.isfinite(sigma_mm)
            )
            for idx in np.flatnonzero(outside):
                if len(self._protected_anchors) >= MAX_PROTECTED_ANCHORS:
                    break
                point = ref[idx]
                in_protected = any(
                    np.all(point >= zone.bounds_m[0])
                    and np.all(point <= zone.bounds_m[1])
                    for zone in self.protected_zones
                )
                if not in_protected:
                    continue
                _append_spaced_anchor(
                    self._protected_anchors,
                    {
                        "height_mm": float(residual_mm[idx]),
                        "sigma_mm": float(sigma_mm[idx]),
                        "point_m": point.tolist(),
                    },
                    limit=MAX_PROTECTED_ANCHORS,
                    min_spacing_m=0.010,
                )

    def observe(self, observation, view_index, *, excluded_mask=None):
        """Absorb one sensor array view and return measured anchor count."""
        try:
            observation = _observation_shape_or_raise(observation)
        except (TypeError, ValueError, OverflowError) as error:
            self.invalid_observations.append(
                {"view": view_index, "reason": str(error)}
            )
            return 0
        camera, intrinsics = observation["T"], observation["K"]
        image_shape = observation["surface"].shape
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
                & (observation["depth_sample_counts"][v, u] > 0)
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
            u, v = u[finite], v[finite]
            if len(indices):
                rays = (
                    np.column_stack(
                        (
                            (u - intrinsics[0, 2]) / intrinsics[0, 0],
                            (v - intrinsics[1, 2]) / intrinsics[1, 1],
                            np.ones(len(u)),
                        )
                    )
                    @ camera[:3, :3].T
                )
                # The sensor sigma is along camera Z, not the CAD normal.
                normal_sigma = observation["noise_sigma_mm"] * np.abs(
                    np.einsum("ij,ij->i", rays, normals[indices])
                )
                lower = residual - 2 * normal_sigma
                upper = residual + 2 * normal_sigma
                self.views[index].add(view_index)
                view_counts = self.sample_counts[index].setdefault(
                    view_index, np.zeros(len(points), dtype=int)
                )
                view_counts[indices] = np.maximum(
                    view_counts[indices],
                    observation["depth_sample_counts"][v, u],
                )
                self.troughs[index][indices] = np.minimum(
                    self.troughs[index][indices], residual
                )
                self.heights[index][indices] = np.maximum(
                    self.heights[index][indices], residual
                )
                self.lower[index][indices] = np.minimum(
                    self.lower[index][indices],
                    lower,
                )
                self.upper[index][indices] = np.maximum(
                    self.upper[index][indices],
                    upper,
                )
                strongest = lower > self.positive_lower[index][indices]
                positive_indices = indices[strongest]
                self.positive_lower[index][positive_indices] = lower[strongest]
                self.positive_estimates[index][positive_indices] = residual[
                    strongest
                ]
                self.positive_counts[index][positive_indices] = observation[
                    "depth_sample_counts"
                ][v[strongest], u[strongest]]
                for anchor_index in positive_indices:
                    self.positive_views[index][anchor_index] = view_index
                measured_count += len(indices)
        self._observe_local_baseline(observation, excluded_mask)
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
        if not isinstance(whole_workpiece_scan, (bool, np.bool_)):
            raise ValueError("Whole-workpiece scan scope must be boolean")
        whole_workpiece_scan = bool(whole_workpiece_scan)
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
        for (
            target,
            lower,
            upper,
            heights,
            troughs,
            views,
            samples,
            positive_lower,
            positive_estimates,
            positive_counts,
            positive_views,
        ) in zip(
            self.targets,
            self.lower,
            self.upper,
            self.heights,
            self.troughs,
            self.views,
            self.sample_counts,
            self.positive_lower,
            self.positive_estimates,
            self.positive_counts,
            self.positive_views,
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
                    "anchor_estimates_mm": [
                        float(value) if np.isfinite(value) else None
                        for value in heights
                    ],
                    "anchor_observation_counts": (
                        np.sum(list(samples.values()), axis=0).tolist()
                        if samples
                        else [0] * len(upper)
                    ),
                    "anchor_positive_lower_mm": [
                        float(value) if np.isfinite(value) else None
                        for value in positive_lower
                    ],
                    "anchor_positive_estimates_mm": [
                        float(value) if np.isfinite(value) else None
                        for value in positive_estimates
                    ],
                    "anchor_positive_observation_counts": (
                        positive_counts.tolist()
                    ),
                    "anchor_positive_view_indices": positive_views,
                    "points_m": target["points"].tolist(),
                }
            )
        pending = [r["status"] for r in rows if r["status"] != "verified"]
        uncertain = pending.count("uncertain_measurement")
        overgrind = sum(row["overgrind"] for row in rows)
        negative_limit_risk = sum(row["negative_limit_risk"] for row in rows)
        baseline_anchors = [
            *self._baseline_by_side[-1],
            *self._baseline_by_side[1],
        ]
        local_baseline = (
            {
                "baseline": baseline_anchors,
                "protected": list(self._protected_anchors),
                "source": "untouched outside-band bilateral nominal anchors from observed depth frames",
                "frame": self.coordinate_frame,
            }
            if baseline_anchors or self._protected_anchors
            else None
        )
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
                "source": "reported effective camera-depth noise projected onto each CAD normal",
                "cross_view_fusion": "extreme interval envelope",
                "positive_confirmation": "same-pixel independent depth samples in the strongest positive view",
                "calibrated": False,
            },
            "invalid_observations": self.invalid_observations,
            "truth_used_for_decision": False,
            **(
                {"local_baseline": local_baseline}
                if local_baseline is not None
                else {}
            ),
            "observational_release": bool(rows)
            and not pending
            and not self.invalid_observations
            and whole_workpiece_scan,
        }
