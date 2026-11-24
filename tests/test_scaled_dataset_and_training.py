"""Tests for procedural castings, domain randomization, and scaled training helpers."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from daiyi_grinding import dataset_pipeline as dp
from daiyi_grinding.procedural_castings import (
    CASTING_BUILDERS,
    materialize_procedural_castings,
)
from daiyi_grinding.scaled_training import (
    augment_chip,
    map_semantic_to_3class,
)


class ProceduralCastingsTests(unittest.TestCase):
    def test_materialize_all_eight_procedural_castings_with_honest_provenance(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            records = materialize_procedural_castings(tmp)
            self.assertEqual(len(records), len(CASTING_BUILDERS))
            for record in records:
                self.assertGreater(record["faces"], 1000)
                pkg = json.loads(
                    Path(record["package_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    pkg["cad_source"], "procedural_parametric_casting"
                )
                self.assertFalse(pkg["physical_changeover_validated"])
                self.assertTrue(Path(record["mesh_path"]).is_file())


class DomainRandomizationAndHardNegativeTests(unittest.TestCase):
    def test_domain_randomized_rgb_ignores_semantic_labels_and_varies_by_rng(
        self,
    ):
        normals = np.zeros((48, 64, 3), dtype=float)
        normals[..., 2] = -1.0
        surface = np.ones((48, 64), dtype=bool)
        obs = {
            "normals": normals,
            "surface": surface,
            "specular": np.zeros((48, 64), dtype=bool),
            "oxide": np.zeros((48, 64), dtype=bool),
            "dropout": np.zeros((48, 64), dtype=bool),
        }
        sem_a = np.full(
            (48, 64), dp.SEMANTIC_LABELS["nominal_cad"], dtype=np.uint8
        )
        sem_b = np.full((48, 64), dp.SEMANTIC_LABELS["flash"], dtype=np.uint8)
        rgb_a = dp._synthetic_rgb(
            obs, sem_a, rng=np.random.default_rng(42), domain_randomization=True
        )
        rgb_b = dp._synthetic_rgb(
            obs, sem_b, rng=np.random.default_rng(42), domain_randomization=True
        )
        np.testing.assert_array_equal(rgb_a, rgb_b)

        rgb_c = dp._synthetic_rgb(
            obs, sem_a, rng=np.random.default_rng(99), domain_randomization=True
        )
        self.assertGreater(
            float(np.abs(rgb_a.astype(float) - rgb_c.astype(float)).mean()), 2.0
        )

    def test_normal_hard_negative_has_zero_defects_and_nonzero_drift_residual(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            rec = materialize_procedural_castings(tmp, ids=["p01"])[0]
            mesh = dp.load_mesh(rec["mesh_path"])
            feats = dp.extract_features(
                mesh, "p01", rec["mesh_path"], dp.FeatureConfig()
            )
            profile = dp.SCALED_REGIME_PROFILES["high_drift_warped"]
            rng = np.random.default_rng(123)
            sample = profile.draw(rng)
            arrays, rgb, meta = dp.render_normal_sample(
                mesh,
                feats,
                profile,
                sample,
                rng,
                regime_name="high_drift_warped",
                image_size=(160, 120),
            )
            self.assertEqual(rgb.shape, (120, 160, 3))
            self.assertEqual(meta["peak_residual_mm"], 0.0)
            self.assertFalse(np.isin(arrays["semantic"], (1, 2, 5, 6)).any())
            valid = np.isfinite(arrays["depth_m"]) & np.isfinite(
                arrays["depth_nominal_m"]
            )
            self.assertTrue(valid.any())
            diff_mm = np.abs(
                (arrays["depth_nominal_m"][valid] - arrays["depth_m"][valid])
                * 1000.0
            )
            self.assertGreater(float(np.percentile(diff_mm, 95)), 0.15)


class ScaledTrainingHelperTests(unittest.TestCase):
    def test_map_semantic_to_3class_handles_all_classes_and_intermediates(self):
        sem = np.array([[0, 1, 2, 3, 4, 5, 6, 6]], dtype=np.uint8)
        res = np.array(
            [[0.0, 0.8, 0.7, 0.0, 2.5, 1.2, 0.18, -0.08]], dtype=np.float32
        )
        mapped_flash = map_semantic_to_3class(sem, res, "flash")
        np.testing.assert_array_equal(mapped_flash, [[0, 1, 2, 0, 0, 1, 1, 0]])
        mapped_burr = map_semantic_to_3class(sem, res, "burr")
        np.testing.assert_array_equal(mapped_burr, [[0, 1, 2, 0, 0, 1, 2, 0]])

    def test_augment_chip_preserves_shape_finiteness_and_applies_rgb_dropout(
        self,
    ):
        x = np.ones((11, 256, 256), dtype=np.float32) * 0.4
        y = np.zeros((256, 256), dtype=np.uint8)
        y[100:120, 100:120] = 2
        rng = np.random.default_rng(7)
        saw_zero_rgb = False
        for _ in range(30):
            ax, ay = augment_chip(
                x, y, rng, rgb_dropout_prob=0.5, residual_tilt_prob=0.5
            )
            self.assertEqual(ax.shape, (11, 256, 256))
            self.assertEqual(ay.shape, (256, 256))
            self.assertTrue(np.isfinite(ax).all())
            if float(np.abs(ax[:3]).max()) == 0.0:
                saw_zero_rgb = True
        self.assertTrue(saw_zero_rgb)
