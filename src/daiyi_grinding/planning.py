"""Measured point clouds -> process paths, orientation, approach and retract."""

import numpy as np
from scipy.ndimage import (
    distance_transform_edt,
    gaussian_filter1d,
    map_coordinates,
)
from scipy.spatial import cKDTree

from .perception import project

RECIPES = {
    "seam": dict(
        name="thin_parting_fin",
        tool="ball_abrasive_6mm",
        feed_mm_s=12.0,
        stepover_mm=1.8,
        overlap=0.4,
        force_target_n=None,
    ),
    "burr": dict(
        name="localized_burr",
        tool="ball_abrasive_6mm",
        feed_mm_s=8.0,
        stepover_mm=1.8,
        overlap=0.4,
        force_target_n=None,
    ),
}


def constrain_roi(points, observation, margin_m=0.004):
    """Final continuous trace guard: 3 mm ball radius + 1 mm research allowance.

    Dense 0.2mm samples and a conservative extra pixel avoid bridging a concave
    selection after smoothing. Returns disjoint admissible segments.
    """
    selection = observation.get("selection")
    if selection is None or np.all(selection):
        return [points]
    scale = observation["scale"]
    distances = distance_transform_edt(np.pad(selection, 1))[1:-1, 1:-1] * scale
    dense = []
    for a, b in zip(points[:-1], points[1:]):
        n = max(1, int(np.ceil(np.linalg.norm(b - a) / 0.0002)))
        dense.extend(
            a + (b - a) * t for t in np.linspace(0, 1, n, endpoint=False)
        )
    dense = np.array([*dense, points[-1]])
    uv = project(dense, observation["K"], observation["T"])
    allowed = map_coordinates(
        distances, [uv[:, 1], uv[:, 0]], order=1, mode="constant", cval=0
    ) >= (margin_m + scale)
    segments = []
    for inds in np.split(
        np.arange(len(dense)), np.flatnonzero(allowed[1:] != allowed[:-1]) + 1
    ):
        if len(inds) > 1 and allowed[inds[0]]:
            segments.append(dense[inds])
    return segments


def plan(detections, observation, selected_ids=None, clearance_mm=1.0):
    """Default surface-following clearance demo. A negative offset is not a cut model."""
    paths = []
    for d in detections:
        if d.kind not in RECIPES:
            continue
        if selected_ids is not None and d.id not in selected_ids:
            continue
        points = d.xyz
        center = points[:, :2].mean(axis=0)
        _, _, v = np.linalg.svd(points[:, :2] - center, full_matrices=False)
        local = (points[:, :2] - center) @ v.T
        tree = cKDTree(points[:, :2])
        recipe = RECIPES[d.kind].copy()
        # Reference surface normal from a measured annulus around the detection.
        image_xyz = observation["xyz"]
        rows, cols = d.pixels.T
        r0, r1 = (
            max(0, rows.min() - 25),
            min(image_xyz.shape[0], rows.max() + 26),
        )
        c0, c1 = (
            max(0, cols.min() - 25),
            min(image_xyz.shape[1], cols.max() + 26),
        )
        patch = image_xyz[r0:r1, c0:c1].reshape(-1, 3)
        refz = np.median(points[:, 2]) - d.height_mm / 1000
        patch = patch[
            (patch[:, 2] > refz - 0.0015) & (patch[:, 2] < refz + 0.0008)
        ]
        if len(patch) > 15:
            A = np.c_[patch[:, :2] - center, np.ones(len(patch))]
            slope = np.linalg.lstsq(A, patch[:, 2], rcond=None)[0][:2]
        else:
            slope = np.zeros(2)
        normal = np.r_[-slope, 1.0]
        normal /= np.linalg.norm(normal)
        # Limit this first prototype to surfaces close to the fixture top plane.
        if normal[2] < np.cos(np.deg2rad(12)):
            continue
        zaxis = -normal
        # Ball tool is rotationally symmetric: keep a stable in-plane yaw.
        xaxis = np.array([1.0, 0, 0])
        xaxis -= zaxis * np.dot(xaxis, zaxis)
        xaxis /= np.linalg.norm(xaxis)
        R = np.column_stack([xaxis, np.cross(zaxis, xaxis), zaxis])
        lanes = (
            [0.0]
            if d.kind == "seam"
            else list(
                np.arange(
                    local[:, 1].min(), local[:, 1].max() + 0.00001, 0.0018
                )
            )
        )
        for lane_index, lane in enumerate(lanes):
            path = []
            coord = np.arange(
                local[:, 0].min(), local[:, 0].max() + 0.0005, 0.0015
            )
            if d.kind == "seam":
                for t in coord:
                    near = np.abs(local[:, 0] - t) < 0.0017
                    if not near.any():
                        continue
                    # Center line comes from visible ridge samples, never generator geometry.
                    p = points[near].mean(axis=0)
                    p[2] = np.quantile(points[near, 2], 0.90)
                    path.append(p)
            else:
                for t in coord:
                    xy = center + np.array([t, lane]) @ v
                    distance, index = tree.query(xy)
                    if distance > 0.00085:
                        continue
                    p = np.r_[xy, points[index, 2]]
                    path.append(p)
            if len(path) < 2:
                continue
            path = np.array(path)
            if lane_index % 2:
                path = path[::-1]
            if len(path) > 5:
                path = gaussian_filter1d(path, 0.65, axis=0, mode="nearest")
            path += normal * clearance_mm / 1000
            # Do not bridge missing-data gaps as an unobserved cutting segment.
            breaks = (
                np.flatnonzero(
                    np.linalg.norm(np.diff(path, axis=0), axis=1) > 0.004
                )
                + 1
            )
            segments = [
                part
                for raw in np.split(path, breaks)
                if len(raw) > 1
                for part in constrain_roi(raw, observation)
            ]
            for segment in segments:
                if len(segment) < 2:
                    continue
                paths.append(
                    dict(
                        detection_id=d.id,
                        kind=d.kind,
                        xyz=segment.tolist(),
                        rotation=R.tolist(),
                        normal=normal.tolist(),
                        approach=(segment[0] + normal * 0.025).tolist(),
                        retract=(segment[-1] + normal * 0.025).tolist(),
                        recipe=recipe,
                        clearance_mm=clearance_mm,
                    )
                )
    return paths


def path_length(paths):
    return sum(
        float(np.linalg.norm(np.diff(np.array(p["xyz"]), axis=0), axis=1).sum())
        for p in paths
    )
