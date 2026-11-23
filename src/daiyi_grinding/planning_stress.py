"""Low-resource planning failures and explicit, serial offline MuJoCo runs."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import shlex
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
from scipy.spatial.transform import Rotation

from . import project
from .workstation_model import GRASP_SETUPS, write_json
from .workstation_motion import (
    bind_commands,
    preflight,
    transform,
)
from .workstation_planning import (
    Commands,
    PlanningBudget,
    PlanningBudgetExceeded,
)
from .workstation_transfers import bridge

REGISTRATION = (
    project.RUNS
    / "enterprise_offline_20261123/planning/holdout_registration_01.json"
)
MUJOCO_LOCK = project.RUNS / "enterprise_offline_20261123/mujoco.lock"
STRESS_HOLDOUT_SEEDS = (2026112321, 2026112327)
REAL_DATA_PENDING = [
    "confirmed same-part Yang 097 raw scan and approved defect roots",
    "ESTUN controller joint mapping and camera/TCP calibration",
    "fixture repeatability and grasp stiffness measurements",
    "force, abrasive compliance and material removal calibration",
    "independent signed before/after profile measurements with uncertainty",
]


def sha256(path):
    """Hash a file without duplicating large feedback or mesh files in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PlanningFixture:
    """Analytical failure fixture, not an ESTUN robot or a physical workpiece.

    Three coordinates move a unit-radius reference by ``sin(q)``; the last
    three orient it. Narrow obstacles are supplied in joint coordinates. This
    cheap fixture exercises actual command building, curve checks and joins.
    """

    def __init__(self, obstacle=None):
        self.data = SimpleNamespace(qpos=np.zeros(6))
        self.scratch = SimpleNamespace(qpos=np.zeros(6), contact=[])
        self.q_index = np.arange(6)
        self.lower = np.array([-1.4, -1.4, -1.4, -7.0, -7.0, -7.0])
        self.upper = -self.lower
        self.gripper = 0
        self.grasp_setup = "fixture_a"
        self.gripper_from_workpiece = np.eye(4)
        self.workpiece_from_gripper = np.eye(4)
        self.loading_rotation = np.eye(3)
        self.tool = SimpleNamespace(key="fixture", geom="belt")
        self.obstacle = obstacle
        self.model = SimpleNamespace(
            geom=lambda index: SimpleNamespace(
                name=("robot_tip", "fixed_obstacle")[index]
            )
        )

    def body_transform(self, body, data=None):
        del body
        q = (self.data if data is None else data).qpos
        return transform(
            Rotation.from_euler("xyz", q[3:]).as_matrix(), np.sin(q[:3])
        )

    def ik(self, pose, seed):
        del seed
        position = pose[:3, 3]
        if np.any(np.abs(position) > np.sin(1.4)):
            raise ValueError("压力样本目标超出解析工作空间")
        q = np.r_[
            np.arcsin(position),
            Rotation.from_matrix(pose[:3, :3]).as_euler("xyz"),
        ]
        return q, 0.0, 0.0

    def set_scratch_pose(self, q, attached=False):
        del attached
        self.scratch.qpos = np.asarray(q).copy()
        self.scratch.contact = (
            [SimpleNamespace(dist=-0.001, geom1=0, geom2=1)]
            if self.obstacle is not None
            and self.obstacle[0] <= q[0] <= self.obstacle[1]
            else []
        )


def fixture_command(q0, *, phase="trace", segment_id=1):
    """Create a complete reference command, including its original segment ID."""
    q = np.array([q0, 0, 0, 0, 0, 0], dtype=float)
    return {
        "t": 1.0,
        "q": q,
        "pose": transform(np.eye(3), [np.sin(q0), 0, 0]),
        "phase": phase,
        "attached": True,
        "point": np.zeros(3),
        "detection_id": 1,
        "segment_id": segment_id,
    }


def aggregate_workpieces(workpieces):
    """Count complete source segments separately from all-segment workpieces."""
    segments = [passed for item in workpieces for passed in item["segments"]]
    return {
        "source_segment_count": len(segments),
        "complete_source_segment_count": sum(
            bool(passed) for passed in segments
        ),
        "workpiece_count": len(workpieces),
        "all_segments_verified_workpiece_count": sum(
            bool(item["segments"]) and all(item["segments"])
            for item in workpieces
        ),
        "physical_workpiece_release_count": 0,
        "scope": "complete injected source segments; whole-path planning counts exclude profile/inspection release",
    }


