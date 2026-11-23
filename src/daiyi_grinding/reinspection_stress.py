"""Offline sensor-array stress suite for signed post-grind reinspection."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from .project import RUNS
from .team_data_reuse import sha256
from .workstation_model import write_json
from .workstation_quality import Reinspection

OUTPUT_SCHEMA = "enterprise-reinspection-stress-v1"
TOLERANCE_MM = 0.3
DEFAULT_OUTPUT_ROOT = RUNS / "enterprise_offline_20261123" / "reinspection"
REPORT_DATE = "2026-11-23"
SOURCE_FILES = (
    "src/daiyi_grinding/workstation_quality.py",
    "src/daiyi_grinding/workstation_run.py",
    "src/daiyi_grinding/reinspection_stress.py",
    "tests/test_reinspection_stress.py",
    "tests/test_workstation_stopping.py",
    "docs/reinspection_stress_20261123.md",
)


def _target():
    pixel_u = np.array([18, 27, 36, 45], dtype=float)
    pixel_v = np.array([18, 27, 36, 45], dtype=float)
    points = np.stack(
        [
            (pixel_u - 31.5) / 80.0,
            (pixel_v - 31.5) / 80.0,
            np.ones(len(pixel_u)),
        ],
        axis=1,
    )
    normals = np.tile(np.array([0.0, 0.0, -1.0]), (len(points), 1))
    return {
        "id": 1,
        "kind": "seam",
        "points": points,
        "normals": normals,
    }


def _camera():
    return np.eye(4)


def _intrinsics():
    return np.array([[80.0, 0.0, 31.5], [0.0, 80.0, 31.5], [0.0, 0.0, 1.0]])


def _sensor_observation(
    height_mm,
    *,
    target=None,
    noise_sigma_mm=0.025,
    noise_seed=None,
    normals=None,
    reference_shift_m=0.0,
    selection=None,
    invalid_noise=False,
    measured_nan_pixels=(),
):
    target = _target() if target is None else target
    camera, intrinsics = _camera(), _intrinsics()
    height, width = 64, 64
    columns, rows = np.meshgrid(np.arange(width), np.arange(height))
    reference = np.stack(
        [
            (columns - intrinsics[0, 2]) / intrinsics[0, 0],
            (rows - intrinsics[1, 2]) / intrinsics[1, 1],
            np.ones((height, width)),
        ],
        axis=-1,
    )
    measured = reference.copy()
    measured[..., 2] = 1.0 - float(height_mm) / 1000.0
    if noise_seed is not None:
        rng = np.random.default_rng(noise_seed)
        measured[..., 2] += rng.normal(
            0.0, noise_sigma_mm / 1000.0, (height, width)
        )
    for pixel in measured_nan_pixels:
        measured[pixel[1], pixel[0], :] = np.nan
    if reference_shift_m:
        reference[..., 0] += float(reference_shift_m)
    used_normals = (
        np.tile(normals, (len(target["points"]), 1))
        if normals is not None
        else target["normals"]
    )
    return {
        "T": camera,
        "K": intrinsics,
        "surface": np.ones((height, width), dtype=bool),
        "selection": (
            np.ones((height, width), dtype=bool)
            if selection is None
            else np.asarray(selection, dtype=bool)
        ),
        "reference_xyz": reference,
        "xyz": measured,
        "noise_sigma_mm": np.nan if invalid_noise else noise_sigma_mm,
    }, {
        **target,
        "normals": used_normals,
    }


def _left_roi_selection():
    selection = np.zeros((64, 64), dtype=bool)
    selection[:, 32:] = True
    return selection


def _case_definitions():
    return (
        {
            "name": "nominal_signed",
            "height_mm": 0.08,
            "noise_sigma_mm": 0.025,
            "expected_status": "verified",
        },
        {
            "name": "positive_near_line_uncertain",
            "height_mm": 0.27,
            "noise_sigma_mm": 0.025,
            "expected_status": "uncertain_measurement",
        },
        {
            "name": "positive_excess",
            "height_mm": 0.46,
            "noise_sigma_mm": 0.025,
            "expected_status": "needs_rework",
        },
        {
            "name": "negative_overgrind",
            "height_mm": -0.36,
            "noise_sigma_mm": 0.025,
            "expected_status": "needs_rework",
        },
        {
            "name": "negative_near_line_uncertain",
            "height_mm": -0.27,
            "noise_sigma_mm": 0.025,
            "expected_status": "uncertain_measurement",
        },
        {
            "name": "noisy_near_line_uncertain",
            "height_mm": 0.25,
            "noise_sigma_mm": 0.035,
            "noise_seed": 20261123,
            "expected_status": "uncertain_measurement",
        },
        {
            "name": "dropout_and_nan",
            "height_mm": 0.08,
            "noise_sigma_mm": 0.025,
            "measured_nan_pixels": ((18, 18), (45, 45)),
            "expected_status": "insufficient_observation",
        },
        {
            "name": "invalid_nan_noise",
            "height_mm": 0.08,
            "invalid_noise": True,
            "expected_status": "insufficient_observation",
        },
        {
            "name": "backface_oblique_occlusion",
            "height_mm": 0.08,
            "normals": np.array([0.2, 0.1, 0.97]),
            "expected_status": "insufficient_observation",
        },
        {
            "name": "registered_view_mismatch",
            "height_mm": 0.08,
            "reference_shift_m": 0.003,
            "expected_status": "insufficient_observation",
        },
        {
            "name": "limited_roi",
            "height_mm": 0.08,
            "selection": _left_roi_selection(),
            "expected_status": "insufficient_observation",
        },
    )


def _save_input(path, observations, target):
    arrays = {
        "target_points": target["points"],
        "target_normals": target["normals"],
    }
    for index, observation in enumerate(observations):
        prefix = f"view_{index:02d}_"
        arrays.update(
            {
                prefix + key: value
                for key, value in observation.items()
                if key not in {"tool"}
            }
        )
    np.savez_compressed(path, **arrays)
    return sha256(path)


def _source_inputs():
    files = []
    for relative in SOURCE_FILES:
        path = Path(__file__).resolve().parents[2] / relative
        if path.exists():
            files.append(
                {
                    "path": relative,
                    "sha256": sha256(path),
                    "role": "implementation-or-test-source",
                }
            )
    return files


def _artifact_records(paths):
    return [
        {"path": str(path), "sha256": sha256(path)}
        for path in paths
        if path.exists()
    ]


def _case_result(case, report):
    region = report["regions"][0]
    return {
        "name": case["name"],
        "status": region["status"],
        "expected_status": case["expected_status"],
        "expectation_met": region["status"] == case["expected_status"],
        "status_reason": region["status_reason"],
        "truth_used_for_decision": False,
        "report": report,
    }


def _write_report(path, cases, metrics):
    lines = [
        "# 097 磨后 signed 复检压力套件",
        "",
        f"日期：{REPORT_DATE}；公差：±{TOLERANCE_MM:.2f} mm。",
        "",
        "全部输入为合成 RGB-D 数组：同一 CAD 参考面、同一登记锚点和相机模型。",
        "报告不读取真值余量；真值仅用于测试期望值核对。",
        "",
        "## 逐案结果",
        "",
        "| 案例 | 判定 | 原因 | 覆盖 | signed 峰值 mm |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for case in cases:
        region = case["report"]["regions"][0]
        signed_peak = (
            f"{region['signed_peak_mm']:.3f}"
            if region["signed_peak_mm"] is not None
            else "not measured"
        )
        lines.append(
            f"| {case['name']} | {region['status']} | "
            f"{region['status_reason']} | "
            f"{region['anchor_coverage']:.2f} | "
            f"{signed_peak} |"
        )
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            f"- 通过 {metrics['verified']} 例，保守拒绝 {metrics['rejected']} 例。",
            f"- 负向过磨拒绝 {metrics['negative_rejections']} 例。",
            f"- 漏测/非法/遮挡/错配/ROI 拒绝 {metrics['insufficient']} 例。",
            f"- 未预期结果 {metrics['unexpected_failures']} 例。",
            "",
            "## 限制",
            "",
            "未使用真实 097 独立扫描、真实过磨样本或标定后的相机不确定度。",
            "±2σ 上下界来自输入声明的 `noise_sigma_mm`，不是实测误差预算。",
            "补扫用例只验证锚点覆盖恢复，不验证转台运动或 MuJoCo 几何。",
            "",
            "## 主链接口",
            "",
            "`workstation_inspection.Inspection` 继续构造 `Reinspection(targets)`、",
            "逐视角调用 `observe(observation, view_index)`，并在 `scan()` 中调用",
            "`report(0.3, whole_workpiece_scan=...)`。signed 上下界进入复检回执。",
            "`workstation_run.continuation_reason()` 根据观测选择继续或停止；",
            "过磨、负边界不确定或非法复检时停止自动回磨，即使还有路径和预算。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _new_output_directory(output=None):
    if output is not None:
        path = Path(output)
        if path.exists():
            raise ValueError(f"Reinspection output must be new: {path}")
        path.mkdir(parents=True)
        return path
    index = 1
    while (path := DEFAULT_OUTPUT_ROOT / f"sensor_only_{index:03d}").exists():
        index += 1
    path.mkdir(parents=True)
    return path


def run(output=None):
    """Run the low-resource sensor-array suite and publish evidence."""
    output = _new_output_directory(output)
    cases = []
    input_records = []
    for case in _case_definitions():
        observation, target = _sensor_observation(
            case["height_mm"],
            noise_sigma_mm=case.get("noise_sigma_mm", 0.025),
            noise_seed=case.get("noise_seed"),
            normals=case.get("normals"),
            reference_shift_m=case.get("reference_shift_m", 0.0),
            selection=case.get("selection"),
            invalid_noise=case.get("invalid_noise", False),
            measured_nan_pixels=case.get("measured_nan_pixels", ()),
        )
        case_directory = output / case["name"]
        case_directory.mkdir()
        input_hash = _save_input(
            case_directory / "input.npz", [observation], target
        )
        tracker = Reinspection([target])
        measured_count = tracker.observe(observation, 0)
        report = tracker.report(TOLERANCE_MM)
        result = _case_result(case, report)
        result["measured_anchors"] = measured_count
        write_json(case_directory / "case.json", result)
        cases.append(result)
        input_records.append(
            {
                "case": case["name"],
                "path": str(case_directory / "input.npz"),
                "sha256": input_hash,
                "source": "synthetic sensor arrays; no MuJoCo or hardware",
            }
        )

    supplement_directory = output / "supplementary_coverage"
    supplement_directory.mkdir()
    first, target = _sensor_observation(
        0.08, selection=_left_roi_selection(), noise_sigma_mm=0.025
    )
    second, _ = _sensor_observation(0.08, noise_sigma_mm=0.025)
    supplement_hash = _save_input(
        supplement_directory / "input.npz", [first, second], target
    )
    tracker = Reinspection([target])
    tracker.observe(first, 0)
    before = tracker.report(TOLERANCE_MM)
    tracker.observe(second, 1)
    after = tracker.report(TOLERANCE_MM)
    supplement_case = {
        "name": "supplementary_coverage",
        "status": after["regions"][0]["status"],
        "expected_status": "verified",
        "expectation_met": after["regions"][0]["status"] == "verified",
        "status_reason": after["regions"][0]["status_reason"],
        "truth_used_for_decision": False,
        "before": before,
        "report": after,
        "same_input_except": "view 1 removes the ROI selection restriction",
        "uncertainty_calibrated": False,
    }
    write_json(supplement_directory / "case.json", supplement_case)
    cases.append(supplement_case)
    input_records.append(
        {
            "case": "supplementary_coverage",
            "path": str(supplement_directory / "input.npz"),
            "sha256": supplement_hash,
            "source": "synthetic sensor arrays; no MuJoCo or hardware",
        }
    )

    statuses = [case["status"] for case in cases]
    metrics = {
        "total_cases": len(cases),
        "verified": statuses.count("verified"),
        "rejected": len(statuses) - statuses.count("verified"),
        "needs_rework": statuses.count("needs_rework"),
        "uncertain": statuses.count("uncertain_measurement"),
        "insufficient": statuses.count("insufficient_observation"),
        "negative_rejections": sum(
            case["report"]["regions"][0]["status_reason"] == "overgrind"
            for case in cases
        ),
        "unexpected_failures": sum(
            not case["expectation_met"] for case in cases
        ),
        "all_expectations_met": all(case["expectation_met"] for case in cases),
        "tolerance_mm": TOLERANCE_MM,
    }
    write_json(output / "summary.json", metrics)
    write_json(
        output / "case_results.json",
        {
            "schema": OUTPUT_SCHEMA,
            "cases": [
                {key: value for key, value in case.items() if key != "report"}
                for case in cases
            ],
        },
    )
    _write_report(output / "report.md", cases, metrics)
    evidence = {
        "task": "sensor-only signed reinspection, supplementary coverage and stress audit",
        "scope": {
            "schema": OUTPUT_SCHEMA,
            "offline": True,
            "sensor_array_only": True,
            "mujoco_used": False,
            "mujoco_lock_required": False,
            "hardware_controlled": False,
            "truth_used_for_release": False,
            "tolerance_mm": TOLERANCE_MM,
        },
        "commands": {
            "suite": "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            ".venv/bin/python -m daiyi_grinding.reinspection_stress "
            f"--output {output}",
            "old_quality_tests": "MUJOCO_GL=egl OMP_NUM_THREADS=1 "
            "MKL_NUM_THREADS=1 .venv/bin/python -m unittest "
            "tests.test_workstation_quality -v",
            "new_stress_tests": "MUJOCO_GL=egl OMP_NUM_THREADS=1 "
            "MKL_NUM_THREADS=1 .venv/bin/python -m unittest "
            "tests.test_reinspection_stress -v",
            "ruff": ".venv/bin/python -m ruff check "
            "src/daiyi_grinding/workstation_quality.py "
            "src/daiyi_grinding/reinspection_stress.py "
            "tests/test_reinspection_stress.py",
        },
        "inputs": {
            "source_root": str(Path(__file__).resolve().parents[2]),
            "files": _source_inputs(),
            "sensor_cases": input_records,
        },
        "metrics": metrics,
        "failures": {
            "expected_rejections": metrics["rejected"],
            "unexpected_suite_failure_count": metrics["unexpected_failures"],
            "unexpected_cases": [
                case["name"] for case in cases if not case["expectation_met"]
            ],
        },
        "artifacts": _artifact_records(
            [
                output / "summary.json",
                output / "case_results.json",
                output / "report.md",
                *(output / case["name"] / "case.json" for case in cases),
            ]
        ),
        "real_data_pending": {
            "independent_real_097_postgrind_sensor_scan": True,
            "same_part_before_after_measurements": True,
            "calibrated_camera_uncertainty": True,
            "real_overgrind_and_near_tolerance_samples": True,
        },
    }
    write_json(output / "evidence.json", evidence)
    return {
        "output": str(output),
        **metrics,
    }


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--output",
        help="new output directory; omit for a fresh default directory",
    )
    arguments = argument_parser.parse_args()
    result = run(arguments.output)
    exit_code = 1 if result["unexpected_failures"] else 0
    print(
        f"Reinspection stress: {result['output']}; "
        f"verified={result['verified']}; rejected={result['rejected']}; "
        f"unexpected={result['unexpected_failures']}",
        flush=True,
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
