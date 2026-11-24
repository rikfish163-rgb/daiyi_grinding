"""Finite stock removal must depend on actual footprint and elapsed engagement."""

import contextlib
import io
import unittest

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.workstation_model import Workstation
from daiyi_grinding.workstation_motion import GeometricRemoval
from daiyi_grinding.workstation_run import parser
from daiyi_grinding.workstation_tooling import fixed_tool


def stock_simulator():
    """Build an actual excess mesh facing the production fixed belt."""
    sim = Workstation.__new__(Workstation)
    sim.model = mujoco.MjModel.from_xml_string(
        """
        <mujoco>
          <asset>
            <mesh name="excess"
              vertex="-0.01 -0.01 -0.00008 0.01 -0.01 -0.00008
                       0.01  0.01 -0.00008 -0.01 0.01 -0.00008
                      -0.01 -0.01  0.005 0.01 -0.01 0.005
                       0.01  0.01  0.005 -0.01 0.01 0.005"
              face="0 2 1 0 3 2 4 5 6 4 6 7
                    0 1 5 0 5 4 1 2 6 1 6 5
                    2 3 7 2 7 6 3 0 4 3 4 7"/>
          </asset>
          <worldbody>
            <body name="workpiece_097">
              <freejoint/>
              <inertial pos="0 0 0" mass="1" diaginertia="0.1 0.1 0.1"/>
              <geom name="added_defect_0" type="mesh" mesh="excess"/>
            </body>
          </worldbody>
        </mujoco>
        """
    )
    sim.data = mujoco.MjData(sim.model)
    sim.workpiece = sim.model.body("workpiece_097").id
    sim.tool = fixed_tool("belt")
    sim.private_defects = [
        {
            "geom": "added_defect_0",
            "kind": "seam",
            "nominal_plane_z_m": 0,
        }
    ]
    sim.data.qpos[:3] = sim.tool.contact + [0.0001, 0, 0]
    sim.data.qpos[3:7] = Rotation.from_euler("y", -90, degrees=True).as_quat()[
        [3, 0, 1, 2]
    ]
    mujoco.mj_forward(sim.model, sim.data)
    return sim


class FiniteRemovalTests(unittest.TestCase):
    def test_stock_accumulates_rate_without_crossing_the_tool_surface(self):
        removal = GeometricRemoval(stock_simulator(), rate_mm_s=1.0)
        removal.cut(duration_s=0.5)
        self.assertAlmostEqual(
            removal.summary()["remaining_peak_mm"], 4.5, places=5
        )
        removal.cut(duration_s=0.5)
        self.assertAlmostEqual(
            removal.summary()["remaining_peak_mm"], 4.0, places=5
        )
        removal.cut(duration_s=10)
        self.assertAlmostEqual(
            removal.summary()["remaining_peak_mm"], 0.1, places=5
        )
        np.testing.assert_array_equal(removal.parts[0]["base"][:, 2], 0)

    def test_time_outside_the_actual_footprint_does_not_remove_stock(self):
        sim = stock_simulator()
        removal = GeometricRemoval(sim, rate_mm_s=1.0)
        sim.data.qpos[1] += 1
        sim.data.time += 30
        mujoco.mj_forward(sim.model, sim.data)
        removal.cut(duration_s=30)
        np.testing.assert_array_equal(
            removal.parts[0]["vertices"], removal.parts[0]["initial"]
        )
        sim.data.qpos[1] -= 1
        mujoco.mj_forward(sim.model, sim.data)
        removal.cut(duration_s=0.1)
        self.assertAlmostEqual(
            removal.summary()["remaining_peak_mm"], 4.9, places=5
        )

    def test_same_engagement_time_is_independent_of_update_interval(self):
        peaks = []
        for interval, count in ((1.0, 1), (0.01, 100), (0.02, 50)):
            removal = GeometricRemoval(stock_simulator(), rate_mm_s=1.0)
            for _ in range(count):
                removal.cut(duration_s=interval)
            peaks.append(removal.summary()["remaining_peak_mm"])
        np.testing.assert_allclose(peaks, 4.0, atol=0.00001)

    def test_unconfigured_rate_preserves_geometric_sweep(self):
        removal = GeometricRemoval(stock_simulator())
        removal.cut()
        self.assertAlmostEqual(
            removal.summary()["remaining_peak_mm"], 0.1, places=5
        )
        self.assertIsNone(removal.summary()["rate_mm_s"])

    def test_cli_rejects_rates_that_could_grow_or_invalidate_stock(self):
        argument_parser = parser()
        for value in ("0", "-1", "nan", "inf"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    argument_parser.parse_args(["--removal-rate-mm-s", value])
        self.assertEqual(
            argument_parser.parse_args(
                ["--removal-rate-mm-s", "0.03375"]
            ).removal_rate_mm_s,
            0.03375,
        )


if __name__ == "__main__":
    unittest.main()
