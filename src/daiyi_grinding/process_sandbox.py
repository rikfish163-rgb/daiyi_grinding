"""Bounded two-stage synthetic grinding-process sandbox.

The sandbox is deliberately independent of MuJoCo and
``workstation_motion.GeometricRemoval``. It uses replaceable synthetic proxies
for signed surface error, feed, force, compliance, material removal, tool wear,
temperature and measurement uncertainty. Results support interface rehearsal and
calibration planning, but never a physical process conclusion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import project

CONFIG_FILE = project.ROOT / "config/process_sandbox_20261123.json"
OUTPUT_ROOT = project.RUNS / "enterprise_offline_20261123/process"
SCHEMA = "daiyi-process-sandbox-v1"
RESULT_SCHEMA = "daiyi-process-sandbox-result-v1"
EVIDENCE_SCHEMA = "daiyi-process-sandbox-evidence-v1"
TOLERANCE_MM = 0.3
POINT_COUNT = 9
PROCESS_LENGTH_MM = 120.0
STAGES = ("roughing", "finishing")
POLICIES = ("adaptive", "fixed")
RELEASE_STOPS = {"already_in_signed_tolerance", "signed_tolerance_reached"}
REJECTION_STOPS = {
    "initial_undercut",
    "undercut",
    "measurement_missing",
    "measurement_uncertainty",
    "protected_zone_out_of_tolerance",
    "protected_zone_intersection",
    "collision_proxy",
    "pass_limit",
    "rework_budget",
    "budget_stop",
    "force_sensor_dropout",
    "force_limit_engaged",
    "thermal_guard",
}
R_AND_D_ROLES = (
    "surface_model",
    "feed_and_force",
    "compliance",
    "material_removal",
    "tool_wear",
    "thermal_proxy",
    "measurement_uncertainty",
    "adaptive_control",
)


def sha256(path):
    """Return the SHA-256 of a local file for provenance-only pinning."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config(path=CONFIG_FILE):
    """Load and validate the synthetic process configuration."""
    configuration = json.loads(Path(path).read_text(encoding="utf-8"))
    _normalize_config(configuration)
    validate_config(configuration)
    return configuration


def _normalize_config(configuration):
    """Fill explicit case defaults so each stress case records only deltas."""
    for case in configuration.get("cases", []):
        initial = case.setdefault("initial_mm", {})
        initial.setdefault("spread", 0.0)
        initial.setdefault("protected_points", [])
        initial.setdefault("protected_level_mm", 0.0)
        case.setdefault("rate_bias", 0.0)
        case.setdefault("material_factor", 0.9)
        case.setdefault("hardness_factor", 1.0)
        case.setdefault("initial_wear_index", 0.0)
        case.setdefault("force_sensor", {})
        measurement_settings = case.setdefault("measurement", {})
        measurement_settings.setdefault("sigma_mm", 0.03)
        measurement_settings.setdefault("bias_mm", 0.0)
        measurement_settings.setdefault("uncertainty_mm", 0.0588)
        measurement_settings.setdefault("missing_points", [])
        measurement_settings.setdefault("missing_from_scan", 0)
        path = case.setdefault("path", {})
        path.setdefault("clearance_mm", 8.0)
        path.setdefault(
            "contact_points",
            sorted(set(range(POINT_COUNT)) - set(initial["protected_points"])),
        )
        stage_limits = case.setdefault("stage_limits", {})
        stage_limits.setdefault("roughing", {"max_passes": 3})
        stage_limits.setdefault("finishing", {"max_passes": 4})
        case.setdefault("max_rework_passes", 3)
        case.setdefault("max_runtime_s", 25.0)
        case.setdefault("expected_stop", None)
        case.setdefault("expected_false_release", False)


