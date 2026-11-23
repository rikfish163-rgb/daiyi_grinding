"""Offline registration stress cases and an explicit synthetic error budget.

The suite never controls hardware or imports a real scan. Lengths inside this
module are metres unless a name or field explicitly ends in ``_mm``. A manifest
transform named ``T_workpiece_scan_m`` maps scan-frame column vectors into the
097 workpiece frame.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .cad_defects import DEFAULT_MESH
from .casting_scan_calibration import closest_surface, load_cloud
from .project import RUNS
from .team_data_reuse import sha256
from .workstation_model import write_json

INPUT_SCHEMA = "enterprise-registration-input-v1"
OUTPUT_SCHEMA = "enterprise-registration-stress-v1"
EXPECTED_PART_ID = "097"
TARGET_MM = 0.1
DEFAULT_OUTPUT = RUNS / "enterprise_offline_20261123" / "registration"


def rigid_transform(
    rotation_deg=(0.0, 0.0, 0.0), translation_m=(0.0, 0.0, 0.0)
):
    """Return a rigid 4x4 T_A_B matrix for column vectors."""
    transform = np.eye(4)
    transform[:3, :3] = Rotation.from_euler(
        "xyz", rotation_deg, degrees=True
    ).as_matrix()
    transform[:3, 3] = np.asarray(translation_m, float)
    return transform


def pose_error(estimated, truth):
    """Compare two rigid transforms without using registration residuals."""
    estimated = np.asarray(estimated, float)
    truth = np.asarray(truth, float)
    return {
        "translation_mm": float(
            np.linalg.norm(estimated[:3, 3] - truth[:3, 3]) * 1000
        ),
        "rotation_deg": float(
            np.rad2deg(
                Rotation.from_matrix(
                    estimated[:3, :3] @ truth[:3, :3].T
                ).magnitude()
            )
        ),
    }


def write_ascii_pcd(path, points_m, units="mm"):
    """Write an explicit-row XYZ PCD; NaNs preserve missing organized rows."""
    if units not in {"mm", "m"}:
        raise ValueError("PCD units must be explicitly mm or m")
    points = np.asarray(points_m, float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("PCD requires XYZ rows")
    values = points * (1000 if units == "mm" else 1)
    path = Path(path)
    with path.open("w", encoding="ascii") as stream:
        stream.write(
            "# .PCD v0.7; units are external metadata and must stay explicit\n"
            "VERSION .7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\n"
            "COUNT 1 1 1\n"
            f"WIDTH {len(values)}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\n"
            f"POINTS {len(values)}\nDATA ascii\n"
        )
        np.savetxt(stream, values, fmt="%.9g")
    return path


class SurfaceProjector:
    """Project to CAD faces with a conservative centroid-sphere fallback.

    Nearby centroids provide an initial distance. Each triangle lies within
    its centroid sphere, so faces whose sphere can improve that distance are
    also projected. A large face cannot be missed merely because its centroid
    is far away. Queries stay local without building a points-by-all-faces
    triangle array.
    """

    def __init__(self, mesh, candidate_faces=256, batch_size=128):
        if candidate_faces < 1 or batch_size < 1 or not len(mesh.faces):
            raise ValueError("Projection requires positive sizes and CAD faces")
        self.mesh = mesh
        self.candidate_faces = min(int(candidate_faces), len(mesh.faces))
        self.batch_size = int(batch_size)
        self.tree = cKDTree(mesh.triangles_center)
        self.face_radii = np.linalg.norm(
            mesh.triangles - mesh.triangles_center[:, None, :], axis=2
        ).max(axis=1)
        self.max_face_radius = float(self.face_radii.max())

    def project(self, points_m):
        points = np.asarray(points_m, float)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("Projection requires XYZ rows")
        if not np.isfinite(points).all():
            raise ValueError("Projection requires finite points")
        if not len(points):
            raise ValueError("Projection requires at least one point")
        closest_all, distance_all, face_all = [], [], []
        for start in range(0, len(points), self.batch_size):
            batch = points[start : start + self.batch_size]
            _, candidate_ids = self.tree.query(
                batch, k=self.candidate_faces, workers=1
            )
            candidate_ids = np.asarray(candidate_ids)
            if candidate_ids.ndim == 1:
                candidate_ids = candidate_ids[:, None]
            triangles = self.mesh.triangles[candidate_ids]
            query = np.repeat(
                batch[:, None, :], self.candidate_faces, axis=1
            ).reshape(-1, 3)
            closest = trimesh.triangles.closest_point(
                triangles.reshape(-1, 3, 3), query
            ).reshape(batch.shape[0], self.candidate_faces, 3)
            distance = np.linalg.norm(closest - batch[:, None, :], axis=2)
            best = distance.argmin(axis=1)
            rows = np.arange(batch.shape[0])
            batch_closest = closest[rows, best].copy()
            batch_distance = distance[rows, best].copy()
            batch_faces = candidate_ids[rows, best].copy()
            for row, point in enumerate(batch):
                nearby = np.asarray(
                    self.tree.query_ball_point(
                        point,
                        batch_distance[row] + self.max_face_radius + 1e-12,
                    ),
                    dtype=int,
                )
                lower_bound = (
                    np.linalg.norm(
                        self.mesh.triangles_center[nearby] - point, axis=1
                    )
                    - self.face_radii[nearby]
                )
                fallback = nearby[
                    (lower_bound <= batch_distance[row] + 1e-12)
                    & ~np.isin(nearby, candidate_ids[row])
                ]
                if not len(fallback):
                    continue
                candidates = trimesh.triangles.closest_point(
                    self.mesh.triangles[fallback],
                    np.repeat(point[None, :], len(fallback), axis=0),
                )
                distances = np.linalg.norm(candidates - point, axis=1)
                index = int(distances.argmin())
                if distances[index] < batch_distance[row]:
                    batch_closest[row] = candidates[index]
                    batch_distance[row] = distances[index]
                    batch_faces[row] = fallback[index]
            closest_all.append(batch_closest)
            distance_all.append(batch_distance)
            face_all.append(batch_faces)
        return (
            np.vstack(closest_all),
            np.concatenate(distance_all),
            np.concatenate(face_all),
        )


def _rigid_or_raise(transform):
    transform = np.asarray(transform, float)
    if (
        transform.shape != (4, 4)
        or not np.isfinite(transform).all()
        or not np.allclose(transform[3], [0, 0, 0, 1])
        or not np.allclose(
            transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-6
        )
        or not np.isclose(np.linalg.det(transform[:3, :3]), 1.0, atol=1e-6)
    ):
        raise ValueError("T_workpiece_scan_m must be a rigid metre transform")
    return transform


def _index_array(value, row_count, label):
    if not isinstance(value, list) or any(
        type(index) is not int for index in value
    ):
        raise ValueError(f"{label} must contain integer original rows")
    indices = np.asarray(value, int)
    if (
        indices.ndim != 1
        or len(indices) == 0
        or np.unique(indices).size != len(indices)
        or indices.min() < 0
        or indices.max() >= row_count
    ):
        raise ValueError(f"{label} must be unique in-range original rows")
    return indices


def validate_import_manifest(manifest, mesh, expected_cad_sha256):
    """Validate a replaceable full-part 097 manifest without inferring identity."""
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if manifest.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"Manifest schema must be {INPUT_SCHEMA}")
    if manifest.get("part_id") != EXPECTED_PART_ID:
        raise ValueError("This importer is bound to nominal part 097")
    if manifest.get("cad_sha256") != expected_cad_sha256:
        raise ValueError("CAD identity hash does not match nominal 097 CAD")
    if manifest.get("part_matches_cad_confirmed") is not True:
        raise ValueError(
            "Independent part-to-CAD identity confirmation is required"
        )
    if not str(manifest.get("identity_evidence", "")).strip():
        raise ValueError("Identity evidence is required")
    source_kind = manifest.get("source_kind")
    if source_kind not in {"real_scan", "synthetic_control"}:
        raise ValueError("source_kind must be real_scan or synthetic_control")
    if source_kind == "real_scan" and "truth" in manifest:
        raise ValueError("Real imports must not contain synthetic truth")
    if source_kind == "synthetic_control" and "truth" not in manifest:
        raise ValueError("Synthetic controls require explicit truth")
    if (
        source_kind == "synthetic_control"
        and manifest.get("synthetic_data_only") is not True
    ):
        raise ValueError(
            "synthetic_data_only must be true for synthetic_control"
        )
    if manifest.get("processing_permission_confirmed") is not True:
        raise ValueError(
            "Processing-row disposition must be independently confirmed"
        )

    base = Path(manifest.get("_base_directory", "."))
    cloud_file = base / manifest["cloud_file"]
    if sha256(cloud_file) != manifest["cloud_sha256"]:
        raise ValueError("Raw point-cloud source hash mismatch")
    points = load_cloud(cloud_file, manifest["units"])
    finite = np.isfinite(points).all(axis=1)
    train = _index_array(
        manifest["normal_train_indices"], len(points), "normal_train_indices"
    )
    holdout = _index_array(
        manifest["normal_holdout_indices"],
        len(points),
        "normal_holdout_indices",
    )
    processing = _index_array(
        manifest["processing_indices"], len(points), "processing_indices"
    )
    selected = np.concatenate([train, holdout, processing])
    if np.unique(selected).size != len(selected):
        raise ValueError("Train, holdout and processing rows must be disjoint")
    finite_train = train[finite[train]]
    finite_holdout = holdout[finite[holdout]]
    finite_processing = processing[finite[processing]]
    if (
        len(finite_train) < 60
        or len(finite_holdout) < 12
        or len(finite_processing) < 6
    ):
        raise ValueError(
            "Insufficient finite train, holdout or processing rows"
        )
    _rigid_or_raise(manifest["T_workpiece_scan_m"])

    nominal_diagonal_m = float(np.linalg.norm(mesh.extents))
    finite_diagonal_m = float(np.linalg.norm(np.ptp(points[finite], axis=0)))
    ratio = finite_diagonal_m / nominal_diagonal_m
    if manifest.get("unit_consistency_mode") != "nominal_full_part_extent":
        raise ValueError("Full-part unit consistency mode is required")
    if not 0.5 <= ratio <= 2.0:
        raise ValueError(
            "Cloud extent is inconsistent with explicit units and nominal 097 CAD"
        )
    return {
        "points": points,
        "finite_mask": finite,
        "normal_train_indices": train,
        "normal_holdout_indices": holdout,
        "processing_indices": processing,
        "finite_counts": {
            "train": int(len(finite_train)),
            "holdout": int(len(finite_holdout)),
            "processing": int(len(finite_processing)),
        },
        "source_kind": source_kind,
        "unit_extent_ratio": ratio,
    }


def _distribution(values_mm):
    values = np.asarray(values_mm, float)
    if not len(values):
        return None
    return {
        "count": int(len(values)),
        "mean_mm": float(values.mean()),
        "rms_mm": float(np.sqrt(np.mean(values**2))),
        "p95_mm": float(np.quantile(values, 0.95)),
        "absolute_p95_mm": float(np.quantile(np.abs(values), 0.95)),
        "max_mm": float(np.max(np.abs(values))),
    }


def _normal_metrics(projector, points_m, transform):
    points = (
        np.asarray(points_m, float) @ transform[:3, :3].T + transform[:3, 3]
    )
    closest, distance, faces = projector.project(points)
    normals = projector.mesh.face_normals[faces]
    signed = np.einsum("ij,ij->i", points - closest, normals) * 1000
    return {
        "count": int(len(points)),
        "signed_point_to_plane": _distribution(signed),
        "absolute_distance": _distribution(distance * 1000),
    }


def register_normal_regions(
    mesh, normal_points_m, initial_transform, projector
):
    """Refine six-DOF pose using only finite normal-region training rows."""
    transform = _rigid_or_raise(initial_transform).copy()
    initial = transform.copy()
    points = np.asarray(normal_points_m, float)
    finite = np.isfinite(points).all(axis=1)
    points = points[finite]
    if len(points) < 30:
        raise ValueError("Fewer than 30 finite normal training rows")
    trace = []
    rank = 0
    for _ in range(12):
        transformed = points @ transform[:3, :3].T + transform[:3, 3]
        closest, distance, faces = projector.project(transformed)
        if np.quantile(distance, 0.9) > 0.010:
            raise ValueError("Normal-region initialization is too far from CAD")
        normals = mesh.face_normals[faces]
        residual = np.einsum("ij,ij->i", transformed - closest, normals)
        centered = transformed - transformed.mean(axis=0)
        scale = max(np.linalg.norm(centered, axis=1).mean(), 1e-4)
        observability = np.column_stack(
            [np.cross(centered, normals) / scale, normals]
        )
        rank = int(np.linalg.matrix_rank(observability, tol=1e-5))
        if rank < 6:
            raise ValueError(
                "Normal regions do not constrain six rigid pose DOFs"
            )
        jacobian = np.column_stack([np.cross(transformed, normals), normals])
        weights = np.sqrt(
            np.minimum(1, 0.0002 / np.maximum(np.abs(residual), 1e-12))
        )
        delta = np.linalg.lstsq(
            jacobian * weights[:, None], -residual * weights, rcond=None
        )[0]
        if (
            np.linalg.norm(delta[:3]) > np.deg2rad(0.5)
            or np.linalg.norm(delta[3:]) > 0.001
        ):
            delta /= max(
                np.linalg.norm(delta[:3]) / np.deg2rad(0.5),
                np.linalg.norm(delta[3:]) / 0.001,
            )
        increment = np.eye(4)
        increment[:3, :3] = Rotation.from_rotvec(delta[:3]).as_matrix()
        increment[:3, 3] = delta[3:]
        transform = increment @ transform
        trace.append(float(np.sqrt(np.mean(residual**2)) * 1000))
        if (
            np.linalg.norm(delta[:3]) < 1e-8
            and np.linalg.norm(delta[3:]) < 1e-8
        ):
            break
    change = transform @ np.linalg.inv(initial)
    if np.linalg.norm(change[:3, 3]) > 0.005 or Rotation.from_matrix(
        change[:3, :3]
    ).magnitude() > np.deg2rad(3):
        raise ValueError("Registration exceeded bounded known-pose refinement")
    return transform, {
        "rank": rank,
        "iterations": len(trace),
        "fit_trace_rms_mm": trace,
        "fit": _normal_metrics(projector, points, transform),
    }


def _sample_diverse_faces(mesh, rng, count):
    probabilities = mesh.area_faces / mesh.area_faces.sum()
    for _ in range(8):
        face_ids = rng.choice(
            len(mesh.faces), size=count, replace=False, p=probabilities
        )
        centers = mesh.triangles_center[face_ids]
        normals = mesh.face_normals[face_ids]
        centered = centers - centers.mean(axis=0)
        scale = max(np.linalg.norm(centered, axis=1).mean(), 1e-4)
        observability = np.column_stack(
            [np.cross(centered, normals) / scale, normals]
        )
        if np.linalg.matrix_rank(observability, tol=1e-5) == 6:
            return face_ids
    raise ValueError("Could not sample six-DOF-observable normal anchors")


def build_synthetic_cloud(mesh, seed):
    """Build an explicit 216-row synthetic 097 control cloud."""
    rng = np.random.default_rng(seed)
    face_ids = _sample_diverse_faces(mesh, rng, 216)
    normal_faces = face_ids[:180]
    defect_faces = face_ids[180:]
    normal_points = mesh.triangles_center[normal_faces]
    defect_heights_mm = np.tile([0.35, 0.50, 0.75], 12)
    defect_points = (
        mesh.triangles_center[defect_faces]
        + mesh.face_normals[defect_faces] * (defect_heights_mm / 1000)[:, None]
    )
    truth = rigid_transform(
        rotation_deg=(2.0, -4.0, 6.0),
        translation_m=(0.021, -0.013, 0.007),
    )
    workpiece_points = np.vstack([normal_points, defect_points])
    scan_points = workpiece_points @ np.linalg.inv(truth)[:3, :3].T
    scan_points += np.linalg.inv(truth)[:3, 3]
    return {
        "points_scan_m": scan_points,
        "workpiece_points_m": workpiece_points,
        "normal_train_indices": np.arange(140),
        "normal_holdout_indices": np.arange(140, 180),
        "processing_indices": np.arange(180, 216),
        "expected_defect_heights_mm": defect_heights_mm,
        "truth_transform": truth,
    }


def _planar_training_replacement(mesh, control, seed):
    rng = np.random.default_rng(seed + 101)
    face_id = int(np.argmin(mesh.area_faces))
    triangle = mesh.triangles[face_id]
    barycentric = rng.dirichlet(np.ones(3), size=140)
    planar_workpiece = barycentric @ triangle
    points = control["points_scan_m"].copy()
    scan_rows = (
        planar_workpiece @ np.linalg.inv(control["truth_transform"])[:3, :3].T
    )
    scan_rows += np.linalg.inv(control["truth_transform"])[:3, 3]
    points[:140] = scan_rows
    return points


def _apply_case_effect(name, control, rng):
    points = control["points_scan_m"].copy()
    if name == "noisy":
        points[:, 2] += rng.normal(0.0, 0.00006, size=len(points))
    elif name == "dropout":
        missing = rng.random(len(points)) < 0.06
        points[missing] = np.nan
    elif name == "occluded":
        threshold = np.quantile(points[:, 1], 0.82)
        points[points[:, 1] > threshold] = np.nan
    return points


def _manifest_for_case(name, control, cloud_file, units="mm", cad_hash=None):
    finite = np.isfinite(control["points_scan_m"]).all(axis=1)
    truth = control["truth_transform"]
    manifest = {
        "schema": INPUT_SCHEMA,
        "part_id": EXPECTED_PART_ID,
        "part_matches_cad_confirmed": True,
        "identity_evidence": (
            "Synthetic control generated directly from the nominal 097 CAD; "
            "not a real scan and not evidence about the physical camera or fixture."
        ),
        "source_kind": "synthetic_control",
        "synthetic_data_only": True,
        "cad_sha256": sha256(DEFAULT_MESH) if cad_hash is None else cad_hash,
        "cloud_file": cloud_file.name,
        "cloud_sha256": sha256(cloud_file),
        "units": units,
        "coverage_scope": "full_part_normal_and_processing_rows",
        "unit_consistency_mode": "nominal_full_part_extent",
        "T_workpiece_scan_m": truth.tolist(),
        "normal_train_indices": control["normal_train_indices"].tolist(),
        "normal_holdout_indices": control["normal_holdout_indices"].tolist(),
        "processing_indices": control["processing_indices"].tolist(),
        "processing_permission_confirmed": True,
        "truth": {
            "T_workpiece_scan_m": truth.tolist(),
            "processing_signed_heights_mm": control[
                "expected_defect_heights_mm"
            ].tolist(),
        },
        "finite_rows": int(finite.sum()),
        "input_rows": int(len(control["points_scan_m"])),
    }
    return manifest


def _case_metrics(control, points, initial_transform, projector):
    train_rows = control["normal_train_indices"]
    holdout_rows = control["normal_holdout_indices"]
    processing_rows = control["processing_indices"]
    estimated_transform, registration = register_normal_regions(
        projector.mesh,
        points[train_rows],
        initial_transform,
        projector,
    )
    holdout_points = points[holdout_rows]
    holdout_finite = np.isfinite(holdout_points).all(axis=1)
    holdout = _normal_metrics(
        projector,
        holdout_points[holdout_finite],
        estimated_transform,
    )
    finite_processing = np.isfinite(points[processing_rows]).all(axis=1)
    processing_points = points[processing_rows][finite_processing]
    transformed = (
        processing_points @ estimated_transform[:3, :3].T
        + estimated_transform[:3, 3]
    )
    closest, _, faces = projector.project(transformed)
    normals = projector.mesh.face_normals[faces]
    heights = np.einsum("ij,ij->i", transformed - closest, normals) * 1000
    expected = control["expected_defect_heights_mm"][finite_processing]
    height_error = heights - expected
    return {
        "status": "pass",
        "T_workpiece_scan_m": estimated_transform.tolist(),
        "registration": registration,
        "independent_holdout": holdout,
        "processing_signed_normal_height": {
            "measured": _distribution(heights),
            "expected": _distribution(expected),
            "error": _distribution(height_error),
        },
        "missing_processing_rows": int((~finite_processing).sum()),
        "truth_pose_error": pose_error(
            estimated_transform, control["truth_transform"]
        ),
    }


def _projection_selfcheck(mesh, projector, control):
    normal_points = (
        control["points_scan_m"][:32] @ control["truth_transform"][:3, :3].T
        + control["truth_transform"][:3, 3]
    )
    scan_points = np.vstack(
        [normal_points[:16], control["workpiece_points_m"][180:196]]
    )
    fast = projector.project(scan_points)[0]
    exact = closest_surface(mesh, scan_points)[0]
    return float(np.max(np.linalg.norm(fast - exact, axis=1)) * 1000)


def run_error_budget(seed, samples=20000):
    """Propagate explicit stress assumptions; none are measured hardware data."""
    if type(samples) is not int or samples < 32:
        raise ValueError("At least 32 Monte Carlo samples are required")
    assumptions = {
        "depth_noise_sigma_mm": 0.06,
        "camera_extrinsic_translation_sigma_mm": 0.05,
        "camera_extrinsic_rotation_sigma_deg": 0.01,
        "turntable_angle_sigma_deg": 0.01,
        "turntable_axis_tilt_sigma_deg": 0.01,
        "fixture_tcp_translation_sigma_mm": 0.05,
        "fixture_tcp_rotation_sigma_deg": 0.01,
    }
    geometry = {
        "camera_to_point_distance_mm": 764.319,
        "turntable_radius_mm": 270.0,
        "turntable_axis_measurement_height_mm": 50.0,
        "fixture_tcp_lever_arm_mm": 100.0,
    }
    rng = np.random.default_rng(seed)
    camera_lever = np.array([0.0, 0.0, geometry["camera_to_point_distance_mm"]])
    turntable_lever = np.array(
        [
            geometry["turntable_radius_mm"],
            0.0,
            geometry["turntable_axis_measurement_height_mm"],
        ]
    )
    fixture_lever = np.array([geometry["fixture_tcp_lever_arm_mm"], 0.0, 0.0])

    def rotation_error(sigma_deg, lever):
        angles = rng.normal(0.0, np.deg2rad(sigma_deg), (samples, 3))
        return np.cross(angles, lever)

    depth = np.zeros((samples, 3))
    depth[:, 2] = rng.normal(0.0, assumptions["depth_noise_sigma_mm"], samples)
    turntable_angles = np.zeros((samples, 3))
    turntable_angles[:, 2] = rng.normal(
        0.0, np.deg2rad(assumptions["turntable_angle_sigma_deg"]), samples
    )
    axis_tilts = rng.normal(
        0.0,
        np.deg2rad(assumptions["turntable_axis_tilt_sigma_deg"]),
        (samples, 3),
    )
    axis_tilts[:, 2] = 0.0
    components = {
        "depth_noise_mm": depth,
        "camera_extrinsic_translation_mm": rng.normal(
            0.0,
            assumptions["camera_extrinsic_translation_sigma_mm"],
            (samples, 3),
        ),
        "camera_extrinsic_rotation_mm": rotation_error(
            assumptions["camera_extrinsic_rotation_sigma_deg"], camera_lever
        ),
        "turntable_angle_mm": np.cross(turntable_angles, turntable_lever),
        "turntable_axis_tilt_mm": np.cross(axis_tilts, turntable_lever),
        "fixture_tcp_translation_mm": rng.normal(
            0.0,
            assumptions["fixture_tcp_translation_sigma_mm"],
            (samples, 3),
        ),
        "fixture_tcp_rotation_mm": rotation_error(
            assumptions["fixture_tcp_rotation_sigma_deg"], fixture_lever
        ),
    }
    total = np.linalg.norm(sum(components.values()), axis=1)
    rss = float(
        np.sqrt(
            assumptions["depth_noise_sigma_mm"] ** 2
            + 3 * assumptions["camera_extrinsic_translation_sigma_mm"] ** 2
            + 2
            * (
                np.deg2rad(assumptions["camera_extrinsic_rotation_sigma_deg"])
                * geometry["camera_to_point_distance_mm"]
            )
            ** 2
            + (
                np.deg2rad(assumptions["turntable_angle_sigma_deg"])
                * geometry["turntable_radius_mm"]
            )
            ** 2
            + np.deg2rad(assumptions["turntable_axis_tilt_sigma_deg"]) ** 2
            * (
                geometry["turntable_radius_mm"] ** 2
                + 2 * geometry["turntable_axis_measurement_height_mm"] ** 2
            )
            + 3 * assumptions["fixture_tcp_translation_sigma_mm"] ** 2
            + 2
            * (
                np.deg2rad(assumptions["fixture_tcp_rotation_sigma_deg"])
                * geometry["fixture_tcp_lever_arm_mm"]
            )
            ** 2
        )
    )
    p95 = float(np.quantile(total, 0.95))
    return {
        "method": "sum of independent 3D first-order perturbation vectors",
        "samples": int(samples),
        "seed": int(seed),
        "target_mm": TARGET_MM,
        "support_criterion": "P95 point-localization error <= 0.1 mm",
        "criterion_scope": (
            "offline diagnostic, not an agreed enterprise acceptance statistic; "
            "calibration offsets are sampled across independent setups, "
            "not averaged away by taking more camera frames"
        ),
        "axis_convention": (
            "translation/rotation sigmas are per axis; depth is axial, "
            "turntable angle is about Z, tilt about X/Y; lever vectors "
            "and first-order cross products are synthetic assumptions"
        ),
        "assumptions": assumptions,
        "geometry_assumptions": geometry,
        "provenance": {
            "depth_noise": (
                "config/workstation.json virtual sensor assumption; not an "
                "ALSON S1-M or S2-C measured specification"
            ),
            "other_sigmas": (
                "offline stress assumptions; no real extrinsic, turntable, "
                "fixture or TCP calibration is available"
            ),
            "geometry": (
                "camera distance derived from virtual workstation positions; "
                "turntable radius from the virtual config; remaining lever "
                "arms are explicit stress assumptions"
            ),
        },
        "analytic_rss_1sigma_mm": rss,
        "monte_carlo": _distribution(total),
        "component_1sigma_mm": {
            name: float(np.sqrt(np.mean(np.sum(values**2, axis=1))))
            for name, values in components.items()
        },
        "p95_mm": p95,
        "supports_target": bool(p95 <= TARGET_MM),
    }


def _case_definitions():
    return [
        ("known_transform", "pass"),
        ("noisy", "pass"),
        ("dropout", "pass"),
        ("occluded", "pass"),
        ("incorrect_units", "reject"),
        ("degenerate_normals", "reject"),
        ("wrong_cad_identity", "reject"),
    ]


def _run_case(
    name,
    expectation,
    control,
    mesh,
    output,
    projector,
    seed,
):
    case_dir = output / name
    case_dir.mkdir()
    rng = np.random.default_rng(
        seed + _case_definitions().index((name, expectation))
    )
    points = _apply_case_effect(name, control, rng)
    if name == "degenerate_normals":
        points = _planar_training_replacement(mesh, control, seed)
    cloud_file = case_dir / "synthetic_scan.pcd"
    manifest_units = "m" if name == "incorrect_units" else "mm"
    write_ascii_pcd(cloud_file, points, units="mm")
    cad_hash = (
        "wrong-cad-identity-rejection-control"
        if name == "wrong_cad_identity"
        else None
    )
    manifest = _manifest_for_case(
        name, control, cloud_file, units=manifest_units, cad_hash=cad_hash
    )
    manifest["_base_directory"] = str(case_dir)
    manifest_file = case_dir / "manifest.json"
    write_json(manifest_file, manifest)
    result = {"case": name, "expectation": expectation}
    try:
        validated = validate_import_manifest(
            manifest, mesh, sha256(DEFAULT_MESH)
        )
        points_m = validated["points"]
        initial_transform = control["truth_transform"].copy()
        if name not in {"known_transform", "degenerate_normals"}:
            initial_transform = (
                rigid_transform(
                    rotation_deg=(0.012, -0.008, 0.006),
                    translation_m=(0.00008, -0.00005, 0.00004),
                )
                @ initial_transform
            )
        metrics = _case_metrics(control, points_m, initial_transform, projector)
        result.update(metrics)
    except (KeyError, ValueError) as error:
        result.update(
            {
                "status": "rejected_expected"
                if expectation == "reject"
                else "failed",
                "reason": str(error),
            }
        )
    rejection_reasons = {
        "incorrect_units": "Cloud extent is inconsistent",
        "degenerate_normals": "do not constrain six rigid pose DOFs",
        "wrong_cad_identity": "CAD identity hash does not match",
    }
    expected_reason = rejection_reasons.get(name)
    result["expected_rejection_reason"] = expected_reason
    result["expected_outcome_met"] = (
        expectation == "pass" and result["status"] == "pass"
    ) or (
        expectation == "reject"
        and result["status"] == "rejected_expected"
        and expected_reason in result.get("reason", "")
    )
    write_json(case_dir / "case.json", result)
    return result, cloud_file, manifest_file


def run(output, seed=20261123, monte_carlo_samples=20000):
    """Run the small default offline suite; output must be a new directory."""
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(
            "Output is not empty; choose a new registration directory"
        )
    output.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.load_mesh(DEFAULT_MESH, process=True)
    projector = SurfaceProjector(mesh)
    control = build_synthetic_cloud(mesh, seed)
    selfcheck_difference_mm = _projection_selfcheck(mesh, projector, control)
    if selfcheck_difference_mm > 1e-6:
        raise ValueError(
            "Fast surface projection differs from exact CAD reference"
        )

    case_results, inputs, artifacts, failures = [], [], [], []
    for name, expectation in _case_definitions():
        result, cloud_file, manifest_file = _run_case(
            name,
            expectation,
            control,
            mesh,
            output,
            projector,
            seed,
        )
        case_results.append(result)
        inputs.append(
            {
                "role": name,
                "cloud_path": str(cloud_file),
                "cloud_sha256": sha256(cloud_file),
                "manifest_path": str(manifest_file),
                "manifest_sha256": sha256(manifest_file),
                "provenance": "synthetic control generated from nominal 097 CAD",
            }
        )
        artifacts.extend(
            [
                {"path": str(cloud_file), "sha256": sha256(cloud_file)},
                {"path": str(manifest_file), "sha256": sha256(manifest_file)},
                {
                    "path": str(output / name / "case.json"),
                    "sha256": sha256(output / name / "case.json"),
                },
            ]
        )
        if result["status"] in {"rejected_expected", "failed"}:
            failures.append(
                {
                    "case": name,
                    "status": result["status"],
                    "reason": result.get("reason"),
                    "expected": result["status"] == "rejected_expected",
                }
            )

    budget = run_error_budget(seed, monte_carlo_samples)
    budget_file = output / "error_budget.json"
    write_json(budget_file, budget)
    schema = {
        "schema": INPUT_SCHEMA,
        "real_data_requirements": [
            "source_kind must be real_scan and synthetic truth must be absent",
            "independently confirmed 097 identity and matching CAD hash",
            "raw cloud hash, explicit mm or m units, and original-row indices",
            "disjoint normal train, normal holdout and processing rows",
            "rigid T_workpiece_scan_m with translation in metres",
            "independently confirmed processing disposition",
        ],
        "synthetic_marker_required": "synthetic_control must set synthetic_data_only=true",
    }
    schema_file = output / "import_schema.json"
    write_json(schema_file, schema)

    passed = sum(result["status"] == "pass" for result in case_results)
    expected_rejections = sum(
        result["status"] == "rejected_expected" for result in case_results
    )
    unexpected_failures = [
        result for result in case_results if not result["expected_outcome_met"]
    ]
    summary = {
        "schema": OUTPUT_SCHEMA,
        "task": "offline registration, coordinate and error-budget rehearsal",
        "source_kind": "synthetic_control",
        "simulation_only": True,
        "hardware_controlled": False,
        "seed": seed,
        "case_results": case_results,
        "cases_passed": int(passed),
        "cases_rejected_as_expected": int(expected_rejections),
        "unexpected_failures": int(len(unexpected_failures)),
        "all_expectations_met": not unexpected_failures,
        "projection_selfcheck_max_difference_mm": selfcheck_difference_mm,
        "error_budget": budget,
    }
    summary_file = output / "summary.json"
    write_json(summary_file, summary)
    report_file = output / "report.md"
    report_file.write_text(
        "# Offline registration stress report\n\n"
        "- Source: explicit synthetic control from the nominal 097 CAD.\n"
        f"- Cases: {passed} passed, {expected_rejections} rejected as expected.\n"
        f"- Unexpected failures: {len(unexpected_failures)}.\n"
        f"- Budget P95: {budget['p95_mm']:.6f} mm.\n"
        f"- Supports ±0.1 mm target: {str(budget['supports_target']).lower()}.\n",
        encoding="utf-8",
    )
    commands = [
        (
            "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            ".venv/bin/python -m unittest tests.test_registration_stress -v"
        ),
        ".venv/bin/python -m ruff check src/daiyi_grinding/registration_stress.py tests/test_registration_stress.py",
        ".venv/bin/python -m ruff format --check src/daiyi_grinding/registration_stress.py tests/test_registration_stress.py",
        (
            "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            f".venv/bin/python -m daiyi_grinding.registration_stress --output {output}"
        ),
    ]
    evidence = {
        "schema": "enterprise-offline-registration-evidence-v1",
        "task": summary["task"],
        "scope": {
            "offline_software_only": True,
            "mujoco_processes_started": 0,
            "real_scan_used": False,
            "robot_control": False,
            "external_messages": False,
        },
        "commands": commands,
        "inputs": [
            {
                "role": "nominal_cad",
                "path": str(DEFAULT_MESH),
                "sha256": sha256(DEFAULT_MESH),
                "provenance": "supplied nominal 097 workpiece CAD",
            },
            *inputs,
        ],
        "metrics": {
            "projection_selfcheck_max_difference_mm": selfcheck_difference_mm,
            "cases_passed": int(passed),
            "cases_rejected_as_expected": int(expected_rejections),
            "unexpected_failures": int(len(unexpected_failures)),
            "error_budget_p95_mm": budget["p95_mm"],
            "supports_0_1mm_target": budget["supports_target"],
        },
        "failures": failures,
        "artifacts": [
            *artifacts,
            {"path": str(budget_file), "sha256": sha256(budget_file)},
            {"path": str(schema_file), "sha256": sha256(schema_file)},
            {"path": str(summary_file), "sha256": sha256(summary_file)},
            {"path": str(report_file), "sha256": sha256(report_file)},
        ],
        "real_data_pending": {
            "confirmed_camera_model_and_serial": True,
            "measured_intrinsics_and_distance_error": True,
            "measured_camera_extrinsics": True,
            "turntable_encoder_axis_and_angle_calibration": True,
            "fixture_and_tcp_lever_arm_calibration": True,
            "independent_real_097_scan_with_holdout_labels": True,
            "note": "097 real-scan fitting remains false; no synthetic result is real evidence.",
        },
    }
    write_json(output / "evidence.json", evidence)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--monte-carlo-samples", type=int, default=20000)
    args = parser.parse_args()
    report = run(args.output, args.seed, args.monte_carlo_samples)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "cases_passed": report["cases_passed"],
                "cases_rejected_as_expected": report[
                    "cases_rejected_as_expected"
                ],
                "unexpected_failures": report["unexpected_failures"],
                "error_budget_p95_mm": report["error_budget"]["p95_mm"],
                "supports_0_1mm_target": report["error_budget"][
                    "supports_target"
                ],
            },
            indent=2,
        )
    )
    if report["unexpected_failures"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
