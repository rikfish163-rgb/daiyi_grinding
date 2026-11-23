#!/usr/bin/env python3
"""Publish the complete matrix, keeping development and new-seed results apart."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from validate_closed_loop_matrix import CASES, read, report, write


def summarize(output):
    plan = read(output / "plan.json")
    rows = report(output, plan)
    expected = {
        (case, seed) for case in plan["cases"] for seed in plan["seeds"]
    }
    actual = {(row["case"], row["seed"]) for row in rows}
    if actual != expected:
        raise ValueError(
            f"Incomplete matrix: missing={sorted(expected - actual)}"
        )
    counts = Counter()
    for row in rows:
        metrics = read(Path(row["result"]))["perception"]["metrics"]
        counts.update(
            {name: metrics[name] for name in ("tp", "fp", "fn", "tn")}
        )
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    return {
        "episodes": len(rows),
        "process_failures": sum(row["exit_code"] != 0 for row in rows),
        "fully_passed": sum(row["validation_pass"] for row in rows),
        "normal_passed": sum(row["normal_no_action_pass"] for row in rows),
        "residual_within_tolerance": sum(
            row["remaining_peak_mm"] is not None
            and row["remaining_peak_mm"] <= 0.3
            for row in rows
        ),
        "executed_segments_across_passes": sum(
            row["executed_segments_across_passes"] for row in rows
        ),
        "unexpected_contacts": sum(row["unexpected_contacts"] for row in rows),
        "classification_or_coverage_failure_episodes": sum(
            row["matched_objects"] < row["generated_objects"] for row in rows
        ),
        "motion_gate_failure_episodes": sum(
            row["motion_gate_passed"] is False for row in rows
        ),
        "pooled_binary_precision": tp / (tp + fp) if tp + fp else None,
        "pooled_binary_recall": tp / (tp + fn) if tp + fn else None,
        "pooled_binary_f1": 2 * tp / (2 * tp + fp + fn)
        if 2 * tp + fp + fn
        else None,
        "rows": rows,
    }


def triple(rows, function):
    return " / ".join(function(row) for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    baseline = summarize(root / "baseline")
    final = summarize(root / "final")
    geometric = summarize(root / "geometric_diagnosis")
    document = {
        "baseline": baseline,
        "final": final,
        "geometric_diagnosis": geometric,
    }
    write(root / "summary.json", document)
    lines = [
        "# 十四工况多种子闭环结果",
        "",
        "同一 097 CAD、ESTUN 整站、固定砂带、六视角、最多两轮。",
        f"原版本 9801–9803：42 次，完整通过 {baseline['fully_passed']} 次。",
        f"冻结修复版本 9901–9903：42 次，完整通过 {final['fully_passed']} 次；",
        f"其中正常无动作对照 {final['normal_passed']} 次，加工点追踪门失败 {final['motion_gate_failure_episodes']} 次。",
        "下表每个单元格三项依次对应 **9901 / 9902 / 9903**。",
        "初轮执行数只统计完整返回的实际动力学执行；跨轮执行段可能重复同一区域。",
        "",
        "| 工况 | 初检像素 F1 % | 初轮执行/候选 | 跨轮执行段 | 最大残余 mm | 完整通过 | 主要未通过原因 |",
        "|---|---|---|---|---|---|---|",
    ]
    for case, label in CASES.items():
        rows = sorted(
            (row for row in final["rows"] if row["case"] == case),
            key=lambda row: row["seed"],
        )
        reasons = []
        if any(row["remaining_region_count"] for row in rows):
            reasons.append("余量超差")
        if any(
            row["matched_objects"] < row["generated_objects"] for row in rows
        ):
            reasons.append("漏检或分类不全")
        if any(row["motion_gate_passed"] is False for row in rows):
            reasons.append("实际追踪超阈值")
        if any(
            row["initial_admitted"] < row["initial_candidates"] for row in rows
        ):
            reasons.append("部分路径安全拒绝")
        f1 = triple(
            rows,
            lambda row: "—" if row["f1"] is None else f"{100 * row['f1']:.2f}",
        )
        initial = triple(
            rows,
            lambda row: (
                f"{row['initial_admitted'] if row['completed_passes'] else 0}"
                f"/{row['initial_candidates']}"
            ),
        )
        executed = triple(
            rows, lambda row: str(row["executed_segments_across_passes"])
        )
        residual = triple(
            rows,
            lambda row: (
                "—"
                if row["remaining_peak_mm"] is None
                else f"{row['remaining_peak_mm']:.3f}"
            ),
        )
        lines.append(
            f"| {label} | {f1} | {initial} | {executed} | {residual} | "
            f"{sum(row['validation_pass'] for row in rows)}/3 | "
            f"{'；'.join(reasons) if reasons else '正常无动作通过' if 'normal' in case else '通过'} |"
        )
    lines.extend(
        [
            "",
            "像素 F1 为批缝/毛刺二值指标，不能代替分类、缺陷覆盖或加工成功率。",
            "粗大余料的 review 标签不计入该 F1；正常 F1 无定义。",
            "不同种子组用于不同版本验证，组间差异不能直接称为配对提升。",
            "",
            "## 已验证的失败修复",
            "",
            "连续候选曲线提前验收并回滚：毛刺 9803 原来整条计划被撤销，"
            "实际执行 0 段；修复后两轮执行 4 段，最大余量 2.543→1.823 mm，"
            "仍有不可放行区域，未称为完整成功。",
            "",
            "低矮薄批缝的现有几何检测替代方案，同开发种子完整回归：",
            "",
            "| 种子 | 学习模型 F1 % | 几何检测 F1 % | 学习模型残余 mm | 几何检测残余 mm | 几何闭环结果 |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in sorted(geometric["rows"], key=lambda row: row["seed"]):
        previous = next(
            old
            for old in baseline["rows"]
            if old["case"] == "thin" and old["seed"] == row["seed"]
        )
        lines.append(
            f"| {row['seed']} | {100 * previous['f1']:.2f} | {100 * row['f1']:.2f} | "
            f"{previous['remaining_peak_mm']:.3f} | {row['remaining_peak_mm']:.3f} | "
            f"{'通过' if row['validation_pass'] else '未完整通过'} |"
        )
    guard = read(
        root / "contact_guard_trial/episodes/burr_9903/result.json", {}
    )
    if guard:
        lines.extend(
            [
                "",
                "名义工件接触保护的单独开发试验（9903，未混入 42 次新种子矩阵）：",
                "在原通用碰撞门之外增加 0.05 mm 名义工件/砂带接触深度限制，"
                "拒绝外壁问题段并改变另一个接触位置。实际执行 "
                f"{guard['admitted_path_count']}/{guard['path_count']} 段，"
                f"加工点 P95 {guard['execution']['surface_tracking_p95_mm']:.3f} mm，"
                f"意外接触 {guard['execution']['unexpected_contact_count']}，"
                f"最大余量 {guard['final_stock']['remaining_peak_mm']:.3f} mm。"
                "解决了该次允许接触造成的运动门失败，未解决外壁加工与分类。",
            ]
        )
    coarse = read(
        root / "coarse_review_trial_q95/episodes/coarse_9901/result.json", {}
    )
    if coarse:
        lines.extend(
            [
                "",
                "粗大余料的测量复核门（9901，单独开发回归）：采用 95% 分位"
                "CAD 法向余量，测得粗大区域 6.186 mm，超过既有 5 mm 复核门。"
                "该区域转 review；初检类别匹配由 1/2 提升至 2/2，"
                "二值误报像素由 960 减至 23。其余区域仍独立验收；"
                f"最终最大余量 {coarse['final_stock']['remaining_peak_mm']:.3f} mm，"
                "整体仍需复核与处理。此 F1 的变化不代表完成了粗大余料打磨。",
            ]
        )
    lines.extend(
        [
            "",
            "完整逐次指标、每处缺陷余量、实际运动误差和具体碰撞部件：",
            "",
            f"- [修复版本 42 次明细]({root / 'final/report.md'})",
            f"- [矩阵 CSV]({root / 'final/matrix.csv'})",
            f"- [每处残余 CSV]({root / 'final/residual_regions.csv'})",
            f"- [所有路径与判定失败原因 CSV]({root / 'final/failures.csv'})",
            f"- [原版本 42 次明细]({root / 'baseline/report.md'})",
            f"- [隔离修复补丁]({root / 'repair_trials/repair.patch'})",
            "",
            "当前证据限于合成 097 与实际仿真积分驱动的几何余料去除。"
            "抓手/工件与砂带机的真实 CAD 干涉不能靠删除障碍或放宽阈值消除。"
            "三种种子不足以证明多 CAD 或真实扫描泛化，真实工艺参数仍需标定。",
        ]
    )
    (root / "summary.md").write_text("\n".join(lines) + "\n")
    print(
        {
            key: {k: v for k, v in value.items() if k != "rows"}
            for key, value in document.items()
        }
    )


if __name__ == "__main__":
    main()
