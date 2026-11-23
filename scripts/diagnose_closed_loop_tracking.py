#!/usr/bin/env python3
"""Replay the same admitted joint curve more slowly, with unchanged safety gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from daiyi_grinding.learned_defects import LearnedDetector, file_sha256
from daiyi_grinding.workstation_inspection import Inspection
from daiyi_grinding.workstation_model import (
    CELL_CONFIG,
    Workstation,
    write_json,
)
from daiyi_grinding.workstation_motion import execute, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--time-factor", type=float, default=2)
    args = parser.parse_args()
    if args.time_factor < 1:
        raise ValueError("This diagnosis only slows the admitted curve")
    original = json.loads((args.episode / "result.json").read_text())
    command_file = args.episode / "motion_commands.json"
    commands = json.loads(command_file.read_text())
    for command in commands:
        command["t"] *= args.time_factor
        for name in ("q", "pose", "point"):
            if command.get(name) is not None:
                command[name] = np.asarray(command[name])
    args.output.mkdir(parents=True, exist_ok=False)
    sim = Workstation(
        args.output,
        original["seed"],
        original["case"],
        tool="belt",
        casting_profile=original.get("casting_profile")
        or "research_casting_v1",
    )
    try:
        detector = LearnedDetector(args.episode / "detector_model.pt")
        _, _, initial = Inspection(
            sim, original["seed"], learned_detector=detector
        ).scan()
        checks = preflight(sim, commands)
        if not checks["passed"]:
            raise RuntimeError(
                "The slowed identical curve failed geometric preflight"
            )
        motion, removal = execute(
            sim, commands, "removal", lambda state: None, realtime_factor=0
        )
        removal.upload()
        _, _, after = Inspection(
            sim,
            original["seed"],
            revision=initial["prediction_revision"] + 1,
            prefix="rescan_",
            learned_detector=detector,
        ).scan()
        limit = CELL_CONFIG["planning"]["tracking_p95_limit_mm"]
        passed = (
            motion["unexpected_contact_count"] == 0
            and motion["tracking_trace_p95_mm"] is not None
            and motion["tracking_trace_p95_mm"] < limit
            and motion["surface_tracking_p95_mm"] is not None
            and motion["surface_tracking_p95_mm"] < limit
        )
        result = {
            "source_episode": str(args.episode.resolve()),
            "source_commands_sha256": file_sha256(command_file),
            "time_factor": args.time_factor,
            "geometry_and_joint_waypoints_unchanged": True,
            "safety_thresholds_unchanged": True,
            "preflight": checks,
            "initial_perception": initial,
            "execution": motion,
            "final_stock": removal.summary(),
            "reinspection": after,
            "motion_gate_passed": passed,
            "scope": (
                "Paired timing diagnosis on a previously observed failure. "
                "Development evidence, no new perception/planning claim."
            ),
        }
        write_json(args.output / "result.json", result)
        write_json(
            args.output / "replay.json",
            sim.replay_recording.document(
                source="actual integrated timing diagnosis",
                sample_interval_s=0.1,
            ),
        )
        print(
            {
                "surface_p95_mm": motion["surface_tracking_p95_mm"],
                "joint_fk_p95_mm": motion["tracking_trace_p95_mm"],
                "contacts": motion["unexpected_contact_count"],
                "remaining_peak_mm": removal.summary()["remaining_peak_mm"],
                "motion_gate_passed": passed,
            },
            flush=True,
        )
    finally:
        sim.close()


if __name__ == "__main__":
    main()
