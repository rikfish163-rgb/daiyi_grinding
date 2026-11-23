#!/usr/bin/env python3
"""Plot frozen model predictions and actual grinding feedback for review."""

from __future__ import annotations

import csv
import json

import matplotlib
import numpy as np
from matplotlib.font_manager import FontProperties

from daiyi_grinding import project
from daiyi_grinding.defect_training import load_labels, load_public

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = project.ROOT / "runs/learned_grinding_20261121"
FONT = FontProperties(
    fname="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
)
COLORS = {1: np.array([26, 190, 149]), 2: np.array([242, 161, 45])}


def overlay(rgb, semantic):
    image = rgb.astype(np.float32).copy()
    for label, color in COLORS.items():
        mask = semantic == label
        image[mask] = image[mask] * 0.35 + color * 0.65
    return image.astype(np.uint8)


def prediction_details():
    folder = ROOT / "test_01"
    evaluated = json.loads((folder / "evaluation.json").read_text())
    chosen = [
        ("casting_flash", "变厚飞边"),
        ("casting_curled", "卷曲薄片"),
        ("casting_burr", "局部毛刺"),
    ]
    figure, axes = plt.subplots(3, 3, figsize=(10, 9), dpi=160)
    for row, (case, label) in enumerate(chosen):
        observation = next(
            item
            for item in evaluated["views"]
            if item["seed"] == 9601
            and item["case"] == case
            and item["view"] == 0
        )
        public = load_public(observation)
        truth = load_labels(observation)
        with np.load(folder / f"{case}_seed9601_prediction.npz") as arrays:
            predicted = arrays["semantic"]
        pixels = np.argwhere(truth > 0)
        if not len(pixels):
            raise ValueError("Chosen visualization has no visible defect")
        center = (pixels.min(axis=0) + pixels.max(axis=0)) / 2
        size = int(max(64, np.ptp(pixels, axis=0).max() + 48))
        low = np.maximum(0, (center - size / 2).astype(int))
        high = np.minimum(truth.shape, low + size)
        window = (slice(low[0], high[0]), slice(low[1], high[1]))
        images = (
            public["rgb"],
            overlay(public["rgb"], truth),
            overlay(public["rgb"], predicted),
        )
        for col, image in enumerate(images):
            axes[row, col].imshow(image[window], interpolation="nearest")
            axes[row, col].set_xticks([])
            axes[row, col].set_yticks([])
            for spine in axes[row, col].spines.values():
                spine.set_color("#cbd5da")
        f1 = observation["metrics"]["foreground"]["f1"]
        axes[row, 0].set_ylabel(
            f"{label}\n本帧 F1 {f1:.1%}", fontproperties=FONT, fontsize=11
        )
    for axis, title in zip(axes[0], ("原始 RGB", "评价真值", "冻结模型预测")):
        axis.set_title(title, fontproperties=FONT, fontsize=13, pad=12)
    figure.suptitle(
        "未见种子 9601 · 固定相机 0° · 原图与模型缺陷分割",
        fontproperties=FONT,
        fontsize=16,
        y=0.98,
    )
    figure.text(
        0.5,
        0.015,
        "绿色：飞边；橙色：毛刺。真值仅用于评价和裁剪展示；全部 84 帧测试均纳入指标。",
        ha="center",
        fontproperties=FONT,
        fontsize=10,
        color="#485d68",
    )
    figure.tight_layout(rect=(0, 0.045, 1, 0.94))
    figure.savefig(folder / "defect_detail.png", facecolor="white")
    plt.close(figure)


def grinding_feedback():
    folder = ROOT / "burr_9101_closed_loop_01"
    result = json.loads((folder / "result.json").read_text())
    with (folder / "execution.csv").open() as file:
        rows = [
            row
            for row in csv.DictReader(file)
            if row["phase"] == "trace" and row["surface_tracking_mm"]
        ]
    times = np.array([float(row["time_s"]) for row in rows])
    errors = np.array([float(row["surface_tracking_mm"]) for row in rows])
    figure, axes = plt.subplots(1, 2, figsize=(11, 4.5), dpi=160)
    for part in np.split(
        np.arange(len(times)), np.where(np.diff(times) > 0.03)[0] + 1
    ):
        axes[0].plot(times[part], errors[part], color="#087763", linewidth=1)
    axes[0].axhline(1, color="#c15733", linestyle="--", linewidth=1)
    axes[0].set_ylim(bottom=0)
    axes[0].set_title(
        f"实际加工点跟踪 · P95 {np.quantile(errors, 0.95):.3f} mm",
        fontproperties=FONT,
    )
    axes[0].set_xlabel("仿真时间 / s", fontproperties=FONT)
    axes[0].set_ylabel("实际加工点误差 / mm", fontproperties=FONT)
    regions = result["final_stock"]["residual_regions"]
    peaks = [region["remaining_peak_mm"] for region in regions]
    bars = axes[1].bar(range(3), peaks, color="#087763", width=0.55)
    axes[1].axhline(0.3, color="#c15733", linestyle="--", linewidth=1)
    axes[1].set_ylim(0, 0.36)
    axes[1].set_xticks(
        range(3), ["上表面 1", "上表面 2", "外侧面"], fontproperties=FONT
    )
    axes[1].set_ylabel("剩余几何峰值 / mm", fontproperties=FONT)
    axes[1].set_title("3 处余料均低于 0.3 mm", fontproperties=FONT)
    for bar, peak in zip(bars, peaks):
        axes[1].text(
            bar.get_x() + bar.get_width() / 2,
            peak + 0.008,
            f"{peak:.3f}",
            ha="center",
        )
    for axis in axes:
        axis.grid(axis="y", color="#e2e8eb")
        axis.set_axisbelow(True)
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle(
        "当前 ESTUN / 097 基准毛刺工况：模型识别 → 自动规划 → 积分执行 → 复检",
        fontproperties=FONT,
        fontsize=14,
        y=0.99,
    )
    figure.text(
        0.5,
        0.01,
        "开发工况；异常接触 0。仅几何去除仿真；类别匹配 1/3，完整任务保留待复核状态。",
        ha="center",
        fontproperties=FONT,
        fontsize=10,
        color="#485d68",
    )
    figure.tight_layout(rect=(0, 0.06, 1, 0.93))
    figure.savefig(folder / "grinding_evidence.png", facecolor="white")
    plt.close(figure)


if __name__ == "__main__":
    prediction_details()
    grinding_feedback()
