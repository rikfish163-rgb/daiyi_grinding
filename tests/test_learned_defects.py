import json
import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daiyi_grinding.defect_training import data_index
from daiyi_grinding.learned_defects import (
    FEATURE_VERSION,
    LearnedDetector,
    registered_checkpoint,
    sensor_features,
    tile_origins,
)


@unittest.skipUnless(find_spec("torch"), "requires optional PyTorch")
class LearnedThreadTests(unittest.TestCase):
    def setUp(self):
        import torch

        from daiyi_grinding.defect_network import DefectUNet

        self.addCleanup(torch.set_num_threads, torch.get_num_threads())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkpoint = Path(directory.name) / "weights.pt"
        model = DefectUNet(width=4)
        torch.save(
            {
                "feature_version": FEATURE_VERSION,
                "model_config": model.config,
                "state_dict": model.state_dict(),
                "probability_threshold": 0.7,
            },
            self.checkpoint,
        )

    def test_explicit_omp_thread_count_is_preserved(self):
        import torch

        for count in (1, 2):
            with self.subTest(count=count):
                with patch.dict(os.environ, {"OMP_NUM_THREADS": str(count)}):
                    LearnedDetector(self.checkpoint, device="cpu")
                    self.assertEqual(torch.get_num_threads(), count)

    def test_unspecified_omp_thread_count_keeps_default(self):
        import torch

        with patch.dict(os.environ):
            os.environ.pop("OMP_NUM_THREADS", None)
            LearnedDetector(self.checkpoint, device="cpu")
            self.assertEqual(torch.get_num_threads(), 4)


class SensorFeatureTests(unittest.TestCase):
    def test_invalid_depth_has_explicit_validity_and_finite_features(self):
        depth = np.ones((32, 40), np.float32)
        depth[10, 12] = np.nan
        rgb = np.full((32, 40, 3), 120, np.uint8)
        intrinsics = np.array([[100, 0, 20], [0, 100, 16], [0, 0, 1]])
        features = sensor_features(rgb, depth, np.ones_like(depth), intrinsics)
        self.assertEqual(features.shape, (11, 32, 40))
        self.assertTrue(np.isfinite(features).all())
        self.assertEqual(features[-1, 10, 12], 0)
        self.assertEqual(features[3, 10, 12], 0)
        self.assertEqual(features[-1, 11, 12], 1)

    def test_foreground_only_cad_reference_with_nan_background_succeeds(self):
        reference = np.full((32, 40), np.nan, np.float32)
        reference[8:24, 10:30] = 0.85
        depth = reference.copy()
        depth[12:16, 15:20] = 0.849
        rgb = np.full((32, 40, 3), 120, np.uint8)
        intrinsics = np.array([[100, 0, 20], [0, 100, 16], [0, 0, 1]])
        features = sensor_features(rgb, depth, reference, intrinsics)
        self.assertEqual(features.shape, (11, 32, 40))
        self.assertTrue(np.isfinite(features).all())
        self.assertEqual(features[-1, 0, 0], 0)
        self.assertEqual(features[3, 0, 0], 0)
        self.assertAlmostEqual(float(features[3, 14, 17]), 1.0 / 6.0, places=4)
        with self.assertRaisesRegex(ValueError, "Too few valid depth"):
            sensor_features(
                rgb, depth, np.full_like(reference, np.nan), intrinsics
            )

    def test_tiling_covers_image_boundaries_without_resizing(self):
        for length in (80, 256, 768, 1024):
            covered = np.zeros(length, bool)
            for position in tile_origins(length):
                covered[position : position + 256] = True
            self.assertTrue(covered.all())


class LearnedCandidateTests(unittest.TestCase):
    def test_model_can_find_region_outside_old_detector_mask(self):
        detector = LearnedDetector.__new__(LearnedDetector)
        detector.threshold = 0.7
        probability = np.zeros((3, 32, 32), np.float32)
        probability[0] = 1
        probability[:, 12:16, 10:18] = 0
        probability[2, 12:16, 10:18] = 0.95
        probability[0, 12:16, 10:18] = 0.05
        detector.probabilities = lambda *args: probability
        detector.metadata = lambda: {"truth_used_for_inference": False}
        rows, cols = np.indices((32, 32))
        xyz = np.stack((cols / 1000, rows / 1000, np.ones_like(rows)), -1)
        observation = {
            "surface": np.ones((32, 32), bool),
            "selection": np.ones((32, 32), bool),
            "residual": np.full((32, 32), 0.001),
            "mask": np.zeros((32, 32), bool),
            "xyz": xyz,
            "scale": 0.001,
            "noise_sigma_mm": 0.12,
            "K": np.eye(3),
        }
        detections, _ = detector.detect(
            None, np.ones((32, 32)), None, observation
        )
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "burr")
        self.assertEqual(observation["mask"].sum(), 32)
        # Exact same model output cannot authorize masked/hidden surfaces.
        observation["surface"][12:16, 10:18] = False
        detections, _ = detector.detect(
            None, np.ones((32, 32)), None, observation
        )
        self.assertEqual(detections, [])
        self.assertFalse(observation["mask"].any())

    def test_unverified_checkpoint_is_not_exposed_to_web(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "checkpoint": "missing.pt",
                        "checkpoint_sha256": "wrong",
                        "feature_version": "rgbd_registered_v1",
                    }
                )
            )
            with patch(
                "daiyi_grinding.learned_defects.REGISTRY_FILE", registry
            ):
                with self.assertRaisesRegex(ValueError, "模型文件"):
                    registered_checkpoint()


class EntitySplitTests(unittest.TestCase):
    def test_same_entity_cannot_cross_training_validation_or_test(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            data_index(
                [],
                {
                    "training_seeds": [1, 2],
                    "validation_seeds": [2],
                    "test_seeds": [3],
                },
            )

    def test_missing_complete_entity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing complete"):
            data_index(
                [],
                {
                    "training_seeds": [1],
                    "validation_seeds": [2],
                    "test_seeds": [3],
                },
            )


if __name__ == "__main__":
    unittest.main()
