"""Paired CAD thin-ridge detection at fixed camera resolutions."""

import argparse
import hashlib
import json
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import project
from .cad_resolution import RESOLUTIONS, SPLITS
from .defect_study import summarize
from .evaluation import score
from .perception import detect, overlay
from .planning import plan
from .reference_detection import configuration as reference_configuration
from .reference_detection import detect_reference
from .run import write_json
from .thin_reference import configuration as thin_configuration
from .thin_reference import detect_thin_reference

DEFAULT_OUTPUT = project.RUNS / "defect_study_20261118"
METHODS = ("geometry", "cad_reference", "thin_reference")
SOURCE_FILES = (
    "cad_refinement.py",
    "cad_resolution.py",
    "cad_defects.py",
    "perception.py",
    "reference_detection.py",
    "thin_reference.py",
    "planning.py",
    "evaluation.py",
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def predict(method, observation):
    """Use public observations only, with reference as an explicit extra input."""
    bounds = np.stack(
        [
            observation["inspection_bounds_min"],
            observation["inspection_bounds_max"],
        ]
    )
    rgb, depth = observation["rgb"], observation["depth"]
    intrinsics, transform = observation["K"], observation["T_world_camera"]
    if method == "geometry":
        return detect(
            rgb, depth, intrinsics, transform, inspection_bounds=bounds
        )
    detector = {
        "cad_reference": detect_reference,
        "thin_reference": detect_thin_reference,
    }[method]
    return detector(
        rgb,
        depth,
        observation["reference_depth"],
        intrinsics,
        transform,
        inspection_bounds=bounds,
    )


def evaluate(output, resolution, split):
    """Freeze methods then measure identical views without label access."""
    output = Path(output)
    dataset = (
        output / "cad_resolution_dataset" / f"resolution_{resolution}" / split
    )
    comparison = output / "cad_refinement" / f"resolution_{resolution}" / split
    comparison.mkdir(parents=True, exist_ok=True)
    if (comparison / "summary.json").exists():
        raise FileExistsError(f"Comparison exists: {comparison}")
    manifest = json.loads((dataset / "manifest.json").read_text())
    snapshot = comparison / "source_snapshot"
    snapshot.mkdir(exist_ok=False)
    for filename in SOURCE_FILES:
        shutil.copyfile(project.SOURCE / filename, snapshot / filename)
    source_hashes = {name: sha256(snapshot / name) for name in SOURCE_FILES}
    protocol = {
        "dataset_protocol": manifest["protocol"],
        "split": split,
        "resolution": RESOLUTIONS[resolution],
        "methods": METHODS,
        "reference_configuration": reference_configuration(),
        "thin_reference_configuration": thin_configuration(),
        "source_hashes": source_hashes,
        "reference_input": "same-pose ideal CAD view for both reference methods",
        "frozen_before_evaluation_utc": datetime.now(timezone.utc).isoformat(),
        "selection_rule": "normal no-action guard, then pooled thin F1",
        "scope": "CAD synthetic visible-pixel defects and surface path previews",
    }
    write_json(comparison / "protocol.json", protocol)
    rows = []
    for record in manifest["samples"]:
        sample = Path(record["sample_directory"])
        with np.load(sample / "observation.npz") as captured:
            observation = {name: captured[name] for name in captured.files}
        sample_id = f"{record['case']}_{record['seed']}"
        for method in METHODS:
            start = time.perf_counter()
            detections, auxiliary = predict(method, observation)
            elapsed = (time.perf_counter() - start) * 1000
            planning_start = time.perf_counter()
            paths = plan(detections, auxiliary, clearance_mm=3)
            planning_ms = (time.perf_counter() - planning_start) * 1000
            with np.load(sample / "evaluator/truth.npz") as evaluator:
                metrics = score(
                    detections,
                    evaluator["truth"],
                    paths,
                    evaluator["trueK"],
                    evaluator["trueT"],
                    evaluator["ideal_depth"],
                )
            row = {
                "id": sample_id,
                "case": record["case"],
                "seed": record["seed"],
                "method": method,
                "resolution": resolution,
                "split": split,
                "inference_ms": elapsed,
                "planning_ms": planning_ms,
                "path_count": len(paths),
                **metrics,
            }
            rows.append(row)
            prediction = np.zeros(observation["depth"].shape, np.uint8)
            for detection in detections:
                if detection.kind in ("seam", "burr"):
                    prediction[tuple(detection.pixels.T)] = (
                        1 if detection.kind == "seam" else 2
                    )
            destination = comparison / method / sample_id
            destination.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(destination / "prediction.npz", pred=prediction)
            write_json(destination / "metrics.json", row)
            write_json(
                destination / "detections.json",
                [detection.summary() for detection in detections],
            )
            write_json(destination / "paths.json", paths)
            if record["seed"] == SPLITS[split][0]:
                Image.fromarray(
                    overlay(
                        observation["rgb"],
                        detections,
                        paths,
                        observation["K"],
                        observation["T_world_camera"],
                    )
                ).save(destination / "overlay.png")
            print(
                f"{resolution} {split} {sample_id} {method} "
                f"P={metrics['precision']:.3f} R={metrics['recall']:.3f}",
                flush=True,
            )
    after = {name: sha256(project.SOURCE / name) for name in SOURCE_FILES}
    changed = [
        name for name in SOURCE_FILES if after[name] != source_hashes[name]
    ]
    write_json(
        comparison / "source_integrity.json",
        {
            "source_hashes_after": after,
            "unchanged": not changed,
            "changed": changed,
        },
    )
    if changed:
        raise RuntimeError(f"Evaluated source changed: {changed}")
    (comparison / "episodes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    summary = summarize(rows)
    for row in summary:
        row["requires_reference"] = row["method"] != "geometry"
        row["nominal_selected_pixels"] = row["normal_structure_selected_pixels"]
    result = {
        "completed": len(rows),
        "resolution": resolution,
        "split": split,
        "methods": METHODS,
        "summary": summary,
    }
    write_json(comparison / "summary.json", result)
    return result


def select_reference(output):
    """Choose the CAD method using both development resolutions only."""
    output = Path(output)
    metrics = defaultdict(list)
    files = []
    for resolution in RESOLUTIONS:
        path = (
            output
            / "cad_refinement"
            / f"resolution_{resolution}"
            / "development/summary.json"
        )
        files.append(path)
        for row in json.loads(path.read_text())["summary"]:
            if row["method"] != "geometry":
                metrics[row["method"]].append(row)
    ranked = []
    for method, rows in metrics.items():
        normal = [row for row in rows if row["case"] == "normal"]
        thin = [row for row in rows if row["case"] == "thin"]
        total_tp = sum(row["tp"] for row in thin)
        total_fp = sum(row["fp"] for row in thin)
        total_fn = sum(row["fn"] for row in thin)
        coarse = [row for row in rows if row["case"] == "coarse"]
        eligible = (
            all(row["no_action_samples"] == row["samples"] for row in normal)
            and all(row["review_samples"] == 0 for row in normal)
            and all(
                row["unsupported_stump_selected_pixels"] == 0 for row in coarse
            )
        )
        ranked.append(
            {
                "method": method,
                "eligible": eligible,
                "pooled_thin_f1": 2
                * total_tp
                / max(2 * total_tp + total_fp + total_fn, 1),
                "pooled_thin_recall": total_tp / max(total_tp + total_fn, 1),
            }
        )
    eligible = [row for row in ranked if row["eligible"]]
    if not eligible:
        raise RuntimeError("No reference method meets development guards")
    selected = max(eligible, key=lambda row: row["pooled_thin_f1"])["method"]
    result = {
        "selected_reference_method": selected,
        "chosen_on_split": "development",
        "selected_utc": datetime.now(timezone.utc).isoformat(),
        "ranked": ranked,
        "development_summary_sha256": {
            str(path): sha256(path) for path in files
        },
        "requires_registered_reference": True,
    }
    write_json(output / "cad_refinement/selection.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--resolution", type=int, choices=tuple(RESOLUTIONS), default=1024
    )
    parser.add_argument("--split", choices=tuple(SPLITS), default="development")
    parser.add_argument("--select", action="store_true")
    args = parser.parse_args()
    if args.select:
        print(json.dumps(select_reference(args.output), ensure_ascii=False))
    else:
        evaluate(args.output, args.resolution, args.split)


if __name__ == "__main__":
    main()