def run_suite(seeds=STRESS_HOLDOUT_SEEDS):
    """Run the original checker and the repair on identical declared inputs."""
    cases = []
    workpieces = {"legacy_four": [], "bounded_joint": []}
    for seed in seeds:
        rng = np.random.default_rng(seed)
        center = float(rng.uniform(0.03, 0.08))
        obstacle = (center - 0.01, center + 0.01)
        sim = PlanningFixture(obstacle)
        safe = fixture_command(0.015)
        blocked = fixture_command(0.4, segment_id=2)
        outcomes = {}
        for policy in workpieces:
            checked = [
                preflight(sim, [command], sampling_policy=policy)
                for command in (safe, blocked)
            ]
            statuses = [item["passed"] for item in checked]
            workpieces[policy].append({"seed": seed, "segments": statuses})
            outcomes[policy] = checked
        cases.append(
            {
                "id": f"mid_curve_collision_{seed}",
                "kind": "mid_curve_collision",
                "seed": seed,
                "input": {"obstacle_q0_interval_rad": list(obstacle)},
                "source_segment_ids": [1, 2],
                "expected": "first segment complete; second whole segment rejected",
                "guard_passed": outcomes["bounded_joint"][0]["passed"]
                and not outcomes["bounded_joint"][1]["passed"],
                "outcomes": outcomes,
            }
        )

    sim = PlanningFixture()
    builder = Commands(sim)
    try:
        builder.leg(transform(np.eye(3), [1.2, 0, 0]), "trace")
        reason, rejected = None, False
    except ValueError as error:
        reason, rejected = str(error), True
    cases.append(
        {
            "id": "unreachable",
            "kind": "unreachable",
            "guard_passed": rejected and not builder.items,
            "retained_commands": len(builder.items),
            "reason": reason,
            "scope": "unreachable entire trace rolls back without retained fragments",
        }
    )

    start, end = fixture_command(0, phase="retract"), fixture_command(0.03)
    end["q"][5] = 2 * np.pi
    replacement = bridge(sim, start, end)
    cases.append(
        {
            "id": "different_joint_branch",
            "kind": "different_joint_branch",
            "guard_passed": replacement is None,
            "replacement_retained": replacement is not None,
            "expected_endpoint_q6_rad": float(end["q"][5]),
            "scope": "same Cartesian orientation on another joint branch cannot replace the original join",
        }
    )

    budget = PlanningBudget(max_ik_calls=2)
    builder = Commands(sim, budget=budget)
    try:
        builder.leg(transform(np.eye(3), [0.024, 0, 0]), "trace")
        reason, rejected = None, False
    except PlanningBudgetExceeded as error:
        reason, rejected = str(error), True
    cases.append(
        {
            "id": "ik_budget_exhaustion",
            "kind": "budget_exhaustion",
            "guard_passed": rejected and not builder.items,
            "retained_commands": len(builder.items),
            "budget": budget.summary(),
            "reason": reason,
        }
    )
    checked = preflight(sim, [fixture_command(0.4)], sample_budget=4)
    cases.append(
        {
            "id": "preflight_budget_exhaustion",
            "kind": "budget_exhaustion",
            "guard_passed": not checked["passed"]
            and checked.get("budget_exhausted", False),
            "outcome": checked,
        }
    )

    commands = bind_commands(sim, [fixture_command(0.04)])
    before = preflight(sim, commands)
    sim.grasp_setup = "fixture_b"
    sim.gripper_from_workpiece = transform(np.eye(3), [0.001, 0, 0])
    sim.workpiece_from_gripper = np.linalg.inv(sim.gripper_from_workpiece)
    after = preflight(sim, commands)
    cases.append(
        {
            "id": "fixture_change_stale_plan",
            "kind": "fixture_change_stale_plan",
            "guard_passed": before["passed"]
            and not after["passed"]
            and after.get("stale_plan", False),
            "before": before,
            "after": after,
        }
    )
    return {
        "schema_version": 1,
        "scope": "analytical software failure injection; no CAD grinding, no evaluator labels and no real release",
        "baseline_scope": "exact old four-fraction sampling on the same C1 curve; other safety guards remain current",
        "cases": cases,
        "guards_passed": sum(item["guard_passed"] for item in cases),
        "guard_count": len(cases),
        "workpiece_counts": {
            policy: aggregate_workpieces(rows)
            for policy, rows in workpieces.items()
        },
    }


