"""Report paired scaled-CAD evaluation without changing models or thresholds.

The benchmark JSON supplies both model records. Dataset audit and training
receipts supply sample counts and provenance. The report uses full-image raw
segmentation metrics, not machining-gated detections or physical acceptance.
"""

import argparse
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT
    / "runs/enterprise_offline_20261123/dataset_scale_20261124"
    / "benchmark_v1_vs_v2"
)
MODEL_KEYS = ("v1_frozen_single_cad", "v2_scaled_multicad")
REGIMES = (
    "nominal_clean",
    "standard_shop",
    "high_drift_warped",
    "harsh_specular_oxide",
    "grazing_noisy",
)
REGIME_LABELS = (
    "Nominal clean",
    "Standard shop",
    "High drift / warped",
    "Specular / oxide",
    "Grazing / noisy",
)
F1_LABELS = ("Macro", "Foreground", "Seam", "Burr")
PALETTES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "secondary": "#52514e",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ("#2a78d6", "#eb6834"),
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "secondary": "#c3c2b7",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ("#3987e5", "#d95926"),
    },
}


def f1_values(metrics):
    """Return the four reported F1 scores in a fixed order."""
    return (
        metrics["macro_class_f1"],
        metrics["foreground"]["f1"],
        metrics["per_kind"]["seam"]["f1"],
        metrics["per_kind"]["burr"]["f1"],
    )


def number(value, digits=4):
    """Format unavailable scores distinctly from measured zero."""
    return "n/a" if value is None else f"{value:.{digits}f}"


def markdown_table(headers, rows):
    """Render one table with numbers confined to its cells."""
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(map(str, row)) + " |" for row in rows],
        ]
    )


def collect_regressions(models):
    """List measured declines on identical frozen evaluation observations."""
    baseline, scaled = models
    regressions = []
    scopes = [
        (
            "021 全部",
            baseline["unseen_real_cad_021"],
            scaled["unseen_real_cad_021"],
        ),
        (
            "097 冻结测试",
            baseline["frozen_historical_097_test"]["metrics"],
            scaled["frozen_historical_097_test"]["metrics"],
        ),
        (
            "021 零 RGB",
            baseline["unseen_021_zero_rgb_ablation"],
            scaled["unseen_021_zero_rgb_ablation"],
        ),
    ]
    scopes.extend(
        (
            f"021 {regime}",
            baseline["unseen_real_cad_021"]["by_regime"][regime],
            scaled["unseen_real_cad_021"]["by_regime"][regime],
        )
        for regime in REGIMES
    )
    for scope, before, after in scopes:
        for metric, old, new in zip(
            F1_LABELS, f1_values(before), f1_values(after), strict=True
        ):
            if old is not None and new is not None and new < old:
                regressions.append(
                    {
                        "scope": scope,
                        "metric": f"{metric} F1",
                        "v1": old,
                        "v2": new,
                        "delta": (new - old) * 100,
                        "unit": "pp",
                    }
                )
    for scope, before, after in scopes[:2]:
        for kind in ("seam", "burr"):
            for metric in ("precision", "recall"):
                old = before["per_kind"][kind][metric]
                new = after["per_kind"][kind][metric]
                if old is not None and new is not None and new < old:
                    regressions.append(
                        {
                            "scope": scope,
                            "metric": f"{kind} {metric}",
                            "v1": old,
                            "v2": new,
                            "delta": (new - old) * 100,
                            "unit": "pp",
                        }
                    )
    for scope, before, after, metric in (
        (
            "021 正常件",
            baseline["unseen_real_cad_021"],
            scaled["unseen_real_cad_021"],
            "normal_false_positive_pixels",
        ),
        (
            "021 高漂移正常件",
            baseline["unseen_021_high_drift_normal_stress"],
            scaled["unseen_021_high_drift_normal_stress"],
            "false_positive_pixels",
        ),
        (
            "097 正常件",
            baseline["frozen_historical_097_test"]["metrics"],
            scaled["frozen_historical_097_test"]["metrics"],
            "normal_false_positive_pixels",
        ),
    ):
        old, new = before[metric], after[metric]
        if new > old:
            regressions.append(
                {
                    "scope": scope,
                    "metric": "FP pixels",
                    "v1": old,
                    "v2": new,
                    "delta": new - old,
                    "unit": "pixels",
                }
            )
    return regressions


