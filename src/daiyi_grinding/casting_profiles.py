"""Versioned casting priors, kept distinct from measured 097 calibration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import numpy as np

from . import project

BASE_PROFILE = "research_casting_v1"
TEAM_PROFILE = "team_prior_20261121"
PROFILE_FILES = {
    BASE_PROFILE: project.ROOT / "config/casting_synthesis.json",
    TEAM_PROFILE: project.ROOT / "config/casting_team_prior_20261121.json",
}
PROFILE_LABELS = {
    BASE_PROFILE: "研究合成 v1 · 原始参数",
    TEAM_PROFILE: "杨的方法 + 曹的数据先验",
}


def available_profiles():
    return tuple(key for key, path in PROFILE_FILES.items() if path.is_file())


def load_profile(profile=BASE_PROFILE):
    """Load an explicit profile without mutating global generation settings."""
    if isinstance(profile, dict):
        settings = deepcopy(profile)
    else:
        if profile not in PROFILE_FILES:
            raise ValueError(f"Unknown casting profile: {profile}")
        path = PROFILE_FILES[profile]
        if not path.is_file():
            raise ValueError(f"Casting profile is not prepared: {profile}")
        settings = json.loads(path.read_text())
    if settings.get("coordinate_frame") != "workpiece_097":
        raise ValueError("Casting profile must identify the 097 metric frame")
    for family, names in {
        "variable_flash": ("length_mm", "height_mm", "full_width_mm"),
        "interrupted_flash": (
            "length_mm",
            "height_mm",
            "full_width_mm",
            "gap_mm",
        ),
        "curled_flash": (
            "length_mm",
            "sheet_arc_mm",
            "thickness_mm",
            "bend_radians",
        ),
        "torn_flash": (
            "length_mm",
            "sheet_arc_mm",
            "thickness_mm",
            "bend_radians",
            "gap_mm",
        ),
        "irregular_burr": ("footprint_diameter_mm", "height_mm"),
    }.items():
        for name in names:
            interval = np.asarray(settings[family][name], float)
            if (
                interval.shape != (2,)
                or not np.isfinite(interval).all()
                or not 0 < interval[0] < interval[1]
            ):
                raise ValueError(f"Invalid {family}.{name} interval")
    sensor = settings.get("sensor_prior")
    if sensor is not None and (
        not 0 < sensor["depth_noise_sigma_mm"] < 2
        or not 0 <= sensor["dropout_probability"] < 0.5
    ):
        raise ValueError("Invalid synthetic sensor prior")
    return settings


def profile_hashes():
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in PROFILE_FILES.values()
        if path.is_file()
    }


def profile_provenance(settings):
    return {
        "key": settings["profile"],
        "label": PROFILE_LABELS.get(settings["profile"], settings["profile"]),
        "real_scan_fitted": settings.get("real_scan_fitted", False),
        "parting_curve_status": settings["parting_curve_status"],
        "calibration_status": settings.get(
            "calibration_status", "research assumptions; no real scans fitted"
        ),
        "parameter_sources": settings.get("parameter_sources", {}),
        "dataset_binding": settings.get("dataset_binding"),
        "sensor_prior": settings.get("sensor_prior"),
        "trained_model_used": False,
    }


def sample_compact_prior(settings, rng):
    """Sample one paired height/radii record; do not break their covariance."""
    rows = settings.get("compact_excess_prior", {}).get(
        "training_instances", []
    )
    if not rows:
        return None
    row = rows[int(rng.integers(len(rows)))]
    dimensions = np.asarray(
        [row["height_mm"], *row["radii_uv_mm"]], dtype=float
    )
    if not np.isfinite(dimensions).all() or (dimensions <= 0).any():
        raise ValueError("Invalid compact-excess training instance")
    if row.get("optimizer_split") != "train":
        raise ValueError("Only training instances may define a synthesis prior")
    return deepcopy(row)


def sensor_for_profile(settings, seed):
    prior = settings.get("sensor_prior")
    if prior is None:
        return None
    effective = deepcopy(prior)
    components = prior.get("components", [])
    if components:
        weights = np.asarray([item["weight"] for item in components], float)
        if (
            not np.isfinite(weights).all()
            or (weights < 0).any()
            or weights.sum() <= 0
        ):
            raise ValueError("Invalid sensor mixture weights")
        rng = np.random.default_rng(seed + prior["seed_offset"])
        index = int(rng.choice(len(components), p=weights / weights.sum()))
        effective.update(components[index], component_index=index)
    return effective