@contextmanager
def serial_mujoco(lock_path=MUJOCO_LOCK):
    """Acquire one cooperative global lock; fail before loading a busy model."""
    lock_path = Path(lock_path).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"MuJoCo 共享锁忙：{lock_path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run_mujoco_fixture(output):
    """Verify the sampling defect with real, unchanged sphere collision geometry."""
    import mujoco

    output = Path(output)
    output.mkdir(exist_ok=False)
    center = 0.06
    bodies = "".join(
        f'<body name="axis_{index}"><joint type="hinge" axis="0 0 1" '
        'range="-2 2"/><inertial pos="0 0 0" mass="0.1" '
        'diaginertia="0.01 0.01 0.01"/>'
        for index in range(6)
    )
    xml = (
        '<mujoco model="planning_mid_curve_collision"><compiler angle="radian"/>'
        '<option gravity="0 0 0"/><worldbody>'
        f'<geom name="fixed_obstacle" type="sphere" size="0.003" '
        f'pos="{np.cos(center)} {np.sin(center)} 0"/>'
        f'{bodies}<geom name="robot_tip" type="sphere" size="0.004" '
        'pos="1 0 0" mass="0.1"/>' + "</body>" * 6 + "</worldbody></mujoco>"
    )
    scene = output / "scene.xml"
    scene.write_text(xml + "\n")
    sim = PlanningFixture()
    sim.scene_file = scene
    sim.model = mujoco.MjModel.from_xml_path(str(scene))
    sim.data = mujoco.MjData(sim.model)
    sim.scratch = mujoco.MjData(sim.model)
    sim.lower, sim.upper = np.full(6, -2.0), np.full(6, 2.0)

    def set_pose(q, attached=False):
        del attached
        sim.scratch.qpos[:] = q
        mujoco.mj_forward(sim.model, sim.scratch)

    sim.set_scratch_pose = set_pose
    command = fixture_command(0.4, phase="transfer")
    result = {
        "scope": "real MuJoCo sphere collision fixture, no grinding or ESTUN success claim",
        "scene": {"path": str(scene.resolve()), "sha256": sha256(scene)},
        "legacy_four": preflight(sim, [command], sampling_policy="legacy_four"),
        "bounded_joint": preflight(sim, [command]),
        "collision_geometry_modified_between_policies": False,
    }
    result["guard_passed"] = (
        result["legacy_four"]["passed"]
        and not result["bounded_joint"]["passed"]
    )
    write_json(output / "result.json", result)
    return result


def run_closed_loop(output, seed, grasp_setup):
    """Use the existing whole-cell entry point, without replacing its quality API."""
    from .workstation_run import parser as cell_parser
    from .workstation_run import run as cell_run

    cell_output = Path(output) / "closed_loop"
    cell_arguments = [
        "--workpiece-profile",
        "097_thin_seam",
        "--seed",
        str(seed),
        "--grasp-setup",
        grasp_setup,
        "--mode",
        "removal",
        "--max-passes",
        "1",
        "--realtime-factor",
        "0",
        "--output",
        str(cell_output),
    ]
    args = cell_parser().parse_args(cell_arguments)
    command = [
        sys.executable,
        "-m",
        "daiyi_grinding.workstation_run",
        *cell_arguments,
    ]
    cell_run(args)
    result = json.loads((cell_output / "result.json").read_text())
    execution = result.get("execution", {})
    return {
        "command": command,
        "output": str(cell_output.resolve()),
        "seed": seed,
        "case": "thin",
        "grasp_setup": grasp_setup,
        "sample_role": "holdout"
        if seed in (2026112307, 2026112311)
        else "development_regression",
        "source_segment_count": result["path_count"],
        "complete_source_segment_count": result["admitted_path_count"],
        "all_segments_verified": result["preflight"].get(
            "all_paths_passed", False
        ),
        "robot_executed": result["robot_executed"],
        "material_removal_simulated": result["material_removal_simulated"],
        "motion_gate_passed": result.get("motion_gate_passed"),
        "tracking_trace_p95_mm": execution.get("tracking_trace_p95_mm"),
        "surface_tracking_p95_mm": execution.get("surface_tracking_p95_mm"),
        "unexpected_contact_count": execution.get("unexpected_contact_count"),
        "remaining_peak_mm": execution.get("removal", {}).get(
            "remaining_peak_mm"
        ),
        "observational_release": result.get("observational_release", False),
        "goal_complete": result["goal_complete"],
        "controller_truth_used": result["controller_stopping"][
            "truth_used_for_control"
        ],
        "stopping_reason": result["controller_stopping"].get("reason"),
        "scope": "offline synthetic 097 geometric removal; 1 mm motion gate does not establish enterprise +/-0.3 mm profile acceptance",
    }


