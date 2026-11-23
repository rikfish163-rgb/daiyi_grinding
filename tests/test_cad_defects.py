"""Check attached CAD additions and the public observation boundary."""

import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from daiyi_grinding.cad_defects import (
    DEFAULT_MESH,
    generate_defects,
    render_sample,
    top_support,
)


@unittest.skipUnless(
    DEFAULT_MESH.exists() and find_spec("trimesh") is not None,
    "Imported CAD and experiments dependencies are required",
)
class CadDefectTests(unittest.TestCase):
    def test_additions_are_embedded_and_supported(self):
        support = top_support(str(DEFAULT_MESH.resolve()))
        for case in ("seam", "burr", "thin", "coarse"):
            defects = generate_defects(DEFAULT_MESH, 7007, case)
            self.assertGreater(len(defects), 0)
            for mesh, metadata in defects:
                self.assertTrue(mesh.is_watertight)
                self.assertTrue(mesh.is_winding_consistent)
                self.assertGreater(mesh.volume, 0)
                self.assertLess(mesh.bounds[0, 2], 0.361)
                self.assertGreater(mesh.bounds[1, 2], 0.361)
                self.assertGreater(metadata["height_peak_mm"], 0)
                self.assertEqual(metadata["base_embedding_mm"], 0.08)
            self.assertGreater(support["mask"].sum(), 500)

    def test_normal_contains_no_added_defect_geometry(self):
        self.assertEqual(generate_defects(DEFAULT_MESH, 7007, "normal"), [])

    def test_renderer_exports_reference_and_isolates_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary) / "mixed"
            metadata = render_sample(sample, 7007, "mixed")
            with np.load(sample / "observation.npz") as observation:
                self.assertNotIn("truth", observation.files)
                self.assertNotIn("segmentation", observation.files)
                self.assertEqual(observation["rgb"].shape, (768, 1024, 3))
                self.assertEqual(observation["depth"].shape, (768, 1024))
                self.assertEqual(
                    observation["reference_depth"].shape, (768, 1024)
                )
                self.assertGreater(observation["inspection_bounds_min"][2], 0.9)
                self.assertLess(observation["inspection_bounds_max"][2], 1.1)
                reference = observation["reference_depth"]
            with np.load(sample / "evaluator/truth.npz") as evaluator:
                self.assertGreater((evaluator["truth"] == 1).sum(), 0)
                self.assertGreater((evaluator["truth"] == 2).sum(), 0)
                selected = np.isin(evaluator["truth"], [1, 2])
                self.assertGreater(
                    np.median(
                        reference[selected] - evaluator["ideal_depth"][selected]
                    ),
                    0.0001,
                )
            self.assertGreater(metadata["visible_label_pixels"]["1"], 0)


if __name__ == "__main__":
    unittest.main()
