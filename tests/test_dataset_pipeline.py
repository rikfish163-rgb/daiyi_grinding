"""Regression checks for multi-master synthesis and sensor degradation."""

import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage

from daiyi_grinding import dataset_pipeline as pipeline
from daiyi_grinding import workpiece_packages

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_021 = PROJECT_ROOT / "config/workpieces/wp_021.json"
PACKAGE_097 = PROJECT_ROOT / "config/workpieces/wp_097.json"


def _plane_mesh(size_m=1.0, z_m=0.0):
    vertices = np.array(
        [
            [-size_m, -size_m, z_m],
            [size_m, -size_m, z_m],
            [size_m, size_m, z_m],
            [-size_m, size_m, z_m],
        ]
    )
    return trimesh.Trimesh(vertices, [[0, 1, 2], [0, 2, 3]], process=False)


def _tilted_plane(angle_deg, z_m=0.0):
    mesh = _plane_mesh(0.5, z_m)
    rotation = trimesh.transformations.rotation_matrix(
        math.radians(angle_deg), [0, 1, 0]
    )
    mesh.apply_transform(rotation)
    return mesh


def _camera(distance_m=0.5):
    return pipeline.look_at(
        (0.0, 0.0, distance_m), (0.0, 0.0, 0.0), 160, 120, 46.0
    )


def _clean_profile(**overrides):
    values = {
        "base_depth_sigma_mm": 0.05,
        "range_sigma_mm_per_m": 0.0,
        "incidence_sigma_gain_mm": 0.0,
        "incidence_power": 1.6,
        "base_dropout": 0.0,
        "incidence_dropout_gain": 0.0,
        "edge_gradient_mm": 1000.0,
        "edge_band_px": 1,
        "edge_flying_fraction": 0.0,
        "specular_patches_per_m2": 0.0,
        "oxide_patches_per_m2": 0.0,
        "registration_translation_mm": 0.0,
        "registration_rotation_mrad": 0.0,
        "normal_bias_mm": 0.0,
        "normal_bias_pattern_mm": 0.0,
    }
    values.update(overrides)
    return pipeline.SensorDegradationProfile(**values)


class FeatureExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = pipeline.FeatureConfig(heightmap_spacing_m=0.002)
        cls.meshes = {
            workpiece_id: pipeline.load_mesh(path)
            for workpiece_id, path in pipeline.WORKPIECE_MESHES.items()
        }
        cls.features = {
            workpiece_id: pipeline.extract_features(
                cls.meshes[workpiece_id],
                workpiece_id,
                path,
                cls.config,
            )
            for workpiece_id, path in pipeline.WORKPIECE_MESHES.items()
        }

    def test_both_cads_produce_all_feature_families(self):
        for workpiece_id, features in self.features.items():
            with self.subTest(workpiece=workpiece_id):
                summary = features.summary()["features"]
                self.assertGreater(summary["parting_candidate"]["count"], 0)
                self.assertGreater(summary["sharp_edge"]["count"], 0)
                self.assertGreater(summary["hole_rim"]["count"], 0)
                self.assertGreater(summary["gate_root_zone"]["count"], 0)
                self.assertGreater(summary["flat_surface"]["count"], 0)

    def test_feature_points_are_finite_and_attached(self):
        for workpiece_id, features in self.features.items():
            mesh = self.meshes[workpiece_id]
            for line in features.lines:
                with self.subTest(workpiece=workpiece_id, kind=line.kind):
                    self.assertTrue(np.isfinite(line.points_m).all())
                    self.assertTrue(np.isfinite(line.normals_m).all())
                    self.assertGreaterEqual(line.length_m, 0.0)
            for line in features.by_kind("parting_candidate"):
                selected = line.points_m[
                    np.linspace(
                        0,
                        len(line.points_m) - 1,
                        min(12, len(line.points_m)),
                        dtype=int,
                    )
                ]
                _, distance, _ = trimesh.proximity.closest_point_naive(
                    mesh, selected
                )
                self.assertLess(float(distance.max()), 0.002)

    def test_parting_candidates_follow_the_draft_outline(self):
        for workpiece_id, features in self.features.items():
            with self.subTest(workpiece=workpiece_id):
                candidates = features.by_kind("parting_candidate")
                self.assertTrue(
                    any(
                        line.metadata.get("mean_top_access", 0.0) >= 0.9
                        for line in candidates
                    )
                )
                insets = [
                    line
                    for line in candidates
                    if line.source == "draft_silhouette_inset"
                ]
                self.assertTrue(insets)
                for line in insets:
                    self.assertGreaterEqual(
                        line.metadata["mean_top_access"], 0.9
                    )
                    self.assertAlmostEqual(
                        line.metadata["inset_mm"], 8.0, places=6
                    )

    def test_split_is_stable_and_never_leaks_a_workpiece(self):
        identifiers = ["097", "021", "100", "101", "102", "103"]
        first = pipeline.unseen_cad_split(identifiers, seed=5)
        second = pipeline.unseen_cad_split(identifiers, seed=5)
        self.assertEqual(first.record(), second.record())
        pipeline.validate_unseen_cad_split(first)
        covered = set(first.train) | set(first.val) | set(first.test)
        self.assertEqual(covered, set(identifiers))
        self.assertFalse(set(first.train) & set(first.test))
        self.assertFalse(set(first.train) & set(first.val))
        self.assertFalse(set(first.val) & set(first.test))
        with self.assertRaisesRegex(ValueError, "two splits"):
            pipeline.validate_unseen_cad_split(
                pipeline.SplitAssignment(
                    train=("097",),
                    val=("097",),
                    test=("021",),
                    seed=0,
                    fractions=(0.5, 0.25, 0.25),
                )
            )
        with self.assertRaisesRegex(ValueError, "two workpieces"):
            pipeline.unseen_cad_split(["097"])

    def test_interventions_map_to_hard_example_requests(self):
        entries = [
            {
                "timestamp": "2026-11-24T00:00:00Z",
                "stage": "PLANNED",
                "action": "set_params",
                "target_id": 4,
                "diff": {
                    "feed_scale": {"before": 1.0, "after": 0.6},
                    "passes": {"before": 1, "after": 2},
                },
            },
            {
                "timestamp": "2026-11-24T00:01:00Z",
                "stage": "EXECUTING",
                "action": "set_params",
                "target_id": 5,
                "diff": {"normal_offset_mm": {"before": 0.08, "after": 0.3}},
            },
            {"diff": {"unrelated": {"before": 0, "after": 1}}},
        ]
        result = pipeline.interventions_to_hard_examples(entries)
        self.assertEqual(
            result["schema"], "daiyi-intervention-hard-examples-v1"
        )
        self.assertEqual(len(result["requests"]), 2)
        first, second = result["requests"]
        self.assertEqual(first["requested_kind"], "thick_stock")
        self.assertEqual(
            first["requested_intermediate_state"], "rough_residual"
        )
        self.assertEqual(first["severity"], "high")
        self.assertEqual(second["requested_kind"], "allowance_offset")
        self.assertEqual(second["requested_intermediate_state"], "thin_stock")
        for request in result["requests"]:
            self.assertFalse(request["truth_used_for_control"])
        with self.assertRaises(ValueError):
            pipeline.interventions_to_hard_examples("not-a-log")

    def test_single_workpiece_is_development_only(self):
        assignment = pipeline.assign_splits(["097"])
        self.assertEqual(assignment.train, ("097",))
        self.assertFalse(assignment.test)
        with self.assertRaises(ValueError):
            pipeline.assert_samples_respect_split(
                [
                    {"workpiece_id": "097", "split": "train"},
                    {"workpiece_id": "097", "split": "test"},
                ]
            )


class DefectSynthesisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = pipeline.FeatureConfig(heightmap_spacing_m=0.002)
        cls.records = {}
        for workpiece_id, path in pipeline.WORKPIECE_MESHES.items():
            mesh = pipeline.load_mesh(path)
            features = pipeline.extract_features(
                mesh, workpiece_id, path, cls.config
            )
            defects = pipeline.generate_defects(mesh, features, 20261124)
            cls.records[workpiece_id] = {
                "mesh": mesh,
                "features": features,
                "defects": defects,
                "scene": pipeline.assemble_scene(mesh, defects),
            }

    def test_three_defect_kinds_are_closed_rooted_and_on_cad(self):
        for workpiece_id, record in self.records.items():
            kinds = {defect.kind for defect in record["defects"]}
            self.assertEqual(kinds, {"flash", "burr", "casting_root"})
            for defect in record["defects"]:
                with self.subTest(workpiece=workpiece_id, kind=defect.kind):
                    self.assertTrue(defect.mesh.is_watertight)
                    self.assertTrue(defect.mesh.is_winding_consistent)
                    self.assertGreater(defect.mesh.volume, 0.0)
                    self.assertGreater(defect.peak_height_mm, 0.3)
                    validation = defect.geometry_validation
                    self.assertLess(
                        validation["root_max_cad_distance_mm"], 0.05
                    )
                    self.assertLess(validation["exact_support_max_mm"], 0.1)
                    self.assertEqual(validation["exact_support_samples"], 24)
            scene = record["scene"]
            for identity in range(1, 4):
                selected = scene.face_instance == identity
                self.assertGreater(int(selected.sum()), 0)
                self.assertTrue(
                    np.isfinite(
                        scene.vertex_residual_mm[scene.mesh.faces[selected]]
                    ).all()
                )

    def test_charts_are_valid_and_intermediate_states_are_bounded(self):
        for workpiece_id, record in self.records.items():
            for identity, defect in enumerate(record["defects"], 1):
                chart = defect.chart
                with self.subTest(workpiece=workpiece_id, kind=defect.kind):
                    self.assertGreater(float(chart.valid.mean()), 0.8)
                    rng = np.random.default_rng(identity)
                    rough = pipeline.intermediate_height(
                        chart, "rough_residual", rng
                    )
                    thin = pipeline.intermediate_height(
                        chart, "thin_stock", rng
                    )
                    over = pipeline.intermediate_height(
                        chart, "micro_overgrind", rng
                    )
                    self.assertGreaterEqual(float(rough.min()), 0.0)
                    self.assertLessEqual(
                        float(rough.max()),
                        defect.peak_height_mm + 1e-6,
                    )
                    self.assertGreaterEqual(float(thin.min()), 0.0)
                    self.assertLessEqual(float(thin.max()), 0.25)
                    self.assertLess(float(over.min()), -0.02)
                    self.assertGreater(float(over.min()), -0.35)
                    self.assertLess(
                        float(over.max()), defect.peak_height_mm + 1e-6
                    )
                    surface = chart.surface_mesh(over)
                    self.assertGreater(len(surface.faces), 0)
                    flattened = over.reshape(-1)
                    face_heights = flattened[surface.faces].mean(axis=1)
                    self.assertTrue(np.isfinite(face_heights).all())


