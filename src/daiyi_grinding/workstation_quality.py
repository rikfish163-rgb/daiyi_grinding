"""Sensor-only reinspection of the nominal surface under registered defects."""

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .perception import project

MAX_ANCHORS = 96
REFERENCE_MATCH_M = 0.0015
MIN_VIEW_COSINE = 0.25


def surface_anchors(detection, observation):
    """Save referred CAD samples, independent of future detector outputs."""
    rows, cols = detection.pixels.T
    points = observation["reference_xyz"][rows, cols]
    normals = observation["normals"][rows, cols]
    valid = (
        np.isfinite(points).all(axis=1)
        & np.isfinite(normals).all(axis=1)
        & (np.linalg.norm(normals, axis=1) > 0.9)
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
    """Measure each initial region at its saved CAD locations across views."""

    def __init__(self, targets):
        self.targets = targets
        self.upper = [np.full(len(row["points"]), -np.inf) for row in targets]
        self.heights = [values.copy() for values in self.upper]
        self.views = [set() for _ in targets]
        self.last_camera = None

    def observe(self, observation, view_index):
        camera = observation["T"]
        self.last_camera = camera
        width, height = observation["surface"].shape[::-1]
        for index, target in enumerate(self.targets):
            points, normals = target["points"], target["normals"]
            facing = camera[:3, 3] - points
            directions = facing / np.maximum(
                np.linalg.norm(facing, axis=1, keepdims=True), 1e-12
            )
            with np.errstate(divide="ignore", invalid="ignore"):
                uv = project(points, observation["K"], camera)
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
            paired = (
                observation["surface"][v, u]
                & observation["selection"][v, u]
                & (
                    np.linalg.norm(reference - points[indices], axis=1)
                    <= REFERENCE_MATCH_M
                )
            )
            indices = indices[paired]
            u, v = u[paired], v[paired]
            displacement = (
                observation["xyz"][v, u] - observation["reference_xyz"][v, u]
            )
            residual = (
                np.einsum("ij,ij->i", displacement, normals[indices]) * 1000
            )
            finite = np.isfinite(residual)
            indices, residual = indices[finite], residual[finite]
            if len(indices):
                self.views[index].add(view_index)
                self.heights[index][indices] = np.maximum(
                    self.heights[index][indices], residual
                )
                self.upper[index][indices] = np.maximum(
                    self.upper[index][indices],
                    residual + 2 * observation["noise_sigma_mm"],
                )

    def supplementary_angle(self, current_angle, choices):
        """Prefer a view facing the largest number of unmeasured CAD anchors."""
        if self.last_camera is None:
            return None
        counts = {}
        for angle in choices:
            rotation = Rotation.from_euler(
                "z", current_angle - angle, degrees=True
            )
            camera_position = rotation.apply(self.last_camera[:3, 3])
            score = 0
            for target, upper in zip(self.targets, self.upper):
                unseen = ~np.isfinite(upper)
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
        rows = []
        for target, upper, heights, views in zip(
            self.targets, self.upper, self.heights, self.views
        ):
            measured = np.isfinite(upper)
            peak = float(heights[measured].max()) if measured.any() else None
            upper_peak = (
                float(upper[measured].max()) if measured.any() else None
            )
            state = (
                "needs_rework"
                if peak is not None and peak > tolerance_mm
                else "insufficient_observation"
                if not len(upper) or not measured.all()
                else "uncertain_measurement"
                if upper_peak > tolerance_mm
                else "verified"
            )
            rows.append(
                {
                    "id": target["id"],
                    "kind": target["kind"],
                    "status": state,
                    "observations": sorted(views),
                    "anchors": len(upper),
                    "observed_anchors": int(measured.sum()),
                    "peak_mm": peak,
                    "upper_peak_mm": upper_peak,
                }
            )
        pending = [r["status"] for r in rows if r["status"] != "verified"]
        return {
            "regions": rows,
            "verified": len(rows) - len(pending),
            "needs_rework": pending.count("needs_rework"),
            "insufficient": len(pending) - pending.count("needs_rework"),
            "coverage_scope": "six_view"
            if whole_workpiece_scan
            else "limited_view_or_roi",
            "observational_release": bool(rows)
            and not pending
            and whole_workpiece_scan,
        }