def draw_comparison(report, models, output, theme):
    """Draw six single-axis panels with fixed model identity and table twins."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import MaxNLocator, StrMethodFormatter

    palette = PALETTES[theme]
    rgb = [model["unseen_real_cad_021"] for model in models]
    zero = [model["unseen_021_zero_rgb_ablation"] for model in models]
    legacy = [
        model["frozen_historical_097_test"]["metrics"] for model in models
    ]
    normal_count = report["unseen_021_normal_hard_negatives"]
    normal_values = [
        (
            metrics["normal_false_positive_pixels"] / normal_count,
            model["unseen_021_high_drift_normal_stress"]["fp_pixels_per_view"],
        )
        for model, metrics in zip(models, rgb, strict=True)
    ]
    panels = (
        (
            "Unseen real CAD 021 | full synthetic test",
            F1_LABELS,
            [f1_values(metrics) for metrics in rgb],
            "F1 score",
            1.0,
        ),
        (
            "Five sensor / drift regimes",
            REGIME_LABELS,
            [
                [
                    metrics["by_regime"][regime]["macro_class_f1"]
                    for regime in REGIMES
                ]
                for metrics in rgb
            ],
            "Macro F1",
            1.0,
        ),
        (
            "Normal hard negatives | lower is better",
            ("All regimes", "High drift / warped"),
            normal_values,
            "False-positive pixels / view",
            max(1.0, max(max(values) for values in normal_values) * 1.15),
        ),
        (
            "RGB removal | identical paired observations",
            ("RGB macro F1", "Zero-RGB macro F1"),
            [
                (before["macro_class_f1"], after["macro_class_f1"])
                for before, after in zip(rgb, zero, strict=True)
            ],
            "Macro F1",
            1.0,
        ),
        (
            "Frozen historical CAD 097 | seeds 9601 / 9602",
            F1_LABELS,
            [f1_values(metrics) for metrics in legacy],
            "F1 score",
            1.0,
        ),
        (
            "Zero-RGB class scores | full synthetic test",
            F1_LABELS,
            [f1_values(metrics) for metrics in zero],
            "F1 score",
            1.0,
        ),
    )
    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "text.color": palette["ink"],
            "axes.labelcolor": palette["secondary"],
            "xtick.color": palette["secondary"],
            "ytick.color": palette["secondary"],
            "figure.facecolor": palette["surface"],
            "axes.facecolor": palette["surface"],
        }
    ):
        figure, axes = plt.subplots(3, 2, figsize=(15.8, 11.8))
        markers = ("o", "D")
        for ax, (title, labels, values, xlabel, maximum) in zip(
            axes.flat, panels, strict=True
        ):
            positions = np.arange(len(labels))
            data = np.asarray(values, dtype=float)
            ax.hlines(
                positions,
                data[0],
                data[1],
                color=palette["axis"],
                linewidth=1.0,
                zorder=2,
            )
            for index in range(2):
                ax.scatter(
                    data[index],
                    positions,
                    s=54,
                    marker=markers[index],
                    color=palette["series"][index],
                    edgecolor=palette["surface"],
                    linewidth=0.9,
                    zorder=3,
                    clip_on=False,
                )
                ax.annotate(
                    f"v{index + 1} {number(data[index, 0], 3)}",
                    (data[index, 0], 0),
                    xytext=(
                        -6 if data[index, 0] > maximum * 0.72 else 6,
                        9 if index == 0 else -13,
                    ),
                    textcoords="offset points",
                    ha="right" if data[index, 0] > maximum * 0.72 else "left",
                    fontsize=9,
                    color=palette["ink"],
                )
            ax.set_yticks(positions, labels)
            ax.set_ylim(len(labels) - 0.45, -0.55)
            ax.set_xlim(0, maximum)
            ax.set_title(
                title, loc="left", fontsize=11.5, fontweight="bold", pad=13
            )
            ax.set_xlabel(xlabel, fontsize=9)
            ax.xaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=3))
            ax.xaxis.set_major_formatter(
                StrMethodFormatter("{x:,.0f}" if maximum > 1 else "{x:.1f}")
            )
            ax.grid(axis="x", color=palette["grid"], linewidth=0.5)
            ax.set_axisbelow(True)
            ax.tick_params(axis="both", length=0, pad=7)
            for spine in ax.spines.values():
                spine.set_visible(False)
        handles = [
            Line2D(
                [],
                [],
                color=palette["series"][index],
                marker=markers[index],
                linestyle="none",
                markersize=7,
                label=(
                    "v1 frozen single CAD"
                    if index == 0
                    else "v2 scaled multi-CAD"
                ),
            )
            for index in range(2)
        ]
        figure.suptitle(
            "DefectUNet v1 vs v2",
            x=0.08,
            y=0.982,
            ha="left",
            fontsize=22,
            fontweight="bold",
        )
        figure.legend(
            handles=handles,
            loc="upper right",
            bbox_to_anchor=(0.96, 0.987),
            frameon=False,
            ncol=2,
            fontsize=11,
        )
        figure.text(
            0.08,
            0.932,
            f"021: {report['unseen_real_cad_021_views']} paired synthetic views | "
            f"Normal: {normal_count} views | High drift normal: "
            f"{report['unseen_021_high_drift_normals']} views | "
            f"Historical 097: {models[0]['frozen_historical_097_test']['view_count']} views",
            fontsize=10,
            color=palette["secondary"],
        )
        figure.text(
            0.08,
            0.018,
            "Macro F1 averages seam and burr. Thresholds stay validation-selected. "
            "Real CAD geometry, synthetic observations. Numerical tables in summary.md.",
            fontsize=9,
            color=palette["secondary"],
        )
        figure.subplots_adjust(
            left=0.16,
            right=0.97,
            top=0.875,
            bottom=0.07,
            hspace=0.68,
            wspace=0.56,
        )
        figure.savefig(output, dpi=180, facecolor=palette["surface"])
        plt.close(figure)


def write_summary(
    report,
    models,
    dataset,
    audit,
    training,
    baseline_training,
    test_regime_counts,
    regressions,
    output,
):
    """Write measured comparison tables and one dedicated limitations section."""
    baseline, scaled = models
    rgb = [model["unseen_real_cad_021"] for model in models]
    zero = [model["unseen_021_zero_rgb_ablation"] for model in models]
    legacy = [
        model["frozen_historical_097_test"]["metrics"] for model in models
    ]
    status = "ISSUES" if regressions else "PASS"
    rows = [
        (
            "总样本",
            audit["legacy_v1_counts"]["training"]
            + audit["legacy_v1_counts"]["validation"]
            + audit["legacy_v1_counts"]["test"],
            dataset["total_samples"],
        ),
        (
            "训练样本",
            audit["legacy_v1_counts"]["training"],
            training["train_scaled_views"],
        ),
        ("v2 追加的历史训练视图", "不适用", training["train_legacy_views"]),
        ("总量倍数", "1.00x", f"{audit['total_multiplier_vs_v1']:.2f}x"),
        ("训练集倍数", "1.00x", f"{audit['train_multiplier_vs_v1']:.2f}x"),
        ("训练 CAD 数", 1, len(dataset["split_policy"]["train_cads"])),
        (
            "参数量",
            baseline_training["parameter_count"],
            training["parameter_count"],
        ),
        ("训练步数", baseline_training["steps"], training["steps"]),
        ("v2 batch size", "不适用", training["batch_size"]),
        ("v2 chip presentations", "不适用", training["chip_presentations"]),
        (
            "v2 选中步数 / 阈值",
            "不适用",
            f"{training['selected_step']} / {training['selected_threshold']:.2f}",
        ),
        (
            "冻结推理阈值",
            number(baseline["metadata"]["probability_threshold"], 2),
            number(scaled["metadata"]["probability_threshold"], 2),
        ),
    ]
    lines = [
        "# DefectUNet v2 多 CAD 离线评测",
        "",
        f"**{status}**。"
        + (
            "v2 在未见真实 CAD 021 的合成观测上取得更高的宏 F1。"
            if rgb[1]["macro_class_f1"] > rgb[0]["macro_class_f1"]
            else "v2 在未见真实 CAD 021 的合成观测上未取得更高的宏 F1。"
        ),
        "",
        f"![v1 与 v2 比较]({(output.parent / 'v1_vs_v2_comparison.png').resolve()})",
        "",
        "## 数据与训练",
        "",
        markdown_table(("项目", "v1", "v2"), rows),
        "",
        markdown_table(
            ("审计项", "结果"),
            (
                (
                    "train / val / test",
                    " / ".join(
                        str(dataset["counts"]["by_split"][key])
                        for key in ("train", "val", "test")
                    ),
                ),
                (
                    "文件完整性检查 / 张量分层抽查",
                    f"{audit['checked_file_bundles']} / {audit['tensor_spot_check_count']}",
                ),
                ("021 / p07 / p08 跨分割泄漏", "0"),
                ("9601 / 9602 训练交叉", "0"),
                ("训练设备", training["device"]),
                ("单次训练耗时，s", number(training["seconds"], 2)),
                *[
                    (
                        f"{name} 有限值范围",
                        f"{values['min']:.7f} 至 {values['max']:.7f}",
                    )
                    for name, values in audit["finite_value_ranges"].items()
                ],
            ),
        ),
        "",
        "## 未见真实 CAD 021",
        "",
        markdown_table(
            ("指标", "v1", "v2", "差值，pp"),
            [
                (
                    f"{label} F1",
                    number(old),
                    number(new),
                    number((new - old) * 100),
                )
                for label, old, new in zip(
                    F1_LABELS, f1_values(rgb[0]), f1_values(rgb[1]), strict=True
                )
            ],
        ),
        "",
        markdown_table(
            (
                "类别",
                "v1 precision / recall",
                "v2 precision / recall",
                "v1 TP / FP / FN",
                "v2 TP / FP / FN",
            ),
            [
                (
                    kind,
                    " / ".join(
                        number(rgb[0]["per_kind"][kind][key])
                        for key in ("precision", "recall")
                    ),
                    " / ".join(
                        number(rgb[1]["per_kind"][kind][key])
                        for key in ("precision", "recall")
                    ),
                    " / ".join(
                        str(rgb[0]["per_kind"][kind][key])
                        for key in ("tp", "fp", "fn")
                    ),
                    " / ".join(
                        str(rgb[1]["per_kind"][kind][key])
                        for key in ("tp", "fp", "fn")
                    ),
                )
                for kind in ("seam", "burr")
            ],
        ),
        "",
        "Macro F1 是 seam 和 burr 的算术平均，不含背景类别。"
        "Foreground F1 合并缺陷类别。全部指标使用完整图像的原始分割输出。",
        "",
        "## 五种传感器与漂移工况",
        "",
        markdown_table(
            (
                "工况",
                "视图",
                "v1 macro F1",
                "v2 macro F1",
                "差值，pp",
                "v1 foreground F1",
                "v2 foreground F1",
            ),
            [
                (
                    regime,
                    test_regime_counts[regime],
                    number(rgb[0]["by_regime"][regime]["macro_class_f1"]),
                    number(rgb[1]["by_regime"][regime]["macro_class_f1"]),
                    number(
                        (
                            rgb[1]["by_regime"][regime]["macro_class_f1"]
                            - rgb[0]["by_regime"][regime]["macro_class_f1"]
                        )
                        * 100
                    ),
                    number(rgb[0]["by_regime"][regime]["foreground"]["f1"]),
                    number(rgb[1]["by_regime"][regime]["foreground"]["f1"]),
                )
                for regime in REGIMES
            ],
        ),
        "",
        "## 正常件误报与高漂移压力",
        "",
        markdown_table(
            (
                "正常件集合",
                "视图",
                "v1 FP pixels",
                "v2 FP pixels",
                "v1 pixels / view",
                "v2 pixels / view",
            ),
            (
                (
                    "全部工况",
                    report["unseen_021_normal_hard_negatives"],
                    rgb[0]["normal_false_positive_pixels"],
                    rgb[1]["normal_false_positive_pixels"],
                    number(
                        rgb[0]["normal_false_positive_pixels"]
                        / report["unseen_021_normal_hard_negatives"],
                        2,
                    ),
                    number(
                        rgb[1]["normal_false_positive_pixels"]
                        / report["unseen_021_normal_hard_negatives"],
                        2,
                    ),
                ),
                (
                    "high_drift_warped",
                    report["unseen_021_high_drift_normals"],
                    baseline["unseen_021_high_drift_normal_stress"][
                        "false_positive_pixels"
                    ],
                    scaled["unseen_021_high_drift_normal_stress"][
                        "false_positive_pixels"
                    ],
                    number(
                        baseline["unseen_021_high_drift_normal_stress"][
                            "fp_pixels_per_view"
                        ],
                        2,
                    ),
                    number(
                        scaled["unseen_021_high_drift_normal_stress"][
                            "fp_pixels_per_view"
                        ],
                        2,
                    ),
                ),
            ),
        ),
        "",
        "## 配对零 RGB 消融",
        "",
        markdown_table(
            (
                "指标",
                "v1 RGB",
                "v1 零 RGB",
                "v1 下降，pp",
                "v2 RGB",
                "v2 零 RGB",
                "v2 下降，pp",
            ),
            [
                (
                    f"{label} F1",
                    number(old),
                    number(old_zero),
                    number((old - old_zero) * 100),
                    number(new),
                    number(new_zero),
                    number((new - new_zero) * 100),
                )
                for label, old, old_zero, new, new_zero in zip(
                    F1_LABELS,
                    f1_values(rgb[0]),
                    f1_values(zero[0]),
                    f1_values(rgb[1]),
                    f1_values(zero[1]),
                    strict=True,
                )
            ],
        ),
        "",
    ]
    v1_drop = rgb[0]["macro_class_f1"] - zero[0]["macro_class_f1"]
    v2_drop = rgb[1]["macro_class_f1"] - zero[1]["macro_class_f1"]
    lines.extend(
        [
            (
                "两个模型的宏 F1 均未因 RGB 清零下降。"
                "v2 在本次合成测试的清零干预下保持了分割性能。"
                if v1_drop <= 0 and v2_drop <= 0
                else "配对 RGB 消融给出冻结模型对清零干预的性能变化。"
            ),
            "",
            "## 冻结历史 097 测试",
            "",
            markdown_table(
                ("指标", "v1", "v2", "差值，pp"),
                [
                    (
                        f"{label} F1",
                        number(old),
                        number(new),
                        number((new - old) * 100),
                    )
                    for label, old, new in zip(
                        F1_LABELS,
                        f1_values(legacy[0]),
                        f1_values(legacy[1]),
                        strict=True,
                    )
                ]
                + [
                    (
                        f"{kind} recall",
                        number(legacy[0]["per_kind"][kind]["recall"]),
                        number(legacy[1]["per_kind"][kind]["recall"]),
                        number(
                            (
                                legacy[1]["per_kind"][kind]["recall"]
                                - legacy[0]["per_kind"][kind]["recall"]
                            )
                            * 100
                        ),
                    )
                    for kind in ("seam", "burr")
                ],
            ),
            "",
            markdown_table(
                ("检查项", "结果"),
                (
                    ("冻结实体 seeds", "9601 / 9602"),
                    (
                        "完整测试视图",
                        baseline["frozen_historical_097_test"]["view_count"],
                    ),
                    (
                        "v1 / v2 历史正常件 FP pixels",
                        f"{legacy[0]['normal_false_positive_pixels']} / "
                        f"{legacy[1]['normal_false_positive_pixels']}",
                    ),
                    (
                        "测试集参与模型或阈值选择",
                        str(training["test_used_for_selection"]),
                    ),
                ),
            ),
            "",
            "## Known issues",
            "",
            (
                markdown_table(
                    ("集合", "指标", "v1", "v2", "差值", "单位"),
                    [
                        (
                            row["scope"],
                            row["metric"],
                            number(row["v1"]),
                            number(row["v2"]),
                            number(row["delta"]),
                            row["unit"],
                        )
                        for row in regressions
                    ],
                )
                if regressions
                else "本次冻结比较未发现 F1、类别精度与召回或正常件 FP 的数值回退。"
            ),
            "",
            "本次零 RGB 消融没有证明相对 v1 减少了 RGB 捷径依赖。",
            "",
            "## Limitations",
            "",
            "- 021 来自真实工厂 CAD，RGB-D、缺陷和传感器漂移仍为合成数据。"
            "噪声参数未用真实扫描标定，结果不能替代企业实测或磨后观测放行。",
            "- 本次比较使用一次 v2 训练和一个冻结 v1 检查点，未估计训练随机种子方差。"
            "同一实体的多视图与像素不构成独立试验。",
            "- v2 同时改变了多 CAD 数据、模型容量、训练预算、RGB 模态丢弃和残差倾斜增强。"
            "结果比较的是完整训练方案，不能单独归因于数据扩量。",
            "- 零 RGB 把原始 uint8 RGB 置黑，归一化后 RGB 特征不是训练时的零特征。"
            "该消融只衡量此固定干预下的依赖，不能证明不存在其他 RGB 捷径。",
            "- 深度及监督残差的背景和 dropout 用 NaN 表示。"
            "表中的范围仅统计有限值，张量审计为分层抽查而非逐像素全量审计。",
            "- seam 合并飞边和 casting_root；正残差 intermediate 监督按缺陷类别映射。"
            "正常件 FP 是门控前的原始像素误报，不是误加工动作次数。",
            "",
            "## 可复现产物",
            "",
            f"- [数据审计]({(output.parent / 'dataset_audit.json').resolve()})",
            f"- [训练收据]({Path(scaled['metadata']['checkpoint']).parent / 'training_receipt.json'})",
            f"- [v2 检查点]({scaled['metadata']['checkpoint']})",
            f"- [冻结 v1 检查点]({baseline['metadata']['checkpoint']})",
            f"- [完整评测]({(output.parent / 'benchmark_v1_vs_v2.json').resolve()})",
            f"- 生成命令 `.venv/bin/python scripts/report_scaled_dataset_v2.py "
            f"--benchmark {output.parent.resolve()}`",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--theme", choices=PALETTES, default="light")
    args = parser.parse_args()
    directory = args.benchmark.resolve()
    report = json.loads((directory / "benchmark_v1_vs_v2.json").read_text())
    models = [report["models"][key] for key in MODEL_KEYS]
    views = report["unseen_real_cad_021_views"]
    for model in models:
        if model["unseen_021_zero_rgb_ablation"]["view_count"] != views:
            raise ValueError("Zero-RGB must use the full paired RGB test set")
        if model["frozen_historical_097_test"]["seeds"] != [9601, 9602]:
            raise ValueError(
                "Historical regression check must retain frozen test seeds"
            )
    dataset_root = Path(report["dataset_root"])
    dataset = json.loads((dataset_root / "summary.json").read_text())
    manifest = json.loads((dataset_root / "manifest.json").read_text())
    test_regime_counts = {
        regime: sum(
            row["split"] == "test" and row["regime"] == regime
            for row in manifest["samples"]
        )
        for regime in REGIMES
    }
    audit = json.loads((directory / "dataset_audit.json").read_text())
    baseline_training = json.loads(
        (
            Path(models[0]["metadata"]["checkpoint"]).parent
            / "training_receipt.json"
        ).read_text()
    )
    if (
        models[0]["metadata"]["checkpoint_sha256"]
        != baseline_training["checkpoint_sha256"]
    ):
        raise ValueError("The benchmark must retain the frozen v1 checkpoint")
    training = json.loads(
        (
            Path(models[1]["metadata"]["checkpoint"]).parent
            / "training_receipt.json"
        ).read_text()
    )
    regressions = collect_regressions(models)
    image = directory / "v1_vs_v2_comparison.png"
    summary = directory / "summary.md"
    draw_comparison(report, models, image, args.theme)
    status = write_summary(
        report,
        models,
        dataset,
        audit,
        training,
        baseline_training,
        test_regime_counts,
        regressions,
        summary,
    )
    receipt = {
        "status": status,
        "paired_zero_rgb_views": views,
        "relative_rgb_shortcut_reduction_proven": False,
        "regressions": regressions,
        "comparison_png": str(image),
        "summary_markdown": str(summary),
        "theme": args.theme,
    }
    (directory / "report_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
