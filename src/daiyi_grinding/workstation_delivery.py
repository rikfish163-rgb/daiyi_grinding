"""Export a simulation run receipt and independently verify supplied site measurements."""

import csv
import hashlib
import json
from pathlib import Path

from .workstation_model import write_json


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


def site_acceptance(manifest):
    """Score independently measured before/after points against signed tolerances."""
    path = Path(manifest)
    specification = json.loads(path.read_text())
    if specification.get("source_kind") != "real_measurement":
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
    limits = specification["signed_limits_mm"]
    lower, upper = float(limits["lower"]), float(limits["upper"])
    if not (-0.30 <= lower < 0 < upper <= 0.30):
        raise ValueError("型面容差须在项目的 ±0.30 mm 范围内")
    source = (path.parent / specification["measurements_file"]).resolve()
    if (
        hashlib.sha256(source.read_bytes()).hexdigest()
        != specification["measurements_sha256"]
    ):
        raise ValueError("实测 CSV 哈希与验收清单不一致")
    with source.open(newline="", encoding="utf-8-sig") as file:
        measurements = list(csv.DictReader(file))
    if not measurements:
        raise ValueError("至少需要一条同件对应点的磨前及磨后测量")
    zones = specification["required_zones"]
    if (
        not isinstance(zones, list)
        or not zones
        or any(not isinstance(zone, str) or not zone for zone in zones)
        or len(set(zones)) != len(zones)
    ):
        raise ValueError("必须登记需要独立测量的加工和保护区域")
    seen = set()
    failed, covered = [], set()
    for row in measurements:
        identity = (row["zone"], row["point_id"])
        if (
            not all(identity)
            or identity in seen
            or row["serial"] != specification["serial"]
        ):
            raise ValueError("点位须唯一且属于同一件工件")
        seen.add(identity)
        before, after, uncertainty = [
            float(row[k]) for k in ("before_mm", "after_mm", "uncertainty_mm")
        ]
        if (
            not all(
                float("-inf") < v < float("inf")
                for v in (before, after, uncertainty)
            )
            or uncertainty < 0
        ):
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
        "scope": "measured surface conformance; controller and process commissioning are separate",
    }


def main():
    import argparse

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
