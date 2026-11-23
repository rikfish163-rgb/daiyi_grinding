"""Data units, unknown-part rejection, source splits and observed-cloud reuse."""

import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh

from daiyi_grinding import serve
from daiyi_grinding.cad_defects import DEFAULT_MESH
from daiyi_grinding.casting_defects import generate_casting_defects
from daiyi_grinding.casting_profiles import (
    BASE_PROFILE,
    TEAM_PROFILE,
    available_profiles,
    load_profile,
    sample_compact_prior,
    sensor_for_profile,
)
from daiyi_grinding.casting_scan_calibration import (
    _lzf_decode,
    calibrate_scan,
    load_cloud,
    read_pcd,
    register_normal_regions,
)
from daiyi_grinding.team_data_reuse import sha256
from daiyi_grinding.workstation_reuse import backproject_observation


class PointCloudContractTests(unittest.TestCase):
    def test_ascii_binary_and_compressed_keep_units_nan_and_original_rows(self):
        values = np.array([[10, 20, 30], [40, np.nan, 60]], "<f4")
        header = (
            "VERSION .7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\n"
            "COUNT 1 1 1\nWIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA "
        )
        field_major = values.T.copy().tobytes()
        compressed = bytes([len(field_major) - 1]) + field_major
        contents = {
            "ascii": b"10 20 30\n40 nan 60\n",
            "binary": values.tobytes(),
            "binary_compressed": struct.pack(
                "<II", len(compressed), len(field_major)
            )
            + compressed,
        }
        with tempfile.TemporaryDirectory() as directory:
            for mode, content in contents.items():
                path = Path(directory) / f"{mode}.pcd"
                path.write_bytes((header + mode + "\n").encode() + content)
                np.testing.assert_allclose(
                    read_pcd(path), values, equal_nan=True
                )
                np.testing.assert_allclose(
                    load_cloud(path, "mm"),
                    values / 1000,
                    atol=1e-8,
                    equal_nan=True,
                )
                with self.assertRaisesRegex(ValueError, "explicitly"):
                    load_cloud(path, "auto")
        self.assertEqual(
            _lzf_decode(bytes([2, 97, 98, 99, 64, 2]), 7), b"abcabca"
        )

    def test_unknown_scan_cannot_be_called_real_097_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "scan.json"
            manifest.write_text(
                json.dumps(
                    {
                        "part_id": "unidentified_sample_20261116_01",
                        "part_matches_cad_confirmed": False,
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "unknown sample"):
                calibrate_scan(manifest, Path(directory) / "output")

    def test_planar_normal_anchors_cannot_verify_six_dof_registration(self):
        mesh = trimesh.creation.box(extents=[0.1, 0.1, 0.1])
        x, y = np.meshgrid(
            np.linspace(-0.035, 0.035, 8), np.linspace(-0.035, 0.035, 8)
        )
        points = np.column_stack([x.ravel(), y.ravel(), np.full(x.size, 0.05)])
        with self.assertRaisesRegex(ValueError, "six rigid"):
            register_normal_regions(mesh, points, np.eye(4))

    def test_metric_scan_control_measures_added_stock_without_marking_it_real(
        self,
    ):
        mesh = trimesh.load_mesh(DEFAULT_MESH, process=True)
        anchors = mesh.triangles_center[
            np.linspace(0, len(mesh.faces) - 1, 120, dtype=int)
        ]
        _, item = generate_casting_defects(
            DEFAULT_MESH, 9301, "casting_flash", BASE_PROFILE
        )[0]
        vertices = np.asarray(item["original_vertices"])[::20]
        roots = np.asarray(item["root_curves_m"][0])
        cloud = np.vstack([anchors, vertices])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cloud_file, root_file = folder / "scan.npy", folder / "roots.npy"
            np.save(cloud_file, cloud * 1000)
            np.save(root_file, roots * 1000)
            transform = np.eye(4)
            transform[:3, 3] = [0.0001, -0.00008, 0.00005]
            manifest = folder / "scan.json"
            manifest.write_text(
                json.dumps(
                    {
                        "part_id": "097",
                        "part_matches_cad_confirmed": True,
                        "identity_evidence": "synthetic nominal CAD control",
                        "root_permission_confirmed": True,
                        "cad_sha256": sha256(DEFAULT_MESH),
                        "source_kind": "synthetic_control",
                        "cloud_file": cloud_file.name,
                        "cloud_sha256": sha256(cloud_file),
                        "units": "mm",
                        "normal_region_indices": list(range(len(anchors))),
                        "processing_region_indices": list(
                            range(len(anchors), len(cloud))
                        ),
                        "T_workpiece_scan_m": transform.tolist(),
                        "root_points_file": root_file.name,
                    }
                )
            )
            result = calibrate_scan(manifest, folder / "output")
            self.assertFalse(result["real_scan_fitted"])
            self.assertEqual(result["registration"]["rank"], 6)
            self.assertGreater(result["measured_positive_points"], 6)
            self.assertGreater(
                result["visible_normal_height_mm"]["median"], 0.3
            )
            self.assertFalse(result["full_thickness_measured"])
            self.assertFalse(result["simulation_profile_overwritten"])


class TeamPriorTests(unittest.TestCase):
    def test_joint_dimensions_and_train_only_sampling_are_preserved(self):
        profile = load_profile(BASE_PROFILE)
        row = {
            "sample_id": "train_example",
            "optimizer_split": "train",
            "height_mm": 1.2,
            "radii_uv_mm": [2.3, 0.9],
        }
        profile["compact_excess_prior"] = {"training_instances": [row]}
        sampled = sample_compact_prior(profile, np.random.default_rng(3))
        self.assertEqual(sampled, row)
        profile["compact_excess_prior"]["training_instances"][0][
            "optimizer_split"
        ] = "test"
        with self.assertRaisesRegex(ValueError, "training"):
            sample_compact_prior(profile, np.random.default_rng(3))

    def test_sensor_component_is_shared_by_entity_and_legacy_has_no_prior(self):
        profile = load_profile(BASE_PROFILE)
        self.assertIsNone(sensor_for_profile(profile, 9301))
        profile["sensor_prior"] = {
            "seed_offset": 2,
            "components": [
                {
                    "weight": 0.5,
                    "depth_noise_sigma_mm": 0.08,
                    "dropout_probability": 0.003,
                },
                {
                    "weight": 0.5,
                    "depth_noise_sigma_mm": 0.12,
                    "dropout_probability": 0.003,
                },
            ],
        }
        self.assertEqual(
            sensor_for_profile(profile, 9301), sensor_for_profile(profile, 9301)
        )

    def test_verified_team_profile_remains_a_train_prior_with_unverified_mold_lines(
        self,
    ):
        if TEAM_PROFILE not in available_profiles():
            self.skipTest("The verified public dataset has not been fitted yet")
        profile = load_profile(TEAM_PROFILE)
        self.assertFalse(profile["real_scan_fitted"])
        self.assertFalse(
            profile["dataset_binding"]["validation_or_test_used_to_fit"]
        )
        self.assertTrue(
            all(
                row["optimizer_split"] == "train"
                and row["semantic_code"] == 602
                for row in profile["compact_excess_prior"]["training_instances"]
            )
        )
        self.assertFalse(
            profile["parameter_sources"][
                "root_placement_length_gaps_appearance"
            ]["true_mold_line_known"]
        )
        for mesh, item in generate_casting_defects(
            DEFAULT_MESH, 9301, "casting_burr", TEAM_PROFILE
        ):
            source = item["parameters"]["compact_prior_instance"]
            self.assertTrue(mesh.is_watertight)
            self.assertAlmostEqual(
                item["height_peak_mm"], source["height_mm"], places=5
            )
            self.assertAlmostEqual(
                item["parameters"]["footprint_diameter_mm"],
                2 * max(source["radii_uv_mm"]),
                places=5,
            )
        self.assertEqual(
            serve.validate(
                {"case": "casting_burr", "casting_profile": TEAM_PROFILE}
            )["casting_profile"],
            TEAM_PROFILE,
        )
        with self.assertRaisesRegex(ValueError, "参数版本"):
            serve.validate(
                {"case": "casting_burr", "casting_profile": "../../scan"}
            )

    def test_original_backprojection_uses_current_pixel_convention_and_mm(self):
        rgb = np.zeros((5, 7, 3), np.uint8)
        depth = np.full((5, 7), 2.0)
        intrinsics = np.array([[100, 0, 3], [0, 100, 2], [0, 0, 1]], float)
        camera = np.eye(4)
        camera[:3, 3] = [1, 2, 3]
        mask = np.zeros((5, 7), bool)
        mask[2, 4] = True
        arrays, metadata = backproject_observation(
            rgb, depth, intrinsics, camera, mask, 9301
        )
        np.testing.assert_allclose(
            arrays["points"][arrays["roi_mask"], :3], [[1020, 2000, 5000]]
        )
        self.assertEqual(str(arrays["units"].item()), "mm")
        self.assertEqual(
            str(arrays["roi_source"].item()), "geometric_reference_detector"
        )
        self.assertTrue(metadata["adapter_executed"])
        self.assertFalse(metadata["sam_model_executed"])
        self.assertIn("geometric prediction", metadata["roi_sampling"])
        self.assertFalse(metadata["gt_defect_or_instance_labels_read"])
        empty, report = backproject_observation(
            rgb, depth, intrinsics, camera, np.zeros_like(mask), 9301
        )
        self.assertEqual(len(empty["points"]), 0)
        self.assertFalse(report["adapter_executed"])


if __name__ == "__main__":
    unittest.main()
