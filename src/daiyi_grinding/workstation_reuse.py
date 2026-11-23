"""Run the pinned, observation-only WeldGrind filter in this MuJoCo cell."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import numpy as np

from . import project

UPSTREAM_COMMIT = "0e110bf14952cf89ef127342d6a10932d151fa62"
UPSTREAM = project.ROOT / "external/WeldGrind-V1"
FILTER_MODULES = (
    "physical_trajectory_filter.py",
    "background_surface.py",
    "pointcloud_surface.py",
    "predicted_rgbd_cloud.py",
)


def provenance():
    """Record what is actually available, distinct from reported model scores."""
    module_dir = UPSTREAM / "src/weldgrind_v1"
    hashes = {
        name: hashlib.sha256((module_dir / name).read_bytes()).hexdigest()
        for name in FILTER_MODULES
        if (module_dir / name).is_file()
    }
    return {
        "repository": "https://github.com/CharlesH777/WeldGrind-V1",
        "commit": UPSTREAM_COMMIT,
        "available": len(hashes) == len(FILTER_MODULES),
        "module_sha256": hashes,
        "reused": "original CPU physical_trajectory_filter, local surface constraints and optional original RGB-D backprojection",
        "trained_trajectory_checkpoint_available": False,
        "sam3_weights_available": False,
        "reported_phase_accuracy_percent": 95.42,
        "reported_parameter_count_million": 14.91,
        "reported_scores_reproduced": False,
        "score_scope": "95.42% is motion-phase accuracy; it is not defect detection accuracy",
    }


def filter_path(points, cloud, camera_position, roi_mask=None):
    """Filter measured candidate points; all three arguments are in meters."""
    if not provenance()["available"]:
        raise FileNotFoundError(
            "缺少曹正恒原版后处理源码，请运行 scripts/fetch_weldgrind.py"
        )
    directory = str(UPSTREAM / "src")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    module = importlib.import_module("weldgrind_v1.physical_trajectory_filter")
    config = module.PhysicalFilterConfig(
        max_correction_mm=0.35,
        max_removal_depth_mm=None,
        max_iterations=160,
        tolerance_mm=0.0002,
        global_continuity_guard=True,
        background=module.BackgroundSurfaceConfig(
            max_residual_sigma_mm=0.2,
            max_annulus_width_mm=6.0,
            max_points=4096,
        )
        if roi_mask is not None
        else None,
    )
    points = np.asarray(points)
    result = module.filter_trajectory(
        points * 1000,
        np.asarray(cloud) * 1000,
        predicted_phases=np.zeros(len(points), dtype=np.int64),
        camera_position_mm=np.asarray(camera_position) * 1000,
        config=config,
        roi_mask=roi_mask,
    )
    return result["trajectory_filtered_mm"] / 1000, result["report"]


def backproject_observation(
    rgb, depth_m, intrinsics, camera_world_m, mask, seed
):
    """Execute Cao's original adapter with our actual predicted ROI and units.

    The current ROI is geometric, not a SAM prediction. No labels, nominal
    defect dimensions or trajectories are accepted by this function.
    """
    directory = str(UPSTREAM / "src")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    module = importlib.import_module("weldgrind_v1.predicted_rgbd_cloud")
    transform = np.asarray(camera_world_m, float).copy()
    transform[:3, 3] *= 1000
    camera = {
        "capture_status": "accepted",
        "world_units": "mm",
        "image_size_wh": [rgb.shape[1], rgb.shape[0]],
        "depth_definition": "distance_to_image_plane, along optical +Z",
        "pixel_center_offset": 0.0,
        "intrinsics_K": np.asarray(intrinsics).tolist(),
        "world_from_camera_cv_mm": transform.tolist(),
    }
    if not (mask & np.isfinite(depth_m) & (depth_m > 0)).any():
        return {
            "points": np.empty((0, 6), np.float32),
            "roi_mask": np.empty(0, bool),
            "point_uv": np.empty((0, 2), np.int32),
            "units": np.array("mm"),
            "coordinate_frame": np.array("world"),
            "roi_source": np.array("geometric_reference_detector"),
        }, {
            "adapter_executed": False,
            "reason": "no predicted positive-depth ROI; no cloud invented",
            "point_count": 0,
            "sam_model_executed": False,
            "gt_defect_or_instance_labels_read": False,
        }
    arrays, metadata = module.backproject_predicted_rgbd(
        rgb,
        np.asarray(depth_m) * 1000,
        camera,
        np.asarray(mask, bool),
        seed=int(seed),
    )
    metadata.update(
        adapter_executed=True,
        upstream_commit=UPSTREAM_COMMIT,
        roi_source="geometric_reference_detector",
        pointcloud_source="original_weldgrind_adapter_current_geometric_roi",
        source_depth_preprocessing="current nominal-CAD surface guard and geometric candidate mask; no defect truth",
        source_depth_is_object_foreground_cropped=True,
        sam_model_executed=False,
        pixel_convention_scope="current MuJoCo K uses integer pixel coordinates; original data uses explicit 0.5",
        roi_sampling="all valid geometric prediction ROI pixels up to cap; uniform without replacement when capped",
        context_sampling="uniform valid depth pixels outside the complete geometric prediction; no ROI pixel duplication",
        intended_scope="trajectory input for existing geometric candidates; new perception training must use full public RGB-D, not only these candidate clouds",
    )
    arrays.update(
        roi_source=np.array(metadata["roi_source"]),
        pointcloud_source=np.array(metadata["pointcloud_source"]),
        source_depth_preprocessing=np.array(
            metadata["source_depth_preprocessing"]
        ),
        pointcloud_metadata=np.array(json.dumps(metadata, sort_keys=True)),
    )
    return arrays, metadata


def write_provenance(output):
    Path(output).write_text(
        json.dumps(provenance(), ensure_ascii=False, indent=2) + "\n"
    )
