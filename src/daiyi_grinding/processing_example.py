"""Export a self-contained processing example from saved workstation evidence."""

from __future__ import annotations

import argparse
import base64
import html
import json
import shlex
from pathlib import Path

from . import project

STOPPING = {
    "observed_regions_verified": (
        "观测区域已验证",
        "保留本次加工、复检和运动记录，等待独立现场量测。",
    ),
    "reinspection_requires_review": (
        "复检需要复核",
        "对未放行区域补充有效观测；确认持续正超差后生成局部回磨路径。",
    ),
    "pass_budget_exhausted": (
        "加工轮数预算已用完",
        "保留剩余区域的量测和路径，依据新观测安排下一次独立加工任务。",
    ),
    "motion_safety_gate": (
        "运动门未通过",
        "定位实际轨迹超差或非预期接触，修复对应运动后重新规划。",
    ),
    "followup_safety_rejected": (
        "回磨规划被拒绝",
        "查看回磨段的预检原因，修复对应姿态或转移运动后重新规划。",
    ),
    "followup_planning": (
        "已进入下一轮加工",
        "依据磨后观测重新规划并执行剩余路径。",
    ),
    "overgrind_requires_disposition": (
        "发现负向过磨",
        "停止自动磨削，保留负超差量测并安排工艺处置。",
    ),
    "negative_limit_requires_review": (
        "负向容差边界需要复核",
        "补充有效量测并确认负向余量，当前不继续自动磨削。",
    ),
    "invalid_reinspection_requires_review": (
        "复检输入无效",
        "修复对应观测的无效输入，重新采集完整有效量测。",
    ),
    "no_admitted_path": (
        "没有接纳的加工路径",
        "查看识别候选、加工许可和预检受阻段，修复后重新规划。",
    ),
    "perception_only": (
        "本次只完成观测和规划",
        "查看路径预检结果，再运行离线加工仿真。",
    ),
    "clearance_pass_complete": (
        "间隙轨迹已完成",
        "运行材料去除模式并完成磨后复检，才能评价加工质量。",
    ),
}

STYLE = """
:root {
  --page: #eaf0f5; --sheet: #fff; --ink: #1b3448;
  --muted: #526779; --rule: #cfdae3; --blue: #235d91;
  --good: #2d6c50; --hold: #906c21;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 16px/1.65 "Noto Sans CJK SC", "PingFang SC",
    "Microsoft YaHei", sans-serif;
}
main { max-width: 1180px; margin: 32px auto; background: var(--sheet); }
header, section, footer { padding: 28px 40px; }
header { border-top: 8px solid var(--blue); padding-bottom: 18px; }
h1 { font-size: clamp(28px, 4vw, 44px); line-height: 1.25; margin: 8px 0 18px; }
h2 { font-size: 23px; line-height: 1.35; margin: 0 0 16px; }
h3 { font-size: 17px; margin: 18px 0 8px; }
p { max-width: 74ch; margin: 8px 0; }
.context, figcaption, .note, footer { color: var(--muted); font-size: 14px; }
.disposition {
  padding: 18px 22px; background: var(--page);
  border-left: 4px solid var(--hold); margin-top: 22px;
}
.disposition.pass { border-color: var(--good); }
.disposition strong { display: block; font-size: 21px; margin-bottom: 6px; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
figure { margin: 0; min-width: 0; }
figure img { width: 100%; height: auto; display: block; background: var(--page); }
figcaption { padding: 8px 0; }
.missing {
  display: grid; place-content: center; aspect-ratio: 4 / 3;
  background: var(--page); color: var(--muted);
}
section + section { border-top: 1px solid var(--rule); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--rule); }
th { font-size: 14px; color: var(--muted); background: var(--page); }
td { vertical-align: top; }
td:first-child, th:first-child { padding-left: 0; }
th:first-child { padding-left: 12px; }
.good { color: var(--good); font-weight: 600; }
.hold { color: var(--hold); font-weight: 600; }
.interval { width: 100%; display: block; margin: 18px 0 6px; }
.traces { width: 100%; max-height: 360px; color: var(--blue); }
.round-paths + .round-paths { margin-top: 24px; }
details { margin-top: 18px; }
summary { cursor: pointer; color: var(--blue); padding: 8px 0; }
summary:focus-visible { outline: 3px solid var(--blue); outline-offset: 4px; }
pre {
  padding: 18px; background: var(--page); font-size: 13px;
  white-space: pre-wrap; word-break: break-word; max-height: 600px; overflow: auto;
}
code { overflow-wrap: anywhere; }
footer { border-top: 1px solid var(--rule); overflow-wrap: anywhere; }
@media (max-width: 700px) {
  main { margin: 0; }
  header, section, footer { padding: 24px 18px; }
  .pair { grid-template-columns: 1fr; }
  th, td { padding: 9px 8px; font-size: 14px; }
}
@media print {
  body { background: white; }
  main { margin: 0; max-width: none; }
  details { display: none; }
  figure, tr { break-inside: avoid; }
}
"""


