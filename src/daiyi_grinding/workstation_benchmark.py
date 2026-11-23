"""Frozen held-out current-cell experiments with paired planner comparisons."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from .workstation_model import source_hashes, write_json
from .workstation_run import parser as run_parser
from .workstation_run import run


def fraction(value):
    return "—" if value is None else f"{value * 100:.2f}%"


def summarize(episodes):
    counts = {
        name: sum(row["metrics"][name] for row in episodes)
        for name in ("tp", "fp", "fn", "tn")
    }
    tp, fp, fn, tn = [counts[name] for name in ("tp", "fp", "fn", "tn")]
    generated = sum(row["generated_defects"] for row in episodes)
    visible = sum(row["metrics"]["visible_defect_count"] for row in episodes)
    matched = sum(row["metrics"]["matched_defect_count"] for row in episodes)
    segments = sum(row["segments"] for row in episodes)
    accepted = sum(row["accepted_segments"] for row in episodes)
    baseline = sum(row["baseline_accepted_segments"] for row in episodes)
    return {
        "independent_episodes": len(episodes),
        "cad_workpieces": 1,
        "repeated_views": sum(row["view_count"] for row in episodes),
        **counts,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
        "accuracy": (tp + tn) / (tp + fp + fn + tn)
        if tp + fp + fn + tn
        else None,
        "generated_defects": generated,
        "visible_defects": visible,
        "matched_defects": matched,
        "object_recall_all_generated": matched / generated
        if generated
        else None,
        "object_recall_visible": matched / visible if visible else None,
        "segments": segments,
        "accepted_segments": accepted,
        "complete_segment_rate": accepted / segments if segments else None,
        "baseline_accepted_segments": baseline,
        "baseline_complete_segment_rate": baseline / segments
        if segments
        else None,
        "normal_false_positive_pixels": sum(
            row["metrics"]["fp"] for row in episodes if row["case"] == "normal"
        ),
        "normal_automatic_paths": sum(
            row["segments"] for row in episodes if row["case"] == "normal"
        ),
        "unexpected_preflight_contacts_on_admitted_plans": sum(
            row["preflight_contacts"]
            for row in episodes
            if row["preflight_passed"]
        ),
        "dynamics_episodes": sum(row["robot_executed"] for row in episodes),
        "goal_complete_episodes": sum(row["goal_complete"] for row in episodes),
        "scope": (
            "independent synthetic seeds on one supplied CAD casting; "
            "views within an episode are correlated; paired planner comparison "
            "uses the same observed paths; no real-scanned test set or weights"
        ),
    }


def report(out, episodes, summary, development_seeds):
    text = [
        "# 当前 ESTUN 砂带工位独立验证",
        "",
        f"独立工况：{len(episodes)} 次；CAD：097 一种；"
        f"分度观测：{summary['repeated_views']} 帧（同一工况的帧不是独立样本）。",
        "",
        "| 指标 | 本次结果 |",
        "|---|---:|",
        f"| 可见缺陷像素 Precision | {fraction(summary['precision'])} |",
        f"| 可见缺陷像素 Recall | {fraction(summary['recall'])} |",
        f"| 可见缺陷像素 F1 | {fraction(summary['f1'])} |",
        f"| 可见缺陷像素 IoU | {fraction(summary['iou'])} |",
        f"| 缺陷对象检出 / 全部生成 | {summary['matched_defects']} / {summary['generated_defects']} |",
        f"| 缺陷对象检出 / 可见 | {summary['matched_defects']} / {summary['visible_defects']} |",
        f"| 逐段规划完整验收 | {summary['accepted_segments']} / {summary['segments']} |",
        f"| 同观测原局部单姿态规划完整验收 | {summary['baseline_accepted_segments']} / {summary['segments']} |",
        f"| 正常工件误报像素 / 自动路径 | {summary['normal_false_positive_pixels']} / {summary['normal_automatic_paths']} |",
        f"| 已验收计划的预检非预期接触 | {summary['unexpected_preflight_contacts_on_admitted_plans']} |",
        f"| 完整动力学执行工况 | {summary['dynamics_episodes']} |",
        "",
        "| 工况 | 种子 | 检出/可见/生成 | F1 | 安全段/总段 | 原局部规划 | 采集 s | 规划 s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in episodes:
        metrics = row["metrics"]
        text.append(
            f"| {row['case']} | {row['seed']} | "
            f"{metrics['matched_defect_count']}/{metrics['visible_defect_count']}/"
            f"{row['generated_defects']} | {fraction(metrics['f1'])} | "
            f"{row['accepted_segments']}/{row['segments']} | "
            f"{row['baseline_accepted_segments']} | "
            f"{row['capture_seconds']:.2f} | {row['planning_seconds']:.2f} |"
        )
    text.extend(
        [
            "",
            "像素混淆计数："
            f"TP={summary['tp']}，FP={summary['fp']}，FN={summary['fn']}，"
            f"TN={summary['tn']}。总体 Accuracy 受正常像素占比影响，不作为主指标。",
            "",
            "每次工况保存六帧 RGB-D、相机/编码器变换、逐段拒绝原因、源码快照和哈希。"
            "GT 在检测和预测之后读取，只用于打分，不用于决定复检后的停止或补磨。"
            f"开发及调试种子为 {', '.join(map(str, development_seeds))}；"
            "本报告的种子与它们互斥，参数在首次观测这些新种子前冻结。",
            "",
            "王释钰 11/19 的 11/75（14.7%）来自另外四种工件和未公开的实现，"
            "这里只作组会背景，不能与本表作成对优劣结论。"
            "曹正恒实际复用部分为固定提交的 CPU 轨迹物理滤波代码；"
            "SAM 与训练动作权重缺失，未声称复现其训练模型。",
            "",
            "上述验收为几何预检。没有执行动力学的工况不提供运动跟踪、加工完成率或"
            "表面质量结论。真实相机外参、夹持标定、砂带工艺参数和真实缺陷数据仍需接入。",
            "",
        ]
    )
    (out / "report.md").write_text("\n".join(text))
    with (out / "episodes.csv").open("w", newline="") as file:
        fields = [
            "case",
            "seed",
            "generated_defects",
            "segments",
            "accepted_segments",
            "baseline_accepted_segments",
            "capture_seconds",
            "planning_seconds",
            "preflight_passed",
            "robot_executed",
            "goal_complete",
        ]
        writer = csv.DictWriter(file, fields)
        writer.writeheader()
        for row in episodes:
            writer.writerow({key: row[key] for key in fields})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seeds", default="9201,9202")
    parser.add_argument("--development-seeds", default="7007,9101,9102")
    parser.add_argument(
        "--cases", default="mixed,seam,burr,thin,normal,coarse,noisy"
    )
    parser.add_argument("--views", choices=("single", "multi"), default="multi")
    parser.add_argument("--tool", choices=("belt", "wheel"), default="belt")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    seeds = [int(value) for value in args.seeds.split(",")]
    development_seeds = [
        int(value) for value in args.development_seeds.split(",")
    ]
    if set(seeds) & set(development_seeds):
        raise ValueError("开发或调试种子不可混入独立验证")
    hashes = source_hashes()
    write_json(out / "source_hashes.json", hashes)
    write_json(
        out / "protocol.json",
        {
            **vars(args),
            "development_seeds": development_seeds,
            "holdout_seeds": seeds,
            "frozen_before_holdout": True,
            "source_hashes": hashes,
        },
    )
    episodes = []
    started = time.monotonic()
    for case in args.cases.split(","):
        for seed in seeds:
            if source_hashes() != hashes:
                raise RuntimeError(
                    "源码在独立验证中改变；停止，避免混合版本结果"
                )
            name = f"{case}_{seed}"
            options = [
                "--seed",
                str(seed),
                "--case",
                case,
                "--output",
                str(out / name),
                "--views",
                args.views,
                "--tool",
                args.tool,
                "--compare-planners",
                "--realtime-factor",
                "0",
            ]
            if not args.execute:
                options.append("--perception-only")
            result = run(run_parser().parse_args(options))
            comparison = json.loads(
                (out / name / "planner_comparison.json").read_text()
            )
            row = {
                "case": case,
                "seed": seed,
                "directory": name,
                "metrics": result["perception"]["metrics"],
                "generated_defects": result["perception"]["generated_defects"],
                "view_count": result["perception"]["view_count"],
                "capture_seconds": result["perception"]["seconds"],
                "planning_seconds": result["preflight"]["seconds"],
                "segments": result["path_count"],
                "accepted_segments": result["admitted_path_count"],
                "baseline_accepted_segments": comparison["global"][
                    "admitted_count"
                ],
                "preflight_contacts": result["preflight"].get(
                    "contact_count", 0
                ),
                "preflight_passed": result["preflight"]["passed"],
                "robot_executed": result["robot_executed"],
                "goal_complete": result["goal_complete"],
            }
            episodes.append(row)
            summary = summarize(episodes)
            write_json(
                out / "benchmark.json",
                {
                    "summary": summary,
                    "episodes": episodes,
                    "wall_seconds": time.monotonic() - started,
                    "complete": False,
                    "source_hashes": hashes,
                },
            )
            report(out, episodes, summary, development_seeds)
            print(f"Holdout progress: {len(episodes)} episodes", flush=True)
    write_json(
        out / "benchmark.json",
        {
            "summary": summarize(episodes),
            "episodes": episodes,
            "wall_seconds": time.monotonic() - started,
            "complete": True,
            "source_hashes": hashes,
        },
    )
    print(f"Current-cell benchmark: {out / 'report.md'}", flush=True)


if __name__ == "__main__":
    main()
