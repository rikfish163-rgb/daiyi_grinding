"""Export a simulation run receipt and independently verify supplied site measurements."""

import csv
import hashlib
import json
import math
from pathlib import Path

MEASUREMENT_COLUMNS = (
    "serial",
    "zone",
    "point_id",
    "before_mm",
    "after_mm",
    "uncertainty_mm",
)


def _finite_mm(value):
    """Reject booleans, missing values and nonfinite measurement numbers."""
    if isinstance(value, bool) or value is None:
        raise ValueError("测量与不确定度须为有效毫米数")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("测量与不确定度须为有效毫米数") from error
    if not math.isfinite(number):
        raise ValueError("测量与不确定度须为有效毫米数")
    return number


def _unique_object(pairs):
    """Reject duplicate JSON keys instead of silently using the last one."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"验收清单字段重复：{key}")
        result[key] = value
    return result


def _nonfinite_json(value):
    raise ValueError(f"验收清单包含非有限数：{value}")


def simulation_receipt(directory, result):
    """Record the evidence required to reproduce or disposition a cycle."""
    vision = result["perception"]
    recheck = result.get("reinspection") or {}
    checks = result["preflight"]
    return {
        "schema": "daiyi-station-receipt-v1",
        "evidence_scope": "simulation",
        "controller_program": None,
        "part": result["cad_workpiece"],
        "profile": result["workpiece_profile"],
        "detector": result["detector_kind"],
        "grasp_setup": result["grasp_setup"],
        "fit_and_force_calibrated": False,
        "seconds": {
            "simulation": result["simulation_seconds"],
            "wall": result["wall_seconds"],
            "initial_inspection_wall": vision["seconds"],
            "initial_planning_wall": checks["seconds"],
        },
        "initial": {
            "observed_views": vision["view_count"],
            "generated_candidates": result["path_count"],
            "admitted_segments": result["admitted_path_count"],
            "blocked_by_motion_preflight": checks["blocked_count"],
            "blocked_by_inspection_envelope": vision["review_path_guard"][
                "blocked_paths_count"
            ],
            "transfer_optimization": checks.get("transfer_optimization"),
        },
        "quality": {
            "observational_release": result["observational_release"],
            "geometric_goal_complete": result.get("geometric_goal_complete"),
            "registered_areas": recheck.get("region_reinspection"),
            "simulated_residual_mm": result["final_stock"]["remaining_peak_mm"],
            "motion_gate_passed": result.get("motion_gate_passed"),
            "inspections": len(result["reinspections"]),
            "reason": result["controller_stopping"]["reason"],
            "task_status": result["task_status"],
        },
        "artifacts": [
            name
            for name in (
                "source_hashes.json",
                "registered_regions.json",
                "motion_commands.json",
                "estun_path_preview.csv",
                "execution.csv",
                "after_views.json",
                "result.json",
            )
            if (directory / name).is_file()
        ],
    }


def site_acceptance(manifest, *, expected_manifest_sha256=None):
    """Score real paired surface measurements; process acceptance is separate."""
    return surface_measurement_report(
        manifest,
        allowed_source_kinds={"real_measurement"},
        expected_manifest_sha256=expected_manifest_sha256,
    )


def surface_measurement_report(
    manifest, *, allowed_source_kinds, expected_manifest_sha256=None
):
    """Validate paired measurements with an explicitly restricted source.

    Args:
        manifest: Path to the manifest and its hashed measurement CSV.
        allowed_source_kinds: Sources allowed by the calling workflow. Offline
            rehearsal callers must use only ``synthetic`` and ``example``.
        expected_manifest_sha256: Optional upstream binding to the exact
            manifest bytes already reviewed by an evidence evaluator.

    Returns:
        Signed surface conformance for the declared measurement points. This
        function does not approve controller execution or process commissioning.
        ``zone_roles`` is optional for compatibility with existing site manifests;
        missing roles are explicitly reported as unverified protection coverage.

    Raises:
        ValueError: Identity, provenance, schema or numerical evidence is invalid.
    """
    path = Path(manifest)
    try:
        manifest_bytes = path.read_bytes()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        if (
            expected_manifest_sha256 is not None
            and manifest_digest != expected_manifest_sha256
        ):
            raise ValueError("验收清单哈希在读取后发生变化")
        specification = json.loads(
            manifest_bytes.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_nonfinite_json,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("验收清单不存在或不是有效 JSON") from error
    if not isinstance(specification, dict):
        raise ValueError("验收清单须为 JSON 对象")
    if (
        not isinstance(specification.get("source_kind"), str)
        or specification["source_kind"] not in allowed_source_kinds
    ):
        raise ValueError("现场验收必须使用独立实测记录")
    required_text = (
        "part_id",
        "serial",
        "cad_revision",
        "fixture_revision",
        "measurement_system",
        "controller",
        "reviewer",
    )
    if any(
        not isinstance(specification.get(k), str)
        or not specification[k].strip()
        for k in required_text
    ):
        raise ValueError("缺少工件、工装、测量系统或控制器身份")
    limits = specification.get("signed_limits_mm")
    if not isinstance(limits, dict) or not {"lower", "upper"} <= limits.keys():
        raise ValueError("须明确提供双边型面容差")
    lower, upper = _finite_mm(limits["lower"]), _finite_mm(limits["upper"])
    if not (-0.30 <= lower < 0 < upper <= 0.30):
        raise ValueError("型面容差须在项目的 ±0.30 mm 范围内")
    filename = specification.get("measurements_file")
    expected_hash = specification.get("measurements_sha256")
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(
            character not in "0123456789abcdef" for character in expected_hash
        )
    ):
        raise ValueError("须提供实测 CSV 路径及小写 SHA256")
    source = (path.parent / filename).resolve()
    try:
        source_bytes = source.read_bytes()
    except OSError as error:
        raise ValueError("实测 CSV 不存在或不可读取") from error
    if hashlib.sha256(source_bytes).hexdigest() != expected_hash:
        raise ValueError("实测 CSV 哈希与验收清单不一致")
    # Parse the exact bytes whose hash was checked, even if an upstream writer
    # replaces the CSV while the report is being produced.
    import io

    try:
        reader = csv.DictReader(
            io.StringIO(source_bytes.decode("utf-8-sig"), newline="")
        )
        headers = reader.fieldnames
        if (
            headers is None
            or len(headers) != len(set(headers))
            or not set(MEASUREMENT_COLUMNS) <= set(headers)
        ):
            raise ValueError("实测 CSV 须含全部且不重复的对应点字段")
        measurements = list(reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("实测 CSV 编码或格式无效") from error
    if not measurements:
        raise ValueError("至少需要一条同件对应点的磨前及磨后测量")
    zones = specification.get("required_zones")
    if (
        not isinstance(zones, list)
        or not zones
        or any(
            not isinstance(zone, str)
            or not zone.strip()
            or zone != zone.strip()
            for zone in zones
        )
        or len(set(zones)) != len(zones)
    ):
        raise ValueError("必须登记需要独立测量的加工和保护区域")
    roles = specification.get("zone_roles")
    if roles is not None and (
        not isinstance(roles, dict)
        or set(roles) != set(zones)
        or any(role not in ("machined", "protected") for role in roles.values())
    ):
        raise ValueError("区域角色须与 required_zones 一致并明确加工或保护")
    if specification["serial"] != specification["serial"].strip():
        raise ValueError("工件序列号不能含首尾空白")
    seen = set()
    failed, covered = [], set()
    for row in measurements:
        if None in row or any(
            not isinstance(row.get(key), str) or not row[key].strip()
            for key in MEASUREMENT_COLUMNS
        ):
            raise ValueError("实测 CSV 行不能缺列、多列或包含空对应点字段")
        identity = (row["zone"], row["point_id"])
        if (
            identity in seen
            or row["serial"] != specification["serial"]
            or any(value != value.strip() for value in identity)
        ):
            raise ValueError("点位须唯一且属于同一件工件")
        if row["zone"] not in zones:
            raise ValueError("实测点位属于未登记区域")
        seen.add(identity)
        _before, after, uncertainty = [
            _finite_mm(row[key])
            for key in ("before_mm", "after_mm", "uncertainty_mm")
        ]
        if uncertainty < 0:
            raise ValueError("测量与不确定度须为有效毫米数")
        covered.add(row["zone"])
        if after - uncertainty < lower or after + uncertainty > upper:
            failed.append({"zone": row["zone"], "point_id": row["point_id"]})
    missing = sorted(set(zones) - covered)
    return {
        "part": specification["part_id"],
        "serial": specification["serial"],
        "measured_points": len(seen),
        "missing_zones": missing,
        "out_of_tolerance": failed,
        "signed_limits_mm": limits,
        "surface_measurement_passed": not missing and not failed,
        "source_kind": specification["source_kind"],
        "manifest_sha256": manifest_digest,
        "measurements_file": str(source),
        "measurements_sha256": expected_hash,
        "zone_roles": roles,
        "protection_coverage_verified": bool(roles)
        and "protected" in roles.values()
        and not any(
            zone in missing
            for zone, role in roles.items()
            if role == "protected"
        ),
        "scope": "measured surface conformance; controller and process commissioning are separate",
    }


def main():
    import argparse

    from .workstation_model import write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = site_acceptance(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["surface_measurement_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
