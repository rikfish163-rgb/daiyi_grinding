"""Plant-disturbance adaptation, online rate identification and debiased checks.

The module has two independent halves:

* ``ProcessPlantProxy`` is a hidden synthetic plant with a removal-rate scale
  between 0.4 and 1.8, belt wear, force/temperature limits and a multi-pass
  staging policy. Only ``AnchorObservation`` objects cross into the controller.
* ``LocalBaselineReinspector`` removes a locally estimated systematic bias
  using untouched normal-surface anchors on both sides of the machined band
  before forming calibrated signed intervals.

Both halves expose adapters matching the P0-A session protocols
(``ProcessAdapter`` and ``ReinspectionCalibrator``). No controller or decision
record reads generative truth; ``truth_used_for_control`` and
``truth_used_for_decision`` stay ``False``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import project

SCHEMA = "daiyi-adaptive-process-v1"
OUTPUT_ROOT = project.RUNS / "enterprise_offline_20261123/p1_data_process_01"
TOLERANCE_MM = 0.3
STAGES = ("roughing", "finishing")
MACHINED = "machined"
BASELINE_LEFT = "baseline_left"
BASELINE_RIGHT = "baseline_right"
PROTECTED = "protected"
BASELINE_ROLES = (BASELINE_LEFT, BASELINE_RIGHT)
SESSION_OVERRIDE_KEYS = (
    "feed_scale",
    "normal_offset_mm",
    "passes",
    "dwell_scale",
)


def write_json(path, value):
    """Write finite JSON with a trailing newline."""
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_file(path):
    """Return the SHA-256 digest of one local file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite(value, name, minimum=None, maximum=None):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        bounds = []
        if minimum is not None:
            bounds.append(f">= {minimum}")
        if maximum is not None:
            bounds.append(f"<= {maximum}")
        suffix = f" ({', '.join(bounds)})" if bounds else ""
        raise ValueError(f"{name} must be finite{suffix}")
    return float(value)


@dataclass(frozen=True)
class ProcessLimits:
    """Public process envelope and nominal model coefficients."""

    tolerance_mm: float = TOLERANCE_MM
    finish_target_mm: float = 0.02
    roughing_allowance_mm: float = 0.15
    max_cut_roughing_mm: float = 0.45
    max_cut_finishing_mm: float = 0.15
    max_roughing_passes: int = 7
    max_finishing_passes: int = 3
    min_force_n: float = 8.0
    max_force_n: float = 90.0
    roughing_force_n: float = 60.0
    finishing_force_n: float = 30.0
    min_engaged_seconds: float = 0.05
    max_engaged_seconds: float = 6.0
    max_total_seconds: float = 60.0
    max_temperature_k: float = 12.0
    max_normal_offset_mm: float = 0.20
    max_allowance_mm: float = 0.30
    default_segment_length_mm: float = 60.0
    nominal_feed_mm_s: float = 30.0
    feed_scale_bounds: tuple = (0.35, 1.75)
    nominal_removal_mm_per_n_s: float = 0.0045
    prior_relative_sigma: float = 0.45
    process_noise_relative: float = 0.03
    min_snr: float = 1.5
    relative_rate_bounds: tuple = (0.2, 2.5)

    def __post_init__(self):
        if not 0.0 < self.tolerance_mm <= TOLERANCE_MM:
            raise ValueError(
                "tolerance_mm must be positive and may not exceed 0.3 mm"
            )
        for name in (
            "finish_target_mm",
            "roughing_allowance_mm",
            "max_cut_roughing_mm",
            "max_cut_finishing_mm",
            "min_force_n",
            "max_force_n",
            "roughing_force_n",
            "finishing_force_n",
            "min_engaged_seconds",
            "max_engaged_seconds",
            "max_total_seconds",
            "max_temperature_k",
            "max_normal_offset_mm",
            "max_allowance_mm",
            "default_segment_length_mm",
            "nominal_feed_mm_s",
            "nominal_removal_mm_per_n_s",
            "prior_relative_sigma",
            "process_noise_relative",
            "min_snr",
        ):
            _finite(getattr(self, name), name, minimum=0.0)
        if self.finish_target_mm >= self.roughing_allowance_mm:
            raise ValueError(
                "Finish target must be below the roughing allowance"
            )
        if not (
            self.min_force_n
            <= self.finishing_force_n
            <= self.roughing_force_n
            <= self.max_force_n
        ):
            raise ValueError("Stage forces must lie inside the force envelope")
        if len(self.feed_scale_bounds) != 2 or not (
            0.0 < self.feed_scale_bounds[0] < self.feed_scale_bounds[1]
        ):
            raise ValueError("feed_scale_bounds must be an increasing pair")
        if len(self.relative_rate_bounds) != 2 or not (
            0.0 < self.relative_rate_bounds[0] < self.relative_rate_bounds[1]
        ):
            raise ValueError("relative_rate_bounds must be an increasing pair")
        if type(self.max_roughing_passes) is not int or not (
            1 <= self.max_roughing_passes <= 40
        ):
            raise ValueError("max_roughing_passes must be an integer in 1–40")
        if type(self.max_finishing_passes) is not int or not (
            1 <= self.max_finishing_passes <= 20
        ):
            raise ValueError("max_finishing_passes must be an integer in 1–20")


@dataclass(frozen=True)
class PlantParameters:
    """Hidden plant truth used only by the proxy and the post-hoc audit."""

    base_removal_mm_per_n_s: float = 0.0045
    rate_scale: float = 1.0
    material_factor: float = 1.0
    feed_bias: float = 0.0
    wear_index_per_n_s: float = 0.02
    wear_penalty: float = 0.03
    compliance_mm_per_n: float = 1.0 / 1800.0
    temperature_rise_per_j_k: float = 0.8
    thermal_tau_s: float = 25.0
    initial_wear: float = 0.0
    measurement_sigma_mm: float = 0.02
    measurement_bias_mm: float = 0.0
    measurement_sigma_gain_mm: float = 0.0

    def __post_init__(self):
        for name in (
            "base_removal_mm_per_n_s",
            "rate_scale",
            "material_factor",
            "wear_index_per_n_s",
            "wear_penalty",
            "compliance_mm_per_n",
            "temperature_rise_per_j_k",
            "thermal_tau_s",
            "initial_wear",
            "measurement_sigma_mm",
            "measurement_sigma_gain_mm",
        ):
            _finite(getattr(self, name), name, minimum=0.0)
        if self.base_removal_mm_per_n_s <= 0:
            raise ValueError("base_removal_mm_per_n_s must be positive")
        if not 0.2 <= self.rate_scale <= 2.5:
            raise ValueError("rate_scale must lie in 0.2–2.5")
        _finite(self.measurement_bias_mm, "measurement_bias_mm")


@dataclass(frozen=True)
class AnchorObservation:
    """Signed residuals available to the controller; never latent truth."""

    heights_mm: np.ndarray
    sigma_mm: np.ndarray
    roles: np.ndarray
    points_m: np.ndarray | None = None
    normals_m: np.ndarray | None = None
    scan_index: int = 0

    def __post_init__(self):
        heights = np.asarray(self.heights_mm, dtype=float).reshape(-1)
        sigma = np.asarray(self.sigma_mm, dtype=float).reshape(-1)
        roles = np.asarray(self.roles).reshape(-1)
        if not (
            len(heights) == len(sigma) == len(roles)
            and len(heights)
            and np.isfinite(heights).all()
            and np.isfinite(sigma).all()
            and (sigma > 0).all()
            and set(roles)
            <= {MACHINED, BASELINE_LEFT, BASELINE_RIGHT, PROTECTED}
        ):
            raise ValueError("Anchor observation arrays are inconsistent")
        object.__setattr__(self, "heights_mm", heights)
        object.__setattr__(self, "sigma_mm", sigma)
        object.__setattr__(self, "roles", roles)

    def select(self, *roles):
        """Return the subset for the requested roles."""
        selected = np.isin(self.roles, roles)
        return AnchorObservation(
            heights_mm=self.heights_mm[selected],
            sigma_mm=self.sigma_mm[selected],
            roles=self.roles[selected],
            points_m=None
            if self.points_m is None
            else np.asarray(self.points_m)[selected],
            normals_m=None
            if self.normals_m is None
            else np.asarray(self.normals_m)[selected],
            scan_index=self.scan_index,
        )

    def record(self):
        return {
            "anchors": int(len(self.heights_mm)),
            "roles": {
                role: int(np.sum(self.roles == role))
                for role in (MACHINED, *BASELINE_ROLES, PROTECTED)
            },
            "heights_mm": self.heights_mm.tolist(),
            "sigma_mm": self.sigma_mm.tolist(),
            "scan_index": int(self.scan_index),
        }


@dataclass(frozen=True)
class PassExecution:
    """A hidden-plant pass result, retained for the post-hoc audit only."""

    stage: str
    force_n: float
    feed_mm_s: float
    duration_s: float
    actual_feed_mm_s: float
    removed_mean_mm: float
    tool_efficiency: float
    wear_index: float
    temperature_k: float
    compliance_deflection_mm: float
    energy_j: float
    elapsed_s: float
    guard: str | None

    def record(self):
        return {
            "stage": self.stage,
            "force_n": self.force_n,
            "feed_mm_s": self.feed_mm_s,
            "duration_s": self.duration_s,
            "actual_feed_mm_s": self.actual_feed_mm_s,
            "removed_mean_mm": self.removed_mean_mm,
            "tool_efficiency": self.tool_efficiency,
            "wear_index": self.wear_index,
            "temperature_k": self.temperature_k,
            "compliance_deflection_mm": self.compliance_deflection_mm,
            "energy_j": self.energy_j,
            "elapsed_s": self.elapsed_s,
            "guard": self.guard,
        }


