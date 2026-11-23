"""Reproducible paired defect experiments and original FR5 execution."""

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
from .calibration import load_inspection_calibration
from .evaluation import score, truth_image
from .geometry import generate
from .perception import detect, observe_noise, overlay
from .planning import path_length, plan
from .reference_detection import (
    DEFAULT_BOUNDS,
    configuration,
    detect_reference,
)
from .run import Video, export_paths, write_json
from .sim import Simulation

CASES = ("mixed", "seam", "burr", "normal", "thin", "coarse", "noisy")
SPLITS = {
    "development": (5007, 5008, 5009, 5010),
    "holdout": (6007, 6008, 6009, 6010, 6011, 6012),
}
DEFAULT_OUTPUT = project.RUNS / "defect_study_20261118"


def file_hash(path):
    """Hash a saved source or observation used by this experiment."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_coupon_dataset(output):
    """Capture paired observations; keep scorer labels in separate files."""
    output = Path(output)
    dataset = output / "coupon_dataset"
    dataset.mkdir(parents=True, exist_ok=True)
    index_path = dataset / "index.json"
    if index_path.exists():
        return json.loads(index_path.read_text())
    records = []
    calibration = project.ROOT / "results/calibration/calibration.json"
    write_json(
        dataset / "generation_protocol.json",
        {
            "cases": CASES,
            "splits": SPLITS,
            "reference": "independent noisy normal observation at same pose",
            "reference_noise_independent": True,
            "labels": "separate truth.npz, never passed to detectors",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_hashes": {
                name: file_hash(project.SOURCE / name)
                for name in ("geometry.py", "sim.py", "calibration.py")
            },
        },
    )
    for split, seeds in SPLITS.items():
        for seed in seeds:
            nominal = Simulation(generate(seed, "normal"))
            try:
                nominal_rgb = nominal.render()
                nominal_depth = nominal.render(depth=True)
            finally:
                nominal.close()
            for case in CASES:
                sample = dataset / split / f"{case}_{seed}"
                sample.mkdir(parents=True, exist_ok=True)
                coupon = generate(seed, case)
                simulation = Simulation(coupon)
                try:
                    rgb = simulation.render()
                    ideal_depth = simulation.render(depth=True)
                    true_intrinsics, true_transform = simulation.camera_info()
                    intrinsics, transform = load_inspection_calibration(
                        calibration,
                        camera_position=simulation.data.cam_xpos[
                            simulation.model.camera("inspection").id
                        ],
                    )
                    noise_mm, dropout = (
                        (0.25, 0.025) if case == "noisy" else (0.06, 0.003)
                    )
                    rgb, depth = observe_noise(
                        rgb, ideal_depth, seed, noise_mm, dropout
                    )
                    _, reference_depth = observe_noise(
                        nominal_rgb,
                        nominal_depth,
                        seed + 10837,
                        noise_mm,
                        dropout,
                    )
                    np.savez_compressed(
                        sample / "observation.npz",
                        rgb=rgb,
                        depth=depth,
                        reference_depth=reference_depth,
                        K=intrinsics,
                        T_world_camera=transform,
                        inspection_bounds=np.asarray(DEFAULT_BOUNDS),
                    )
                    truth = truth_image(
                        coupon, ideal_depth, true_intrinsics, true_transform
                    )
                    np.savez_compressed(
                        sample / "truth.npz",
                        truth=truth,
                        ideal_depth=ideal_depth,
                        K=true_intrinsics,
                        T_world_camera=true_transform,
                    )
                    if seed == seeds[0]:
                        Image.fromarray(rgb).save(sample / "rgb.png")
                    record = {
                        "id": f"{split}/{case}_{seed}",
                        "sample": str(sample.relative_to(output)),
                        "split": split,
                        "case": case,
                        "seed": seed,
                        "noise_mm": noise_mm,
                        "dropout": dropout,
                        "observation_sha256": file_hash(
                            sample / "observation.npz"
                        ),
                        "truth_sha256": file_hash(sample / "truth.npz"),
                    }
                    records.append(record)
                    print(f"captured {record['id']}", flush=True)
                finally:
                    simulation.close()
    write_json(index_path, records)
    return records


def predict(method, observation):
    """Dispatch with only public observation inputs at the detector boundary."""
    inputs = [
        observation[name] for name in ("rgb", "depth", "K", "T_world_camera")
    ]
    bounds = observation.get("inspection_bounds", np.asarray(DEFAULT_BOUNDS))
    if method in ("threshold", "geometry"):
        return detect(*inputs, method=method, inspection_bounds=bounds)
    if method == "cad_reference":
        return detect_reference(
            observation["rgb"],
            observation["depth"],
            observation["reference_depth"],
            observation["K"],
            observation["T_world_camera"],
            inspection_bounds=bounds,
        )
    from .detector_candidates import detect_candidate

    return detect_candidate(*inputs, method=method, inspection_bounds=bounds)


def detector_methods():
    """List unchanged baselines, new observation methods, and reference method."""
    from .detector_candidates import CANDIDATE_METHODS

    return ["threshold", "geometry", *CANDIDATE_METHODS, "cad_reference"]


def frozen_snapshot(output, split, methods):
    """Record code and reference configuration before the evaluated split."""
    frozen = Path(output) / "coupon_comparison" / split / "source_snapshot"
    frozen.mkdir(parents=True, exist_ok=True)
    for filename in project.SOURCE.glob("*.py"):
        shutil.copyfile(filename, frozen / filename.name)
    from .detector_candidates import candidate_configurations

    write_json(
        frozen.parent / "protocol.json",
        {
            "split": split,
            "seeds": SPLITS[split],
            "cases": CASES,
            "methods": methods,
            "candidate_configurations": candidate_configurations(),
            "reference_configuration": configuration(),
            "source_hashes": {
                filename.name: file_hash(filename)
                for filename in frozen.glob("*.py")
            },
            "inputs": "one identical noisy observation per sample for methods",
            "additional_reference_input": ["cad_reference"],
            "scope": "paired visible-pixel detection and planned XY footprint",
            "frozen_before_evaluation_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        },
    )


def verify_frozen_sources(comparison):
    """Reject an experiment if evaluated sources changed during the run."""
    comparison = Path(comparison)
    protocol = json.loads((comparison / "protocol.json").read_text())
    after = {
        filename: file_hash(project.SOURCE / filename)
        for filename in protocol["source_hashes"]
    }
    changed = [
        filename
        for filename, digest in after.items()
        if digest != protocol["source_hashes"][filename]
    ]
    write_json(
        comparison / "source_integrity.json",
        {
            "checked_utc": datetime.now(timezone.utc).isoformat(),
            "source_hashes_after": after,
            "unchanged": not changed,
            "changed": changed,
        },
    )
    if changed:
        raise RuntimeError(f"Evaluated source changed during run: {changed}")


def summarize(records):
    """Pool pixel counts by method and case; preserve negative scene meaning."""
    grouped = defaultdict(list)
    for record in records:
        grouped[(record["method"], record["case"])].append(record)
    summaries = []
    for (method, case), rows in grouped.items():
        totals = {
            name: sum(row[name] for row in rows)
            for name in (
                "tp",
                "fp",
                "fn",
                "normal_structure_selected_pixels",
                "unsupported_stump_selected_pixels",
            )
        }
        precision = (
            totals["tp"] / (totals["tp"] + totals["fp"])
            if totals["tp"] + totals["fp"]
            else None
        )
        recall = (
            totals["tp"] / (totals["tp"] + totals["fn"])
            if totals["tp"] + totals["fn"]
            else None
        )
        f1 = (
            2 * totals["tp"] / (2 * totals["tp"] + totals["fp"] + totals["fn"])
            if totals["tp"] + totals["fn"]
            else None
        )
        coverage = [
            row["defect_footprint_coverage_3mm"]
            for row in rows
            if row["defect_footprint_coverage_3mm"] is not None
        ]
        summaries.append(
            {
                "method": method,
                "case": case,
                "samples": len(rows),
                "precision": precision,
                "recall": recall,
                "f1": f1,
                **totals,
                "no_action_samples": sum(
                    row["auto_regions"] == 0 for row in rows
                ),
                "review_samples": sum(
                    row["review_regions"] > 0 for row in rows
                ),
                "mean_inference_ms": float(
                    np.mean([row["inference_ms"] for row in rows])
                ),
                "mean_planning_ms": float(
                    np.mean([row["planning_ms"] for row in rows])
                ),
                "mean_coverage_3mm": (
                    float(np.mean(coverage)) if coverage else None
                ),
                "requires_reference": method == "cad_reference",
            }
        )
    return summaries


def evaluate_coupon_dataset(output, split, methods=None):
    """Score all methods after detection and planning on identical samples."""
    output = Path(output)
    methods = methods or detector_methods()
    index = json.loads((output / "coupon_dataset/index.json").read_text())
    comparison = output / "coupon_comparison" / split
    comparison.mkdir(parents=True, exist_ok=True)
    if (comparison / "summary.json").exists():
        raise FileExistsError(f"Completed comparison exists: {comparison}")
    frozen_snapshot(output, split, methods)
    records = []
    for entry in index:
        if entry["split"] != split:
            continue
        sample = output / entry["sample"]
        with np.load(sample / "observation.npz") as observed:
            observation = {name: observed[name] for name in observed.files}
        for method in methods:
            started = time.perf_counter()
            detections, auxiliary = predict(method, observation)
            inference_ms = (time.perf_counter() - started) * 1000
            planning_started = time.perf_counter()
            paths = (
                plan(detections, auxiliary, clearance_mm=3.0)
                if method != "threshold"
                else []
            )
            planning_ms = (time.perf_counter() - planning_started) * 1000
            with np.load(sample / "truth.npz") as evaluator:
                metrics = score(
                    detections,
                    evaluator["truth"],
                    paths,
                    evaluator["K"],
                    evaluator["T_world_camera"],
                    evaluator["ideal_depth"],
                )
            record = {
                "id": entry["id"],
                "seed": entry["seed"],
                "case": entry["case"],
                "method": method,
                "split": split,
                "inference_ms": inference_ms,
                "planning_ms": planning_ms,
                "path_count": len(paths),
                "selected_backend": auxiliary.get("selected_backend", method),
                **metrics,
            }
            records.append(record)
            destination = (
                comparison / method / f"{entry['case']}_{entry['seed']}"
            )
            destination.mkdir(parents=True, exist_ok=True)
            write_json(
                destination / "detections.json",
                [detection.summary() for detection in detections],
            )
            write_json(destination / "paths.json", paths)
            write_json(destination / "metrics.json", record)
            prediction = np.zeros(observation["depth"].shape, dtype=np.uint8)
            for detection in detections:
                if detection.kind in ("seam", "burr"):
                    prediction[tuple(detection.pixels.T)] = (
                        1 if detection.kind == "seam" else 2
                    )
            np.savez_compressed(destination / "prediction.npz", pred=prediction)
            if entry["seed"] == SPLITS[split][0]:
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
                f"{entry['id']} {method} "
                f"P={metrics['precision']:.3f} R={metrics['recall']:.3f}",
                flush=True,
            )
    rows_file = comparison / "episodes.jsonl"
    rows_file.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    summary = {
        "split": split,
        "completed": len(records),
        "methods": methods,
        "summary": summarize(records),
    }
    verify_frozen_sources(comparison)
    write_json(comparison / "summary.json", summary)
    return summary


def evaluate_cad_dataset(output, split, methods=None):
    """Compare identical CAD views while preserving evaluator label isolation."""
    from .cad_defects import GENERATOR_CONFIG
    from .detector_candidates import candidate_configurations

    output = Path(output)
    methods = methods or detector_methods()
    dataset = output / "cad_dataset" / split
    manifest = json.loads((dataset / "manifest.json").read_text())
    comparison = output / "cad_comparison" / split
    comparison.mkdir(parents=True, exist_ok=True)
    if (comparison / "summary.json").exists():
        raise FileExistsError(f"Completed comparison exists: {comparison}")
    snapshot = comparison / "source_snapshot"
    shutil.copytree(
        project.SOURCE,
        snapshot,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    write_json(
        comparison / "protocol.json",
        {
            "dataset_protocol": manifest["protocol"],
            "generator_config": GENERATOR_CONFIG,
            "methods": methods,
            "candidate_configurations": candidate_configurations(),
            "reference_configuration": configuration(),
            "source_hashes": {
                filename.name: file_hash(filename)
                for filename in snapshot.glob("*.py")
            },
            "reference_input": "cad_reference has an ideal registered CAD view",
            "source_model_stage": "CAD version only, manufacturing stage unknown",
            "frozen_before_evaluation_utc": datetime.now(
                timezone.utc
            ).isoformat(),
        },
    )
    records = []
    for sample_record in manifest["samples"]:
        sample = Path(sample_record["sample_directory"])
        with np.load(sample / "observation.npz") as saved:
            observation = {name: saved[name] for name in saved.files}
        observation["inspection_bounds"] = np.stack(
            [
                observation["inspection_bounds_min"],
                observation["inspection_bounds_max"],
            ]
        )
        sample_id = f"{sample_record['case']}_{sample_record['seed']}"
        for method in methods:
            started = time.perf_counter()
            detections, auxiliary = predict(method, observation)
            inference_ms = (time.perf_counter() - started) * 1000
            planning_started = time.perf_counter()
            paths = (
                plan(detections, auxiliary, clearance_mm=3.0)
                if method != "threshold"
                else []
            )
            planning_ms = (time.perf_counter() - planning_started) * 1000
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
                "id": f"{split}/{sample_id}",
                "case": sample_record["case"],
                "seed": sample_record["seed"],
                "method": method,
                "split": split,
                "inference_ms": inference_ms,
                "planning_ms": planning_ms,
                "path_count": len(paths),
                "selected_backend": auxiliary.get("selected_backend", method),
                **metrics,
            }
            destination = comparison / method / sample_id
            destination.mkdir(parents=True, exist_ok=True)
            prediction = np.zeros(observation["depth"].shape, dtype=np.uint8)
            for detection in detections:
                if detection.kind in ("seam", "burr"):
                    prediction[tuple(detection.pixels.T)] = (
                        1 if detection.kind == "seam" else 2
                    )
            np.savez_compressed(destination / "prediction.npz", pred=prediction)
            write_json(
                destination / "detections.json",
                [detection.summary() for detection in detections],
            )
            write_json(destination / "metrics.json", row)
            write_json(destination / "paths.json", paths)
            if sample_record["seed"] == manifest["protocol"]["seeds"][0]:
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
                f"CAD {sample_id} {method} "
                f"P={metrics['precision']:.3f} R={metrics['recall']:.3f}",
                flush=True,
            )
            records.append(row)
    (comparison / "episodes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
    )
    summary = {
        "split": split,
        "completed": len(records),
        "methods": methods,
        "summary": summarize(records),
    }
    verify_frozen_sources(comparison)
    write_json(comparison / "summary.json", summary)
    return summary


def select_method(output):
    """Choose the observation-only method using development scenes only."""
    output = Path(output)
    table = json.loads(
        (output / "coupon_comparison/development/summary.json").read_text()
    )["summary"]
    method_rows = defaultdict(dict)
    for row in table:
        method_rows[row["method"]][row["case"]] = row
    ranked = []
    for method, rows in method_rows.items():
        normal = rows["normal"]
        coarse = rows["coarse"]
        eligible = (
            method not in ("threshold", "cad_reference")
            and all(
                row["normal_structure_selected_pixels"] == 0
                for row in rows.values()
            )
            and normal["no_action_samples"] == normal["samples"]
            and normal["review_samples"] == 0
            and coarse["unsupported_stump_selected_pixels"] == 0
        )
        ranked.append(
            {
                "method": method,
                "eligible": eligible,
                "thin_recall": rows["thin"]["recall"],
                "thin_f1": rows["thin"]["f1"],
                "mixed_f1": rows["mixed"]["f1"],
                "reference_required": method == "cad_reference",
            }
        )
    candidates = [row for row in ranked if row["eligible"]]
    if not candidates:
        raise RuntimeError("No observation-only detector meets normal guards")
    selected = max(
        candidates,
        key=lambda row: (row["thin_f1"], row["thin_recall"], row["mixed_f1"]),
    )["method"]
    selection = {
        "selected_observation_method": selected,
        "selected_reference_method": "cad_reference",
        "chosen_on_split": "development",
        "rationale": "normal/coarse guards, then thin F1 and recall",
        "ranking": ["thin_f1", "thin_recall", "mixed_f1"],
        "selected_utc": datetime.now(timezone.utc).isoformat(),
        "development_summary_sha256": file_hash(
            output / "coupon_comparison/development/summary.json"
        ),
        "ranked": ranked,
        "requires_holdout_confirmation": True,
    }
    write_json(output / "selection.json", selection)
    return selection


def execute_selected(output, case="mixed", seed=6007):
    """Execute the selected detector's full original FR5 simulation chain."""
    from .execution import execute, make_commands, preflight

    output = Path(output)
    chosen = json.loads((output / "selection.json").read_text())
    method = chosen["selected_observation_method"]
    sample = output / "coupon_dataset/holdout" / f"{case}_{seed}"
    run_output = output / f"selected_{method}_{case}_{seed}"
    if run_output.exists() and any(run_output.iterdir()):
        raise FileExistsError(run_output)
    run_output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with np.load(sample / "observation.npz") as observed:
        observation = {name: observed[name] for name in observed.files}
    simulation = Simulation(generate(seed, case))
    video = None
    try:
        write_json(run_output / "status.json", {"stage": "perception"})
        detections, auxiliary = predict(method, observation)
        paths = plan(detections, auxiliary, clearance_mm=3.0)
        annotated = overlay(
            observation["rgb"],
            detections,
            paths,
            observation["K"],
            observation["T_world_camera"],
        )
        Image.fromarray(observation["rgb"]).save(run_output / "camera_rgb.png")
        Image.fromarray(annotated).save(run_output / "detection_paths.png")
        Image.fromarray(simulation.render("overview", paths=paths)).save(
            run_output / "scene.png"
        )
        shutil.copyfile(
            sample / "observation.npz", run_output / "observation.npz"
        )
        shutil.copytree(
            project.SOURCE,
            run_output / "source_snapshot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        write_json(
            run_output / "detections.json",
            [detection.summary() for detection in detections],
        )
        write_json(run_output / "paths.json", paths)
        export_paths(paths, run_output)
        with np.load(sample / "truth.npz") as evaluator:
            metrics = score(
                detections,
                evaluator["truth"],
                paths,
                evaluator["K"],
                evaluator["T_world_camera"],
                evaluator["ideal_depth"],
            )
        result = {
            "case": case,
            "seed": seed,
            "method": method,
            "path_count": len(paths),
            "path_length_mm": path_length(paths) * 1000,
            "clearance_mm": 3.0,
            "perception": metrics,
            "simulation_only": True,
            "material_removal_simulated": False,
        }
        if not paths:
            raise RuntimeError("Selected detector has no executable path")
        commands, planning = make_commands(simulation, paths)
        check = preflight(simulation, commands)
        result["planning"], result["preflight"] = planning, check
        write_json(run_output / "preflight.json", check)
        np.savez_compressed(
            run_output / "joint_commands.npz",
            time_s=[command["t"] for command in commands],
            q_rad=[command["q"] for command in commands],
            xyz_m=[command["xyz"] for command in commands],
        )
        if check["passed"]:
            video = Video(run_output, observation["rgb"], annotated, paths)
            result["execution"] = execute(
                simulation, commands, run_output, video
            )
            executed = result["execution"]
            result["motion_gate_passed"] = bool(
                executed["unexpected_contact_count"] == 0
                and executed["abrasive_contact_steps"] == 0
                and executed["tracking_trace_p95_mm"] < 1.0
                and executed["tracking_trace_max_mm"] < 2.0
                and executed["tracking_trace_orientation_p95_deg"] < 1.0
            )
        else:
            result["motion_gate_passed"] = False
        result["wall_time_s"] = time.perf_counter() - started
        write_json(run_output / "result.json", result)
        write_json(run_output / "status.json", {"stage": "complete"})
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    except Exception as error:
        write_json(
            run_output / "status.json", {"stage": "failed", "error": str(error)}
        )
        raise
    finally:
        if video:
            video.close()
        simulation.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--stage",
        choices=("prepare", "evaluate", "cad-evaluate", "select", "execute"),
        required=True,
    )
    parser.add_argument("--split", choices=tuple(SPLITS), default="development")
    parser.add_argument("--case", choices=CASES, default="mixed")
    parser.add_argument("--seed", type=int, default=6007)
    args = parser.parse_args()
    if args.stage == "prepare":
        prepare_coupon_dataset(args.output)
    elif args.stage == "evaluate":
        evaluate_coupon_dataset(args.output, args.split)
    elif args.stage == "cad-evaluate":
        evaluate_cad_dataset(args.output, args.split)
    elif args.stage == "select":
        print(json.dumps(select_method(args.output), ensure_ascii=False))
    elif args.stage == "execute":
        execute_selected(args.output, args.case, args.seed)


if __name__ == "__main__":
    main()
