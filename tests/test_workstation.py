"""Current-cell regressions independent of generated defect annotations."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import mujoco
import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.perception import Detection
from daiyi_grinding.thin_reference import (
    THIN_REFERENCE_CONFIG,
    detect_thin_reference,
)
from daiyi_grinding.workstation_evaluation import binary_metrics
from daiyi_grinding.workstation_inspection import (
    Inspection,
    observed_normal_height_mm,
    observed_paths,
    surface_permission,
)
from daiyi_grinding.workstation_model import (
    GRIPPER_FROM_WORKPIECE,
    WORKPIECE_FROM_GRIPPER,
    Workstation,
    component_for_geom,
)
from daiyi_grinding.workstation_motion import (
    GeometricRemoval,
    clip_excess,
    joint_curve,
    workpiece_target,
)
from daiyi_grinding.workstation_tooling import fixed_tool


class CurrentPerceptionTests(unittest.TestCase):
    def test_oblique_subtolerance_signal_is_kept_without_machining_paths(self):
        """Optical depth residual must not become a normal stock allowance."""
        rows, cols = np.indices((24, 32))
        nominal = np.stack(
            [cols * 0.0005, rows * 0.0005, np.full(rows.shape, 0.3)],
            axis=-1,
        )
        normals = np.broadcast_to([0, 0, -1.0], nominal.shape).copy()
        pixels = np.argwhere((rows >= 10) & (rows <= 12) & (cols >= 8))
        observed = nominal.copy()
        # A grazing camera ray has 0.6 mm optical travel but only 0.24 mm
        # component along the measured reference surface normal.
        observed[tuple(pixels.T)] += np.array(
            [np.sqrt(0.0006**2 - 0.00024**2), 0, -0.00024]
        )
        detection = Detection(
            1, "burr", pixels, observed[tuple(pixels.T)], 12, 1.5, 0.6, 1, ""
        )
        mask = np.zeros(rows.shape, bool)
        mask[tuple(pixels.T)] = True
        observation = {
            "xyz": observed,
            "reference_xyz": nominal,
            "normals": normals,
            "mask": mask,
            "surface": np.ones(rows.shape, bool),
            "selection": np.ones(rows.shape, bool),
            "K": np.array([[800, 0, 16], [0, 800, 12], [0, 0, 1]]),
            "T": np.eye(4),
            "tool": fixed_tool("belt"),
        }
        before = mask.copy()
        self.assertFalse(observed_paths(detection, observation))
        self.assertAlmostEqual(
            observed_normal_height_mm(detection, observation), 0.24
        )
        np.testing.assert_array_equal(observation["mask"], before)
        self.assertEqual(detection.height_mm, 0.6)
        pixel_rows, pixel_cols = pixels.T
        observed[pixel_rows, pixel_cols, 2] = (
            nominal[pixel_rows, pixel_cols, 2] - 0.0006
        )
        self.assertTrue(observed_paths(detection, observation))
        observation["normals"][:] = np.nan
        self.assertIsNone(observed_normal_height_mm(detection, observation))
        self.assertFalse(observed_paths(detection, observation))

    def test_sloped_plane_keeps_a_ridge_but_guards_a_true_edge(self):
        shape = (64, 96)
        x = np.arange(shape[1])[None, :]
        reference = np.broadcast_to(1 / (1 + (x - 48) * 0.008), shape).copy()
        observed = reference.copy()
        observed[29:32, 20:70] -= 0.003
        k = np.array([[200, 0, 48], [0, 200, 32], [0, 0, 1]])
        arguments = (
            np.full((*shape, 3), 120, np.uint8),
            observed,
            reference,
            k,
            np.eye(4),
        )
        old, _ = detect_thin_reference(
            *arguments, inspection_bounds=([-2, -2, 0.1], [2, 2, 3])
        )
        new, observation = detect_thin_reference(
            *arguments,
            inspection_bounds=([-2, -2, 0.1], [2, 2, 3]),
            allow_sloped_surface=True,
        )
        self.assertFalse(old)
        self.assertTrue(new)
        self.assertGreater(np.count_nonzero(observation["mask"]), 100)
        reference[:, :15] += 0.05
        observed = reference.copy()
        observed[:, 14] -= 0.004
        detections, evidence = detect_thin_reference(
            arguments[0],
            observed,
            reference,
            k,
            np.eye(4),
            inspection_bounds=([-2, -2, 0.1], [2, 2, 3]),
            allow_sloped_surface=True,
        )
        self.assertFalse(detections)
        self.assertTrue(evidence["reference_discontinuity_guard"][:, 14].all())

    def test_metric_join_preserves_missing_pixels(self):
        shape = (64, 96)
        reference = np.full(shape, 0.5)
        observed = reference.copy()
        observed[30:32, 42:44] -= 0.0015
        observed[30:32, 45:47] -= 0.0015
        observed[30:32, 44] = np.nan
        k = np.array([[800, 0, 48], [0, 800, 32], [0, 0, 1]])
        config = replace(
            THIN_REFERENCE_CONFIG,
            minimum_pixels=6,
            component_join_mm=2,
            closing_pixels=0,
        )
        detections, _ = detect_thin_reference(
            np.full((*shape, 3), 120, np.uint8),
            observed,
            reference,
            k,
            np.eye(4),
            inspection_bounds=([-1, -1, 0.1], [1, 1, 1]),
            config=config,
        )
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].kind, "burr")
        self.assertEqual(len(detections[0].pixels), 8)
        self.assertFalse(
            np.isnan(observed[tuple(detections[0].pixels.T)]).any()
        )

    def test_permission_rejects_hidden_surface_at_same_image_coordinate(self):
        k = np.array([[100, 0, 5], [0, 100, 5], [0, 0, 1]])
        depth = np.ones((11, 11))
        permission = np.zeros(depth.shape, bool)
        permission[5, 5] = True
        points = np.array([[[0, 0, 1], [0, 0, 1.1], [0.01, 0, 1]]])
        allowed = surface_permission(points, k, np.eye(4), depth, permission)
        np.testing.assert_array_equal(allowed, [[True, False, False]])

    def test_metrics_keep_undefined_normal_case_precision(self):
        metrics = binary_metrics(np.zeros(10, bool), np.zeros(10, bool))
        self.assertIsNone(metrics["precision"])
        self.assertIsNone(metrics["recall"])
        self.assertEqual(metrics["tn"], 10)
        self.assertEqual(metrics["accuracy"], 1.0)
        metrics = binary_metrics([True, True, False], [True, False, True])
        self.assertEqual(metrics["tp"], 1)
        self.assertEqual(metrics["fp"], 1)
        self.assertEqual(metrics["fn"], 1)
        self.assertEqual(metrics["f1"], 0.5)


class CurrentStockTests(unittest.TestCase):
    def test_stock_replay_snapshots_are_shared_until_changed_and_remain_immutable(
        self,
    ):
        vertices = np.array([[0, 0, 0.003], [0, 0, 0.002]], dtype=np.float32)
        removal = GeometricRemoval.__new__(GeometricRemoval)
        removal.sim = SimpleNamespace(model=SimpleNamespace(mesh_vert=vertices))
        removal.parts = [{"mesh": 1, "address": 0, "count": 2}]
        original = removal.updates()[0]
        self.assertIs(removal.updates()[0], original)
        vertices[0, 2] = 0.0002
        clipped = removal.updates()[0]
        self.assertIsNot(clipped, original)
        self.assertAlmostEqual(original["positions"][0][2], 0.003)
        self.assertAlmostEqual(clipped["positions"][0][2], 0.0002)
        self.assertEqual(original["id"], clipped["id"])

    def test_normal_attached_stock_is_clipped_without_changing_nominal(self):
        tool = fixed_tool("belt")
        pose = np.eye(4)
        pose[:3, :3] = Rotation.from_euler("y", -90, degrees=True).as_matrix()
        pose[:3, 3] = tool.contact + [0.00015, 0, 0]
        base = np.array([[0, 0, 0], [0, 0.1, 0], [0, 0, 0]])
        normals = np.tile([0, 0, 1], (3, 1))
        vertices = base + normals * np.array([[0.002], [0.002], [0.0]])
        original_base = base.copy()
        clipped, changed = clip_excess(vertices, base, normals, pose, tool)
        np.testing.assert_array_equal(changed, [True, False, False])
        self.assertAlmostEqual(clipped[0, 2], 0.00015)
        np.testing.assert_array_equal(clipped[1:], vertices[1:])
        np.testing.assert_array_equal(base, original_base)

    def test_removal_depends_on_actual_pose(self):
        tool = fixed_tool("belt")
        base = np.zeros((1, 3))
        normals = np.array([[-1, 0, 0]])
        vertices = base + normals * 0.002
        pose = np.eye(4)
        pose[:3, 3] = tool.contact + [0.2, 0, 0]
        clipped, changed = clip_excess(vertices, base, normals, pose, tool)
        self.assertFalse(changed.any())
        np.testing.assert_array_equal(clipped, vertices)

    def test_joint_curve_supports_nonzero_scan_time(self):
        sim = SimpleNamespace(
            data=SimpleNamespace(qpos=np.zeros(6)), q_index=np.arange(6)
        )
        commands = [
            {"t": 1.0, "q": np.full(6, 0.1)},
            {"t": 2.0, "q": np.full(6, 0.2)},
            {"t": 3.0, "q": np.full(6, 0.2)},
        ]
        times, curve = joint_curve(sim, commands, 12.0)
        np.testing.assert_allclose(times, [12, 13, 14, 15])
        np.testing.assert_allclose(curve(13), commands[0]["q"])
        self.assertLess(np.max(np.abs(curve(14, 1))), 1e-12)
        self.assertTrue((curve(np.linspace(12, 15, 101)) >= 0).all())
        self.assertTrue((curve(np.linspace(12, 15, 101)) <= 0.2).all())


class CurrentCellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.sim = Workstation(
            Path(cls.directory.name), seed=9000, case="normal"
        )

    @classmethod
    def tearDownClass(cls):
        cls.sim.close()
        cls.directory.cleanup()

    def test_semantics_distinguish_base_casting_tool_and_fixture(self):
        self.assertEqual(component_for_geom("rob_base_0"), "robot_base")
        self.assertEqual(component_for_geom("wp_097_0"), "workpiece")
        self.assertEqual(component_for_geom("fix_board_0"), "fixture")
        self.assertEqual(component_for_geom("abrasive_belt"), "tool")
        self.assertEqual(component_for_geom("grip_board_0"), "gripper")

    def test_flange_collision_does_not_bridge_the_raised_cad_rim(self):
        sim = self.sim
        rotation = (
            Rotation.from_rotvec([-np.deg2rad(30), 0, 0])
            * Rotation.from_euler("y", -90, degrees=True)
        ).as_matrix()
        point = np.array([-0.205888, 0.089261, 0.361153])
        target = workpiece_target(
            point, rotation, 0.00008, sim.tool.contact + [0, 0.025, 0]
        )
        q, _, _ = sim.ik(target @ WORKPIECE_FROM_GRIPPER, sim.home)
        sim.set_scratch_pose(q, attached=True)
        tool_geom = sim.model.geom(sim.tool.geom).id
        penetrations = [
            contact.dist
            for contact in sim.scratch.contact
            if tool_geom in (contact.geom1, contact.geom2)
        ]
        self.assertGreater(min(penetrations, default=0), -0.00008)

    def test_fixed_camera_and_casting_follow_measured_encoder(self):
        sim = self.sim
        initial_camera = sim.camera_info(frame="world")[1].copy()
        before = sim.body_transform(sim.workpiece).copy()
        Inspection(sim, 9000).rotate(60)
        after = sim.body_transform(sim.workpiece)
        np.testing.assert_allclose(
            sim.camera_info(frame="world")[1], initial_camera, atol=1e-10
        )
        angle = np.rad2deg(
            Rotation.from_matrix(after[:3, :3] @ before[:3, :3].T).as_rotvec()[
                2
            ]
        )
        self.assertAlmostEqual(angle, 60, delta=0.1)
        self.assertAlmostEqual(
            sim.state()["turntable_angle_deg"], 60, delta=0.1
        )

    def test_desired_fk_does_not_teleport_integrated_joints(self):
        sim = self.sim
        q = sim.data.qpos.copy()
        sim.desired_gripper_pose(sim.home + [0.001, 0, 0, 0, 0, 0])
        np.testing.assert_array_equal(sim.data.qpos, q)

    def test_gravity_feedforward_does_not_cancel_free_body_gravity(self):
        sim = self.sim
        sim.apply_gravity_feedforward()
        address = sim.model.joint("workpiece_free").dofadr[0]
        np.testing.assert_array_equal(
            sim.data.qfrc_applied[address : address + 6], np.zeros(6)
        )
        sim.apply_motion_feedforward(np.full(6, 0.01))
        np.testing.assert_array_equal(
            sim.data.qfrc_applied[address : address + 6], np.zeros(6)
        )

    def test_acceleration_feedforward_tracks_short_motion_with_held_payload(
        self,
    ):
        """Compare integrated servos on identical smooth short elbow motions."""
        sim = self.sim
        saved_data = sim.data
        errors = {}
        try:
            for inverse_dynamics in (False, True):
                sim.data = mujoco.MjData(sim.model)
                sim.data.qpos[sim.q_index] = sim.home
                mujoco.mj_forward(sim.model, sim.data)
                pose = sim.body_transform(sim.gripper) @ GRIPPER_FROM_WORKPIECE
                sim.data.qpos[sim.wp_qadr : sim.wp_qadr + 7] = np.r_[
                    pose[:3, 3],
                    Rotation.from_matrix(pose[:3, :3]).as_quat()[[3, 0, 1, 2]],
                ]
                sim.data.eq_active[sim.fixture_weld] = 0
                sim.data.eq_active[sim.gripper_weld] = 1
                sim.data.ctrl[sim.arm_actuators] = sim.home
                mujoco.mj_forward(sim.model, sim.data)
                for _ in range(100):
                    sim.apply_gravity_feedforward()
                    mujoco.mj_step(sim.model, sim.data)
                    mujoco.mj_forward(sim.model, sim.data)
                kp = sim.model.actuator_gainprm[sim.arm_actuators, 0]
                kv = -sim.model.actuator_biasprm[sim.arm_actuators, 2]
                delta = np.array([0, 0.004, 0, 0, 0, 0])
                duration = 0.6
                frequency = 2 * np.pi / duration
                timestep = sim.model.opt.timestep
                tracking = []
                for step in range(round(duration / timestep)):
                    t = step * timestep
                    q = sim.home + delta * (1 - np.cos(frequency * t)) / 2
                    velocity = delta * frequency * np.sin(frequency * t) / 2
                    acceleration = (
                        delta * frequency**2 * np.cos(frequency * t) / 2
                        if inverse_dynamics
                        else np.zeros(6)
                    )
                    sim.data.ctrl[sim.arm_actuators] = (
                        q
                        + (kv + sim.model.dof_damping[sim.dof_index])
                        * velocity
                        / kp
                    )
                    sim.apply_motion_feedforward(acceleration)
                    mujoco.mj_step(sim.model, sim.data)
                    mujoco.mj_forward(sim.model, sim.data)
                    target_q = (
                        sim.home
                        + delta * (1 - np.cos(frequency * (t + timestep))) / 2
                    )
                    target = (
                        sim.desired_gripper_pose(target_q)
                        @ GRIPPER_FROM_WORKPIECE
                    )
                    tracking.append(
                        np.linalg.norm(
                            sim.data.xpos[sim.workpiece] - target[:3, 3]
                        )
                    )
                errors[inverse_dynamics] = float(np.quantile(tracking, 0.95))
        finally:
            sim.data = saved_data
        self.assertLess(errors[True], errors[False] * 0.5)
        self.assertLess(errors[True], 0.0001)


if __name__ == "__main__":
    unittest.main()
