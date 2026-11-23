#!/usr/bin/env python3
"""Pair the frozen learned test with the original detector on identical RGB-D."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.defect_training import load_labels, score_confusion
from daiyi_grinding.learned_defects import LearnedDetector, file_sha256
from daiyi_grinding.thin_reference import detect_thin_reference
from daiyi_grinding.workstation_inspection import BOUNDS, CELL_DETECTOR_CONFIG
from daiyi_grinding.workstation_model import Workstation, write_json


def compare(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    evaluated = json.loads(Path(args.test_result).read_text())
    rows = json.loads(Path(args.split_index).read_text())["splits"]["test"]
    recorded = {
        (row["seed"], row["case"], row["view"]): row
        for row in evaluated["views"]
    }
    if len(recorded) != len(rows):
        raise ValueError(
            "Frozen test observations differ from the paired index"
        )
    checkpoint = evaluated["checkpoint"]
    if file_sha256(checkpoint["checkpoint"]) != checkpoint["checkpoint_sha256"]:
        raise ValueError("Frozen learned weights changed after test evaluation")
    detector = LearnedDetector(checkpoint["checkpoint"])
    sim = Workstation(
        output / "nominal_cad_reconstruction",
        seed=min(row["seed"] for row in rows),
        case="casting_normal",
        tool="belt",
        casting_profile="team_prior_20261121",
    )
    nominal_geoms = [
        index
        for index in range(sim.model.ngeom)
        if sim.model.geom(index).name.startswith("wp_")
    ]
    total = np.zeros((3, 3), np.int64)
    learned_total = np.zeros((3, 3), np.int64)
    by_case = {}
    learned_by_case = {}
    normal_fp = 0
    learned_normal_fp = 0
    review_pixels = 0
    reconstruction_max_mm = 0.0
    records = []
    try:
        for number, row in enumerate(rows):
            key = (row["seed"], row["case"], row["view"])
            learned = recorded[key]
            if learned["public"] != row["public"]:
                raise ValueError(
                    "Paired detectors must use the same observation"
                )
            with np.load(row["public"], allow_pickle=False) as arrays:
                public = {key: arrays[key] for key in arrays.files}
            # Reconstruct only the supplied nominal CAD. This never loads defect
            # geometry, evaluator masks or private root coordinates.
            pose = public["T_world_workpiece"]
            address = sim.wp_qadr
            quaternion = Rotation.from_matrix(pose[:3, :3]).as_quat()
            sim.data.qpos[address : address + 3] = pose[:3, 3]
            sim.data.qpos[address + 3 : address + 7] = quaternion[[3, 0, 1, 2]]
            sim.data.qpos[sim.table_qadr] = np.deg2rad(
                float(public["encoder_angle_deg"])
            )
            mujoco.mj_forward(sim.model, sim.data)
            actual_k, actual_t = sim.camera_info()
            if not (
                np.allclose(actual_k, public["K"], atol=1e-7)
                and np.allclose(
                    actual_t, public["T_workpiece_camera"], atol=1e-7
                )
            ):
                raise ValueError("Nominal reconstruction camera differs")
            segmentation = sim.render(mode="segmentation", reference=True)
            reference_mask = np.isin(segmentation[..., 0], nominal_geoms)
            reference = sim.render(mode="depth", reference=True)
            error = (
                np.abs(
                    reference[reference_mask]
                    - public["reference_depth"][reference_mask]
                )
                * 1000
            )
            max_error = float(error.max()) if len(error) else 0.0
            if max_error > 0.15:
                raise ValueError(
                    "Nominal CAD reconstruction differs from frozen depth"
                )
            reconstruction_max_mm = max(reconstruction_max_mm, max_error)
            detections, observation = detect_thin_reference(
                public["rgb"],
                public["depth"],
                public["reference_depth"],
                public["K"],
                public["T_workpiece_camera"],
                inspection_bounds=BOUNDS,
                config=CELL_DETECTOR_CONFIG,
                reference_object_mask=reference_mask,
                allow_sloped_surface=True,
            )
            predicted = np.zeros(public["depth"].shape, np.uint8)
            review = np.zeros(predicted.shape, bool)
            for detection in detections:
                pixels = tuple(detection.pixels.T)
                if detection.kind in {"seam", "burr"}:
                    predicted[pixels] = 1 if detection.kind == "seam" else 2
                elif detection.kind == "review":
                    review[pixels] = True
            model_detections, _ = detector.detect(
                public["rgb"],
                public["depth"],
                public["reference_depth"],
                observation,
            )
            model_predicted = np.zeros(predicted.shape, np.uint8)
            for detection in model_detections:
                model_predicted[tuple(detection.pixels.T)] = (
                    1 if detection.kind == "seam" else 2
                )
            # Supervision is read only after both candidate pipelines return.
            truth = load_labels(row)
            confusion = np.bincount(
                (truth.astype(np.int64) * 3 + predicted).ravel(), minlength=9
            ).reshape(3, 3)
            model_confusion = np.bincount(
                (truth.astype(np.int64) * 3 + model_predicted).ravel(),
                minlength=9,
            ).reshape(3, 3)
            total += confusion
            learned_total += model_confusion
            by_case.setdefault(row["case"], np.zeros((3, 3), np.int64))
            by_case[row["case"]] += confusion
            learned_by_case.setdefault(row["case"], np.zeros((3, 3), np.int64))
            learned_by_case[row["case"]] += model_confusion
            normal_fp += (
                int(confusion[0, 1:].sum())
                if row["case"] == "casting_normal"
                else 0
            )
            learned_normal_fp += (
                int(model_confusion[0, 1:].sum())
                if row["case"] == "casting_normal"
                else 0
            )
            review_pixels += int(review.sum())
            records.append(
                {
                    "seed": row["seed"],
                    "case": row["case"],
                    "view": row["view"],
                    "public": row["public"],
                    "public_sha256": file_sha256(row["public"]),
                    "geometric": score_confusion(confusion),
                    "geometric_confusion": confusion.tolist(),
                    "geometric_review_pixels": int(review.sum()),
                    "learned_raw": learned["metrics"],
                    "learned": score_confusion(model_confusion),
                    "learned_confusion": model_confusion.tolist(),
                    "nominal_reconstruction_max_mm": max_error,
                }
            )
            if number % 24 == 0:
                print(json.dumps({"paired_view": number}), flush=True)
    finally:
        sim.close()
    result = {
        "seeds": evaluated["seeds"],
        "views": len(rows),
        "cad_count": 1,
        "model_sha256": checkpoint["checkpoint_sha256"],
        "frozen_test_result_sha256": file_sha256(args.test_result),
        "geometric": score_confusion(total),
        "learned": score_confusion(learned_total),
        "learned_raw": evaluated["raw_semantic_metrics"],
        "geometric_normal_false_positive_pixels": normal_fp,
        "learned_normal_false_positive_pixels": learned_normal_fp,
        "learned_raw_normal_false_positive_pixels": evaluated[
            "normal_false_positive_pixels"
        ],
        "geometric_review_pixels": review_pixels,
        "nominal_reconstruction_max_mm": reconstruction_max_mm,
        "per_case": {
            key: {
                "geometric": score_confusion(value),
                "learned": score_confusion(learned_by_case[key]),
                "learned_raw": evaluated["per_case"][key],
            }
            for key, value in by_case.items()
        },
        "scope": (
            "Full-view synthetic semantic masks on identical stored RGB-D. "
            "Both candidate interfaces use the same nominal CAD mask, bounds, "
            "depth-validity and CAD-edge guards. Geometry labels seam/burr only; "
            "its review candidates are reported separately. Raw learned scores "
            "are retained separately from measurable learned components. Frozen "
            "weights/threshold unchanged. Not robot execution or real accuracy."
        ),
        "truth_used_for_inference": False,
        "records": records,
    }
    write_json(output / "comparison.json", result)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "records"},
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-result", required=True)
    parser.add_argument("--split-index", required=True)
    parser.add_argument("--output", required=True)
    compare(parser.parse_args())


if __name__ == "__main__":
    main()