class SensorDegradationTests(unittest.TestCase):
    def setUp(self):
        self.camera = _camera()

    def test_noise_is_reported_and_applied_in_millimetres(self):
        mesh = _plane_mesh()
        ideal = pipeline.rasterize_mesh(mesh, self.camera)["depth_m"]
        rng = np.random.default_rng(3)
        profile = _clean_profile()
        degraded = pipeline.degrade_depth(
            ideal, self.camera, profile.draw(rng), rng
        )
        residual = (degraded["depth_m"] - degraded["depth_ideal_m"]) * 1000
        measured = float(np.nanstd(residual))
        self.assertLess(abs(measured - 0.05), 0.006)
        self.assertLess(float(np.nanmax(np.abs(residual))), 0.3)
        reported = float(np.nanmean(degraded["sigma_mm"]))
        self.assertLess(abs(reported - 0.05), 0.002)

    def test_incidence_raises_sigma_and_dropout(self):
        profile = _clean_profile(
            incidence_sigma_gain_mm=0.8,
            incidence_dropout_gain=0.5,
            base_dropout=0.002,
        )
        results = {}
        for angle, name in ((0.0, "flat"), (60.0, "oblique")):
            camera = _camera(0.8)
            mesh = _tilted_plane(angle)
            ideal = pipeline.rasterize_mesh(mesh, camera)["depth_m"]
            rng = np.random.default_rng(11)
            results[name] = pipeline.degrade_depth(
                ideal, camera, profile.draw(rng), rng
            )
        flat = np.nanmean(results["flat"]["sigma_mm"])
        oblique = np.nanmean(results["oblique"]["sigma_mm"])
        self.assertGreater(oblique, flat * 1.05)
        self.assertGreater(
            float(np.nanmean(results["oblique"]["incidence_deg"])),
            float(np.nanmean(results["flat"]["incidence_deg"])),
        )

    def test_flying_pixels_lie_between_adjacent_surfaces(self):
        lower = _plane_mesh(0.5, 0.0)
        upper = trimesh.Trimesh(
            np.array(
                [
                    [0.001, -0.5, 0.01],
                    [0.5, -0.5, 0.01],
                    [0.5, 0.5, 0.01],
                    [0.001, 0.5, 0.01],
                ]
            ),
            [[0, 1, 2], [0, 2, 3]],
            process=False,
        )
        scene = trimesh.util.concatenate([lower, upper])
        camera = _camera(0.5)
        ideal = pipeline.rasterize_mesh(scene, camera)["depth_m"]
        rng = np.random.default_rng(17)
        profile = _clean_profile(
            edge_gradient_mm=0.5,
            edge_band_px=2,
            edge_flying_fraction=1.0,
        )
        degraded = pipeline.degrade_depth(ideal, camera, profile.draw(rng), rng)
        flying = degraded["flying"]
        self.assertGreater(int(flying.sum()), 0)
        observed = degraded["depth_m"][flying]
        self.assertTrue(np.isfinite(observed).all())
        self.assertGreater(float(observed.min()), 0.487)
        self.assertLess(float(observed.max()), 0.503)

    def test_specular_patch_causes_connected_dropout(self):
        mesh = _plane_mesh()
        camera = _camera(0.25)
        ideal = pipeline.rasterize_mesh(mesh, camera)["depth_m"]
        rng = np.random.default_rng(19)
        profile = _clean_profile(
            specular_patches_per_m2=4000.0,
            specular_radius_mm=(4.0, 6.0),
            specular_dropout=1.0,
        )
        degraded = pipeline.degrade_depth(ideal, camera, profile.draw(rng), rng)
        labels, count = ndimage.label(degraded["specular"])
        self.assertGreater(count, 0)
        sizes = np.bincount(labels.ravel())[1:]
        self.assertGreaterEqual(int(sizes.max()), 5)
        self.assertTrue(
            np.all(~np.isfinite(degraded["depth_m"][degraded["specular"]]))
        )

    def test_calibration_recovers_noise_order_and_bias(self):
        rng = np.random.default_rng(23)
        count = 12000
        angles = rng.uniform(0.0, 70.0, count)
        blocks = np.clip((angles // 12.0).astype(int), 0, 5)
        offsets = np.array([0.30, 0.48, 0.36, 0.58, 0.40, 0.55])
        ranges = offsets[blocks] + rng.uniform(-0.02, 0.02, count)
        secant = 1.0 / np.cos(np.radians(angles)) - 1.0
        sigma = 0.06 + 0.25 * ranges + 0.5 * secant
        bias = 0.04
        residuals = rng.normal(bias, sigma)
        missing = rng.random(count) < np.clip(0.01 + 0.3 * secant, 0, 0.9)
        profile, report = (
            pipeline.SensorDegradationProfile.calibrate_from_residuals(
                residuals, angles, ranges, missing=missing
            )
        )
        self.assertGreater(profile.incidence_sigma_gain_mm, 0.2)
        self.assertGreater(profile.range_sigma_mm_per_m, 0.1)
        self.assertLess(abs(profile.base_depth_sigma_mm - 0.06), 0.03)
        self.assertLess(abs(report["signed_median_bias_mm"] - bias), 0.01)
        self.assertGreater(profile.incidence_dropout_gain, 0.0)
        self.assertFalse(profile.calibrated_from_real_scans)


class EndToEndBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="daiyi-p1-data-")
        cls.output = Path(cls.temporary.name) / "dataset"
        cls.packages = [
            pipeline.load_workpiece_package(str(path))
            for path in (PACKAGE_097, PACKAGE_021)
        ]
        cls.manifest = pipeline.run(
            cls.output,
            packages=[str(PACKAGE_097), str(PACKAGE_021)],
            seeds=(31,),
            image_size=(64, 48),
            cloud_spacing_m=0.001,
            config=pipeline.FeatureConfig(heightmap_spacing_m=0.002),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_p0b_package_object_and_json_agree(self):
        loaded_object = pipeline.load_workpiece_package(
            workpiece_packages.load_workpiece_package("021")
        )
        loaded_json = pipeline.load_workpiece_package(str(PACKAGE_021))
        self.assertEqual(loaded_object.workpiece_id, "021")
        self.assertEqual(loaded_json.workpiece_id, "021")
        self.assertEqual(loaded_object.units, "m")
        self.assertEqual(loaded_json.units, "m")
        self.assertEqual(
            loaded_object.mesh_path.resolve(), loaded_json.mesh_path.resolve()
        )
        self.assertEqual(
            loaded_object.protection_regions, loaded_json.protection_regions
        )
        self.assertEqual(
            loaded_object.protection_regions[0]["name"], "loading_datum"
        )
        self.assertEqual(
            len(loaded_object.protection_regions[0]["bounds_m"]), 2
        )
        self.assertTrue(loaded_object.mesh_path.name.startswith("wp_021"))
        self.assertEqual(
            pipeline.load_workpiece_package(str(PACKAGE_097)).workpiece_id,
            "097",
        )

    def test_manifest_and_bundles_are_self_describing(self):
        manifest = self.manifest
        self.assertEqual(manifest["schema"], pipeline.SCHEMA)
        self.assertEqual(len(manifest["samples"]), 30)
        self.assertEqual(
            {item["workpiece_id"] for item in manifest["samples"]},
            {"097", "021"},
        )
        self.assertTrue((self.output / "manifest.json").is_file())
        self.assertTrue((self.output / "report.md").is_file())
        local = [
            sample
            for sample in manifest["samples"]
            if sample["sample_kind"] == "local_fov"
        ]
        intermediate = [
            sample
            for sample in manifest["samples"]
            if sample["sample_kind"] == "intermediate_state"
        ]
        self.assertTrue(local)
        self.assertTrue(intermediate)
        for sample in (local[0], intermediate[0]):
            folder = self.output / sample["directory"]
            arrays = np.load(folder / "arrays.npz")
            points = np.load(folder / "points.npz")
            roots = np.load(folder / "roots.npz")
            metadata = json.loads((folder / "meta.json").read_text())
            self.assertEqual(arrays["semantic"].shape, arrays["depth_m"].shape)
            self.assertIn("depth_ideal_m", arrays)
            self.assertIn("depth_nominal_m", arrays)
            self.assertEqual(arrays["instance"].dtype, np.uint16)
            self.assertTrue((folder / "rgb.png").is_file())
            self.assertEqual(points["points_m"].shape[1], 3)
            self.assertEqual(points["normals"].shape[1], 3)
            self.assertLessEqual(float(points["spacing_mm"]), 1.0)
            self.assertGreater(int(points["count"]), 0)
            self.assertGreater(len(roots["root_points_m"]), 0)
            self.assertEqual(
                len(roots["root_tangent"]), len(roots["root_points_m"])
            )
            self.assertFalse(metadata["provenance"]["mujoco_used"])
            self.assertFalse(metadata["provenance"]["truth_used_for_control"])
            self.assertTrue(
                metadata["provenance"]["labels_are_supervision_only"]
            )
            self.assertEqual(metadata["schema"], pipeline.BUNDLE_SCHEMA)
        semantic_colors = np.asarray(
            Image.open(self.output / local[0]["directory"] / "rgb.png")
        )
        self.assertGreater(semantic_colors.shape[0], 0)

    def test_intermediate_state_samples_have_physical_residuals(self):
        manifest = self.manifest
        states = {}
        for sample in manifest["samples"]:
            if sample["sample_kind"] != "intermediate_state":
                continue
            arrays = np.load(self.output / sample["directory"] / "arrays.npz")
            states.setdefault(sample["intermediate_state"], []).append(
                arrays["residual_mm"]
            )
        self.assertEqual(
            set(states), {"rough_residual", "thin_stock", "micro_overgrind"}
        )
        for residual in states["rough_residual"]:
            valid = np.isfinite(residual)
            self.assertGreaterEqual(float(np.min(residual[valid])), 0.0)
        for residual in states["thin_stock"]:
            valid = np.isfinite(residual)
            self.assertLessEqual(float(np.max(residual[valid])), 0.25)
        self.assertTrue(
            any(
                float(np.nanmin(item)) < -0.02
                for item in states["micro_overgrind"]
            )
        )
        for sample in manifest["samples"]:
            self.assertIn(sample["split"], {"train", "val", "test"})
        pipeline.assert_samples_respect_split(manifest["samples"])
        split = manifest["split"]
        self.assertFalse(set(split["train"]) & set(split["test"]))
        self.assertFalse(set(split["train"]) & set(split["val"]))
        self.assertFalse(set(split["val"]) & set(split["test"]))
        self.assertEqual(len(split["train"]) + len(split["test"]), 2)
        self.assertEqual(split["val"], [])
        self.assertTrue(split["unseen_cad_evaluation_possible"])
        for workpiece_id in ("097", "021"):
            selected = [
                item
                for item in manifest["samples"]
                if item["workpiece_id"] == workpiece_id
            ]
            self.assertEqual(len(selected), 15)
            self.assertEqual(
                {item["defect_kind"] for item in selected},
                {"flash", "burr", "casting_root"},
            )
            self.assertEqual(
                {item["sample_kind"] for item in selected},
                {"overview", "local_fov", "intermediate_state"},
            )
            self.assertEqual(
                {
                    item["intermediate_state"]
                    for item in selected
                    if item["sample_kind"] == "intermediate_state"
                },
                {"rough_residual", "thin_stock", "micro_overgrind"},
            )
            self.assertEqual(len({item["split"] for item in selected}), 1)

    def test_depth_ideal_is_current_state_and_nominal_is_cad(self):
        micro = next(
            item
            for item in self.manifest["samples"]
            if item["intermediate_state"] == "micro_overgrind"
            and item["workpiece_id"] == "097"
        )
        arrays = np.load(self.output / micro["directory"] / "arrays.npz")
        ideal = arrays["depth_ideal_m"]
        nominal = arrays["depth_nominal_m"]
        valid = np.isfinite(ideal) & np.isfinite(nominal)
        self.assertGreater(int(valid.sum()), 0)
        difference_mm = (ideal[valid] - nominal[valid]) * 1000
        self.assertLess(float(np.min(difference_mm)), -0.02)
        local = next(
            item
            for item in self.manifest["samples"]
            if item["sample_kind"] == "local_fov"
            and item["workpiece_id"] == "021"
        )
        arrays = np.load(self.output / local["directory"] / "arrays.npz")
        ideal = arrays["depth_ideal_m"]
        nominal = arrays["depth_nominal_m"]
        valid = np.isfinite(ideal) & np.isfinite(nominal)
        self.assertGreater(int(valid.sum()), 0)
        self.assertGreater(
            float(np.max(np.abs(ideal[valid] - nominal[valid]))) * 1000,
            0.3,
        )

    def test_synthetic_rgb_is_independent_of_semantic_labels(self):
        rows, cols = 12, 16
        normals = np.zeros((rows, cols, 3))
        normals[..., 2] = 1.0
        normals[0:4, :, 0] = 0.5
        observation = {
            "normals": normals,
            "surface": np.ones((rows, cols), bool),
            "specular": np.zeros((rows, cols), bool),
            "oxide": np.zeros((rows, cols), bool),
            "dropout": np.zeros((rows, cols), bool),
        }
        observation["specular"][2:4, 3:6] = True
        observation["oxide"][6:8, 9:12] = True
        first = pipeline._synthetic_rgb(
            observation, np.zeros((rows, cols), np.uint8)
        )
        second = pipeline._synthetic_rgb(
            observation, np.full((rows, cols), 6, np.uint8)
        )
        self.assertEqual(first.dtype, np.uint8)
        self.assertTrue(np.array_equal(first, second))
        self.assertFalse(
            np.array_equal(
                first,
                np.zeros_like(first),
            )
        )

    def test_point_counts_and_package_protection_are_recorded(self):
        manifest = self.manifest
        for sample in manifest["samples"]:
            folder = self.output / sample["directory"]
            metadata = json.loads((folder / "meta.json").read_text())
            points = np.load(folder / "points.npz")
            with self.subTest(sample=sample["sample_id"]):
                self.assertEqual(
                    int(metadata["point_count"]),
                    len(points["points_m"]),
                )
                self.assertEqual(
                    int(metadata["point_count"]),
                    int(points["count"]),
                )
                self.assertEqual(
                    int(metadata["point_count"]),
                    int(sample["point_count"]),
                )
                provenance = metadata["provenance"]
                self.assertTrue(provenance["protection_regions"])
                self.assertIn(
                    "loading_datum",
                    [zone["name"] for zone in provenance["protection_regions"]],
                )
                self.assertIn("protection_regions_scope", provenance)
                self.assertTrue(
                    provenance["package_metadata"].get("package_path")
                )

    def test_default_package_sources_load_p0b_json(self):
        sources = pipeline.default_package_sources(["097", "021"])
        self.assertEqual(len(sources), 2)
        for source, expected in zip(sources, ("wp_097.json", "wp_021.json")):
            self.assertIsInstance(source, str)
            self.assertTrue(source.endswith(expected))
            package = pipeline.load_workpiece_package(source)
            self.assertTrue(package.protection_regions)
            self.assertEqual(
                package.protection_regions[0]["name"], "loading_datum"
            )

    def test_instance_ids_and_intermediate_protection_are_consistent(self):
        manifest = self.manifest
        for sample in manifest["samples"]:
            folder = self.output / sample["directory"]
            arrays = np.load(folder / "arrays.npz")
            points = np.load(folder / "points.npz")
            roots = np.load(folder / "roots.npz")
            root_ids = set(
                int(value) for value in np.unique(roots["root_instance"])
            )
            array_ids = set(
                int(value) for value in np.unique(arrays["instance"]) if value
            )
            cloud_ids = set(
                int(value) for value in np.unique(points["instance"]) if value
            )
            with self.subTest(sample=sample["sample_id"]):
                self.assertIn(sample["instance"], root_ids)
                self.assertIn(sample["instance"], array_ids)
                self.assertIn(sample["instance"], cloud_ids)
                self.assertEqual(array_ids, root_ids)
                self.assertTrue(cloud_ids <= root_ids)
                if sample["sample_kind"] == "intermediate_state":
                    self.assertEqual(root_ids, {sample["instance"]})
                    self.assertEqual(array_ids, {sample["instance"]})
                    self.assertEqual(cloud_ids, {sample["instance"]})
                    self.assertGreater(int(arrays["protection"].sum()), 0)
                    self.assertGreater(int(points["protection"].sum()), 0)

    def test_synthetic_rgb_is_uint8_and_uses_semantics(self):
        sample = next(
            item
            for item in self.manifest["samples"]
            if item["sample_kind"] == "local_fov"
        )
        folder = self.output / sample["directory"]
        image = np.asarray(Image.open(folder / "rgb.png"))
        self.assertEqual(image.dtype, np.uint8)
        self.assertEqual(image.ndim, 3)
        self.assertEqual(image.shape[2], 3)


if __name__ == "__main__":
    unittest.main()
