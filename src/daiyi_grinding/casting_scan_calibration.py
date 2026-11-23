"""Explicit-unit real-scan ingestion and guarded CAD-relative excess measurement."""

from __future__ import annotations

import argparse
import io
import json
import struct
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

from .cad_defects import DEFAULT_MESH
from .team_data_reuse import distribution, sha256
from .workstation_model import write_json


def _lzf_decode(data, size):
    """Decode PCL's standard LZF stream; compressed PCD is field-major."""
    output, cursor = bytearray(), 0
    while cursor < len(data):
        control = data[cursor]
        cursor += 1
        if control < 32:
            count = control + 1
            if cursor + count > len(data):
                raise ValueError("Truncated PCD literal")
            output.extend(data[cursor : cursor + count])
            cursor += count
        else:
            count = control >> 5
            reference = len(output) - ((control & 31) << 8) - 1
            if count == 7:
                if cursor >= len(data):
                    raise ValueError("Truncated PCD back-reference")
                count += data[cursor]
                cursor += 1
            if cursor >= len(data):
                raise ValueError("Truncated PCD back-reference")
            reference -= data[cursor]
            cursor += 1
            if reference < 0:
                raise ValueError("Invalid PCD back-reference")
            for _ in range(count + 2):
                output.append(output[reference])
                reference += 1
        if len(output) > size:
            raise ValueError("PCD decompression exceeds declared size")
    if len(output) != size:
        raise ValueError("PCD decompression size mismatch")
    return bytes(output)


def read_pcd(path):
    """Read XYZ without changing row order, including NaNs and organized rows."""
    with Path(path).open("rb") as stream:
        header = {}
        while True:
            line = stream.readline()
            if not line:
                raise ValueError("PCD has no DATA header")
            words = line.decode("ascii").strip().split()
            if not words or words[0].startswith("#"):
                continue
            header[words[0].upper()] = words[1:]
            if words[0].upper() == "DATA":
                break
        raw = stream.read()
    names = header.get("FIELDS", header.get("FIELD"))
    sizes = list(map(int, header["SIZE"]))
    types = header["TYPE"]
    counts = list(map(int, header.get("COUNT", ["1"] * len(names))))
    if not (len(names) == len(sizes) == len(types) == len(counts)):
        raise ValueError("Inconsistent PCD fields")
    points = int(
        header.get(
            "POINTS", [str(int(header["WIDTH"][0]) * int(header["HEIGHT"][0]))]
        )[0]
    )
    if not 0 < points <= 10_000_000:
        raise ValueError("Invalid PCD point count")
    formats = {
        ("F", 4): "<f4",
        ("F", 8): "<f8",
        **{
            (kind, size): f"<{code}{size}"
            for kind, code in [("I", "i"), ("U", "u")]
            for size in (1, 2, 4, 8)
        },
    }
    dtype = np.dtype(
        [
            (name, formats[(kind, size)], (count,) if count > 1 else ())
            for name, kind, size, count in zip(names, types, sizes, counts)
        ]
    )
    for name in ("x", "y", "z"):
        if name not in names or counts[names.index(name)] != 1:
            raise ValueError("PCD requires scalar x, y, z")
    mode = header["DATA"][0]
    if mode == "ascii":
        values = np.loadtxt(io.BytesIO(raw), ndmin=2)
        if values.shape != (points, sum(counts)):
            raise ValueError("PCD ASCII row/column count mismatch")
        offsets = np.r_[0, np.cumsum(counts[:-1])]
        return np.column_stack(
            [values[:, offsets[names.index(name)]] for name in ("x", "y", "z")]
        )
    if mode == "binary":
        if len(raw) != points * dtype.itemsize:
            raise ValueError("PCD binary byte count mismatch")
        values = np.frombuffer(raw, dtype=dtype, count=points)
        return np.column_stack([values[name] for name in ("x", "y", "z")])
    if mode == "binary_compressed":
        compressed_size, uncompressed_size = struct.unpack_from("<II", raw)
        if (
            len(raw) != compressed_size + 8
            or uncompressed_size != points * dtype.itemsize
        ):
            raise ValueError("PCD compressed byte count mismatch")
        decoded = _lzf_decode(raw[8:], uncompressed_size)
        cursor, fields = 0, {}
        for name, kind, size, count in zip(names, types, sizes, counts):
            length = points * size * count
            fields[name] = np.frombuffer(
                decoded[cursor : cursor + length], dtype=formats[(kind, size)]
            ).reshape(points, count)
            cursor += length
        return np.column_stack([fields[name][:, 0] for name in ("x", "y", "z")])
    raise ValueError(f"Unsupported PCD DATA mode: {mode}")


