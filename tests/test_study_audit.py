"""Independent audit checks catch label, aggregation and sample leakage errors."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from daiyi_grinding.study_audit import (
    audit_cad_comparison,
    audit_dataset,
    compare_fields,
    pixel_counts,
    pooled_summary,
    projected_coverage,
    replay_prediction,
    sha256,
)


class StudyAuditTests(unittest.TestCase):
    def test_counts_separate_type_errors_from_false_positives(self):
        truth = np.array([[1, 2, 3], [4, 0, 1]], dtype=np.uint8)
        prediction = np.array([[2, 2, 1], [2, 1, 0]], dtype=np.uint8)
        actual = pixel_counts(prediction, truth)
        self.assertEqual(actual["tp"], 2)
        self.assertEqual(actual["fp"], 3)
        self.assertEqual(actual["fn"], 1)
        self.assertEqual(actual["normal_structure_selected_pixels"], 1)
        self.assertEqual(actual["unsupported_stump_selected_pixels"], 1)
        self.assertEqual(actual["wrong_type_pixels"], 1)
        self.assertEqual(actual["selected_pixels"], 5)
        self.assertEqual(actual["target_pixels"], 3)
        self.assertAlmostEqual(actual["precision"], 0.4)
        self.assertAlmostEqual(actual["recall"], 2 / 3)
        self.assertAlmostEqual(actual["f1"], 0.5)

    def test_rejects_shapes_and_undeclared_class_ids(self):
        valid = np.zeros((2, 3), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError, "shapes differ"):
            pixel_counts(valid, valid[:, :2])
        with self.assertRaisesRegex(ValueError, "undeclared"):
            pixel_counts(np.full((2, 3), 3, dtype=np.uint8), valid)
        with self.assertRaisesRegex(ValueError, "integers"):
            pixel_counts(valid.astype(float), valid)

    def test_normal_summary_preserves_undefined_recall(self):
        truth = np.full((2, 3), 3, dtype=np.uint8)
        counts = pixel_counts(np.zeros_like(truth), truth)
        summary = pooled_summary(
            [
                {
                    **counts,
                    "method": "geometry",
                    "case": "normal",
                    "auto_regions": 0,
                    "review_regions": 0,
                    "defect_footprint_coverage_3mm": None,
                }
            ]
        )[0]
        self.assertIsNone(summary["precision"])
        self.assertIsNone(summary["recall"])
        self.assertIsNone(summary["f1"])
        self.assertEqual(summary["no_action_samples"], 1)

    def test_pooled_precision_uses_pixel_counts_not_mean_rates(self):
        rows = []
        for selected, labels in (
            (
                np.array([[1, 0]], dtype=np.uint8),
                np.array([[1, 1]], dtype=np.uint8),
            ),
            (
                np.array([[1, 1]], dtype=np.uint8),
                np.array([[0, 1]], dtype=np.uint8),
            ),
        ):
            rows.append(
                {
                    **pixel_counts(selected, labels),
                    "method": "geometry",
                    "case": "mixed",
                    "auto_regions": 1,
                    "review_regions": 0,
                    "defect_footprint_coverage_3mm": 0.5,
                }
            )
        summary = pooled_summary(rows)[0]
        self.assertEqual(summary["tp"], 2)
        self.assertEqual(summary["fp"], 1)
        self.assertAlmostEqual(summary["precision"], 2 / 3)
        self.assertAlmostEqual(summary["recall"], 2 / 3)

    def test_coverage_uses_world_transform_and_three_mm_radius(self):
        truth = np.array([[1, 0, 1]], dtype=np.uint8)
        depth = np.full(truth.shape, 0.5)
        intrinsics = np.diag([100.0, 100.0, 1.0])
        transform = np.eye(4)
        transform[:3, 3] = [0.2, -0.1, 0.7]
        paths = [{"xyz": [[0.2, -0.1, 1.2], [0.201, -0.1, 1.2]]}]
        self.assertAlmostEqual(
            projected_coverage(truth, depth, intrinsics, transform, paths),
            0.5,
        )
        self.assertEqual(
            projected_coverage(truth, depth, intrinsics, transform, []), 0.0
        )
        self.assertIsNone(
            projected_coverage(
                np.zeros_like(truth), depth, intrinsics, transform, paths
            )
        )

    def test_missing_and_changed_metrics_are_reported(self):
        issues = compare_fields(
            {"tp": 10, "recall": 0.5},
            {"tp": 11},
            ("tp", "recall"),
            "sample",
        )
        self.assertEqual(len(issues), 2)
        self.assertIn("missing recall", issues[1])

    def test_dataset_detects_copied_depth_across_splits(self):
        with tempfile.TemporaryDirectory() as directory:
            study = Path(directory)
            dataset = study / "coupon_dataset"
            dataset.mkdir()
            protocol = {
                "cases": ["normal"],
                "splits": {"development": [5007], "holdout": [6007]},
            }
            (dataset / "generation_protocol.json").write_text(
                json.dumps(protocol)
            )
            entries = []
            for split, seed in (("development", 5007), ("holdout", 6007)):
                sample = dataset / split / f"normal_{seed}"
                sample.mkdir(parents=True)
                observed = sample / "observation.npz"
                truth_path = sample / "truth.npz"
                depth = np.full((3, 4), 0.5, dtype=np.float32)
                np.savez_compressed(
                    observed,
                    rgb=np.zeros((3, 4, 3), dtype=np.uint8),
                    depth=depth,
                    reference_depth=depth + 0.0001,
                    K=np.eye(3),
                    T_world_camera=np.eye(4),
                    inspection_bounds=np.zeros((2, 3)),
                )
                np.savez_compressed(
                    truth_path, truth=np.zeros((3, 4), dtype=np.uint8)
                )
                entries.append(
                    {
                        "id": f"{split}/normal_{seed}",
                        "sample": str(sample.relative_to(study)),
                        "split": split,
                        "seed": seed,
                        "case": "normal",
                        "observation_sha256": sha256(observed),
                        "truth_sha256": sha256(truth_path),
                    }
                )
            (dataset / "index.json").write_text(json.dumps(entries))
            audited = audit_dataset(study)
            self.assertEqual(
                audited["cross_split_duplicate_counts"]["depth_arrays"], 1
            )
            self.assertTrue(
                any(
                    "duplicate depth_arrays" in issue
                    for issue in audited["issues"]
                )
            )

    def test_replay_blocks_detector_loading_private_cad_labels(self):
        observed = {
            "rgb": np.zeros((2, 3, 3), dtype=np.uint8),
            "depth": np.ones((2, 3)),
            "reference_depth": np.ones((2, 3)),
            "K": np.eye(3),
            "T_world_camera": np.eye(4),
            "inspection_bounds_min": np.zeros(3),
            "inspection_bounds_max": np.ones(3),
        }

        def invalid_detector(method, public):
            np.load("/tmp/evaluator/truth.npz")
            return [], {}

        with mock.patch(
            "daiyi_grinding.defect_study.predict", side_effect=invalid_detector
        ):
            with self.assertRaisesRegex(RuntimeError, "evaluation labels"):
                replay_prediction("cad_reference", observed, (2, 3))

    def test_cad_comparison_catches_changed_false_positive_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            sample = dataset / "burr_seed1"
            evaluator = sample / "evaluator"
            evaluator.mkdir(parents=True)
            protocol = {"split": "development", "cases": ["burr"], "seeds": [1]}
            (dataset / "manifest.json").write_text(
                json.dumps(
                    {
                        "protocol": protocol,
                        "samples": [{"case": "burr", "seed": 1}],
                    }
                )
            )
            truth = np.array([[2, 2, 3], [0, 0, 0]], dtype=np.uint8)
            prediction = np.array([[2, 0, 2], [0, 0, 0]], dtype=np.uint8)
            np.savez_compressed(
                evaluator / "truth.npz",
                truth=truth,
                ideal_depth=np.ones((2, 3)),
                trueK=np.eye(3),
                trueT=np.eye(4),
            )
            np.savez_compressed(
                sample / "observation.npz", depth=np.ones((2, 3))
            )
            comparison = root / "comparison"
            snapshot = comparison / "source_snapshot"
            snapshot.mkdir(parents=True)
            classifier = snapshot / "perception.py"
            classifier.write_text('"""Test frozen source."""\n')
            source_hash = {"perception.py": sha256(classifier)}
            (comparison / "protocol.json").write_text(
                json.dumps(
                    {
                        "dataset_protocol": protocol,
                        "methods": ["geometry"],
                        "source_hashes": source_hash,
                    }
                )
            )
            (comparison / "source_integrity.json").write_text(
                json.dumps(
                    {
                        "source_hashes_after": source_hash,
                    }
                )
            )
            counts = pixel_counts(prediction, truth)
            counts.pop("target_pixels")
            row = {
                **counts,
                "id": "development/burr_1",
                "split": "development",
                "case": "burr",
                "seed": 1,
                "method": "geometry",
                "auto_regions": 1,
                "review_regions": 0,
                "path_count": 0,
                "defect_footprint_coverage_3mm": 0.0,
            }
            row["fp"] = 0
            destination = comparison / "geometry/burr_1"
            destination.mkdir(parents=True)
            np.savez_compressed(destination / "prediction.npz", pred=prediction)
            (destination / "metrics.json").write_text(json.dumps(row))
            (destination / "detections.json").write_text(
                json.dumps([{"kind": "burr"}])
            )
            (destination / "paths.json").write_text("[]")
            (comparison / "episodes.jsonl").write_text(json.dumps(row) + "\n")
            (comparison / "summary.json").write_text(
                json.dumps(
                    {
                        "methods": ["geometry"],
                        "completed": 1,
                        "summary": pooled_summary([row]),
                    }
                )
            )
            audited = audit_cad_comparison(dataset, comparison)
            self.assertTrue(
                any("fp expected 1" in issue for issue in audited["issues"])
            )


if __name__ == "__main__":
    unittest.main()
