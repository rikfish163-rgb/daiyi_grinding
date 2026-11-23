"""Convention checks, plus image-detector rejection; run with unittest."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.calibration import (
    estimate_board_pose,
    inverse,
    known_intrinsics,
    load_inspection_calibration,
    solve_eye_to_hand,
    transform,
)


class ConventionTests(unittest.TestCase):
    def test_transform_inverse_and_optical_axes(self):
        T_world_camera = transform(
            np.diag([1.0, -1.0, -1.0]), [-0.49, -0.13, 1.13]
        )
        np.testing.assert_allclose(
            T_world_camera @ inverse(T_world_camera), np.eye(4), atol=1e-12
        )
        world = T_world_camera @ np.array([0.03, 0.02, 0.5, 1.0])
        np.testing.assert_allclose(world, [-0.46, -0.15, 0.63, 1.0])
        T_world_base = transform(t=[0, 0, 0.72])
        np.testing.assert_allclose(
            (inverse(T_world_base) @ T_world_camera)[:3, 3],
            [-0.49, -0.13, 0.41],
        )

    def test_eye_to_hand_inverse_input_returns_base_camera(self):
        rng = np.random.default_rng(29)
        true_camera = transform(
            Rotation.from_euler("xyz", [16, -9, 172], degrees=True).as_matrix(),
            [0.3, -0.2, 0.5],
        )
        mount = transform(
            Rotation.from_euler("xyz", [5, 18, -13], degrees=True).as_matrix(),
            [0.02, -0.01, 0.03],
        )
        grippers = [
            transform(
                Rotation.from_rotvec(rng.uniform(-0.7, 0.7, 3)).as_matrix(),
                rng.uniform(-0.4, 0.4, 3),
            )
            for _ in range(15)
        ]
        observed = [inverse(true_camera) @ g @ mount for g in grippers]
        camera, estimated_mount = solve_eye_to_hand(grippers, observed)
        np.testing.assert_allclose(camera, true_camera, atol=1e-10)
        np.testing.assert_allclose(estimated_mount, mount, atol=1e-10)

    def test_blank_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "checkerboard_not_detected"):
            estimate_board_pose(
                np.zeros((576, 768, 3), np.uint8), known_intrinsics()
            )

    def test_loader_rejects_wrong_seed_placement(self):
        report = dict(
            acceptance_passed=True,
            camera="inspection",
            width=768,
            height=576,
            K=known_intrinsics().tolist(),
            T_world_camera=np.eye(4).tolist(),
            camera_configuration=dict(position_world_m=[-0.49, -0.13, 1.13]),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "placement differs"):
                load_inspection_calibration(
                    path, camera_position=[-0.48, -0.13, 1.13]
                )


if __name__ == "__main__":
    unittest.main()