def load_cloud(path, units):
    if units not in {"mm", "m"}:
        raise ValueError("Point-cloud units must be explicitly mm or m")
    path = Path(path)
    if path.suffix.lower() == ".pcd":
        points = read_pcd(path)
    elif path.suffix.lower() == ".ply":
        points = trimesh.load(path, file_type="ply", process=False).vertices
    elif path.suffix.lower() == ".npy":
        points = np.load(path, allow_pickle=False)
    else:
        raise ValueError("Use PCD, PLY or NPY XYZ observations")
    points = np.asarray(points, float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("Point cloud must contain XYZ rows")
    return points * (0.001 if units == "mm" else 1)


def _rigid(transform):
    transform = np.asarray(transform, float)
    if (
        transform.shape != (4, 4)
        or not np.isfinite(transform).all()
        or not np.allclose(transform[3], [0, 0, 0, 1])
        or not np.allclose(
            transform[:3, :3].T @ transform[:3, :3], np.eye(3), atol=1e-6
        )
        or not np.isclose(np.linalg.det(transform[:3, :3]), 1, atol=1e-6)
    ):
        raise ValueError(
            "T_workpiece_scan_m must be a rigid metre-valued transform"
        )
    return transform


def closest_surface(mesh, points):
    positions, distances, face_ids = [], [], []
    for start in range(0, len(points), 32):
        point, distance, faces = trimesh.proximity.closest_point_naive(
            mesh, points[start : start + 32]
        )
        positions.append(point)
        distances.append(distance)
        face_ids.append(faces)
    return (
        np.vstack(positions),
        np.concatenate(distances),
        np.concatenate(face_ids),
    )


def register_normal_regions(mesh, normal_points_m, initial_transform):
    """Refine a known initialization only; anomaly/processing rows are excluded."""
    transform = _rigid(initial_transform).copy()
    initial = transform.copy()
    trace = []
    for _ in range(15):
        points = normal_points_m @ transform[:3, :3].T + transform[:3, 3]
        closest, distance, faces = closest_surface(mesh, points)
        if np.quantile(distance, 0.9) > 0.010:
            raise ValueError(
                "Normal-region initialization is too far from this CAD"
            )
        normals = mesh.face_normals[faces]
        residual = np.einsum("ij,ij->i", points - closest, normals)
        centered = points - points.mean(axis=0)
        scale = max(np.linalg.norm(centered, axis=1).mean(), 1e-4)
        observability = np.column_stack(
            [np.cross(centered, normals) / scale, normals]
        )
        rank = int(np.linalg.matrix_rank(observability, tol=1e-5))
        if rank < 6:
            raise ValueError(
                "Normal regions do not constrain all six rigid pose DOFs"
            )
        jacobian = np.column_stack([np.cross(points, normals), normals])
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
            np.linalg.norm(delta[:3]) < 1e-7
            and np.linalg.norm(delta[3:]) < 1e-7
        ):
            break
    change = transform @ np.linalg.inv(initial)
    if np.linalg.norm(change[:3, 3]) > 0.005 or Rotation.from_matrix(
        change[:3, :3]
    ).magnitude() > np.deg2rad(3):
        raise ValueError(
            "Registration exceeded the bounded known-pose refinement"
        )
    points = normal_points_m @ transform[:3, :3].T + transform[:3, 3]
    closest, distance, faces = closest_surface(mesh, points)
    signed = np.einsum("ij,ij->i", points - closest, mesh.face_normals[faces])
    p95 = float(np.quantile(distance, 0.95) * 1000)
    if p95 > 0.3:
        raise ValueError(
            f"Normal-region CAD residual P95 {p95:.3f} mm exceeds 0.3 mm"
        )
    return transform, {
        "rank": rank,
        "normal_region_points": len(points),
        "normal_distance_p95_mm": p95,
        "normal_signed_mad_mm": float(
            np.median(np.abs(signed - np.median(signed))) * 1000
        ),
        "rms_trace_mm": trace,
        "method": "bounded Huber point-to-plane rigid refinement on labeled normal rows",
        "nonrigid_deformation_used": False,
        "metrology_accuracy_validated": False,
    }


