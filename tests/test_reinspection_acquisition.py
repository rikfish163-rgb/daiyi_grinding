"""Repeated fixed-pose acquisition must preserve stock and missing data."""

import unittest

import numpy as np

from daiyi_grinding.workstation_inspection import sample_depth


class ReinspectionAcquisitionTests(unittest.TestCase):
    def test_repeats_reduce_noise_without_smoothing_signed_local_features(self):
        ideal = np.ones((64, 64))
        ideal[10:20, 10:20] -= 0.0008
        ideal[40:50, 40:50] += 0.00045
        depth, frames, counts = sample_depth(
            ideal, np.random.default_rng(97103), 0.00006, 0, 9
        )
        self.assertEqual(frames.shape, (9, 64, 64))
        self.assertTrue(np.all(counts == 9))
        self.assertLess(np.std(depth - ideal), np.std(frames[0] - ideal) / 2)
        self.assertAlmostEqual(
            (1 - depth[10:20, 10:20].mean()) * 1000, 0.8, delta=0.01
        )
        self.assertAlmostEqual(
            (1 - depth[40:50, 40:50].mean()) * 1000, -0.45, delta=0.01
        )

    def test_missing_repeated_frames_remain_unobserved(self):
        depth, frames, counts = sample_depth(
            np.ones((8, 8)), np.random.default_rng(97107), 0.00006, 1, 9
        )
        self.assertTrue(np.isnan(depth).all())
        self.assertTrue(np.isnan(frames).all())
        self.assertTrue(np.all(counts == 0))

    def test_additional_acquisition_keeps_the_original_independent_frames(self):
        rng = np.random.default_rng(97111)
        ideal = np.ones((64, 64))
        depth, original, _ = sample_depth(ideal, rng, 0.00006, 0, 9)
        refined, frames, counts = sample_depth(
            ideal, rng, 0.00006, 0, 25, previous_frames=original
        )
        np.testing.assert_array_equal(frames[:9], original)
        self.assertTrue(np.all(counts == 25))
        self.assertLess(np.std(refined - ideal), np.std(depth - ideal) * 0.8)


if __name__ == "__main__":
    unittest.main()