def run(args):
    """Save reviewable commands, source hashes, guards and optional cell evidence."""
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = {
        "task": "offline continuous-curve planning stress",
        "scope": "software failure injection by default; MuJoCo collision and whole-cell grinding only with explicit flags",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "commands": [
            shlex.join(
                [
                    sys.executable,
                    "-m",
                    __package__ + ".planning_stress",
                    *sys.argv[1:],
                ]
            )
        ],
        "inputs": [],
        "metrics": {},
        "failures": [],
        "artifacts": [],
        "real_data_pending": REAL_DATA_PENDING,
    }
    inputs = [
        Path(__file__),
        project.SOURCE / "workstation_planning.py",
        project.SOURCE / "workstation_motion.py",
        project.SOURCE / "workstation_transfers.py",
        project.ROOT / "config/workstation.json",
        REGISTRATION,
    ]
    for path in inputs:
        if path.exists():
            evidence["inputs"].append(
                {"path": str(path.resolve()), "sha256": sha256(path)}
            )
    if not REGISTRATION.exists():
        evidence["failures"].append(
            "missing frozen holdout registration; seeds are regression-only"
        )
    suite = run_suite()
    write_json(output / "suite.json", suite)
    evidence["metrics"]["stress_suite"] = {
        key: suite[key]
        for key in ("guard_count", "guards_passed", "workpiece_counts")
    }
    evidence["failures"].extend(
        item["id"] for item in suite["cases"] if not item["guard_passed"]
    )
    with (output / "cases.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["id", "kind", "guard_passed"]
        )
        writer.writeheader()
        writer.writerows(
            {key: item[key] for key in writer.fieldnames}
            for item in suite["cases"]
        )
    if args.mujoco_fixture or args.closed_loop:
        try:
            with serial_mujoco(args.mujoco_lock):
                if args.mujoco_fixture:
                    checked = run_mujoco_fixture(output / "mujoco_fixture")
                    evidence["metrics"]["mujoco_fixture"] = checked
                    if not checked["guard_passed"]:
                        evidence["failures"].append("mujoco_fixture")
                if args.closed_loop:
                    checked = run_closed_loop(
                        output, args.seed, args.grasp_setup
                    )
                    evidence["commands"].append(shlex.join(checked["command"]))
                    evidence["metrics"]["closed_loop"] = checked
                    if not checked["goal_complete"]:
                        evidence["failures"].append(
                            {
                                "stage": "closed_loop_acceptance",
                                "kind": "recorded_hold",
                                "reason": checked["stopping_reason"],
                                "robot_executed": checked["robot_executed"],
                                "motion_gate_passed": checked[
                                    "motion_gate_passed"
                                ],
                                "observational_release": checked[
                                    "observational_release"
                                ],
                            }
                        )
        except Exception as error:
            evidence["failures"].append(
                {"stage": "explicit_mujoco_run", "reason": str(error)}
            )
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name in {
            "suite.json",
            "cases.csv",
            "result.json",
            "scene.xml",
            "motion_commands.json",
            "preflight.json",
            "pass_00_execution.csv",
            "pass_00_execution.json",
            "registered_regions.json",
            "cycle_receipt.json",
            "rescan_01_perception_metrics.json",
            "source_hashes.json",
        }:
            evidence["artifacts"].append(
                {"path": str(path.resolve()), "sha256": sha256(path)}
            )
    write_json(output / "evidence.json", evidence)
    print(
        json.dumps(
            {
                "output": str(output),
                "guards_passed": suite["guards_passed"],
                "guard_count": suite["guard_count"],
                "failures": evidence["failures"],
                "closed_loop": evidence["metrics"].get("closed_loop"),
            },
            ensure_ascii=False,
        )
    )
    return evidence


def parser():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("--output", required=True)
    argument_parser.add_argument("--mujoco-fixture", action="store_true")
    argument_parser.add_argument("--closed-loop", action="store_true")
    argument_parser.add_argument("--seed", type=int, default=120483)
    argument_parser.add_argument(
        "--grasp-setup", choices=GRASP_SETUPS, default="original"
    )
    argument_parser.add_argument("--mujoco-lock", default=str(MUJOCO_LOCK))
    return argument_parser


def main():
    evidence = run(parser().parse_args())
    errors = [
        item
        for item in evidence["failures"]
        if not isinstance(item, dict) or item.get("kind") != "recorded_hold"
    ]
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
