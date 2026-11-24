"""Assess enterprise targets without promoting offline evidence to real acceptance.

All file access is local and read-only except for a newly created output
directory. Rehearsal inputs always declare ``synthetic``; no robot, controller,
network, torch or MuJoCo execution is imported by this module.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .workstation_delivery import site_acceptance, surface_measurement_report

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = ROOT / "config/enterprise_requirements_20261123.json"
BUNDLE_SCHEMA = "daiyi-enterprise-input-v1"
REPORT_SCHEMA = "daiyi-enterprise-assessment-v1"
IDENTITY_FIELDS = ("part_id", "serial", "cad_revision", "fixture_revision")
ROLE_SCHEMAS = {
    "localization": "daiyi-localization-evidence-v1",
    "timing_receipt": "daiyi-time-evidence-v1",
    "controller_receipt": "daiyi-controller-evidence-v1",
    "reliability_receipt": "daiyi-reliability-evidence-v1",
}
REAL_SOURCES = {
    "localization": "real_measurement",
    "site_manifest": "real_measurement",
    "timing_receipt": "real_process_log",
    "controller_receipt": "real_controller_receipt",
    "reliability_receipt": "real_software_log",
}
ROLES = {
    "run_result",
    "cycle_receipt",
    "registered_regions",
    "source_hashes",
    *ROLE_SCHEMAS,
    "site_manifest",
}
CALIBRATIONS = (
    "camera_calibrated",
    "tcp_calibrated",
    "fixture_validated",
    "coordinate_mapping_confirmed",
    "force_control_calibrated",
    "removal_process_calibrated",
)


class EvidenceError(ValueError):
    """A rejected evidence input with a stable, machine-readable reason."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_json_key", f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def _constant(value):
    raise EvidenceError("nonfinite_json", f"JSON 包含非有限数：{value}")


def _json_bytes(content, *, allow_array=False):
    try:
        value = json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=_object,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(
            "invalid_json", "文件不是有效 UTF-8 JSON"
        ) from error
    if allow_array and isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise EvidenceError("invalid_json_object", "JSON 顶层必须为对象")
    return value