def _number(value, digits=3):
    return "未记录" if value is None else f"{value:.{digits}f}"


def _decision(value, yes="通过", no="未通过"):
    if value is None:
        return "未记录"
    return (
        f'<span class="{"good" if value else "hold"}">'
        f"{yes if value else no}</span>"
    )


def _table(headers, rows):
    headings = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{headings}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _image(directory, filename, caption):
    path = directory / filename
    image = (
        '<img src="data:image/png;base64,'
        + base64.b64encode(path.read_bytes()).decode("ascii")
        + f'" alt="{html.escape(caption, quote=True)}" loading="lazy">'
        if path.is_file()
        else '<div class="missing">本次未保存该图像</div>'
    )
    return (
        f"<figure>{image}<figcaption>{html.escape(caption)}"
        f" <code>{html.escape(filename)}</code></figcaption></figure>"
    )


def _interval_chart(regions, limits):
    measured = [
        region
        for region in regions
        if region.get("lower_peak_mm") is not None
        and region.get("upper_peak_mm") is not None
    ]
    if not measured:
        return "<p>本次未记录可绘制的区域观测区间。</p>"
    lower = limits.get("lower", -0.3)
    upper = limits.get("upper", 0.3)
    extent = (
        max(
            abs(lower),
            abs(upper),
            *(
                abs(r[key])
                for r in measured
                for key in ("lower_peak_mm", "upper_peak_mm")
            ),
        )
        * 1.12
    )
    height = 56 + len(measured) * 46

    def x(value):
        return 120 + (value + extent) / (2 * extent) * 800

    elements = [
        f'<svg class="interval" viewBox="0 0 960 {height}" role="img" '
        'aria-label="各区域磨后观测区间与双边容差">',
        "<title>各区域磨后观测区间与双边容差，单位毫米</title>",
        f'<rect x="{x(lower):.2f}" y="20" '
        f'width="{x(upper) - x(lower):.2f}" height="{height - 40}" '
        'fill="#2d6c50" fill-opacity=".08"/>',
    ]
    for value in (lower, 0, upper):
        elements.append(
            f'<path d="M {x(value):.2f} 20 V {height - 24}" '
            'stroke="#cfdae3" stroke-dasharray="4 4"/>'
            f'<text x="{x(value):.2f}" y="{height - 5}" '
            'text-anchor="middle" font-size="13" fill="#526779">'
            f"{value:+.2f} mm</text>"
        )
    for index, region in enumerate(measured):
        y = 40 + index * 46
        color = "#2d6c50" if region["status"] == "verified" else "#906c21"
        start, end = x(region["lower_peak_mm"]), x(region["upper_peak_mm"])
        elements.append(
            f'<text x="8" y="{y + 5}" font-size="15" fill="#1b3448">'
            f"区域 {html.escape(str(region['id']))}</text>"
            f'<path d="M {start:.2f} {y} H {end:.2f} '
            f"M {start:.2f} {y - 7} V {y + 7} "
            f'M {end:.2f} {y - 7} V {y + 7}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        if region.get("signed_peak_mm") is not None:
            elements.append(
                f'<circle cx="{x(region["signed_peak_mm"]):.2f}" cy="{y}" '
                f'r="5" fill="{color}"/>'
            )
    return "".join([*elements, "</svg>"])


def _path_chart(directory, filename):
    path_file = directory / filename
    if not path_file.is_file():
        return "<p>本轮未保存路径候选文件。</p>"
    paths = json.loads(path_file.read_text())
    if not paths:
        return "<p>本轮没有加工路径候选。</p>"
    traces = [
        [[coordinate * 1000 for coordinate in point] for point in path["xyz"]]
        for path in paths
    ]
    points = [point for trace in traces for point in trace]
    lower = [min(point[axis] for point in points) for axis in range(3)]
    upper = [max(point[axis] for point in points) for axis in range(3)]
    axes = sorted(
        range(3), key=lambda axis: upper[axis] - lower[axis], reverse=True
    )[:2]
    first, second = axes
    scale = min(
        700 / max(upper[first] - lower[first], 1),
        240 / max(upper[second] - lower[second], 1),
    )
    center = [(lower[axis] + upper[axis]) / 2 for axis in range(3)]

    def projection(point):
        return (
            400 + (point[first] - center[first]) * scale,
            150 - (point[second] - center[second]) * scale,
        )

    elements = [
        '<svg class="traces" viewBox="0 0 800 300" role="img" '
        'aria-label="本轮工件坐标系的表面路径候选">',
        "<title>本轮表面路径候选，工件坐标系，单位毫米</title>",
        '<rect x="45" y="20" width="710" height="240" '
        'fill="none" stroke="currentColor" stroke-opacity=".2"/>',
    ]
    for trace, path in zip(traces, paths):
        coordinates = " ".join(
            f"{x:.2f},{y:.2f}"
            for point in trace
            for x, y in [projection(point)]
        )
        elements.append(
            f'<polyline points="{coordinates}" fill="none" '
            'stroke="currentColor" stroke-width="2.5">'
            f"<title>候选段 {html.escape(str(path['segment_id']))}，"
            f"区域 {html.escape(str(path['detection_id']))}</title></polyline>"
        )
    elements.append(
        '<text x="400" y="290" text-anchor="middle" fill="currentColor" '
        f'font-size="14">{"XYZ"[first]} / {"XYZ"[second]} 平面投影；'
        f"{'XYZ'[first]} {lower[first]:.2f} 至 {upper[first]:.2f} mm，"
        f"{'XYZ'[second]} {lower[second]:.2f} 至 {upper[second]:.2f} mm</text>"
    )
    return "".join([*elements, "</svg>"])


def _reproduction_command(config):
    command = [
        str(project.ROOT / ".venv/bin/python"),
        "-m",
        "daiyi_grinding.workstation_run",
    ]
    for key in (
        "workpiece_profile",
        "seed",
        "case",
        "casting_profile",
        "grasp_setup",
        "camera_setup",
        "detector",
        "checkpoint",
        "tool",
        "views",
        "mode",
        "planner",
        "max_passes",
        "realtime_factor",
        "removal_rate_mm_s",
        "roi",
        "ids",
    ):
        if config.get(key) is not None:
            command.extend(["--" + key.replace("_", "-"), str(config[key])])
    for key in ("perception_only", "targeted_rescan", "keep_staging_transfers"):
        if config.get(key):
            command.append("--" + key.replace("_", "-"))
    command.extend(["--output", "新运行目录"])
    return (
        "MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
        "OPENBLAS_NUM_THREADS=1 \\\n"
        f"  flock {shlex.quote(str(project.RUNS / 'enterprise_offline_20261123/mujoco.lock'))} \\\n"
        f"  {shlex.join(command)}"
    )


def export_example(run_directory: Path, output: Path) -> Path:
    """Read a saved run and export its figures, outcomes and rerun command.

    Args:
        run_directory: Existing workstation run with result.json.
        output: New directory for a portable index.html and summary.json.

    Returns:
        Absolute path to the self-contained HTML example.
    """
    run_directory, output = run_directory.resolve(), output.resolve()
    result = json.loads((run_directory / "result.json").read_text())
    config_file = run_directory / "config.json"
    config = (
        json.loads(config_file.read_text()) if config_file.is_file() else {}
    )
    reinspections = result.get("reinspections", [])
    executions = result.get("executions", [])
    stopping = result.get("controller_stopping", {})
    reason = stopping.get("reason", "未记录")
    disposition, next_action = STOPPING.get(
        reason, (reason, "依据当轮观测和停止原因处理后再运行。")
    )
    quality = (
        reinspections[-1].get("region_reinspection", {})
        if reinspections
        else {}
    )
    regions = quality.get("regions", [])
    last_prefix = f"rescan_{len(reinspections):02d}_" if reinspections else ""
    detections_file = run_directory / "detections.json"
    detections = (
        json.loads(detections_file.read_text())
        if detections_file.is_file()
        else []
    )
    view = next(
        (
            d.get("best_view", 0)
            for d in detections
            if d.get("machining_required")
        ),
        0,
    )
    before = f"view_{view:02d}_paths.png"
    after = f"{last_prefix}view_{view:02d}_paths.png"
    if not (run_directory / before).is_file():
        before = "detection_paths.png"
    if not (run_directory / after).is_file() or not reinspections:
        after = "after_detection_paths.png"
    round_rows = []
    round_records = []
    regrind_rows = []
    for index, execution in enumerate(executions):
        recheck = reinspections[index] if index < len(reinspections) else {}
        requality = recheck.get("region_reinspection", {})
        prior = reinspections[index - 1] if index else {}
        view_file = run_directory / f"rescan_{index + 1:02d}_views.json"
        views = json.loads(view_file.read_text()) if view_file.is_file() else []
        frame_count = (
            sum(view["depth_frame_count"] for view in views)
            if views and all("depth_frame_count" in view for view in views)
            else None
        )
        round_reason = recheck.get(
            "stopping_reason",
            reason if index == len(executions) - 1 else "followup_planning",
        )
        round_disposition = STOPPING.get(round_reason, (round_reason,))[0]
        admitted = (
            prior.get("followup_preflight", {}).get("admitted_count")
            if index
            else result.get("admitted_path_count")
        )
        candidates = (
            prior.get("remaining_paths") if index else result.get("path_count")
        )
        round_rows.append(
            [
                "首次加工" if index == 0 else f"回磨 {index}",
                f"{admitted if admitted is not None else '未记录'} / "
                f"{candidates if candidates is not None else '未记录'}",
                _number(execution.get("surface_tracking_p95_mm")),
                str(execution.get("unexpected_contact_count", "未记录")),
                str(requality.get("verified", "未记录")),
                str(requality.get("needs_rework", "未记录")),
                str(requality.get("uncertain", "未记录")),
                str(requality.get("insufficient", "未记录")),
                str(recheck.get("remaining_paths", "未记录")),
                f"{frame_count if frame_count is not None else '未记录'} / "
                f"{len(views) if views else recheck.get('view_count', '未记录')}",
                html.escape(round_disposition),
            ]
        )
        round_records.append(
            {
                "execution_round": index + 1,
                "candidate_path_count": candidates,
                "admitted_path_count": admitted,
                "depth_frame_count": frame_count,
                "view_count": len(views)
                if views
                else recheck.get("view_count"),
                "stopping_reason": round_reason,
                "regrind": recheck.get("regrind"),
            }
        )
        if recheck.get("regrind"):
            mapping = recheck["regrind"]
            regrind_rows.append(
                [
                    f"加工轮 {index + 1} 后",
                    str(mapping.get("positive_anchor_count", "未记录")),
                    str(mapping.get("path_count", "未记录")),
                    str(mapping.get("duplicate_trace_count", "未记录")),
                    str(mapping.get("candidate_path_count", "未记录")),
                    str(
                        recheck.get("followup_preflight", {}).get(
                            "admitted_count", "未记录"
                        )
                        if index + 1 < len(executions)
                        else 0
                    ),
                    str(len(mapping["unmapped_anchors"]))
                    if "unmapped_anchors" in mapping
                    else "未记录",
                    html.escape(mapping.get("reason", "未记录")),
                ]
            )
    path_rounds = [
        ("初始路径候选", "paths.json"),
        *[
            (
                f"加工轮 {index + 1} 后的路径候选",
                f"rescan_{index + 1:02d}_paths.json",
            )
            for index in range(len(reinspections))
        ],
    ]
    path_figures = "".join(
        '<div class="round-paths">'
        f"<h3>{caption}</h3>{_path_chart(run_directory, filename)}"
        f'<p class="note">来源：<code>{filename}</code>。'
        "图中为该轮保存的路径候选；实际接纳段数见加工记录。</p></div>"
        for caption, filename in path_rounds
    )
    region_rows = [
        [
            html.escape(str(region["id"])),
            html.escape(
                {
                    "verified": "已验证",
                    "needs_rework": "需处置",
                    "uncertain_measurement": "量测不确定",
                    "insufficient_observation": "观测不足",
                }.get(region["status"], region["status"])
            ),
            f"{region.get('observed_anchors', '未记录')} / "
            f"{region.get('anchors', '未记录')}",
            _number(region.get("trough_mm")),
            _number(region.get("peak_mm")),
            _number(region.get("lower_peak_mm")),
            _number(region.get("upper_peak_mm")),
        ]
        for region in regions
    ]
    geometry_rows = [
        ["磨前", _number(result.get("final_stock", {}).get("initial_peak_mm"))],
        *[
            [
                "首次加工后" if index == 0 else f"回磨 {index} 后",
                _number(execution.get("removal", {}).get("remaining_peak_mm")),
            ]
            for index, execution in enumerate(executions)
        ],
    ]
    outcomes = [
        [
            "仿真运动",
            _decision(result.get("motion_gate_passed")),
            "实际积分的加工点跟踪和非预期接触",
        ],
        [
            "磨后观测",
            _decision(result.get("observational_release"), "已放行", "未放行"),
            "当轮 RGB-D、保存的 nominal CAD 锚点和双边容差",
        ],
        [
            "事后几何评价",
            _decision(result.get("geometric_goal_complete")),
            "生成余料的几何裁切、可观测性和初始识别评价",
        ],
        [
            "完整仿真任务",
            _decision(result.get("goal_complete"), "已完成", "尚未完成"),
            "磨后观测与事后评价共同满足本次任务条件",
        ],
        ["企业实物验收", "待独立现场量测", "现场同件量测、标定和工艺记录"],
    ]
    summary = {
        "schema": "processing-example-v1",
        "source_run": str(run_directory),
        "task_status": result.get("task_status"),
        "motion_gate_passed": result.get("motion_gate_passed"),
        "observational_release": result.get("observational_release"),
        "geometric_goal_complete": result.get("geometric_goal_complete"),
        "goal_complete": result.get("goal_complete"),
        "enterprise_acceptance": "pending_site_measurements",
        "reason": reason,
        "execution_rounds": len(executions),
        "reinspection_rounds": len(reinspections),
        "rounds": round_records,
    }
    raw = html.escape(
        json.dumps(
            {
                "summary": summary,
                "controller_stopping": stopping,
                "region_reinspection": quality,
                "rounds": round_records,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>097 加工示例：{html.escape(disposition)}</title>
<style>{STYLE}</style></head><body><main>
<header><p class="context">戴屹打磨工程　ESTUN 持件到固定砂带</p>
<h1>097 工件的加工与复检</h1>
<p>从同一轮观测生成路径，执行携件磨削，再用磨后观测决定放行或回磨。
加工前后图、每轮记录和停止原因均来自本次保存的运行产物。</p>
<div class="disposition {
        "pass" if result.get("observational_release") else ""
    }">
<strong>{html.escape(disposition)}</strong><p>{html.escape(next_action)}</p>
<p class="note">停止原因：<code>{html.escape(reason)}</code></p></div></header>
<section><div class="pair">
{_image(run_directory, before, "加工前的识别与路径")}
{_image(run_directory, after, "最后一次磨后复检的识别与路径")}
</div><p class="note">两幅图使用同一转台视角；叠加内容来自各自当轮预测。</p>
</section>
<section><h2>本次结果</h2>
{_table(["评价层次", "结果", "依据"], outcomes)}
{
        _table(
            ["运行信息", "记录"],
            [
                ["种子", html.escape(str(result.get("seed", "未记录")))],
                ["工况", html.escape(str(result.get("case", "未记录")))],
                [
                    "任务状态",
                    html.escape(str(result.get("task_status", "未记录"))),
                ],
                [
                    "实际执行轮数 / 总预算",
                    f"{len(executions)} / {config.get('max_passes', '未记录')}",
                ],
                [
                    "仿真时间 / 墙钟时间（s）",
                    f"{_number(result.get('simulation_seconds'), 2)} / {_number(result.get('wall_seconds'), 2)}",
                ],
            ],
        )
    }</section>
<section><h2>每轮加工与复检</h2>
{
        _table(
            [
                "轮次",
                "接纳 / 候选段",
                "加工点 P95（mm）",
                "非预期接触",
                "已验证区域",
                "需处置",
                "不确定",
                "观测不足",
                "回磨路径",
                "量测帧 / 视角",
                "本轮处置",
            ],
            round_rows,
        )
        if round_rows
        else "<p>本次没有执行磨削。</p>"
    }
<p class="note">每轮加工点误差取实际运动记录；区域状态取对应磨后复检。</p>
</section>
<section><h2>各轮表面路径</h2>{path_figures}
{
        _table(
            [
                "复检轮次",
                "持续正超差锚点",
                "映射子段",
                "重复覆盖子段",
                "去重后候选段",
                "实际下一轮接纳段",
                "未映射锚点",
                "局部回磨处置",
            ],
            regrind_rows,
        )
        if regrind_rows
        else ""
    }
<p class="note">所有路径来自当轮保存的工件坐标；投影保持毫米尺度的等比例。
重复深度采集的帧数取各视角记录，局部回磨映射见展开的处置记录。</p></section>
<section><h2>最后一次区域观测</h2>
{_interval_chart(regions, quality.get("signed_limits_mm", {}))}
<p class="note">区间为各区域锚点量测区间的外包络；
圆点为最大绝对实测余量。浅色带为双边容差范围，单位为毫米。</p>
{
        _table(
            [
                "区域",
                "复检状态",
                "已观察 / 注册锚点",
                "实测最小（mm）",
                "实测最大（mm）",
                "区间下界（mm）",
                "区间上界（mm）",
            ],
            region_rows,
        )
        if region_rows
        else "<p>本次没有记录注册区域复检。</p>"
    }
<details><summary>查看区域量测和停止记录</summary><pre>{raw}</pre></details>
</section>
<section><h2>事后几何评价</h2>
{_table(["阶段", "模拟余料峰值（mm）"], geometry_rows)}
<p>模拟余料峰值来自实际工具扫掠后的生成余料几何。控制器的停止依据由
<code>controller_stopping</code> 单独记录。</p></section>
<section><h2>复跑此工况</h2>
<p>在工程环境中使用同一参数，并将输出参数替换为一个新的运行目录。</p>
<pre>{html.escape(_reproduction_command(config))}</pre>
<p>新运行结束后，可从它的保存目录导出同样的示例页。</p>
<pre>{
        html.escape(
            shlex.join(
                [
                    str(project.ROOT / ".venv/bin/python"),
                    "-m",
                    "daiyi_grinding.processing_example",
                    "新运行目录",
                    "--output",
                    "新示例目录",
                ]
            )
        )
    }</pre></section>
<section><h2>局限与待补输入</h2>
<p>本页记录离线合成观测与仿真积分。相机、TCP、装夹、材料去除率、
力、温升和表面质量需要独立现场数据。复检区间采用本轮记录的量测
不确定度模型；未知系统偏差仍需要校准和交叉量测。</p></section>
<footer>来源运行：<code>{html.escape(str(run_directory))}</code><br>
来源文件：result.json、config.json，以及图注列出的实际图像。
图像和结果摘要均嵌入本页，可直接打开。</footer>
</main></body></html>"""
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    target = output / "index.html"
    target.write_text(document, encoding="utf-8")
    return target


def main(argv=None):
    """Export an existing run without changing its saved evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        target = export_example(args.run_directory, args.output)
    except (OSError, ValueError) as error:
        parser.exit(2, f"processing_example: {error}\n")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
