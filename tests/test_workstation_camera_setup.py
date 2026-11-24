"""Fixed camera placement must agree with scene optics and sensor housing."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from daiyi_grinding.cad_defects import camera_axes
from daiyi_grinding.workstation_model import (
    CAMERA_SETUPS,
    CELL_CONFIG,
    Workstation,
    build_scene,
    inspection_camera_config,
)
from daiyi_grinding.workstation_run import parser


class CameraSetupTests(unittest.TestCase):
    def test_original_preserves_all_camera_settings(self):
        self.assertEqual(inspection_camera_config(), CELL_CONFIG["camera"])
        self.assertEqual(
            inspection_camera_config("original"), CELL_CONFIG["camera"]
        )

    def test_low_side_changes_only_fixed_camera_height(self):
        camera = inspection_camera_config("low_side_simulation")
        self.assertEqual(camera["position_world_m"], [-0.4, -1.25, 1.2])
        self.assertEqual(
            {
                key: value
                for key, value in camera.items()
                if key != "position_world_m"
            },
            {
                key: value
                for key, value in CELL_CONFIG["camera"].items()
                if key != "position_world_m"
            },
        )
        self.assertEqual(
            CELL_CONFIG["camera"]["position_world_m"], [-0.4, -1.25, 1.4]
        )

    def test_unknown_setup_rejects_before_creating_a_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "invalid"
            with self.assertRaisesRegex(ValueError, "相机配置"):
                Workstation(output, camera_setup="moving_camera")
            self.assertFalse(output.exists())

    def test_cli_selects_the_registered_fixed_camera(self):
        self.assertEqual(parser().parse_args([]).camera_setup, "original")
        for name in CAMERA_SETUPS:
            with self.subTest(camera_setup=name):
                self.assertEqual(
                    parser().parse_args(["--camera-setup", name]).camera_setup,
                    name,
                )

    def test_scene_uses_selected_fixed_camera_axes_and_housing(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("original", "low_side_simulation"):
                with self.subTest(camera_setup=name):
                    camera = inspection_camera_config(name)
                    scene, defects = build_scene(
                        Path(directory) / name,
                        seed=9000,
                        case="normal",
                        camera_config=camera,
                    )
                    self.assertEqual(defects, [])
                    root = ET.parse(scene)
                    sensor = root.find(".//camera[@name='inspection']")
                    housing = root.find(
                        ".//body[@name='fixed_inspection_camera']"
                    )
                    np.testing.assert_allclose(
                        np.fromstring(sensor.get("pos"), sep=" "),
                        camera["position_world_m"],
                        atol=1e-8,
                    )
                    np.testing.assert_allclose(
                        np.fromstring(sensor.get("xyaxes"), sep=" "),
                        np.fromstring(
                            camera_axes(
                                camera["position_world_m"],
                                camera["target_world_m"],
                            ),
                            sep=" ",
                        ),
                        atol=1e-8,
                    )
                    self.assertEqual(
                        float(sensor.get("fovy")), camera["fovy_deg"]
                    )
                    position = np.asarray(camera["position_world_m"])
                    target = np.asarray(camera["target_world_m"])
                    expected_housing = position + 0.07 * (
                        position - target
                    ) / np.linalg.norm(position - target)
                    np.testing.assert_allclose(
                        np.fromstring(housing.get("pos"), sep=" "),
                        expected_housing,
                        atol=1e-8,
                    )
                    self.assertIs(
                        root.find(
                            ".//body[@name='fixed_inspection_camera']/.."
                        ),
                        root.find("./worldbody"),
                    )


if __name__ == "__main__":
    unittest.main()
