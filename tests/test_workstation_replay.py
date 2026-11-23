"""Lossless stock state at arbitrary replay frames, including skipped updates."""

import json
import unittest

from daiyi_grinding.workstation_replay import ReplayRecorder, compact_frames


class ReplayExportTests(unittest.TestCase):
    def test_skipping_frames_restores_exact_pose_and_cumulative_stock(self):
        def frame(time, updates=None):
            value = {
                "simulation_time_s": time,
                "bodies": [[time, 0, 1, 1, 0, 0, 0]],
                "joint_angles_deg": [time] * 6,
                "phase": "trace" if time < 4 else "scan",
            }
            if updates is not None:
                value["mesh_updates"] = updates
            return value

        mesh_a = {"id": 1, "positions": [[0, 0, 0.003]]}
        mesh_b = {"id": 2, "positions": [[0, 0, 0.002]]}
        clipped_a = {"id": 1, "positions": [[0, 0, 0.000241981]]}
        frames = [
            frame(0),
            frame(1, [mesh_a, mesh_b]),
            frame(2, [mesh_a, mesh_b]),
            frame(3, [clipped_a]),
            frame(4),
        ]
        compact = compact_frames({"frames": frames, "source": "feedback"})
        self.assertEqual(len(compact["frames"]), len(frames))
        self.assertEqual(compact["stock_snapshots"], 2)
        for recorded, exported in zip(frames, compact["frames"]):
            for key in (
                "simulation_time_s",
                "bodies",
                "joint_angles_deg",
                "phase",
            ):
                self.assertEqual(exported[key], recorded[key])
        # Jump directly from the initial frame to post-machining inspection.
        last = compact["frames"][-1]
        stock = compact["mesh_states"][last["mesh_revision"]]
        self.assertEqual(stock, [clipped_a, mesh_b])
        self.assertEqual(frames[3]["mesh_updates"], [clipped_a])
        self.assertFalse(compact["body_poses_quantized"])

    def test_scan_without_material_updates_needs_no_stock_snapshot(self):
        original = {"frames": [{"phase": "scan", "bodies": [[1, 2, 3]]}]}
        compact = compact_frames(original)
        self.assertEqual(compact["mesh_states"], [[]])
        self.assertEqual(compact["frames"][0]["mesh_revision"], 0)
        self.assertEqual(compact["frames"][0]["bodies"], [[1, 2, 3]])

    def test_long_stationary_stock_record_keeps_all_feedback_without_mesh_copies(
        self,
    ):
        recording = ReplayRecorder()
        mesh = {"id": 3, "positions": [[0, 0, 0.002] for _ in range(6000)]}
        for index in range(5000):
            recording.append(
                {
                    "simulation_time_s": index / 10,
                    "bodies": [[index / 10, 0, 1, 1, 0, 0, 0]],
                    "mesh_updates": [mesh],
                }
            )
        document = recording.document(source="feedback")
        self.assertEqual(len(document["frames"]), 5000)
        self.assertEqual(document["stock_snapshots"], 1)
        self.assertIs(document["mesh_states"][1][0], mesh)
        self.assertEqual(
            document["frames"][-1]["bodies"], [[499.9, 0, 1, 1, 0, 0, 0]]
        )
        self.assertEqual(document["frames"][-1]["mesh_revision"], 1)
        self.assertLess(len(json.dumps(document)), 2_000_000)
        self.assertEqual(document["frames_dropped"], 0)

    def test_already_compact_record_preserves_post_cut_stock_on_export(self):
        recording = ReplayRecorder()
        recording.append(
            {
                "phase": "trace",
                "mesh_updates": [{"id": 1, "positions": [[0, 0, 0.002]]}],
            }
        )
        recording.append(
            {
                "phase": "retract",
                "mesh_updates": [{"id": 1, "positions": [[0, 0, 0.0002]]}],
            }
        )
        recording.append({"phase": "scan", "bodies": [[1, 2, 3]]})
        original = recording.document(source="integrated feedback")
        exported = compact_frames(original)
        self.assertEqual(exported, original)
        last = exported["frames"][-1]
        self.assertEqual(
            exported["mesh_states"][last["mesh_revision"]],
            [{"id": 1, "positions": [[0, 0, 0.0002]]}],
        )


if __name__ == "__main__":
    unittest.main()
