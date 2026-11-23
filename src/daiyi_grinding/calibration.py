"""Image-derived eye-to-hand calibration, simulated FR5 FK only; no transport.

T_A_B maps column coordinates in frame B into frame A. The estimator accepts
images, known intrinsics, board dimensions and FK logs, never renderer poses.
"""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "egl")
import cv2
import mujoco
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from . import project, robot_model
from .geometry import generate

WIDTH, HEIGHT, FOVY = 768, 576, 26.0
PATTERN, SQUARE = (8, 5), 0.020


def transform(R=None, t=None):
    T = np.eye(4)
    if R is not None:
        T[:3, :3] = R
    if t is not None:
        T[:3, 3] = t
    return T


def inverse(T):
    return transform(T[:3, :3].T, -T[:3, :3].T @ T[:3, 3])


def board_points():
    x, y = np.meshgrid(np.arange(PATTERN[0]), np.arange(PATTERN[1]))
    return np.c_[
        (x.ravel() - (PATTERN[0] - 1) / 2) * SQUARE,
        (y.ravel() - (PATTERN[1] - 1) / 2) * SQUARE,
        np.zeros(x.size),
    ].astype(np.float64)


def known_intrinsics(width=WIDTH, height=HEIGHT):
    f = 0.5 * height / np.tan(np.deg2rad(FOVY) / 2)
    return np.array(
        [[f, 0, (width - 1) / 2], [0, f, (height - 1) / 2], [0, 0, 1.0]]
    )


