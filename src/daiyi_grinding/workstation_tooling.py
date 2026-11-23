"""Explicit fixed-tool geometry and process frames for the supplied cell."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FixedTool:
    """A configured abrasive surface; dimensions are simulation parameters."""

    key: str
    label: str
    geom: str
    contact: np.ndarray
    normal: np.ndarray
    half_size: np.ndarray
    source: str

    @property
    def footprint_radius(self):
        return float(np.linalg.norm(self.half_size[1:]))

    def candidates(self):
        if self.key == "wheel":
            for angle in (90, 0, 180, 270):
                yield self.contact + [
                    0,
                    0.019 * np.cos(np.deg2rad(angle)),
                    0.019 * np.sin(np.deg2rad(angle)),
                ]
        else:
            # Contact points stay on the same supplied backing plate.
            for z in (0, 0.055, -0.055, 0.025, -0.025):
                for y in (0, -0.025, 0.025):
                    yield self.contact + [0, y, z]

    def stock_intersection(self, world):
        """Return the active surface's X boundary and swept-volume membership."""
        if self.key == "wheel":
            center = self.contact - [self.half_size[0], 0, 0]
            radial = np.linalg.norm(world[:, 1:] - center[1:], axis=1)
            inside = (
                (radial < self.half_size[1])
                & (world[:, 0] < self.contact[0])
                & (world[:, 0] > center[0] - self.half_size[0])
            )
        else:
            inside = (
                (np.abs(world[:, 1] - self.contact[1]) < self.half_size[1])
                & (np.abs(world[:, 2] - self.contact[2]) < self.half_size[2])
                & (world[:, 0] < self.contact[0])
                & (world[:, 0] > self.contact[0] - 0.012)
            )
        return float(self.contact[0]), inside

    def summary(self):
        return {
            "key": self.key,
            "label": self.label,
            "geom": self.geom,
            "contact_world_m": self.contact.tolist(),
            "normal_world": self.normal.tolist(),
            "half_size_m": self.half_size.tolist(),
            "geometry_source": self.source,
            "real_process_calibrated": False,
        }


def fixed_tool(key="belt"):
    """Keep the historical wheel available, with the latest belt cell default."""
    if key == "wheel":
        return FixedTool(
            "wheel",
            "固定主轴磨轮 · Ø45 × 18 mm",
            "abrasive_wheel",
            np.array([-1.642, -1.125, 1.156]),
            np.array([-1.0, 0.0, 0.0]),
            np.array([0.009, 0.0225, 0.0225]),
            "supplied stiff_spindle_c8.stl; configured front abrasive face",
        )
    if key != "belt":
        raise ValueError(f"Unsupported fixed tool: {key}")
    return FixedTool(
        "belt",
        "固定砂带加工面 · 75 × 140 mm",
        "abrasive_belt",
        np.array([-1.2608838, -0.0024748, 1.1165946]),
        np.array([-1.0, 0.0, 0.0]),
        np.array([0.0005, 0.037596, 0.069995]),
        (
            "1 mm functional abrasive layer in front of the supplied belt CAD "
            "backing plate (x=-1.2618838, y=-0.04007..0.03512, "
            "z=1.04660..1.18659); belt material/thickness/speed are assumptions"
        ),
    )