def calibrate_scan(manifest, output):
    manifest = Path(manifest).resolve()
    specification = json.loads(manifest.read_text())
    if (
        specification.get("part_id") != "097"
        or specification.get("part_matches_cad_confirmed") is not True
        or specification.get("cad_sha256") != sha256(DEFAULT_MESH)
        or not specification.get("identity_evidence")
    ):
        raise ValueError(
            "Require independently confirmed 097 identity and matching nominal CAD; do not force an unknown sample"
        )
    if specification.get("source_kind") not in {
        "real_scan",
        "synthetic_control",
    }:
        raise ValueError(
            "Explicit real-scan or synthetic-control provenance required"
        )
    if specification.get("root_permission_confirmed") is not True:
        raise ValueError(
            "Approved root labels and machining disposition are required"
        )
    cloud_file = manifest.parent / specification["cloud_file"]
    if sha256(cloud_file) != specification["cloud_sha256"]:
        raise ValueError("Raw point-cloud source hash mismatch")
    points = load_cloud(cloud_file, specification["units"])
    normal_indices = np.asarray(specification["normal_region_indices"], int)
    processing_indices = np.asarray(
        specification["processing_region_indices"], int
    )
    if (
        len(normal_indices) < 30
        or len(processing_indices) < 6
        or np.intersect1d(normal_indices, processing_indices).size
        or min(normal_indices.min(), processing_indices.min()) < 0
        or max(normal_indices.max(), processing_indices.max()) >= len(points)
        or not np.isfinite(
            points[np.r_[normal_indices, processing_indices]]
        ).all()
    ):
        raise ValueError(
            "Use finite, disjoint original-row normal and processing indices"
        )
    mesh = trimesh.load_mesh(DEFAULT_MESH, process=True)
    transform, registration = register_normal_regions(
        mesh, points[normal_indices], specification["T_workpiece_scan_m"]
    )
    observed = (
        points[processing_indices] @ transform[:3, :3].T + transform[:3, 3]
    )
    base, distance, faces = closest_surface(mesh, observed)
    normals = mesh.face_normals[faces]
    heights = np.einsum("ij,ij->i", observed - base, normals) * 1000
    tangent_error = (
        np.linalg.norm(
            observed - base - normals * (heights / 1000)[:, None], axis=1
        )
        * 1000
    )
    measured = (
        heights > max(0.15, 4 * registration["normal_signed_mad_mm"])
    ) & (tangent_error < 0.15)
    if measured.sum() < 6:
        raise ValueError(
            "No measured positive excess above normal-region variation"
        )
    roots_file = manifest.parent / specification["root_points_file"]
    roots = load_cloud(roots_file, specification["units"])
    roots = roots @ transform[:3, :3].T + transform[:3, 3]
    if len(roots) < 3 or not np.isfinite(roots).all():
        raise ValueError(
            "Explicit finite approved root observations are required"
        )
    nominal_roots, root_distance, root_faces = closest_surface(mesh, roots)
    if root_distance.max() * 1000 > 0.3:
        raise ValueError(
            "Observed root labels do not agree with the registered 097 CAD"
        )
    tangent = np.gradient(nominal_roots, axis=0)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-12)
    lateral = np.cross(mesh.face_normals[root_faces], tangent)
    lateral /= np.maximum(np.linalg.norm(lateral, axis=1, keepdims=True), 1e-12)
    _, nearest = cKDTree(nominal_roots).query(base[measured])
    across = (
        np.einsum(
            "ij,ij->i",
            base[measured] - nominal_roots[nearest],
            lateral[nearest],
        )
        * 1000
    )
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(
        out / "measured_excess.npz",
        observed_workpiece_m=observed[measured],
        nominal_base_workpiece_m=base[measured],
        signed_normal_height_mm=heights[measured],
        across_root_offset_mm=across,
        approved_nominal_roots_workpiece_m=nominal_roots,
        original_point_indices=processing_indices[measured],
    )
    summary = {
        "schema": "097-observed-excess-calibration-v1",
        "source_kind": specification["source_kind"],
        "real_scan_fitted": specification["source_kind"] == "real_scan",
        "calibration_scope": "observed local normal height and root position; hidden sheet thickness/curl and whole-part distributions remain unknown",
        "part_id": "097",
        "source_cloud_sha256": sha256(cloud_file),
        "root_label_sha256": sha256(roots_file),
        "manifest_sha256": sha256(manifest),
        "cad_sha256": sha256(DEFAULT_MESH),
        "input_units": specification["units"],
        "T_workpiece_scan_m": transform.tolist(),
        "registration": registration,
        "measured_positive_points": int(measured.sum()),
        "visible_normal_height_mm": distribution(heights[measured]),
        "visible_cross_root_offset_mm": distribution(across),
        "root_to_CAD_max_mm": float(root_distance.max() * 1000),
        "full_thickness_measured": False,
        "unobserved_geometry_imputed": False,
        "simulation_profile_overwritten": False,
        "automatic_robot_command_generated": False,
    }
    write_json(out / "calibration.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", type=Path)
    parser.add_argument("--units", choices=("mm", "m"))
    args = parser.parse_args()
    if args.inspect:
        if not args.units:
            parser.error("--inspect requires explicit --units")
        points = load_cloud(args.inspect, args.units)
        finite = np.isfinite(points).all(axis=1)
        result = {
            "source_sha256": sha256(args.inspect),
            "input_rows": len(points),
            "finite_rows": int(finite.sum()),
            "input_units": args.units,
            "bounds_m": (
                np.vstack(
                    [points[finite].min(0), points[finite].max(0)]
                ).tolist()
                if finite.any()
                else None
            ),
            "part_identity_inferred": False,
        }
    else:
        if not args.manifest or not args.output:
            parser.error(
                "Use --manifest and --output, or --inspect and --units"
            )
        result = calibrate_scan(args.manifest, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
