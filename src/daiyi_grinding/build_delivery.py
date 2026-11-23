"""Build a source-backed PDF brief and portable client evidence pack."""

import argparse
import json
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import project

LABELS = {
    "mixed": "批缝与毛刺",
    "seam": "批缝",
    "burr": "局部毛刺",
    "normal": "正常工件",
    "thin": "薄批缝",
    "coarse": "超范围残留",
    "noisy": "噪声与缺失",
}


def build(output):
    import matplotlib
    import numpy as np

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    demo = json.loads(
        (project.ROOT / "results/demo_v3/result.json").read_text()
    )
    bench = json.loads(
        (project.ROOT / "results/benchmark_v3/summary.json").read_text()
    )
    calib = json.loads(
        (project.ROOT / "results/calibration/calibration.json").read_text()
    )
    contact = json.loads(
        (project.ROOT / "results/contact_probe/summary.json").read_text()
    )
    vla = json.loads(
        (project.ROOT / "results/vla/shadow_v1/audit.json").read_text()
    )
    font = FontProperties(
        fname="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    )
    groups = [
        x
        for x in bench["summary"]
        if x["method"] == "geometry"
        and x["case"] in ("mixed", "seam", "burr", "thin", "noisy")
    ]
    fig, ax = plt.subplots(figsize=(9, 3.1), layout="constrained")
    xs = np.arange(len(groups))
    w = 0.32
    ax.bar(
        xs - w / 2,
        [x["precision"] * 100 for x in groups],
        w,
        label="像素精确率",
        color="#248fbd",
    )
    ax.bar(
        xs + w / 2,
        [x["recall"] * 100 for x in groups],
        w,
        label="像素召回率",
        color="#e5a34f",
    )
    ax.set_xticks(xs, [LABELS[x["case"]] for x in groups], fontproperties=font)
    ax.set_ylim(0, 110)
    ax.set_ylabel("%", fontproperties=font)
    ax.grid(axis="y", alpha=0.2)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(prop=font, ncol=2, loc="upper center", frameon=False)
    fig.savefig(out / "perception_benchmark.png", dpi=160)
    plt.close(fig)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = {
        "title": ParagraphStyle(
            "title",
            fontName="STSong-Light",
            fontSize=23,
            leading=32,
            textColor=colors.HexColor("#12334d"),
            spaceAfter=14,
            wordWrap="CJK",
        ),
        "h": ParagraphStyle(
            "h",
            fontName="STSong-Light",
            fontSize=15,
            leading=22,
            textColor=colors.HexColor("#176485"),
            spaceAfter=10,
            wordWrap="CJK",
        ),
        "p": ParagraphStyle(
            "p",
            fontName="STSong-Light",
            fontSize=10.5,
            leading=17,
            spaceAfter=10,
            wordWrap="CJK",
        ),
        "small": ParagraphStyle(
            "small",
            fontName="STSong-Light",
            fontSize=8.5,
            leading=13,
            textColor=colors.HexColor("#526575"),
            spaceAfter=7,
            wordWrap="CJK",
        ),
    }
    story = []

    def p(text, style="p"):
        story.append(Paragraph(text, styles[style]))

    def table(rows, widths=None):
        cells = [
            [Paragraph(escape(str(v)), styles["small"]) for v in row]
            for row in rows
        ]
        t = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f0f5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    (
                        "LINEBELOW",
                        (0, 0),
                        (-1, 0),
                        0.6,
                        colors.HexColor("#9bb4c4"),
                    ),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    (
                        "LINEBELOW",
                        (0, 1),
                        (-1, -1),
                        0.3,
                        colors.HexColor("#dbe3e9"),
                    ),
                ]
            )
        )
        story.append(t)
        story.append(Spacer(1, 12))

    p("铸件批缝与局部毛刺<br/>视觉规划与 FR5 仿真演示", "title")
    p("戴屹项目技术沟通材料 / 2026-10-07 / 研发原型", "small")
    p(
        "已实现从 RGB-D 相机观测、自动区域选择、轨迹及工具姿态生成，到 FR5 MuJoCo 动力学执行的完整原型。人工 ROI 与区域选择共用同一规划流程。当前结果支持开展真实工件测量和真机空跑验证。"
    )
    story.append(
        Image(
            str(project.ROOT / "results/demo_v3/frame_050.jpg"),
            width=500,
            height=312.5,
        )
    )
    story.append(Spacer(1, 12))
    e = demo["execution"]
    table(
        [
            ["本轮证据", "实测结果", "解释范围"],
            [
                "自动轨迹",
                f"{demo['perception']['auto_regions']} 个区域 / {demo['path_count']} 段",
                "按检测几何生成，未逐点人工示教",
            ],
            [
                "仿真跟踪",
                f"P95 {e['tracking_trace_p95_mm']:.3f} mm",
                "理想模型与控制器内的轨迹误差",
            ],
            [
                "空跑接触",
                f"{e['abrasive_contact_steps']} 个积分步",
                "标称 3 mm 间隙，每 2 ms 统计",
            ],
            ["材料去除", "尚未验证", "未模拟真实磨削、粗糙度、磨损或热效应"],
        ],
        [95, 125, 280],
    )
    p(
        "首版工件是带正常结构的程序生成铸件表面试样，尺寸为实验假设。测试范围是可见上表面、机器人持小型球形工具、固定夹具。深腔、倒扣、多面工件和大型浇冒口不属于本轮验证范围。",
        "small",
    )
    story.append(PageBreak())
    p("自动选区的效果与失效边界", "title")
    p(
        f"最终固定测试包含 7 类场景 × 6 个新随机种子，共 42 个场景；两种方法配对生成 {bench['completed']} 条记录。检测器只读取图像、深度和标定参数；缺陷生成标签只用于事后评分。测试仍来自同一程序生成工件族，不能代表真实产线分布。"
    )
    story.append(
        Image(str(out / "perception_benchmark.png"), width=500, height=172)
    )
    rows = [["场景（各 6 件）", "像素精确率", "像素召回率", "投影覆盖 / 处置"]]
    for r in bench["summary"]:
        if r["method"] != "geometry":
            continue
        if r["case"] == "normal":
            rows.append(
                [
                    LABELS[r["case"]],
                    "不适用",
                    "不适用",
                    f"{r['no_action_episodes']}/6 不生成路径",
                ]
            )
        else:
            rows.append(
                [
                    LABELS[r["case"]],
                    f"{r['precision']:.1%}",
                    f"{r['recall']:.1%}",
                    f"{r['mean_coverage_3mm']:.1%}"
                    + ("；超范围区域复核" if r["case"] == "coarse" else ""),
                ]
            )
    table(rows, [120, 90, 90, 200])
    thin = next(
        r
        for r in bench["summary"]
        if r["case"] == "thin" and r["method"] == "geometry"
    )
    p(
        f"薄批缝是明确的失败边界：像素召回率仅 {thin['recall']:.1%}，路径投影覆盖约 {thin['mean_coverage_3mm']:.1%}。接近分辨率和阈值下限时，需要更好的真实测量、其他视角或更强的视觉方法；不能把当前算法直接用于该尺寸范围的生产。"
    )
    p(
        "正常结构自动选中像素为 0；超范围残留不进入小工具路径，并显示复核状态。这仅是本轮有限仿真集的结果。投影覆盖指缺陷 XY 点位于路径中心线 3 mm 半径内，不表示磨掉了材料。",
        "small",
    )
    story.append(PageBreak())
    p("标定 接触与 VLA 的实测边界", "title")
    p("手眼标定已通过真实渲染图像求解", "h")
    p(
        f"30 张虚拟棋盘图中 {calib['successful_views']} 张检测成功；{calib['training_views']} 张用于求解，{calib['holdout_views']} 张留出验证。相机平移真值误差 {calib['camera_truth_error']['translation_mm']:.3f} mm，旋转误差 {calib['camera_truth_error']['rotation_deg']:.4f}°，留出最差三维角点误差 {calib['holdout_worst_point_mm']:.3f} mm。主检测与规划链使用求出的外参。"
    )
    p(
        "内参配置已知，机器人 FK 理想，棋盘尺寸精确；以上数值不等于真实安装精度。上真机需重新标定并以独立测量检查相机、机器人、工具 TCP 的联合误差。",
        "small",
    )
    p("小范围接触试验说明工艺模型仍需辨识", "h")
    rows = [["TCP 名义间隙", "接触时间常数", "仿真接触峰值", "接触积分步"]]
    for t in contact["trials"]:
        ce = t["execution"]
        rows.append(
            [
                f"{t['nominal_clearance_mm']:+.1f} mm",
                f"{t['contact_solref_time_s']:.3f} s",
                f"{ce['max_contact_force_n']:.2f} N",
                str(ce["abrasive_contact_steps"]),
            ]
        )
    table(rows, [125, 125, 125, 125])
    p(
        "同一段扫描路径随间隙和接触参数变化产生不同反力。这里是位置伺服与不去除材料的软接触模型，未实现经过验证的恒力磨削；这些力值不能作为真机设定值。",
        "small",
    )
    p("最新 VLA 已实际尝试，保留负结果", "h")
    p(
        f"Hy-Embodied-0.5-VLA 已完成 {len(vla['runs'])} 组真实权重影子推理，保留原始动作、输入与适配审计。当前双臂预训练动作、相机视图与 FR5 单臂工具存在缺口，全部候选拒绝执行；VLA 动作执行数为 {vla['executed_vla_actions']}。π0.7 纳入技术调研，未宣称在本项目复现。"
    )
    p(
        "后续先采集匹配 FR5 工件和工具的示范，开展动作适配与影子评估，再比较几何基线、学习策略与受约束混合策略。基础模型推理成功不等于打磨任务成功。",
        "small",
    )
    story.append(PageBreak())
    p("FR5 初步真机验证计划", "title")
    table(
        [
            ["阶段", "要完成的工作", "进入下一阶段的证据"],
            [
                "1 真实测量",
                "确认材质、批缝尺寸、正常结构及质量公差；相机扫描真实样本",
                "薄缺陷能被可靠测出，反光/缺失已量化",
            ],
            [
                "2 标定与空跑",
                "相机外参/手眼、工具TCP、装夹重复性；主轴关闭、低速执行自动路径",
                "独立定位验证通过，无越界和非预期接触",
            ],
            [
                "3 小样接触",
                "兼容测力/柔顺工具；无旋转接触、短道磨削、工艺窗口探索",
                "力响应稳定，加工前后测量显示残余与过切满足约定",
            ],
            [
                "4 工件族验证",
                "新批次、正常件、薄缺陷、遮挡与超范围案例；记录人工介入",
                "双方约定的质量、干预率和周期达标",
            ],
        ],
        [85, 215, 200],
    )
    p(
        "需要甲方提供：真实工件和可报废小样、既有工艺参数、必须保留的结构、允许残余和过切、相机与 FR5 控制器版本、工具与夹具资料、现场设备及质量人员。先做测量与空跑，不需要先建设完整生产单元。"
    )
    p(
        "工程计划按一个固定夹具下的工件族、现成设备及现场支持估算，基础开发与适配返工约 35–58 人天；采购、制造等待和独立 VLA 微调另计。该估计用于讨论资源，不是合同工期。"
    )
    p("随附交付物", "h")
    p(
        "演示视频、原始相机图与检测结果、自动轨迹 CSV、仿真与检测指标、标定结果、VLA 审计、完整实验记录及真机计划。团队可通过本地交互页面更换工件种子、自动规划、绘制人工 ROI 并重新执行仿真。"
    )
    p("主要技术依据", "h")
    for title, url in [
        (
            "MuJoCo 官方文档",
            "https://mujoco.readthedocs.io/en/stable/overview.html",
        ),
        (
            "OpenCV 手眼标定",
            "https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html",
        ),
        (
            "FAIRINO 运动接口",
            "https://fairino-doc-en.readthedocs.io/latest/SDKManual/PythonRobotMovement.html",
        ),
        ("RoboGrind 研究", "https://arxiv.org/abs/2402.16542"),
        ("Hy VLA 研究", "https://arxiv.org/abs/2606.14409"),
        ("π0.7 研究", "https://arxiv.org/abs/2604.15483"),
    ]:
        p(f'<link href="{url}" color="#176485">{title}</link>', "small")
    p(
        "公司案例与证据分级、历史名称关系待确认项，见随附 research.md。本材料用于技术可行性沟通，不构成产线验收证明。",
        "small",
    )

    def footer(canvas, doc):
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#687b8d"))
        canvas.drawString(
            47, 23, "戴屹项目 / 视觉规划与 FR5 仿真实测 / 研发原型"
        )
        canvas.drawRightString(A4[0] - 45, 23, str(doc.page))

    pdf = out / "戴屹视觉打磨仿真验证与FR5计划.pdf"
    SimpleDocTemplate(
        str(pdf),
        pagesize=A4,
        rightMargin=47,
        leftMargin=47,
        topMargin=40,
        bottomMargin=40,
        title="戴屹视觉打磨仿真验证与FR5计划",
        author="项目研发演示",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    pack = out / "client_demo"
    pack.mkdir(exist_ok=True)
    shutil.copyfile(pdf, pack / pdf.name)
    for rel in [
        "demo.mp4",
        "camera_rgb.png",
        "detection_paths.png",
        "scene.png",
        "result.json",
        "fr5_path_preview.csv",
        "path_contract.json",
    ]:
        shutil.copyfile(project.ROOT / "results/demo_v3" / rel, pack / rel)
    for name in [
        "research.md",
        "fr5_real_robot_plan.md",
        "demo_script.md",
        "experiment_journal.md",
        "calibration.md",
        "vla_experiment.md",
        "contact_probe.md",
    ]:
        if (project.ROOT / "docs" / name).exists():
            shutil.copyfile(project.ROOT / "docs" / name, pack / name)
    shutil.copyfile(
        project.ROOT / "results/benchmark_v3/summary.json",
        pack / "benchmark_summary.json",
    )
    shutil.copyfile(
        project.ROOT / "results/calibration/calibration.json",
        pack / "calibration.json",
    )
    shutil.copyfile(
        project.ROOT / "results/vla/shadow_v1/audit.json",
        pack / "vla_audit.json",
    )
    shutil.copyfile(
        out / "perception_benchmark.png", pack / "perception_benchmark.png"
    )
    (pack / "index.html").write_text(
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>戴屹视觉打磨仿真演示</title><style>body{margin:0;background:#101723;color:#e8eef8;font:17px/1.8 system-ui}main{max-width:1120px;margin:auto;padding:38px 24px}h1{font-size:34px;line-height:1.4}p{max-width:900px;color:#b9c9dc}a{color:#79dcd1}video,img{width:100%;border-radius:12px;background:#fff}section{margin:30px 0;padding:24px;background:#192436;border-radius:16px}.links{display:flex;gap:24px;flex-wrap:wrap}.notice{color:#efc183;font-size:15px}</style><main><h1>铸件批缝与局部毛刺<br>视觉规划与 FR5 仿真演示</h1><p>相机观测 → 自动选区 → 带姿态的轨迹 → 标定坐标 → MuJoCo 执行</p><div class="links"><a href="戴屹视觉打磨仿真验证与FR5计划.pdf">查看技术简报与真机计划 PDF</a><a href="demo.mp4" download>下载完整视频</a><a href="result.json">查看原始实测数据</a></div><section><video controls preload="metadata" poster="scene.png" src="demo.mp4"></video><p class="notice">视频为3mm名义间隙的仿真空跑，未模拟实际材料去除。跟踪精度为理想模型内测量，不能直接当作真实FR5加工精度。</p></section><section><h2>从原始相机观测识别区域并生成路径</h2><img src="detection_paths.png" alt="实际运行的检测与规划结果"><p>默认由RGB-D几何算法选择加工区域；交互程序另保留矩形、多边形ROI和区域选择。训练标签和仿真真值未传入检测器。</p></section><section><h2>42个场景的配对实验</h2><img src="perception_benchmark.png" alt="像素精确率与召回率"><p>薄批缝仍是明显失效范围；有限程序生成试样的结果不能代替真实工件测试。详见PDF和benchmark_summary.json。</p></section><section><h2>下一步在FR5上验证</h2><p>真实工件测量与标定 → 主轴关闭空跑 → 可报废小样接触 → 工件族质量验证。Hy-VLA已做真实权重影子推理，本轮候选全部拒绝，未宣称其已会打磨。</p><a href="戴屹视觉打磨仿真验证与FR5计划.pdf">阅读完整计划</a></section><p>本文件可离线打开。实时重新拍照、人工ROI与仿真执行需在团队工作区启动Python交互程序，命令见README.txt。</p></main></html>"""
    )
    (pack / "README.txt").write_text(
        "戴屹视觉打磨研究演示\n先看PDF技术简报，再播放demo.mp4。\n当前自动识别、规划与仿真空跑已经串联；没有实际材料去除或真实FR5生产质量验证。\nfr5_path_preview.csv仅为轨迹预览，不是可以直接运行的真实控制器程序。\n交互主工程位于 source/；执行 bash scripts/bootstrap.sh 后，启动 .venv/bin/daiyi-serve --port 8848。\nPDF/视频/数据均可离线查看，技术文档中的团队工作区相对链接需要原项目目录。\n"
    )
    source = pack / "source"
    source.mkdir()
    shutil.copytree(
        project.SOURCE,
        source / "src/daiyi_grinding",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in [
        "README.md",
        "HANDOFF.md",
        "CONTRIBUTING.md",
        "pyproject.toml",
        "requirements.lock.txt",
    ]:
        shutil.copyfile(project.ROOT / name, source / name)
    for name in ["web", "docs", "assets", "scripts", "tests", "config"]:
        shutil.copytree(project.ROOT / name, source / name)
    portable_delivery = source / "deliverables"
    portable_delivery.mkdir()
    shutil.copyfile(pdf, portable_delivery / pdf.name)
    (portable_delivery / "README.txt").write_text(
        "PDF、视频和观测结果位于上一级演示目录；交互主工程从 source/ 启动。\n"
    )
    portable_index = source / "web/index.html"
    portable_index.write_text(
        portable_index.read_text()
        .replace(
            "/deliverables/戴屹视觉打磨演示包.zip", "/deliverables/README.txt"
        )
        .replace(
            "下载完整演示包 <small>ZIP ↓</small>",
            "演示包说明 <small>TXT ↗</small>",
        )
    )
    for directory, names in {
        "demo_v3": [
            "demo.mp4",
            "status.json",
            "config.json",
            "result.json",
            "detections.json",
            "paths.json",
            "camera_rgb.png",
            "detection_paths.png",
            "scene.png",
            "fr5_path_preview.csv",
            "path_contract.json",
        ],
        "calibration": ["calibration.json"],
        "benchmark_v3": ["summary.json", "episodes.jsonl", "protocol.json"],
        "contact_probe": ["summary.json"],
        "vla/shadow_v1": [
            "audit.json",
            "inference.json",
            "input_manifest.json",
        ],
    }.items():
        dest = source / "results" / directory
        dest.mkdir(parents=True)
        for name in names:
            shutil.copyfile(project.RESULTS / directory / name, dest / name)
    (source / "START_HERE.txt").write_text(
        "Linux + Python 3.12。运行 bash scripts/bootstrap.sh，然后 bash scripts/verify.sh；启动 .venv/bin/daiyi-serve --port 8848。浏览器地址 http://127.0.0.1:8848。FR5 mesh 已附带；本机需要 EGL 驱动。GPU 模型环境与权重位于服务器，配置见 HANDOFF.md。\n"
    )
    with zipfile.ZipFile(
        out / "戴屹视觉打磨演示包.zip", "w", zipfile.ZIP_DEFLATED
    ) as z:
        for pth in pack.rglob("*"):
            if pth.is_file():
                z.write(pth, "戴屹视觉打磨演示包/" + str(pth.relative_to(pack)))
    print(
        json.dumps(
            dict(pdf=str(pdf), zip=str(out / "戴屹视觉打磨演示包.zip")),
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=project.RUNS / "delivery_new"
    )
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
