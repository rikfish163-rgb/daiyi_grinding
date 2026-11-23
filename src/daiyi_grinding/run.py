"""Reproducible RGB-D -> defect regions -> paths -> FR5 simulation demo."""

import argparse
import csv
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial.transform import Rotation

from . import project
from .calibration import load_inspection_calibration
from .evaluation import score, truth_image
from .execution import execute, make_commands, preflight
from .geometry import generate
from .perception import detect, observe_noise, overlay
from .planning import path_length, plan
from .sim import Simulation

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def write_json(path, value):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    tmp.replace(p)


def export_paths(paths, out):
    rows = []
    for i, p in enumerate(paths):
        euler = Rotation.from_matrix(p["rotation"]).as_euler(
            "xyz", degrees=True
        )
        for j, xyz in enumerate(p["xyz"]):
            base = np.asarray(xyz) - [0, 0, 0.72]
            rows.append(
                [
                    i,
                    j,
                    p["detection_id"],
                    p["kind"],
                    *(base * 1000),
                    *euler,
                    p["recipe"]["feed_mm_s"],
                    p["clearance_mm"],
                ]
            )
    with (out / "fr5_path_preview.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "path",
                "point",
                "region",
                "type",
                "base_x_mm",
                "base_y_mm",
                "base_z_mm",
                "rx_deg",
                "ry_deg",
                "rz_deg",
                "feed_mm_s",
                "clearance_mm",
            ]
        )
        w.writerows(rows)
    write_json(
        out / "path_contract.json",
        dict(
            simulation_only=True,
            real_robot_enabled=False,
            position_units="millimeters",
            rotation="extrinsic XYZ Euler degrees from scipy; FAIRINO convention must be checked on site",
            input_frame="MuJoCo world",
            export_frame="robot base",
            T_world_base=[
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0.72],
                [0, 0, 0, 1],
            ],
            tcp="nominal 6mm ball abrasive tip; requires actual TCP calibration",
            command_file="preview of machining points; not a complete certified controller program",
            process="surface clearance traversal; no spindle dynamics, removal or validated contact-force recipe",
        ),
    )


class Video:
    def __init__(self, out, rgb, annotated, paths):
        self.writer = imageio.get_writer(
            str(out / "demo.mp4"),
            fps=12,
            codec="libx264",
            quality=7,
            macro_block_size=16,
        )
        self.paths = paths
        self.out = out
        self.raw = Image.fromarray(rgb).resize((480, 360))
        self.annotated = Image.fromarray(annotated).resize((480, 360))
        self.font = ImageFont.truetype(FONT, 20)
        self.small = ImageFont.truetype(FONT, 15)
        self.last_snapshot = -10
        self.last_progress = -10

    def __call__(self, sim, command, t, history):
        frame = Image.new("RGB", (1280, 800), (15, 22, 34))
        draw = ImageDraw.Draw(frame)
        view = Image.fromarray(
            sim.render("overview", width=768, height=576, paths=self.paths)
        ).resize((768, 576))
        frame.paste(view, (20, 135))
        frame.paste(self.annotated, (790, 116))
        close = Image.fromarray(
            sim.render("closeup", width=480, height=288, paths=self.paths)
        )
        frame.paste(close, (790, 481))
        draw.text(
            (24, 17),
            "戴屹视觉打磨研究演示  ·  FR5 / MuJoCo",
            font=self.font,
            fill=(234, 243, 255),
        )
        draw.text(
            (24, 55),
            "RGB-D 观测 → 自动选区 → 轨迹与姿态 → 标定坐标 → 仿真执行",
            font=self.small,
            fill=(104, 214, 199),
        )
        phase = {
            "home": "待机",
            "transfer": "抬刀转移",
            "approach": "接近",
            "trace": "沿检测表面运行",
            "retract": "退刀",
        }.get(command["phase"], command["phase"])
        err = history[-1][7] if history else 0
        draw.text(
            (24, 92),
            f"仿真时间 {t:6.1f}s   阶段 {phase}   区域 {command['detection_id']}   跟踪误差 {err:.2f} mm",
            font=self.small,
            fill=(213, 222, 237),
        )
        draw.text(
            (803, 90),
            "相机图像与自动检测 / 粉色为规划路径",
            font=self.small,
            fill=(213, 222, 237),
        )
        draw.text(
            (25, 721),
            "固定夹具 · 程序生成铸件 · 自动区域选择 · 可选人工 ROI",
            font=self.small,
            fill=(184, 199, 219),
        )
        draw.text(
            (25, 752),
            "当前为带间隙的轨迹验证；未模拟材料去除，未证明加工质量。",
            font=self.small,
            fill=(255, 189, 108),
        )
        array = np.asarray(frame)
        self.writer.append_data(array)
        if t - self.last_snapshot > 30:
            Image.fromarray(array).save(
                self.out / f"frame_{int(t):03d}.jpg", quality=90
            )
            self.last_snapshot = t
        if t - self.last_progress > 10:
            write_json(
                self.out / "status.json",
                dict(stage="executing", simulation_time_s=t),
            )
            self.last_progress = t

    def close(self):
        self.writer.close()


