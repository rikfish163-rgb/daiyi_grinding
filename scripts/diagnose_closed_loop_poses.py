#!/usr/bin/env python3
"""Audit blocked measured paths against the unchanged CAD with extra poses."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from daiyi_grinding.workstation_model import WORKPIECE_FROM_GRIPPER, Workstation
from daiyi_grinding.workstation_motion import (
    NORMAL,
    unexpected_contacts,
    workpiece_target,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds-per-path", type=float, default=45)
    args = parser.parse_args()
    result = json.loads((args.episode / "result.json").read_text())
    paths = json.loads((args.episode / "paths.json").read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    sim = Workstation(
        args.output,
        result["seed"],
        result["case"],
        tool="belt",
        casting_profile=result.get("casting_profile") or "research_casting_v1",
    )
    rows = []
    try:
        for path in paths:
            points = np.asarray(path["nominal_xyz"])[
                [0, len(path["nominal_xyz"]) // 2, -1]
            ]
            alignment = Rotation.align_vectors([NORMAL], [path["normal"]])[0]
            reasons = Counter()
            collisions = Counter()
            feasible = []
            start = time.monotonic()
            attempts = 0
            for tilt in (0, 10, 20):
                for roll in (
                    45,
                    60,
                    30,
                    90,
                    120,
                    150,
                    180,
                    210,
                    0,
                    240,
                    270,
                    300,
                    330,
                    15,
                    75,
                    105,
                    135,
                    165,
                    195,
                    225,
                    255,
                    285,
                    315,
                    345,
                ):
                    for azimuth in (0,) if tilt == 0 else (0, 90, 180, 270):
                        axis = [
                            0,
                            np.cos(np.deg2rad(azimuth)),
                            np.sin(np.deg2rad(azimuth)),
                        ]
                        rotation = (
                            Rotation.from_rotvec(
                                np.asarray(axis) * np.deg2rad(tilt)
                            )
                            * Rotation.from_rotvec(NORMAL * np.deg2rad(roll))
                            * alignment
                        ).as_matrix()
                        for z in (0, 0.055, -0.055, 0.065, -0.065):
                            for y in (0, -0.025, 0.025, -0.035, 0.035):
                                if (
                                    time.monotonic() - start
                                    > args.seconds_per_path
                                ):
                                    break
                                attempts += 1
                                contact = sim.tool.contact + [0, y, z]
                                seed = sim.data.qpos[sim.q_index].copy()
                                clear = True
                                for point in points:
                                    try:
                                        target = workpiece_target(
                                            point, rotation, 0.00008, contact
                                        )
                                        q = sim.ik(
                                            target @ WORKPIECE_FROM_GRIPPER,
                                            seed,
                                        )[0]
                                        sim.set_scratch_pose(q, attached=True)
                                        pairs = unexpected_contacts(
                                            sim, sim.scratch, "trace"
                                        )
                                    except ValueError:
                                        reasons["inverse_kinematics"] += 1
                                        clear = False
                                        break
                                    if pairs:
                                        reasons["CAD_collision"] += 1
                                        for pair in pairs:
                                            collisions[
                                                " / ".join(pair["pair"])
                                            ] += 1
                                        clear = False
                                        break
                                    seed = q
                                if clear:
                                    feasible.append(
                                        {
                                            "tilt_deg": tilt,
                                            "azimuth_deg": azimuth,
                                            "roll_deg": roll,
                                            "contact": contact.tolist(),
                                            "rotation": rotation.tolist(),
                                        }
                                    )
                            if time.monotonic() - start > args.seconds_per_path:
                                break
                        if time.monotonic() - start > args.seconds_per_path:
                            break
                    if time.monotonic() - start > args.seconds_per_path:
                        break
                if time.monotonic() - start > args.seconds_per_path:
                    break
            row = {
                "segment_id": path["segment_id"],
                "kind": path["kind"],
                "normal": path["normal"],
                "attempts": attempts,
                "seconds": time.monotonic() - start,
                "reasons": dict(reasons),
                "collision_pairs": collisions.most_common(12),
                "feasible_sparse_poses": feasible[:30],
                "feasible_count": len(feasible),
                "scope": "Three measured path points, unchanged CAD and limits. Sparse feasibility does not authorize motion.",
            }
            rows.append(row)
            (args.output / "diagnosis.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2)
            )
            print(
                f"segment={path['segment_id']} attempts={attempts} feasible={len(feasible)} collisions={collisions.most_common(3)}",
                flush=True,
            )
    finally:
        sim.close()


if __name__ == "__main__":
    main()
