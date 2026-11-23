"""Held-out simulation truth and metrics; never used as planner conditions."""

from __future__ import annotations

import numpy as np


def binary_metrics(truth, predicted, valid=None):
    """Report confusion counts, F1 and IoU, including undefined denominators."""
    truth, predicted = np.asarray(truth, bool), np.asarray(predicted, bool)
    valid = (
        np.ones(truth.shape, bool) if valid is None else np.asarray(valid, bool)
    )
    tp = int(np.count_nonzero(truth & predicted & valid))
    fp = int(np.count_nonzero(~truth & predicted & valid))
    fn = int(np.count_nonzero(truth & ~predicted & valid))
    tn = int(np.count_nonzero(~truth & ~predicted & valid))

    def ratio(numerator, denominator):
        return float(numerator / denominator) if denominator else None

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "iou": ratio(tp, tp + fp + fn),
        "accuracy": ratio(tp + tn, tp + fp + fn + tn),
    }


def evaluate_view(sim, detections, observation, ideal_depth, reference_depth):
    """Read visible geom identities after predictions exist, for scoring only."""
    segmentation = sim.render(mode="segmentation")
    geom_ids = segmentation[..., 0]
    labels = np.zeros(ideal_depth.shape, np.uint8)
    visible_objects = []
    matched_objects = []
    for item in sim.private_defects:
        if (
            getattr(sim, "evaluation_stock", {}).get(item["geom"], float("inf"))
            <= 0.1
        ):
            continue
        identity = sim.model.geom(item["geom"]).id
        pixels = (geom_ids == identity) & (
            reference_depth - ideal_depth > 0.00010
        )
        labels[pixels] = item["label"]
        if pixels.sum() >= 6:
            visible_objects.append(item["geom"])
            predicted = np.zeros(pixels.shape, bool)
            for detection in detections:
                if detection.kind == item["kind"]:
                    predicted[tuple(detection.pixels.T)] = True
            if np.count_nonzero(pixels & predicted) >= max(
                3, pixels.sum() * 0.2
            ):
                matched_objects.append(item["geom"])
    predicted = np.zeros(labels.shape, bool)
    for detection in detections:
        if detection.kind in {"seam", "burr"}:
            predicted[tuple(detection.pixels.T)] = True
    truth = (labels == 1) | (labels == 2)
    metrics = binary_metrics(
        truth, predicted, observation["surface"] | (labels > 0)
    )
    metrics["per_kind"] = {}
    for kind, label in (("seam", 1), ("burr", 2), ("review", 4)):
        prediction = np.zeros(labels.shape, bool)
        for detection in detections:
            if detection.kind == kind:
                prediction[tuple(detection.pixels.T)] = True
        metrics["per_kind"][kind] = binary_metrics(
            labels == label, prediction, observation["surface"] | (labels > 0)
        )
    metrics["visible_defects"] = visible_objects
    metrics["matched_defects"] = matched_objects
    return metrics, labels


def aggregate_views(views):
    """Pool view pixels and report unique defect recall separately."""
    counts = {
        name: sum(view[name] for view in views)
        for name in ("tp", "fp", "fn", "tn")
    }
    tp, fp, fn, tn = [counts[name] for name in ("tp", "fp", "fn", "tn")]
    visible = {item for view in views for item in view["visible_defects"]}
    matched = {item for view in views for item in view["matched_defects"]}
    return {
        **counts,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
        "accuracy": (tp + tn) / (tp + fp + fn + tn)
        if tp + fp + fn + tn
        else None,
        "visible_defect_count": len(visible),
        "matched_defect_count": len(matched),
        "object_recall": len(matched) / len(visible) if visible else None,
        "scope": "pooled visible simulated pixels across views; repeated observations are not independent samples; synthetic defects only",
        "truth_used_for_inference": False,
    }
