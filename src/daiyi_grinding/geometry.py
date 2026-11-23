"""Procedural cast flange coupons. Ground truth is exported ONLY to the evaluator.

Dimensions are experimental assumptions, not measurements of Daiyi parts.
The height field represents top-visible geometry; undercuts require another model.
"""

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter


@dataclass
class Coupon:
    height: np.ndarray
    labels: np.ndarray
    center: np.ndarray
    halfsize: tuple
    seed: int
    case: str


def generate(seed=7, case="mixed", resolution=(257, 353)):
    rng = np.random.default_rng(seed)
    ny, nx = resolution
    hx, hy = 0.135, 0.100
    x, y = np.meshgrid(np.linspace(-hx, hx, nx), np.linspace(-hy, hy, ny))
    center = np.array(
        [
            -0.49 + rng.uniform(-0.012, 0.012),
            -0.13 + rng.uniform(-0.012, 0.012),
            0.600,
        ]
    )
    # Drafted casting edge and smoothly varying as-cast surface.
    rounded = (np.maximum(np.abs(x) - 0.100, 0) / 0.024) ** 2 + (
        np.maximum(np.abs(y) - 0.065, 0) / 0.024
    ) ** 2
    inside = rounded < 1
    h = 0.025 + 0.0012 * np.sin(14 * x + seed) + 0.0006 * np.cos(24 * y)
    h += gaussian_filter(rng.normal(0, 0.00022, (ny, nx)), 0.65)
    h = np.where(inside, h, 0.003)
    # A recessed opening, a broad design rib and a bolt boss are normal features.
    pocket = ((x / 0.040) ** 2 + ((y - 0.003) / 0.027) ** 2) < 1
    h[pocket] -= 0.012
    rib = (
        0.0042
        * np.exp(-(((y + 0.030) / 0.008) ** 4))
        * np.exp(-(((x + 0.005) / 0.09) ** 8))
    )
    boss = 0.005 * np.clip(
        (0.019 - np.hypot(x - 0.074, y + 0.051)) / 0.003, 0, 1
    )
    normal = (rib > 0.0006) | (boss > 0.0006)
    h += (rib + boss) * inside
    labels = np.zeros((ny, nx), np.uint8)
    labels[normal] = 3
    if case in ("mixed", "seam", "thin", "coarse", "noisy"):
        phase = rng.uniform(-1, 1)
        seam_y = (
            0.058
            + 0.005 * np.sin(x * 37 + phase)
            + 0.001 * np.sin(x * 211 + phase)
        )
        width = rng.uniform(0.0007, 0.0013) * (1 + 0.25 * np.sin(94 * x + seed))
        length = (x > rng.uniform(-0.111, -0.095)) & (
            x < rng.uniform(0.082, 0.103)
        )
        # A thin, asymmetric fin with irregular height, taper and small interruptions.
        profile = np.maximum(0, 1 - np.abs(y - seam_y) / width)
        ridge = (
            profile
            * (
                0.0027
                + 0.0008 * np.sin(x * 137 + seed)
                + 0.0003 * np.sin(x * 483)
            )
            * length
        )
        if case == "thin":
            ridge *= 0.38
        h += ridge
        labels[ridge > 0.00023] = 1
    if case in ("mixed", "burr", "noisy"):
        for cx, cy in [(-0.073, -0.061), (0.095, 0.013)]:
            cx += rng.uniform(-0.007, 0.007)
            cy += rng.uniform(-0.008, 0.008)
            r = np.hypot((x - cx), 1.2 * (y - cy))
            a = np.arctan2(y - cy, x - cx)
            radius = rng.uniform(0.004, 0.006) * (
                1 + 0.17 * np.sin(5 * a + seed) + 0.1 * np.sin(9 * a)
            )
            bump = np.maximum(
                0, 1 - r / np.maximum(radius, 1e-6)
            ) ** 0.7 * rng.uniform(0.002, 0.0045)
            h += bump
            labels[bump > 0.0003] = 2
    if case == "coarse":
        stump = (
            np.maximum(0, 1 - np.hypot(x + 0.065, y + 0.064) / 0.009) ** 0.45
            * 0.014
        )
        h += stump
        labels[stump > 0.001] = 4
    if case not in {
        "mixed",
        "burr",
        "seam",
        "normal",
        "thin",
        "coarse",
        "noisy",
    }:
        raise ValueError(case)
    return Coupon(h.astype(np.float32), labels, center, (hx, hy), seed, case)
