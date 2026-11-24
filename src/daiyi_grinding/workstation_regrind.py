"""Local follow-up traces selected by repeated signed anchor measurements."""

import numpy as np
from scipy.spatial import cKDTree

ANCHOR_TRACE_MATCH_M = 0.002
MAX_TRACE_GAP_M = 0.005


def regrind_paths(
    targets, quality, source_paths, *, start_segment_id=1, rate_mm_s=None
) -> tuple[list[dict], dict]:
    """Select permitted nominal subtraces for persistent positive excess.

    Args:
        targets: Validated nominal CAD anchors registered before machining.
        quality: Current Reinspection report, including per-anchor estimates,
            intervals and independent observation counts.
        source_paths: Previously permitted surface paths for the same workpiece,
            tool and ROI. These are surface traces, not reusable robot commands.
        start_segment_id: First segment identifier for the returned paths.
        rate_mm_s: Optional nominal synthetic normal-depth capacity.

    Returns:
        Paths using the existing workstation schema, and a mapping report.
        Missing, uncertain or negative-risk reinspection returns no paths.
        All returned paths require the current review-footprint checks and a
        fresh whole-cell motion plan.
    """
    report = {
        "source": "current repeated signed measurements at registered CAD anchors",
        "truth_used_for_control": False,
        "requires_fresh_motion_plan": True,
        "source_trace_count": len(source_paths),
        "positive_anchor_count": 0,
        "unmapped_anchors": [],
        "path_count": 0,
    }
    hold_reason = (
        "reinspection_disposition"
        if not quality["automatic_regrind_allowed"]
        else "insufficient_reinspection"
        if quality["insufficient"]
        else "uncertain_reinspection"
        if quality["uncertain"]
        else None
    )
    if hold_reason:
        return [], {**report, "reason": hold_reason}

    by_id = {row["id"]: row for row in quality["regions"]}
    positive = []
    for target in targets:
        row = by_id[target["id"]]
        if (
            target["kind"] not in {"seam", "burr"}
            or row["status_reason"] != "positive_excess"
            or row["observed_anchors"] != row["anchors"]
        ):
            continue
        positive_lower = np.asarray(row["anchor_positive_lower_mm"], float)
        counts = np.asarray(row["anchor_positive_observation_counts"])
        indices = np.flatnonzero(
            (positive_lower > quality["signed_limits_mm"]["upper"])
            & (counts >= 2)
        )
        for index in indices:
            positive.append(
                {
                    "target_id": target["id"],
                    "anchor_index": int(index),
                    "point": np.asarray(target["points"])[index],
                    "normal": np.asarray(target["normals"])[index],
                    "estimate_mm": row["anchor_positive_estimates_mm"][index],
                    "lower_mm": float(positive_lower[index]),
                    "observations": int(counts[index]),
                }
            )
    report["positive_anchor_count"] = len(positive)
    if not positive:
        return [], {**report, "reason": "no_confirmed_positive_excess"}

    anchor_points = np.asarray([row["point"] for row in positive])
    anchor_normals = np.asarray([row["normal"] for row in positive])
    covered = np.zeros(len(positive), bool)
    paths = []
    for source in source_paths:
        normal = np.asarray(source["normal"])
        # A nearby anchor on another CAD face must not select this trace.
        same_chart = anchor_normals @ normal >= 0.9
        compatible = np.flatnonzero(same_chart)
        if not len(compatible):
            continue
        points = np.asarray(source["nominal_xyz"])
        selected = (
            cKDTree(anchor_points[compatible]).query(points)[0]
            <= ANCHOR_TRACE_MATCH_M
        )
        for indices in _local_parts(points, selected):
            part = points[indices]
            support = (
                cKDTree(part).query(anchor_points)[0] <= ANCHOR_TRACE_MATCH_M
            ) & same_chart
            covered |= support
            evidence = [row for row, used in zip(positive, support) if used]
            path = {
                **source,
                "segment_id": start_segment_id + len(paths),
                "xyz": part.tolist(),
                "nominal_xyz": part.tolist(),
                "observed_xyz": [
                    (
                        row["point"] + row["estimate_mm"] / 1000 * row["normal"]
                    ).tolist()
                    for row in evidence
                ],
                "normal_height_mm": max(row["estimate_mm"] for row in evidence),
                "machining_required": True,
                "root_source": "remeasured registered CAD anchors on a previously permitted nominal trace",
                "filter": {
                    "implementation": "local selection of the previously filtered nominal trace",
                    "nominal_points_unchanged": True,
                    "source_segment_id": source["segment_id"],
                },
                "regrind": {
                    "registered_target_ids": sorted(
                        {row["target_id"] for row in evidence}
                    ),
                    "support_anchor_count": len(evidence),
                    "minimum_observations": min(
                        row["observations"] for row in evidence
                    ),
                    "minimum_lower_mm": min(
                        row["lower_mm"] for row in evidence
                    ),
                    "source_segment_id": source["segment_id"],
                    "truth_used_for_control": False,
                },
            }
            if rate_mm_s is not None:
                upper_mm = max(
                    by_id[row["target_id"]]["anchor_bounds_mm"][
                        row["anchor_index"]
                    ][1]
                    for row in evidence
                )
                required_seconds = (
                    upper_mm - source["material_allowance_mm"]
                ) / rate_mm_s
                length_mm = float(
                    np.linalg.norm(np.diff(part, axis=0), axis=1).sum() * 1000
                )
                path["recipe"] = {
                    **source["recipe"],
                    "feed_mm_s": min(
                        source["recipe"]["feed_mm_s"],
                        length_mm / required_seconds,
                    ),
                }
                path["regrind"].update(
                    nominal_rate_mm_s=rate_mm_s,
                    observed_upper_mm=upper_mm,
                    required_engaged_seconds=required_seconds,
                    planned_trace_seconds=length_mm
                    / path["recipe"]["feed_mm_s"],
                )
            paths.append(path)
    report["unmapped_anchors"] = [
        {
            "target_id": row["target_id"],
            "anchor_index": row["anchor_index"],
        }
        for row, used in zip(positive, covered)
        if not used
    ]
    report["path_count"] = len(paths)
    report["reason"] = (
        "unmapped_positive_excess"
        if report["unmapped_anchors"]
        else "local_regrind"
    )
    return paths, report


def additional_regrind_paths(
    paths: list[dict], candidates: list[dict]
) -> list[dict]:
    """Return local traces not already covered on the same nominal CAD chart."""
    added = []
    for path in candidates:
        if any(
            np.dot(path["normal"], current["normal"]) >= 0.9
            and np.all(
                cKDTree(current["nominal_xyz"]).query(path["nominal_xyz"])[0]
                <= ANCHOR_TRACE_MATCH_M
            )
            for current in paths + added
        ):
            continue
        added.append(path)
    return added


def _local_parts(points, selected):
    """Keep adjacent source samples without creating gap-spanning cut lines."""
    continuous = np.split(
        np.arange(len(points)),
        np.flatnonzero(
            np.linalg.norm(np.diff(points, axis=0), axis=1) > MAX_TRACE_GAP_M
        )
        + 1,
    )
    parts = []
    for indices in continuous:
        active = selected[indices]
        if len(indices) < 2 or not active.any():
            continue
        active = active | np.r_[False, active[:-1]] | np.r_[active[1:], False]
        for part in np.split(
            indices,
            np.flatnonzero(active[1:] != active[:-1]) + 1,
        ):
            if len(part) >= 2 and active[np.searchsorted(indices, part[0])]:
                parts.append(part)
    return parts