class ProcessPlantProxy:
    """Hidden, observation-only process plant for offline adaptation tests."""

    def __init__(self, parameters, residual_mm, roles, rng=None):
        self.parameters = parameters
        self.residual_mm = np.asarray(residual_mm, dtype=float).copy()
        self.roles = np.asarray(roles).reshape(-1)
        if not (
            len(self.residual_mm) == len(self.roles)
            and len(self.residual_mm)
            and np.isfinite(self.residual_mm).all()
            and set(self.roles) <= {MACHINED, *BASELINE_ROLES, PROTECTED}
            and (self.roles == MACHINED).any()
        ):
            raise ValueError("Plant anchors and roles are inconsistent")
        self.machined = self.roles == MACHINED
        self.wear = float(parameters.initial_wear)
        self.temperature_k = 0.0
        self.elapsed_s = 0.0
        self.energy_j = 0.0
        self.rng = rng if rng is not None else np.random.default_rng(0)
        self.history = []

    def command(self, stage, force_n, feed_mm_s, duration_s):
        """Execute one uniform pass and return the hidden-plant result."""
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        force = _finite(force_n, "force_n", minimum=0.0)
        feed = _finite(feed_mm_s, "feed_mm_s", minimum=0.0)
        duration = _finite(duration_s, "duration_s", minimum=0.0)
        if force > 90.0:
            raise ValueError("force_n exceeds the shared 90 N envelope")
        guard = None
        executed = duration
        if executed and self.elapsed_s + executed > 600.0:
            executed, guard = max(600.0 - self.elapsed_s, 0.0), "budget_stop"
        actual_feed = feed * (1.0 + self.parameters.feed_bias)
        efficiency = 1.0 / (1.0 + self.parameters.wear_penalty * self.wear)
        removal = (
            (
                self.parameters.base_removal_mm_per_n_s
                * force
                * executed
                * self.parameters.material_factor
                * self.parameters.rate_scale
                * efficiency
            )
            if executed
            else 0.0
        )
        power = force * actual_feed / 1000.0
        tau = self.parameters.thermal_tau_s
        steady = power * self.parameters.temperature_rise_per_j_k * tau
        self.temperature_k += (
            (steady - self.temperature_k) * (-math.expm1(-executed / tau))
            if executed
            else 0.0
        )
        if executed and self.temperature_k > 12.0 and guard is None:
            guard = "thermal_guard"
        self.residual_mm[self.machined] -= removal
        self.wear += force * executed * self.parameters.wear_index_per_n_s
        self.energy_j += power * executed
        self.elapsed_s += executed
        result = PassExecution(
            stage=stage,
            force_n=force,
            feed_mm_s=feed,
            duration_s=executed,
            actual_feed_mm_s=actual_feed,
            removed_mean_mm=float(removal),
            tool_efficiency=float(efficiency),
            wear_index=float(self.wear),
            temperature_k=float(self.temperature_k),
            compliance_deflection_mm=float(
                force * self.parameters.compliance_mm_per_n
            ),
            energy_j=float(self.energy_j),
            elapsed_s=float(self.elapsed_s),
            guard=guard,
        )
        self.history.append(result)
        return result

    def observe(self, scan_index=0):
        """Return a noisy signed observation with the hidden local bias."""
        sigma = self.parameters.measurement_sigma_mm + (
            self.parameters.measurement_sigma_gain_mm
            * np.abs(np.arange(len(self.residual_mm)) % 5)
        )
        noise = self.rng.normal(0.0, sigma)
        observed = (
            self.residual_mm + noise + self.parameters.measurement_bias_mm
        )
        return AnchorObservation(
            heights_mm=observed,
            sigma_mm=np.maximum(sigma, 1e-6),
            roles=self.roles,
            scan_index=scan_index,
        )

    def truth(self):
        """Return the latent residual and process state for the audit only."""
        return {
            "residual_mm": self.residual_mm.tolist(),
            "machined_min_mm": float(self.residual_mm[self.machined].min()),
            "machined_max_mm": float(self.residual_mm[self.machined].max()),
            "wear_index": float(self.wear),
            "temperature_k": float(self.temperature_k),
            "elapsed_s": float(self.elapsed_s),
            "energy_j": float(self.energy_j),
            "truth_used_for_control": False,
        }


@dataclass(frozen=True)
class RateEstimate:
    """Bounded relative removal-rate estimate and its uncertainty."""

    relative_rate: float
    relative_sigma: float
    lower: float
    upper: float
    updates: int
    last_snr: float | None
    bounded: bool
    low_snr_freezes: int
    duplicate_updates_ignored: int
    invalid_updates_ignored: int
    nonpositive_removal_freezes: int
    truth_used_for_control: bool = False

    def record(self):
        return {
            "relative_rate": self.relative_rate,
            "relative_sigma": self.relative_sigma,
            "lower_95": self.lower,
            "upper_95": self.upper,
            "updates": self.updates,
            "last_snr": self.last_snr,
            "bounded": self.bounded,
            "low_snr_freezes": self.low_snr_freezes,
            "duplicate_updates_ignored": self.duplicate_updates_ignored,
            "invalid_updates_ignored": self.invalid_updates_ignored,
            "nonpositive_removal_freezes": self.nonpositive_removal_freezes,
            "truth_used_for_control": False,
        }


@dataclass(frozen=True)
class StagedPass:
    """One planned roughing or finishing pass."""

    stage: str
    target_residual_mm: float
    planned_cut_mm: float
    force_n: float
    note: str

    def record(self):
        return {
            "stage": self.stage,
            "target_residual_mm": self.target_residual_mm,
            "planned_cut_mm": self.planned_cut_mm,
            "force_n": self.force_n,
            "note": self.note,
            "truth_used_for_control": False,
        }


@dataclass(frozen=True)
class StagingPlan:
    """Multi-pass plan from measured stock to the nominal surface."""

    passes: tuple
    status: str
    reason: str
    measured_peak_mm: float
    measured_min_mm: float
    roughing_allowance_mm: float
    finish_target_mm: float
    planned_total_cut_mm: float
    truth_used_for_control: bool = False

    def record(self):
        return {
            "passes": [item.record() for item in self.passes],
            "status": self.status,
            "reason": self.reason,
            "measured_peak_mm": self.measured_peak_mm,
            "measured_min_mm": self.measured_min_mm,
            "roughing_allowance_mm": self.roughing_allowance_mm,
            "finish_target_mm": self.finish_target_mm,
            "planned_total_cut_mm": self.planned_total_cut_mm,
            "truth_used_for_control": False,
        }


def segment_process_context(segment, plan):
    """Map a staging plan onto a P0-A/P0-B segment without changing identity.

    ``region_id``, ``grasp_setup`` and ``split_from`` are copied unchanged so
    the caller can attach the process metadata to its own multi-grip structure.
    """
    if not isinstance(segment, dict):
        raise ValueError("segment must be a mapping")
    if not isinstance(plan, StagingPlan):
        raise ValueError("plan must be a StagingPlan")
    first = plan.passes[0] if plan.passes else None
    context = {
        key: segment[key]
        for key in ("region_id", "grasp_setup", "split_from", "workpiece_id")
        if key in segment
    }
    context.update(
        stage=None if first is None else first.stage,
        planned_cut_mm=0.0 if first is None else first.planned_cut_mm,
        target_residual_mm=None if first is None else first.target_residual_mm,
        planned_passes=len(plan.passes),
        plan_status=plan.status,
        plan_reason=plan.reason,
        truth_used_for_control=False,
    )
    return context


@dataclass(frozen=True)
class EngagementPlan:
    """One controller action with its predicted interval and guards."""

    stage: str
    force_n: float
    feed_mm_s: float
    duration_s: float
    feed_scale: float
    normal_offset_mm: float
    passes: int
    desired_cut_mm: float
    predicted_removal_mm: float
    predicted_lower_mm: float
    predicted_upper_mm: float
    remaining_mm: float
    offset_limited_mm: float
    stop_reason: str | None
    rate: RateEstimate
    truth_used_for_control: bool = False

    def record(self):
        return {
            "stage": self.stage,
            "force_n": self.force_n,
            "feed_mm_s": self.feed_mm_s,
            "duration_s": self.duration_s,
            "feed_scale": self.feed_scale,
            "normal_offset_mm": self.normal_offset_mm,
            "passes": self.passes,
            "desired_cut_mm": self.desired_cut_mm,
            "predicted_removal_mm": self.predicted_removal_mm,
            "predicted_lower_mm": self.predicted_lower_mm,
            "predicted_upper_mm": self.predicted_upper_mm,
            "remaining_mm": self.remaining_mm,
            "offset_limited_mm": self.offset_limited_mm,
            "stop_reason": self.stop_reason,
            "rate": self.rate.record(),
            "truth_used_for_control": False,
        }


