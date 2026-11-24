"""Sensor-only features and portable supervised casting-defect inference.

This module never reads semantic labels, generator roots or simulator defect IDs.
The registered normal depth is an explicit input and remains an idealized CAD
registration assumption in the current simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

from . import project
from .perception import Detection
from .thin_reference import _fill_for_geometry, _measure_region

FEATURE_NAMES = (
    "red",
    "green",
    "blue",
    "signed_registered_depth_residual",
    "local_median_residual",
    "local_residual_spread",
    "normal_x_magnitude",
    "normal_y_magnitude",
    "normal_z_magnitude",
    "nominal_depth",
    "measured_depth_valid",
)
FEATURE_VERSION = "rgbd_registered_v1"
REGISTRY_FILE = project.ROOT / "config/learned_detector.json"


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def registered_checkpoint():
    """Only expose a locally frozen, hash-verified checkpoint to the web app."""
    if not REGISTRY_FILE.is_file():
        return None
    registry = json.loads(REGISTRY_FILE.read_text())
    checkpoint = project.ROOT / registry["checkpoint"]
    if (
        not checkpoint.is_file()
        or file_sha256(checkpoint) != registry["checkpoint_sha256"]
        or registry["feature_version"] != FEATURE_VERSION
    ):
        raise ValueError("已登记模型文件或输入版本不一致，请重新验证模型")
    return checkpoint.resolve()


def registered_model_hashes():
    if not REGISTRY_FILE.is_file():
        return {}
    checkpoint = registered_checkpoint()
    return {
        "learned_detector.json": file_sha256(REGISTRY_FILE),
        "registered_detector_model.pt": file_sha256(checkpoint),
    }


def sensor_features(rgb, depth, reference_depth, intrinsics):
    """Eleven measured/CAD channels, with no labels or entity coordinates."""
    if rgb.shape[:2] != depth.shape or reference_depth.shape != depth.shape:
        raise ValueError("RGB-D and nominal reference must have the same shape")
    reference, _ = _fill_for_geometry(reference_depth)
    valid = np.isfinite(depth) & (depth > 0.05) & (depth < 10)
    observed = np.where(valid, depth, reference).astype(np.float32)
    residual = (reference - observed) * 1000
    residual = np.clip(residual, -12, 24).astype(np.float32)
    median = cv2.medianBlur(residual, 3)
    average = cv2.blur(residual, (5, 5))
    variance = np.maximum(
        cv2.blur(residual * residual, (5, 5)) - average * average, 0
    )
    rows, cols = np.indices(depth.shape, dtype=np.float32)
    x = (cols - intrinsics[0, 2]) * reference / intrinsics[0, 0]
    y = (rows - intrinsics[1, 2]) * reference / intrinsics[1, 1]
    xyz = np.stack((x, y, reference), axis=-1)
    normals = np.cross(np.gradient(xyz, axis=1), np.gradient(xyz, axis=0))
    normals /= np.maximum(np.linalg.norm(normals, axis=-1, keepdims=True), 1e-9)
    channels = [
        *[
            (rgb[..., index].astype(np.float32) / 255 - 0.5) * 2
            for index in range(3)
        ],
        residual / 6,
        median / 6,
        np.sqrt(variance) / 6,
        *[np.abs(normals[..., index]) for index in range(3)],
        np.clip(reference - 1, -1, 2),
        valid.astype(np.float32),
    ]
    return np.nan_to_num(np.stack(channels), nan=0, posinf=0, neginf=0).astype(
        np.float32
    )


def tile_origins(length, tile_size=256, stride=192):
    if length <= tile_size:
        return [0]
    return sorted(
        set([*range(0, length - tile_size + 1, stride), length - tile_size])
    )


class LearnedDetector:
    """Frozen U-Net weights and validation-selected probability threshold."""

    def __init__(self, checkpoint, device=None, threshold=None):
        import torch

        from .defect_network import DefectUNet

        self.path = Path(checkpoint).resolve()
        self.sha256 = file_sha256(self.path)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))
        package = torch.load(self.path, map_location="cpu", weights_only=True)
        if package.get("feature_version") != FEATURE_VERSION:
            raise ValueError(
                "Checkpoint feature schema does not match inference"
            )
        config = package["model_config"]
        self.model = DefectUNet(config["channels"], config["width"])
        self.model.load_state_dict(package["state_dict"], strict=True)
        self.model.eval().to(self.device)
        self.threshold = float(
            package["probability_threshold"] if threshold is None else threshold
        )
        self.package = {
            key: value for key, value in package.items() if key != "state_dict"
        }

    def metadata(self):
        return {
            "implementation": "trained RGB-D DefectUNet",
            "checkpoint": str(self.path),
            "checkpoint_sha256": self.sha256,
            "feature_version": FEATURE_VERSION,
            "features": list(FEATURE_NAMES),
            "model_config": self.package["model_config"],
            "probability_threshold": self.threshold,
            "registered_nominal_depth_required": True,
            "trained_on_real_scans": False,
            "truth_used_for_inference": False,
        }

    def probabilities(self, rgb, depth, reference_depth, intrinsics):
        import torch

        features = sensor_features(rgb, depth, reference_depth, intrinsics)
        height, width = depth.shape
        tile = 256
        padded = np.pad(
            features,
            ((0, 0), (0, max(0, tile - height)), (0, max(0, tile - width))),
            mode="edge",
        )
        origins = [
            (row, col)
            for row in tile_origins(padded.shape[1])
            for col in tile_origins(padded.shape[2])
        ]
        probabilities = np.zeros((3, *padded.shape[1:]), np.float32)
        weights = np.zeros(padded.shape[1:], np.float32)
        taper = np.maximum(np.hanning(tile), 0.1).astype(np.float32)
        blend = taper[:, None] * taper[None]
        with torch.inference_mode():
            for start in range(0, len(origins), 8):
                positions = origins[start : start + 8]
                patches = np.stack(
                    [
                        padded[:, row : row + tile, col : col + tile]
                        for row, col in positions
                    ]
                )
                tensor = torch.from_numpy(patches).to(self.device)
                values = self.model(tensor).softmax(dim=1).cpu().numpy()
                for (row, col), value in zip(positions, values):
                    probabilities[:, row : row + tile, col : col + tile] += (
                        value * blend
                    )
                    weights[row : row + tile, col : col + tile] += blend
        return (probabilities / np.maximum(weights, 1e-9))[:, :height, :width]

    def detect(self, rgb, depth, reference_depth, observation):
        """Measure learned components on the existing permitted nominal chart.

        Semantic prediction covers the full RGB-D view, independently of the old
        detector's positive mask. Only measured, non-edge nominal surfaces can
        yield machining candidates; all robot commands still need cell preflight.
        """
        started = time.monotonic()
        probabilities = self.probabilities(
            rgb, depth, reference_depth, observation["K"]
        )
        classes = probabilities.argmax(axis=0)
        foreground = probabilities[1:].max(axis=0)
        measurable = observation["surface"] & observation["selection"]
        residual = observation["residual"]
        # This is a metrology guard against normal-surface noise, not a reuse of
        # the old threshold/connected-components candidate detector.
        positive = residual > max(0.00010, observation["noise_sigma_mm"] / 1000)
        candidate = (foreground >= self.threshold) & measurable & positive
        detections = []
        mask = np.zeros(depth.shape, bool)
        for label, kind in ((1, "seam"), (2, "burr")):
            components, count = ndimage.label(
                candidate & (classes == label),
                structure=np.ones((3, 3), np.uint8),
            )
            for identity in range(1, count + 1):
                component = components == identity
                pixels = np.argwhere(component)
                if len(pixels) < 6:
                    continue
                points = observation["xyz"][tuple(pixels.T)]
                length, width, height = _measure_region(
                    points, pixels, component, residual, observation["scale"]
                )
                mask[tuple(pixels.T)] = True
                detections.append(
                    Detection(
                        id=len(detections) + 1,
                        kind=kind,
                        pixels=pixels,
                        xyz=points,
                        length_mm=length,
                        width_mm=width,
                        height_mm=height,
                        confidence=float(
                            probabilities[label][tuple(pixels.T)].mean()
                        ),
                        reason="supervised RGB-D mask with independently measured CAD excess; robot safety pending",
                    )
                )
        observation["mask"] = mask
        observation["learned_detector"] = {
            **self.metadata(),
            "inference_seconds": time.monotonic() - started,
            "raw_foreground_pixels": int((foreground >= self.threshold).sum()),
            "measurable_foreground_pixels": int(mask.sum()),
        }
        return detections, probabilities
