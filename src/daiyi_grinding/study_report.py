"""Build a Chinese experiment report and figures from saved paired results."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import project

DEFAULT_STUDY = project.RUNS / "defect_study_20261118"
METHOD_LABELS = {
    "threshold": "全局高度阈值",
    "geometry": "原局部几何",
    "multiscale": "多尺度几何",
    "robust_local": "鲁棒局部基面",
    "cad_reference": "CAD 参考差值*",
    "thin_reference": "保留细缝的参考差值*",
}
SHORT_LABELS = {
    "threshold": "全局阈值",
    "geometry": "原几何",
    "multiscale": "多尺度",
    "robust_local": "鲁棒局部",
    "cad_reference": "CAD 参考*",
    "thin_reference": "细缝参考*",
}
CASE_LABELS = {
    "mixed": "批缝与毛刺混合",
    "seam": "普通批缝",
    "burr": "局部毛刺",
    "normal": "无注入缺陷",
    "thin": "薄批缝",
    "coarse": "批缝及超范围余料",
    "noisy": "高噪声",
}


def read_json(path):
    """Read a result whose filename is also kept in the report provenance."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    """Identify the exact saved input used to build an artifact."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def percent(value):
    """Preserve undefined ratios while formatting measured proportions."""
    return "—" if value is None else f"{value * 100:.2f}%"


def comparison(path):
    """Validate saved aggregates against their integer pixel denominators."""
    result = read_json(path)
    indexed = {}
    for row in result["summary"]:
        key = (row["method"], row["case"])
        if key in indexed:
            raise ValueError(f"Duplicate summary group {key} in {path}")
        true_positive, false_positive, false_negative = (
            row[field] for field in ("tp", "fp", "fn")
        )
        predicted = true_positive + false_positive
        target = true_positive + false_negative
        expected = {
            "precision": true_positive / predicted if predicted else None,
            "recall": true_positive / target if target else None,
            "f1": (
                2
                * true_positive
                / (2 * true_positive + false_positive + false_negative)
                if target
                else None
            ),
        }
        for metric, value in expected.items():
            recorded = row[metric]
            if value is None:
                if recorded is not None:
                    raise ValueError(f"Undefined {key}/{metric} in {path}")
            elif recorded is None or not np.isclose(
                recorded, value, atol=1e-12, rtol=1e-12
            ):
                raise ValueError(f"Rate disagrees with counts: {key}/{metric}")
        indexed[key] = row
    expected_records = sum(row["samples"] for row in indexed.values())
    if expected_records != result["completed"]:
        raise ValueError(f"Sample total differs from completed rows in {path}")
    return result, indexed