class AdaptiveProcessController:
    """Bound the removal-rate gain from observations and stage the passes.

    The controller updates a scalar relative removal gain with a bounded
    recursive least-squares step. Durations use the upper confidence bound so
    a faster-than-estimated plant under-removes instead of overgrinding. Low
    signal-to-noise updates freeze the estimate; negative observed removal or
    a measured overgrind produces a stop rather than a larger gain.
    """

    def __init__(
        self,
        limits=ProcessLimits(),
        *,
        relative_rate=1.0,
        relative_sigma=None,
        process_noise=None,
    ):
        self.limits = limits
        self._relative_rate = _finite(
            relative_rate,
            "relative_rate",
            minimum=limits.relative_rate_bounds[0],
            maximum=limits.relative_rate_bounds[1],
        )
        self._variance = (
            _finite(
                relative_sigma
                if relative_sigma is not None
                else limits.prior_relative_sigma,
                "relative_sigma",
                minimum=1e-4,
            )
            ** 2
        )
        self._process_noise = _finite(
            process_noise
            if process_noise is not None
            else limits.process_noise_relative,
            "process_noise",
            minimum=0.0,
        )
        self._updates = 0
        self._last_snr = None
        self._bounded = False
        self._low_snr_freezes = 0
        self._consumed_pairs = set()
        self._duplicate_updates_ignored = 0
        self._invalid_updates_ignored = 0
        self._nonpositive_removal_freezes = 0
        self.history = []
        self.last_decision = None

    @property
    def estimate(self):
        sigma = math.sqrt(max(self._variance, 0.0))
        lower = max(
            self._relative_rate - 1.96 * sigma,
            self.limits.relative_rate_bounds[0],
        )
        upper = min(
            self._relative_rate + 1.96 * sigma,
            self.limits.relative_rate_bounds[1],
        )
        return RateEstimate(
            relative_rate=float(self._relative_rate),
            relative_sigma=float(sigma),
            lower=float(lower),
            upper=float(upper),
            updates=self._updates,
            last_snr=None if self._last_snr is None else float(self._last_snr),
            bounded=self._bounded,
            low_snr_freezes=self._low_snr_freezes,
            duplicate_updates_ignored=self._duplicate_updates_ignored,
            invalid_updates_ignored=self._invalid_updates_ignored,
            nonpositive_removal_freezes=self._nonpositive_removal_freezes,
        )

    def update_from_observation(
        self, before, after, *, engaged_seconds, force_n
    ):
        """Update the relative gain from matched before/after observations."""
        if not isinstance(before, AnchorObservation) or not isinstance(
            after, AnchorObservation
        ):
            raise ValueError("Rate identification requires two observations")
        if len(before.heights_mm) != len(
            after.heights_mm
        ) or not np.array_equal(before.roles, after.roles):
            raise ValueError("Before/after anchor sets must match")
        if (
            type(engaged_seconds) not in (int, float)
            or not math.isfinite(engaged_seconds)
            or engaged_seconds <= 0.0
            or type(force_n) not in (int, float)
            or not math.isfinite(force_n)
            or force_n <= 0.0
        ):
            self._invalid_updates_ignored += 1
            return self.estimate
        fingerprint = hashlib.sha256()
        for array in (
            before.heights_mm,
            after.heights_mm,
            before.sigma_mm,
            after.sigma_mm,
        ):
            fingerprint.update(
                np.ascontiguousarray(array, dtype=np.float64).tobytes()
            )
        fingerprint.update(
            np.asarray([engaged_seconds, force_n], dtype=np.float64).tobytes()
        )
        fingerprint.update("|".join(before.roles.tolist()).encode())
        identity = fingerprint.hexdigest()
        if identity in self._consumed_pairs:
            self._duplicate_updates_ignored += 1
            return self.estimate
        self._consumed_pairs.add(identity)
        selected = before.roles == MACHINED
        if selected.sum() < 2:
            raise ValueError("Rate identification needs at least two anchors")
        # The measurement bias cancels in a before/after difference.
        delta = before.heights_mm[selected] - after.heights_mm[selected]
        variance = (
            np.mean(
                before.sigma_mm[selected] ** 2 + after.sigma_mm[selected] ** 2
            )
            / selected.sum()
        )
        if len(delta) > 1:
            variance += float(np.var(delta, ddof=1)) / len(delta)
        measurement_sigma = math.sqrt(max(variance, 1e-12))
        basis = (
            self.limits.nominal_removal_mm_per_n_s
            * float(force_n)
            * float(engaged_seconds)
        )
        if basis <= 1e-6:
            return self.estimate
        observed = float(np.mean(delta))
        snr = observed / measurement_sigma
        self._last_snr = snr
        if observed <= 0.0:
            # Negative or zero apparent removal is drift/noise, not evidence of
            # a slower plant. Freeze the gain instead of clipping it downward.
            self._nonpositive_removal_freezes += 1
            return self.estimate
        if snr < self.limits.min_snr:
            self._low_snr_freezes += 1
            return self.estimate
        self._variance += self._process_noise**2
        gain = (
            self._variance
            * basis
            / (basis**2 * self._variance + measurement_sigma**2)
        )
        self._relative_rate += gain * (observed - basis * self._relative_rate)
        self._variance = max(self._variance * (1.0 - gain * basis), 1e-6)
        lower, upper = self.limits.relative_rate_bounds
        bounded = False
        if self._relative_rate < lower:
            self._relative_rate, bounded = lower, True
        if self._relative_rate > upper:
            self._relative_rate, bounded = upper, True
        if bounded:
            self._variance = max(
                self._variance, self.limits.prior_relative_sigma**2 * 0.25
            )
        self._bounded = bounded
        self._updates += 1
        row = {
            "observed_removal_mm": observed,
            "basis_mm_at_unit_gain": basis,
            "measurement_sigma_mm": measurement_sigma,
            "snr": snr,
            "relative_rate": float(self._relative_rate),
            "relative_sigma": float(math.sqrt(self._variance)),
            "engaged_seconds": float(engaged_seconds),
            "force_n": float(force_n),
        }
        self.history.append(row)
        return self.estimate

    def _overgrind_risk(self, measured_mm):
        lower_bound = np.asarray(measured_mm, dtype=float) - 2 * 0.02
        return float(lower_bound.min()) < -self.limits.tolerance_mm

    def plan_staged_passes(self, measured_mm):
        """Plan roughing to an allowance, then finishing to the nominal face."""
        measured = np.asarray(measured_mm, dtype=float).reshape(-1)
        measured = measured[np.isfinite(measured)]
        if not len(measured):
            return StagingPlan(
                (),
                "insufficient_observation",
                "no finite machined anchors",
                0.0,
                0.0,
                self.limits.roughing_allowance_mm,
                self.limits.finish_target_mm,
                0.0,
            )
        peak = float(measured.max())
        lowest = float(measured.min())
        target = self.limits.finish_target_mm
        passes = []
        if lowest < -self.limits.tolerance_mm:
            return StagingPlan(
                (),
                "overgrind_hold",
                "measured residual is already below the lower limit",
                peak,
                lowest,
                self.limits.roughing_allowance_mm,
                target,
                0.0,
            )
        if peak <= target + 0.005:
            return StagingPlan(
                (),
                "already_within_target",
                "peak is within the finish target",
                peak,
                lowest,
                self.limits.roughing_allowance_mm,
                target,
                0.0,
            )
        safe_cut = lowest + 0.9 * self.limits.tolerance_mm
        desired = min(peak - target, max(safe_cut, 0.0))
        if desired <= 0.0:
            return StagingPlan(
                (),
                "insufficient_budget",
                "uniform pass would overgrind the lowest anchor",
                peak,
                lowest,
                self.limits.roughing_allowance_mm,
                target,
                0.0,
            )
        remaining = desired
        roughing_target = peak - desired + self.limits.roughing_allowance_mm
        rough_cut = max(0.0, peak - max(roughing_target, target))
        rough_cut = min(rough_cut, remaining)
        for _ in range(self.limits.max_roughing_passes):
            if rough_cut <= 0.01 or remaining <= 0.01:
                break
            cut = min(self.limits.max_cut_roughing_mm, rough_cut, remaining)
            passes.append(
                StagedPass(
                    "roughing",
                    float(
                        peak - sum(item.planned_cut_mm for item in passes) - cut
                    ),
                    float(cut),
                    float(self.limits.roughing_force_n),
                    "leave the finishing allowance",
                )
            )
            remaining -= cut
            rough_cut -= cut
        finishing_remaining = remaining
        for _ in range(self.limits.max_finishing_passes):
            if finishing_remaining <= 0.005:
                break
            cut = min(self.limits.max_cut_finishing_mm, finishing_remaining)
            passes.append(
                StagedPass(
                    "finishing",
                    float(
                        peak - sum(item.planned_cut_mm for item in passes) - cut
                    ),
                    float(cut),
                    float(self.limits.finishing_force_n),
                    "finish to the nominal face",
                )
            )
            finishing_remaining -= cut
        planned = float(sum(item.planned_cut_mm for item in passes))
        if planned + 1e-9 < desired:
            status, reason = (
                "insufficient_budget",
                "pass limits leave measured stock above the finish target",
            )
        elif planned + 1e-9 < peak - target:
            status, reason = (
                "overgrind_limited",
                "the lowest anchor limits a uniform pass below the peak",
            )
        else:
            status, reason = "ready", "staged passes cover the measured stock"
        return StagingPlan(
            tuple(passes),
            status,
            reason,
            peak,
            lowest,
            self.limits.roughing_allowance_mm,
            target,
            planned,
        )

    def recommend(
        self,
        desired_cut_mm,
        *,
        force_n,
        feed_mm_s=None,
        stage="finishing",
        segment_length_mm=None,
        current_allowance_mm=None,
    ):
        """Size one pass conservatively from the upper rate confidence bound."""
        if stage not in STAGES:
            raise ValueError(f"Unknown stage: {stage}")
        desired = max(_finite(desired_cut_mm, "desired_cut_mm"), 0.0)
        force = _finite(
            force_n,
            "force_n",
            minimum=self.limits.min_force_n,
            maximum=self.limits.max_force_n,
        )
        nominal_feed = _finite(
            feed_mm_s
            if feed_mm_s is not None
            else self.limits.nominal_feed_mm_s,
            "feed_mm_s",
            minimum=1e-6,
        )
        estimate = self.estimate
        sizing_desired = desired
        if current_allowance_mm is not None:
            # The path depth allowance caps what one pass can physically cut;
            # the rest is reported and handled by later observed passes.
            sizing_desired = min(desired, self.limits.max_allowance_mm)
        if desired <= 0.005:
            return EngagementPlan(
                stage,
                force,
                nominal_feed,
                0.0,
                1.0,
                0.0,
                0,
                desired,
                0.0,
                0.0,
                0.0,
                0.0,
                "already_within_target",
                estimate,
            )
        removal_per_second_upper = (
            self.limits.nominal_removal_mm_per_n_s
            * force
            * max(estimate.upper, 1e-6)
        )
        duration = sizing_desired / removal_per_second_upper
        feed_scale = 1.0
        if segment_length_mm is not None:
            length = _finite(
                segment_length_mm, "segment_length_mm", minimum=0.1
            )
            nominal_duration = length / nominal_feed
            feed_scale = float(
                np.clip(
                    nominal_duration / max(duration, 1e-9),
                    *self.limits.feed_scale_bounds,
                )
            )
            duration = float(
                np.clip(
                    length / (nominal_feed * feed_scale),
                    self.limits.min_engaged_seconds,
                    self.limits.max_engaged_seconds,
                )
            )
            feed_scale = float(
                np.clip(
                    length / (nominal_feed * max(duration, 1e-9)),
                    *self.limits.feed_scale_bounds,
                )
            )
            duration = length / (nominal_feed * feed_scale)
        else:
            duration = float(
                np.clip(
                    duration,
                    self.limits.min_engaged_seconds,
                    self.limits.max_engaged_seconds,
                )
            )
        predicted = (
            self.limits.nominal_removal_mm_per_n_s
            * force
            * duration
            * estimate.relative_rate
        )
        lower = (
            self.limits.nominal_removal_mm_per_n_s
            * force
            * duration
            * estimate.lower
        )
        upper = (
            self.limits.nominal_removal_mm_per_n_s
            * force
            * duration
            * estimate.upper
        )
        remaining = max(desired - predicted, 0.0)
        stop = None
        if self._temperature_proxy(
            force, nominal_feed * feed_scale, duration
        ) > (self.limits.max_temperature_k):
            stop = "thermal_budget"
            duration = 0.0
            predicted = lower = upper = 0.0
            remaining = desired
        offset = 0.0
        offset_limited = 0.0
        if current_allowance_mm is not None:
            allowance = float(current_allowance_mm)
            target_allowance = min(desired, self.limits.max_allowance_mm)
            offset = target_allowance - allowance
            offset = float(
                np.clip(
                    offset,
                    -self.limits.max_normal_offset_mm,
                    self.limits.max_normal_offset_mm,
                )
            )
            offset = float(
                np.clip(
                    offset,
                    -self.limits.max_allowance_mm - allowance,
                    self.limits.max_allowance_mm - allowance,
                )
            )
            offset_limited = max(desired - (allowance + offset), 0.0)
        plan = EngagementPlan(
            stage=stage,
            force_n=force,
            feed_mm_s=float(nominal_feed * feed_scale),
            duration_s=duration,
            feed_scale=feed_scale,
            normal_offset_mm=offset,
            passes=1,
            desired_cut_mm=desired,
            predicted_removal_mm=float(predicted),
            predicted_lower_mm=float(lower),
            predicted_upper_mm=float(upper),
            remaining_mm=float(remaining),
            offset_limited_mm=float(offset_limited),
            stop_reason=stop,
            rate=estimate,
        )
        return plan

    @staticmethod
    def _temperature_proxy(force_n, feed_mm_s, duration_s):
        power = force_n * feed_mm_s / 1000.0
        tau = 25.0
        steady = power * 0.8 * tau
        return float(steady * (1.0 - math.exp(-duration_s / tau)))

    def segment_length_mm(self, nominal_xyz):
        if nominal_xyz is None or len(nominal_xyz) < 2:
            return self.limits.default_segment_length_mm
        points = np.asarray(nominal_xyz, dtype=float)
        if points.ndim != 2 or points.shape[1] != 3:
            return self.limits.default_segment_length_mm
        return float(
            np.linalg.norm(np.diff(points, axis=0), axis=1).sum() * 1000
        )

    def recommend_segment(
        self,
        observed_height_mm,
        *,
        segment_length_mm,
        nominal_feed_mm_s,
        current_allowance_mm,
        force_n,
        stage,
    ):
        """Plan one session segment from its measured excess and allowance."""
        observed = max(_finite(observed_height_mm, "observed_height_mm"), 0.0)
        desired = max(observed - self.limits.finish_target_mm, 0.0)
        return self.recommend(
            desired,
            force_n=force_n,
            feed_mm_s=nominal_feed_mm_s,
            stage=stage,
            segment_length_mm=segment_length_mm,
            current_allowance_mm=current_allowance_mm,
        )

    def as_session_adapter(self):
        """Return a callable matching P0-A's ``ProcessAdapter`` protocol."""
        return SessionProcessAdapter(self)

    def __call__(self, segment, session_state):
        return self.as_session_adapter()(segment, session_state)

    def decision_record(self):
        """Return the last controller decision with its truth-free markers."""
        if self.last_decision is None:
            return {
                "decision": None,
                "truth_used_for_control": False,
            }
        return {
            "decision": self.last_decision,
            "estimate": self.estimate.record(),
            "truth_used_for_control": False,
        }


