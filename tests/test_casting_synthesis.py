"""Root attachment, real thin-sheet geometry and supervision boundaries."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

from daiyi_grinding import serve
from daiyi_grinding.cad_defects import DEFAULT_MESH
from daiyi_grinding.casting_defects import (
    casting_appearance,
    generate_casting_defects,
)
from daiyi_grinding.casting_synthesis import (
    LabelledInspection,
    macro_preview,
    project_roots,
)
from daiyi_grinding.workstation_model import Workstation, build_scene


class CastingGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = generate_casting_defects(
            DEFAULT_MESH, 9301, "casting_mixed"
        )

    def test_each_fragment_is_closed_and_embedded_with_normal_stock(self):
        for mesh, item in self.items:
            with self.subTest(morphology=item["morphology"]):
                self.assertTrue(mesh.is_watertight)
                self.assertTrue(mesh.is_winding_consistent)
                self.assertGreater(mesh.volume, 0)
                vertices = np.asarray(item["original_vertices"])
                base = np.asarray(item["base_vertices"])
                normals = np.asarray(item["vertex_normals"])
                displacement = vertices - base
                heights = np.einsum("ij,ij->i", displacement, normals)
                tangent_error = displacement - normals * heights[:, None]
                self.assertLess(
                    np.linalg.norm(tangent_error, axis=1).max(), 0.00015
                )
                components = mesh.split(only_watertight=True)
                self.assertEqual(len(components), len(item["root_curves_m"]))
                for boundary in item["removal_boundary_curves_m"]:
                    np.testing.assert_allclose(boundary[0], boundary[-1])
                for component in components:
                    _, indices = cKDTree(vertices).query(component.vertices)
                    self.assertLess(heights[indices].min(), -0.00002)
                    self.assertGreater(heights[indices].max(), 0.00025)
                # Independently measure root distance to the supplied CAD.
                cad = trimesh.load_mesh(DEFAULT_MESH, process=True)
                samples = np.vstack(item["root_curves_m"])[::15]
                centers = cad.triangles_center
                near = cKDTree(samples).query(centers)[0] < 0.020
                patch = cad.submesh(
                    [np.flatnonzero(near)], append=True, repair=False
                )
                _, distance, _ = trimesh.proximity.closest_point_naive(
                    patch, samples
                )
                self.assertLess(distance.max(), 0.000015)

    def test_curled_sheet_really_turns_down_and_torn_fragments_keep_gaps(self):
        mesh, item = next(
            pair
            for pair in self.items
            if pair[1]["morphology"] == "curled_flash"
        )
        rows = len(item["root_curves_m"][0])
        half = len(mesh.vertices) // 2
        center = (mesh.vertices[:half] + mesh.vertices[half:]).reshape(
            rows, 23, 3
        ) / 2
        normal = np.asarray(item["surface_normal"])
        increments = np.diff(center, axis=1) @ normal
        self.assertTrue((increments[:, 0] > 0).all())
        self.assertTrue((increments[:, -1] < 0).all())
        for _, broken in self.items:
            if broken["morphology"] in {"torn_flash", "interrupted_flash"}:
                self.assertEqual(len(broken["root_curves_m"]), 3)
                self.assertEqual(len(broken["parameters"]["actual_gaps_mm"]), 2)
                self.assertGreater(
                    min(broken["parameters"]["actual_gaps_mm"]), 1.0
                )
                if broken["morphology"] == "torn_flash":
                    self.assertGreater(
                        broken["parameters"]["minimum_inner_bend_radius_mm"],
                        0.0,
                    )

    def test_seed_is_reproducible_and_changes_shape(self):
        first = generate_casting_defects(DEFAULT_MESH, 9301, "casting_curled")
        repeat = generate_casting_defects(DEFAULT_MESH, 9301, "casting_curled")
        other = generate_casting_defects(DEFAULT_MESH, 9302, "casting_curled")
        np.testing.assert_array_equal(
            first[0][0].vertices, repeat[0][0].vertices
        )
        self.assertNotEqual(
            first[0][0].vertices.tobytes(), other[0][0].vertices.tobytes()
        )

    def test_http_cases_are_restricted_to_current_workstation(self):
        self.assertEqual(
            serve.validate({"case": "casting_curled"})["case"],
            "casting_curled",
        )
        with self.assertRaisesRegex(ValueError, "ESTUN"):
            serve.validate({"case": "casting_curled", "scene": "fr5"})

    def test_normal_control_has_no_added_geometry(self):
        self.assertEqual(
            generate_casting_defects(DEFAULT_MESH, 9301, "casting_normal"), []
        )
        self.assertNotEqual(casting_appearance(9301), casting_appearance(9302))
        with tempfile.TemporaryDirectory() as directory:
            appearances = []
            for case in ("casting_normal", "casting_curled"):
                scene, _ = build_scene(Path(directory) / case, 9301, case)
                appearances.append(
                    ET.parse(scene)
                    .find(".//material[@name='wp_097_mat0']")
                    .attrib
                )
            self.assertEqual(appearances[0], appearances[1])


class CastingLabelTests(unittest.TestCase):
    def test_hidden_and_missing_depth_are_distinct(self):
        points = np.array([[0, 0, 1], [0, 0, 2], [0, 0, -1]], dtype=float)
        intrinsics = np.array([[100, 0, 5], [0, 100, 5], [0, 0, 1]])
        ideal = np.ones((11, 11))
        measured = ideal.copy()
        measured[5, 5] = np.nan
        labels = project_roots(points, intrinsics, np.eye(4), ideal, measured)
        np.testing.assert_array_equal(
            labels["root_depth_consistent"], [True, False, False]
        )
        self.assertFalse(labels["root_sensor_depth_valid"].any())
        np.testing.assert_array_equal(
            labels["root_in_image"], [True, True, False]
        )

    def test_current_camera_export_keeps_truth_separate_and_material_identical(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sim = Workstation(folder, 9301, "casting_curled")
            try:
                inspection = LabelledInspection(sim, 9301)
                inspection.rotate(0)
                inspection.capture(0)
                nominal_material = sim.model.mat("wp_097_mat0").id
                self.assertEqual(
                    sim.model.geom_matid[sim.model.geom("added_defect_0").id],
                    nominal_material,
                )
                with np.load(folder / "public/view_00.npz") as public:
                    self.assertEqual(public["rgb"].shape, (768, 1024, 3))
                    self.assertIn("encoder_angle_deg", public.files)
                    self.assertIn("T_world_workpiece", public.files)
                    self.assertFalse(
                        any(
                            term in key
                            for key in public.files
                            for term in (
                                "truth",
                                "semantic",
                                "instance",
                                "root",
                            )
                        )
                    )
                with np.load(folder / "evaluator/view_00_labels.npz") as labels:
                    self.assertIn("root_curve_offsets", labels.files)
                    self.assertGreater(
                        len(labels["root_points_workpiece_m"]), 30
                    )
                    self.assertEqual(labels["semantic"].shape, (768, 1024))
                    self.assertGreater((labels["instance"] > 0).sum(), 5)
                original_near = sim.model.vis.map.znear
                original_fixed = sim.data.cam_xpos[
                    sim.model.camera("inspection").id
                ].copy()
                preview = macro_preview(
                    sim, sim.private_defects[0], folder / "macro.png"
                )
                self.assertGreater(preview["defect_visible_pixels"], 30)
                self.assertEqual(sim.model.vis.map.znear, original_near)
                np.testing.assert_allclose(
                    sim.data.cam_xpos[sim.model.camera("inspection").id],
                    original_fixed,
                )
            finally:
                sim.close()


if __name__ == "__main__":
    unittest.main()
