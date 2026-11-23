"""Regression tests for ROI permission boundaries, using synthetic RGB-D only.

Run: .venv/bin/python -m unittest discover -s tests -p test_roi.py -v
No MuJoCo scene, robot transport, or rendered ground truth is needed.
"""

import unittest

import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates

from daiyi_grinding.perception import (
    Detection,
    detect,
    project,
    roi_mask,
    unproject,
)
from daiyi_grinding.planning import plan


class RoiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.shape = (240, 320)
        self.K = np.array(
            [[1010.0, 0.0, 159.5], [0.0, 1010.0, 119.5], [0.0, 0.0, 1.0]]
        )
        self.T = np.eye(4)
        self.T[:3, :3] = np.diag([1.0, -1.0, -1.0])
        self.T[:3, 3] = [-0.49, -0.13, 1.13]
        self.depth = np.full(self.shape, 0.505, np.float32)
        self.scale = 0.505 / 1010
        self.rgb = np.full((*self.shape, 3), 160, np.uint8)

    def seam_fixture(self, roi=None, rows=(118, 123), cols=(40, 281)):
        selection = roi_mask(self.shape, roi)
        depth = self.depth.copy()
        depth[rows[0] : rows[1], cols[0] : cols[1]] = 0.503
        xyz = unproject(depth, self.K, self.T)
        pixels = np.array(
            [
                (r, c)
                for r in range(*rows)
                for c in range(*cols)
                if selection[r, c]
            ],
            dtype=int,
        )
        detection = Detection(
            1,
            "seam",
            pixels,
            xyz[pixels[:, 0], pixels[:, 1]],
            120.0,
            2.5,
            2.0,
            0.9,
            "synthetic measured seam",
        )
        observation = dict(
            xyz=xyz,
            selection=selection,
            K=self.K,
            T=self.T,
            scale=self.scale,
            mask=selection.copy(),
        )
        return detection, observation

    def assert_trace_inside_with_tool_margin(self, paths, selection):
        """Check the continuous polyline independently at 0.05 mm spacing.

        EDT is bilinearly sampled at floating pixel positions. Half a pixel
        accounts for discrete boundary rasterization, not a physical margin.
        The requested 4 mm includes the 3 mm tool radius plus 1 mm allowance.
        """
        distances = distance_transform_edt(np.pad(selection, 1))[1:-1, 1:-1]
        for path in paths:
            points = np.asarray(path["xyz"], float)
            self.assertGreaterEqual(len(points), 2)
            samples = []
            for start, end in zip(points, points[1:]):
                n = max(1, int(np.ceil(np.linalg.norm(end - start) / 0.00005)))
                samples.append(
                    start + (end - start) * np.linspace(0, 1, n + 1)[:, None]
                )
            pixels = project(np.concatenate(samples), self.K, self.T)
            self.assertTrue(np.isfinite(pixels).all())
            self.assertTrue(
                np.all(
                    (pixels[:, 0] >= 0) & (pixels[:, 0] <= self.shape[1] - 1)
                )
            )
            self.assertTrue(
                np.all(
                    (pixels[:, 1] >= 0) & (pixels[:, 1] <= self.shape[0] - 1)
                )
            )
            clearance_px = map_coordinates(
                distances,
                [pixels[:, 1], pixels[:, 0]],
                order=1,
                mode="constant",
                cval=0.0,
            )
            self.assertGreaterEqual(
                float(clearance_px.min()) * self.scale,
                0.004 - self.scale / 2,
                "trace segment/tool envelope crosses the permitted ROI boundary",
            )

    def test_c_shaped_roi_cannot_average_trace_into_hole(self):
        # This exact narrow C-shaped selection previously returned 18 trace
        # vertices, 17 of them in the unselected central row. The tool cannot
        # fit in these arms; safely returning no path is allowed.
        roi = [
            [50, 97],
            [101, 97],
            [101, 99],
            [52, 99],
            [52, 101],
            [101, 101],
            [101, 103],
            [50, 103],
        ]
        detection, observation = self.seam_fixture(
            roi, rows=(98, 103), cols=(50, 101)
        )
        paths = plan([detection], observation, clearance_mm=3.0)
        self.assert_trace_inside_with_tool_margin(
            paths, observation["selection"]
        )

    def test_concave_notch_splits_continuous_trace(self):
        # A narrow notch is smaller than the old 4 mm gap splitting threshold.
        # Both large sides admit the ball; joining them crosses forbidden space.
        roi = [
            [20, 60],
            [150, 60],
            [150, 132],
            [155, 132],
            [155, 60],
            [299, 60],
            [299, 180],
            [20, 180],
        ]
        detection, observation = self.seam_fixture(roi)
        paths = plan([detection], observation, clearance_mm=3.0)
        self.assertGreaterEqual(
            len(paths),
            2,
            "large valid sides should remain separate trace paths",
        )
        self.assert_trace_inside_with_tool_margin(
            paths, observation["selection"]
        )

    def test_large_roi_keeps_usable_trace_and_four_mm_margin(self):
        roi = [[20, 70], [299, 70], [299, 170], [20, 170]]
        detection, observation = self.seam_fixture(roi)
        paths = plan([detection], observation, clearance_mm=3.0)
        self.assertTrue(
            paths, "ROI filtering must retain a usable interior trace"
        )
        self.assert_trace_inside_with_tool_margin(
            paths, observation["selection"]
        )

    def test_no_roi_still_plans_nonempty_trace(self):
        detection, observation = self.seam_fixture()
        paths = plan([detection], observation, clearance_mm=3.0)
        self.assertTrue(paths)
        self.assertGreater(sum(len(path["xyz"]) for path in paths), 10)
        self.assert_trace_inside_with_tool_margin(
            paths, observation["selection"]
        )

    def test_roi_fragment_of_normal_rib_stays_preserve(self):
        depth = self.depth.copy()
        # Broad 8 mm by 95 mm design rib; a 6 mm crop would look burr-like
        # if classification were incorrectly recomputed after ROI cropping.
        depth[100:116, 65:255] = 0.503
        full, _ = detect(self.rgb, depth, self.K, self.T)
        ribs = [d for d in full if d.length_mm > 50 and d.kind == "preserve"]
        self.assertEqual(
            len(ribs), 1, "synthetic normal rib must be detected as preserve"
        )
        roi = [[140, 101], [152, 101], [152, 113], [140, 113]]
        cropped, observation = detect(self.rgb, depth, self.K, self.T, roi=roi)
        self.assertTrue(cropped, "test must actually select some rib pixels")
        self.assertTrue(all(d.kind == "preserve" for d in cropped))
        self.assertTrue(all(d.id == ribs[0].id for d in cropped))
        self.assertLess(
            sum(len(d.pixels) for d in cropped), len(ribs[0].pixels)
        )
        self.assertFalse(
            plan(cropped, observation, clearance_mm=3.0),
            "normal design rib fragments must never receive abrasive trajectories",
        )


if __name__ == "__main__":
    unittest.main()