class SessionProcessAdapter:
    """P0-A ``ProcessAdapter``: ``(segment, session_state) -> dict | None``."""

    DURATION_KEYS = (
        "engaged_duration_s",
        "contact_duration_s",
        "removal_contact_s",
        "duration_s",
    )

    def __init__(self, controller):
        if not isinstance(controller, AdaptiveProcessController):
            raise ValueError("SessionProcessAdapter requires a controller")
        self.controller = controller
        self._previous_estimates = {}
        self.identification_events = []

    def absorb_session_state(self, session_state):
        """Use the session's signed evidence to update the rate estimate.

        A checkpoint only supplies observations: region anchor estimates and
        an execution duration. The measurement bias cancels in the
        before/after difference, so this update remains truth-free.
        """
        if not isinstance(session_state, dict):
            return None
        report = session_state.get("reinspection", {})
        regions = report.get("regions", []) if isinstance(report, dict) else []
        executions = session_state.get("execution_history", [])
        duration = None
        force = None
        if isinstance(executions, list):
            for execution in reversed(executions):
                if not isinstance(execution, dict):
                    continue
                for key in self.DURATION_KEYS:
                    value = execution.get(key)
                    if (
                        type(value) in (int, float)
                        and math.isfinite(value)
                        and value > 0
                    ):
                        duration = float(value)
                        break
                if duration is not None:
                    for key in ("commanded_force_n", "force_n"):
                        value = execution.get(key)
                        if type(value) in (int, float) and value > 0:
                            force = float(value)
                            break
                    break
        force = (
            force
            if force is not None
            else self.controller.limits.finishing_force_n
        )
        updated = 0
        for region in regions:
            if not isinstance(region, dict):
                continue
            identity = region.get("id")
            estimates = region.get("anchor_estimates_mm")
            if identity is None or not isinstance(estimates, list):
                continue
            current = np.array(
                [np.nan if item is None else float(item) for item in estimates]
            )
            previous = self._previous_estimates.get(identity)
            if (
                duration is not None
                and previous is not None
                and len(previous) == len(current)
            ):
                matched = np.isfinite(previous) & np.isfinite(current)
                if matched.sum() >= 2:
                    sigma = self._region_sigma(region, int(matched.sum()))
                    before = AnchorObservation(
                        heights_mm=previous[matched],
                        sigma_mm=np.full(int(matched.sum()), sigma),
                        roles=np.array([MACHINED] * int(matched.sum())),
                    )
                    after = AnchorObservation(
                        heights_mm=current[matched],
                        sigma_mm=np.full(int(matched.sum()), sigma),
                        roles=np.array([MACHINED] * int(matched.sum())),
                    )
                    self.controller.update_from_observation(
                        before,
                        after,
                        engaged_seconds=duration,
                        force_n=force,
                    )
                    updated += 1
                    self.identification_events.append(
                        {
                            "region_id": identity,
                            "matched_anchors": int(matched.sum()),
                            "engaged_seconds": duration,
                            "force_n": force,
                            "estimate": self.controller.estimate.record(),
                        }
                    )
            self._previous_estimates[identity] = current
        return None if not updated else self.controller.estimate.record()

    def _region_sigma(self, region, count):
        bounds = region.get("anchor_bounds_mm")
        if isinstance(bounds, list) and bounds:
            widths = [
                (item[1] - item[0]) / 4.0
                for item in bounds
                if isinstance(item, (list, tuple))
                and len(item) == 2
                and item[0] is not None
                and item[1] is not None
            ]
            if len(widths) >= count:
                return float(np.clip(np.median(widths), 0.005, 0.5))
        return 0.02

    def __call__(self, segment, session_state):
        if not isinstance(segment, dict) or not isinstance(session_state, dict):
            raise ValueError(
                "Session adapter requires segment and state mappings"
            )
        self.absorb_session_state(session_state)
        path = segment.get("path", {})
        if not isinstance(path, dict):
            path = {}
        observed = path.get("normal_height_mm")
        observed = 0.0 if observed is None else float(observed)
        allowance = float(path.get("material_allowance_mm") or 0.0)
        recipe = (
            path.get("recipe", {})
            if isinstance(path.get("recipe"), dict)
            else {}
        )
        feed = float(
            recipe.get("feed_mm_s") or self.controller.limits.nominal_feed_mm_s
        )
        length = self.controller.segment_length_mm(path.get("nominal_xyz"))
        stage = (
            "roughing"
            if observed > self.controller.limits.roughing_allowance_mm
            else "finishing"
        )
        force = (
            self.controller.limits.roughing_force_n
            if stage == "roughing"
            else self.controller.limits.finishing_force_n
        )
        plan = self.controller.recommend_segment(
            observed,
            segment_length_mm=length,
            nominal_feed_mm_s=feed,
            current_allowance_mm=allowance,
            force_n=force,
            stage=stage,
        )
        self.controller.last_decision = {
            "segment_id": segment.get("segment_id"),
            "region_id": segment.get("region_id"),
            **plan.record(),
        }
        if plan.stop_reason == "already_within_target":
            return {
                "feed_scale": 1.0,
                "normal_offset_mm": 0.0,
                "passes": 0,
                "dwell_scale": 1.0,
                "truth_used_for_control": False,
            }
        return {
            "feed_scale": plan.feed_scale,
            "normal_offset_mm": plan.normal_offset_mm,
            "passes": 1,
            "dwell_scale": 1.0,
            "truth_used_for_control": False,
        }

    def last_evidence(self):
        """Return the rate evidence for ``session_state.json`` persistence."""
        return {
            "estimate": self.controller.estimate.record(),
            "last_decision": self.controller.last_decision,
            "identification_events": list(self.identification_events),
            "truth_used_for_control": False,
        }