def _number(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError("invalid_number", f"{name} 须为有限且有效的数值")
    try:
        number = float(value)
    except OverflowError as error:
        raise EvidenceError(
            "invalid_number", f"{name} 超出有效数值范围"
        ) from error
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise EvidenceError("invalid_number", f"{name} 须为有限且有效的数值")
    return number


def _count(value, name):
    number = _number(value, name, minimum=0)
    if number != int(number):
        raise EvidenceError("invalid_count", f"{name} 须为非负整数")
    return int(number)


def _text(value, name):
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise EvidenceError("missing_identity", f"{name} 缺失或含首尾空白")
    return value


def _hash(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceError("invalid_sha256", f"{name} 须为小写 SHA256")
    return value


def _write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def artifact_reference(path, *, relative_to=None):
    """Pin a local artifact by its bytes without claiming source authenticity."""
    path = Path(path).resolve()
    name = str(path if relative_to is None else path.relative_to(relative_to))
    return {
        "path": name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _read_reference(reference, base, role, inputs):
    if not isinstance(reference, dict):
        raise EvidenceError("invalid_reference", f"{role} 须提供路径及哈希")
    path = (base / _text(reference.get("path"), f"{role}.path")).resolve()
    expected = _hash(reference.get("sha256"), f"{role}.sha256")
    entry = {
        "role": role,
        "path": str(path),
        "expected_sha256": expected,
        "sha256": None,
        "origin": "local supplied artifact; declared provenance is not authenticated",
        "integrity": "missing",
    }
    inputs.append(entry)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise EvidenceError(
            "missing_artifact", f"{role} 文件不可读取"
        ) from error
    actual = hashlib.sha256(content).hexdigest()
    entry.update(
        sha256=actual, integrity="pass" if actual == expected else "fail"
    )
    if actual != expected:
        raise EvidenceError("hash_mismatch", f"{role} 哈希与绑定输入不一致")
    return path, content, entry


def load_requirements(path=DEFAULT_REQUIREMENTS):
    """Load pinned target sources and reject a weakened enterprise threshold."""
    path = Path(path).resolve()
    try:
        content = path.read_bytes()
        requirements = _json_bytes(content)
    except OSError as error:
        raise EvidenceError("missing_requirements", str(path)) from error
    if requirements.get("schema") != "daiyi-enterprise-requirements-v1":
        raise EvidenceError("invalid_requirements", "要求配置 schema 不匹配")
    rows = requirements.get("requirements")
    if not isinstance(rows, list) or not rows:
        raise EvidenceError("invalid_requirements", "要求配置缺少指标列表")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceError("invalid_requirements", "指标须为对象")
        name = _text(row.get("id"), "requirement.id")
        if name in indexed:
            raise EvidenceError("invalid_requirements", "指标 ID 重复")
        if row.get("kind") not in (
            "enterprise_target",
            "offline_gate",
            "diagnostic",
        ):
            raise EvidenceError("invalid_requirements", "指标 kind 不匹配")
        indexed[name] = row
    required_ids = {
        "source_integrity",
        "defect_binary",
        "defect_category",
        "localization",
        "motion_tracking",
        "simulated_residual",
        "signed_surface",
        "simulation_cycle",
        "controller_commissioning",
        "changeover",
        "new_product_occupancy",
        "adaptive_changeover",
        "continuous_reliability",
        "virtual_reliability",
    }
    if set(indexed) != required_ids:
        raise EvidenceError(
            "invalid_requirements", "指标列表缺失或包含未知指标"
        )
    enterprise_ids = {
        "localization",
        "signed_surface",
        "controller_commissioning",
        "changeover",
        "new_product_occupancy",
        "adaptive_changeover",
        "continuous_reliability",
    }
    for name, row in indexed.items():
        expected_kind = (
            "enterprise_target"
            if name in enterprise_ids
            else "diagnostic"
            if name == "defect_binary"
            else "offline_gate"
        )
        if row["kind"] != expected_kind:
            raise EvidenceError(
                "invalid_requirements", "不能改变指标的证据层级"
            )
    if indexed["defect_binary"].get("target") is not None:
        raise EvidenceError(
            "invalid_requirements", "来源未约定二值 F1 验收目标"
        )
    try:
        loc = indexed["localization"]["target"]
        surface = indexed["signed_surface"]["target"]
        if not 0 < _number(loc["maximum_error_mm"], "定位公差") <= 0.1:
            raise EvidenceError("weakened_target", "不能放宽 ±0.1 mm 定位目标")
        if not (
            -0.3
            <= _number(surface["lower_mm"], "型面下界")
            < 0
            < _number(surface["upper_mm"], "型面上界")
            <= 0.3
        ):
            raise EvidenceError("weakened_target", "不能放宽 ±0.3 mm 型面目标")
        for name, maximum in (
            ("changeover", 1800),
            ("new_product_occupancy", 7200),
        ):
            if (
                not 0
                < _number(indexed[name]["target"]["maximum_seconds"], name)
                <= maximum
            ):
                raise EvidenceError("weakened_target", f"不能放宽 {name} 目标")
        for name, field in (
            ("continuous_reliability", "minimum_hours"),
            ("virtual_reliability", "minimum_virtual_hours"),
        ):
            if _number(indexed[name]["target"][field], name) < 800:
                raise EvidenceError("weakened_target", "不能缩短 800 小时目标")
            gap = _number(
                indexed[name]["target"]["maximum_heartbeat_gap_seconds"],
                "心跳间隔",
                minimum=0,
            )
            if not 0 < gap <= 3600:
                raise EvidenceError(
                    "weakened_target", "不能扩大本地连续日志的心跳间隔"
                )
        if (
            not 0
            < _number(
                indexed["motion_tracking"]["target"]["p95_strictly_below_mm"],
                "运动门槛",
            )
            <= 1
        ):
            raise EvidenceError("weakened_target", "不能放宽现有运动门槛")
        if (
            not 0
            < _number(
                indexed["simulated_residual"]["target"][
                    "maximum_positive_residual_mm"
                ],
                "仿真余料门槛",
            )
            <= 0.3
        ):
            raise EvidenceError("weakened_target", "不能放宽仿真余料门槛")
        if set(loc["required_defect_classes"]) != {
            "flash",
            "burr",
            "casting_root",
        }:
            raise EvidenceError("weakened_target", "不能删去愿景中的缺陷类别")
    except (KeyError, TypeError) as error:
        raise EvidenceError(
            "invalid_requirements", "要求配置 target 不完整"
        ) from error
    requirements["_indexed"] = indexed
    requirements["_path"] = str(path)
    requirements["_sha256"] = hashlib.sha256(content).hexdigest()
    return requirements


def _row(
    requirements,
    name,
    evidence_status="unknown",
    measured=None,
    *,
    real=False,
    reason="缺少相应证据",
    input_roles=(),
):
    definition = requirements["_indexed"][name]
    enterprise = definition["kind"] == "enterprise_target"
    return {
        **definition,
        "status": evidence_status if not enterprise or real else "unknown",
        "evidence_status": evidence_status,
        "real_evidence": real,
        "measured": measured,
        "input_roles": list(input_roles),
        "reason": reason,
    }


def _identity(data, role):
    if role == "run_result":
        return {"part_id": _text(data.get("cad_workpiece"), "cad_workpiece")}
    if role == "cycle_receipt":
        return {"part_id": _text(data.get("part"), "part")}
    if role in ("source_hashes", "registered_regions"):
        return {}
    return {field: _text(data.get(field), field) for field in IDENTITY_FIELDS}


def _validate_role(data, role):
    if role == "registered_regions":
        if not isinstance(data, list):
            raise EvidenceError(
                "invalid_registered_regions", "原登记区域须为锚点数组"
            )
        _registered_anchors(data)
        return "archived_CAD_region_registry; no independent measurement"
    if role == "run_result":
        if (
            data.get("scene") != "workstation"
            or data.get("schema_version") != 3
        ):
            raise EvidenceError(
                "unsupported_run", "仅支持当前 workstation v3 结果"
            )
        return "simulation"
    if role == "cycle_receipt":
        if (
            data.get("schema") != "daiyi-station-receipt-v1"
            or data.get("evidence_scope") != "simulation"
        ):
            raise EvidenceError("unsupported_receipt", "运行回执须明确为仿真")
        return "simulation"
    if role == "source_hashes":
        if not data:
            raise EvidenceError("empty_source_hashes", "源码绑定不能为空")
        for name, digest in data.items():
            _text(name, "source_name")
            if Path(name).name != name:
                raise EvidenceError(
                    "invalid_source_name", "源码绑定名称须为文件名"
                )
            _hash(digest, name)
        return "archived_source_hashes"
    kind = data.get("source_kind")
    allowed = ("synthetic", "example", REAL_SOURCES[role])
    if role == "reliability_receipt":
        allowed += ("virtual_reliability",)
    if kind not in allowed:
        raise EvidenceError("invalid_source_kind", f"{role} 来源种类无效")
    if role in ROLE_SCHEMAS and data.get("schema") != ROLE_SCHEMAS[role]:
        raise EvidenceError("unsupported_schema", f"{role} schema 不匹配")
    return kind


def _registered_anchors(regions):
    """Retain the independently archived IDs and original anchor counts."""
    anchors = {}
    for region in regions:
        if not isinstance(region, dict):
            raise EvidenceError(
                "invalid_registered_regions", "登记区域须为对象"
            )
        identifier = _count(region.get("id"), "登记区域 ID")
        if not identifier or identifier in anchors:
            raise EvidenceError(
                "invalid_registered_regions", "登记区域 ID 须为不重复的正整数"
            )
        kind = _text(region.get("kind"), "登记区域类别")
        points, normals = region.get("points"), region.get("normals")
        if (
            not isinstance(points, list)
            or not points
            or not isinstance(normals, list)
            or len(points) != len(normals)
        ):
            raise EvidenceError(
                "invalid_registered_regions",
                "登记区域须保留配对的原始锚点/法向",
            )
        for point, normal in zip(points, normals):
            for vector in (point, normal):
                if not isinstance(vector, list) or len(vector) != 3:
                    raise EvidenceError(
                        "invalid_registered_regions",
                        "锚点与法向须为三维有限向量",
                    )
                for coordinate in vector:
                    _number(coordinate, "登记区域坐标")
            if not any(normal):
                raise EvidenceError(
                    "invalid_registered_regions", "登记区域法向不能为零"
                )
        anchors[identifier] = {"anchors": len(points), "kind": kind}
    return anchors


def _motion_row(data, requirements):
    """Validate feedback independently of perception and cycle diagnostics."""
    executed = data.get("robot_executed")
    if not isinstance(executed, bool):
        raise EvidenceError(
            "invalid_execution_flag", "robot_executed 须为布尔值"
        )
    executions = data.get("executions")
    if not isinstance(executions, list) or bool(executions) != executed:
        raise EvidenceError(
            "missing_execution_evidence", "执行标志与逐轮反馈不一致"
        )
    limit = requirements["_indexed"]["motion_tracking"]["target"][
        "p95_strictly_below_mm"
    ]
    traces, surfaces, contacts = [], [], 0
    for item in executions:
        if not isinstance(item, dict):
            raise EvidenceError("invalid_execution", "逐轮反馈须为对象")
        traces.append(
            _number(item.get("tracking_trace_p95_mm"), "抓手P95", minimum=0)
        )
        surfaces.append(
            _number(item.get("surface_tracking_p95_mm"), "加工点P95", minimum=0)
        )
        contacts += _count(item.get("unexpected_contact_count"), "非预期接触")
    motion_pass = (
        bool(executions)
        and max(traces) < limit
        and max(surfaces) < limit
        and contacts == 0
    )
    if executed and data.get("motion_gate_passed") is not motion_pass:
        raise EvidenceError(
            "motion_receipt_disagreement", "声明运动门槛与逐轮反馈不一致"
        )
    return _row(
        requirements,
        "motion_tracking",
        ("pass" if motion_pass else "fail") if executed else "unknown",
        {
            "trace_p95_max_mm": max(traces, default=None),
            "surface_p95_max_mm": max(surfaces, default=None),
            "unexpected_contacts": contacts,
            "executed_passes": len(executions),
        },
        reason="实际反馈对目标关节曲线的 FK；无执行时不能声称运动通过",
        input_roles=("run_result", "motion_feedback"),
    )


def _observation_gate(data, receipt, registered_regions, requirements):
    """Check raw, current-round observations without filling legacy fields."""
    reasons = []

    def hold(code):
        if code not in reasons:
            reasons.append(code)

    initial_views = _count(data["perception"].get("view_count"), "初检视角")
    if initial_views < 6:
        hold("initial_six_views_missing")
    registry = (
        _registered_anchors(registered_regions)
        if registered_regions is not None
        else {}
    )
    if not registry:
        hold("original_region_registry_missing")
    final = data.get("reinspection")
    rounds = data.get("reinspections")
    executions = data.get("executions")
    if (
        not isinstance(final, dict)
        or not isinstance(rounds, list)
        or not rounds
        or not isinstance(executions, list)
        or len(rounds) != len(executions)
        or rounds[-1] != final
    ):
        hold("current_reinspection_round_missing_or_mismatched")
    final_report = (
        final.get("region_reinspection") if isinstance(final, dict) else None
    )
    if receipt is not None and (
        not isinstance(receipt.get("quality"), dict)
        or receipt["quality"].get("registered_areas") != final_report
    ):
        hold("registered_quality_receipt_disagreement")
    if receipt is not None and isinstance(receipt.get("initial"), dict):
        if receipt["initial"].get("observed_views") != initial_views:
            hold("initial_view_receipt_disagreement")
    checked_rounds = rounds if isinstance(rounds, list) and rounds else [final]
    previous_revision = None
    final_views = None
    lower_limit = requirements["_indexed"]["signed_surface"]["target"][
        "lower_mm"
    ]
    upper_limit = requirements["_indexed"]["signed_surface"]["target"][
        "upper_mm"
    ]
    for index, scan in enumerate(checked_rounds):
        if not isinstance(scan, dict):
            hold("missing_reinspection_report")
            continue
        is_final = index == len(checked_rounds) - 1
        views = _count(scan.get("view_count"), "复检视角")
        if is_final:
            final_views = views
        if views < 6:
            hold("reinspection_six_views_missing")
        revision = _count(scan.get("revision"), "复检 revision")
        if (
            _count(scan.get("pass_index"), "复检轮次") != index
            or revision
            != _count(scan.get("prediction_revision"), "预测 revision")
            or (previous_revision is not None and revision <= previous_revision)
            or (
                is_final
                and revision
                != _count(data.get("prediction_revision"), "最终 revision")
            )
        ):
            hold("reinspection_round_identity_mismatch")
        previous_revision = revision
        report = scan.get("region_reinspection")
        if not isinstance(report, dict) or not isinstance(
            report.get("regions"), list
        ):
            hold("missing_reinspection_report")
            continue
        if report.get("coverage_scope") != "six_view":
            hold("reinspection_coverage_incomplete")
        if report.get("truth_used_for_decision") is not False:
            hold("missing_observation_decision_provenance")
        for key in ("overgrind", "negative_limit_risk"):
            if key not in report:
                hold("legacy_signed_proof_missing")
            elif _count(report[key], key):
                hold(key)
        invalid = report.get("invalid_observations")
        if not isinstance(invalid, list):
            hold("legacy_signed_proof_missing")
        elif invalid:
            hold("invalid_observations")
        if report.get("automatic_regrind_allowed") is not True:
            hold("automatic_regrind_hold")
        regions = report["regions"]
        observed = {}
        for region in regions:
            if not isinstance(region, dict):
                hold("invalid_reinspection_region")
                continue
            identifier = _count(region.get("id"), "复检区域 ID")
            if not identifier or identifier in observed:
                hold("duplicate_or_invalid_reinspection_region")
            observed[identifier] = region
            for key in ("overgrind", "negative_limit_risk"):
                if not isinstance(region.get(key), bool):
                    hold("legacy_signed_proof_missing")
                elif region[key]:
                    hold(key)
            for field, limit, code in (
                ("trough_mm", lower_limit, "overgrind"),
                ("lower_peak_mm", lower_limit, "negative_limit_risk"),
                ("peak_mm", upper_limit, "region_observation_hold"),
                ("upper_peak_mm", upper_limit, "region_observation_hold"),
            ):
                if field not in region:
                    hold("legacy_signed_proof_missing")
                elif region[field] is None:
                    if is_final:
                        hold("region_observation_hold")
                else:
                    value = _number(region[field], field)
                    negative = field in ("trough_mm", "lower_peak_mm")
                    if (negative and value < limit) or (
                        not negative and is_final and value > limit
                    ):
                        hold(code)
            anchors = _count(region.get("anchors"), "复检锚点")
            measured = _count(region.get("observed_anchors"), "已观测锚点")
            if measured > anchors:
                hold("invalid_anchor_counts")
            if identifier in registry and (
                anchors != registry[identifier]["anchors"]
                or region.get("kind") != registry[identifier]["kind"]
            ):
                hold("original_registered_anchors_changed")
            bounds = region.get("anchor_bounds_mm")
            if not isinstance(bounds, list) or len(bounds) != anchors:
                hold("legacy_signed_proof_missing")
            else:
                for bound in bounds:
                    if (
                        not isinstance(bound, list)
                        or len(bound) != 2
                        or bound[0] is None
                        or bound[1] is None
                    ):
                        if is_final:
                            hold("region_observation_hold")
                        continue
                    lower = _number(bound[0], "锚点下界")
                    upper = _number(bound[1], "锚点上界")
                    if lower > upper:
                        hold("invalid_signed_anchor_bounds")
                    if lower < lower_limit:
                        hold("negative_limit_risk")
                    if is_final and upper > upper_limit:
                        hold("region_observation_hold")
            if is_final and (
                region.get("status") != "verified"
                or not anchors
                or measured != anchors
                or region.get("positive_excess") is True
                or (
                    "anchor_coverage" in region
                    and _number(region["anchor_coverage"], "锚点覆盖率") != 1
                )
            ):
                hold("region_observation_hold")
        if set(registry) - set(observed):
            hold("original_registered_regions_missing")
        if is_final:
            if (
                not regions
                or _count(report.get("verified"), "通过区域") != len(regions)
                or report.get("observational_release") is not True
                or _count(scan.get("remaining_regions"), "剩余区域") != 0
            ):
                hold("region_observation_hold")
            for key in ("needs_rework", "uncertain", "insufficient"):
                if key not in report:
                    hold("legacy_quality_summary_incomplete")
                elif _count(report[key], key):
                    hold("region_observation_hold")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "initial_view_count": initial_views,
        "final_view_count": final_views,
        "registered_region_count": len(registry),
        "signed_evidence_filled_from_truth": False,
    }


def _run_rows(data, receipt, requirements, motion, registered_regions=None):
    metrics = data["perception"]["metrics"]
    tp, fp, fn, tn = [
        _count(metrics.get(key), f"binary.{key}")
        for key in ("tp", "fp", "fn", "tn")
    ]
    denominator = 2 * tp + fp + fn
    binary = {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "f1": 2 * tp / denominator if denominator else None,
        "scope": "pooled simulated pixels; repeated views are correlated",
    }
    generated = _count(data["perception"].get("generated_defects"), "generated")
    matched = _count(metrics.get("matched_defect_count"), "matched")
    visible = _count(metrics.get("visible_defect_count"), "visible")
    if not 0 <= matched <= visible <= generated:
        raise EvidenceError(
            "inconsistent_object_counts", "匹配/可见/生成数量不一致"
        )
    category = {"generated": generated, "visible": visible, "matched": matched}
    rows = [
        _row(
            requirements,
            "defect_binary",
            measured=binary,
            reason="F1 为二值诊断；来源未约定百分比验收目标，也不是毫米定位精度",
            input_roles=("run_result",),
        ),
        _row(
            requirements,
            "defect_category",
            "pass" if matched == generated else "fail",
            category,
            reason="逐生成实例的正确类别匹配；不可用二值 F1 代替",
            input_roles=("run_result",),
        ),
    ]
    executed = data.get("robot_executed")
    if not isinstance(executed, bool):
        raise EvidenceError(
            "invalid_execution_flag", "robot_executed 须为布尔值"
        )
    motion_pass = motion["evidence_status"] == "pass"
    stock = data.get("final_stock")
    if not isinstance(stock, dict):
        raise EvidenceError("missing_stock", "缺少仿真正余料统计")
    residual = _number(stock.get("remaining_peak_mm"), "正余料峰值", minimum=0)
    residual_pass = (
        residual
        <= requirements["_indexed"]["simulated_residual"]["target"][
            "maximum_positive_residual_mm"
        ]
    )
    rows.append(
        _row(
            requirements,
            "simulated_residual",
            "pass" if residual_pass else "fail",
            {"positive_peak_mm": residual, "signed_overcut_measured": False},
            reason="合成余料正峰值；名义 CAD 不变，无法证明真实过磨与双边型面精度",
            input_roles=("run_result",),
        )
    )
    candidates = _count(data.get("path_count"), "候选路径")
    observed_views = _count(data["perception"].get("view_count"), "观测视角")
    normal = (
        data.get("case") in ("normal", "casting_normal")
        and generated == 0
        and fp == 0
        and fn == 0
        and candidates == 0
        and not executed
        and observed_views >= 6
        and not registered_regions
    )
    observation = (
        _observation_gate(data, receipt, registered_regions, requirements)
        if executed
        else None
    )
    full = (
        executed
        and data.get("goal_complete") is True
        and data.get("observational_release") is True
        and data.get("geometric_goal_complete") is True
        and motion_pass
        and residual_pass
        and matched == generated
        and observation["passed"]
    )
    if receipt is not None:
        quality = receipt.get("quality")
        seconds = receipt.get("seconds")
        if not isinstance(quality, dict) or not isinstance(seconds, dict):
            raise EvidenceError(
                "incomplete_cycle_receipt", "回执缺少质量或计时"
            )
        for key in (
            "motion_gate_passed",
            "observational_release",
            "geometric_goal_complete",
            "task_status",
        ):
            optional_no_motion = (
                not executed
                and key in ("motion_gate_passed", "geometric_goal_complete")
                and key not in data
            )
            value = None if optional_no_motion else data.get(key)
            if (
                key not in quality
                or (key not in data and not optional_no_motion)
                or quality[key] != value
                or type(quality[key]) is not type(value)
            ):
                raise EvidenceError(
                    "cycle_receipt_disagreement", f"回执 {key} 与运行结果不一致"
                )
        if (
            _number(quality.get("simulated_residual_mm"), "回执余料", minimum=0)
            != residual
        ):
            raise EvidenceError(
                "cycle_receipt_disagreement", "回执余料与运行结果不一致"
            )
        for key, result_key in (
            ("wall", "wall_seconds"),
            ("simulation", "simulation_seconds"),
        ):
            if _number(seconds.get(key), key, minimum=0) != _number(
                data.get(result_key), result_key, minimum=0
            ):
                raise EvidenceError(
                    "cycle_receipt_disagreement", "回执时钟与运行结果不一致"
                )
    rows.append(
        _row(
            requirements,
            "simulation_cycle",
            ("pass" if normal or full else "fail")
            if receipt is not None
            else "unknown",
            {
                "normal_no_action_control": normal,
                "nontrivial_grinding_complete": full,
                "observation_gate": observation,
                "observational_release": data.get("observational_release"),
                "reported_task_status": data.get("task_status"),
                "stopping_reason": (data.get("controller_stopping") or {}).get(
                    "reason"
                ),
                "run_wall_seconds": data.get("wall_seconds"),
                "simulation_seconds": data.get("simulation_seconds"),
                "initial_candidates": candidates,
                "initial_admitted": data.get("admitted_path_count"),
                "preflight_blocked_count": (data.get("preflight") or {}).get(
                    "blocked_count"
                ),
                "inspection_protection_holds": (
                    data["perception"].get("review_path_guard") or {}
                ).get("blocked_paths_count"),
                "reinspection_regions": (
                    (data.get("reinspection") or {}).get("region_reinspection")
                    or {}
                ).get("regions"),
                "reported_region_quality": (
                    (data.get("reinspection") or {}).get("region_reinspection")
                ),
                "preflight_rejections": [
                    row
                    for row in (data.get("preflight") or {}).get("rows", [])
                    if row.get("passed") is False
                    or row.get("admitted") is False
                    or row.get("status")
                    in ("blocked", "partial", "rejected", "failed")
                ],
            },
            reason="正常无动作对照与加工闭环分别计数；运行墙钟不等于现场换型/占机",
            input_roles=("run_result", "cycle_receipt", "registered_regions"),
        )
    )
    return rows


def _localization_row(data, requirements):
    target = requirements["_indexed"]["localization"]["target"]
    if data.get("units") != "mm":
        raise EvidenceError("invalid_units", "定位坐标单位须明确为 mm")
    _text(data.get("coordinate_frame"), "coordinate_frame")
    _text(data.get("measurement_system"), "measurement_system")
    _text(data.get("reviewer"), "reviewer")
    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        raise EvidenceError(
            "missing_localization_samples", "缺少独立定位对应点"
        )
    errors, covered, seen = [], set(), set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise EvidenceError("invalid_localization_sample", "对应点须为对象")
        identity = (
            _text(sample.get("zone"), "zone"),
            _text(sample.get("point_id"), "point_id"),
        )
        if identity in seen:
            raise EvidenceError("duplicate_point", "定位对应点重复")
        seen.add(identity)
        defect_class = _text(sample.get("defect_class"), "defect_class")
        if defect_class not in target["required_defect_classes"]:
            raise EvidenceError(
                "unknown_defect_class", "定位类别不在企业目标内"
            )
        covered.add(defect_class)
        coordinates = []
        for name in ("reference_mm", "predicted_mm"):
            values = sample.get(name)
            if not isinstance(values, list) or len(values) != 3:
                raise EvidenceError(
                    "invalid_coordinates", "定位对应点须为三维毫米坐标"
                )
            coordinates.append([_number(value, name) for value in values])
        uncertainty = _number(
            sample.get("uncertainty_mm"), "定位不确定度", minimum=0
        )
        distance = math.dist(*coordinates)
        errors.append(
            {
                "zone": identity[0],
                "point_id": identity[1],
                "defect_class": defect_class,
                "error_mm": distance,
                "upper_error_mm": distance + uncertainty,
            }
        )
    maximum = max(item["upper_error_mm"] for item in errors)
    missing = sorted(set(target["required_defect_classes"]) - covered)
    sufficient = (
        not missing
        and data.get("coverage_complete") is True
        and data.get("independent_reference") is True
        and data.get("calibration_verified") is True
    )
    state = (
        "pass"
        if sufficient and maximum <= target["maximum_error_mm"]
        else "fail"
    )
    return _row(
        requirements,
        "localization",
        state,
        {
            "sample_count": len(errors),
            "maximum_upper_error_mm": maximum,
            "missing_defect_classes": missing,
            "samples": errors,
            "reference_and_coverage_verified": sufficient,
        },
        real=data["source_kind"] == "real_measurement" and sufficient,
        reason="三维误差+不确定度；独立参考、标定和目标类别覆盖须明确",
        input_roles=("localization",),
    )


def _surface_row(path, data, requirements, inputs):
    expected_manifest = next(
        item["sha256"]
        for item in inputs
        if item["role"] == "site_manifest" and item["path"] == str(path)
    )
    if data["source_kind"] == "real_measurement":
        report = site_acceptance(
            path, expected_manifest_sha256=expected_manifest
        )
    else:
        report = surface_measurement_report(
            path,
            allowed_source_kinds={"synthetic", "example"},
            expected_manifest_sha256=expected_manifest,
        )
    inputs.append(
        {
            "role": "paired_surface_csv",
            "path": report["measurements_file"],
            "sha256": report["measurements_sha256"],
            "expected_sha256": data["measurements_sha256"],
            "origin": data["source_kind"],
            "integrity": "pass",
        }
    )
    target = requirements["_indexed"]["signed_surface"]["target"]
    # A narrower manifest tolerance is valid; a wider one cannot override targets.
    limits = report["signed_limits_mm"]
    if (
        float(limits["lower"]) < target["lower_mm"]
        or float(limits["upper"]) > target["upper_mm"]
    ):
        raise EvidenceError(
            "weakened_surface_limits", "量测清单不能放宽型面目标"
        )
    roles = report["zone_roles"] or {}
    protected = report["protection_coverage_verified"]
    machined = "machined" in roles.values()
    state = (
        "pass"
        if report["surface_measurement_passed"] and protected and machined
        else "fail"
    )
    return _row(
        requirements,
        "signed_surface",
        state,
        report,
        real=data["source_kind"] == "real_measurement" and bool(roles),
        reason="双边区间含过磨；加工区/保护区角色或点位缺失均不放行",
        input_roles=("site_manifest", "paired_surface_csv"),
    )


def _timing_rows(data, requirements):
    if data.get("clock_kind") != "wall_monotonic":
        raise EvidenceError(
            "invalid_clock",
            "换型及占机须使用墙钟单调时间，不能使用计划/仿真时间",
        )
    _text(data.get("equipment_id"), "equipment_id")
    before = _text(data.get("product_from"), "product_from")
    after = _text(data.get("product_to"), "product_to")
    if not isinstance(data.get("code_changed"), bool):
        raise EvidenceError(
            "missing_changeover_declaration", "须明确换型是否修改代码"
        )
    events = data.get("events")
    intervals = data.get("occupied_intervals")
    if (
        not isinstance(events, list)
        or not events
        or not isinstance(intervals, list)
        or not intervals
    ):
        raise EvidenceError(
            "missing_timing_intervals", "缺少换型事件或新品实际占机区间"
        )
    times, previous = {}, -math.inf
    for event in events:
        if not isinstance(event, dict):
            raise EvidenceError("invalid_event", "事件须为对象")
        name = _text(event.get("event"), "event")
        stamp = _number(event.get("t_s"), "墙钟时间", minimum=0)
        if name in times or stamp <= previous:
            raise EvidenceError(
                "invalid_event_order", "事件须唯一且墙钟严格递增"
            )
        times[name], previous = stamp, stamp
    if set(times) != {
        "changeover_start",
        "changeover_ready",
        "new_product_start",
        "new_product_ready",
    }:
        raise EvidenceError(
            "incomplete_timing_events", "须登记换型与新品的开始和完成"
        )
    if (
        times["changeover_ready"] <= times["changeover_start"]
        or times["new_product_ready"] <= times["new_product_start"]
    ):
        raise EvidenceError("invalid_timing_range", "结束事件须晚于开始")
    occupied = []
    for interval in intervals:
        if not isinstance(interval, dict):
            raise EvidenceError("invalid_occupancy", "占机区间须为对象")
        start = _number(interval.get("start_s"), "占机开始", minimum=0)
        end = _number(interval.get("end_s"), "占机结束", minimum=0)
        if (
            not times["new_product_start"]
            <= start
            < end
            <= times["new_product_ready"]
        ):
            raise EvidenceError(
                "invalid_occupancy", "占机区间须位于新品导入墙钟范围内"
            )
        occupied.append((start, end))
    merged = []
    for start, end in sorted(occupied):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    changeover = times["changeover_ready"] - times["changeover_start"]
    occupancy = sum(end - start for start, end in merged)
    complete = (
        data.get("complete") is True
        and data.get("first_good_part_verified") is True
    )
    real = data["source_kind"] == "real_process_log" and complete
    rows = []
    for name, seconds in (
        ("changeover", changeover),
        ("new_product_occupancy", occupancy),
    ):
        state = (
            "pass"
            if complete
            and seconds
            <= requirements["_indexed"][name]["target"]["maximum_seconds"]
            else "fail"
        )
        rows.append(
            _row(
                requirements,
                name,
                state,
                {
                    "wall_seconds": seconds,
                    "clock_kind": data["clock_kind"],
                    "phase_complete": complete,
                    "occupied_interval_union": merged
                    if name == "new_product_occupancy"
                    else None,
                },
                real=real,
                reason="完整墙钟事件；新品占机使用同一设备区间并集",
                input_roles=("timing_receipt",),
            )
        )
    rows.append(
        _row(
            requirements,
            "adaptive_changeover",
            "pass"
            if complete and before != after and data["code_changed"] is False
            else "fail",
            {
                "product_from": before,
                "product_to": after,
                "code_changed": data["code_changed"],
            },
            real=real,
            reason="不同产品的实际换型完成并明确代码未改动",
            input_roles=("timing_receipt",),
        )
    )
    return rows


def _controller_row(data, base, requirements, inputs):
    for key in ("request_id", "execution_id", "controller_id"):
        _text(data.get(key), key)
    plan_hash = _hash(data.get("plan_sha256"), "plan_sha256")
    _, plan, _ = _read_reference(
        data.get("plan"), base, "controller_plan", inputs
    )
    if hashlib.sha256(plan).hexdigest() != plan_hash:
        raise EvidenceError(
            "controller_plan_mismatch", "控制回执与计划文件哈希不一致"
        )
    _, program, _ = _read_reference(
        data.get("program"), base, "controller_program", inputs
    )
    feedback_path, content, entry = _read_reference(
        data.get("feedback"), base, "controller_feedback", inputs
    )
    feedback = _json_bytes(content)
    if feedback.get("schema") != "daiyi-controller-feedback-v1":
        raise EvidenceError(
            "invalid_controller_feedback", "控制反馈 schema 不匹配"
        )
    if feedback.get("source_kind") != data["source_kind"]:
        raise EvidenceError(
            "feedback_source_mismatch", "反馈来源与回执来源不一致"
        )
    entry["origin"] = feedback["source_kind"]
    for key in (*IDENTITY_FIELDS, "request_id", "execution_id"):
        if feedback.get(key) != data.get(key):
            raise EvidenceError(
                "mixed_controller_feedback", f"控制反馈 {key} 身份不一致"
            )
    actual_program = hashlib.sha256(program).hexdigest()
    if (
        feedback.get("program_sha256") != actual_program
        or feedback.get("plan_sha256") != data["plan_sha256"]
    ):
        raise EvidenceError(
            "controller_binding_mismatch", "控制反馈未绑定同一程序和计划"
        )
    samples = feedback.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise EvidenceError("missing_controller_feedback", "缺少逐时刻控制反馈")
    previous, contacts = -math.inf, 0
    for sample in samples:
        if not isinstance(sample, dict):
            raise EvidenceError(
                "invalid_controller_feedback", "控制反馈样本须为对象"
            )
        stamp = _number(sample.get("t_s"), "反馈时间", minimum=0)
        if stamp <= previous:
            raise EvidenceError(
                "controller_clock_regression", "反馈时间须严格递增"
            )
        previous = stamp
        contacts += _count(sample.get("unexpected_contacts"), "反馈接触")
    declared = _count(data.get("unexpected_contacts"), "回执接触")
    if contacts != declared:
        raise EvidenceError(
            "controller_contact_mismatch", "反馈与回执接触数量不一致"
        )
    calibrated = all(data.get(key) is True for key in CALIBRATIONS)
    passed = (
        data.get("completed") is True
        and feedback.get("completed") is True
        and contacts == 0
        and calibrated
    )
    return _row(
        requirements,
        "controller_commissioning",
        "pass" if passed else "fail",
        {
            "completed": data.get("completed"),
            "feedback_completed": feedback.get("completed"),
            "calibrations": {key: data.get(key) for key in CALIBRATIONS},
            "unexpected_contacts": contacts,
            "feedback_samples": len(samples),
            "feedback_path": str(feedback_path),
            "program_sha256": actual_program,
        },
        real=data["source_kind"] == "real_controller_receipt",
        reason="校验回执和反馈绑定；本评估器不会下发或执行控制程序",
        input_roles=(
            "controller_receipt",
            "controller_plan",
            "controller_program",
            "controller_feedback",
        ),
    )


def _reliability_rows(data, requirements):
    boot_id = _text(data.get("boot_id"), "boot_id")
    real = data["source_kind"] == "real_software_log"
    expected_clock = "wall_monotonic" if real else "virtual"
    if data.get("clock_kind") != expected_clock:
        raise EvidenceError(
            "reliability_clock_mismatch", "真实可靠性与虚拟可靠性时钟不能混用"
        )
    events = data.get("heartbeats")
    if not isinstance(events, list) or len(events) < 2:
        raise EvidenceError("missing_reliability_trace", "缺少连续可靠性心跳")
    stamps, failures = [], []
    for event in events:
        if not isinstance(event, dict) or not isinstance(
            event.get("healthy"), bool
        ):
            raise EvidenceError("unknown_health", "每个心跳须明确健康状态")
        try:
            event_boot = _text(event.get("boot_id"), "heartbeat.boot_id")
        except EvidenceError as error:
            raise EvidenceError(
                "missing_heartbeat_boot_id", "每个心跳须保留明确的同次启动身份"
            ) from error
        if event_boot != boot_id:
            raise EvidenceError(
                "mixed_reliability_boot",
                "不能用不同启动或重启后的心跳拼接连续运行",
            )
        stamp = _number(event.get("t_s"), "心跳时间", minimum=0)
        if stamps and stamp <= stamps[-1]:
            raise EvidenceError(
                "reliability_restart",
                "同次启动时间回退或重复，不能拼接重启运行",
            )
        stamps.append(stamp)
        if event["healthy"] is False:
            failures.append(
                {"t_s": stamp, "reason": _text(event.get("reason"), "故障原因")}
            )
    elapsed = stamps[-1] - stamps[0]
    gap = max(after - before for before, after in zip(stamps, stamps[1:]))
    name = "continuous_reliability" if real else "virtual_reliability"
    target = requirements["_indexed"][name]["target"]
    minimum = target["minimum_hours" if real else "minimum_virtual_hours"]
    complete = data.get("complete") is True
    if real:
        try:
            start = datetime.fromisoformat(data["wall_started_at"])
            end = datetime.fromisoformat(data["wall_finished_at"])
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("缺少时区")
            if abs((end - start).total_seconds() - elapsed) > 1:
                raise ValueError("墙钟与单调计时不一致")
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceError(
                "invalid_wall_reliability", "真实可靠性需一致的带时区墙钟起止"
            ) from error
    passed = (
        complete
        and not failures
        and gap <= target["maximum_heartbeat_gap_seconds"]
        and elapsed / 3600 >= minimum
    )
    measured = {
        "elapsed_hours": elapsed / 3600,
        "clock_kind": data["clock_kind"],
        "boot_id": boot_id,
        "heartbeat_boot_ids_verified": True,
        "heartbeat_count": len(events),
        "maximum_gap_seconds": gap,
        "failures": failures,
        "complete": complete,
        "scope": "continuous real software log"
        if real
        else "synthetic virtual clock trace; not 800 wall hours",
    }
    return [
        _row(
            requirements,
            name,
            "pass" if passed else "fail",
            measured,
            real=real,
            reason=measured["scope"],
            input_roles=("reliability_receipt",),
        )
    ]


def _source_audit(hashes, base, inputs):
    verified, unresolved = [], []
    fixed = {
        "scene.xml": ROOT / "assets/ier220_workstation/scene.xml",
        "wp_097_c0.stl": ROOT
        / "assets/ier220_workstation/meshes/workpiece/wp_097_c0.stl",
        "learned_detector.json": ROOT / "config/learned_detector.json",
    }
    for name, digest in hashes.items():
        path = base / "source_snapshot" / name
        if not path.is_file() and name in fixed:
            path = fixed[name]
        if not path.is_file() and name == "registered_detector_model.pt":
            registration = _json_bytes(
                (ROOT / "config/learned_detector.json").read_bytes()
            )
            path = ROOT / _text(registration.get("checkpoint"), "checkpoint")
        if not path.is_file():
            unresolved.append(name)
            continue
        _read_reference(
            {"path": str(path), "sha256": digest},
            base,
            f"archived_source:{name}",
            inputs,
        )
        verified.append(name)
    return {
        "verified": verified,
        "unresolved": unresolved,
        "scope": "archived snapshots; current source drift does not invalidate historical experiments",
    }


def assess_bundle(
    bundle, *, base, requirements=None, bundle_path=None, bundle_bytes=None
):
    """Assess an input bundle; unknown and synthetic targets never pass as real.

    Bundle schema: ``daiyi-enterprise-input-v1``, optional shared ``identity``,
    and ``artifacts`` mapping supported roles to ``{path, sha256}`` references.
    Paths are relative to ``base`` unless absolute. Each role's JSON schema
    and source kind are validated independently. Reports retain rejected inputs
    and their reasons instead of silently omitting them. If a bundle path is
    supplied, its parsed and hashed bytes must be the same captured read.
    """
    requirements = requirements or load_requirements()
    base = Path(base).resolve()
    inputs, failures, loaded, paths, rejected = [], [], {}, {}, set()
    rows = {name: _row(requirements, name) for name in requirements["_indexed"]}

    def reject(role, error):
        rejected.add(role)
        failures.append(
            {
                "role": role,
                "code": getattr(error, "code", "invalid_evidence"),
                "reason": str(error),
                "scope": "input rejected; no real acceptance inferred",
            }
        )

    if bundle_path is not None:
        bundle_path = Path(bundle_path).resolve()
        content = (
            bundle_path.read_bytes() if bundle_bytes is None else bundle_bytes
        )
        captured = _json_bytes(content)
        if captured != bundle:
            raise EvidenceError(
                "bundle_content_mismatch",
                "提供的 bundle 与绑定字节不是同一份输入",
            )
        bundle = captured
        inputs.append(
            {
                "path": str(bundle_path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "role": "bundle",
                "origin": "local evaluation manifest; parsed and hashed from one read",
                "integrity": "pass",
            }
        )
    elif bundle_bytes is not None:
        raise EvidenceError(
            "missing_bundle_path", "提供绑定字节时须指定 bundle 路径"
        )
    if not isinstance(bundle, dict) or bundle.get("schema") != BUNDLE_SCHEMA:
        raise EvidenceError(
            "unsupported_bundle", "企业评估 bundle schema 不匹配"
        )
    references = bundle.get("artifacts")
    if not isinstance(references, dict) or set(references) - ROLES:
        raise EvidenceError(
            "invalid_bundle_roles", "artifacts 须使用支持的角色"
        )
    identity = bundle.get("identity", {})
    if not isinstance(identity, dict) or set(identity) - set(IDENTITY_FIELDS):
        raise EvidenceError("invalid_bundle_identity", "bundle 身份字段无效")
    expected_identity = {
        key: _text(value, key) for key, value in identity.items()
    }
    inputs.append(
        {
            "role": "requirements",
            "path": requirements["_path"],
            "sha256": requirements["_sha256"],
            "origin": requirements["agreement_status"],
            "integrity": "pass",
        }
    )
    for name, reference in requirements.get("sources", {}).items():
        try:
            _, _, entry = _read_reference(
                reference, ROOT, f"requirement_source:{name}", inputs
            )
            entry["origin"] = reference.get("status")
        except (EvidenceError, OSError, ValueError) as error:
            reject(f"requirement_source:{name}", error)
    for role, reference in references.items():
        try:
            path, content, entry = _read_reference(
                reference, base, role, inputs
            )
            data = _json_bytes(
                content, allow_array=role == "registered_regions"
            )
            entry["origin"] = _validate_role(data, role)
            observed_identity = _identity(data, role)
            for key, value in observed_identity.items():
                if key in expected_identity and expected_identity[key] != value:
                    raise EvidenceError(
                        "mixed_identity", f"{role}.{key} 与同件输入不一致"
                    )
                expected_identity.setdefault(key, value)
            loaded[role], paths[role] = data, path
        except (EvidenceError, OSError, ValueError) as error:
            reject(role, error)
    source_audit = None
    if "source_hashes" in loaded:
        try:
            source_audit = _source_audit(
                loaded["source_hashes"], paths["source_hashes"].parent, inputs
            )
        except (EvidenceError, OSError, ValueError) as error:
            reject("source_hashes", error)
    execution_fact = {
        "robot_executed": None,
        "feedback_status": "unknown",
        "source_role": "run_result",
        "scope": "hash/schema/identity-checked execution declaration; not a physical receipt",
    }
    if "run_result" in loaded:
        declared = loaded["run_result"].get("robot_executed")
        if isinstance(declared, bool):
            execution_fact["robot_executed"] = declared
        try:
            rows["motion_tracking"] = _motion_row(
                loaded["run_result"], requirements
            )
            execution_fact["feedback_status"] = (
                "accepted" if declared else "not_executed"
            )
        except (EvidenceError, KeyError, TypeError, ValueError) as error:
            execution_fact["feedback_status"] = "rejected"
            reject("motion_feedback", error)
    operations = (
        (
            "run_result",
            lambda: _run_rows(
                loaded["run_result"],
                loaded.get("cycle_receipt"),
                requirements,
                rows["motion_tracking"],
                loaded.get("registered_regions"),
            ),
        ),
        (
            "localization",
            lambda: [_localization_row(loaded["localization"], requirements)],
        ),
        (
            "site_manifest",
            lambda: [
                _surface_row(
                    paths["site_manifest"],
                    loaded["site_manifest"],
                    requirements,
                    inputs,
                )
            ],
        ),
        (
            "timing_receipt",
            lambda: _timing_rows(loaded["timing_receipt"], requirements),
        ),
        (
            "controller_receipt",
            lambda: [
                _controller_row(
                    loaded["controller_receipt"],
                    paths["controller_receipt"].parent,
                    requirements,
                    inputs,
                )
            ],
        ),
        (
            "reliability_receipt",
            lambda: _reliability_rows(
                loaded["reliability_receipt"], requirements
            ),
        ),
    )
    for role, operation in operations:
        if role not in loaded or role in rejected:
            continue
        try:
            for row in operation():
                rows[row["id"]] = row
        except (
            EvidenceError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            reject(role, error)
    role_to_rows = {
        "run_result": (
            "defect_binary",
            "defect_category",
            "motion_tracking",
            "simulated_residual",
            "simulation_cycle",
        ),
        "cycle_receipt": ("simulation_cycle",),
        "registered_regions": ("simulation_cycle",),
        "motion_feedback": ("motion_tracking", "simulation_cycle"),
        "localization": ("localization",),
        "site_manifest": ("signed_surface",),
        "timing_receipt": (
            "changeover",
            "new_product_occupancy",
            "adaptive_changeover",
        ),
        "controller_receipt": ("controller_commissioning",),
        "reliability_receipt": (
            "continuous_reliability",
            "virtual_reliability",
        ),
    }
    for role in rejected:
        for name in role_to_rows.get(role, ()):
            if (
                role == "run_result"
                and name == "motion_tracking"
                and "run_result" in loaded
            ):
                continue
            measured = rows[name].get("measured")
            rows[name] = _row(
                requirements,
                name,
                "fail",
                measured=measured,
                reason="输入已被拒绝；详见 failures",
                input_roles=(role,),
            )
    integrity = "fail" if failures else ("pass" if references else "unknown")
    if source_audit and source_audit["unresolved"] and integrity == "pass":
        integrity = "unknown"
    rows["source_integrity"] = _row(
        requirements,
        "source_integrity",
        integrity,
        {"rejected_roles": sorted(rejected), "source_audit": source_audit},
        reason="SHA256 校验与同件身份；哈希不认证声明的实测来源",
        input_roles=tuple(references),
    )
    matrix = list(rows.values())
    targets = [row for row in matrix if row["kind"] == "enterprise_target"]
    target_evidence_complete = (
        integrity == "pass"
        and bool(targets)
        and all(row["status"] == "pass" for row in targets)
    )
    pending = [row["id"] for row in targets if row["status"] != "pass"]
    for row in matrix:
        if row["evidence_status"] == "fail" and not any(
            failure["role"] in row["input_roles"] for failure in failures
        ):
            failures.append(
                {
                    "role": row["id"],
                    "code": "criterion_not_met",
                    "reason": row["reason"],
                    "measured": row["measured"],
                    "scope": "measured offline criterion; real status is evaluated separately",
                }
            )
    return {
        "schema": REPORT_SCHEMA,
        "subject": expected_identity,
        "scope": "local evidence assessment; offline/synthetic results are not real enterprise acceptance",
        "agreement_status": requirements["agreement_status"],
        "matrix": matrix,
        "execution_fact": execution_fact,
        "inputs": inputs,
        "failures": failures,
        "enterprise_targets_evidenced": target_evidence_complete,
        "enterprise_acceptance_passed": False,
        "formal_acceptance_status": "pending_confirmed_acceptance_protocol",
        "real_data_pending": pending,
    }


def evaluate_evidence(manifest, *, requirements_path=DEFAULT_REQUIREMENTS):
    """Read and assess a pinned bundle from disk."""
    path = Path(manifest).resolve()
    content = path.read_bytes()
    return assess_bundle(
        _json_bytes(content),
        base=path.parent,
        requirements=load_requirements(requirements_path),
        bundle_path=path,
        bundle_bytes=content,
    )


def evaluate_run(directory, *, requirements=None):
    """Read a historical run and receipt without executing or changing either."""
    directory = Path(directory).resolve()
    artifacts = {}
    for role, filename in (
        ("run_result", "result.json"),
        ("cycle_receipt", "cycle_receipt.json"),
        ("source_hashes", "source_hashes.json"),
    ):
        path = directory / filename
        if path.is_file():
            artifacts[role] = artifact_reference(path)
        else:
            artifacts[role] = {"path": str(path), "sha256": "0" * 64}
    registry = directory / "registered_regions.json"
    if registry.is_file():
        artifacts["registered_regions"] = artifact_reference(registry)
    report = assess_bundle(
        {"schema": BUNDLE_SCHEMA, "artifacts": artifacts},
        base=directory,
        requirements=requirements,
    )
    report["run_directory"] = str(directory)
    return report


def _example_data():
    """Build labeled contract examples; these are not a new grinding run."""
    identity = {
        "part_id": "097",
        "serial": "SYNTHETIC-097-A",
        "cad_revision": "synthetic-cad-contract-v1",
        "fixture_revision": "synthetic-fixture-contract-v1",
    }
    registered = [
        {
            "id": index,
            "kind": "burr",
            "source_kind": "synthetic",
            "points": [[index * 0.01, 0, 0], [index * 0.01, 0.001, 0]],
            "normals": [[0, 0, 1], [0, 0, 1]],
        }
        for index in range(1, 4)
    ]
    reinspection = {
        "source": "synthetic round-bound fixture; no sensor or robot execution",
        "view_count": 6,
        "revision": 1,
        "prediction_revision": 1,
        "pass_index": 0,
        "remaining_regions": 0,
        "region_reinspection": {
            "regions": [
                {
                    "id": region["id"],
                    "kind": region["kind"],
                    "status": "verified",
                    "status_reason": "synthetic_signed_interval_within_limits",
                    "overgrind": False,
                    "negative_limit_risk": False,
                    "positive_excess": False,
                    "observations": list(range(6)),
                    "anchors": 2,
                    "observed_anchors": 2,
                    "anchor_coverage": 1,
                    "peak_mm": 0.12,
                    "trough_mm": 0.08,
                    "lower_peak_mm": 0.06,
                    "upper_peak_mm": 0.14,
                    "anchor_bounds_mm": [[0.06, 0.14], [0.07, 0.13]],
                }
                for region in registered
            ],
            "verified": 3,
            "needs_rework": 0,
            "uncertain": 0,
            "insufficient": 0,
            "overgrind": 0,
            "negative_limit_risk": 0,
            "automatic_regrind_allowed": True,
            "invalid_observations": [],
            "coverage_scope": "six_view",
            "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
            "truth_used_for_decision": False,
            "observational_release": True,
        },
    }
    result = {
        "schema_version": 3,
        "scene": "workstation",
        "source_kind": "synthetic",
        "evidence_scope": "synthetic_contract_rehearsal",
        "robot": "NO HARDWARE; schema fixture only",
        "cad_workpiece": "097",
        "case": "burr",
        "perception": {
            "metrics": {
                "tp": 90,
                "fp": 5,
                "fn": 5,
                "tn": 1000,
                "visible_defect_count": 3,
                "matched_defect_count": 3,
            },
            "generated_defects": 3,
            "view_count": 6,
        },
        "robot_executed": True,
        "path_count": 3,
        "executions": [
            {
                "tracking_trace_p95_mm": 0.6,
                "surface_tracking_p95_mm": 0.8,
                "unexpected_contact_count": 0,
            }
        ],
        "motion_gate_passed": True,
        "goal_complete": True,
        "observational_release": True,
        "geometric_goal_complete": True,
        "task_status": "synthetic_complete",
        "wall_seconds": 30,
        "simulation_seconds": 240,
        "final_stock": {"remaining_peak_mm": 0.12},
        "controller_stopping": {"reason": "synthetic_contract_fixture"},
        "prediction_revision": 1,
        "reinspection": reinspection,
        "reinspections": [copy.deepcopy(reinspection)],
    }
    localization = {
        **identity,
        "schema": ROLE_SCHEMAS["localization"],
        "source_kind": "synthetic",
        "units": "mm",
        "coordinate_frame": "synthetic_robot_base",
        "measurement_system": "synthetic_independent_reference",
        "reviewer": "offline_contract_suite",
        "coverage_complete": True,
        "independent_reference": True,
        "calibration_verified": True,
        "samples": [
            {
                "zone": f"synthetic_{kind}",
                "point_id": f"p{index}",
                "defect_class": kind,
                "reference_mm": [index * 10, 0, 0],
                "predicted_mm": [index * 10 + 0.04, 0.02, 0.01],
                "uncertainty_mm": 0.02,
            }
            for index, kind in enumerate(("flash", "burr", "casting_root"))
        ],
    }
    site = {
        **identity,
        "source_kind": "synthetic",
        "measurement_system": "synthetic_signed_measurement",
        "controller": "synthetic_controller_stub",
        "reviewer": "offline_suite",
        "signed_limits_mm": {"lower": -0.3, "upper": 0.3},
        "required_zones": ["machined", "protected"],
        "zone_roles": {"machined": "machined", "protected": "protected"},
        "measurements_file": "surface.csv",
    }
    timing = {
        **identity,
        "schema": ROLE_SCHEMAS["timing_receipt"],
        "source_kind": "synthetic",
        "clock_kind": "wall_monotonic",
        "equipment_id": "SYNTHETIC_NO_EQUIPMENT",
        "product_from": "synthetic-product-A",
        "product_to": "synthetic-product-B",
        "code_changed": False,
        "complete": True,
        "first_good_part_verified": True,
        "events": [
            {"event": "new_product_start", "t_s": 0},
            {"event": "changeover_start", "t_s": 100},
            {"event": "changeover_ready", "t_s": 1300},
            {"event": "new_product_ready", "t_s": 5500},
        ],
        "occupied_intervals": [
            {"start_s": 0, "end_s": 4000},
            {"start_s": 3500, "end_s": 5500},
        ],
    }
    controller = {
        **identity,
        "schema": ROLE_SCHEMAS["controller_receipt"],
        "source_kind": "synthetic",
        "request_id": "SYNTHETIC-REQUEST",
        "execution_id": "SYNTHETIC-EXECUTION",
        "controller_id": "NO_CONTROLLER",
        "completed": True,
        "unexpected_contacts": 0,
        **{key: True for key in CALIBRATIONS},
    }
    reliability = {
        **identity,
        "schema": ROLE_SCHEMAS["reliability_receipt"],
        "source_kind": "synthetic",
        "clock_kind": "virtual",
        "boot_id": "synthetic-virtual-clock",
        "complete": True,
        "heartbeats": [
            {
                "t_s": hour * 3600,
                "healthy": True,
                "boot_id": "synthetic-virtual-clock",
            }
            for hour in range(801)
        ],
    }
    return {
        "identity": identity,
        "run_result": result,
        "registered_regions": registered,
        "localization": localization,
        "site_manifest": site,
        "timing_receipt": timing,
        "controller_receipt": controller,
        "reliability_receipt": reliability,
        "surface_rows": [
            ["SYNTHETIC-097-A", "machined", "m1", 0.8, 0.12, 0.02],
            ["SYNTHETIC-097-A", "protected", "p1", 0.01, -0.05, 0.02],
        ],
    }


def _example_receipt(result):
    return {
        "schema": "daiyi-station-receipt-v1",
        "evidence_scope": "simulation",
        "source_kind": "synthetic",
        "part": "097",
        "quality": {
            key: result[key]
            for key in (
                "motion_gate_passed",
                "observational_release",
                "geometric_goal_complete",
                "task_status",
            )
        }
        | {
            "simulated_residual_mm": result["final_stock"]["remaining_peak_mm"],
            "registered_areas": copy.deepcopy(
                (result.get("reinspection") or {}).get("region_reinspection")
            ),
        },
        "initial": {"observed_views": result["perception"]["view_count"]},
        "seconds": {
            "wall": result["wall_seconds"],
            "simulation": result["simulation_seconds"],
        },
    }


def _rehearsal_cases():
    """Name concrete failure modes and the expected observable decisions."""
    return [
        (
            "synthetic_normal",
            {
                "localization": "pass",
                "signed_surface": "pass",
                "motion_tracking": "pass",
                "simulation_cycle": "pass",
                "changeover": "pass",
                "new_product_occupancy": "pass",
                "controller_commissioning": "pass",
                "virtual_reliability": "pass",
            },
        ),
        (
            "normal_no_action",
            {"simulation_cycle": "pass", "motion_tracking": "unknown"},
        ),
        ("positive_surface_excess", {"signed_surface": "fail"}),
        ("negative_surface_overcut", {"signed_surface": "fail"}),
        ("surface_uncertainty_exceeds", {"signed_surface": "fail"}),
        ("protection_points_missing", {"signed_surface": "fail"}),
        ("protection_registration_missing", {"signed_surface": "fail"}),
        (
            "csv_hash_damage",
            {"signed_surface": "fail", "source_integrity": "fail"},
        ),
        (
            "mixed_csv_serial",
            {"signed_surface": "fail", "source_integrity": "fail"},
        ),
        ("csv_nan", {"signed_surface": "fail", "source_integrity": "fail"}),
        ("json_nan", {"localization": "fail", "source_integrity": "fail"}),
        ("localization_overlimit", {"localization": "fail"}),
        ("localization_class_missing", {"localization": "fail"}),
        (
            "motion_overlimit",
            {"motion_tracking": "fail", "simulation_cycle": "fail"},
        ),
        ("motion_at_boundary", {"motion_tracking": "fail"}),
        (
            "high_f1_wrong_class",
            {"defect_category": "fail", "simulation_cycle": "fail"},
        ),
        (
            "timing_overlimit",
            {"changeover": "fail", "new_product_occupancy": "fail"},
        ),
        (
            "planned_time_as_wall",
            {"changeover": "fail", "source_integrity": "fail"},
        ),
        ("controller_incomplete", {"controller_commissioning": "fail"}),
        (
            "controller_plan_hash_damage",
            {"controller_commissioning": "fail", "source_integrity": "fail"},
        ),
        (
            "controller_feedback_missing",
            {"controller_commissioning": "fail", "source_integrity": "fail"},
        ),
        (
            "mixed_controller_identity",
            {"controller_commissioning": "fail", "source_integrity": "fail"},
        ),
        (
            "cycle_receipt_disagreement",
            {"simulation_cycle": "fail", "source_integrity": "fail"},
        ),
        ("cycle_initial_single_view", {"simulation_cycle": "fail"}),
        ("cycle_final_single_view", {"simulation_cycle": "fail"}),
        ("cycle_original_region_missing", {"simulation_cycle": "fail"}),
        ("cycle_original_anchor_missing", {"simulation_cycle": "fail"}),
        ("cycle_region_hold", {"simulation_cycle": "fail"}),
        ("cycle_overgrind", {"simulation_cycle": "fail"}),
        ("cycle_negative_limit_risk", {"simulation_cycle": "fail"}),
        ("cycle_invalid_observations", {"simulation_cycle": "fail"}),
        ("cycle_registered_quality_disagreement", {"simulation_cycle": "fail"}),
        ("cycle_round_identity_mismatch", {"simulation_cycle": "fail"}),
        ("cycle_legacy_signed_proof_missing", {"simulation_cycle": "fail"}),
        (
            "cycle_positive_rework_then_verified",
            {"simulation_cycle": "pass", "motion_tracking": "pass"},
        ),
        ("cycle_unsafe_rework_then_verified", {"simulation_cycle": "fail"}),
        (
            "execution_feedback_rejected",
            {"motion_tracking": "fail", "simulation_cycle": "fail"},
        ),
        (
            "execution_feedback_absent",
            {"motion_tracking": "fail", "simulation_cycle": "fail"},
        ),
        ("virtual_fault", {"virtual_reliability": "fail"}),
        (
            "virtual_restart",
            {"virtual_reliability": "fail", "source_integrity": "fail"},
        ),
        ("virtual_heartbeat_gap", {"virtual_reliability": "fail"}),
        (
            "virtual_mixed_boot",
            {"virtual_reliability": "fail", "source_integrity": "fail"},
        ),
        (
            "virtual_missing_boot",
            {"virtual_reliability": "fail", "source_integrity": "fail"},
        ),
        (
            "evidence_absent",
            {
                "source_integrity": "unknown",
                "localization": "unknown",
                "signed_surface": "unknown",
                "continuous_reliability": "unknown",
            },
        ),
    ]


def _mutate_example(name, data):
    result = data["run_result"]
    surface = data["surface_rows"]
    report = result["reinspection"]["region_reinspection"]
    if name == "normal_no_action":
        result.update(
            case="normal",
            path_count=0,
            robot_executed=False,
            executions=[],
            motion_gate_passed=None,
            goal_complete=False,
            observational_release=False,
            geometric_goal_complete=None,
            task_status="no_defect_detected",
            final_stock={"remaining_peak_mm": 0},
            reinspection=None,
            reinspections=[],
        )
        data["registered_regions"] = []
        result["perception"].update(generated_defects=0)
        result["perception"]["metrics"].update(
            tp=0, fp=0, fn=0, visible_defect_count=0, matched_defect_count=0
        )
    elif name == "positive_surface_excess":
        surface[0][4] = 0.35
    elif name == "negative_surface_overcut":
        surface[1][4] = -0.35
    elif name == "surface_uncertainty_exceeds":
        surface[0][4] = 0.29
    elif name == "protection_points_missing":
        del surface[1:]
    elif name == "protection_registration_missing":
        del surface[1:]
        data["site_manifest"]["required_zones"] = ["machined"]
        data["site_manifest"]["zone_roles"] = {"machined": "machined"}
    elif name == "mixed_csv_serial":
        surface[1][0] = "SYNTHETIC-DIFFERENT-PART"
    elif name == "csv_nan":
        surface[0][4] = "NaN"
    elif name == "json_nan":
        data["localization"]["samples"][0]["predicted_mm"][0] = float("nan")
    elif name == "localization_overlimit":
        data["localization"]["samples"][0]["predicted_mm"][0] = 0.11
    elif name == "localization_class_missing":
        data["localization"]["samples"].pop()
    elif name in ("motion_overlimit", "motion_at_boundary"):
        result["executions"][0]["surface_tracking_p95_mm"] = (
            1.1 if name == "motion_overlimit" else 1.0
        )
        result["motion_gate_passed"] = False
    elif name == "high_f1_wrong_class":
        result["perception"]["metrics"].update(
            tp=1000, fp=0, fn=0, matched_defect_count=1
        )
    elif name == "cycle_initial_single_view":
        result["perception"]["view_count"] = 1
    elif name == "cycle_final_single_view":
        result["reinspection"]["view_count"] = 1
    elif name == "cycle_original_region_missing":
        report["regions"].pop()
        report["verified"] = len(report["regions"])
    elif name == "cycle_original_anchor_missing":
        report["regions"][0].update(
            anchors=1, observed_anchors=1, anchor_bounds_mm=[[0.06, 0.14]]
        )
    elif name == "cycle_region_hold":
        report["regions"][0]["status"] = "uncertain_measurement"
    elif name == "cycle_overgrind":
        report["overgrind"] = 1
        report["regions"][0]["overgrind"] = True
    elif name == "cycle_negative_limit_risk":
        report["negative_limit_risk"] = 1
        report["regions"][0]["negative_limit_risk"] = True
    elif name == "cycle_invalid_observations":
        report["invalid_observations"] = [
            {"view": 2, "reason": "synthetic invalid RGB-D arrays"}
        ]
    elif name == "cycle_round_identity_mismatch":
        result["reinspection"]["pass_index"] = 1
    elif name == "cycle_legacy_signed_proof_missing":
        for key in (
            "overgrind",
            "negative_limit_risk",
            "automatic_regrind_allowed",
            "invalid_observations",
            "truth_used_for_decision",
        ):
            report.pop(key)
        for region in report["regions"]:
            for key in (
                "overgrind",
                "negative_limit_risk",
                "anchor_bounds_mm",
                "trough_mm",
                "lower_peak_mm",
            ):
                region.pop(key)
    elif name in (
        "cycle_positive_rework_then_verified",
        "cycle_unsafe_rework_then_verified",
    ):
        earlier = result["reinspections"][0]
        earlier["remaining_regions"] = 1
        earlier_report = earlier["region_reinspection"]
        earlier_report.update(
            verified=2, needs_rework=1, observational_release=False
        )
        earlier_report["regions"][0].update(
            status="needs_rework",
            positive_excess=True,
            peak_mm=0.43,
            upper_peak_mm=0.45,
            anchor_bounds_mm=[[0.31, 0.45], [0.07, 0.13]],
        )
        if name == "cycle_unsafe_rework_then_verified":
            earlier_report.update(
                negative_limit_risk=1, automatic_regrind_allowed=False
            )
            earlier_report["regions"][0]["negative_limit_risk"] = True
            earlier_report["regions"][0]["anchor_bounds_mm"][0] = [-0.31, 0.1]
        result["executions"].append(copy.deepcopy(result["executions"][0]))
        result["reinspection"].update(
            revision=2, prediction_revision=2, pass_index=1
        )
        result["prediction_revision"] = 2
        result["reinspections"].append(copy.deepcopy(result["reinspection"]))
    elif name == "execution_feedback_rejected":
        result["executions"][0].pop("tracking_trace_p95_mm")
    elif name == "execution_feedback_absent":
        result["executions"] = []
    elif name == "timing_overlimit":
        data["timing_receipt"]["events"][2]["t_s"] = 2500
        data["timing_receipt"]["events"][3]["t_s"] = 9000
        data["timing_receipt"]["occupied_intervals"][-1]["end_s"] = 9000
    elif name == "planned_time_as_wall":
        data["timing_receipt"]["clock_kind"] = "planned_motion"
    elif name == "controller_incomplete":
        data["controller_receipt"]["completed"] = False
    elif name == "mixed_controller_identity":
        data["controller_receipt"]["serial"] = "SYNTHETIC-OTHER-SERIAL"
    elif name == "virtual_fault":
        data["reliability_receipt"]["heartbeats"][100].update(
            healthy=False, reason="synthetic_process_crash"
        )
    elif name == "virtual_restart":
        data["reliability_receipt"]["heartbeats"][400]["t_s"] = 0
    elif name == "virtual_heartbeat_gap":
        trace = data["reliability_receipt"]["heartbeats"]
        data["reliability_receipt"]["heartbeats"] = [trace[0], trace[-1]]
    elif name == "virtual_mixed_boot":
        data["reliability_receipt"]["heartbeats"][400]["boot_id"] = (
            "synthetic-second-boot"
        )
    elif name == "virtual_missing_boot":
        data["reliability_receipt"]["heartbeats"][400].pop("boot_id")
    if result["reinspections"]:
        result["reinspections"][-1] = copy.deepcopy(result["reinspection"])


def _write_example(directory, name):
    directory.mkdir()
    data = copy.deepcopy(_example_data())
    _mutate_example(name, data)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        (
            "serial",
            "zone",
            "point_id",
            "before_mm",
            "after_mm",
            "uncertainty_mm",
        )
    )
    writer.writerows(data.pop("surface_rows"))
    (directory / "surface.csv").write_text(stream.getvalue(), encoding="utf-8")
    data["site_manifest"]["measurements_sha256"] = artifact_reference(
        directory / "surface.csv"
    )["sha256"]
    if name == "csv_hash_damage":
        data["site_manifest"]["measurements_sha256"] = "0" * 64
    _write_json(
        directory / "plan.json",
        {
            "schema": "synthetic-plan-contract-v1",
            "source_kind": "synthetic",
            "simulation_only": True,
            "not_executable": True,
        },
    )
    plan = artifact_reference(directory / "plan.json", relative_to=directory)
    _write_json(
        directory / "program.json",
        {
            "schema": "synthetic-program-contract-v1",
            "source_kind": "synthetic",
            "simulation_only": True,
            "not_executable": True,
            "plan_sha256": plan["sha256"],
        },
    )
    controller = data["controller_receipt"]
    controller.update(
        plan=plan,
        plan_sha256=plan["sha256"],
        program=artifact_reference(
            directory / "program.json", relative_to=directory
        ),
    )
    feedback = {
        **{
            key: controller[key]
            for key in (
                *IDENTITY_FIELDS,
                "request_id",
                "execution_id",
                "source_kind",
                "completed",
                "plan_sha256",
            )
        },
        "schema": "daiyi-controller-feedback-v1",
        "program_sha256": controller["program"]["sha256"],
        "samples": [
            {"t_s": 0, "unexpected_contacts": 0},
            {"t_s": 1, "unexpected_contacts": 0},
        ],
    }
    _write_json(directory / "feedback.json", feedback)
    controller["feedback"] = artifact_reference(
        directory / "feedback.json", relative_to=directory
    )
    if name == "controller_plan_hash_damage":
        controller["plan_sha256"] = "0" * 64
    elif name == "controller_feedback_missing":
        controller["feedback"]["path"] = "absent_feedback.json"
    data["cycle_receipt"] = _example_receipt(data["run_result"])
    if name == "cycle_receipt_disagreement":
        data["cycle_receipt"]["quality"]["simulated_residual_mm"] = 0.8
    elif name == "cycle_registered_quality_disagreement":
        data["cycle_receipt"]["quality"]["registered_areas"]["regions"].pop()
    identity = data.pop("identity")
    references = {}
    for role, payload in data.items():
        path = directory / f"{role}.json"
        if name == "json_nan" and role == "localization":
            # This file is an intentionally malformed synthetic input. Aggregate
            # reports remain finite JSON, and its actual bytes/hash are retained.
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        else:
            _write_json(path, payload)
        references[role] = artifact_reference(path, relative_to=directory)
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "source_kind": "synthetic",
        "identity": identity,
        "artifacts": {} if name == "evidence_absent" else references,
    }
    _write_json(directory / "input.json", bundle)
    return bundle


def run_synthetic_suite(directory, *, requirements=None):
    """Execute small contract rehearsals in a new directory and retain failures."""
    requirements = requirements or load_requirements()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    cases = []
    for name, expected in _rehearsal_cases():
        target = directory / name
        bundle = _write_example(target, name)
        report = assess_bundle(
            bundle,
            base=target,
            requirements=requirements,
            bundle_path=target / "input.json",
        )
        observed = {
            row["id"]: row["evidence_status"] for row in report["matrix"]
        }
        actual_targets_unknown = all(
            row["status"] == "unknown"
            for row in report["matrix"]
            if row["kind"] == "enterprise_target"
        )
        passed = (
            all(observed[name] == value for name, value in expected.items())
            and actual_targets_unknown
            and not report["enterprise_targets_evidenced"]
            and not report["enterprise_acceptance_passed"]
        )
        _write_json(target / "assessment.json", report)
        cases.append(
            {
                "case": name,
                "source_kind": "synthetic",
                "expected": expected,
                "observed": {key: observed[key] for key in expected},
                "expected_decision_reproduced": passed,
                "all_enterprise_targets_unknown": actual_targets_unknown,
                "assessment": str(target / "assessment.json"),
                "inputs": report["inputs"],
                "failures": report["failures"],
            }
        )
    summary = {
        "schema": "daiyi-enterprise-rehearsal-suite-v1",
        "scope": "synthetic schema/acceptance rehearsal; no new grinding or real measurements",
        "case_count": len(cases),
        "expected_decisions_reproduced": sum(
            case["expected_decision_reproduced"] for case in cases
        ),
        "wall_seconds": time.perf_counter() - started,
        "cases": cases,
        "virtual_hours_are_wall_hours": False,
    }
    _write_json(directory / "suite_summary.json", summary)
    return summary


def _flatten_matrix(reports):
    return [
        {
            "assessment": name,
            "subject": report["subject"],
            **row,
            "traceable_inputs": [
                item
                for item in report["inputs"]
                if item["role"] in row["input_roles"]
            ],
        }
        for name, report in reports
        for row in report["matrix"]
    ]


def _archive_summary(reports):
    normal, nontrivial, motion_passes, executed = 0, 0, 0, 0
    (
        feedback_accepted,
        feedback_rejected,
        feedback_unknown,
        execution_unknown,
    ) = (0, 0, 0, 0)
    for _, report in reports:
        indexed = {row["id"]: row for row in report["matrix"]}
        cycle = indexed["simulation_cycle"]
        measured = cycle.get("measured") or {}
        normal += (
            cycle["status"] == "pass"
            and measured.get("normal_no_action_control") is True
        )
        nontrivial += (
            cycle["status"] == "pass"
            and measured.get("nontrivial_grinding_complete") is True
        )
        motion = indexed["motion_tracking"]
        fact = report.get("execution_fact") or {}
        declared = fact.get("robot_executed")
        executed += declared is True
        execution_unknown += declared is None
        feedback_accepted += (
            declared is True and fact.get("feedback_status") == "accepted"
        )
        feedback_rejected += (
            declared is True and fact.get("feedback_status") == "rejected"
        )
        feedback_unknown += declared is True and fact.get(
            "feedback_status"
        ) not in ("accepted", "rejected")
        motion_passes += declared is True and motion["status"] == "pass"
    return {
        "assessed_runs": len(reports),
        "normal_no_action_controls": normal,
        "nontrivial_complete_cycles": nontrivial,
        "executed_runs": executed,
        "executed_feedback_accepted": feedback_accepted,
        "executed_feedback_rejected": feedback_rejected,
        "executed_feedback_unknown": feedback_unknown,
        "execution_unknown_runs": execution_unknown,
        "motion_gate_passes": motion_passes,
        "real_enterprise_acceptances": sum(
            report["enterprise_acceptance_passed"] for _, report in reports
        ),
    }


def main(argv=None):
    """Run the evaluator and rehearsal suite; require a new output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--requirements", type=Path, default=DEFAULT_REQUIREMENTS
    )
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--bundle", type=Path, action="append", default=[])
    parser.add_argument(
        "--site-manifest", type=Path, action="append", default=[]
    )
    parser.add_argument("--skip-suite", action="store_true")
    parser.add_argument("--suite-only", action="store_true")
    args = parser.parse_args(argv)
    if args.suite_only and (
        args.skip_suite or args.run or args.bundle or args.site_manifest
    ):
        parser.error("--suite-only 不能与输入选项或 --skip-suite 同用")
    if args.output.exists():
        parser.error("--output 必须是新目录，不能覆盖已有证据")
    requirements = load_requirements(args.requirements)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    reports, groups, command_failures = [], {}, []
    suite = (
        None
        if args.skip_suite
        else run_synthetic_suite(
            output / "synthetic_suite", requirements=requirements
        )
    )
    inputs_requested = bool(args.run or args.bundle or args.site_manifest)
    collections = []
    if not args.suite_only and not inputs_requested:
        for name, directory in (
            (
                "same_input_42",
                ROOT / "runs/daiyi_full_chain_20261122/same_input_42/episodes",
            ),
            ("final_seven", ROOT / "runs/daiyi_full_chain_20261122/final"),
        ):
            if directory.is_dir():
                collections.append(
                    (
                        name,
                        sorted(
                            path
                            for path in directory.iterdir()
                            if path.is_dir()
                            and (path / "result.json").is_file()
                        ),
                    )
                )
            else:
                command_failures.append(
                    {
                        "code": "archive_missing",
                        "path": str(directory),
                        "reason": "默认历史输入目录缺失，不能自动视为通过",
                    }
                )
        args.site_manifest.append(ROOT / "config/site_acceptance.example.json")
    if args.run:
        collections.append(("requested_runs", args.run))
    for group, directories in collections:
        group_reports = []
        destination = output / "current" / group
        destination.mkdir(parents=True)
        for index, directory in enumerate(directories):
            report = evaluate_run(directory, requirements=requirements)
            name = f"{group}/{directory.name}"
            _write_json(
                destination / f"{index:02d}_{directory.name}.json", report
            )
            reports.append((name, report))
            group_reports.append((name, report))
        groups[group] = _archive_summary(group_reports)
    for index, path in enumerate(args.bundle):
        try:
            content = path.read_bytes()
            report = assess_bundle(
                _json_bytes(content),
                base=path.resolve().parent,
                requirements=requirements,
                bundle_path=path,
                bundle_bytes=content,
            )
            _write_json(output / f"bundle_{index:02d}.json", report)
            reports.append((f"bundle_{index:02d}", report))
        except (EvidenceError, OSError, ValueError) as error:
            command_failures.append(
                {
                    "code": getattr(error, "code", "invalid_bundle"),
                    "path": str(path),
                    "reason": str(error),
                }
            )
    for index, path in enumerate(args.site_manifest):
        try:
            report = assess_bundle(
                {
                    "schema": BUNDLE_SCHEMA,
                    "artifacts": {"site_manifest": artifact_reference(path)},
                },
                base=path.resolve().parent,
                requirements=requirements,
            )
            _write_json(output / f"site_manifest_{index:02d}.json", report)
            reports.append((f"site_manifest_{index:02d}", report))
        except (EvidenceError, OSError, ValueError) as error:
            command_failures.append(
                {
                    "code": getattr(error, "code", "invalid_site_manifest"),
                    "path": str(path),
                    "reason": str(error),
                }
            )
    _write_json(
        output / "acceptance_matrix.json",
        {
            "schema": "daiyi-enterprise-matrix-v1",
            "agreement_status": requirements["agreement_status"],
            "rows": _flatten_matrix(reports),
        },
    )
    failures = command_failures + [
        {"assessment": name, **failure}
        for name, report in reports
        for failure in report["failures"]
    ]
    if suite:
        failures += [
            {"assessment": f"synthetic/{case['case']}", **failure}
            for case in suite["cases"]
            for failure in case["failures"]
        ]
    input_records = [
        record for _, report in reports for record in report["inputs"]
    ]
    if suite:
        input_records += [
            record for case in suite["cases"] for record in case["inputs"]
        ]
    input_records += [
        {
            **artifact_reference(path),
            "role": "implementation",
            "origin": "this task's implementation and fault-oriented tests",
            "integrity": "pass",
        }
        for path in (
            Path(__file__).resolve(),
            ROOT / "src/daiyi_grinding/workstation_delivery.py",
            ROOT / "tests/test_enterprise_acceptance.py",
        )
    ]
    unique_inputs = {
        json.dumps(record, sort_keys=True): record for record in input_records
    }
    artifacts = [
        artifact_reference(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    evidence = {
        "schema": "daiyi-enterprise-offline-evidence-v1",
        "task": "企业要求与离线证据评估器、严格量测校验及 synthetic 演练",
        "scope": "offline software only; no physical robot, real measurement, message, commit or PR",
        "created_at": datetime.now().astimezone().isoformat(),
        "commands": [
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "daiyi_grinding.enterprise_acceptance",
                    *(sys.argv[1:] if argv is None else argv),
                ],
                "cwd": str(Path.cwd()),
                "environment": {
                    name: os.environ.get(name)
                    for name in (
                        "MUJOCO_GL",
                        "OMP_NUM_THREADS",
                        "MKL_NUM_THREADS",
                    )
                },
            }
        ],
        "inputs": list(unique_inputs.values()),
        "metrics": {
            "archive_groups": groups,
            "assessment_count": len(reports),
            "synthetic_cases": suite["case_count"] if suite else 0,
            "synthetic_expected_decisions_reproduced": suite[
                "expected_decisions_reproduced"
            ]
            if suite
            else 0,
            "wall_seconds": time.perf_counter() - started,
            "virtual_800h_is_real_800h": False,
            "real_enterprise_acceptances": 0,
        },
        "failures": failures,
        "artifacts": artifacts,
        "real_data_pending": {
            "requirements": sorted(
                {
                    name
                    for _, report in reports
                    for name in report["real_data_pending"]
                }
                or {
                    row["id"]
                    for row in requirements["requirements"]
                    if row["kind"] == "enterprise_target"
                }
            ),
            "items": [
                "同件真实 097 磨前/磨后独立量测、加工区和保护区点位",
                "飞边、毛刺、浇铸残根的独立三维参考及相机/装夹/TCP 标定",
                "真实 ESTUN 程序/计划/反馈回执及力控、去除工艺标定",
                "跨工件换型和新品占机的完整单机墙钟记录",
                "同一软件启动的连续 800 小时健康日志",
                "双方确认的实物验收采样、统计和误差定义",
            ],
        },
        "senior_model_claims": {
            "cpu_filter_previously_reused": True,
            "sam_or_action_checkpoint_run_this_task": False,
            "missing_senior_weights_reproduced": False,
        },
    }
    _write_json(output / "evidence.json", evidence)
    success = not command_failures and (
        suite is None
        or suite["case_count"] == suite["expected_decisions_reproduced"]
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "suite_passed": success,
                "synthetic_cases": suite["case_count"] if suite else 0,
                "synthetic_expected_decisions_reproduced": suite[
                    "expected_decisions_reproduced"
                ]
                if suite
                else 0,
                "archive_groups": groups,
                "enterprise_acceptance_passed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
