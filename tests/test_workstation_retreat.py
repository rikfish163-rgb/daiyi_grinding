"""A real observed outer-wall path must retain a reachable complete retreat."""

import json
import tempfile
import unittest
from pathlib import Path

from daiyi_grinding.workstation_model import Workstation
from daiyi_grinding.workstation_planning import build_plan
from daiyi_grinding.workstation_tooling import FixedTool


class OuterWallContactBelt(FixedTool):
    """Restrict this regression to one existing belt contact, with full geometry."""

    def candidates(self):
        yield self.contact + [0, -0.025, 0]


class ObservedOuterWallRetreatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.sim = Workstation(
            Path(cls.directory.name),
            seed=9000,
            case="normal",
            camera_setup="low_side_simulation",
        )
        tool = cls.sim.tool
        cls.sim.tool = OuterWallContactBelt(
            tool.key,
            tool.label,
            tool.geom,
            tool.contact,
            tool.normal,
            tool.half_size,
            tool.source,
        )
        cls.path = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "observed_outer_wall_burr_2026112397.json"
            ).read_text()
        )["path"]

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()
        cls.directory.cleanup()

    def test_observed_outer_wall_finishes_processing_and_home(self):
        commands, admitted, check = build_plan(self.sim, [self.path])
        self.assertTrue(check["all_paths_passed"], check["rows"])
        self.assertEqual(admitted[0]["nominal_xyz"], self.path["nominal_xyz"])
        self.assertEqual(commands[-1]["phase"], "home")
        self.assertFalse(commands[-1]["attached"])
        self.assertTrue(
            any(command.get("event") == "release" for command in commands)
        )
        self.assertEqual(check["contact_count"], 0)
        self.assertEqual(self.path["material_allowance_mm"], 0.08)


if __name__ == "__main__":
    unittest.main()
