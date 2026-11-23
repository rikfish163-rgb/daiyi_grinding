"""Evaluate RGB-only silhouette and intensity baselines on author-labelled flash samples."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage


def read_sample(root: Path, sample_id: str):
    rgb = Image.open(root / "images" / f"{sample_id}.jpg").convert("RGB")
    shape = (800, round(rgb.height * 800 / rgb.width))
    rgb = np.asarray(rgb.resize(shape, Image.Resampling.BILINEAR))
    label = Image.open(root / "labels" / f"{sample_id}.png")
    truth = np.asarray(label.resize(shape, Image.Resampling.NEAREST)) > 0
    return rgb, truth


def predict(rgb: np.ndarray, method: str, radius: int, threshold: int):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    foreground = (gray >= threshold).astype(np.uint8)
    connected, count = ndimage.label(foreground)
    if count:
        sizes = np.bincount(connected.ravel())
        sizes[0] = 0
        foreground = (connected == sizes.argmax()).astype(np.uint8)
    foreground = ndimage.binary_fill_holes(foreground).astype(np.uint8)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)
    )
    if method == "silhouette_opening":
        opened = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
        return (foreground > 0) & (opened == 0)
    residual = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    return (residual > 25) & (foreground > 0)


def score(prediction: np.ndarray, truth: np.ndarray):
    true_positive = int((prediction & truth).sum())
    false_positive = int((prediction & ~truth).sum())
    false_negative = int((~prediction & truth).sum())
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-10)
    return dict(
        tp=true_positive,
        fp=false_positive,
        fn=false_negative,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def aggregate(rows):
    total = {key: sum(row[key] for row in rows) for key in ("tp", "fp", "fn")}
    precision = total["tp"] / max(total["tp"] + total["fp"], 1)
    recall = total["tp"] / max(total["tp"] + total["fn"], 1)
    return dict(
        **total,
        precision=precision,
        recall=recall,
        f1=2 * precision * recall / max(precision + recall, 1e-10),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "runs/defect_study_20261118/external_samples/ssal",
    )
    args = parser.parse_args()
    development = [
        "1_3648_2736",
        "2_3648_2736",
        "3_3648_2736",
        "10_3648_2736",
    ]
    holdout = [
        "100_3648_2736",
        "101_3648_2736",
        "1_4032_3024",
        "2_4032_3024",
    ]
    protocol = {
        "dataset": "ZianYuCode/SSAL author-labelled injection-molded flash",
        "source_url": "https://github.com/ZianYuCode/SSAL",
        "license": "CC0-1.0",
        "image_width_px": 800,
        "development": development,
        "holdout": holdout,
        "methods": ["silhouette_opening", "intensity_tophat"],
        "radius_candidates_px": [6, 12, 24, 40],
        "foreground_threshold_candidates": [100, 140, 180, 210],
        "selection_metric": "pooled development pixel F1",
        "truth_access": "prediction accepts RGB and frozen parameters only",
        "scope": "8-image transferred morphology stress test, no metal or independent-part generalization claim",
    }
    (args.data / "benchmark_protocol.json").write_text(
        json.dumps(protocol, indent=2), encoding="utf-8"
    )
    all_samples = {
        sample_id: read_sample(args.data, sample_id)
        for sample_id in development + holdout
    }
    results = []
    selected = {}
    for method in protocol["methods"]:
        development_trials = []
        for radius in protocol["radius_candidates_px"]:
            for threshold in protocol["foreground_threshold_candidates"]:
                rows = []
                for sample_id in development:
                    rgb, truth = all_samples[sample_id]
                    prediction = predict(rgb, method, radius, threshold)
                    rows.append(score(prediction, truth))
                development_trials.append(
                    dict(
                        method=method,
                        radius_px=radius,
                        threshold=threshold,
                        **aggregate(rows),
                    )
                )
        best = max(development_trials, key=lambda row: row["f1"])
        selected[method] = best
        for sample_id in holdout:
            rgb, truth = all_samples[sample_id]
            started = time.perf_counter()
            prediction = predict(
                rgb, method, best["radius_px"], best["threshold"]
            )
            row = dict(
                method=method,
                sample_id=sample_id,
                runtime_s=time.perf_counter() - started,
                **score(prediction, truth),
            )
            results.append(row)
    summary = {
        method: aggregate([row for row in results if row["method"] == method])
        for method in protocol["methods"]
    }
    artifact = dict(
        protocol=protocol,
        selected_from_development=selected,
        holdout_results=results,
        holdout_summary=summary,
    )
    (args.data / "benchmark_results.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    panels = []
    for sample_id in holdout:
        rgb, truth = all_samples[sample_id]
        method = "silhouette_opening"
        params = selected[method]
        prediction = predict(
            rgb, method, params["radius_px"], params["threshold"]
        )
        annotated = rgb.copy()
        annotated[truth] = (
            0.4 * annotated[truth] + 0.6 * np.array([255, 80, 30])
        ).astype(np.uint8)
        annotated[prediction & ~truth] = [60, 130, 255]
        annotated[prediction & truth] = [40, 235, 130]
        frame = Image.fromarray(annotated)
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, 800, 30), fill="black")
        draw.text((10, 8), sample_id, fill="white")
        panels.append(frame)
    tile = Image.new("RGB", (1600, 2 * panels[0].height), "#17202a")
    for index, panel in enumerate(panels):
        tile.paste(panel, ((index % 2) * 800, (index // 2) * panels[0].height))
    tile.save(args.data / "benchmark_preview.png")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
