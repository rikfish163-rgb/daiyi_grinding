"""The current ESTUN cell: scan, predict, repair, execute and reinspect."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from . import project
from .casting_defects import CONFIG_FILE as CASTING_CONFIG_FILE
from .casting_profiles import (
    BASE_PROFILE,
    PROFILE_FILES,
    available_profiles,
    profile_provenance,
)
from .planning import path_length
from .workstation_defects import WORKSTATION_CASES
from .workstation_delivery import simulation_receipt
from .workstation_inspection import (
    CELL_DETECTOR_CONFIG,
    NORMAL_HEIGHT_QUANTILE,
    Inspection,
)
from .workstation_jobs import JOB_FIELDS, JOB_FILE, job_profile, job_profiles
from .workstation_model import (
    CAMERA_SETUPS,
    CELL_CONFIG,
    COMPONENTS,
    GRASP_SETUPS,
    SOURCE_FILES,
    Workstation,
    source_hashes,
    write_json,
)
from .workstation_motion import (
    GeometricRemoval,
    choose_process_orientation,
    execute,
    make_commands,
    preflight,
)
from .workstation_planning import build_plan
from .workstation_quality import add_targets
from .workstation_reuse import provenance
from .workstation_transfers import shorten_transfers
from .workstation_web import export_scene


def continuation_reason(
    mode,
    motion_gate_passed,
    quality,
    remaining_paths,
    pass_index,
    max_passes,
    observational_release,
):
    """Choose continuation from sensor evidence and the remaining pass budget."""
    if mode != "removal":
        return "clearance_pass_complete"
    if not motion_gate_passed:
        return "motion_safety_gate"
    if quality.get("overgrind", 0):
        return "overgrind_requires_disposition"
    if quality.get("negative_limit_risk", 0):
        return "negative_limit_requires_review"
    if quality.get("invalid_observations"):
        return "invalid_reinspection_requires_review"
    if not remaining_paths:
        return (
            "observed_regions_verified"
            if observational_release
            else "reinspection_requires_review"
        )
    if pass_index + 1 >= max_passes:
        return "pass_budget_exhausted"
    return "followup_planning"


def global_plan(sim, paths, mode):
    """Retain the original single-orientation cell planner as a local ablation."""
    started = time.monotonic()
    if not paths:
        return (
            [],
            [],
            {
                "passed": True,
                "admitted_count": 0,
                "segment_count": 0,
                "blocked_count": 0,
                "rows": [],
                "seconds": 0,
            },
        )
    rotation, orientation = choose_process_orientation(sim, paths)
    commands = []
    checks = {"passed": False}
    if rotation is not None:
        try:
            commands, motion = make_commands(sim, paths, rotation, mode)
            checks = {**preflight(sim, commands), **motion}
        except ValueError as error:
            checks = {"passed": False, "reason": str(error)}
    admitted = paths if checks["passed"] else []
    return (
        commands if checks["passed"] else [],
        admitted,
        {
            **checks,
            "orientation": orientation,
            "admitted_count": len(admitted),
            "blocked_count": len(paths) - len(admitted),
            "segment_count": len(paths),
            "complete_segment_rate": len(admitted) / len(paths),
            "rows": [
                {
                    "segment_id": path["segment_id"],
                    "detection_id": path["detection_id"],
                    "status": "passed" if checks["passed"] else "blocked",
                    "reason": "original single-orientation whole-plan gate",
                }
                for path in paths
            ],
            "seconds": time.monotonic() - started,
            "scope": "previous local MuJoCo cell planner on identical current observations; not Wang Shiyu's unpublished implementation",
        },
    )


def export_commands(output, commands, prefix=""):
    """Save exact joint commands plus a compact world-coordinate route preview."""
    serialized, route = [], []
    for item in commands:
        serialized.append(
            {
                **item,
                "q": item["q"].tolist(),
                "pose": item["pose"].tolist(),
                "point": item["point"].tolist()
                if item.get("point") is not None
                else None,
            }
        )
        route.append(
            {
                "xyz": item["pose"][:3, 3].tolist(),
                "phase": item["phase"],
                "time_s": item["t"],
            }
        )
    write_json(output / f"{prefix}motion_commands.json", serialized)
    write_json(
        output / f"{prefix}motion_preview.json",
        {
            "frame": "world",
            "point": "gripper origin",
            "points": route,
            "source": "accepted robot command route; preview is not execution feedback",
        },
    )


def run(args):
    profile = job_profile(getattr(args, "workpiece_profile", None))
    if profile:
        for key in JOB_FIELDS:
            setattr(args, key, profile[key])
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    args.targeted_rescan = getattr(args, "targeted_rescan", False)
    sim = None
    latest_revision, pass_index = 0, 0
    start_wall = time.monotonic()
    learned_detector = None
    detector_kind = getattr(args, "detector", "geometric")
    if detector_kind in {"learned", "hybrid"}:
        from .learned_defects import LearnedDetector

        checkpoint = getattr(args, "checkpoint", None)
        if not checkpoint and profile:
            from .learned_defects import registered_checkpoint

            checkpoint = registered_checkpoint()
        if not checkpoint:
            raise ValueError("学习型检测须提供已训练且验证过的模型检查点")
        learned_detector = LearnedDetector(checkpoint)

    def status(stage, **extra):
        write_json(
            out / "status.json",
            {
                "stage": stage,
                "scene": "workstation",
                "prediction_revision": latest_revision,
                **extra,
            },
        )

    def publish(state):
        nonlocal latest_revision
        latest_revision = max(
            latest_revision, state.get("prediction_revision", latest_revision)
        )
        state["pass_index"] = pass_index
        write_json(out / "live.json", state)
        stage = (
            "capturing"
            if state["phase"] in {"scan", "inspect"}
            else "executing"
        )
        status(
            stage,
            simulation_time_s=state["simulation_time_s"],
            phase=state["phase"],
            view_count=state.get("view_count"),
            progress=state.get("progress", 0),
        )

    def planning_progress(value):
        status(
            "planning",
            admitted_count=value["admitted_count"],
            checked_segments=len(value["rows"]),
            segment_count=value["segment_count"],
        )
        write_json(out / "planning_live.json", value)

    def plan_current(paths, prefix=""):
        if args.planner == "global":
            commands, admitted, checks = global_plan(sim, paths, args.mode)
        else:
            commands, admitted, checks = build_plan(
                sim, paths, args.mode, planning_progress
            )
            if not getattr(args, "keep_staging_transfers", False):
                commands, admitted, checks = shorten_transfers(
                    sim, commands, admitted, checks
                )
        checks["orientation"] = checks.get(
            "orientation",
            {
                "passed": bool(admitted) or not paths,
                "per_segment": True,
                "tool": sim.tool.label,
            },
        )
        write_json(out / f"{prefix}preflight.json", checks)
        write_json(out / f"{prefix}admitted_paths.json", admitted)
        return commands, admitted, checks

    try:
        status("capturing")
        snapshot = out / "source_snapshot"
        snapshot.mkdir()
        for name in SOURCE_FILES:
            shutil.copyfile(project.SOURCE / name, snapshot / name)
        shutil.copyfile(
            project.ROOT / "config/workstation.json",
            snapshot / "workstation.json",
        )
        shutil.copyfile(
            CASTING_CONFIG_FILE, snapshot / "casting_synthesis.json"
        )
        shutil.copyfile(JOB_FILE, snapshot / "workstation_jobs.json")
        for profile_file in PROFILE_FILES.values():
            if (
                profile_file.is_file()
                and profile_file.name != "casting_synthesis.json"
            ):
                shutil.copyfile(profile_file, snapshot / profile_file.name)
        write_json(out / "source_hashes.json", source_hashes())
        write_json(out / "source_reuse.json", provenance())
        sim = Workstation(
            out,
            args.seed,
            args.case,
            tool=args.tool,
            casting_profile=getattr(args, "casting_profile", BASE_PROFILE),
            grasp_setup=getattr(args, "grasp_setup", "original"),
            camera_setup=getattr(args, "camera_setup", "original"),
        )
        write_json(
            out / "config.json",
            {
                **vars(args),
                "cell": {**CELL_CONFIG, "camera": sim.camera_config},
            },
        )
        export_scene(sim)
        write_json(out / "components.json", COMPONENTS)
        write_json(out / "live.json", sim.state())
        Image.fromarray(sim.render(camera="cell_overview")).save(
            out / "scene.png"
        )
        roi = None
        if args.roi:
            value = json.loads(Path(args.roi).read_text())
            roi = value["polygon"] if isinstance(value, dict) else value
        ids = (
            [int(value) for value in args.ids.split(",")]
            if args.ids is not None
            else None
        )
        angles = [0] if args.views == "single" else None
        if learned_detector is not None:
            shutil.copyfile(learned_detector.path, out / "detector_model.pt")
            write_json(out / "detector_model.json", learned_detector.metadata())
        inspection = Inspection(
            sim,
            args.seed,
            roi,
            ids,
            publish=publish,
            learned_detector=learned_detector,
            hybrid=detector_kind == "hybrid",
        )
        detections, paths, perception = inspection.scan(angles)
        full_scope = ids is None or set(ids) == {
            d["id"] for d in detections if d["machining_required"]
        }
        targets = inspection.registered_targets(include_unselected=full_scope)
        permitted_paths = list(paths)
        write_json(
            out / "registered_regions.json",
            [
                {
                    **target,
                    "points": target["points"].tolist(),
                    "normals": target["normals"].tolist(),
                }
                for target in targets
            ],
        )
        latest_revision = perception["prediction_revision"]
        status("planning", path_count=len(paths))
        commands, admitted, checks = plan_current(paths)
        if args.compare_planners and args.planner == "segmented":
            _, _, baseline = global_plan(sim, paths, args.mode)
            write_json(
                out / "planner_comparison.json",
                {
                    "same_scene_seed": args.seed,
                    "same_observed_paths": True,
                    "global": baseline,
                    "segmented": checks,
                    "historical_meeting_rate_percent": 14.7,
                    "historical_meeting_rate_scope": "Wang Shiyu, 11/19, four different Real3D-AD workpieces, 75 segments; contextual only, not a paired comparison",
                },
            )
        export_commands(out, commands)
        with (out / "estun_path_preview.csv").open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(
                [
                    "segment",
                    "detection_id",
                    "view",
                    "frame",
                    "x_mm",
                    "y_mm",
                    "z_mm",
                    "nx",
                    "ny",
                    "nz",
                    "tool",
                ]
            )
            for path in paths:
                for point in path["xyz"]:
                    writer.writerow(
                        [
                            path["segment_id"],
                            path["detection_id"],
                            path["view_index"],
                            "workpiece_097",
                            *[value * 1000 for value in point],
                            *path["normal"],
                            sim.tool.key,
                        ]
                    )
        result = {
            "schema_version": 3,
            "scene": "workstation",
            "robot": "ESTUN iER220-2700",
            "seed": args.seed,
            "case": args.case,
            "casting_profile": (
                sim.casting_profile["profile"] if sim.casting_profile else None
            ),
            "casting_profile_provenance": (
                profile_provenance(sim.casting_profile)
                if sim.casting_profile
                else None
            ),
            "synthetic_sensor_prior": sim.sensor_prior,
            "roi": roi,
            "mode": args.mode,
            "planner": args.planner,
            "views": args.views,
            "width": 1024,
            "height": 768,
            "cad_workpiece": "097",
            "workpiece_profile": profile,
            "components_separate": True,
            "tool": sim.tool.summary(),
            "operation": "robot grasps and carries workpiece to fixed tool",
            "gripper_is_grinding_tool": False,
            "grasp_setup": sim.grasp_setup,
            "fixture_loading_yaw_deg": 180
            if sim.grasp_setup == "original"
            else 0,
            "grasp_real_validated": False,
            "perception": {
                **perception,
                "auto_regions": sum(
                    item["machining_required"] for item in detections
                ),
                "detected_defect_signals": sum(
                    item["kind"] in {"seam", "burr"} for item in detections
                ),
            },
            "path_count": len(paths),
            "admitted_path_count": len(admitted),
            "path_length_mm": path_length(paths) * 1000,
            "coordinate_frame": "workpiece_097",
            "clearance_mm": 3
            if args.mode == "clearance"
            else CELL_CONFIG["planning"]["material_allowance_mm"],
            "preflight": checks,
            "targeted_rescan": args.targeted_rescan,
            "robot_executed": False,
            "material_removal_simulated": False,
            "prediction_revision": latest_revision,
            "detector": {
                "hybrid": "CAD-reference seam geometry and trained RGB-D DefectUNet defect candidates",
                "learned": "trained RGB-D DefectUNet with measured CAD reference",
                "geometric": "full-CAD registered-reference detector, fixed-camera multi-view observations",
            }[detector_kind],
            "detector_kind": detector_kind,
            "learned_detector": learned_detector.metadata()
            if learned_detector is not None
            else None,
            "detector_configuration": asdict(CELL_DETECTOR_CONFIG),
            "force_control_validated": False,
            "camera": "fixed virtual 3D inspection camera",
            "camera_setup": sim.camera_setup,
            "camera_config": sim.camera_config,
            "source_reuse": provenance(),
            "executions": [],
            "reinspections": [],
            "goal_complete": False,
            "observational_release": False,
            "controller_stopping": {
                "source": "observed normal excess, motion safety, path availability and pass budget",
                "truth_used_for_control": False,
                "normal_height_quantile": NORMAL_HEIGHT_QUANTILE,
                "target_remaining_height_mm": CELL_CONFIG["planning"][
                    "target_remaining_height_mm"
                ],
                "reason": "perception_only"
                if args.perception_only
                else "no_admitted_path",
            },
        }
        write_json(out / "result.json", result)
        removal = GeometricRemoval(
            sim, rate_mm_s=getattr(args, "removal_rate_mm_s", None)
        )
        if not args.perception_only and admitted and checks["passed"]:
            for pass_index in range(args.max_passes):
                execution, removal = execute(
                    sim,
                    commands,
                    args.mode,
                    publish,
                    args.realtime_factor,
                    removal=removal,
                    prefix=f"pass_{pass_index:02d}_",
                )
                result["executions"].append(execution)
                result["execution"] = execution
                result["robot_executed"] = True
                result["material_removal_simulated"] = args.mode == "removal"
                result["motion_gate_passed"] = all(
                    item["unexpected_contact_count"] == 0
                    and item["tracking_trace_p95_mm"] is not None
                    and item["tracking_trace_p95_mm"]
                    < CELL_CONFIG["planning"]["tracking_p95_limit_mm"]
                    and item["surface_tracking_p95_mm"] is not None
                    and item["surface_tracking_p95_mm"]
                    < CELL_CONFIG["planning"]["tracking_p95_limit_mm"]
                    for item in result["executions"]
                )
                write_json(out / "result.json", result)
                status("rescanning")
                removal.upload()
                prefix = f"rescan_{pass_index + 1:02d}_"
                reinspection = Inspection(
                    sim,
                    args.seed,
                    roi,
                    revision=latest_revision + 1,
                    prefix=prefix,
                    publish=publish,
                    learned_detector=learned_detector,
                    hybrid=detector_kind == "hybrid",
                    targets=targets,
                    permitted_paths=permitted_paths,
                    whole_workpiece=full_scope,
                    targeted_rescan=args.targeted_rescan,
                    removal_rate_mm_s=getattr(args, "removal_rate_mm_s", None),
                )
                remaining_detections, remaining_paths, measured = (
                    reinspection.scan(angles)
                )
                latest_revision = measured["prediction_revision"]
                for file in (
                    "camera_rgb.png",
                    "detection_paths.png",
                    "paths.json",
                    "detections.json",
                    "views.json",
                    "perception_metrics.json",
                    "pointcloud.json",
                ):
                    shutil.copyfile(
                        out / f"{prefix}{file}", out / f"after_{file}"
                    )
                recheck = {
                    **measured,
                    "remaining_regions": sum(
                        item["machining_required"]
                        for item in remaining_detections
                    ),
                    "detected_defect_signals": sum(
                        item["kind"] in {"seam", "burr"}
                        for item in remaining_detections
                    ),
                    "remaining_paths": len(remaining_paths),
                    "revision": latest_revision,
                    "pass_index": pass_index,
                    "source": "new fixed-camera RGB-D after actual integrated robot motion and stock update",
                    "machining_eligibility_source": "current measured normal excess and repeated signed anchor bounds on permitted CAD traces",
                    "regrind": measured["region_reinspection"]["local_regrind"],
                }
                result["reinspections"].append(recheck)
                result["reinspection"] = recheck
                result["prediction_revision"] = latest_revision
                result["execution"]["removal"] = removal.summary()
                quality = recheck["region_reinspection"]
                result["observational_release"] = bool(
                    args.mode == "removal"
                    and result["motion_gate_passed"]
                    and quality["observational_release"]
                    and not recheck["remaining_regions"]
                )
                result["geometric_goal_complete"] = bool(
                    args.mode == "removal"
                    and result["motion_gate_passed"]
                    and removal.summary()["remaining_peak_mm"]
                    <= CELL_CONFIG["planning"]["target_remaining_height_mm"]
                    and perception["unobserved_defects"] == 0
                    and perception["metrics"]["matched_defect_count"]
                    == perception["generated_defects"]
                )
                result["goal_complete"] = (
                    result["observational_release"]
                    and result["geometric_goal_complete"]
                )
                write_json(out / "result.json", result)
                # Evaluation truth grades the outcome above. Only sensor
                # evidence, motion safety and the budget choose continuation.
                stopping = result["controller_stopping"]
                stopping["reason"] = continuation_reason(
                    args.mode,
                    result["motion_gate_passed"],
                    quality,
                    remaining_paths,
                    pass_index,
                    args.max_passes,
                    result["observational_release"],
                )
                recheck["stopping_reason"] = stopping["reason"]
                if stopping["reason"] != "followup_planning":
                    break
                status("planning")
                commands, admitted, followup = plan_current(
                    remaining_paths, prefix=prefix
                )
                export_commands(out, commands, prefix=prefix)
                recheck["followup_preflight"] = followup
                next_ids = {path["detection_id"] for path in admitted}
                targets = add_targets(
                    targets,
                    [
                        target
                        for target in reinspection.registered_targets()
                        if target["id"] in next_ids
                        or target["kind"] == "review"
                    ],
                )
                recheck["next_round_registered_ids"] = [
                    target["id"] for target in targets
                ]
                permitted_paths.extend(
                    path for path in remaining_paths if "regrind" not in path
                )
                if not admitted or not followup["passed"]:
                    stopping["reason"] = "followup_safety_rejected"
                    recheck["stopping_reason"] = stopping["reason"]
                    break
            shutil.copyfile(
                out / f"pass_{pass_index:02d}_execution.csv",
                out / "execution.csv",
            )
            write_json(out / "execution.json", result["execution"])
        result["wall_seconds"] = time.monotonic() - start_wall
        result["simulation_seconds"] = float(sim.data.time)
        result["final_stock"] = removal.summary()
        result["task_status"] = (
            "complete"
            if result["goal_complete"]
            else "partial"
            if result["robot_executed"]
            else "ready"
            if admitted and checks["passed"]
            else "review"
            if any(item["kind"] == "review" for item in detections)
            or (
                not paths
                and any(item["machining_required"] for item in detections)
            )
            else "no_defect_detected"
            if not paths
            else "blocked"
        )
        write_json(out / "result.json", result)
        write_json(out / "cycle_receipt.json", simulation_receipt(out, result))
        write_json(
            out / "replay.json",
            sim.replay_recording.document(
                source="actual integrated scan, joint and carried-workpiece states",
                sample_interval_s=0.1,
            ),
        )
        write_json(
            out / "live.json",
            sim.state(
                "complete" if result["robot_executed"] else "ready",
                prediction_revision=latest_revision,
                progress=1.0,
                pass_index=pass_index,
                mesh_updates=removal.updates(),
            ),
        )
        status("complete", task_status=result["task_status"])
        print(
            f"ESTUN cell: {out}; views={perception['view_count']}; "
            f"regions={len(detections)}; "
            f"accepted={result['admitted_path_count']}/{result['path_count']}; "
            f"executed={result['robot_executed']}; task={result['task_status']}",
            flush=True,
        )
        return result
    except Exception as error:
        status("failed", error=str(error))
        traceback.print_exc()
        raise
    finally:
        if sim is not None:
            sim.close()


def positive_removal_rate(value):
    """Decode a finite, positive synthetic normal-depth rate in mm/s."""
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0:
        raise argparse.ArgumentTypeError("去除率必须是有限正数，单位 mm/s")
    return rate


def parser():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--seed", type=int, default=7007)
    argument_parser.add_argument("--workpiece-profile", choices=job_profiles())
    argument_parser.add_argument(
        "--grasp-setup", choices=GRASP_SETUPS, default="original"
    )
    argument_parser.add_argument(
        "--camera-setup",
        choices=CAMERA_SETUPS,
        default="original",
        help="独立仿真固定相机布局，低位侧视用于名义 CAD 外壁补采",
    )
    argument_parser.add_argument(
        "--case",
        choices=WORKSTATION_CASES,
        default="mixed",
    )
    argument_parser.add_argument(
        "--casting-profile", choices=available_profiles(), default=BASE_PROFILE
    )
    argument_parser.add_argument(
        "--output", default=str(project.RUNS / "workstation/current_001")
    )
    argument_parser.add_argument("--perception-only", action="store_true")
    argument_parser.add_argument("--roi")
    argument_parser.add_argument("--ids")
    argument_parser.add_argument(
        "--mode", choices=("removal", "clearance"), default="removal"
    )
    argument_parser.add_argument(
        "--views", choices=("single", "multi"), default="multi"
    )
    argument_parser.add_argument(
        "--tool", choices=("belt", "wheel"), default="belt"
    )
    argument_parser.add_argument(
        "--planner", choices=("global", "segmented"), default="segmented"
    )
    argument_parser.add_argument("--compare-planners", action="store_true")
    argument_parser.add_argument(
        "--max-passes", type=int, choices=(1, 2, 3), default=2
    )
    argument_parser.add_argument("--realtime-factor", type=float, default=2.0)
    argument_parser.add_argument(
        "--removal-rate-mm-s",
        type=positive_removal_rate,
        help="仿真假定法向去除率（mm/s），按实际工具足迹与积分时长累积",
    )
    argument_parser.add_argument(
        "--detector",
        choices=("geometric", "learned", "hybrid"),
        default="geometric",
    )
    argument_parser.add_argument("--checkpoint")
    argument_parser.add_argument("--targeted-rescan", action="store_true")
    argument_parser.add_argument(
        "--keep-staging-transfers", action="store_true"
    )
    return argument_parser


def main():
    run(parser().parse_args())


if __name__ == "__main__":
    main()