def run(args):
    out = Path(args.output).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError(
            f"Output is not empty; choose a new run directory: {out}"
        )
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    write_json(out / "status.json", dict(stage="capturing"))
    config = vars(args).copy()
    config["started_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(out / "config.json", config)
    snapshot = out / "source_snapshot"
    snapshot.mkdir()
    for p in project.SOURCE.glob("*.py"):
        shutil.copyfile(p, snapshot / p.name)
    sim = Simulation(generate(args.seed, args.case))
    video = None
    try:
        rgb = sim.render()
        ideal_depth = sim.render(depth=True)
        trueK, trueT = sim.camera_info()
        calibration = project.ROOT / "results/calibration/calibration.json"
        K, T = load_inspection_calibration(
            calibration,
            camera_position=sim.data.cam_xpos[
                sim.model.camera("inspection").id
            ],
        )
        shutil.copyfile(calibration, out / "calibration.json")
        rgb, depth = observe_noise(
            rgb, ideal_depth, args.seed, args.noise_mm, args.dropout
        )
        # Public sensor boundary: detector has no Coupon/model/geom IDs.
        np.savez_compressed(
            out / "observation.npz", rgb=rgb, depth=depth, K=K, T_world_camera=T
        )
        Image.fromarray(rgb).save(out / "camera_rgb.png")
        roi = json.loads(Path(args.roi).read_text()) if args.roi else None
        if isinstance(roi, dict):
            roi = roi["polygon"]
        if args.method in ("threshold", "geometry"):
            detections, aux = detect(
                rgb, depth, K, T, method=args.method, roi=roi
            )
        else:
            from .detector_candidates import detect_candidate

            detections, aux = detect_candidate(
                rgb, depth, K, T, method=args.method, roi=roi
            )
        selected = (
            [int(x) for x in args.ids.split(",") if x] if args.ids else None
        )
        paths = plan(
            detections,
            aux,
            selected_ids=selected,
            clearance_mm=args.clearance_mm,
        )
        annotated = overlay(rgb, detections, paths, K, T)
        Image.fromarray(annotated).save(out / "detection_paths.png")
        write_json(out / "detections.json", [d.summary() for d in detections])
        write_json(out / "paths.json", paths)
        export_paths(paths, out)
        # Hidden simulator truth is constructed/accessed only AFTER decisions.
        truth = truth_image(
            generate(args.seed, args.case), ideal_depth, trueK, trueT
        )
        metrics = score(detections, truth, paths, trueK, trueT, ideal_depth)
        result = dict(
            seed=args.seed,
            case=args.case,
            method=args.method,
            selected_backend=aux.get("selected_backend", args.method),
            noise_mm=args.noise_mm,
            dropout=args.dropout,
            calibration_source="image + FK solved eye-to-hand",
            roi=roi,
            selected_ids=selected,
            perception=metrics,
            path_count=len(paths),
            path_length_mm=path_length(paths) * 1000,
            clearance_mm=args.clearance_mm,
            simulation_only=True,
            material_removal_simulated=False,
        )
        Image.fromarray(sim.render("overview", paths=paths)).save(
            out / "scene.png"
        )
        if paths and not args.perception_only:
            write_json(out / "status.json", dict(stage="planning"))
            commands, info = make_commands(sim, paths)
            check = preflight(sim, commands)
            result["planning"] = info
            result["preflight"] = check
            write_json(out / "preflight.json", check)
            np.savez_compressed(
                out / "joint_commands.npz",
                time_s=[c["t"] for c in commands],
                q_rad=[c["q"] for c in commands],
                xyz_m=[c["xyz"] for c in commands],
            )
            if check["passed"]:
                if args.video:
                    video = Video(out, rgb, annotated, paths)
                result["execution"] = execute(sim, commands, out, video)
                # Acceptance values are local research gates, not customer tolerances.
                e = result["execution"]
                result["motion_gate_passed"] = bool(
                    e["unexpected_contact_count"] == 0
                    and e["abrasive_contact_steps"] == 0
                    and e["tracking_trace_p95_mm"] < 1.0
                    and e["tracking_trace_max_mm"] < 2.0
                    and e["tracking_trace_orientation_p95_deg"] < 1.0
                )
            else:
                result["motion_gate_passed"] = False
        result["wall_time_s"] = time.perf_counter() - started
        write_json(out / "result.json", result)
        write_json(
            out / "status.json", dict(stage="complete", result="result.json")
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    except Exception as e:
        write_json(out / "status.json", dict(stage="failed", error=str(e)))
        raise
    finally:
        if video:
            video.close()
        sim.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--case",
        choices=["mixed", "seam", "burr", "normal", "thin", "coarse", "noisy"],
        default="mixed",
    )
    p.add_argument("--output", default=str(project.RUNS / "demo_new"))
    p.add_argument(
        "--method",
        choices=["threshold", "geometry", "multiscale", "robust_local"],
        default="geometry",
    )
    p.add_argument("--noise-mm", type=float, default=0.06)
    p.add_argument("--dropout", type=float, default=0.003)
    p.add_argument("--clearance-mm", type=float, default=3.0)
    p.add_argument(
        "--roi", help="JSON polygon in captured image pixel coordinates"
    )
    p.add_argument("--ids", help="Optional comma-separated detection IDs")
    p.add_argument("--video", action="store_true")
    p.add_argument("--perception-only", action="store_true")
    return p


def main():
    run(parser().parse_args())


if __name__ == "__main__":
    main()
