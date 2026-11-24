"""Normal-scan injection and saved closed-loop harvesting, without simulation."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from daiyi_grinding import project
from daiyi_grinding.dataset_pipeline import BUNDLE_SCHEMA, SEMANTIC_LABELS
from daiyi_grinding.learned_defects import sensor_features
from daiyi_grinding.real_normal_synthesis import (
    HARD_EXAMPLE_WEIGHTS,
    harvest_closed_loop_hard_examples,
    inject_synthetic_defects_on_scan,
    write_injected_bundle,
)
from daiyi_grinding.scaled_training import load_scaled_bundle


def flat_scan():
    depth = np.full((140, 180), 0.7, np.float32)
    rgb = np.full((*depth.shape, 3), 144, np.uint8)
    camera = np.array(
        [[1400, 0, 89.5], [0, 1400, 69.5], [0, 0, 1]], dtype=float
    )
    return rgb, depth, depth.copy(), camera


def save_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def saved_run(root, *, positive=True, review=True):
    """Fixture mirrors signed-anchor reports, best-view boxes and P0 logs."""
    root.mkdir()
    initial = [
        {"id": 1, "kind": "seam", "bbox": [2, 3, 7, 5], "best_view": 0},
        *(
            [
                {
                    "id": 2,
                    "kind": "review",
                    "bbox": [12, 12, 15, 15],
                    "best_view": 0,
                }
            ]
            if review
            else []
        ),
    ]
    anchors = [
        {"id": 1, "kind": "seam", "points": [[0.1, 0.2, 0.3], [0.11, 0.2, 0.3]]}
    ]
    first = {
        "id": 1,
        "kind": "seam",
        "status": "needs_rework" if positive else "verified",
        "status_reason": "positive_excess"
        if positive
        else "signed_interval_within_limits",
        "positive_excess": positive,
        "peak_mm": 0.7 if positive else 0.02,
        "lower_peak_mm": 0.4 if positive else -0.02,
        "upper_peak_mm": 0.8 if positive else 0.04,
        "observations": [0, 1],
        "anchors": 2,
        "observed_anchors": 2,
        "anchor_bounds_mm": [[0.4, 0.8], [0.1, 0.2]],
    }
    final = {
        **first,
        "status": "verified",
        "status_reason": "signed_interval_within_limits",
        "positive_excess": False,
        "peak_mm": 0.02,
        "observations": [0],
    }
    save_json(
        root / "result.json",
        {
            "task_status": "complete",
            "cad_workpiece": "097",
            "material_removal_simulated": True,
            "reinspections": [
                {"region_reinspection": {"regions": [first]}},
                {"region_reinspection": {"regions": [final]}},
            ],
            "reinspection": {"region_reinspection": {"regions": [final]}},
            "final_stock": {"remaining_peak_mm": 900},
        },
    )
    save_json(root / "status.json", {"stage": "complete"})
    save_json(root / "cycle_receipt.json", {"evidence_scope": "simulation"})
    save_json(root / "registered_regions.json", anchors)
    save_json(root / "detections.json", initial)
    save_json(
        root / "rescan_01_detections.json",
        [{"id": 1, "kind": "review", "bbox": [20, 20, 26, 24], "best_view": 1}]
        if review
        else [],
    )
    save_json(root / "rescan_02_detections.json", [])
    save_json(root / "after_detections.json", [])
    save_json(
        root / "session_state.json",
        {
            "stage": "DONE",
            "workpiece_id": "097",
            "regions": initial,
            "registered_regions": anchors,
            "segments": [{"segment_id": 7, "region_id": 1}],
        },
    )
    for name in [
        "view_00",
        "rescan_01_view_00",
        "rescan_01_view_01",
        "rescan_02_view_00",
    ]:
        depth = np.full((32, 40), 0.8, np.float32)
        np.savez_compressed(
            root / f"{name}_observation.npz", depth=depth, reference_depth=depth
        )
    return root


class NormalScanInjectionTests(unittest.TestCase):
    def test_bundle_loads_exact_scaled_contract_with_finite_eleven_channels(
        self,
    ):
        rgb, depth, nominal, camera = flat_scan()
        result = inject_synthetic_defects_on_scan(
            rgb,
            depth,
            nominal,
            camera,
            seed=17,
            base_scan_source="factory/normal_001",
        )
        with tempfile.TemporaryDirectory() as tmp:
            row = write_injected_bundle(
                tmp, "normal_001_injected", result, workpiece_id="097", seed=17
            )
            item = load_scaled_bundle(tmp, row)
            features = sensor_features(
                item["rgb"], item["depth"], item["reference_depth"], item["K"]
            )
            self.assertEqual(features.shape, (11, *depth.shape))
            self.assertEqual(features.dtype, np.float32)
            self.assertTrue(np.isfinite(features).all())
            self.assertEqual(set(np.unique(item["labels"])), {0, 1, 2})
            folder = Path(tmp) / row["directory"]
            self.assertEqual(
                {path.name for path in folder.iterdir()},
                {
                    "arrays.npz",
                    "rgb.png",
                    "semantic.png",
                    "protection.png",
                    "meta.json",
                },
            )
            meta = json.loads((folder / "meta.json").read_text())
            self.assertEqual(meta["schema"], BUNDLE_SCHEMA)
            self.assertEqual(meta["camera"]["K"], camera.tolist())
            self.assertEqual(meta["base_scan_source"], "factory/normal_001")
            self.assertTrue(meta["synthetic_defect_injected"])
            self.assertFalse(meta["real_defect_scan"])
            with Image.open(folder / "semantic.png") as image:
                np.testing.assert_array_equal(
                    np.asarray(image), result.arrays["semantic"]
                )
            with Image.open(folder / "protection.png") as image:
                np.testing.assert_array_equal(
                    np.asarray(image, dtype=bool), result.arrays["protection"]
                )

    def test_flat_grey_inputs_never_receive_defect_class_colour(self):
        rgb, depth, nominal, camera = flat_scan()
        for kinds in [("flash",), ("burr",), ("flash", "burr")]:
            with self.subTest(kinds=kinds):
                result = inject_synthetic_defects_on_scan(
                    rgb, depth, nominal, camera, seed=42, defect_kinds=kinds
                )
                np.testing.assert_array_equal(
                    result.rgb[..., 0], result.rgb[..., 1]
                )
                np.testing.assert_array_equal(
                    result.rgb[..., 1], result.rgb[..., 2]
                )
                self.assertGreater(np.count_nonzero(result.rgb != rgb), 0)
                self.assertLess(
                    np.count_nonzero(result.rgb[..., 0] != rgb[..., 0]),
                    depth.size * 0.1,
                )

    def test_protrusion_and_ideal_depth_match_residual_and_millimetre_ranges(
        self,
    ):
        rgb, depth, nominal, camera = flat_scan()
        result = inject_synthetic_defects_on_scan(
            rgb, depth, nominal, camera, seed=42
        )
        arrays = result.arrays
        np.testing.assert_allclose(
            (depth - arrays["depth_m"]) * 1000, arrays["residual_mm"], atol=1e-6
        )
        np.testing.assert_allclose(
            (nominal - arrays["depth_ideal_m"]) * 1000,
            arrays["residual_mm"],
            atol=1e-4,
        )
        np.testing.assert_array_equal(arrays["depth_nominal_m"], nominal)
        self.assertEqual(arrays["instance"].dtype, np.uint16)
        for defect in result.metadata["defects"]:
            pixels = np.argwhere(arrays["instance"] == defect["instance"])
            selected = arrays["instance"] == defect["instance"]
            peak = float(arrays["residual_mm"][selected].max())
            low, high = (
                (0.35, 1.8) if defect["kind"] == "flash" else (0.30, 1.4)
            )
            self.assertGreaterEqual(peak, low - 1e-4)
            self.assertLessEqual(peak, high + 1e-4)
            self.assertAlmostEqual(peak, defect["height_mm"], delta=1e-4)
            eigenvalues = np.linalg.eigvalsh(np.cov(pixels.T))
            aspect = np.sqrt(eigenvalues[-1] / max(eigenvalues[0], 0.05))
            if defect["kind"] == "flash":
                self.assertGreater(aspect, 4)
            else:
                self.assertLess(aspect, 2)
        self.assertTrue(arrays["protection"].any())
        self.assertFalse(
            (arrays["protection"] & (arrays["instance"] > 0)).any()
        )
        self.assertTrue(
            np.all(
                arrays["semantic"][arrays["protection"]]
                == SEMANTIC_LABELS["nominal_cad"]
            )
        )

    def test_placement_ignores_rgb_and_source_text_and_preserves_inputs(self):
        rgb, depth, nominal, camera = flat_scan()
        copies = [value.copy() for value in (rgb, depth, nominal, camera)]
        first = inject_synthetic_defects_on_scan(
            rgb, depth, nominal, camera, seed=7, base_scan_source="normal_a"
        )
        second = inject_synthetic_defects_on_scan(
            np.full_like(rgb, [40, 90, 150]),
            depth,
            nominal,
            camera,
            seed=7,
            base_scan_source="normal_b",
        )
        for key in first.arrays:
            np.testing.assert_array_equal(first.arrays[key], second.arrays[key])
        self.assertEqual(first.metadata["defects"], second.metadata["defects"])
        for before, after in zip(
            copies, (rgb, depth, nominal, camera), strict=True
        ):
            np.testing.assert_array_equal(before, after)
        repeated = inject_synthetic_defects_on_scan(
            rgb, depth, nominal, camera, seed=7, base_scan_source="normal_a"
        )
        np.testing.assert_array_equal(first.rgb, repeated.rgb)
        self.assertEqual(first.metadata, repeated.metadata)

    def test_invalid_scan_pixels_are_not_filled_or_labelled_as_defects(self):
        rgb, depth, nominal, camera = flat_scan()
        depth[:10] = np.nan
        depth[:, 0] = np.inf
        depth[30:35, 30:35] = 0
        nominal[50:55, 50:55] = np.nan
        result = inject_synthetic_defects_on_scan(
            rgb, depth, nominal, camera, seed=3
        )
        invalid = ~result.arrays["surface"]
        np.testing.assert_array_equal(
            result.arrays["depth_m"][invalid], depth[invalid]
        )
        self.assertTrue(np.isnan(result.arrays["residual_mm"][invalid]).all())
        self.assertFalse(result.arrays["instance"][invalid].any())
        self.assertFalse(result.arrays["semantic"][invalid].any())
        features = sensor_features(
            result.rgb,
            result.arrays["depth_m"],
            result.arrays["depth_nominal_m"],
            camera,
        )
        self.assertTrue(np.isfinite(features).all())

    def test_matching_normal_control_retains_registration_error_and_scan_group(
        self,
    ):
        rgb, depth, nominal, camera = flat_scan()
        nominal += np.float32(0.00025)
        control = inject_synthetic_defects_on_scan(
            rgb,
            depth,
            nominal,
            camera,
            seed=19,
            count=0,
            base_scan_source="scan_pair",
        )
        injected = inject_synthetic_defects_on_scan(
            rgb, depth, nominal, camera, seed=19, base_scan_source="scan_pair"
        )
        np.testing.assert_array_equal(control.rgb, rgb)
        np.testing.assert_array_equal(control.arrays["depth_m"], depth)
        self.assertFalse(control.metadata["synthetic_defect_injected"])
        self.assertFalse(control.arrays["residual_mm"].any())
        np.testing.assert_allclose(
            (nominal - injected.arrays["depth_m"]) * 1000
            - injected.arrays["residual_mm"],
            (nominal - depth) * 1000,
            atol=1e-4,
        )
        with tempfile.TemporaryDirectory() as tmp:
            normal_row = write_injected_bundle(
                tmp, "normal", control, workpiece_id="097"
            )
            injected_row = write_injected_bundle(
                tmp, "injected", injected, workpiece_id="097"
            )
            self.assertEqual(
                normal_row["base_scan_group"], injected_row["base_scan_group"]
            )
            self.assertEqual(normal_row["split"], injected_row["split"])
            self.assertEqual(normal_row["sample_kind"], "normal_hard_negative")
            self.assertFalse(
                load_scaled_bundle(tmp, normal_row)["labels"].any()
            )
            self.assertTrue(
                (sensor_features(rgb, depth, nominal, camera)[3] > 0).all()
            )

    def test_invalid_boundary_inputs_raise_useful_errors(self):
        rgb, depth, nominal, camera = flat_scan()
        cases = [
            ({"rgb": rgb.astype(np.float32)}, "uint8"),
            ({"reference_depth_m": nominal[:20]}, "matching shapes"),
            ({"intrinsics": np.zeros((3, 3))}, "intrinsics"),
            ({"defect_kinds": ("casting_root",)}, "flash and burr"),
            ({"count": -1}, "count"),
            ({"count": 1.5}, "count"),
            ({"seed": -1}, "seed"),
            ({"base_scan_source": ""}, "origin"),
            (
                {"depth_m": np.full_like(depth, np.nan)},
                "valid registered surface",
            ),
        ]
        for changes, message in cases:
            with self.subTest(changes=tuple(changes)):
                with self.assertRaisesRegex(ValueError, message):
                    inject_synthetic_defects_on_scan(
                        **{
                            "rgb": rgb,
                            "depth_m": depth,
                            "reference_depth_m": nominal,
                            "intrinsics": camera,
                            "seed": 2,
                            **changes,
                        }
                    )

    def test_writer_never_overwrites_an_existing_sample(self):
        result = inject_synthetic_defects_on_scan(*flat_scan(), seed=6)
        with tempfile.TemporaryDirectory() as tmp:
            row = write_injected_bundle(
                tmp, "saved", result, workpiece_id="097"
            )
            path = Path(tmp) / row["directory"] / "meta.json"
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_injected_bundle(tmp, "saved", result, workpiece_id="097")
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "directory name"):
                write_injected_bundle(
                    tmp, "../outside", result, workpiece_id="097"
                )
            with self.assertRaisesRegex(ValueError, "split"):
                write_injected_bundle(
                    tmp, "new", result, workpiece_id="097", split="training"
                )


class ClosedLoopHarvestTests(unittest.TestCase):
    def test_signed_reports_preserve_round_and_later_resolution_not_stock_truth(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = saved_run(root / "run")
            manifest = harvest_closed_loop_hard_examples(run, root / "harvest")
            positive = [
                row
                for row in manifest["examples"]
                if row["example_kind"] == "persistent_positive_residual"
            ]
            self.assertEqual(len(positive), 1)
            example = positive[0]
            self.assertEqual(example["source"]["rescan_index"], 1)
            self.assertEqual(
                example["source"]["json_pointer"],
                "/reinspections/0/region_reinspection/regions/0",
            )
            self.assertEqual(
                example["source"]["observation_files"],
                [
                    "rescan_01_view_00_observation.npz",
                    "rescan_01_view_01_observation.npz",
                ],
            )
            self.assertEqual(example["region"]["peak_mm"], 0.7)
            self.assertEqual(example["region"]["coordinate_frame"], "workpiece")
            self.assertTrue(example["resolved_by_final_scan"])
            self.assertEqual(example["final_status"], "verified")
            self.assertEqual(example["defect_kind"], "flash")
            self.assertFalse(example["training_ready"])
            self.assertNotIn("final_stock", json.dumps(manifest))
            self.assertEqual(manifest["evidence_scope"], "simulation")
            self.assertFalse(manifest["real_defect_scan"])
            saved = json.loads(
                (root / "harvest/hard_examples_manifest.json").read_text()
            )
            self.assertEqual(saved, manifest)

    def test_review_bboxes_use_only_the_corresponding_best_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = harvest_closed_loop_hard_examples(
                saved_run(root / "run"), root / "harvest"
            )
            reviews = [
                row
                for row in manifest["examples"]
                if row["example_kind"] == "review_region"
            ]
            self.assertEqual(len(reviews), 2)
            later = next(
                row
                for row in reviews
                if row["source"]["file"].startswith("rescan_01")
            )
            self.assertEqual(later["region"]["bbox_xyxy"], [20, 20, 26, 24])
            self.assertEqual(
                later["source"]["observation_files"],
                ["rescan_01_view_01_observation.npz"],
            )
            self.assertIsNone(later["defect_kind"])
            self.assertFalse(later["training_ready"])

    def test_operator_masks_and_parameter_edits_map_to_distinct_supervision(
        self,
    ):
        interventions = [
            {"action": "heartbeat", "diff": {}},
            {
                "action": "mask_false_positive",
                "target_id": 1,
                "reason": "confirmed normal casting edge",
                "before": {"approval": "pending"},
                "after": {"approval": "masked"},
            },
            {
                "action": "set_params",
                "target_id": 7,
                "stage": "PLANNED",
                "diff": {"passes": {"before": 1, "after": 2}},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = saved_run(root / "run")
            manifest = harvest_closed_loop_hard_examples(
                run, root / "harvest", interventions=interventions
            )
            negatives = [
                row
                for row in manifest["examples"]
                if row["example_kind"] == "operator_false_positive"
            ]
            self.assertEqual(len(negatives), 1)
            negative = negatives[0]
            self.assertEqual(negative["target_class"], 0)
            self.assertIsNone(
                negative["synthesis_request"]["requested_defect_kind"]
            )
            self.assertEqual(negative["region"]["bbox_xyxy"], [2, 3, 7, 5])
            self.assertEqual(
                negative["source"]["observation_files"],
                ["view_00_observation.npz"],
            )
            self.assertTrue(negative["example_id"].endswith("intervention_1"))
            parameter = next(
                row
                for row in manifest["examples"]
                if row["example_kind"] == "operator_intervention"
            )
            self.assertEqual(parameter["region_id"], 1)
            self.assertEqual(
                parameter["synthesis_request"]["requested_kind"], "thick_stock"
            )
            self.assertEqual(
                parameter["synthesis_request"]["requested_intermediate_state"],
                "rough_residual",
            )
            self.assertTrue(
                all(
                    not row["truth_used_for_control"]
                    for row in manifest["examples"]
                )
            )

    def test_jsonl_auto_loading_and_sampling_are_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = saved_run(root / "run")
            (run / "interventions.jsonl").write_text(
                json.dumps(
                    {
                        "action": "mask_false_positive",
                        "target_id": 1,
                        "diff": {},
                    }
                )
                + "\n\n"
            )
            first = harvest_closed_loop_hard_examples(run, root / "first")
            second = harvest_closed_loop_hard_examples(run, root / "second")
            self.assertEqual(first, second)
            self.assertEqual(
                (root / "first/hard_examples_manifest.json").read_bytes(),
                (root / "second/hard_examples_manifest.json").read_bytes(),
            )
            self.assertAlmostEqual(
                sum(row["sampling_probability"] for row in first["examples"]),
                1.0,
            )
            self.assertEqual(
                len({row["example_id"] for row in first["examples"]}),
                len(first["examples"]),
            )
            for row in first["examples"]:
                self.assertEqual(
                    row["sampling_weight"],
                    HARD_EXAMPLE_WEIGHTS[row["example_kind"]],
                )

    def test_fallback_mines_only_saved_post_grind_depth_not_initial_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "old_run"
            run.mkdir()
            save_json(
                run / "result.json",
                {"task_status": "partial", "material_removal_simulated": True},
            )
            save_json(run / "status.json", {"stage": "complete"})
            nominal = np.full((32, 40), 0.8, np.float32)
            depth = nominal.copy()
            depth[8:15, 10:20] -= 0.002
            surface = np.ones(depth.shape, dtype=bool)
            surface[20:30, 25:35] = False
            depth[20:30, 25:35] -= 0.009
            np.savez_compressed(
                run / "view_00_observation.npz",
                depth=nominal - 0.01,
                reference_depth=nominal,
            )
            np.savez_compressed(
                run / "rescan_01_view_00_observation.npz",
                depth=depth,
                reference_depth=nominal,
                reference_object_mask=surface,
                synthetic_noise_sigma_mm=0.04,
            )
            manifest = harvest_closed_loop_hard_examples(run, root / "harvest")
            self.assertEqual(len(manifest["examples"]), 1)
            example = manifest["examples"][0]
            self.assertEqual(
                example["source"]["file"], "rescan_01_view_00_observation.npz"
            )
            self.assertEqual(example["region"]["bbox_xyxy"], [10, 8, 19, 14])
            self.assertAlmostEqual(
                example["region"]["peak_mm"], 2.0, delta=0.001
            )
            self.assertIsNone(example["defect_kind"])
            self.assertFalse(example["training_ready"])

    def test_empty_harvest_is_finite_and_does_not_invent_failed_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = saved_run(root / "run", positive=False, review=False)
            manifest = harvest_closed_loop_hard_examples(run, root / "harvest")
            self.assertEqual(manifest["examples"], [])
            self.assertEqual(manifest["intervention_requests"], [])
            json.dumps(manifest, allow_nan=False)

    def test_intervention_paths_invalid_inputs_active_runs_and_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = saved_run(root / "run")
            log = root / "operator.json"
            save_json(log, [{"action": "mask_false_positive", "target_id": 1}])
            manifest = harvest_closed_loop_hard_examples(
                run, root / "harvest", interventions=log
            )
            self.assertEqual(len(manifest["intervention_requests"]), 1)
            with self.assertRaises(FileExistsError):
                harvest_closed_loop_hard_examples(run, root / "harvest")
            with self.assertRaisesRegex(ValueError, "log records"):
                harvest_closed_loop_hard_examples(
                    run, root / "bad_rows", interventions=["not a record"]
                )
            with self.assertRaisesRegex(ValueError, "does not exist"):
                harvest_closed_loop_hard_examples(
                    run, root / "bad_path", interventions=root / "missing.jsonl"
                )
            with self.assertRaisesRegex(ValueError, "no saved result"):
                harvest_closed_loop_hard_examples(
                    root / "missing_run", root / "missing_out"
                )
            save_json(run / "status.json", {"stage": "grinding"})
            with self.assertRaisesRegex(ValueError, "finished"):
                harvest_closed_loop_hard_examples(run, root / "active_out")

    def test_real_saved_functional_runs_preserve_empirical_scope_and_status(
        self,
    ):
        base = project.RUNS / "enterprise_offline_20261123/functional_resume"
        names = ["thin_120483_finite_03", "burr_2026112397_low_camera_02"]
        if not all((base / name / "result.json").is_file() for name in names):
            self.skipTest(
                "historical offline runs are not installed in this checkout"
            )
        with tempfile.TemporaryDirectory() as tmp:
            thin = harvest_closed_loop_hard_examples(
                base / names[0], Path(tmp) / "thin"
            )
            positive = [
                row
                for row in thin["examples"]
                if row["example_kind"] == "persistent_positive_residual"
            ]
            self.assertEqual(len(positive), 2)
            self.assertTrue(
                all(row["resolved_by_final_scan"] for row in positive)
            )
            self.assertEqual(thin["run_status"], "complete")
            self.assertEqual(thin["evidence_scope"], "simulation")
            burr = harvest_closed_loop_hard_examples(
                base / names[1], Path(tmp) / "burr"
            )
            reviews = [
                row
                for row in burr["examples"]
                if row["example_kind"] == "review_region"
            ]
            self.assertGreaterEqual(len(reviews), 1)
            self.assertEqual(burr["run_status"], "partial")
            self.assertTrue(all(not row["training_ready"] for row in reviews))


if __name__ == "__main__":
    unittest.main()
