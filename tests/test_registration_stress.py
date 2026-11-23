"""Registration stress schema, coordinate and rejection checks."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import trimesh

from daiyi_grinding.cad_defects import DEFAULT_MESH
from daiyi_grinding.casting_scan_calibration import load_cloud
from daiyi_grinding.registration_stress import (
    INPUT_SCHEMA,
    SurfaceProjector,
    rigid_transform,
    run,
    run_error_budget,
    validate_import_manifest,
    write_ascii_pcd,
)
from daiyi_grinding.team_data_reuse import sha256


class RegistrationStressTests(unittest.TestCase):
    def test_rigid_transform_convention_and_pcd_roundtrip(self):
        transform = rigid_transform(
            rotation_deg=(10, -20, 30),
            translation_m=(0.02, -0.03, 0.04),
        )
        inverse = np.linalg.inv(transform)
        point = np.array([0.12, -0.08, 0.05, 1.0])
        np.testing.assert_allclose(
            inverse @ transform @ point, point, atol=1e-12
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.pcd"
            points = np.array([[0.001, -0.002, 0.003], [np.nan, 0.004, np.nan]])
            write_ascii_pcd(path, points, units="mm")
            loaded = load_cloud(path, "mm")
            np.testing.assert_allclose(
                loaded, points, atol=1e-7, equal_nan=True
            )
            self.assertIn(
                "VIEWPOINT 0 0 0 1 0 0 0", path.read_text(encoding="ascii")
            )

    def test_large_face_is_not_missed_by_centroid_candidates(self):
        mesh = trimesh.Trimesh(
            vertices=[
                [-1, -1, 0],
                [10, -1, 0],
                [-1, 10, 0],
                [-0.1, -0.1, 0.01],
                [0.1, -0.1, 0.01],
                [0, 0.1, 0.01],
            ],
            faces=[[0, 1, 2], [3, 4, 5]],
            process=False,
        )
        point = np.array([[0, 0, 0.001]])
        closest, distance, face = SurfaceProjector(
            mesh, candidate_faces=1
        ).project(point)
        np.testing.assert_allclose(closest, [[0, 0, 0]], atol=1e-12)
        np.testing.assert_allclose(distance, [0.001], atol=1e-12)
        np.testing.assert_array_equal(face, [0])

    def test_vector_error_budget_matches_analytic_rms(self):
        budget = run_error_budget(seed=20261123, samples=20000)
        self.assertAlmostEqual(
            budget["monte_carlo"]["rms_mm"],
            budget["analytic_rss_1sigma_mm"],
            delta=0.005,
        )
        self.assertGreater(budget["p95_mm"], budget["target_mm"])
        self.assertFalse(budget["supports_target"])

    def test_real_import_cannot_include_synthetic_truth(self):
        mesh = trimesh.creation.icosphere(subdivisions=1, radius=0.1)
        with tempfile.TemporaryDirectory() as directory:
            cloud = Path(directory) / "cloud.pcd"
            points = np.zeros((3, 3))
            write_ascii_pcd(cloud, points, units="mm")
            manifest = {
                "schema": INPUT_SCHEMA,
                "part_id": "097",
                "cad_sha256": sha256(DEFAULT_MESH),
                "part_matches_cad_confirmed": True,
                "identity_evidence": "real fixture evidence",
                "source_kind": "real_scan",
                "cloud_file": cloud.name,
                "cloud_sha256": sha256(cloud),
                "units": "mm",
                "T_workpiece_scan_m": np.eye(4).tolist(),
                "normal_train_indices": [0],
                "normal_holdout_indices": [1],
                "processing_indices": [2],
                "processing_permission_confirmed": True,
                "truth": {"invalid_for_real": True},
            }
            with self.assertRaisesRegex(ValueError, "synthetic truth"):
                validate_import_manifest(manifest, mesh, sha256(DEFAULT_MESH))

    def test_default_small_suite_separates_fit_and_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "registration"
            summary = run(output, seed=20261123, monte_carlo_samples=512)
            self.assertEqual(summary["cases_passed"], 4)
            self.assertEqual(summary["cases_rejected_as_expected"], 3)
            self.assertEqual(summary["unexpected_failures"], 0)
            self.assertTrue(summary["all_expectations_met"])
            self.assertFalse(summary["error_budget"]["supports_target"])
            for case_name in (
                "known_transform",
                "noisy",
                "dropout",
                "occluded",
            ):
                result = json.loads(
                    (output / case_name / "case.json").read_text()
                )
                self.assertEqual(result["status"], "pass")
                self.assertIn("fit", result["registration"])
                self.assertIsNotNone(result["independent_holdout"])
            rejections = {}
            for case_name in (
                "incorrect_units",
                "degenerate_normals",
                "wrong_cad_identity",
            ):
                result = json.loads(
                    (output / case_name / "case.json").read_text()
                )
                self.assertEqual(result["status"], "rejected_expected")
                rejections[case_name] = result["reason"]
            self.assertIn("extent", rejections["incorrect_units"])
            self.assertIn("six rigid", rejections["degenerate_normals"])
            self.assertIn("CAD identity", rejections["wrong_cad_identity"])
            evidence = json.loads((output / "evidence.json").read_text())
            for key in (
                "task",
                "scope",
                "commands",
                "inputs",
                "metrics",
                "failures",
                "artifacts",
                "real_data_pending",
            ):
                self.assertIn(key, evidence)
            self.assertFalse(evidence["scope"]["real_scan_used"])
            self.assertTrue(
                evidence["real_data_pending"][
                    "independent_real_097_scan_with_holdout_labels"
                ]
            )


if __name__ == "__main__":
    unittest.main()
