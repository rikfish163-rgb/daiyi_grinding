"""Targeted checks for the offline perception stress suite."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daiyi_grinding import perception_stress
from daiyi_grinding.perception import Detection
from daiyi_grinding.perception_stress import (
    CELL_BOUNDS,
    PERTURBATIONS,
    _reference_hybrid_candidates,
    candidate_source_parity,
    evaluate_frame,
    hybrid_candidates_interface,
    load_labels,
    load_observation,
    main,
    object_matching,
    perturb_depth,
    reconstruct_observation,
    run,
    score_view,
    sha256,
    stable_hash,
    synthetic_root_localization,
    testability,
)


def synthetic_frame(height=192, width=256, bump_mm=4.0, bump_size=12):
    """Flat registered plane with one compact burr-like bump."""
    depth = np.full((height, width), 0.2, dtype=np.float32)
    reference = depth.copy()
    center = (height // 2, width // 2)
    half = bump_size // 2
    rows = slice(center[0] - half, center[0] + half)
    cols = slice(center[1] - half, center[1] + half)
    depth[rows, cols] -= bump_mm / 1000
    rgb = np.full((height, width, 3), 128, dtype=np.uint8)
    intrinsics = np.array(
        [
            [200.0, 0.0, (width - 1) / 2],
            [0.0, 200.0, (height - 1) / 2],
            [0.0, 0.0, 1.0],
        ]
    )
    transform = np.eye(4)
    semantic = np.zeros((height, width), dtype=np.uint8)
    semantic[rows, cols] = 2
    instance = np.zeros((height, width), dtype=np.uint16)
    instance[rows, cols] = 1
    return {
        "rgb": rgb,
        "depth": depth,
        "reference_depth": reference,
        "K": intrinsics,
        "T_workpiece_camera": transform,
        "labels": {
            "semantic": semantic,
            "instance": instance,
            "root_points_workpiece_m": np.zeros((0, 3), dtype=np.float32),
            "root_instances": np.zeros(0, dtype=np.uint16),
            "root_in_image": np.zeros(0, dtype=bool),
            "root_depth_consistent": np.zeros(0, dtype=bool),
            "root_sensor_depth_valid": np.zeros(0, dtype=bool),
        },
    }


def write_frame(directory, frame):
    directory = Path(directory)
    directory.mkdir(exist_ok=True)
    public = dict(frame)
    labels = public.pop("labels")
    public_path = directory / "view_00.npz"
    labels_path = directory / "view_00_labels.npz"
    np.savez_compressed(public_path, **public)
    np.savez_compressed(labels_path, **labels)
    return public_path, labels_path


class PerceptionStressTests(unittest.TestCase):
    def test_import_does_not_load_torch_or_mujoco(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import daiyi_grinding.perception_stress; "
                "assert 'torch' not in sys.modules; "
                "assert 'mujoco' not in sys.modules; print('lightweight import')",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("lightweight import", result.stdout)

    def test_cell_bounds_and_perturbation_specs_are_ordered(self):
        lower, upper = np.asarray(CELL_BOUNDS)
        self.assertTrue(np.all(lower < upper))
        kinds = [spec["kind"] for spec in PERTURBATIONS]
        self.assertIn("small_target_stress", kinds)
        self.assertEqual(len(kinds), len(set(kinds)))

    def test_geometric_reconstruction_finds_the_synthetic_bump(self):
        frame = synthetic_frame()
        detections, observation = reconstruct_observation(frame)
        self.assertTrue(len(detections) >= 1)
        kind = {detection.kind for detection in detections}
        self.assertIn("burr", kind)
        self.assertIn("noise_sigma_mm", observation)
        self.assertEqual(observation["surface"].shape, frame["depth"].shape)

    def test_scoring_requires_labels_only_after_detection(self):
        frame = synthetic_frame()
        detections, observation = reconstruct_observation(frame)
        with tempfile.TemporaryDirectory() as directory:
            labels = load_labels(write_frame(directory, frame)[1])
        metrics = score_view(detections, labels, observation)
        truth = labels["semantic"] == 2
        self.assertGreaterEqual(metrics["binary"]["tp"], 1)
        self.assertLessEqual(metrics["binary"]["fn"], int(truth.sum()) * 0.5)
        self.assertEqual(metrics["object_matching"]["matched_objects"], 1)
        self.assertEqual(
            metrics["testability"]["measurable_defect_pixels"],
            int(truth.sum()),
        )
        root = metrics["synthetic_root_localization"]
        self.assertEqual(root["computed_instances"], 0)
        self.assertEqual(root["missing_instances"], 1)
        self.assertIsNone(root["mean_region_root_error_mm"])

    def test_all_predictions_precede_evaluator_loading(self):
        frame = synthetic_frame()
        events = []

        class Detector:
            threshold = 0.35

            def detect(self, rgb, depth, reference, observation):
                events.append("learned")
                return [], None

        def labels_after_predictions(path):
            events.append("labels")
            return frame["labels"]

        def hybrid_after_predictions(geometric, learned, shape):
            events.append("hybrid")
            return _reference_hybrid_candidates(
                geometric, learned, shape
            ), "hybrid"

        with tempfile.TemporaryDirectory() as directory:
            public_path, labels_path = write_frame(directory, frame)
            with (
                patch.object(
                    perception_stress,
                    "load_labels",
                    side_effect=labels_after_predictions,
                ),
                patch.object(
                    perception_stress,
                    "hybrid_candidates_interface",
                    side_effect=hybrid_after_predictions,
                ),
            ):
                record = evaluate_frame(public_path, labels_path, Detector())
        self.assertEqual(events, ["learned", "hybrid", "labels"])
        self.assertEqual(
            set(record["paired_observation_hashes"]),
            {"rgb", "depth", "reference_depth", "K", "T_workpiece_camera"},
        )

    def test_public_observation_rejects_evaluator_fields(self):
        frame = synthetic_frame()
        with tempfile.TemporaryDirectory() as directory:
            public_path, _ = write_frame(directory, frame)
            public = load_observation(public_path)
            public["semantic"] = frame["labels"]["semantic"]
            np.savez_compressed(public_path, **public)
            with self.assertRaisesRegex(ValueError, "Evaluator fields"):
                load_observation(public_path)

    def test_review_discovery_is_separate_from_classified_candidates(self):
        frame = synthetic_frame()
        detections, observation = reconstruct_observation(frame)
        review = [replace(item, kind="review") for item in detections]
        metrics = score_view(review, frame["labels"], observation)
        self.assertEqual(metrics["binary"]["tp"], 0)
        self.assertGreater(metrics["anomaly_binary"]["tp"], 0)
        self.assertEqual(metrics["object_matching"]["matched_objects"], 0)
        self.assertEqual(
            metrics["anomaly_object_matching"]["matched_objects"], 1
        )
        preserve = [replace(item, kind="preserve") for item in detections]
        metrics = score_view(preserve, frame["labels"], observation)
        self.assertGreater(metrics["preserve_pixels"], 0)
        self.assertEqual(metrics["binary"]["tp"], 0)

    def test_merged_prediction_cannot_match_two_instances(self):
        frame = synthetic_frame()
        detections, observation = reconstruct_observation(frame)
        self.assertEqual(len(detections), 1)
        labels = dict(frame["labels"])
        labels["instance"] = labels["instance"].copy()
        center = labels["instance"].shape[0] // 2
        labels["instance"][:center][labels["instance"][:center] == 1] = 2
        matched = object_matching(detections, labels, observation)
        self.assertEqual(matched["visible_objects"], 2)
        self.assertEqual(matched["matched_objects"], 1)
        self.assertEqual(matched["counts"]["fn"], 1)

    def test_tiny_instances_stay_in_pixel_scores(self):
        frame = synthetic_frame()
        _, observation = reconstruct_observation(frame)
        semantic = np.zeros(frame["depth"].shape, dtype=np.uint8)
        instance = np.zeros(frame["depth"].shape, dtype=np.uint16)
        semantic[90, 120:123] = 2
        instance[90, 120:123] = 1
        labels = {**frame["labels"], "semantic": semantic, "instance": instance}
        metrics = score_view([], labels, observation)
        self.assertEqual(metrics["binary"]["fn"], 3)
        self.assertEqual(metrics["object_matching"]["visible_objects"], 0)
        self.assertEqual(metrics["size_strata"]["small"]["defect_pixels"], 3)
        self.assertEqual(
            metrics["testability"]["tiny_instances_below_cell_eligibility"], 1
        )

    def test_roots_without_measurable_points_are_reported_missing(self):
        frame = synthetic_frame()
        detections, observation = reconstruct_observation(frame)
        labels = frame["labels"]
        labels = dict(labels)
        labels["root_points_workpiece_m"] = np.array(
            [[0.0, 0.0, 0.21]], dtype=np.float32
        )
        labels["root_instances"] = np.array([1], dtype=np.uint16)
        labels["root_in_image"] = np.array([True])
        labels["root_depth_consistent"] = np.array([False])
        labels["root_sensor_depth_valid"] = np.array([False])
        root = synthetic_root_localization(detections, labels, observation)
        self.assertEqual(root["computed_instances"], 0)
        record = root["records"][0]
        self.assertEqual(record["status"], "missing")
        self.assertIn("measurable root point", record["reason"])
        self.assertIsNone(root["mean_region_root_error_mm"])

    def test_testability_reports_root_fractions_without_fabrication(self):
        frame = synthetic_frame()
        _, observation = reconstruct_observation(frame)
        labels = dict(frame["labels"])
        labels["root_points_workpiece_m"] = np.array(
            [[0.0, 0.0, 0.21]], dtype=np.float32
        )
        labels["root_instances"] = np.array([1], dtype=np.uint16)
        labels["root_in_image"] = np.array([True])
        labels["root_depth_consistent"] = np.array([True])
        labels["root_sensor_depth_valid"] = np.array([False])
        report = testability(labels, observation)
        self.assertEqual(
            report["root_measurability"]["measurable_root_points"], 0
        )
        self.assertEqual(
            report["root_measurability"]["measurable_root_fraction"], 0.0
        )

    def test_pressure_invalidates_archived_root_sensor_validity(self):
        frame = synthetic_frame()
        row, col = (length // 2 for length in frame["depth"].shape)
        labels = dict(frame["labels"])
        labels.update(
            root_points_workpiece_m=np.array([[0.0, 0.0, 0.2]], np.float32),
            root_instances=np.array([1], np.uint16),
            root_pixels=np.array([[col, row]], np.int32),
            root_in_image=np.array([True]),
            root_depth_consistent=np.array([True]),
            root_sensor_depth_valid=np.array([True]),
        )
        _, observation = reconstruct_observation(frame)
        self.assertEqual(
            testability(labels, observation)["root_measurability"][
                "measurable_root_points"
            ],
            1,
        )
        modified = dict(frame)
        modified["depth"] = frame["depth"].copy()
        modified["depth"][row, col] = np.nan
        _, observation = reconstruct_observation(modified)
        self.assertEqual(
            testability(labels, observation)["root_measurability"][
                "measurable_root_points"
            ],
            0,
        )

    def test_perturbations_are_seeded_and_preserve_nans(self):
        frame = synthetic_frame()
        depth = frame["depth"].copy()
        depth[0, 0] = np.nan
        for spec in PERTURBATIONS:
            first = perturb_depth(
                depth,
                spec,
                np.random.default_rng(7),
                None
                if spec["kind"] != "small_target_stress"
                else {"residual": np.zeros(depth.shape)},
            )
            second = perturb_depth(
                depth,
                spec,
                np.random.default_rng(7),
                None
                if spec["kind"] != "small_target_stress"
                else {"residual": np.zeros(depth.shape)},
            )
            np.testing.assert_allclose(first, second, equal_nan=True)
            self.assertTrue(np.isnan(first[0, 0]))
        biased = perturb_depth(
            frame["depth"].copy(),
            {"kind": "depth_bias", "bias_mm": 0.3},
            np.random.default_rng(0),
        )
        delta = biased - frame["depth"]
        np.testing.assert_allclose(delta[1:, 1:], 0.3 / 1000, atol=1e-6)
        with self.assertRaisesRegex(ValueError, "observation"):
            perturb_depth(
                frame["depth"].copy(),
                {"kind": "small_target_stress"},
                np.random.default_rng(0),
            )

    def test_small_target_pressure_uses_measured_component_size(self):
        depth = np.full((32, 48), 0.2, np.float32)
        residual = np.zeros(depth.shape, np.float32)
        residual[2:4, 2:6] = 0.004
        residual[12:28, 20:40] = 0.004
        modified = perturb_depth(
            depth,
            {
                "kind": "small_target_stress",
                "residual_seed_mm": 0.4,
                "dropout_fraction": 1.0,
                "maximum_component_pixels": 64,
            },
            np.random.default_rng(0),
            {"residual": residual},
        )
        self.assertEqual(np.isnan(modified).sum(), 8)
        self.assertTrue(np.isfinite(modified[12:28, 20:40]).all())

    def test_hybrid_merge_keeps_preferred_and_drops_overlaps(self):
        shape = (32, 32)
        pixels_a = np.array([[2, 3], [3, 3], [4, 3]])
        pixels_b = np.array([[2, 20], [3, 20], [4, 20]])
        geometric = [
            Detection(1, "seam", pixels_a, np.zeros((3, 3)), 1, 1, 1, 0.5, "g")
        ]
        learned = [
            Detection(1, "burr", pixels_b, np.zeros((3, 3)), 1, 1, 1, 0.5, "l"),
            Detection(2, "burr", pixels_a, np.zeros((3, 3)), 1, 1, 1, 0.5, "l"),
        ]
        merged = _reference_hybrid_candidates(geometric, learned, shape)
        self.assertEqual(len(merged), 2)
        self.assertEqual([d.id for d in merged], [1, 2])
        merged, backend = hybrid_candidates_interface(geometric, learned, shape)
        self.assertEqual(len(merged), 2)
        self.assertIn("hybrid", backend)

    def test_stable_hash_is_json_independent_of_key_order(self):
        self.assertEqual(
            stable_hash({"a": 1, "b": 2}), stable_hash({"b": 2, "a": 1})
        )

    def test_changed_hybrid_source_is_rejected_without_mujoco_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "workstation_inspection.py"
            source.write_text(
                "def hybrid_candidates(geometric, learned, shape):\n"
                "    return learned\n",
                encoding="utf-8",
            )
            candidate_source_parity.cache_clear()
            with patch.object(perception_stress, "HYBRID_SOURCE", source):
                with self.assertRaisesRegex(ValueError, "policy changed"):
                    candidate_source_parity()
            candidate_source_parity.cache_clear()

    def test_missing_pair_labels_are_not_silently_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            public = batch / "case" / "public"
            public.mkdir(parents=True)
            np.savez_compressed(public / "view_00.npz", depth=np.zeros((2, 2)))
            np.savez_compressed(
                public / "view_00_weldgrind_cloud.npz",
                points=np.zeros((0, 3)),
            )
            with patch.object(perception_stress, "REGRESSION_BATCH", batch):
                _, pairs = perception_stress._case_views("case")
            self.assertEqual(len(pairs), 1)
            self.assertFalse(pairs[0][1].exists())

    def test_nonempty_output_is_preserved_before_model_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            history = output / "historical.json"
            history.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(
                perception_stress, "_load_frozen_detector"
            ) as loader:
                with self.assertRaisesRegex(ValueError, "not empty"):
                    run(output)
                loader.assert_not_called()
            self.assertEqual(history.read_text(), '{"keep": true}')

    def test_unexpected_frame_exception_produces_failed_evidence_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "injected_failure"
            with (
                patch.object(
                    perception_stress,
                    "evaluate_frame",
                    side_effect=KeyError("injected frame fault"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "--output",
                        str(output),
                        "--cases",
                        "casting_normal_seed9301",
                        "--perturbation-cases",
                        "",
                        "--limit-views",
                        "1",
                    ]
                )
            self.assertEqual(code, 1)
            evidence = json.loads((output / "evidence.json").read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertEqual(
                evidence["failures"][0]["exception_type"], "KeyError"
            )
            self.assertEqual(
                evidence["metrics"]["expected_regression_frames"], 1
            )
            self.assertEqual(evidence["metrics"]["known_regression_frames"], 0)

    def test_smoke_run_on_one_archived_normal_view(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "perception"
            summary = run(
                output,
                seed=20261123,
                case_names=("casting_normal_seed9301",),
                perturbation_cases=(),
                view_limit=1,
            )
            self.assertEqual(summary["metrics"]["known_regression_frames"], 1)
            self.assertEqual(summary["metrics"]["perturbation_count"], 0)
            self.assertEqual(summary["failures"], [])
            self.assertEqual(summary["status"], "complete")
            evidence = json.loads((output / "evidence.json").read_text())
            self.assertEqual(evidence["scope"]["mujoco_processes_started"], 0)
            self.assertFalse(evidence["scope"]["mujoco_imported_by_suite"])
            runtime = evidence["resources"]["learned_inference"]
            self.assertEqual(runtime["device"], "cpu")
            self.assertEqual(runtime["torch_threads"], 1)
            self.assertEqual(runtime["opencv_threads"], 1)
            self.assertFalse(runtime["weights_updated"])
            self.assertEqual(
                runtime["checkpoint"]["sha256"],
                sha256(runtime["checkpoint"]["path"]),
            )
            self.assertTrue(
                evidence["real_data_pending"][
                    "real_root_localization_within_0_1mm"
                ]
            )
            for artifact in evidence["artifacts"]:
                self.assertTrue(Path(artifact["path"]).is_file())
                self.assertEqual(artifact["sha256"], sha256(artifact["path"]))
            normal = summary["metrics"]["comparison"]
            for interface in ("geometric", "learned", "hybrid"):
                self.assertEqual(normal[interface]["binary_pooled"]["fp"], 0)


if __name__ == "__main__":
    unittest.main()
