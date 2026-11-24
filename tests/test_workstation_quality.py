import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from daiyi_grinding.serve import validate
from daiyi_grinding.workstation_delivery import site_acceptance
from daiyi_grinding.workstation_model import grasp_frames
from daiyi_grinding.workstation_quality import Reinspection, add_targets
from daiyi_grinding.workstation_transfers import continuous_joins, splice


class ReinspectionTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "id": 1,
            "kind": "seam",
            "points": np.array([[0, 0, 1.0]]),
            "normals": np.array([[0, 0, -1.0]]),
        }

    def evidence(self, height_mm, *, visible=True):
        return {
            "T": np.eye(4),
            "K": np.eye(3),
            "surface": np.full((2, 2), visible),
            "selection": np.ones((2, 2), dtype=bool),
            "reference_xyz": np.tile([0, 0, 1.0], (2, 2, 1)),
            "xyz": np.tile([0, 0, 1 - height_mm / 1000], (2, 2, 1)),
            "noise_sigma_mm": 0.025,
        }

    def test_disappearing_detection_does_not_dispatch_known_excess(self):
        tracker = Reinspection([self.target])
        tracker.observe(self.evidence(0.46), 0)
        result = tracker.report(0.30)
        self.assertFalse(result["observational_release"])
        self.assertEqual(result["needs_rework"], 1)

    def test_missing_or_uncertain_measurement_requires_review(self):
        tracker = Reinspection([self.target])
        tracker.observe(self.evidence(0, visible=False), 0)
        self.assertEqual(tracker.report(0.30)["insufficient"], 1)
        tracker.observe(self.evidence(0.28), 1)
        result = tracker.report(0.30)
        self.assertEqual(
            result["regions"][0]["status"], "uncertain_measurement"
        )
        self.assertFalse(result["observational_release"])

    def test_low_residual_requires_full_scope_for_clearance(self):
        tracker = Reinspection([self.target])
        tracker.observe(self.evidence(0.10), 0)
        self.assertTrue(tracker.report(0.30)["observational_release"])
        self.assertFalse(
            tracker.report(0.30, whole_workpiece_scan=False)[
                "observational_release"
            ]
        )

    def test_depth_axis_uncertainty_projects_onto_oblique_normal(self):
        target = {
            **self.target,
            "normals": np.array([[0.8, 0, -0.6]]),
        }
        tracker = Reinspection([target])
        tracker.observe(self.evidence(0.35), 0)
        row = tracker.report(0.30)["regions"][0]
        self.assertAlmostEqual(row["peak_mm"], 0.21)
        np.testing.assert_allclose(row["anchor_bounds_mm"], [[0.18, 0.24]])

    def test_repeat_mean_sigma_is_not_reduced_twice(self):
        evidence = {
            **self.evidence(0.24),
            "noise_sigma_mm": 0.02,
            "depth_sample_counts": np.full((2, 2), 9, dtype=int),
            "depth_frame_count": 9,
        }
        tracker = Reinspection([self.target])
        tracker.observe(evidence, 0)
        row = tracker.report(0.30)["regions"][0]
        np.testing.assert_allclose(row["anchor_bounds_mm"], [[0.20, 0.28]])
        self.assertEqual(row["anchor_positive_observation_counts"], [9])
        self.assertEqual(row["anchor_observation_counts"], [9])

    def test_high_point_confirmation_uses_its_own_repeat_view(self):
        tracker = Reinspection([self.target])
        repeated = {
            **self.evidence(0.05),
            "noise_sigma_mm": 0.02,
            "depth_sample_counts": np.full((2, 2), 9, dtype=int),
            "depth_frame_count": 9,
        }
        tracker.observe(repeated, 0)
        tracker.observe(repeated, 0)
        tracker.observe(self.evidence(0.42), 1)
        row = tracker.report(0.30)["regions"][0]
        np.testing.assert_allclose(row["anchor_bounds_mm"], [[0.01, 0.47]])
        np.testing.assert_allclose(row["anchor_positive_lower_mm"], [0.37])
        np.testing.assert_allclose(row["anchor_positive_estimates_mm"], [0.42])
        self.assertEqual(row["anchor_observation_counts"], [10])
        self.assertEqual(row["anchor_positive_observation_counts"], [1])
        self.assertEqual(row["anchor_positive_view_indices"], [1])

        tracker.observe(self.evidence(-0.36), 2)
        report = tracker.report(0.30)
        row = report["regions"][0]
        self.assertTrue(row["overgrind"])
        self.assertFalse(report["automatic_regrind_allowed"])
        self.assertAlmostEqual(row["lower_peak_mm"], -0.41)
        self.assertAlmostEqual(row["anchor_positive_lower_mm"][0], 0.37)

    def test_zero_raw_samples_do_not_supply_an_anchor_measurement(self):
        tracker = Reinspection([self.target])
        tracker.observe(
            {
                **self.evidence(0.05),
                "depth_sample_counts": np.zeros((2, 2), dtype=int),
                "depth_frame_count": 9,
            },
            0,
        )
        report = tracker.report(0.30)
        row = report["regions"][0]
        self.assertEqual(report["insufficient"], 1)
        self.assertEqual(row["anchor_observation_counts"], [0])
        self.assertEqual(row["anchor_positive_lower_mm"], [None])
        self.assertFalse(report["observational_release"])

    def test_new_regions_keep_stable_ids_and_skip_existing_support(self):
        added = {**self.target, "points": np.array([[0.010, 0, 1]])}
        rows = add_targets([self.target], [self.target, added])
        self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_supplementary_view_faces_missing_anchors(self):
        tracker = Reinspection(
            [
                {
                    **self.target,
                    "points": np.zeros((1, 3)),
                    "normals": np.array([[0, -1.0, 0]]),
                }
            ]
        )
        tracker.last_camera = np.eye(4)
        tracker.last_camera[0, 3] = 1.0
        self.assertEqual(tracker.supplementary_angle(300, (315, 330, 345)), 315)
        tracker.upper[0][0] = 0.1
        self.assertIsNone(tracker.supplementary_angle(300, (315, 330, 345)))


