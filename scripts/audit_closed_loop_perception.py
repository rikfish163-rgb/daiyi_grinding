#!/usr/bin/env python3
"""Diagnose saved predictions after inference, without changing their masks."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image


def audit_episode(episode, threshold):
    """Separate probability misses, semantic mistakes and measurement guards."""
    counts = Counter()
    per_kind = {}
    for prediction in sorted(episode.glob("view_*_model_prediction.npz")):
        prefix = prediction.name.removesuffix("_model_prediction.npz")
        truth = np.asarray(Image.open(episode / f"{prefix}_truth.png"))
        with np.load(prediction) as saved:
            probability = saved["probability"].astype(np.float32)
            semantic = saved["semantic"]
            measured = saved["measurable_mask"]
        foreground = probability[1:].max(axis=0)
        for label, kind in ((1, "seam"), (2, "burr")):
            pixels = truth == label
            local = Counter(
                truth_pixels=int(pixels.sum()),
                below_probability_threshold=int(
                    (pixels & (foreground < threshold)).sum()
                ),
                foreground_threshold_but_background_class=int(
                    (pixels & (foreground >= threshold) & (semantic == 0)).sum()
                ),
                measurement_guard_or_small_component=int(
                    (
                        pixels
                        & (foreground >= threshold)
                        & (semantic > 0)
                        & ~measured
                    ).sum()
                ),
                retained_wrong_class=int(
                    (pixels & measured & (semantic != label)).sum()
                ),
                retained_correct_class=int(
                    (pixels & measured & (semantic == label)).sum()
                ),
            )
            counts.update(local)
            per_kind.setdefault(kind, Counter()).update(local)
    return {
        "counts": dict(counts),
        "per_kind": {kind: dict(value) for kind, value in per_kind.items()},
        "scope": (
            "Evaluation labels read from saved truth images AFTER frozen "
            "predictions. Six correlated views; no inference or training changes. "
            "Float16 archived probabilities can round values near the threshold."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.35)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text())
    rows = []
    for row in matrix["rows"]:
        if row["case"] in {"normal", "casting_normal"}:
            continue
        diagnosis = audit_episode(Path(row["result"]).parent, args.threshold)
        rows.append({"case": row["case"], "seed": row["seed"], **diagnosis})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    )
    for row in rows:
        print(row["case"], row["seed"], row["counts"], flush=True)


if __name__ == "__main__":
    main()