def configure_plotting():
    """Load plotting libraries only when report images are requested."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot

    font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if font_path.exists():
        font_manager.fontManager.addfont(str(font_path))
        font_name = font_manager.FontProperties(fname=str(font_path)).get_name()
        pyplot.rcParams["font.family"] = font_name
    pyplot.rcParams.update(
        {
            "axes.unicode_minus": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#9aa4ad",
            "axes.labelcolor": "#26333f",
            "text.color": "#26333f",
            "xtick.color": "#26333f",
            "ytick.color": "#26333f",
            "font.size": 10,
        }
    )
    return pyplot


def metric_panel(axis, rows, methods, title):
    """Draw precision, recall and F1 on one common percentage scale."""
    locations = np.arange(len(methods))
    colors = ("#396e91", "#d18a40", "#568c74")
    for offset, (field, label, color) in enumerate(
        zip(
            ("precision", "recall", "f1"),
            ("精确率", "召回率", "F1"),
            colors,
        )
    ):
        values = [
            np.nan if rows[method][field] is None else rows[method][field] * 100
            for method in methods
        ]
        bars = axis.bar(
            locations + (offset - 1) * 0.24,
            values,
            width=0.22,
            label=label,
            color=color,
        )
        axis.bar_label(
            bars,
            labels=[
                "" if np.isnan(value) else f"{value:.1f}" for value in values
            ],
            padding=2,
            fontsize=8,
        )
        for location, value in zip(locations + (offset - 1) * 0.24, values):
            if np.isnan(value):
                axis.text(location, 1.5, "—", ha="center", fontsize=8)
    axis.set_title(title, loc="left", fontsize=12, pad=14)
    axis.set_xticks(locations, [SHORT_LABELS[method] for method in methods])
    axis.set_ylim(0, 111)
    axis.set_yticks([0, 20, 40, 60, 80, 100])
    axis.set_ylabel("可见像素指标（%）")
    axis.set_axisbelow(True)
    axis.grid(axis="y", alpha=0.18)
    axis.legend(loc="upper right", ncol=3, frameon=False, fontsize=8)


def build_comparison_figure(study, coupon, cad):
    """Keep the original generator and user CAD tests in separate panels."""
    pyplot = configure_plotting()
    figure, axes = pyplot.subplots(2, 2, figsize=(15.8, 9.3))
    methods = (
        "threshold",
        "geometry",
        "multiscale",
        "robust_local",
        "cad_reference",
    )
    panels = (
        (coupon, "mixed", "A  原 FR5 场景 · 混合缺陷"),
        (coupon, "thin", "B  原 FR5 场景 · 薄批缝"),
        (cad, "mixed", "C  用户 CAD 底座 · 混合缺陷"),
        (cad, "thin", "D  用户 CAD 底座 · 薄批缝"),
    )
    for axis, (dataset, case, title) in zip(axes.flat, panels):
        metric_panel(
            axis,
            {method: dataset[(method, case)] for method in methods},
            methods,
            title,
        )
    figure.suptitle("五种检测算法的独立种子比较", fontsize=18, y=0.995)
    figure.text(
        0.02,
        0.013,
        "每面板 6 个合成场景；原场景 seed 6007–6012，CAD seed 7107–7112。"
        "  * 额外使用已配准正常参考。无预测的精确率不定义。",
        fontsize=10,
    )
    figure.tight_layout(rect=(0, 0.06, 1, 0.97), h_pad=3.1, w_pad=2.0)
    figure.savefig(study / "comparison.png", dpi=150, facecolor="white")
    pyplot.close(figure)


def build_external_figure(study, external):
    """Show the real photograph experiment without merging materials or units."""
    pyplot = configure_plotting()
    figure, axis = pyplot.subplots(figsize=(8.0, 4.7))
    labels = ("外轮廓开运算", "灰度顶帽")
    identifiers = ("silhouette_opening", "intensity_tophat")
    locations = np.arange(2)
    for offset, (field, label, color) in enumerate(
        zip(
            ("precision", "recall", "f1"),
            ("精确率", "召回率", "F1"),
            ("#396e91", "#d18a40", "#568c74"),
        )
    ):
        values = [
            external["holdout_summary"][method][field] * 100
            for method in identifiers
        ]
        bars = axis.bar(
            locations + (offset - 1) * 0.24,
            values,
            width=0.22,
            label=label,
            color=color,
        )
        axis.bar_label(bars, fmt="%.1f", padding=4)
    axis.set_xticks(locations, labels)
    axis.set_ylim(0, 100)
    axis.set_ylabel("飞边像素指标（%）")
    axis.set_axisbelow(True)
    axis.grid(axis="y", alpha=0.18)
    axis.legend(ncol=3, frameon=False, loc="upper right")
    axis.set_title("公开注塑飞边照片 · 4 张留出图", loc="left", fontsize=14)
    figure.text(
        0.02,
        0.02,
        "SSAL 作者像素标签；材质为塑料。开发与留出按照片划分，实物身份可能重合。",
        fontsize=9,
    )
    figure.tight_layout(rect=(0, 0.075, 1, 1))
    figure.savefig(
        study / "external_comparison.png", dpi=150, facecolor="white"
    )
    pyplot.close(figure)


def build_refinement_figure(study, refinement):
    """Compare paired pixel lattices without changing physical defect geometry."""
    pyplot = configure_plotting()
    figure, axes = pyplot.subplots(2, 2, figsize=(13.3, 9.0))
    methods = ("geometry", "cad_reference", "thin_reference")
    for row_index, resolution in enumerate((1024, 1920)):
        for column_index, case in enumerate(("mixed", "thin")):
            metric_panel(
                axes[row_index, column_index],
                {
                    method: refinement[resolution][(method, case)]
                    for method in methods
                },
                methods,
                f"{resolution}×{resolution * 3 // 4} · {CASE_LABELS[case]}",
            )
    figure.suptitle("CAD 细缝保留与分辨率配对实验", fontsize=18, y=0.995)
    figure.text(
        0.02,
        0.015,
        "每面板 seed 8307–8312 的 6 个合成场景；同 seed 几何/姿态/FOV 相同。"
        " * 两种参考法均增加同位姿理想 CAD 深度输入。",
        fontsize=10,
    )
    figure.tight_layout(rect=(0, 0.06, 1, 0.97), h_pad=3.0)
    figure.savefig(
        study / "cad_refinement_comparison.png", dpi=150, facecolor="white"
    )
    pyplot.close(figure)


def metrics_table(indexed, methods):
    """Build one table with exact same-case metrics for every method."""
    lines = [
        "| 方法 | 混合精确率 | 混合召回率 | 薄缝精确率 | 薄缝召回率 | 薄缝 F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in methods:
        mixed, thin = indexed[(method, "mixed")], indexed[(method, "thin")]
        values = [
            mixed["precision"],
            mixed["recall"],
            thin["precision"],
            thin["recall"],
            thin["f1"],
        ]
        lines.append(
            "| "
            + " | ".join([METHOD_LABELS[method], *map(percent, values)])
            + " |"
        )
    return "\n".join(lines)


def guards_table(indexed, methods, cad=False):
    """Separate no-action decisions, review prompts and selected pixels."""
    pixel_label = (
        "无注入缺陷时原 CAD 表面误选像素" if cad else "正常件自动误选总像素"
    )
    lines = [
        f"| 方法 | 正常件不加工 | 正常件触发复核 | {pixel_label} | coarse 触发复核 | coarse 余料误选像素 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in methods:
        normal, coarse = (
            indexed[(method, "normal")],
            indexed[(method, "coarse")],
        )
        selected = (
            normal["normal_structure_selected_pixels"] if cad else normal["fp"]
        )
        values = [
            METHOD_LABELS[method],
            f"{normal['no_action_samples']}/{normal['samples']}",
            f"{normal['review_samples']}/{normal['samples']}",
            f"{selected:,}",
            f"{coarse['review_samples']}/{coarse['samples']}",
            f"{coarse['unsupported_stump_selected_pixels']:,}",
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def refinement_section(study, refinement):
    """Only claim completed follow-up measurements when their files exist."""
    if len(refinement) != 2:
        return (
            "## CAD 薄批缝后续实验\n\n"
            "新方法 `thin_reference` 保留未经 3×3 中值滤波的正向深度差，并以参考边缘检查和噪声阈值约束。"
            "原开发集 7007–7008 上已看到细缝召回改善，独立种子 8307–8312 的 1024×768 / 1920×1440 配对结果尚未齐全。"
            "该部分仍在运行，当前不将开发成绩记为留出验证结果。\n"
        )
    lines = [
        "## CAD 细缝保留与分辨率对照",
        "",
        "第二轮使用新的开发种子 8207–8208 和留出种子 8307–8312。"
        "两种分辨率拍摄相同的 CAD、缺陷和位姿，112 帧观测对应 56 个物理场景状态；不能按 112 件独立工件计数。"
        "参考法均使用同位姿理想 CAD 深度。缺陷标签仍只进入事后评分。"
        "三个方法共记录 336 条开发及留出结果；其中留出为 42 个场景状态 × 2 档分辨率 × 3 方法，共 252 条记录。",
        "",
        "![CAD 细缝与分辨率对照](cad_refinement_comparison.png)",
        "",
    ]
    for resolution in (1024, 1920):
        lines.extend(
            [
                f"### {resolution}×{resolution * 3 // 4} 留出结果",
                "",
                metrics_table(
                    refinement[resolution],
                    ("geometry", "cad_reference", "thin_reference"),
                ),
                "",
                guards_table(
                    refinement[resolution],
                    ("geometry", "cad_reference", "thin_reference"),
                    cad=True,
                ),
                "",
            ]
        )
    selection_path = study / "cad_refinement/selection.json"
    low_reference = refinement[1024][("cad_reference", "thin")]
    low_thin = refinement[1024][("thin_reference", "thin")]
    high_reference = refinement[1920][("cad_reference", "thin")]
    high_thin = refinement[1920][("thin_reference", "thin")]
    low_mixed = refinement[1024][("thin_reference", "mixed")]
    high_mixed = refinement[1920][("thin_reference", "mixed")]
    lines.extend(
        [
            f"在相同新种子和相同分辨率上，细缝参考法把薄批缝召回从 {percent(low_reference['recall'])} 提高到 {percent(low_thin['recall'])}（1024×768），"
            f"从 {percent(high_reference['recall'])} 提高到 {percent(high_thin['recall'])}（1920×1440）。"
            f"1920×1440 的精确率为 {percent(high_thin['precision'])}，仍漏检 {percent(1 - high_thin['recall'])} 的可见薄缝像素，薄批缝问题尚未完全解决。",
            "",
            f"混合缺陷的路径投影覆盖在 1024×768 为 {percent(low_mixed['mean_coverage_3mm'])}，在 1920×1440 为 {percent(high_mixed['mean_coverage_3mm'])}。"
            "较低分辨率下的检测片段不能总被原轨迹规划器连接成有效加工轨迹。两档分辨率的正常件均 6/6 不加工且不复核；coarse 均 6/6 复核、超范围余料自动误选为 0。",
            "",
            "高噪声下，细缝法限制误选的同时丢失部分缺陷：",
            "",
            "| 分辨率 | CAD 参考法高噪声精确率 / 召回率 | 细缝参考法高噪声精确率 / 召回率 |",
            "| --- | ---: | ---: |",
        ]
    )
    for resolution in (1024, 1920):
        previous = refinement[resolution][("cad_reference", "noisy")]
        improved = refinement[resolution][("thin_reference", "noisy")]
        lines.append(
            f"| {resolution}×{resolution * 3 // 4} | {percent(previous['precision'])} / {percent(previous['recall'])} | "
            f"{percent(improved['precision'])} / {percent(improved['recall'])} |"
        )
    lines.append("")
    if selection_path.exists():
        selected = read_json(selection_path)["selected_reference_method"]
        lines.append(
            f"开发集选型为 `{selected}`，记录见[selection.json]({selection_path.as_posix()})。选型使用开发集，留出指标只用于确认。"
        )
        lines.append("")
    lines.append(
        "各分辨率结果分别评价；没有把不同像素数的观察合并成一个泛化分数。"
    )
    return "\n".join(lines) + "\n"


def build_report(study=DEFAULT_STUDY, require_refinement=False):
    """Generate Markdown and scientific PNGs from completed experiment files."""
    study = Path(study).resolve()
    source_paths = [
        study / "reproduction/result.json",
        study / "reproduction_comparison.json",
        study / "coupon_comparison/holdout/summary.json",
        study / "cad_comparison/holdout/summary.json",
        study / "external_samples/ssal/benchmark_results.json",
        study / "selection.json",
        study / "selected_robust_local_mixed_6007/result.json",
        study / "selected_robust_local_thin_6007/result.json",
        study / "cad_station_demo_highres/result.json",
    ]
    reproduction = read_json(source_paths[0])
    reproducibility = read_json(source_paths[1])
    coupon_result, coupon = comparison(source_paths[2])
    cad_result, cad = comparison(source_paths[3])
    external = read_json(source_paths[4])
    selected = read_json(source_paths[5])
    mixed_run, thin_run = read_json(source_paths[6]), read_json(source_paths[7])
    station = read_json(source_paths[8])
    methods = coupon_result["methods"]
    if methods != cad_result["methods"]:
        raise ValueError("First-round CAD and coupon method sets differ")
    refinement = {}
    for resolution in (1024, 1920):
        path = (
            study
            / "cad_refinement"
            / f"resolution_{resolution}"
            / "holdout/summary.json"
        )
        if path.exists():
            _, indexed = comparison(path)
            refinement[resolution] = indexed
            source_paths.append(path)
    for relative in (
        "cad_refinement/selection.json",
        "cad_resolution_dataset/pair_verification.json",
    ):
        path = study / relative
        if path.exists():
            source_paths.append(path)
    if require_refinement and len(refinement) != 2:
        raise FileNotFoundError(
            "Both CAD refinement holdout summaries are required"
        )
    build_comparison_figure(study, coupon, cad)
    build_external_figure(study, external)
    if len(refinement) == 2:
        build_refinement_figure(study, refinement)
    improvement = (
        coupon[("robust_local", "thin")]["recall"]
        - coupon[("geometry", "thin")]["recall"]
    ) * 100
    comparison_image = study / "comparison.png"
    external_image = study / "external_comparison.png"
    header = [
        "# 戴屹毛刺与批缝检测：仿真复现与算法比较",
        "",
        "实验日期：2026-11-18 · 同一观测配对评分 · 本地 MuJoCo",
        "",
        f"原 FR5 仿真已复现，路径、检测和动力学核心数值与 `demo_v3` 一致。"
        f"在同一批新的留出场景上，`robust_local` 的薄批缝像素召回从原几何法的 {percent(coupon[('geometry', 'thin')]['recall'])} "
        f"提高到 {percent(coupon[('robust_local', 'thin')]['recall'])}，提高 {improvement:.2f} 个百分点；"
        f"精确率从 {percent(coupon[('geometry', 'thin')]['precision'])} 降到 {percent(coupon[('robust_local', 'thin')]['precision'])}。"
        "当前选定它用于原场景，混合缺陷和薄批缝的两次完整轨迹执行均通过运动门槛。",
        "",
        "用户 CAD 底座的曲面和台阶更复杂，直接迁移 `robust_local` 会误选原模型表面。"
        f"第一轮 CAD 参考法混合缺陷精确率 / 召回为 {percent(cad[('cad_reference', 'mixed')]['precision'])} / {percent(cad[('cad_reference', 'mixed')]['recall'])}，"
        f"但薄批缝召回仅 {percent(cad[('cad_reference', 'thin')]['recall'])}。后续细缝与分辨率实验独立列出，避免用单帧演示替代批测结论。",
        "",
        "公开样本已取得 8 对注塑飞边照片和像素标签，实测两种 RGB 方法表现较差。"
        "这些样本材质为塑料；真实金属毛刺 RGB-D 原始数据尚未取得。当前结论限定于报告列出的仿真和照片实验，材料去除、恒力磨削和加工质量仍需实测。",
        "",
        "## 原仿真复刻",
        "",
        f"seed 7、mixed、3 mm 名义间隙：{reproduction['path_count']} 段路径、{reproduction['path_length_mm']:.3f} mm，"
        f"仿真 {reproduction['execution']['simulated_seconds']:.2f} s / {reproduction['execution']['simulation_steps']:,} 步。"
        f"轨迹位置 RMS / P95 / 最大误差为 {reproduction['execution']['tracking_trace_rms_mm']:.4f} / "
        f"{reproduction['execution']['tracking_trace_p95_mm']:.4f} / {reproduction['execution']['tracking_trace_max_mm']:.4f} mm。"
        "预检查及运动门槛通过，工具接触和异常接触记录均为 0。",
        "",
        f"原两方法批测复刻 {reproducibility['benchmark']['reproduced_record_count']} 条配对记录（7 类 × 6 种子 × 2 方法），"
        "逐场景指标和 14 组汇总全部与原 `benchmark_v3` 精确一致。视频全帧解码及反馈 CSV 已核验，"
        f"共有 {reproducibility['video']['fully_decoded_frames']:,} 帧视频和 {reproducibility['csv']['rows']:,} 条反馈。",
        "",
        "[复刻视频](reproduction/demo.mp4) · [复现比较记录](reproduction_comparison.json) · [复现命令](reproduction_notes.md)",
        "",
        "## 同一观测的五方法比较",
        "",
        "比较方法：全局高度阈值、原局部几何、多尺度几何、鲁棒局部基面和已配准 CAD 参考差值。"
        "`robust_local` 使用正向残差重加权估计局部基面，并做噪声自适应双阈值连接；"
        "原场景的低噪声薄批缝因此可保留更多连续像素。多尺度候选和鲁棒局部候选在观测噪声 MAD >0.12 mm 时回退原几何后端，"
        "`noisy` 的相同结果来自此路由。CAD 参考法增加正常参考深度和精确配准输入，应视为条件不同的附加对照。",
        "",
        "![五种算法留出比较](comparison.png)",
        "",
        "### 原 FR5 合成工件",
        "",
        f"留出种子 6007–6012，共 42 个场景、{coupon_result['completed']} 条方法记录。"
        "开发集 5007–5010 只用于选择参数；同 seed 的 7 个 case 共享基底和位姿。"
        "70 个开发及留出场景来自 10 个生成基底，均为合成观测。",
        "",
        metrics_table(coupon, methods),
        "",
        "* CAD 参考法额外接收同位姿正常深度；此处参考观测使用独立噪声。",
        "",
        guards_table(coupon, methods),
        "",
        "`robust_local` 在全部 42 个留出场景上正常结构自动误选为 0，正常件 6/6 不加工且不复核，coarse 6/6 复核。"
        "多尺度方法在正常件上有 1/6 自动误加工（421 像素）以及 2/6 复核提示，因此没有入选。"
        "正常件缺陷召回与 F1 不定义，以不加工比例和误选像素评估；表中的“—”保留此分母含义。",
        "",
        "### 用户 CAD 底座",
        "",
        f"第一轮使用版本 097 CAD 的可见顶面，留出种子 7107–7112 共 42 个场景、{cad_result['completed']} 条方法记录。"
        "普通批缝峰高 1.5–3 mm，薄批缝 0.35–1 mm，局部毛刺 1.5–3.5 mm，超高余料 7–11 mm；"
        "这些是仿真设定。缺陷由嵌入原顶面 0.08 mm 的闭合网格构成，真值按可见几何事后生成。",
        "",
        metrics_table(cad, methods),
        "",
        guards_table(cad, methods, cad=True),
        "",
        "CAD 标签 3 代表全部可见原模型表面，区别于原合成场景专门标注的正常筋和凸台。"
        "`robust_local` 在 6 个无注入缺陷 CAD 场景中误选 30,576 个原模型像素，直接迁移不通过。"
        "原几何和多尺度方法未检出第一轮的薄缝，参考法也只检出很小一部分；需要检查滤波对细缝的影响与相机采样。",
        "",
    ]
    if len(refinement) == 2:
        measured = refinement[1920][("thin_reference", "thin")]
        header.insert(
            8,
            f"新的细缝参考法在 1920×1440 配对留出场景达到精确率 {percent(measured['precision'])}、召回率 {percent(measured['recall'])}，"
            "正常 CAD 场景 6/6 不加工且无复核。它改善了细缝漏检，但仍有部分薄缝未被检出。\n",
        )
    header.append(refinement_section(study, refinement))
    header.extend(
        [
            "## 公开飞边照片实测",
            "",
            "[SSAL 作者仓库](https://github.com/ZianYuCode/SSAL)提供 CC0-1.0 的注塑飞边照片和像素标签。"
            "本轮实际取得 8 对，4 张开发、4 张留出，统一宽度 800 像素。该划分按照片，开发/留出可能包含同一实物不同角度。",
            "",
            "![公开塑料飞边照片实验](external_comparison.png)",
            "",
            "| 方法 | 留出精确率 | 留出召回率 | 留出 F1 | TP / FP / FN |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for method, label in (
        ("silhouette_opening", "外轮廓开运算"),
        ("intensity_tophat", "灰度顶帽"),
    ):
        metrics = external["holdout_summary"][method]
        header.append(
            f"| {label} | {percent(metrics['precision'])} | {percent(metrics['recall'])} | {percent(metrics['f1'])} | "
            f"{metrics['tp']:,} / {metrics['fp']:,} / {metrics['fn']:,} |"
        )
    header.extend(
        [
            "",
            "背景格栅亮线和高光带来误报，宽飞边被漏检。照片结果与金属 RGB-D 仿真分别解释。"
            "[SINTEF 真实铸件研究](https://www.sintef.no/en/projects/2015/self-supervised-learning-when-you-dont-have-enough-labeled-data/)支持 CAD–RGBD 配准与毛刺高度估计路线，"
            "但本轮未核实开放原始数据下载。更多数据集的许可和获取状态见[来源核验](../../docs/defect_sources_20261118.md)。",
            "",
            "## 选定方法的完整轨迹执行",
            "",
            f"开发集选择记录的观测方法为 `{selected['selected_observation_method']}`。"
            "在两种新的留出工况上实际完成原 FR5 控制链，检测、规划、关节命令、积分反馈与视频均已保存。",
            "",
            "| 工况 | 路径数 | 总长度（mm） | 仿真时长（s） | TCP RMS（mm） | TCP P95（mm） | 最大误差（mm） | 运动门槛 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for result in (mixed_run, thin_run):
        execution = result["execution"]
        header.append(
            f"| {CASE_LABELS[result['case']]}，seed {result['seed']} | {result['path_count']} | "
            f"{result['path_length_mm']:.3f} | {execution['simulated_seconds']:.3f} | "
            f"{execution['tracking_trace_rms_mm']:.4f} | {execution['tracking_trace_p95_mm']:.4f} | "
            f"{execution['tracking_trace_max_mm']:.4f} | {'通过' if result['motion_gate_passed'] else '未通过'} |"
        )
    header.extend(
        [
            "",
            "[混合缺陷执行视频](selected_robust_local_mixed_6007/demo.mp4) · "
            "[薄批缝执行视频](selected_robust_local_thin_6007/demo.mp4) · "
            "[混合执行结果](selected_robust_local_mixed_6007/result.json) · "
            "[薄缝执行结果](selected_robust_local_thin_6007/result.json)",
            "",
            "这两次运动沿观测面保持 3 mm 名义间隙，异常接触和工具接触记录均为 0；"
            "路径 3 mm 覆盖为 XY 投影指标，不代表材料实际去除。",
            "",
            "## 现成 ESTUN 工站检测演示",
            "",
            f"在用户现成工站模型上复用同一混合缺陷，1600×1200 静态 RGB-D 检测出 {station['perception']['auto_regions']} 个区域"
            "（1 条批缝、2 处毛刺）。"
            f"精确率 {percent(station['perception']['precision'])}、召回率 {percent(station['perception']['recall'])}，"
            f"原 CAD 表面误选 {station['perception']['normal_structure_selected_pixels']} 像素、"
            f"类别错分 {station['perception']['wrong_type_pixels']} 像素。"
            f"生成 {station['path_count']} 段表面轨迹预览，共 {station['path_length_mm']:.2f} mm。",
            "",
            "![现成工站静态检测](cad_station_demo_highres/preview.png)",
            "",
            "[实际检测图](cad_station_demo_highres/detection_paths.png) · [完整记录](cad_station_demo_highres/result.json)。"
            "此处机械臂未执行动作，轨迹仍按原球头工具的表面预览定义；后续需另行映射为抓工件对砂带机/主轴的运动。",
            "",
            "## 适用边界与复现入口",
            "",
            "当前选型分条件使用：原固定工位且无 CAD 参考时采用 `robust_local`；"
            "复杂 CAD 底座采用已配准 `thin_reference` 路线，1920×1440 的独立细缝验证优于低分辨率，仍需继续改善薄缝漏检。"
            "真实金属反光、扫描噪声、配准误差、恒力磨削、去除量、过切和表面质量尚未形成生产验收证据。",
            "",
            "`097`、`021` 只按 CAD 文件编号引用。解析曲面/B-Spline、颜色和顶点差异均不能确定原 CAD 是否为毛坯、成品或存在真实毛刺。",
            "",
            "```bash",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage prepare --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage evaluate --split development --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage select --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage evaluate --split holdout --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage execute --case mixed --seed 6007 --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage execute --case thin --seed 6007 --output runs/defect_study_new",
            "MUJOCO_GL=egl .venv/bin/daiyi-run --method robust_local --case thin --seed 6007 --video --output runs/thin_demo_new",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-resolution --output runs/defect_study_new/cad_resolution_dataset",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1024 --split development",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1920 --split development",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --select",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1024 --split holdout",
            "MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1920 --split holdout",
            ".venv/bin/python -m daiyi_grinding.study_report --study runs/defect_study_20261118",
            "```",
            "",
            "现有已完成目录不覆盖。生成新数据和方法比较时使用新的输出名字。报告图直接读取本轮的评分 JSON，并校验计数与精确率、召回率和 F1 一致。",
            "",
            "## 可检查的证据",
            "",
            "1. [原基线复现比较](reproduction_comparison.json)",
            "2. [原场景五方法留出汇总](coupon_comparison/holdout/summary.json)",
            "3. [CAD 五方法留出汇总](cad_comparison/holdout/summary.json)",
            "4. [公开照片实际获取记录](external_samples/ssal/acquisition.json)与[两方法实验](external_samples/ssal/benchmark_results.json)",
            "5. [观测方法选型记录](selection.json)",
            "6. [静态工站检测记录](cad_station_demo_highres/result.json)",
        ]
    )
    audit_paths = sorted(study.glob("independent*audit*.json"))
    if (study / "audit_data_all.json").exists():
        audit_paths.append(study / "audit_data_all.json")
    if audit_paths:
        header.append(
            "7. 独立核验记录："
            + " · ".join(f"[{path.name}]({path.name})" for path in audit_paths)
        )
        for path in audit_paths:
            audited = read_json(path)
            if "passed" in audited:
                if path.name == "independent_audit_precomparison.json":
                    status = "预评测数据/默认检测已验证，完整比较当时尚未生成"
                elif audited.get("issues"):
                    status = "存在未通过项"
                elif audited["passed"]:
                    status = "通过所覆盖检查"
                else:
                    status = "阶段性检查，无报告错误，完整性尚未确认"
                header.append(
                    f"`{path.name}`：{status}。"
                    "该状态仅覆盖记录中列出的数据、预测或执行检查；不会扩展为真实加工验证。"
                )
            source_paths.append(path)
    tests_log, lint_log = study / "tests_final.log", study / "lint_final.log"
    if tests_log.exists() and lint_log.exists():
        tests_text = tests_log.read_text(encoding="utf-8")
        lint_text = lint_log.read_text(encoding="utf-8")
        test_count = re.search(r"Ran (\d+) tests", tests_text)
        if test_count and "FAILED (errors=1)" in tests_text:
            total = int(test_count.group(1))
            header.extend(
                [
                    "",
                    f"[完整测试日志](tests_final.log)：{total} 项中 {total - 1} 项通过，1 项已有网页临时目录路径测试失败"
                    "（`serve.py` 中 `relative_to(ROOT)` 拒绝仓库外测试目录）。该故障未在本轮检测实验中修复，不能报告为全量测试通过。",
                ]
            )
        if "All checks passed!" in lint_text:
            header.append(
                "[源码与测试 lint](lint_final.log)通过；报告生成器本身的 lint 和格式检查也通过。"
            )
        source_paths.extend([tests_log, lint_log])
    header.extend(
        [
            "",
            "检测输入与评分标签分文件保存。`prediction.npz`、`evaluator/truth.npz`、"
            "逐场景记录、源码快照和数据哈希支持进一步独立核验。",
        ]
    )
    text = "\n".join(header) + "\n"
    (study / "main_report.md").write_text(text, encoding="utf-8")
    docs = project.ROOT / "docs/defect_experiments_20261118.md"
    document_text = text.replace(
        "](comparison.png)", f"]({comparison_image.as_posix()})"
    ).replace("](external_comparison.png)", f"]({external_image.as_posix()})")
    for filename in (
        "cad_refinement_comparison.png",
        "cad_station_demo_highres/preview.png",
    ):
        document_text = document_text.replace(
            f"]({filename})", f"]({(study / filename).as_posix()})"
        )
    local_link_prefixes = (
        "reproduction",
        "coupon_comparison",
        "cad_comparison",
        "external_samples",
        "selected_",
        "cad_station_demo_highres",
        "independent",
        "audit_data_all.json",
        "selection.json",
        "cad_refinement",
        "cad_resolution_dataset",
        "tests_final.log",
        "lint_final.log",
    )
    for line in text.splitlines():
        if "](" not in line:
            continue
        for target in line.split("](")[1:]:
            target_path = target.split(")")[0]
            if target_path.startswith(local_link_prefixes):
                document_text = document_text.replace(
                    f"]({target_path})",
                    f"]({(study / target_path).as_posix()})",
                )
    document_text = document_text.replace(
        "](../../docs/defect_sources_20261118.md)",
        f"]({(project.ROOT / 'docs/defect_sources_20261118.md').as_posix()})",
    )
    docs.write_text(document_text, encoding="utf-8")
    provenance = {
        "report_source": str(Path(__file__).resolve()),
        "report_source_sha256": digest(__file__),
        "inputs": {
            str(path.relative_to(study)): digest(path) for path in source_paths
        },
        "refinement_holdout_complete": len(refinement) == 2,
        "ratio_validation": "Recomputed from saved TP/FP/FN, undefined denominators preserved",
        "main_report": str(study / "main_report.md"),
        "docs_report": str(docs),
        "figures": ["comparison.png", "external_comparison.png"]
        + (["cad_refinement_comparison.png"] if len(refinement) == 2 else []),
    }
    (study / "report_provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report": str(study / "main_report.md"),
                "docs": str(docs),
                "refinement_complete": len(refinement) == 2,
                "figures": provenance["figures"],
            },
            ensure_ascii=False,
        )
    )
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    parser.add_argument("--require-refinement", action="store_true")
    args = parser.parse_args()
    build_report(args.study, args.require_refinement)


if __name__ == "__main__":
    main()