class IntegrationContractTests(unittest.TestCase):
    def test_known_preset_is_resolved_by_normal_web_entry(self):
        job = validate(
            {
                "workpiece_profile": "097_thin_seam",
                "case": "burr",
                "targeted_rescan": True,
                "seed": 120481,
            }
        )
        self.assertEqual(job["case"], "thin")
        self.assertEqual(job["detector"], "geometric")
        self.assertTrue(job["targeted_rescan"])
        self.assertEqual(job["seed"], 120481)
        with self.assertRaises(ValueError):
            validate({"workpiece_profile": "unregistered_part"})
        with self.assertRaises(ValueError):
            validate({"grasp_setup": "unreviewed_hardware"})

    def test_opposite_clocking_retains_original_robot_loading_rotation(self):
        original, before = grasp_frames("original")
        opposite, after = grasp_frames("opposite_simulation")
        np.testing.assert_allclose(
            before @ np.linalg.inv(original)[:3],
            after @ np.linalg.inv(opposite)[:3],
            atol=1e-10,
        )

    def test_trace_join_preserves_endpoints_and_fails_on_disjoint_motion(self):
        phases = ("trace", "retract", "return", "transfer", "approach", "trace")
        commands = [
            {
                "phase": phase,
                "q": np.zeros(6),
                "point": np.zeros(3),
                "attached": True,
                "detection_id": 1,
                "segment_id": 1 if index < 3 else 2,
                "t": index + 1.0,
            }
            for index, phase in enumerate(phases)
        ]
        self.assertEqual(list(continuous_joins(commands)), [(0, 4, 1)])
        out = splice(commands, {0: (4, [])})
        self.assertEqual([x["t"] for x in out], [1, 2])
        commands[4]["q"] = np.ones(6)
        self.assertEqual(list(continuous_joins(commands)), [])

    def test_independent_measurement_includes_overcut_and_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            pasta = Path(directory)
            csv_path = pasta / "paired.csv"
            csv_path.write_text(
                "serial,zone,point_id,before_mm,after_mm,uncertainty_mm\n"
                "a1,ground,g1,0.8,0.1,0.02\n"
                "a1,protected,p1,0,-0.29,0.02\n"
            )
            manifest = {
                "source_kind": "real_measurement",
                "part_id": "097",
                "serial": "a1",
                "cad_revision": "cad-1",
                "fixture_revision": "fixture-1",
                "measurement_system": "measurement-1",
                "controller": "ctrl-1",
                "reviewer": "test",
                "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
                "measurements_file": csv_path.name,
                "measurements_sha256": hashlib.sha256(
                    csv_path.read_bytes()
                ).hexdigest(),
                "required_zones": ["ground", "protected", "side"],
            }
            path = pasta / "manifest.json"
            path.write_text(json.dumps(manifest))
            report = site_acceptance(path)
            self.assertFalse(report["surface_measurement_passed"])
            self.assertEqual(report["missing_zones"], ["side"])
            self.assertEqual(
                report["out_of_tolerance"],
                [{"zone": "protected", "point_id": "p1"}],
            )
