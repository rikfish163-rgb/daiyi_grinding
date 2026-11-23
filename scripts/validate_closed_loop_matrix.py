#!/usr/bin/env python3
"""Run and report the fourteen UI conditions in an isolated frozen workspace."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "casting_mixed": "变厚批缝＋卷曲／破裂＋毛刺",
    "casting_flash": "变厚弯曲批缝",
    "casting_interrupted": "断续飞边",
    "casting_curled": "卷曲薄片飞边",
    "casting_torn": "破裂与缺口飞边",
    "casting_burr": "不规则局部毛刺",
    "casting_normal": "正常铸件（同材质对照）",
    "mixed": "批缝＋局部毛刺",
    "seam": "批缝",
    "burr": "局部毛刺",
    "thin": "低矮薄批缝",
    "normal": "正常工件",
    "coarse": "粗大余料（需复核）",
    "noisy": "混合缺陷与深度噪声",
}
REASONS = {
    "residual_exceeds_tolerance": "残余余量超过 0.3 mm",
    "initial_objects_unobserved": "初检有未观测到的缺陷",
    "initial_object_detection_or_classification_incomplete": "初检漏检或类别匹配不全",
    "motion_safety_gate": "实际运动误差或接触超过安全阈值",
    "pass_budget_exhausted": "两轮加工预算已用完",
    "followup_safety_rejected": "复检后路径被安全门拒绝",
    "observed_regions_without_candidate_paths": "复检见超差区域但没有候选路径",
    "measured_review_region_excludes_tool_footprint": "砂带覆盖范围与需复核区域重叠，禁止自动加工",
}


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(output, seeds, source_workspace=ROOT):
    """Copy source and geometry so concurrent project changes cannot affect runs."""
    output.mkdir(parents=True, exist_ok=True)
    plan_path = output / "plan.json"
    if plan_path.exists():
        plan = read(plan_path)
        if plan["seeds"] != seeds:
            raise ValueError("Existing matrix has different seeds")
        return plan
    source = output / "source"
    source.mkdir()
    shutil.copytree(
        source_workspace / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(source_workspace / "config", source / "config")
    shutil.copytree(source_workspace / "assets", source / "assets")
    (source / "external").mkdir()
    shutil.copytree(
        source_workspace / "external/WeldGrind-V1",
        source / "external/WeldGrind-V1",
    )
    (source / "feishu_docs").symlink_to(
        ROOT / "feishu_docs", target_is_directory=True
    )
    registry = read(source / "config/learned_detector.json")
    checkpoint = source / registry["checkpoint"]
    checkpoint.parent.mkdir(parents=True)
    shutil.copyfile(source_workspace / registry["checkpoint"], checkpoint)
    if digest(checkpoint) != registry["checkpoint_sha256"]:
        raise ValueError("Registered checkpoint hash does not match")
    hashes = {
        str(p.relative_to(source)): digest(p)
        for group in ("src", "config", "assets", "external")
        for p in sorted((source / group).rglob("*"))
        if p.is_file()
        and "__pycache__" not in p.parts
        and ".git" not in p.parts
    }
    plan = {
        "schema_version": 1,
        "created_unix": time.time(),
        "seeds": seeds,
        "cases": CASES,
        "expected_episodes": len(CASES) * len(seeds),
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": digest(checkpoint),
        "source_hashes": hashes,
        "parameters": {
            "mode": "removal",
            "views": "multi",
            "tool": "belt",
            "detector": "learned",
            "planner": "segmented",
            "max_passes": 2,
            "casting_profile": "team_prior_20261121",
        },
        "scope": "Synthetic 097 CAD, new seeds; diagnosis changes convert seeds to development regression. No real process calibration.",
    }
    write(plan_path, plan)
    return plan


def run_episode(output, plan, case, seed, timeout):
    name = f"{case}_{seed}"
    episode = output / "episodes" / name
    receipt = output / "receipts" / f"{name}.json"
    if receipt.is_file():
        return read(receipt)
    if episode.exists():
        raise ValueError(f"Interrupted episode requires inspection: {episode}")
    (output / "episodes").mkdir(exist_ok=True)
    receipt.parent.mkdir(exist_ok=True)
    log_path = output / "logs" / f"{name}.log"
    log_path.parent.mkdir(exist_ok=True)
    command = [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "daiyi_grinding.workstation_run",
        "--case",
        case,
        "--seed",
        str(seed),
        "--checkpoint",
        plan["checkpoint"],
        "--output",
        str(episode),
        "--realtime-factor",
        "0",
    ]
    for key, value in plan["parameters"].items():
        command.extend(["--" + key.replace("_", "-"), str(value)])
    environment = {
        **os.environ,
        "PYTHONPATH": str(output / "source/src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MUJOCO_GL": "egl",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    started = time.monotonic()
    timed_out = False
    with log_path.open("w") as log:
        try:
            completed = subprocess.run(
                command,
                cwd=output / "source",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
    record = {
        "case": case,
        "seed": seed,
        "episode": str(episode),
        "command": command,
        "exit_code": code,
        "timed_out": timed_out,
        "wall_seconds": time.monotonic() - started,
        "log": str(log_path),
    }
    write(receipt, record)
    return record


def episode_row(receipt):
    episode = Path(receipt["episode"])
    result = read(episode / "result.json", {})
    status = read(episode / "status.json", {})
    perception = result.get("perception", {})
    metrics = perception.get("metrics", {})
    stock = result.get("final_stock", {})
    executions = result.get("executions", [])
    reinspections = result.get("reinspections", [])
    after = reinspections[-1].get("metrics", {}) if reinspections else {}
    checks = [result.get("preflight", {})]
    checks += [
        item["followup_preflight"]
        for item in result.get("reinspections", [])
        if "followup_preflight" in item
    ]
    executed_segments = sum(
        checks[i].get("admitted_count", 0)
        for i in range(min(len(executions), len(checks)))
    )
    failures = []
    for index, check in enumerate(checks):
        for row in check.get("rows", []):
            if row.get("status") != "passed":
                failures.append(
                    {
                        "pass": index,
                        "segment": row.get("segment_id"),
                        "reason": row.get("reason"),
                        "alternatives": row.get("failure_reasons", []),
                    }
                )
        if not check.get("passed", False) and check.get("reason"):
            failures.append({"pass": index, "reason": check["reason"]})
    if receipt["exit_code"] != 0:
        failures.append(
            {
                "reason": status.get("error")
                or (
                    "episode timeout"
                    if receipt["timed_out"]
                    else "process failure"
                )
            }
        )
    remaining = [
        r
        for r in stock.get("residual_regions", [])
        if r["remaining_peak_mm"] > 0.3
    ]
    reason = result.get("controller_stopping", {}).get("reason")
    review_guarded = perception.get("review_path_guard", {}).get(
        "blocked_paths_count", 0
    )
    if review_guarded:
        failures.append(
            {"reason": "measured_review_region_excludes_tool_footprint"}
        )
    if reason and reason not in {
        "no_observed_excess_above_tolerance",
        "no_admitted_path",
    }:
        failures.append({"reason": reason})
    normal = receipt["case"] in {"normal", "casting_normal"}
    normal_pass = (
        normal
        and status.get("stage") == "complete"
        and not perception.get("generated_defects")
        and metrics.get("fp", 0) == 0
        and result.get("path_count") == 0
        and not result.get("robot_executed")
        and all(
            item.get("metrics", {}).get("fp", 0) == 0 for item in reinspections
        )
        and all(item.get("remaining_paths", 0) == 0 for item in reinspections)
    )
    successful = bool(result.get("goal_complete")) or normal_pass
    row = {
        "case": receipt["case"],
        "label": CASES[receipt["case"]],
        "seed": receipt["seed"],
        "stage": status.get("stage", "missing"),
        "exit_code": receipt["exit_code"],
        "precision": metrics.get("precision"),
        "recall": metrics.get("recall"),
        "f1": metrics.get("f1"),
        "false_positive_pixels": metrics.get("fp"),
        "post_scan_count": len(reinspections),
        "post_precision": after.get("precision"),
        "post_recall": after.get("recall"),
        "post_f1": after.get("f1"),
        "post_false_positive_pixels": after.get("fp"),
        "generated_objects": perception.get("generated_defects"),
        "visible_objects": metrics.get("visible_defect_count"),
        "matched_objects": metrics.get("matched_defect_count"),
        "unobserved_objects": perception.get("unobserved_defects"),
        "initial_candidates": result.get("path_count"),
        "review_guarded_paths": review_guarded,
        "initial_admitted": result.get("admitted_path_count"),
        "completed_passes": len(executions),
        "executed_segments_across_passes": executed_segments,
        "robot_executed": result.get("robot_executed", False),
        "initial_peak_mm": stock.get("initial_peak_mm"),
        "remaining_peak_mm": stock.get("remaining_peak_mm"),
        "remaining_region_count": len(remaining),
        "remaining_regions": remaining,
        "all_residual_regions": stock.get("residual_regions", []),
        "surface_tracking_p95_mm": max(
            (
                e["surface_tracking_p95_mm"]
                for e in executions
                if e.get("surface_tracking_p95_mm") is not None
            ),
            default=None,
        ),
        "unexpected_contacts": sum(
            e.get("unexpected_contact_count", 0) for e in executions
        ),
        "motion_gate_passed": result.get("motion_gate_passed"),
        "task_status": result.get("task_status"),
        "goal_complete": result.get("goal_complete", False),
        "normal_no_action_pass": normal_pass,
        "validation_pass": successful,
        "stop_reason": reason,
        "failures": failures,
        "wall_seconds": receipt["wall_seconds"],
        "result": str(episode / "result.json"),
        "log": receipt["log"],
    }
    outcome_failures = []
    if remaining:
        outcome_failures.append("residual_exceeds_tolerance")
    if perception.get("unobserved_defects", 0):
        outcome_failures.append("initial_objects_unobserved")
    if metrics.get("matched_defect_count", 0) < perception.get(
        "generated_defects", 0
    ):
        outcome_failures.append(
            "initial_object_detection_or_classification_incomplete"
        )
    if row["motion_gate_passed"] is False:
        outcome_failures.append("motion_safety_gate")
    row["outcome_failures"] = outcome_failures
    row["failure_reasons"] = "；".join(
        REASONS.get(reason, reason)
        for reason in dict.fromkeys(
            outcome_failures
            + [f["reason"] for f in failures if f.get("reason")]
        )
    )
    return row


def percent(value):
    return "—" if value is None else f"{value * 100:.2f}%"


def number(value):
    return "—" if value is None else f"{value:.3f}"


def report(output, plan):
    rows = [
        episode_row(read(p))
        for p in sorted((output / "receipts").glob("*.json"))
    ]
    write(
        output / "matrix.json",
        {
            "expected": plan["expected_episodes"],
            "completed": len(rows),
            "validation_pass_count": sum(r["validation_pass"] for r in rows),
            "rows": rows,
        },
    )
    if rows:
        fields = [
            k
            for k in rows[0]
            if k
            not in {
                "failures",
                "remaining_regions",
                "all_residual_regions",
                "outcome_failures",
            }
        ]
        with (output / "matrix.csv").open("w", newline="") as file:
            writer = csv.DictWriter(
                file, fieldnames=fields, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)
        with (output / "residual_regions.csv").open("w", newline="") as file:
            fields = [
                "case",
                "seed",
                "name",
                "kind",
                "region",
                "remaining_peak_mm",
                "vertices_above_0_3mm",
            ]
            writer = csv.DictWriter(
                file, fieldnames=fields, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                for region in row["all_residual_regions"]:
                    writer.writerow(
                        {"case": row["case"], "seed": row["seed"], **region}
                    )
        with (output / "failures.csv").open("w", newline="") as file:
            fields = [
                "case",
                "seed",
                "pass",
                "segment",
                "reason",
                "alternatives",
            ]
            writer = csv.DictWriter(
                file, fieldnames=fields, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                for failure in row["failures"] + [
                    {"reason": reason} for reason in row["outcome_failures"]
                ]:
                    writer.writerow(
                        {
                            "case": row["case"],
                            "seed": row["seed"],
                            **failure,
                            "reason": REASONS.get(
                                failure["reason"], failure["reason"]
                            ),
                            "alternatives": "；".join(
                                failure.get("alternatives", [])
                            ),
                        }
                    )
    lines = [
        f"# {len(plan['cases'])} 工况闭环验证",
        "",
        f"已记录 {len(rows)}/{plan['expected_episodes']} 次；每种工况 {len(plan['seeds'])} 个种子。",
        "模型、配置、CAD 和后处理代码在 plan.json 冻结。初始候选分母与跨轮执行段数分别记录。",
        "正常工况通过标准：无缺陷、无误报像素、无自动路径、无机器人加工；其 F1 无定义。",
        "安全拒绝是有效验证结果，不等于打磨完成。所有数字限于合成 097 与几何余料去除。",
        "像素 F1 统计批缝/毛刺的二值检测，不代表类别准确率；粗大余料的 review 标签不计入该 F1，当前三类网络没有 review 输出。",
        "",
        "| 工况 | 种子 | 初检/末次复检 F1 | 匹配/可见/生成 | 初轮执行/候选 | 跨轮执行段 | 初始→残余 mm | 超差区 | 加工点 P95 mm | 结果/原因 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for case in CASES:
        for row in sorted(
            (r for r in rows if r["case"] == case), key=lambda r: r["seed"]
        ):
            reasons = list(
                dict.fromkeys(
                    f["reason"] for f in row["failures"] if f.get("reason")
                )
            )
            reasons = row["outcome_failures"] + reasons
            reasons = [REASONS.get(reason, reason) for reason in reasons]
            result = "通过" if row["validation_pass"] else "未完整通过"
            if reasons:
                result += "：" + "；".join(reasons[:3]).replace("|", "/")
            initial_executed = (
                row["initial_admitted"] if row["completed_passes"] else 0
            )
            lines.append(
                f"| {row['label']} | {row['seed']} | {percent(row['f1'])}/{percent(row['post_f1'])} | "
                f"{row['matched_objects']}/{row['visible_objects']}/{row['generated_objects']} | "
                f"{initial_executed}/{row['initial_candidates']} | {row['executed_segments_across_passes']} | "
                f"{number(row['initial_peak_mm'])}→{number(row['remaining_peak_mm'])} | "
                f"{row['remaining_region_count']} | {number(row['surface_tracking_p95_mm'])} | {result} |"
            )
    lines.extend(["", "## 逐次证据", ""])
    for row in rows:
        lines.append(
            f"- {row['case']} / {row['seed']}：[原始结果]({row['result']})；[运行日志]({row['log']})"
        )
    lines.extend(["", "## 每个缺陷区域的最终余量", ""])
    for row in rows:
        regions = [
            f"{r['name']} ({r.get('kind')}, {r.get('region')}): "
            f"{number(r['remaining_peak_mm'])} mm"
            for r in row["all_residual_regions"]
        ]
        lines.append(
            f"- {row['case']} / {row['seed']}："
            + ("；".join(regions) if regions else "无生成缺陷")
        )
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=[9801, 9802, 9803]
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--source-workspace", type=Path, default=ROOT)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    plan = freeze(output, args.seeds, args.source_workspace.resolve())
    if args.report_only:
        report(output, plan)
        return
    # Fast no-action and basic burr checks first; all UI cases remain mandatory.
    order = ["normal", "casting_normal", "burr"] + [
        c for c in CASES if c not in {"normal", "casting_normal", "burr"}
    ]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as pool:
        pending = {
            pool.submit(run_episode, output, plan, c, s, args.timeout): (c, s)
            for c in order
            for s in args.seeds
        }
        for task in concurrent.futures.as_completed(pending):
            record = task.result()
            row = episode_row(record)
            rows = report(output, plan)
            print(
                f"{len(rows)}/{plan['expected_episodes']} {row['case']} {row['seed']} "
                f"exit={row['exit_code']} paths={row['initial_admitted']}/{row['initial_candidates']} "
                f"peak={row['remaining_peak_mm']} pass={row['validation_pass']}",
                flush=True,
            )


if __name__ == "__main__":
    main()
