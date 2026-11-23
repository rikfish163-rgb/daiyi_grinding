"""Optional external VLA shadow-evaluation interface; no robot execution or networking.

Use the workspace MuJoCo venv for prepare/audit. Running `infer` requires an
explicit external model environment (--model-root or DAIYI_HY_ROOT); this
repository does not bundle VLA weights or external model code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import project

DEFAULT_OUTPUT = project.RUNS / "vla/shadow_new"
PROMPTS = {
    "grind": "Use the ball abrasive held by the left robot arm to trace the narrow casting parting seam and local burrs on the metal flange. Preserve the broad design rib, boss and recessed opening. Keep the other arm still.",
    "hold": "Keep both robot arms still. Do not touch the metal flange.",
}


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


def prepare(args):
    import cv2
    from scipy.spatial.transform import Rotation

    from daiyi_grinding.geometry import generate
    from daiyi_grinding.perception import detect
    from daiyi_grinding.planning import plan
    from daiyi_grinding.sim import Simulation

    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(
            "Choose a fresh output directory; inputs cannot be overwritten"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    sim = Simulation(generate(args.scene_seed, args.case))
    try:
        frames = {
            name: sim.render(name)
            for name in ("overview", "wrist", "inspection")
        }
        depth = sim.render("inspection", depth=True)
        K, T = sim.camera_info("inspection")
        p = sim.data.site_xpos[sim.tcp].copy()
        R = sim.data.site_xmat[sim.tcp].reshape(3, 3).copy()
        q = Rotation.from_matrix(R).as_quat()[[3, 0, 1, 2]]
        # A virtual duplicated TCP, explicitly NOT an observed second robot.
        state = np.tile(np.r_[p, q, 0.0], 2).astype(np.float32)
        np.savez_compressed(
            args.output / "input.npz",
            head=frames["overview"],
            left=frames["wrist"],
            right=frames["inspection"],
            eef=state,
            rgb=frames["inspection"],
            depth=depth,
            K=K,
            T_world_camera=T,
            qpos=sim.home.copy(),
            tcp_rotation=R,
        )
        for name, rgb in frames.items():
            cv2.imwrite(
                str(args.output / (name + ".png")),
                cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            )
        detections, observation = detect(frames["inspection"], depth, K, T)
        paths = plan(detections, observation)
        write_json(
            args.output / "baseline.json",
            {
                "detections": [d.summary() for d in detections],
                "paths": paths,
                "baseline": "RGB-D geometric reference, not VLA labels",
            },
        )
        selected = [d.xyz for d in detections if d.kind in {"seam", "burr"}]
        np.savez_compressed(
            args.output / "reference_points.npz",
            selected_xyz=np.vstack(selected) if selected else np.empty((0, 3)),
        )
        snapshot = args.output / "source_snapshot"
        shutil.copytree(
            project.SOURCE,
            snapshot,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        manifest = {
            "scope": "MuJoCo rendered FR5 single-arm casting scene; shadow inference only",
            "scene_seed": args.scene_seed,
            "scene_case": args.case,
            "frame_shape": list(frames["overview"].shape),
            "camera_mapping": {
                "top_head": "overview",
                "hand_left": "FR5 wrist",
                "hand_right": "inspection (fixed camera substituted for absent right wrist)",
            },
            "state_semantics": "[left xyz m,wxyz,gripper; right xyz m,wxyz,gripper]; left=FR5 ball tip TCP; right=duplicated virtual TCP; grippers=0 placeholders",
            "coordinate_assumption": "MuJoCo world numbers passed as RoboTwin world; no learned or calibrated cross-embodiment transform",
            "depth_usage": "used by geometric baseline and audit only; Hy RGB/state input does not consume depth",
            "history": "first observation only; 6 history slots at interval 5, startup zero padding, no invented temporal motion",
            "tool": "nominal 6mm spherical abrasive, TCP at ball tip; RoboTwin EEF/gripper/tool convention uncalibrated",
            "source_directory": "source_snapshot",
            "input_schema_version": 1,
            "unconditional_execution_blockers": [
                "dual_arm_to_FR5_mapping_not_calibrated",
                "missing_second_arm_and_right_wrist",
                "RoboTwin_EEF_to_ball_tool_TCP_not_calibrated",
                "no_casting_grinding_finetune_or_task_success_validation",
                "no_action_timebase_force_or_material_removal_model",
            ],
        }
        write_json(args.output / "input_manifest.json", manifest)
        print(
            json.dumps(
                {
                    "prepared": str(args.output),
                    "detections": len(detections),
                    "paths": len(paths),
                }
            )
        )
    finally:
        sim.close()


class HyShadowCandidatePolicy:
    """Candidate strategy interface. Returned arrays must never be executed directly."""

    def __init__(self, checkpoint, model_root):
        sys.path.insert(0, str(model_root))
        from extension.policy_runtime import FrozenChunkRuntime

        self.runtime = FrozenChunkRuntime(checkpoint)
        self.raw_normalized = None
        original_decoder = self.runtime.wrapper._decode_actions

        def capture_decoder(actions, anchor):
            self.raw_normalized = actions.copy()
            return original_decoder(actions, anchor)

        self.runtime.wrapper._decode_actions = capture_decoder

    def propose(self, frames, state, instruction, seed):
        from extension.temporal import HistoryAssembler

        temporal = HistoryAssembler(
            history_size=6, interval=5, execution_steps=7
        )
        temporal.append(frames)
        history = temporal.build_batch()
        self.runtime.reset(seed)
        decoded = self.runtime.infer(
            {
                "frames": frames,
                "state": state.copy(),
                "instruction": instruction,
            },
            history,
        )
        return {
            "decoded_world_eef": decoded,
            "raw_normalized": self.raw_normalized.copy(),
            "history": {k: v.cpu().numpy() for k, v in history.items()},
            "execution_allowed": False,
        }


def infer(args):
    if args.model_root is None or not args.model_root.is_dir():
        raise RuntimeError(
            "External --model-root (or DAIYI_HY_ROOT) directory is required "
            "for infer; daiyi_grinding does not bundle VLA models or weights"
        )
    if (
        args.checkpoint is None
        or not (args.checkpoint / "model.safetensors").is_file()
    ):
        raise RuntimeError(
            f"Model checkpoint not found under {args.checkpoint}"
        )
    import torch

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    torch.set_num_threads(4)
    if not torch.cuda.is_available():
        raise RuntimeError(
            "Existing model environment and a free CUDA GPU are required"
        )
    checkpoint = args.checkpoint.resolve()
    source = json.loads((args.model_root / "configs/sources.json").read_text())
    # Verify actual local large weights, not just a copied metadata claim.
    weight_sha = sha_file(checkpoint / "model.safetensors")
    if weight_sha != source["hf_weights_sha256"]:
        raise RuntimeError("Checkpoint SHA256 does not match pinned release")
    with np.load(args.output / "input.npz", allow_pickle=False) as z:
        frames = [z[k].copy() for k in ("head", "left", "right")]
        state = z["eef"].copy()
    torch.cuda.reset_peak_memory_stats()
    start = time.perf_counter()
    policy = HyShadowCandidatePolicy(checkpoint, args.model_root)
    torch.cuda.synchronize()
    load_seconds = time.perf_counter() - start
    runs = []
    variants = [("grind", "observed", s, False) for s in args.seeds]
    variants += [
        ("hold", "observed", args.seeds[0], False),
        ("grind", "black_images", args.seeds[0], False),
        ("grind", "observed", args.seeds[0], True),
    ]
    with torch.inference_mode():
        for index, (prompt_id, visual, seed, repeated) in enumerate(variants):
            run_id = f"{index:02d}_{prompt_id}_{visual}_seed{seed}" + (
                "_repeat" if repeated else ""
            )
            current_frames = (
                frames
                if visual == "observed"
                else [np.zeros_like(f) for f in frames]
            )
            torch.cuda.synchronize()
            start = time.perf_counter()
            candidate = policy.propose(
                current_frames, state, PROMPTS[prompt_id], seed
            )
            torch.cuda.synchronize()
            seconds = time.perf_counter() - start
            arrays = {
                "decoded_world_eef": candidate["decoded_world_eef"],
                "raw_normalized": candidate["raw_normalized"],
            }
            arrays.update(
                {"history__" + k: v for k, v in candidate["history"].items()}
            )
            np.savez_compressed(args.output / (run_id + ".npz"), **arrays)
            runs.append(
                {
                    "id": run_id,
                    "seed": seed,
                    "prompt_id": prompt_id,
                    "prompt": PROMPTS[prompt_id],
                    "visual": visual,
                    "repeated": repeated,
                    "inference_seconds": seconds,
                    "raw_normalized_shape": list(
                        arrays["raw_normalized"].shape
                    ),
                    "decoded_shape": list(arrays["decoded_world_eef"].shape),
                    "finite": bool(
                        all(np.isfinite(a).all() for a in arrays.values())
                    ),
                    "execution_allowed": False,
                }
            )
            write_json(args.output / "runs_partial.json", runs)
            print(
                json.dumps(
                    {
                        "run": run_id,
                        "seconds": seconds,
                        "decoded_shape": runs[-1]["decoded_shape"],
                    }
                ),
                flush=True,
            )
    metadata = {
        "status": "real_released_checkpoint_shadow_inference_completed",
        "source": source,
        "actual_weight_sha256": weight_sha,
        "actual_code_commit": subprocess.check_output(
            [
                "git",
                "-C",
                str(args.model_root / "vendor/Hy-Embodied-0.5-VLA"),
                "rev-parse",
                "HEAD",
            ],
            text=True,
        ).strip(),
        "checkpoint": str(checkpoint),
        "load_seconds": load_seconds,
        "weight_load_audit": policy.runtime.load_audit,
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(),
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30,
        "peak_reserved_gib": torch.cuda.max_memory_reserved() / 2**30,
        "official_decoder": "rel_abs; equal relative/absolute positions and slerp quaternions; wxyz dual-arm EEF",
        "raw_semantics": "normalized model output before official denormalization and rel_abs decoding; not FR5 joint commands",
        "train_updates": 0,
        "runs": runs,
        "execution_allowed": False,
    }
    write_json(args.output / "inference.json", metadata)
    print(
        json.dumps(
            {
                "status": metadata["status"],
                "runs": len(runs),
                "load_seconds": load_seconds,
            }
        ),
        flush=True,
    )


def audit(args):
    from scipy.spatial import cKDTree
    from scipy.spatial.transform import Rotation

    from daiyi_grinding.execution import collision_pairs
    from daiyi_grinding.geometry import generate
    from daiyi_grinding.sim import Simulation

    manifest = json.loads((args.output / "input_manifest.json").read_text())
    inference = json.loads((args.output / "inference.json").read_text())
    baseline = json.loads((args.output / "baseline.json").read_text())
    with np.load(args.output / "input.npz", allow_pickle=False) as z:
        state = z["eef"].copy()
    with np.load(args.output / "reference_points.npz") as z:
        surface_points = z["selected_xyz"].copy()
    path_points = (
        np.vstack([p["xyz"] for p in baseline["paths"]])
        if baseline["paths"]
        else np.empty((0, 3))
    )
    tree = cKDTree(path_points) if len(path_points) else None
    surface_tree = cKDTree(surface_points) if len(surface_points) else None
    sim = Simulation(generate(manifest["scene_seed"], manifest["scene_case"]))
    records = []
    action_map = {}
    try:
        for run in inference["runs"]:
            with np.load(args.output / (run["id"] + ".npz")) as z:
                actions = z["decoded_world_eef"].copy()
            action_map[run["id"]] = actions
            xyz = actions[:, :3]
            quat = actions[:, 3:7]
            rotation = Rotation.from_quat(quat[:, [1, 2, 3, 0]])
            z_axis = rotation.as_matrix()[:, :, 2]
            downward_degrees = np.rad2deg(
                np.arccos(np.clip(-z_axis[:, 2], -1, 1))
            )
            deltas = np.linalg.norm(
                np.diff(np.vstack([state[:3], xyz]), axis=0), axis=1
            )
            workspace = (
                (xyz[:, 0] >= -0.67)
                & (xyz[:, 0] <= -0.31)
                & (xyz[:, 1] >= -0.27)
                & (xyz[:, 1] <= 0.20)
                & (xyz[:, 2] >= 0.612)
                & (xyz[:, 2] <= 0.95)
            )
            ik = []
            seed = sim.home.copy()
            for index, (p, r) in enumerate(zip(xyz, rotation.as_matrix())):
                try:
                    q, pe, re = sim.ik(p, r, seed)
                    sim.scratch.qpos[:] = q
                    import mujoco

                    mujoco.mj_forward(sim.model, sim.scratch)
                    contacts = collision_pairs(sim, sim.scratch)
                    ik.append(
                        {
                            "step": index,
                            "solved": True,
                            "position_error_mm": pe * 1000,
                            "rotation_error_deg": float(np.rad2deg(re)),
                            "q": q.tolist(),
                            "proxy_penetrations": [
                                [a, b, -d * 1000]
                                for a, b, d in contacts
                                if d < -0.0001
                            ],
                        }
                    )
                    seed = q
                except ValueError as error:
                    ik.append(
                        {"step": index, "solved": False, "reason": str(error)}
                    )
            records.append(
                {
                    "id": run["id"],
                    "execution_allowed": False,
                    "unconditional_blockers": manifest[
                        "unconditional_execution_blockers"
                    ],
                    "left_xyz_min_m": xyz.min(axis=0).tolist(),
                    "left_xyz_max_m": xyz.max(axis=0).tolist(),
                    "first_left_translation_mm": float(deltas[0] * 1000),
                    "max_translation_per_untimed_waypoint_mm": float(
                        deltas.max() * 1000
                    ),
                    "translation_steps_above_20mm": int((deltas > 0.020).sum()),
                    "workspace_rejected_steps": np.flatnonzero(
                        ~workspace
                    ).tolist(),
                    "tool_down_angle_deg_range": [
                        float(downward_degrees.min()),
                        float(downward_degrees.max()),
                    ],
                    "orientation_rejected_steps_above_12deg": np.flatnonzero(
                        downward_degrees > 12
                    ).tolist(),
                    "quaternion_norm_max_error": float(
                        np.abs(np.linalg.norm(quat, axis=1) - 1).max()
                    ),
                    "nearest_geometric_path_mm": None
                    if tree is None
                    else (tree.query(xyz)[0] * 1000).tolist(),
                    "nearest_selected_surface_mm": None
                    if surface_tree is None
                    else (surface_tree.query(xyz)[0] * 1000).tolist(),
                    "virtual_right_arm_motion_max_mm": float(
                        np.linalg.norm(
                            actions[:, 8:11] - state[8:11], axis=1
                        ).max()
                        * 1000
                    ),
                    "left_gripper_range": [
                        float(actions[:, 7].min()),
                        float(actions[:, 7].max()),
                    ],
                    "ik_solved_steps": sum(x["solved"] for x in ik),
                    "ik_audit": ik,
                    "ik_scope": "left decoded EEF hypothetically interpreted as FR5 ball-tip TCP; no corrected frame mapping; endpoint proxy contact checks only",
                }
            )
    finally:
        sim.close()
    base_id = inference["runs"][0]["id"]
    base = action_map[base_id]
    comparisons = []
    for run in inference["runs"][1:]:
        current = action_map[run["id"]]
        comparisons.append(
            {
                "reference": base_id,
                "candidate": run["id"],
                "max_absolute_decoded_difference": float(
                    np.abs(current - base).max()
                ),
                "left_xyz_rms_difference_mm": float(
                    np.sqrt(np.mean((current[:, :3] - base[:, :3]) ** 2)) * 1000
                ),
                "bitwise_equal": bool(np.array_equal(current, base)),
                "interpretation": "diagnostic sensitivity/repeatability; does not measure instruction following or task success",
            }
        )
    report = {
        "status": "completed_shadow_only_all_candidates_blocked",
        "execution_allowed": False,
        "executed_vla_actions": 0,
        "vla_task_success": None,
        "limits": {
            "workspace_xyz_m": [[-0.67, -0.31], [-0.27, 0.20], [0.612, 0.95]],
            "max_untimed_translation_step_m": 0.020,
            "tool_down_angle_deg": 12,
        },
        "limits_scope": "configured diagnostic envelope for prototype, not a certified process or safety specification",
        "runs": records,
        "paired_comparisons": comparisons,
    }
    write_json(args.output / "audit.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "runs": len(records),
                "ik_solved": [r["ik_solved_steps"] for r in records],
                "executed_vla_actions": 0,
            }
        )
    )


def main():
    env_model_root = os.environ.get("DAIYI_HY_ROOT")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "infer", "audit"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--model-root",
        type=Path,
        default=Path(env_model_root) if env_model_root else None,
    )
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--scene-seed", type=int, default=7)
    parser.add_argument("--case", default="mixed")
    parser.add_argument("--seeds", type=int, nargs="+", default=[17, 29, 43])
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.model_root is not None:
        args.model_root = args.model_root.resolve()
    if args.checkpoint is not None:
        args.checkpoint = args.checkpoint.resolve()
    elif args.model_root is not None:
        args.checkpoint = args.model_root / "checkpoints/robotwin"
    {"prepare": prepare, "infer": infer, "audit": audit}[args.mode](args)


if __name__ == "__main__":
    main()