@dataclass(frozen=True)
class LocalBaselineConfig:
    """Thresholds for the local normal-reference bias correction."""

    tolerance_mm: float = TOLERANCE_MM
    sigma_multiplier: float = 2.0
    min_baseline_anchors: int = 12
    min_baseline_per_side: int = 3
    require_both_sides: bool = True
    max_bias_mm: float = 0.6
    max_baseline_spread_mm: float = 0.35
    bias_sigma_floor_mm: float = 0.005
    protected_tolerance_mm: float = 0.15
    require_protection: bool = False
    min_protected_anchors: int = 4
    consistency_sigma: float = 3.0
    max_reference_overlap_mm: float = 1.0

    def __post_init__(self):
        if not 0.0 < self.tolerance_mm <= TOLERANCE_MM:
            raise ValueError(
                "tolerance_mm must be positive and may not exceed 0.3 mm"
            )
        for name in (
            "sigma_multiplier",
            "max_bias_mm",
            "max_baseline_spread_mm",
            "bias_sigma_floor_mm",
            "protected_tolerance_mm",
            "consistency_sigma",
            "max_reference_overlap_mm",
        ):
            _finite(getattr(self, name), name, minimum=0.0)
        for name in (
            "min_baseline_anchors",
            "min_baseline_per_side",
            "min_protected_anchors",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not 0.0 < self.protected_tolerance_mm <= self.tolerance_mm:
            raise ValueError(
                "Protected tolerance must be tighter than the gate"
            )


@dataclass(frozen=True)
class SignedAnchors:
    """Signed residual samples relative to registered nominal CAD anchors."""

    heights_mm: np.ndarray
    sigma_mm: np.ndarray | float
    sides: np.ndarray | None = None
    points_m: np.ndarray | None = None
    normals_m: np.ndarray | None = None
    label: str = "region"

    def __post_init__(self):
        heights = np.asarray(self.heights_mm, dtype=float).reshape(-1)
        sigma = np.broadcast_to(
            np.asarray(self.sigma_mm, dtype=float), heights.shape
        ).copy()
        if not (
            len(heights)
            and np.isfinite(heights).all()
            and np.isfinite(sigma).all()
            and (sigma > 0).all()
        ):
            raise ValueError("Signed anchor heights and sigma must be finite")
        object.__setattr__(self, "heights_mm", heights)
        object.__setattr__(self, "sigma_mm", sigma)
        if self.sides is not None:
            sides = np.asarray(self.sides).reshape(-1)
            if len(sides) != len(heights) or not set(sides) <= {-1, 1}:
                raise ValueError("Baseline sides must be -1/+1 per anchor")
            object.__setattr__(self, "sides", sides)


@dataclass(frozen=True)
class ReinspectionVerdict:
    """Calibrated local reinspection result; the raw gate is never widened."""

    status: str
    reason: str
    bias_mm: float
    bias_sigma_mm: float
    baseline_mad_mm: float
    baseline_anchors: int
    machined_anchors: int
    protected_anchors: int
    anchor_bounds_mm: tuple
    anchor_states: tuple
    calibrated_min_mm: float | None
    calibrated_max_mm: float | None
    automatic_regrind_allowed: bool
    bias_veto: bool
    naive_status: str
    naive_false_release: bool
    naive_false_reject: bool
    protection_status: str
    tolerance_mm: float
    truth_used_for_decision: bool = False

    def record(self):
        return {
            "status": self.status,
            "reason": self.reason,
            "bias_mm": self.bias_mm,
            "bias_sigma_mm": self.bias_sigma_mm,
            "baseline_mad_mm": self.baseline_mad_mm,
            "baseline_anchors": self.baseline_anchors,
            "machined_anchors": self.machined_anchors,
            "protected_anchors": self.protected_anchors,
            "anchor_bounds_mm": self.anchor_bounds_mm,
            "anchor_states": self.anchor_states,
            "calibrated_min_mm": self.calibrated_min_mm,
            "calibrated_max_mm": self.calibrated_max_mm,
            "automatic_regrind_allowed": self.automatic_regrind_allowed,
            "bias_veto": self.bias_veto,
            "naive_status": self.naive_status,
            "naive_false_release": self.naive_false_release,
            "naive_false_reject": self.naive_false_reject,
            "protection_status": self.protection_status,
            "tolerance_mm": self.tolerance_mm,
            "gate_unchanged": True,
            "truth_used_for_decision": False,
        }


def _anchor_state(lower, upper, tolerance):
    if lower > tolerance:
        return "excess"
    if upper < -tolerance:
        return "overgrind"
    if lower >= -tolerance and upper <= tolerance:
        return "conforming"
    return "uncertain"


def _aggregate_states(states):
    if not states:
        return "hold"
    if "overgrind" in states:
        return "overgrind"
    if "uncertain" in states:
        return "uncertain"
    if "excess" in states:
        return "positive_excess"
    return "pass"


class LocalBaselineReinspector:
    """Remove a locally estimated systematic bias before the signed decision.

    Untouched normal-surface anchors on both sides of the machined band
    estimate the local offset by a robust median and MAD. A calibrated
    interval propagates the anchor sigma and the bias standard error. The
    returned status is never looser than the naive interval; protection and
    local-consistency checks can only tighten it.
    """

    def __init__(self, config=LocalBaselineConfig()):
        self.config = config

    def as_session_calibrator(self):
        """Return a callable matching P0-A's ``ReinspectionCalibrator``."""
        return SessionReinspectionCalibrator(self)

    def __call__(self, report, session_state):
        return self.as_session_calibrator()(report, session_state)

    def estimate_baseline(self, baseline):
        """Return the robust local bias and its standard error."""
        if not isinstance(baseline, SignedAnchors):
            raise ValueError("Baseline must be SignedAnchors")
        heights = baseline.heights_mm
        if len(heights) < self.config.min_baseline_anchors:
            return None, "baseline_insufficient"
        if self.config.require_both_sides:
            if baseline.sides is None:
                return None, "baseline_sides_missing"
            for side in (-1, 1):
                if int(np.sum(baseline.sides == side)) < (
                    self.config.min_baseline_per_side
                ):
                    return None, "baseline_sides_missing"
        center = float(np.median(heights))
        mad = float(1.4826 * np.median(np.abs(heights - center)))
        if mad > self.config.max_baseline_spread_mm:
            return None, "baseline_inconsistent"
        if abs(center) > self.config.max_bias_mm:
            return None, "baseline_bias_out_of_range"
        scale = max(mad, float(np.sqrt(np.mean(baseline.sigma_mm**2))))
        sigma_bias = max(
            scale / math.sqrt(len(heights)),
            self.config.bias_sigma_floor_mm,
        )
        return (
            {"bias_mm": center, "bias_sigma_mm": sigma_bias, "mad_mm": mad},
            None,
        )

    def evaluate(self, machined, baseline, protected=None):
        """Return the calibrated reinspection verdict."""
        if not isinstance(machined, SignedAnchors):
            raise ValueError("Machined anchors must be SignedAnchors")
        multiplier = self.config.sigma_multiplier
        tolerance = self.config.tolerance_mm
        naive_lower = machined.heights_mm - multiplier * machined.sigma_mm
        naive_upper = machined.heights_mm + multiplier * machined.sigma_mm
        naive_states = [
            _anchor_state(lower, upper, tolerance)
            for lower, upper in zip(naive_lower, naive_upper)
        ]
        naive_status = _aggregate_states(naive_states)
        if (
            baseline.points_m is not None
            and machined.points_m is not None
            and len(baseline.points_m)
            and len(machined.points_m)
        ):
            distances = np.linalg.norm(
                np.asarray(baseline.points_m, dtype=float)[:, None, :]
                - np.asarray(machined.points_m, dtype=float)[None, :, :],
                axis=2,
            )
            if float(distances.min()) * 1000 < (
                self.config.max_reference_overlap_mm
            ):
                return self._hold_verdict(
                    "baseline_overlaps_target",
                    None,
                    machined,
                    baseline,
                    protected,
                    naive_status,
                )
        bias, error = self.estimate_baseline(baseline)
        if error is not None:
            return self._hold_verdict(
                error, bias, machined, baseline, protected, naive_status
            )
        calibrated = machined.heights_mm - bias["bias_mm"]
        sigma_calibrated = np.sqrt(
            machined.sigma_mm**2 + bias["bias_sigma_mm"] ** 2
        )
        lower = calibrated - multiplier * sigma_calibrated
        upper = calibrated + multiplier * sigma_calibrated
        states = [
            _anchor_state(item_lower, item_upper, tolerance)
            for item_lower, item_upper in zip(lower, upper)
        ]
        status = _aggregate_states(states)
        protection_status = "not_supplied"
        protection_ok = True
        if protected is not None:
            if len(protected.heights_mm) < self.config.min_protected_anchors:
                protection_status = "protected_missing"
                protection_ok = False
            else:
                protected_calibrated = protected.heights_mm - bias["bias_mm"]
                protected_sigma = np.sqrt(
                    protected.sigma_mm**2 + bias["bias_sigma_mm"] ** 2
                )
                protected_lower = (
                    protected_calibrated - multiplier * protected_sigma
                )
                protected_upper = (
                    protected_calibrated + multiplier * protected_sigma
                )
                within = bool(
                    (
                        protected_lower >= -self.config.protected_tolerance_mm
                    ).all()
                    and (
                        protected_upper <= self.config.protected_tolerance_mm
                    ).all()
                )
                consistency_sigma = math.sqrt(
                    bias["bias_sigma_mm"] ** 2
                    + float(np.mean(protected.sigma_mm**2))
                    / len(protected.heights_mm)
                )
                consistent = (
                    abs(
                        float(np.median(protected.heights_mm)) - bias["bias_mm"]
                    )
                    <= self.config.consistency_sigma * consistency_sigma
                )
                if not within:
                    protection_status, protection_ok = (
                        "protected_violation",
                        False,
                    )
                elif not consistent:
                    protection_status, protection_ok = (
                        "local_bias_inconsistent",
                        False,
                    )
                else:
                    protection_status = "intact"
        elif self.config.require_protection:
            protection_status, protection_ok = "protected_missing", False
        if not protection_ok:
            status = "hold"
        automatic_regrind = (
            status == "positive_excess"
            and protection_ok
            and all(state in {"conforming", "excess"} for state in states)
        )
        naive_false_release = naive_status == "pass" and status != "pass"
        naive_false_reject = naive_status != "pass" and status == "pass"
        bias_veto = bool(
            naive_false_release
            or (status == "hold")
            or (
                naive_status in {"pass", "positive_excess"}
                and status in {"uncertain", "overgrind"}
            )
        )
        return ReinspectionVerdict(
            status=status,
            reason=self._reason(status, protection_status),
            bias_mm=float(bias["bias_mm"]),
            bias_sigma_mm=float(bias["bias_sigma_mm"]),
            baseline_mad_mm=float(bias["mad_mm"]),
            baseline_anchors=int(len(baseline.heights_mm)),
            machined_anchors=int(len(machined.heights_mm)),
            protected_anchors=0
            if protected is None
            else int(len(protected.heights_mm)),
            anchor_bounds_mm=tuple(
                (float(item_lower), float(item_upper))
                for item_lower, item_upper in zip(lower, upper)
            ),
            anchor_states=tuple(states),
            calibrated_min_mm=float(np.min(lower)),
            calibrated_max_mm=float(np.max(upper)),
            automatic_regrind_allowed=bool(automatic_regrind),
            bias_veto=bias_veto,
            naive_status=naive_status,
            naive_false_release=bool(naive_false_release),
            naive_false_reject=bool(naive_false_reject),
            protection_status=protection_status,
            tolerance_mm=tolerance,
        )

    def _hold_verdict(
        self, error, bias, machined, baseline, protected, naive_status
    ):
        return ReinspectionVerdict(
            status="hold",
            reason=error,
            bias_mm=0.0 if bias is None else float(bias["bias_mm"]),
            bias_sigma_mm=0.0 if bias is None else float(bias["bias_sigma_mm"]),
            baseline_mad_mm=0.0 if bias is None else float(bias["mad_mm"]),
            baseline_anchors=int(len(baseline.heights_mm)),
            machined_anchors=int(len(machined.heights_mm)),
            protected_anchors=0
            if protected is None
            else int(len(protected.heights_mm)),
            anchor_bounds_mm=(),
            anchor_states=(),
            calibrated_min_mm=None,
            calibrated_max_mm=None,
            automatic_regrind_allowed=False,
            bias_veto=error
            in {
                "baseline_inconsistent",
                "baseline_bias_out_of_range",
                "baseline_overlaps_target",
            },
            naive_status=naive_status,
            naive_false_release=False,
            naive_false_reject=False,
            protection_status="unverified",
            tolerance_mm=self.config.tolerance_mm,
        )

    @staticmethod
    def _reason(status, protection_status):
        if protection_status not in {"intact", "not_supplied"}:
            return protection_status
        return {
            "pass": "calibrated_interval_within_limits",
            "positive_excess": "calibrated_lower_bound_above_upper_limit",
            "overgrind": "calibrated_upper_bound_below_lower_limit",
            "uncertain": "calibrated_interval_crosses_a_limit",
            "hold": "local_baseline_unavailable",
        }[status]


class SessionReinspectionCalibrator:
    """P0-A ``ReinspectionCalibrator``: ``(report, state) -> dict``."""

    BASELINE_KEYS = ("local_baseline", "baseline_anchors")

    def __init__(self, reinspector):
        if not isinstance(reinspector, LocalBaselineReinspector):
            raise ValueError(
                "SessionReinspectionCalibrator requires a reinspector"
            )
        self.reinspector = reinspector

    def __call__(self, report, session_state):
        if not isinstance(report, dict) or not isinstance(session_state, dict):
            raise ValueError("Calibrator requires report and state mappings")
        baseline_record = self._baseline_record(report, session_state)
        if baseline_record is None:
            return {
                "status": "baseline_unavailable",
                "reason": "no untouched local normal anchors were supplied",
                "bias_veto": False,
                "gate_unchanged": True,
                "truth_used_for_decision": False,
            }
        machined = self._anchors(
            report.get("machined")
            or baseline_record.get("machined")
            or report.get("regions")
        )
        baseline = self._anchors(baseline_record.get("baseline"))
        protected = self._anchors(baseline_record.get("protected"))
        if machined is None or baseline is None:
            return {
                "status": "baseline_unavailable",
                "reason": "machined or baseline anchor schema is incomplete",
                "bias_veto": False,
                "gate_unchanged": True,
                "truth_used_for_decision": False,
            }
        verdict = self.reinspector.evaluate(machined, baseline, protected)
        return {
            **verdict.record(),
            "source": "local normal-surface baseline correction",
            "gate_unchanged": True,
        }

    @staticmethod
    def _point(value):
        if isinstance(value, dict):
            point = value.get("point", value.get("point_m", value.get("xyz_m")))
        else:
            point = value
        if point is None:
            return None
        return [float(item) for item in point]

    @staticmethod
    def _baseline_record(report, session_state):
        """Return the local-baseline mapping or ``None``.

        ``local_baseline`` is preferred; ``baseline_anchors`` is accepted as a
        fallback from either the report or the observed session context.
        """
        for source in (report, session_state):
            value = source.get("local_baseline")
            if isinstance(value, dict):
                return value
        for source in (report, session_state):
            value = source.get("baseline_anchors")
            if value is None:
                continue
            if isinstance(value, dict) and (
                "baseline" in value or "protected" in value
            ):
                return value
            if isinstance(value, dict):
                return {"baseline": value}
            if isinstance(value, (list, tuple)):
                return {"baseline": list(value)}
        return None

    def _region_samples(self, region):
        """Parse one production region's per-anchor estimates and bounds."""
        raw_heights = region.get("anchor_estimates_mm")
        if not isinstance(raw_heights, list):
            return None
        bounds = region.get("anchor_bounds_mm", [])
        raw_points = region.get("points_m", region.get("anchor_points_m"))
        keep = [
            index for index, item in enumerate(raw_heights) if item is not None
        ]
        if not keep:
            return [], [], []
        heights = [float(raw_heights[index]) for index in keep]
        sigma = []
        for index in keep:
            item = bounds[index] if index < len(bounds) else None
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and item[0] is not None
                and item[1] is not None
            ):
                sigma.append(max((item[1] - item[0]) / 4.0, 0.005))
            else:
                sigma.append(0.02)
        points = None
        if raw_points is not None and len(raw_points) == len(raw_heights):
            points = [self._point(raw_points[index]) for index in keep]
        return heights, sigma, points

    def _anchors(self, value):
        if value is None:
            return None
        if isinstance(value, SignedAnchors):
            return value
        if isinstance(value, list):
            heights, sigma, sides, points = [], [], [], []
            for row in value:
                if not isinstance(row, dict):
                    return None
                if "height_mm" in row:
                    heights.append(float(row["height_mm"]))
                    sigma.append(float(row.get("sigma_mm", 0.02)))
                    sides.append(int(row.get("side", 0)))
                    points.append(self._point(row))
                    continue
                if "anchor_estimates_mm" not in row:
                    return None
                parsed = self._region_samples(row)
                if parsed is None:
                    return None
                region_heights, region_sigma, region_points = parsed
                if not region_heights:
                    continue
                heights.extend(region_heights)
                sigma.extend(region_sigma)
                sides.extend([0] * len(region_heights))
                points.extend(
                    region_points
                    if region_points is not None
                    else [None] * len(region_heights)
                )
            if not heights:
                return None
            sides_array = None
            if set(sides) <= {-1, 1} and any(sides):
                sides_array = np.asarray(sides, dtype=int)
            points_array = None
            if points and all(point is not None for point in points):
                points_array = np.asarray(points, dtype=float)
            return SignedAnchors(
                heights_mm=np.asarray(heights),
                sigma_mm=np.asarray(sigma),
                sides=sides_array,
                points_m=points_array,
            )
        if isinstance(value, dict):
            if "anchor_estimates_mm" in value:
                parsed = self._region_samples(value)
                if parsed is None:
                    return None
                heights, sigma, points = parsed
                if not heights:
                    return None
                return SignedAnchors(
                    heights_mm=np.asarray(heights),
                    sigma_mm=np.asarray(sigma),
                    points_m=None
                    if points is None
                    else np.asarray(points, dtype=float),
                )
            if "heights_mm" in value:
                points = value.get("points_m")
                return SignedAnchors(
                    heights_mm=np.asarray(value["heights_mm"]),
                    sigma_mm=np.asarray(value.get("sigma_mm", 0.02)),
                    sides=None
                    if value.get("sides") is None
                    else np.asarray(value["sides"]),
                    points_m=None
                    if points is None
                    else np.asarray(points, dtype=float),
                )
        return None