def estimate_board_pose(rgb, K):
    """Detect actual rendered corners; red marker identifies board origin corner."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ok, corners = cv2.findChessboardCornersSB(
        gray, PATTERN, cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    )
    if not ok:
        raise ValueError("checkerboard_not_detected")
    red = (rgb[:, :, 0].astype(float) > 1.7 * rgb[:, :, 1]) & (
        rgb[:, :, 0] > 90
    )
    yy, xx = np.nonzero(red)
    if len(xx) < 12:
        raise ValueError("orientation_marker_not_detected")
    marker = np.array([np.mean(xx), np.mean(yy)])
    grid = corners.reshape(PATTERN[1], PATTERN[0], 2)
    # SB may start at any of four corners. Check row/column reversals, using
    # only the colored marker detected in the image, never projected truth.
    alternatives = [grid, grid[::-1], grid[:, ::-1], grid[::-1, ::-1]]
    chosen = min(alternatives, key=lambda g: np.linalg.norm(g[0, 0] - marker))
    corners = chosen.reshape(-1, 2).copy()
    ok, rvec, tvec = cv2.solvePnP(
        board_points(), corners, K, None, flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not ok or tvec[2, 0] <= 0:
        raise ValueError("pnp_failed_or_behind_camera")
    rvec, tvec = cv2.solvePnPRefineLM(
        board_points(), corners, K, None, rvec, tvec
    )
    projected, _ = cv2.projectPoints(board_points(), rvec, tvec, K, None)
    rms = float(
        np.sqrt(
            np.mean(np.sum((projected.reshape(-1, 2) - corners) ** 2, axis=1))
        )
    )
    return transform(cv2.Rodrigues(rvec)[0], tvec.ravel()), corners, rms


def solve_eye_to_hand(T_base_grippers, T_camera_targets):
    """OpenCV's eye-to-hand substitution returns T_base_camera.

    inverse(T_base_gripper) @ T_base_camera @ T_camera_target = constant
    T_gripper_target. This is the documented AX=XB convention.
    """
    if len(T_base_grippers) < 6:
        raise ValueError("need_at_least_six_diverse_poses")
    poses = [inverse(T) for T in T_base_grippers]
    R, t = cv2.calibrateHandEye(
        [T[:3, :3] for T in poses],
        [T[:3, 3] for T in poses],
        [T[:3, :3] for T in T_camera_targets],
        [T[:3, 3] for T in T_camera_targets],
        method=cv2.CALIB_HAND_EYE_PARK,
    )
    T_base_camera = transform(R, t.ravel())
    if not np.isfinite(T_base_camera).all():
        raise ValueError("hand_eye_not_finite")
    mounts = [
        inverse(g) @ T_base_camera @ c
        for g, c in zip(T_base_grippers, T_camera_targets)
    ]
    T_gripper_target = transform(
        Rotation.from_matrix(np.array([T[:3, :3] for T in mounts]))
        .mean()
        .as_matrix(),
        np.mean([T[:3, 3] for T in mounts], axis=0),
    )
    return T_base_camera, T_gripper_target


class CalibrationRenderer:
    """Independent scene with real FR5 joint frames and a visible mocap board.

    Joint poses are ideal, encoder/FK error is not simulated. No joints are
    dynamically executed here; the full demo separately tests FR5 dynamics.
    """

    def __init__(self, camera_xy):
        root = ET.Element("mujoco", model="FR5 image calibration fixture")
        robot_model.sub(root, "compiler", angle="radian")
        visual = robot_model.sub(root, "visual")
        robot_model.sub(visual, "global", offwidth=WIDTH, offheight=HEIGHT)
        robot_model.sub(visual, "quality", offsamples="4")
        robot_model.sub(
            visual,
            "headlight",
            ambient=".7 .7 .7",
            diffuse=".3 .3 .3",
            specular="0 0 0",
        )
        world = robot_model.sub(root, "worldbody")
        robot_model.sub(
            world,
            "camera",
            name="inspection",
            pos=robot_model.s([*camera_xy, 1.13]),
            xyaxes="1 0 0 0 1 0",
            fovy=FOVY,
        )
        robot_model.sub(
            world,
            "geom",
            type="plane",
            size="2 2 .01",
            pos="0 0 .55",
            rgba=".4 .4 .4 1",
        )
        base = robot_model.sub(world, "body", name="base_link", pos="0 0 .72")
        for i, (name, pos, rpy, limits) in enumerate(robot_model.JOINTS):
            base = robot_model.sub(
                base,
                "body",
                name=f"link_{i}",
                pos=robot_model.s(pos),
                euler=robot_model.s(rpy),
            )
            robot_model.sub(
                base,
                "joint",
                name=name,
                axis="0 0 1",
                range=robot_model.s(limits),
            )
            robot_model.sub(
                base,
                "inertial",
                mass="1",
                pos="0 0 0",
                diaginertia=".01 .01 .01",
            )
        robot_model.sub(
            base,
            "site",
            name="tcp",
            pos="0 0 .166",
            size=".001",
            rgba="0 0 0 0",
        )
        board = robot_model.sub(world, "body", name="board", mocap="true")
        robot_model.sub(
            board,
            "geom",
            type="box",
            size=".105 .080 .001",
            pos="0 0 -.0015",
            rgba="1 1 1 1",
        )
        for row in range(PATTERN[1] + 1):
            for col in range(PATTERN[0] + 1):
                robot_model.sub(
                    board,
                    "geom",
                    type="box",
                    size=robot_model.s([SQUARE / 2, SQUARE / 2, 0.0005]),
                    pos=robot_model.s(
                        [
                            (col - PATTERN[0] / 2) * SQUARE,
                            (row - PATTERN[1] / 2) * SQUARE,
                            -0.0005,
                        ]
                    ),
                    rgba=".015 .015 .015 1"
                    if (row + col) % 2 == 0
                    else "1 1 1 1",
                )
        robot_model.sub(
            board,
            "geom",
            type="box",
            size=".006 .006 .0005",
            pos="-.094 -.069 -.0005",
            rgba="1 .02 .02 1",
        )
        self.xml = ET.tostring(root, encoding="unicode")
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self.data = mujoco.MjData(self.model)
        self.site = self.model.site("tcp").id
        self.renderer = mujoco.Renderer(self.model, height=HEIGHT, width=WIDTH)
        self.lower = self.model.jnt_range[:, 0] + 0.02
        self.upper = self.model.jnt_range[:, 1] - 0.02

    def fk(self, q):
        self.data.qpos[:] = q
        mujoco.mj_forward(self.model, self.data)
        return transform(
            self.data.site_xmat[self.site].reshape(3, 3).copy(),
            self.data.site_xpos[self.site].copy(),
        )

    def ik(self, desired, seed):
        def residual(q):
            actual = self.fk(q)
            return np.r_[
                10 * (actual[:3, 3] - desired[:3, 3]),
                Rotation.from_matrix(
                    desired[:3, :3] @ actual[:3, :3].T
                ).as_rotvec(),
            ]

        sol = least_squares(
            residual,
            np.clip(seed, self.lower, self.upper),
            bounds=(self.lower, self.upper),
            max_nfev=180,
            ftol=1e-10,
            xtol=1e-10,
            gtol=1e-10,
        )
        error = residual(sol.x)
        if (
            np.linalg.norm(error[:3]) > 0.001
            or np.linalg.norm(error[3:]) > 0.002
        ):
            raise ValueError("fr5_ik_pose_unreachable")
        return sol.x.copy(), self.fk(sol.x)

    def render(self, T_world_target):
        self.data.mocap_pos[0] = T_world_target[:3, 3]
        xyzw = Rotation.from_matrix(T_world_target[:3, :3]).as_quat()
        self.data.mocap_quat[0] = xyzw[[3, 0, 1, 2]]
        mujoco.mj_forward(self.model, self.data)
        self.renderer.update_scene(self.data, camera="inspection")
        return self.renderer.render().copy()

    def close(self):
        self.renderer.close()


def pose_error(estimated, truth):
    return dict(
        translation_mm=float(
            np.linalg.norm(estimated[:3, 3] - truth[:3, 3]) * 1000
        ),
        rotation_deg=float(
            np.rad2deg(
                Rotation.from_matrix(
                    estimated[:3, :3] @ truth[:3, :3].T
                ).magnitude()
            )
        ),
    )


def run(output, seed=7, poses=30):
    if Path(output).exists() and any(Path(output).iterdir()):
        raise ValueError(
            "Output is not empty; choose a new calibration directory"
        )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    image_dir = output / "images"
    image_dir.mkdir(exist_ok=True)
    # The inspection station is fixed at the original seed-7 placement;
    # --seed varies acquisition poses, not the physical camera installation.
    coupon = generate(7)
    renderer = CalibrationRenderer(coupon.center[:2])
    (output / "scene.xml").write_text(renderer.xml)
    K = known_intrinsics()
    T_world_base = transform(t=[0, 0, 0.72])
    # Private fixture truth is ONLY used to build the rendered scene and score.
    true_mount = transform(np.diag([1.0, -1.0, -1.0]), [0.012, -0.008, 0.045])
    true_camera = transform(
        np.diag([1.0, -1.0, -1.0]), [*coupon.center[:2], 1.13]
    )
    rng = np.random.default_rng(seed + 193)
    records, failures, observed, fks = [], [], [], []
    qseed = np.array([0.0, -1.1, 1.6, -2.1, -1.57, 0.0])
    try:
        for i in range(poses):
            angles = rng.uniform([-22, -22, -35], [22, 22, 35])
            board = transform(
                Rotation.from_euler("xyz", angles, degrees=True).as_matrix(),
                [
                    coupon.center[0] + rng.uniform(-0.028, 0.028),
                    coupon.center[1] + rng.uniform(-0.028, 0.028),
                    rng.uniform(0.65, 0.72),
                ],
            )
            desired_gripper = board @ inverse(true_mount)
            try:
                qseed, actual_gripper = renderer.ik(desired_gripper, qseed)
                rgb = renderer.render(actual_gripper @ true_mount)
                cv2.imwrite(
                    str(image_dir / f"pose_{i:02d}_rgb.png"),
                    cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                )
                camera_target, corners, rms = estimate_board_pose(rgb, K)
                overlay = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                cv2.drawChessboardCorners(
                    overlay,
                    PATTERN,
                    corners.astype(np.float32).reshape(-1, 1, 2),
                    True,
                )
                cv2.imwrite(
                    str(image_dir / f"pose_{i:02d}_corners.png"), overlay
                )
                base_gripper = inverse(T_world_base) @ actual_gripper
                observed.append(camera_target)
                fks.append(base_gripper)
                records.append(
                    dict(
                        index=i,
                        split="holdout" if i % 5 == 0 else "train",
                        q_rad=qseed.tolist(),
                        T_base_gripper=base_gripper.tolist(),
                        T_camera_target=camera_target.tolist(),
                        corners_px=corners.tolist(),
                        reprojection_rms_px=rms,
                        commanded_xyz_angles_deg=angles.tolist(),
                    )
                )
            except ValueError as error:
                failures.append(dict(index=i, reason=str(error)))
        training = [
            j for j, record in enumerate(records) if record["split"] == "train"
        ]
        holdout = [
            j
            for j, record in enumerate(records)
            if record["split"] == "holdout"
        ]
        if len(holdout) < 3:
            raise ValueError("fewer_than_three_detected_holdout_poses")
        base_camera, mount = solve_eye_to_hand(
            [fks[j] for j in training], [observed[j] for j in training]
        )
        world_camera = T_world_base @ base_camera
        errors = []
        for j in holdout:
            # Held-out PnP camera coordinates mapped using the estimated camera;
            # reference points use independently recorded FK and physical mount.
            predicted = (
                board_points() @ observed[j][:3, :3].T + observed[j][:3, 3]
            )
            predicted = predicted @ base_camera[:3, :3].T + base_camera[:3, 3]
            reference_T = fks[j] @ true_mount
            reference = (
                board_points() @ reference_T[:3, :3].T + reference_T[:3, 3]
            )
            distances = np.linalg.norm(predicted - reference, axis=1) * 1000
            errors.append(
                dict(
                    index=records[j]["index"],
                    point_rms_mm=float(np.sqrt(np.mean(distances**2))),
                    point_max_mm=float(distances.max()),
                    closure_error=pose_error(
                        inverse(fks[j]) @ base_camera @ observed[j], mount
                    ),
                )
            )
        angles = np.array([r["commanded_xyz_angles_deg"] for r in records])
        report = dict(
            schema_version=1,
            simulation_only=True,
            demo_seed=7,
            acquisition_seed=seed,
            camera="inspection",
            frame_convention="T_A_B maps B column coordinates to A; meters; OpenCV camera +X right +Y down +Z forward",
            width=WIDTH,
            height=HEIGHT,
            fovy_deg=FOVY,
            K=K.tolist(),
            distortion_coefficients=[0] * 5,
            intrinsics_source="known configured MuJoCo pinhole FOV; intrinsics are not estimated",
            robot_pose_source="ideal FR5 FK using scene.JOINTS; no encoder or mechanical errors",
            board_inner_corners=list(PATTERN),
            square_m=SQUARE,
            T_world_base=T_world_base.tolist(),
            T_base_camera=base_camera.tolist(),
            T_world_camera=world_camera.tolist(),
            T_gripper_target_estimated=mount.tolist(),
            camera_configuration=dict(
                position_world_m=[*coupon.center[:2].tolist(), 1.13],
                xyaxes=[1, 0, 0, 0, 1, 0],
            ),
            estimator="detected checkerboard+RGB orientation marker -> PnP -> OpenCV PARK eye-to-hand",
            successful_views=len(records),
            training_views=len(training),
            holdout_views=len(holdout),
            failures=failures,
            multiaxis_rotation_span_deg=np.ptp(angles, axis=0).tolist(),
            camera_truth_error=pose_error(world_camera, true_camera),
            mount_truth_error=pose_error(mount, true_mount),
            reprojection_mean_rms_px=float(
                np.mean([r["reprojection_rms_px"] for r in records])
            ),
            holdout=errors,
            holdout_worst_point_mm=max(e["point_max_mm"] for e in errors),
            truth_usage="renderer fixture placement and evaluation only; not passed to corner detector, PnP or hand-eye solver",
            acceptance_passed=False,
        )
        report["acceptance_passed"] = (
            report["camera_truth_error"]["translation_mm"] < 2
            and report["camera_truth_error"]["rotation_deg"] < 0.3
            and report["holdout_worst_point_mm"] < 2
        )
        (output / "calibration.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        (output / "observations.json").write_text(
            json.dumps(records, indent=2) + "\n"
        )
        return report
    finally:
        renderer.close()


def load_inspection_calibration(
    path, width=WIDTH, height=HEIGHT, camera_position=None
):
    """Load matched successful calibration; reject a different camera placement."""
    report = json.loads(Path(path).read_text())
    if (
        not report.get("acceptance_passed")
        or report.get("camera") != "inspection"
    ):
        raise ValueError(
            "calibration failed acceptance or names a different camera"
        )
    if (width, height) != (report["width"], report["height"]):
        raise ValueError("calibration image dimensions differ")
    if camera_position is not None and not np.allclose(
        camera_position,
        report["camera_configuration"]["position_world_m"],
        atol=1e-9,
        rtol=0,
    ):
        raise ValueError(
            "camera placement differs; rerun calibration for this camera station"
        )
    return np.asarray(report["K"]), np.asarray(report["T_world_camera"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default=str(project.RUNS / "calibration_new")
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--poses", type=int, default=30)
    args = parser.parse_args()
    report = run(args.output, args.seed, args.poses)
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    "successful_views",
                    "failures",
                    "camera_truth_error",
                    "reprojection_mean_rms_px",
                    "holdout_worst_point_mm",
                    "acceptance_passed",
                ]
            },
            indent=2,
        )
    )
    if not report["acceptance_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
