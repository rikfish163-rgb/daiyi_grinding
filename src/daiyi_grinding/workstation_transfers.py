"""Remove detours between already accepted machining segments."""

from collections import Counter

import numpy as np

from .workstation_motion import binding_failure, preflight
from .workstation_planning import Commands, PlanningBudget


def transfer_windows(commands):
    """Find the retracted end of a trace and the next pre-approach waypoint."""
    start = None
    for index, command in enumerate(commands):
        phase = command["phase"]
        if phase == "retract" and command["attached"]:
            start = index
        elif phase == "approach":
            if start is not None and index > start + 2:
                yield start, index - 1
            start = None
        elif (
            phase not in {"transfer", "return"}
            or not command["attached"]
            or command.get("event")
        ):
            start = None


def continuous_joins(commands):
    """Join only existing traces whose end and start share an exact joint pose."""
    for start, retreat_end in transfer_windows(commands):
        before = start
        while before >= 0 and commands[before]["phase"] == "retract":
            before -= 1
        after = retreat_end + 1
        while (
            after + 1 < len(commands)
            and commands[after + 1]["phase"] == "approach"
        ):
            after += 1
        last, first = commands[before], commands[after]
        if (
            before >= 0
            and last["phase"] == "trace"
            and after + 1 < len(commands)
            and commands[after + 1]["phase"] == "trace"
            and last["detection_id"] == first["detection_id"]
            and last["segment_id"] != first["segment_id"]
            and np.allclose(last["point"], first["point"], atol=1e-7, rtol=0)
            and np.allclose(last["q"], first["q"], atol=1e-7, rtol=0)
        ):
            yield before, after, start


def bridge(sim, start, end):
    if (
        not start["attached"]
        or not end["attached"]
        or start.get("event")
        or end.get("event")
    ):
        return None
    builder = Commands(sim, budget=PlanningBudget(seconds=3.0))
    builder.q = start["q"].copy()
    builder.pose = start["pose"].copy()
    builder.attached = True
    goal_workpiece = end["pose"] @ sim.gripper_from_workpiece
    builder.leg(goal_workpiece, "transfer")
    if not np.allclose(builder.q, end["q"], atol=1e-5, rtol=0):
        return None
    # Keep exactly the joint state chosen by the original plan at the join.
    builder.items[-1] = {**end, "t": builder.items[-1]["t"]}
    return builder.items


def splice(commands, shortcuts):
    modified = []
    offset = 0.0
    index = 0
    while index < len(commands):
        command = commands[index]
        modified.append({**command, "t": command["t"] + offset})
        if index not in shortcuts:
            index += 1
            continue
        end, replacement = shortcuts[index]
        start_time = command["t"] + offset
        modified.extend(
            {**item, "t": start_time + item["t"]} for item in replacement
        )
        offset = modified[-1]["t"] - commands[end]["t"]
        index = end + 1
    return modified


def shorten_transfers(sim, commands, admitted, checks):
    if binding_failure(sim, commands):
        return (
            [],
            [],
            {
                **checks,
                **preflight(sim, commands),
                "admitted_count": 0,
                "blocked_count": checks.get("segment_count", len(admitted)),
                "all_paths_passed": False,
            },
        )
    if not checks["passed"] or len(admitted) < 2:
        return commands, admitted, checks

    original_duration = commands[-1]["t"]
    joins = {first: (last, []) for first, last, _ in continuous_joins(commands)}
    removed_detours = {start for _, _, start in continuous_joins(commands)}
    shortcuts = dict(joins)
    skipped = Counter()
    for start, end in transfer_windows(commands):
        if start in removed_detours:
            continue
        try:
            replacement = bridge(sim, commands[start], commands[end])
        except ValueError:
            skipped["connector_blocked"] += 1
            continue
        if not replacement:
            skipped["different_ik_branch"] += 1
        elif replacement[-1]["t"] >= commands[end]["t"] - commands[start]["t"]:
            skipped["no_time_saving"] += 1
        else:
            shortcuts[start] = end, replacement

    selected = shortcuts
    optimized = commands
    curve_check = checks
    if selected:
        optimized = splice(commands, selected)
        curve_check = preflight(sim, optimized)
        if not curve_check["passed"]:
            # Preserve any independently valid shortcuts. Every resulting
            # combination is checked, including interpolation at both joins.
            selected = {}
            for start, shortcut in shortcuts.items():
                trial = {**selected, start: shortcut}
                candidate = splice(commands, trial)
                checked = preflight(sim, candidate)
                if checked["passed"]:
                    selected, optimized, curve_check = trial, candidate, checked
                else:
                    skipped["interpolated_connector_blocked"] += 1
            if not selected:
                optimized, curve_check = commands, checks

    stats = {
        "original_duration_s": original_duration,
        "optimized_duration_s": optimized[-1]["t"],
        "direct_transfers": len(selected.keys() - joins.keys()),
        "merged_traces": len(selected.keys() & joins.keys()),
        "candidate_shortcuts": len(shortcuts),
        "preserved_original_transfers": dict(skipped),
        "original_samples": len(commands),
        "optimized_samples": len(optimized),
    }
    return (
        optimized,
        admitted,
        {
            **checks,
            **curve_check,
            "duration_s": stats["optimized_duration_s"],
            "sample_count": len(optimized),
            "transfer_optimization": stats,
        },
    )