@dataclass(frozen=True)
class ProcessCase:
    """One disturbance scenario for the paired offline stress suite."""

    case_id: str
    rate_scale: float
    peak_mm: float
    spread_mm: float = 0.06
    wear_index_per_n_s: float = 0.02
    wear_penalty: float = 0.03
    material_factor: float = 1.0
    measurement_sigma_mm: float = 0.02
    measurement_bias_mm: float = 0.0
    max_passes: int = 14
    feed_mm_s: float = 30.0

    def __post_init__(self):
        if not isinstance(self.case_id, str) or not self.case_id:
            raise ValueError("case_id must be a nonempty string")
        _finite(self.rate_scale, "rate_scale", minimum=0.2, maximum=2.5)
        _finite(self.peak_mm, "peak_mm", minimum=0.0)
        _finite(self.spread_mm, "spread_mm", minimum=0.0)
        _finite(self.measurement_sigma_mm, "measurement_sigma_mm", minimum=1e-6)
        _finite(self.measurement_bias_mm, "measurement_bias_mm")
        if type(self.max_passes) is not int or not 1 <= self.max_passes <= 40:
            raise ValueError("max_passes must be an integer in 1–40")
        if self.peak_mm - self.spread_mm < 0.0:
            raise ValueError("spread_mm may not remove the whole stock")


