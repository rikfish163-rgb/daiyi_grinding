"""Fixed-pixel RGB-D must follow current surface geometry after grinding."""

import unittest

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.cad_defects import camera_axes
from daiyi_grinding.perception import unproject
from daiyi_grinding.workstation_model import Workstation
from daiyi_grinding.workstation_motion import GeometricRemoval
from daiyi_grinding.workstation_tooling import FixedTool


class WorkstationSensorRenderingTests(unittest.TestCase):
    def setUp(self):
        camera = camera_axes([0, -0.5, 0.8], [0, 0, 0])
        self.sim = Workstation.__new__(Workstation)
        self.sim.model = mujoco.MjModel.from_xml_string(
            f"""
            <mujoco>
              <visual>
                <global offwidth="128" offheight="128"/>
                <quality offsamples="4"/>
                <map znear="0.02" zfar="25"/>
              </visual>
              <asset>
                <mesh name="excess"
                  vertex="-0.05 -0.05 -0.00008
                           0.05 -0.05 -0.00008
                           0.05  0.05 -0.00008
                          -0.05  0.05 -0.00008
                          -0.05 -0.05  0.005
                           0.05 -0.05  0.005
                           0.05  0.05  0.005
                          -0.05  0.05  0.005"
                  face="0 2 1 0 3 2 4 5 6 4 6 7
                        0 1 5 0 5 4 1 2 6 1 6 5
                        2 3 7 2 7 6 3 0 4 3 4 7"/>
              </asset>
              <worldbody>
                <camera name="inspection" pos="0 -0.5 0.8"
                        xyaxes="{camera}" fovy="44"/>
                <body name="workpiece_097">
                  <freejoint name="workpiece_free"/>
                  <inertial pos="0 0 0" mass="1"
                            diaginertia="0.1 0.1 0.1"/>
                  <geom name="wp_nominal" type="box" pos="0 0 -0.01"
                        size="1 1 0.01" contype="0" conaffinity="0"/>
                  <geom name="added_defect_0" type="mesh" mesh="excess"
                        rgba="1 0 0 1" group="4"
                        contype="0" conaffinity="0"/>
                </body>
              </worldbody>
            </mujoco>
            """
        )
        self.sim.data = mujoco.MjData(self.sim.model)
        self.sim.workpiece = self.sim.model.body("workpiece_097").id
        self.sim.renderers = {}
        self.sim.private_defects = [
            {
                "geom": "added_defect_0",
                "kind": "seam",
                "nominal_plane_z_m": 0,
            }
        ]
        self.sim.tool = FixedTool(
            "belt",
            "test plane",
            "",
            np.array([-0.0001, 0, 0]),
            np.array([-1.0, 0, 0]),
            np.array([0.0005, 1, 1]),
            "analytic contact surface",
        )
        mujoco.mj_forward(self.sim.model, self.sim.data)

    def tearDown(self):
        self.sim.close()

    def surface_points(self, *, reference=False):
        depth = self.sim.render(
            mode="depth", reference=reference, width=128, height=128
        )
        intrinsics, camera = self.sim.camera_info(
            width=128, height=128, frame="world"
        )
        return unproject(depth, intrinsics, camera)[60:68, 60:68]

    def test_depth_matches_the_calibrated_pixel_center_ray(self):
        points = self.surface_points(reference=True)
        np.testing.assert_allclose(points[..., 2], 0, atol=0.000002)
        self.assertEqual(self.sim.model.vis.quality.offsamples, 4)

    def test_cut_updates_both_rgb_and_measurement_renderers(self):
        before_rgb = self.sim.render(width=128, height=128)
        self.assertGreater(self.surface_points()[..., 2].min(), 0.004)
        removal = GeometricRemoval(self.sim)
        process_rotation = np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]])
        self.sim.data.qpos[3:7] = Rotation.from_matrix(
            process_rotation
        ).as_quat()[[3, 0, 1, 2]]
        mujoco.mj_forward(self.sim.model, self.sim.data)
        removal.cut()
        self.sim.data.qpos[3:7] = [1, 0, 0, 0]
        mujoco.mj_forward(self.sim.model, self.sim.data)
        removal.upload()
        np.testing.assert_allclose(
            self.surface_points()[..., 2], 0.0001, atol=0.000002
        )
        after_rgb = self.sim.render(width=128, height=128)
        self.assertFalse(np.array_equal(before_rgb, after_rgb))
        self.assertFalse(removal.changed)


if __name__ == "__main__":
    unittest.main()