def _number(value, name, *, minimum=None):
    """Validate one finite numeric configuration value."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (minimum is not None and value < minimum)
    ):
        bounds = f" >= {minimum}" if minimum is not None else ""
        raise ValueError(f"{name} must be a finite number{bounds}")
    return float(value)


def _positive(value, name):
    if _number(value, name) <= 0.0:
        raise ValueError(f"{name} must be positive")


def _integer(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _validate_points(points, name):
    if (
        not isinstance(points, list)
        or any(
            type(index) is not int or not 0 <= index < POINT_COUNT
            for index in points
        )
        or len(points) != len(set(points))
    ):
        raise ValueError(f"{name} must contain unique point indices 0–8")


def _validate_parameters(parameters):
    if parameters["surface_tolerance_mm"] != TOLERANCE_MM:
        raise ValueError("The signed tolerance must be ±0.3 mm")
    for key in (
        "base_removal_mm_per_n_s",
        "compliance_n_per_mm",
        "thermal_tau_s",
        "temperature_rise_per_j_k",
        "temperature_guard_k",
        "reference_residual_mm",
        "minimum_engagement_ratio",
        "maximum_engagement_ratio",
        "maximum_force_n",
        "nominal_material_factor",
        "minimum_clearance_mm",
    ):
        _positive(parameters[key], key)
    for key in ("wear_removal_penalty", "wear_index_per_n_s"):
        _number(parameters[key], key, minimum=0)
    if (
        parameters["minimum_engagement_ratio"]
        > parameters["maximum_engagement_ratio"]
    ):
        raise ValueError("Synthetic engagement ratio bounds are reversed")
    if (
        _number(parameters["roughing_exit_mm"], "roughing_exit_mm")
        <= TOLERANCE_MM
        or abs(
            _number(
                parameters["finishing_target_offset_mm"],
                "finishing_target_offset_mm",
            )
        )
        > TOLERANCE_MM
    ):
        raise ValueError("Stage targets must straddle the positive tolerance")
    for stage in STAGES:
        _positive(parameters["stage_force_gain"][stage], f"{stage} force gain")
        limits = parameters["stage_limits"][stage]
        for key in (
            "adaptive_feed_mm_s",
            "fixed_feed_mm_s",
            "minimum_force_n",
            "maximum_force_n",
            "fixed_force_n",
        ):
            _positive(limits[key], f"{stage}.{key}")
        if not (
            limits["minimum_force_n"]
            <= limits["fixed_force_n"]
            <= limits["maximum_force_n"]
            <= parameters["maximum_force_n"]
        ):
            raise ValueError(f"{stage} commanded forces outside model bounds")


def _validate_case(case):
    initial = case["initial_mm"]
    for key in ("base", "spread", "protected_level_mm"):
        _number(initial[key], f"initial_mm.{key}")
    _validate_points(initial["protected_points"], "protected_points")
    _validate_points(case["path"]["contact_points"], "contact_points")
    if not case["path"]["contact_points"]:
        raise ValueError("At least one candidate contact point is required")
    _number(case["path"]["clearance_mm"], "path.clearance_mm")
    if _number(case["rate_bias"], "rate_bias") <= -1:
        raise ValueError("rate_bias must preserve positive actual feed")
    for key in ("material_factor", "initial_wear_index"):
        _number(case[key], key, minimum=0)
    _positive(case["hardness_factor"], "hardness_factor")
    _positive(case["max_runtime_s"], "max_runtime_s")
    _integer(case["max_rework_passes"], "max_rework_passes", minimum=0)
    for stage in STAGES:
        _integer(
            case["stage_limits"][stage]["max_passes"],
            f"{stage}.max_passes",
            minimum=1,
        )
    if "drop_after_s" in case["force_sensor"]:
        _number(
            case["force_sensor"]["drop_after_s"],
            "force_sensor.drop_after_s",
            minimum=0,
        )
    settings = case["measurement"]
    _number(settings["sigma_mm"], "measurement.sigma_mm", minimum=0)
    _number(settings["bias_mm"], "measurement.bias_mm")
    _number(settings["uncertainty_mm"], "measurement.uncertainty_mm", minimum=0)
    _validate_points(settings["missing_points"], "measurement.missing_points")
    _integer(settings["missing_from_scan"], "measurement.missing_from_scan", 0)
    expected = case["expected_stop"]
    if expected is not None and expected not in RELEASE_STOPS | REJECTION_STOPS:
        raise ValueError(f"Unknown expected_stop: {expected}")
    if type(case["expected_false_release"]) is not bool:
        raise ValueError("expected_false_release must be a boolean")
    if not isinstance(case["tags"], list) or not all(
        isinstance(tag, str) and tag for tag in case["tags"]
    ):
        raise ValueError("Case tags must be a list of nonempty strings")


def validate_config(configuration):
    """Reject schema drift and an incomplete stress-case matrix."""
    if configuration.get("schema") != SCHEMA:
        raise ValueError("Unexpected process sandbox schema")
    if configuration.get("source_kind") != "synthetic_proxy_not_real_data":
        raise ValueError("The process sandbox requires synthetic inputs")
    if not configuration.get("seed") or not configuration.get(
        "parameter_sources"
    ):
        raise ValueError("Process seed and parameter provenance are required")
    _validate_parameters(configuration["model_parameters"])
    cases = configuration.get("cases")
    if not isinstance(cases, list) or len(cases) < 24:
        raise ValueError("At least 24 process cases are required")
    identifiers = set()
    for case in cases:
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", case_id)
            or case_id in identifiers
        ):
            raise ValueError(f"Invalid or duplicate case_id: {case_id}")
        identifiers.add(case_id)
        _validate_case(case)
    required_tags = {
        "initial_stock",
        "rate_bias",
        "force_dropout",
        "force_limit",
        "collision_proxy",
        "tool_wear",
        "thermal_proxy",
        "overgrind",
        "nonconvergence",
        "budget_stop",
        "rework_budget",
        "protected_zone",
        "measurement_missing",
        "measurement_bias",
    }
    observed_tags = {tag for case in cases for tag in case["tags"]}
    if not required_tags <= observed_tags:
        raise ValueError(
            f"Missing stress coverage: {sorted(required_tags - observed_tags)}"
        )


def initial_residual(case, point_count=POINT_COUNT):
    """Build a deterministic signed surface profile in millimetres."""
    settings = case["initial_mm"]
    residual = np.full(point_count, float(settings["base"]), dtype=float)
    spread = float(settings.get("spread", 0.0))
    if spread:
        residual += spread * np.sin(np.linspace(0.0, math.pi, point_count))
    protected = settings.get("protected_points", [])
    if protected:
        residual[list(protected)] = float(
            settings.get("protected_level_mm", 0.0)
        )
    if not np.isfinite(residual).all():
        raise ValueError("Initial residual surface contains nonfinite values")
    return residual


def measurement(residual, case, rng, scan_index=0):
    """Simulate a signed scan with separately declared instrument uncertainty."""
    settings = case["measurement"]
    sigma = settings["sigma_mm"]
    noise = np.clip(
        rng.normal(0.0, sigma, size=residual.shape), -3 * sigma, 3 * sigma
    )
    observed = residual + noise + settings["bias_mm"]
    if scan_index >= settings["missing_from_scan"]:
        observed[settings["missing_points"]] = np.nan
    return observed, settings["uncertainty_mm"]


def _engagement_ratio(parameters, profile):
    """Use the same bounded engagement formula for prediction and simulation."""
    worst = max(float(np.max(profile)), 0.05)
    ratio = (worst + 0.05) / parameters["reference_residual_mm"]
    return min(
        max(parameters["minimum_engagement_ratio"], ratio),
        parameters["maximum_engagement_ratio"],
    )


def force_proxy(case, parameters, stage, residual, commanded_force):
    """Return the synthetic sensor force at engagement, including overload."""
    return (
        commanded_force
        * _engagement_ratio(
            parameters, residual[case["path"]["contact_points"]]
        )
        * case["hardness_factor"]
        * parameters["stage_force_gain"][stage]
    )


def _stage_settings(parameters, stage, measured_residual, policy):
    """Select feed and force from the scan and nominal model coefficients."""
    limits = parameters["stage_limits"][stage]
    if policy == "fixed":
        return limits["fixed_feed_mm_s"], limits["fixed_force_n"]
    feed = limits["adaptive_feed_mm_s"]
    offset_key = (
        "roughing_exit_mm"
        if stage == "roughing"
        else "finishing_target_offset_mm"
    )
    target = max(0.0, float(np.max(measured_residual)) - parameters[offset_key])
    estimated_removal_per_n = (
        parameters["base_removal_mm_per_n_s"]
        * (PROCESS_LENGTH_MM / feed)
        * parameters["nominal_material_factor"]
        * _engagement_ratio(parameters, measured_residual)
        * parameters["stage_force_gain"][stage]
    )
    force = min(
        limits["maximum_force_n"],
        max(limits["minimum_force_n"], target / estimated_removal_per_n),
    )
    return feed, force


@dataclass
class _PlantState:
    """Synthetic physical state used by the simulator and the outcome audit."""

    residual: np.ndarray
    wear: float
    temperature: float = 0.0
    elapsed: float = 0.0
    energy: float = 0.0


def _thermal_limit_time(temperature, steady_temperature, parameters):
    limit = parameters["temperature_guard_k"]
    if temperature >= limit:
        return 0.0
    if steady_temperature <= limit:
        return math.inf
    return -parameters["thermal_tau_s"] * math.log1p(
        -(limit - temperature) / (steady_temperature - temperature)
    )


def _simulate_pass(case, parameters, state, command):
    """Integrate a uniform pass proxy until the first instrument guard fires."""
    stage = command["stage"]
    actual_feed = command["commanded_feed_mm_s"] * (1.0 + case["rate_bias"])
    full_duration = PROCESS_LENGTH_MM / actual_feed
    force = force_proxy(
        case,
        parameters,
        stage,
        state.residual,
        command["commanded_force_n"],
    )
    power = force * actual_feed / 1000.0
    tau = parameters["thermal_tau_s"]
    steady_temperature = power * parameters["temperature_rise_per_j_k"] * tau
    overload = force > parameters["maximum_force_n"]
    if overload:
        duration, stop = 0.0, "force_limit_engaged"
    else:
        # Faults at the end of a pass precede acceptance. Completing a pass
        # exactly at the time limit is allowed.
        duration, stop = min(
            (
                (
                    case["force_sensor"].get("drop_after_s", math.inf)
                    - state.elapsed,
                    "force_sensor_dropout",
                ),
                (
                    _thermal_limit_time(
                        state.temperature, steady_temperature, parameters
                    ),
                    "thermal_guard",
                ),
                (full_duration, None),
                (case["max_runtime_s"] - state.elapsed, "budget_stop"),
            ),
            key=lambda event: event[0],
        )

    tool_efficiency = 1.0 / (
        1.0 + parameters["wear_removal_penalty"] * state.wear
    )
    removal = (
        parameters["base_removal_mm_per_n_s"]
        * duration
        * force
        * case["material_factor"]
        * tool_efficiency
    )
    removed = np.zeros(POINT_COUNT)
    removed[case["path"]["contact_points"]] = removal
    state.residual -= removed
    state.wear += force * duration * parameters["wear_index_per_n_s"]
    state.energy += power * duration
    state.temperature += (steady_temperature - state.temperature) * (
        -math.expm1(-duration / tau)
    )
    state.elapsed += duration
    return {
        **command,
        "actual_feed_mm_s": actual_feed,
        "force_at_engagement_n": force,
        "measured_force_n": None if stop == "force_sensor_dropout" else force,
        "force_sensor_available": stop != "force_sensor_dropout",
        "force_limit_engaged": overload,
        "compliance_deflection_mm": force / parameters["compliance_n_per_mm"],
        "engaged_duration_s": duration,
        "pass_completed": duration == full_duration,
        "removed_depth_min_mm": float(np.min(removed)),
        "removed_depth_max_mm": float(np.max(removed)),
        "residual_min_mm": float(np.min(state.residual)),
        "residual_max_mm": float(np.max(state.residual)),
        "tool_wear_index": state.wear,
        "temperature_rise_k": state.temperature,
        "elapsed_s": state.elapsed,
        "mechanical_energy_j": state.energy,
        "stop_reason": stop or "continue",
    }, stop


def _accepted_interval(observed, uncertainty):
    return bool(
        np.all(observed - uncertainty >= -TOLERANCE_MM)
        and np.all(observed + uncertainty <= TOLERANCE_MM)
    )


def _scan_stop(observed, uncertainty, protected_points, initial):
    """Return a terminal release or review reason using scan inputs only."""
    if not np.isfinite(observed).all():
        return "measurement_missing"
    if protected_points and not _accepted_interval(
        observed[protected_points], uncertainty
    ):
        return "protected_zone_out_of_tolerance"
    if np.any(observed + uncertainty < -TOLERANCE_MM):
        return "initial_undercut" if initial else "undercut"
    if _accepted_interval(observed, uncertainty):
        return (
            "already_in_signed_tolerance"
            if initial
            else "signed_tolerance_reached"
        )
    # Ambiguity at the lower limit or absence of a positive violation gives
    # the controller no evidence that another uniform pass is appropriate.
    if (
        np.any(observed - uncertainty < -TOLERANCE_MM)
        or max(observed) <= TOLERANCE_MM
    ):
        return "measurement_uncertainty"
    return None


def _scan_record(scan_index, observed, uncertainty):
    return {
        "scan_index": scan_index,
        "signed_measurement_mm": [
            float(value) if np.isfinite(value) else None for value in observed
        ],
        "uncertainty_mm": uncertainty,
        "missing_points": np.flatnonzero(~np.isfinite(observed)).tolist(),
        "interval_conforming": _accepted_interval(observed, uncertainty),
    }


def _preflight(case, parameters, elapsed, command):
    if set(case["path"]["contact_points"]).intersection(
        case["initial_mm"]["protected_points"]
    ):
        return "protected_zone_intersection"
    if command["clearance_proxy_mm"] < parameters["minimum_clearance_mm"]:
        return "collision_proxy"
    if elapsed >= case["force_sensor"].get("drop_after_s", math.inf):
        return "force_sensor_dropout"
    if command["planned_duration_s"] > case["max_runtime_s"] - elapsed:
        return "budget_stop"
    return None


def run_policy(case, configuration, policy):
    """Run a policy with shared guards and signed sensor-only release."""
    if policy not in POLICIES:
        raise ValueError(f"Unknown process policy: {policy}")
    parameters = configuration["model_parameters"]
    seed_text = f"{configuration['seed']}:{case['case_id']}"
    seed = int.from_bytes(
        hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big"
    )
    rng = np.random.default_rng(seed)
    initial = initial_residual(case)
    state = _PlantState(initial.copy(), case["initial_wear_index"])
    observed, uncertainty = measurement(state.residual, case, rng)
    scans = [_scan_record(0, observed, uncertainty)]
    trajectory = []
    blocked_attempt = None
    passes = dict.fromkeys(STAGES, 0)
    stage = "roughing"
    protected = case["initial_mm"]["protected_points"]
    contact = case["path"]["contact_points"]

    while True:
        stop = _scan_stop(
            observed, uncertainty, protected, initial=len(scans) == 1
        )
        if stop:
            break
        if (
            np.max(observed[contact]) <= parameters["roughing_exit_mm"]
            or passes["roughing"]
            >= case["stage_limits"]["roughing"]["max_passes"]
        ):
            stage = "finishing"
        if passes[stage] >= case["stage_limits"][stage]["max_passes"]:
            stop = "pass_limit"
            break
        if stage == "finishing" and passes[stage] > case["max_rework_passes"]:
            stop = "rework_budget"
            break

        feed, force = _stage_settings(
            parameters, stage, observed[contact], policy
        )
        command = {
            "case_id": case["case_id"],
            "policy": policy,
            "pass": len(trajectory) + 1,
            "stage": stage,
            "commanded_feed_mm_s": feed,
            "commanded_force_n": force,
            "planned_duration_s": PROCESS_LENGTH_MM / feed,
            "clearance_proxy_mm": case["path"]["clearance_mm"],
            "input_scan_index": scans[-1]["scan_index"],
        }
        stop = _preflight(case, parameters, state.elapsed, command)
        if stop:
            blocked_attempt = {**command, "stop_reason": stop}
            break
        row, stop = _simulate_pass(case, parameters, state, command)
        if row["engaged_duration_s"] > 0:
            passes[stage] += 1
            scan_index = len(scans)
            observed, uncertainty = measurement(
                state.residual, case, rng, scan_index
            )
            scans.append(_scan_record(scan_index, observed, uncertainty))
        row["output_scan_index"] = scans[-1]["scan_index"]
        available = observed[np.isfinite(observed)]
        row.update(
            measured_residual_min_mm=float(available.min())
            if available.size
            else None,
            measured_residual_max_mm=float(available.max())
            if available.size
            else None,
            measurement_uncertainty_mm=uncertainty,
        )
        trajectory.append(row)
        if stop:
            break

    if trajectory and blocked_attempt is None:
        trajectory[-1]["stop_reason"] = stop
    released = stop in RELEASE_STOPS

    # The evaluator inspects latent residual only after the controller has
    # stopped. Its findings never override the reported controller decision.
    undercut_points = np.flatnonzero(state.residual < -TOLERANCE_MM).tolist()
    excess_points = np.flatnonzero(state.residual > TOLERANCE_MM).tolist()
    true_conforming = not (undercut_points or excess_points)
    false_release = released and not true_conforming
    return {
        "schema": RESULT_SCHEMA,
        "case_id": case["case_id"],
        "policy": policy,
        "parameters": parameters,
        "initial_residual_mm": initial.tolist(),
        "measurement_seed": seed,
        "scans": scans,
        "trajectory": trajectory,
        "blocked_attempt": blocked_attempt,
        "pass_counts": passes,
        "rework_pass_count": max(0, passes["finishing"] - 1),
        "stop_reason": stop,
        "failures": [] if released else [stop],
        "accepted_by_measurement": scans[-1]["interval_conforming"],
        "passed": released,
        "true_signed_conforming": true_conforming,
        "audit": {
            "false_release": false_release,
            "excess_points": excess_points,
            "undercut_points": undercut_points,
            "failures": ["measurement_acceptance_true_mismatch"]
            if false_release
            else [],
        },
        "residual_profile_mm": state.residual.tolist(),
        "worst_positive_residual_mm": float(np.max(state.residual)),
        "worst_negative_residual_mm": float(np.min(state.residual)),
        "elapsed_s": state.elapsed,
        "mechanical_energy_j": state.energy,
        "tool_wear_index": state.wear,
        "temperature_rise_k": state.temperature,
        "scope": "synthetic bounded proxy",
    }


def _contract_failures(case, adaptive, fixed):
    failures = []
    if (
        adaptive["initial_residual_mm"] != fixed["initial_residual_mm"]
        or adaptive["scans"][0] != fixed["scans"][0]
        or adaptive["measurement_seed"] != fixed["measurement_seed"]
    ):
        failures.append("paired initial state or scan stream differs")
    for result in (adaptive, fixed):
        policy = result["policy"]
        expected = case["expected_stop"]
        if expected is not None and result["stop_reason"] != expected:
            failures.append(
                f"{policy}: expected {expected}, got {result['stop_reason']}"
            )
        if result["audit"]["false_release"] != case["expected_false_release"]:
            failures.append(
                f"{policy}: false-release audit differs from contract"
            )
        if result["passed"] and (
            result["failures"] or not result["accepted_by_measurement"]
        ):
            failures.append(f"{policy}: release lacks a clear measured gate")
        if (
            result["elapsed_s"] > case["max_runtime_s"] + 1e-9
            or result["rework_pass_count"] > case["max_rework_passes"]
            or any(
                result["pass_counts"][stage]
                > case["stage_limits"][stage]["max_passes"]
                for stage in STAGES
            )
        ):
            failures.append(f"{policy}: process budget exceeded")
        if any(
            result["initial_residual_mm"][index]
            != result["residual_profile_mm"][index]
            for index in case["initial_mm"]["protected_points"]
        ):
            failures.append(f"{policy}: protected point was altered")
    return failures


def run_case(case, configuration):
    """Run paired adaptive and fixed policies on one synthetic case."""
    adaptive = run_policy(case, configuration, "adaptive")
    fixed = run_policy(case, configuration, "fixed")
    return {
        "case_id": case["case_id"],
        "case": case,
        "adaptive": adaptive,
        "fixed": fixed,
        "contract_failures": _contract_failures(case, adaptive, fixed),
        "comparison": {
            "adaptive_passed": adaptive["passed"],
            "fixed_passed": fixed["passed"],
            "adaptive_false_release": adaptive["audit"]["false_release"],
            "fixed_false_release": fixed["audit"]["false_release"],
            "adaptive_advantage": adaptive["passed"] and not fixed["passed"],
            "fixed_advantage": fixed["passed"] and not adaptive["passed"],
            "pass_count_delta": sum(adaptive["pass_counts"].values())
            - sum(fixed["pass_counts"].values()),
            "elapsed_delta_s": round(
                adaptive["elapsed_s"] - fixed["elapsed_s"], 6
            ),
            "energy_delta_j": round(
                adaptive["mechanical_energy_j"] - fixed["mechanical_energy_j"],
                6,
            ),
            "worst_positive_residual_delta_mm": round(
                adaptive["worst_positive_residual_mm"]
                - fixed["worst_positive_residual_mm"],
                6,
            ),
            "worst_negative_residual_delta_mm": round(
                adaptive["worst_negative_residual_mm"]
                - fixed["worst_negative_residual_mm"],
                6,
            ),
        },
    }


def run_suite(configuration, case_ids=None):
    """Run all cases, or an explicit named subset for focused tests."""
    validate_config(configuration)
    cases = configuration["cases"]
    if case_ids is not None:
        case_ids = tuple(case_ids)
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError(
                "Select at least one case and use each case ID once"
            )
        indexed = {case["case_id"]: case for case in cases}
        missing = set(case_ids) - indexed.keys()
        if missing:
            raise ValueError(f"Unknown process cases: {sorted(missing)}")
        cases = [indexed[name] for name in case_ids]
    return [run_case(case, configuration) for case in cases]


def _policy_summary(records):
    releases = sum(record["passed"] for record in records)
    false_releases = sum(record["audit"]["false_release"] for record in records)
    return {
        "pass_count": releases,
        "conforming_release_count": releases - false_releases,
        "false_release_count": false_releases,
        "rejection_count": len(records) - releases,
        "true_excess_case_count": sum(
            bool(r["audit"]["excess_points"]) for r in records
        ),
        "true_undercut_case_count": sum(
            bool(r["audit"]["undercut_points"]) for r in records
        ),
        "residual_extrema_mm": [
            min(r["worst_negative_residual_mm"] for r in records),
            max(r["worst_positive_residual_mm"] for r in records),
        ],
        "mean_elapsed_s": round(
            float(np.mean([r["elapsed_s"] for r in records])), 6
        ),
        "mean_energy_j": round(
            float(np.mean([r["mechanical_energy_j"] for r in records])), 6
        ),
        "mean_passes": round(
            float(np.mean([sum(r["pass_counts"].values()) for r in records])), 6
        ),
        "stop_counts": dict(
            sorted(Counter(r["stop_reason"] for r in records).items())
        ),
        "failure_counts": dict(
            sorted(
                Counter(
                    failure for r in records for failure in r["failures"]
                ).items()
            )
        ),
    }


def _summary(results):
    return {
        "case_count": len(results),
        "policy_run_count": 2 * len(results),
        **{
            policy: _policy_summary([result[policy] for result in results])
            for policy in POLICIES
        },
        "adaptive_advantage_count": sum(
            result["comparison"]["adaptive_advantage"] for result in results
        ),
        "fixed_advantage_count": sum(
            result["comparison"]["fixed_advantage"] for result in results
        ),
        "both_released_count": sum(
            result["adaptive"]["passed"] and result["fixed"]["passed"]
            for result in results
        ),
        "neither_released_count": sum(
            not (result["adaptive"]["passed"] or result["fixed"]["passed"])
            for result in results
        ),
        "unexpected_failure_count": sum(
            len(result["contract_failures"]) for result in results
        ),
        "tag_counts": dict(
            sorted(
                Counter(
                    tag for result in results for tag in result["case"]["tags"]
                ).items()
            )
        ),
    }


def calibration_plan():
    """Describe the measured fields needed to replace synthetic assumptions."""
    return {
        "schema": "daiyi-process-sandbox-calibration-v1",
        "real_data_pending": True,
        "required_measured_fields": {
            "surface": [
                "part_id",
                "serial",
                "cad_revision",
                "fixture_revision",
                "point_id",
                "before_mm",
                "after_mm",
                "measurement_uncertainty_mm",
                "measurement_device_id",
                "protected_zone",
            ],
            "process": [
                "timestamp_s",
                "commanded_feed_mm_s",
                "measured_actual_feed_mm_s",
                "commanded_normal_force_n",
                "measured_normal_force_n",
                "tool_id",
                "tool_diameter_mm",
                "tool_wear_measurement",
                "tool_temperature_k",
                "ambient_temperature_k",
            ],
            "removal": [
                "before_surface_mm",
                "after_surface_mm",
                "removed_volume_mm3_or_mass_g",
                "material_density_g_cm3",
                "surface_roughness_ra_um_or_sa_um",
            ],
        },
        "identifiable_with_design": {
            "base_removal_mm_per_n_s": "factorial force x feed x repeat passes",
            "force_gain": "instrumented normal force versus command and stock",
            "compliance_n_per_mm": "known load versus deflection sweep",
            "wear_index_per_n_s": "paired force-energy and tool metrology blocks",
            "temperature_rise_per_j_k_and_tau_s": "instrumented temperature time series",
            "measurement_sigma_and_bias": "repeated calibrated signed measurements",
        },
        "not_identifiable_from_current_swept_geometry": [
            "normal force",
            "material removal rate",
            "tool wear",
            "workpiece temperature",
            "surface roughness",
            "fixture compliance",
            "signed surface acceptance",
        ],
        "experiment_design": [
            "Randomize a 3x3x3 grid of normal force, feed and repeated passes within the bounded recipe.",
            "Run at least three fresh tools with tool metrology before, during and after each wear block.",
            "Separately measure fixture/tool-stack deflection under static loads without material removal.",
            "Record temperature at the tool and nominal surface and retain a cool-down time series.",
            "Measure the same signed points before and after every pass with an independent device.",
            "Hold out complete parts and tools for confirmation, not merely randomized point rows.",
        ],
        "replacement_interface": {
            "model_parameters": "config/process_sandbox_20261123.json:model_parameters",
            "case_stressors": "config/process_sandbox_20261123.json:cases",
            "python_api": [
                "daiyi_grinding.process_sandbox.load_config",
                "daiyi_grinding.process_sandbox.run_suite",
                "daiyi_grinding.process_sandbox.write_suite_outputs",
            ],
        },
    }


TRAJECTORY_FIELDS = (
    "case_id",
    "policy",
    "pass",
    "stage",
    "input_scan_index",
    "output_scan_index",
    "clearance_proxy_mm",
    "commanded_feed_mm_s",
    "actual_feed_mm_s",
    "commanded_force_n",
    "force_at_engagement_n",
    "measured_force_n",
    "force_sensor_available",
    "force_limit_engaged",
    "compliance_deflection_mm",
    "planned_duration_s",
    "engaged_duration_s",
    "pass_completed",
    "removed_depth_min_mm",
    "removed_depth_max_mm",
    "residual_min_mm",
    "residual_max_mm",
    "measured_residual_min_mm",
    "measured_residual_max_mm",
    "measurement_uncertainty_mm",
    "tool_wear_index",
    "temperature_rise_k",
    "elapsed_s",
    "mechanical_energy_j",
    "stop_reason",
)

COMPARISON_FIELDS = (
    "case_id",
    "adaptive_passed",
    "fixed_passed",
    "adaptive_false_release",
    "fixed_false_release",
    "adaptive_advantage",
    "fixed_advantage",
    "pass_count_delta",
    "elapsed_delta_s",
    "energy_delta_j",
    "worst_positive_residual_delta_mm",
    "worst_negative_residual_delta_mm",
    "adaptive_failures",
    "fixed_failures",
    "contract_failures",
)


def _write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _artifact(path, base):
    path = Path(path)
    return {
        "path": str(path.relative_to(base)),
        "sha256": sha256(path),
    }


def _source_input(path, role, assumption):
    resolved = Path(path).resolve()
    return {
        "role": role,
        "path": str(resolved),
        "sha256": sha256(resolved),
        "assumption_origin": assumption,
    }


def write_suite_outputs(
    results, configuration, output, command, *, config_path=CONFIG_FILE
):
    """Write reproducible case, comparison, calibration and evidence outputs."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    case_directory = output / "cases"
    case_directory.mkdir()
    artifact_paths = []

    for result in results:
        path = case_directory / f"{result['case_id']}.json"
        _write_json(path, result)
        artifact_paths.append(path)

    trajectory_path = output / "process_trajectories.csv"
    with trajectory_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRAJECTORY_FIELDS)
        writer.writeheader()
        for result in results:
            for policy in POLICIES:
                for row in result[policy]["trajectory"]:
                    writer.writerow(row)
    artifact_paths.append(trajectory_path)

    comparison_path = output / "case_comparison.csv"
    with comparison_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COMPARISON_FIELDS)
        writer.writeheader()
        for result in results:
            comparison = result["comparison"]
            writer.writerow(
                {
                    "case_id": result["case_id"],
                    **comparison,
                    "adaptive_failures": ";".join(
                        result["adaptive"]["failures"]
                    ),
                    "fixed_failures": ";".join(result["fixed"]["failures"]),
                    "contract_failures": ";".join(result["contract_failures"]),
                }
            )
    artifact_paths.append(comparison_path)

    summary_path = output / "summary.json"
    summary = _summary(results)
    _write_json(summary_path, summary)
    artifact_paths.append(summary_path)

    calibration_path = output / "calibration_plan.json"
    _write_json(calibration_path, calibration_plan())
    artifact_paths.append(calibration_path)

    resolved_config_path = output / "resolved_config.json"
    _write_json(resolved_config_path, configuration)
    artifact_paths.append(resolved_config_path)

    source_paths = (
        (
            Path(config_path).resolve(),
            "process_sandbox_config",
            "synthetic assumptions and stressors",
        ),
        (__file__, "process_sandbox_model", "synthetic model implementation"),
        (
            "docs/workstation_full_chain_20261122.md",
            "main_chain_boundary",
            "same-point signed measurements and protected zone convention",
        ),
        (
            "docs/team_data_reuse_20261121.md",
            "team_synthetic_priors",
            "Yang's synthetic stock scale and Cao's compact bumps and depth noise",
        ),
        (
            "feishu_docs/11.12代码结构、模块功能变化__wiki-H5fxwWJ3Yi5sHVkPRYTcYWc8nCb.md",
            "senior_1112",
            "documented precontact clearance and absent process calibration",
        ),
        (
            "config/enterprise_requirements_20261123.json",
            "enterprise_target",
            "signed ±0.3 mm vision target and coverage convention",
        ),
    )
    inputs = [
        _source_input(project.ROOT / path, role, assumption)
        for path, role, assumption in source_paths
    ]
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "task": "enterprise offline process-control sandbox without real grinding data",
        "scope": {
            "offline_only": True,
            "robot_commands": False,
            "network": False,
            "mujoco": False,
            "torch": False,
            "main_chain_geometric_removal_modified": False,
            "process_model": "synthetic bounded proxy",
            "truth_for_control_or_release": False,
            "pass_count_meaning": "controller releases based on measurement and guards",
            "signed_surface_target_mm": [-TOLERANCE_MM, TOLERANCE_MM],
            "r_and_d_roles": list(R_AND_D_ROLES),
        },
        "commands": [command],
        "working_directory": str(Path.cwd()),
        "inputs": inputs,
        "metrics": summary,
        "failures": {
            "process_rejections": [
                {
                    "case_id": result["case_id"],
                    "policy": policy,
                    "stop_reason": result[policy]["stop_reason"],
                    "expected_stop": result["case"]["expected_stop"],
                }
                for result in results
                for policy in POLICIES
                if not result[policy]["passed"]
            ],
            "false_releases": [
                {
                    "case_id": result["case_id"],
                    "policy": policy,
                    "expected_fault": result["case"]["expected_false_release"],
                    **result[policy]["audit"],
                }
                for result in results
                for policy in POLICIES
                if result[policy]["audit"]["false_release"]
            ],
            "unexpected_suite_failures": [
                {
                    "case_id": result["case_id"],
                    "failures": result["contract_failures"],
                }
                for result in results
                if result["contract_failures"]
            ],
        },
        "artifacts": [_artifact(path, output) for path in artifact_paths],
        "real_data_pending": [
            "signed before/after surface measurements on the same points",
            "instrumented normal force and actual feed",
            "fixture/tool-stack compliance calibration",
            "tool wear metrology",
            "material removal mass or volume",
            "tool and surface temperature",
            "surface roughness measurements",
            "ESTUN controller process receipts",
        ],
    }
    evidence_path = output / "evidence.json"
    _write_json(evidence_path, evidence)
    return evidence