DEFAULT_CASES = (
    ProcessCase("rate_040", 0.4, 1.2),
    ProcessCase("rate_070", 0.7, 1.2),
    ProcessCase("rate_100", 1.0, 1.2),
    ProcessCase("rate_130", 1.3, 1.2),
    ProcessCase("rate_180", 1.8, 1.2),
    ProcessCase(
        "wear_severe", 1.0, 2.4, wear_index_per_n_s=0.08, wear_penalty=0.08
    ),
    ProcessCase("uneven_stock", 1.0, 1.0, spread_mm=0.22),
    ProcessCase("hidden_bias_negative", 1.0, 1.2, measurement_bias_mm=-0.22),
    ProcessCase("hidden_bias_positive", 1.0, 1.2, measurement_bias_mm=0.25),
)


def _anchors_for_case(case, seed):
    count = 15
    positions = np.linspace(0.0, 1.0, count)
    machined = case.peak_mm - case.spread_mm * positions
    baseline_count = 7
    protected_count = 6
    residual = np.r_[
        machined,
        np.zeros(baseline_count),
        np.zeros(baseline_count),
        np.zeros(protected_count),
    ]
    roles = np.array(
        [MACHINED] * count
        + [BASELINE_LEFT] * baseline_count
        + [BASELINE_RIGHT] * baseline_count
        + [PROTECTED] * protected_count
    )
    return ProcessPlantProxy(
        PlantParameters(
            rate_scale=case.rate_scale,
            wear_index_per_n_s=case.wear_index_per_n_s,
            wear_penalty=case.wear_penalty,
            material_factor=case.material_factor,
            measurement_sigma_mm=case.measurement_sigma_mm,
            measurement_bias_mm=case.measurement_bias_mm,
        ),
        residual,
        roles,
        np.random.default_rng(seed),
    )


def _signed_anchors(observation, roles, label, *, with_sides=False):
    """Convert observed anchors into the calibrated reinspection schema."""
    subset = observation.select(*roles)
    sides = None
    if with_sides:
        sides = np.where(subset.roles == BASELINE_LEFT, -1, 1)
    return SignedAnchors(
        heights_mm=subset.heights_mm,
        sigma_mm=subset.sigma_mm,
        sides=sides,
        label=label,
    )


def _evaluate_observation(reinspector, observation):
    return reinspector.evaluate(
        _signed_anchors(observation, (MACHINED,), "machined"),
        _signed_anchors(
            observation, BASELINE_ROLES, "baseline", with_sides=True
        ),
        _signed_anchors(observation, (PROTECTED,), "protected"),
    )


def run_process_case(case, seed=0):
    """Run one observation-only adaptation loop and audit it afterwards."""
    if not isinstance(case, ProcessCase):
        raise ValueError("run_process_case requires a ProcessCase")
    plant = _anchors_for_case(case, seed)
    limits = ProcessLimits()
    controller = AdaptiveProcessController(limits)
    reinspector = LocalBaselineReinspector()
    initial = plant.observe(0)
    observation = initial
    rows = []
    stop_reason = "pass_budget_exhausted"
    for pass_index in range(case.max_passes):
        machined = observation.select(MACHINED)
        staging = controller.plan_staged_passes(machined.heights_mm)
        if staging.status == "already_within_target":
            stop_reason = "already_within_target"
            break
        if staging.status == "overgrind_hold":
            stop_reason = "overgrind_hold"
            break
        if not staging.passes:
            stop_reason = staging.status
            break
        staged = staging.passes[0]
        plan = controller.recommend(
            staged.planned_cut_mm,
            force_n=staged.force_n,
            feed_mm_s=case.feed_mm_s,
            stage=staged.stage,
        )
        if plan.stop_reason is not None or plan.duration_s <= 0.0:
            stop_reason = plan.stop_reason or "no_engagement"
            break
        execution = plant.command(
            staged.stage, plan.force_n, plan.feed_mm_s, plan.duration_s
        )
        after = plant.observe(pass_index + 1)
        estimate = controller.update_from_observation(
            observation,
            after,
            engaged_seconds=execution.duration_s,
            force_n=execution.force_n,
        )
        verdict = _evaluate_observation(reinspector, after)
        rows.append(
            {
                "pass": pass_index + 1,
                "staging_status": staging.status,
                "staged": staged.record(),
                "plan": plan.record(),
                "execution": execution.record(),
                "estimate": estimate.record(),
                "verdict": verdict.record(),
            }
        )
        observation = after
        if verdict.status in {"pass", "overgrind"}:
            stop_reason = verdict.status
            break
        if execution.guard is not None:
            stop_reason = execution.guard
            break
    final_verdict = _evaluate_observation(reinspector, observation)
    truth = plant.truth()
    true_machined = np.asarray(truth["residual_mm"])[plant.roles == MACHINED]
    true_peak = float(np.max(np.abs(true_machined)))
    final_estimate = controller.estimate
    false_release = (
        final_verdict.status == "pass" and true_peak > limits.tolerance_mm
    )
    converged = abs(final_estimate.relative_rate - case.rate_scale) <= 0.15
    return {
        "case_id": case.case_id,
        "seed": seed,
        "case": {
            "rate_scale": case.rate_scale,
            "peak_mm": case.peak_mm,
            "spread_mm": case.spread_mm,
            "wear_index_per_n_s": case.wear_index_per_n_s,
            "wear_penalty": case.wear_penalty,
            "material_factor": case.material_factor,
            "measurement_sigma_mm": case.measurement_sigma_mm,
            "measurement_bias_mm": case.measurement_bias_mm,
        },
        "initial_observation": initial.record(),
        "final_observation": observation.record(),
        "passes": rows,
        "pass_count": len(rows),
        "stop_reason": stop_reason,
        "final_verdict": final_verdict.record(),
        "final_estimate": final_estimate.record(),
        "rate_error": float(final_estimate.relative_rate - case.rate_scale),
        "converged": bool(converged),
        "released": final_verdict.status == "pass",
        "true_peak_mm": true_peak,
        "false_release": bool(false_release),
        "audit": {
            **truth,
            "true_rate_scale": case.rate_scale,
            "rate_error": float(final_estimate.relative_rate - case.rate_scale),
            "false_release": bool(false_release),
            "truth_role": "post-hoc only; never read by the controller",
        },
        "truth_used_for_control": False,
    }


@dataclass(frozen=True)
class BiasInterceptionCase:
    """A hidden measurement-bias scenario for the naive/calibrated comparison."""

    case_id: str
    true_machined_mm: float
    bias_mm: float
    expected_calibrated: str


BIAS_CASES = (
    BiasInterceptionCase(
        "hidden_negative_false_release", 0.36, -0.18, "positive_excess"
    ),
    BiasInterceptionCase(
        "hidden_positive_false_release", -0.36, 0.18, "overgrind"
    ),
    BiasInterceptionCase("hidden_positive_false_reject", 0.10, 0.25, "pass"),
    BiasInterceptionCase("hidden_negative_false_reject", -0.13, -0.22, "pass"),
)


def run_bias_interception_case(case, seed=0):
    """Compare the naive signed gate with the locally calibrated gate."""
    if not isinstance(case, BiasInterceptionCase):
        raise ValueError("run_bias_interception_case requires a case")
    rng = np.random.default_rng(seed)
    sigma = 0.02
    baseline_heights = case.bias_mm + rng.normal(0.0, 0.015, 24)
    machined_heights = (
        case.true_machined_mm + case.bias_mm + rng.normal(0.0, sigma, 9)
    )
    protected_heights = case.bias_mm + rng.normal(0.0, 0.015, 8)
    machined = SignedAnchors(machined_heights, sigma, label="machined")
    baseline = SignedAnchors(
        baseline_heights,
        0.015,
        sides=np.array([-1] * 12 + [1] * 12),
        label="baseline",
    )
    protected = SignedAnchors(protected_heights, 0.015, label="protected")
    verdict = LocalBaselineReinspector().evaluate(machined, baseline, protected)
    return {
        "case_id": case.case_id,
        "true_machined_mm": case.true_machined_mm,
        "injected_bias_mm": case.bias_mm,
        "expected_calibrated": case.expected_calibrated,
        "verdict": verdict.record(),
        "calibrated_correct": verdict.status == case.expected_calibrated,
        "naive_wrong": (
            verdict.naive_false_release or verdict.naive_false_reject
        ),
        "truth_used_for_decision": False,
    }


def run_suite(seeds=(0,), cases=DEFAULT_CASES):
    """Run all process cases and hidden-bias comparisons."""
    process = [run_process_case(case, seed) for case in cases for seed in seeds]
    bias = [
        run_bias_interception_case(case, seed)
        for case in BIAS_CASES
        for seed in seeds
    ]
    converged = sum(item["converged"] for item in process)
    false_releases = sum(item["false_release"] for item in process)
    summary = {
        "case_count": len({item["case_id"] for item in process}),
        "run_count": len(process),
        "converged_rate_count": converged,
        "max_abs_rate_error": max(abs(item["rate_error"]) for item in process),
        "mean_rate_error": float(
            np.mean([item["rate_error"] for item in process])
        ),
        "mean_passes": float(np.mean([item["pass_count"] for item in process])),
        "released_count": sum(item["released"] for item in process),
        "false_release_count": false_releases,
        "bias_case_count": len(bias),
        "bias_calibrated_correct": sum(
            item["calibrated_correct"] for item in bias
        ),
        "bias_naive_wrong": sum(item["naive_wrong"] for item in bias),
    }
    return {"process": process, "bias": bias, "summary": summary}


def _write_outputs(results, output, command):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    case_directory = output / "cases"
    case_directory.mkdir()
    for item in results["process"]:
        write_json(
            case_directory / f"{item['case_id']}_seed{item['seed']}.json", item
        )
    rows = []
    for item in results["process"]:
        for row in item["passes"]:
            rows.append(
                {
                    "case_id": item["case_id"],
                    "seed": item["seed"],
                    "pass": row["pass"],
                    "stage": row["staged"]["stage"],
                    "staged_cut_mm": row["staged"]["planned_cut_mm"],
                    "duration_s": row["execution"]["duration_s"],
                    "force_n": row["execution"]["force_n"],
                    "removed_mean_mm": row["execution"]["removed_mean_mm"],
                    "observed_snr": row["estimate"]["last_snr"],
                    "relative_rate": row["estimate"]["relative_rate"],
                    "relative_sigma": row["estimate"]["relative_sigma"],
                    "true_rate_audit_only": item["case"]["rate_scale"],
                    "wear_index": row["execution"]["wear_index"],
                    "verdict": row["verdict"]["status"],
                    "truth_used_for_control": False,
                }
            )
    csv_path = output / "rate_identification.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        fieldnames = list(rows[0]) if rows else ["case_id"]
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    write_json(
        output / "bias_interception.json",
        {
            "schema": "daiyi-bias-interception-v1",
            "cases": results["bias"],
            "calibration_rule": (
                "subtract the robust local baseline median and propagate its "
                "standard error; the signed gate is never widened"
            ),
            "truth_used_for_decision": False,
        },
    )
    write_json(output / "summary.json", results["summary"])
    evidence = {
        "schema": "daiyi-adaptive-process-evidence-v1",
        "task": "P1-B process disturbance, online rate identification and "
        "local-baseline debiased reinspection",
        "command": command,
        "scope": {
            "offline_only": True,
            "mujoco_used": False,
            "real_data_used": False,
            "robot_commands": False,
            "truth_used_for_control": False,
            "truth_used_for_decision": False,
            "plant": "synthetic proxy with hidden 0.4–1.8 rate scale and wear",
            "calibration": "local normal anchors, robust median/MAD",
        },
        "inputs": {
            "module_sha256": sha256_file(__file__),
            "real_rate_calibration_pending": True,
        },
        "metrics": results["summary"],
        "real_data_pending": [
            "paired before/after signed measurements at identical CAD anchors",
            "instrumented normal force and actual belt speed",
            "belt wear metrology over force x time blocks",
            "fixture compliance under static load",
            "independent hidden-bias audits from a calibrated artifact",
        ],
    }
    write_json(output / "evidence.json", evidence)
    (output / "report.md").write_text(_report(results), encoding="utf-8")
    return evidence


def _report(results):
    lines = [
        "# 工艺扰动、在线去除率辨识与局部基准去偏置复检",
        "",
        "本运行只用观测证据驱动控制与放行。plant 的 0.4×–1.8× 去除率偏差、"
        "砂带磨损、柔顺/温升边界和隐藏测量偏置均属于离线合成代理；"
        "真值只用于运行结束后的审计，`truth_used_for_control` 与"
        "`truth_used_for_decision` 均为 `false`。",
        "",
        "## 在线去除率收敛",
        "",
        "| 工况 | 真实倍率（审计） | 最终估计 | 95% 区间 | 绝对误差 | 道次 | 停因 | 放行 | 误放行 |",
        "| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- |",
    ]
    for item in results["process"]:
        estimate = item["final_estimate"]
        lines.append(
            "| {case} | {truth:.2f} | {rate:.3f} | {low:.3f}–{high:.3f} | "
            "{error:+.3f} | {passes} | {stop} | {released} | {false} |".format(
                case=item["case_id"],
                truth=item["case"]["rate_scale"],
                rate=estimate["relative_rate"],
                low=estimate["lower_95"],
                high=estimate["upper_95"],
                error=item["rate_error"],
                passes=item["pass_count"],
                stop=item["stop_reason"],
                released="是" if item["released"] else "否",
                false="是" if item["false_release"] else "否",
            )
        )
    summary = results["summary"]
    lines.extend(
        [
            "",
            f"收敛到 ±0.15 倍率的运行 {summary['converged_rate_count']} / "
            f"{summary['run_count']}；平均道次 {summary['mean_passes']:.2f}；"
            f"放行 {summary['released_count']}；误放行 "
            f"{summary['false_release_count']}。",
            "",
            "## 隐藏测量偏置拦截",
            "",
            "| 工况 | 真实余量 mm | 注入偏置 mm | 朴素判定 | 校准判定 | 期望 | 校准正确 | naive 误判 |",
            "| --- | ---: | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for item in results["bias"]:
        verdict = item["verdict"]
        lines.append(
            "| {case} | {truth:+.2f} | {bias:+.2f} | {naive} | {calibrated} | "
            "{expected} | {correct} | {wrong} |".format(
                case=item["case_id"],
                truth=item["true_machined_mm"],
                bias=item["injected_bias_mm"],
                naive=verdict["naive_status"],
                calibrated=verdict["status"],
                expected=item["expected_calibrated"],
                correct="是" if item["calibrated_correct"] else "否",
                wrong="是" if item["naive_wrong"] else "否",
            )
        )
    lines.extend(
        [
            "",
            "局部基准由加工带两侧未加工的正常表面锚点估计：稳健中位数给出偏置，"
            "MAD 与锚点 σ 给出不确定度，再传播到加工锚点的 ±2σ 校准区间。"
            "保护区锚点单独核验完好度和局部偏置一致性；任何不一致只会收紧判定。",
            "",
            "## 复现",
            "",
            "```bash",
            "OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \\",
            "  .venv/bin/python -m daiyi_grinding.adaptive_process \\",
            "  --output process_stress_new",
            "```",
            "",
            "## 限制",
            "",
            "- plant 为合成代理：去除率、磨损、温升和柔顺均未由真实磨削标定。",
            "- 在线辨识只估计相对倍率；绝对材料去除系数仍需现场同点配对量测。",
            "- 隐藏偏置场景是构造的合成审计，不代表现场偏置分布。",
            "",
        ]
    )
    return "\n".join(lines)


def _output_path(value):
    root = OUTPUT_ROOT.resolve()
    requested = Path(value)
    candidate = (
        (root / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    if not candidate.is_relative_to(root):
        raise ValueError(f"--output must stay under {root}")
    return candidate


def main(argv=None):
    """Run the offline process adaptation and debiasing stress suite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="new directory under runs/enterprise_offline_20261123/"
        "p1_data_process_01",
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=[0])
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    if any(seed < 0 for seed in args.seeds):
        parser.error("seeds must be nonnegative")
    output = _output_path(args.output)
    results = run_suite(tuple(args.seeds))
    evidence = _write_outputs(
        results,
        output,
        " ".join(
            [
                sys.executable,
                "-m",
                "daiyi_grinding.adaptive_process",
                *arguments,
            ]
        ),
    )
    print(
        json.dumps(
            {
                "output": str(output),
                **evidence["metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