def _output_path(value):
    root = OUTPUT_ROOT.resolve()
    requested = Path(value)
    if not requested.is_absolute() and len(requested.parts) == 1:
        requested = root / requested
    output = requested.resolve()
    if output == root or not output.is_relative_to(root):
        raise ValueError(f"--output must name a fresh directory under {root}")
    return output


def _command(arguments):
    thread_limits = [
        f"{name}={os.environ[name]}"
        for name in (
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
        )
        if name in os.environ
    ]
    launcher = [sys.executable, "-m", "daiyi_grinding.process_sandbox"]
    return shlex.join(
        (["env", *thread_limits] if thread_limits else [])
        + launcher
        + arguments
    )


def main(argv=None):
    """Run a low-resource offline process-control suite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(CONFIG_FILE),
        help="process sandbox JSON configuration",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="new run name or path under runs/enterprise_offline_20261123/process",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        default=None,
        help="optional explicit case IDs; default runs the full suite",
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    output = _output_path(args.output)
    config_path = Path(args.config).resolve()
    configuration = load_config(config_path)
    results = run_suite(configuration, args.cases)
    evidence = write_suite_outputs(
        results,
        configuration,
        output,
        _command(arguments),
        config_path=config_path,
    )
    metrics = evidence["metrics"]
    print(
        json.dumps(
            {
                "output": str(output),
                "cases": metrics["case_count"],
                "adaptive_releases": metrics["adaptive"]["pass_count"],
                "fixed_releases": metrics["fixed"]["pass_count"],
                "adaptive_conforming_releases": metrics["adaptive"][
                    "conforming_release_count"
                ],
                "fixed_conforming_releases": metrics["fixed"][
                    "conforming_release_count"
                ],
                "false_releases": sum(
                    metrics[policy]["false_release_count"]
                    for policy in POLICIES
                ),
                "unexpected_failures": metrics["unexpected_failure_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(metrics["unexpected_failure_count"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
