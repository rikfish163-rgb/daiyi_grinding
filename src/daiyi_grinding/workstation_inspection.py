"""Fixed-camera, encoder-indexed RGB-D inspection of the complete CAD casting."""

from __future__ import annotations

import copy
import time
from dataclasses import replace

import mujoco
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree

from .perception import overlay, project, roi_mask, unproject
from .planning import constrain_roi
from .thin_reference import THIN_REFERENCE_CONFIG, detect_thin_reference
from .workstation_evaluation import aggregate_views, evaluate_view
from .workstation_model import CELL_CONFIG, write_json
from .workstation_quality import Reinspection, surface_anchors
from .workstation_regrind import additional_regrind_paths, regrind_paths
from .workstation_reuse import filter_path

BOUNDS = ([-0.29, -0.23, 0.04], [0.23, 0.23, 0.41])
CELL_DETECTOR_CONFIG = replace(
    THIN_REFERENCE_CONFIG, minimum_pixels=6, component_join_mm=2.0
)
NORMAL_HEIGHT_QUANTILE = 0.8
REINSPECTION_DEPTH_FRAMES = 9
MAX_REINSPECTION_DEPTH_FRAMES = 25


def sample_depth(ideal, rng, sigma_m, dropout, frame_count, previous_frames=()):
    """Acquire independent simulated depths at one fixed encoder pose."""
    frames = list(previous_frames)
    for _ in range(len(frames), frame_count):
        frame = ideal + rng.normal(0, sigma_m, ideal.shape)
        frame[rng.random(frame.shape) < dropout] = np.nan
        frames.append(frame)
    frames = np.stack(frames)
    counts = np.isfinite(frames).sum(axis=0)
    depth = np.full(ideal.shape, np.nan)
    np.divide(
        np.nansum(frames, axis=0),
        counts,
        out=depth,
        where=counts > 0,
    )
    return depth, frames, counts


def surface_normals(reference_xyz, camera):
    """Camera-oriented normals from the registered visible CAD surface."""
    down = np.gradient(reference_xyz, axis=0)
    right = np.gradient(reference_xyz, axis=1)
    normals = np.cross(right, down)
    lengths = np.linalg.norm(normals, axis=-1, keepdims=True)
    normals /= np.maximum(lengths, 1e-12)
    toward_camera = camera[:3, 3] - reference_xyz
    normals[np.einsum("ijk,ijk->ij", normals, toward_camera) < 0] *= -1
    return normals


def surface_permission(points, first_k, first_t, first_depth, permission):
    """Bind image permission to the same visible 3D surface across table views."""
    flat = points.reshape(-1, 3)
    uv = project(flat, first_k, first_t)
    finite = np.isfinite(uv).all(axis=1)
    uv = np.nan_to_num(uv, nan=-1, posinf=-1, neginf=-1)
    uv = np.clip(uv, -1e6, 1e6).round().astype(int)
    height, width = first_depth.shape
    inside = (
        finite
        & (uv[:, 0] >= 0)
        & (uv[:, 0] < width)
        & (uv[:, 1] >= 0)
        & (uv[:, 1] < height)
    )
    allowed = np.zeros(len(uv), bool)
    first_camera = np.linalg.inv(first_t)
    projected_depth = (flat @ first_camera[:3, :3].T + first_camera[:3, 3])[
        :, 2
    ]
    u, v = uv[inside].T
    allowed[inside] = (
        permission[v, u]
        & (projected_depth[inside] > 0)
        & (np.abs(first_depth[v, u] - projected_depth[inside]) < 0.003)
    )
    return allowed.reshape(points.shape[:-1])


def observed_normal_height_mm(
    detection, observation, quantile=NORMAL_HEIGHT_QUANTILE
):
    """Estimate excess along visible CAD normals using only measured points.

    Optical-axis residuals can exceed machining tolerance in oblique views.
    Keep those detection pixels for scoring; decide machining from this robust
    normal component, independently of generated defect or removal labels.
    """
    rows, cols = detection.pixels.T
    displacement = (
        observation["xyz"][rows, cols]
        - observation["reference_xyz"][rows, cols]
    )
    normals = observation["normals"][rows, cols]
    lengths = np.linalg.norm(normals, axis=1)
    valid = (
        np.isfinite(displacement).all(axis=1)
        & np.isfinite(normals).all(axis=1)
        & (lengths > 0.9)
    )
    if not valid.any():
        return None
    heights = np.einsum(
        "ij,ij->i", displacement[valid], normals[valid] / lengths[valid, None]
    )
    positive = heights[heights > 0]
    return (
        float(np.quantile(positive, quantile) * 1000) if len(positive) else 0.0
    )


def machining_required(detection, observation):
    height = observed_normal_height_mm(detection, observation)
    return bool(
        detection.kind in {"seam", "burr"}
        and height is not None
        and height > CELL_CONFIG["planning"]["target_remaining_height_mm"]
    )


def apply_review_envelope(detections, observation):
    """Hold excessive measured stock for review independently of model class."""
    protected = []
    for detection in detections:
        height = observed_normal_height_mm(
            detection, observation, quantile=0.95
        )
        if (
            detection.kind in {"seam", "burr"}
            and height is not None
            and height > CELL_DETECTOR_CONFIG.review_height_mm
        ):
            detection = replace(
                detection,
                kind="review",
                reason=(
                    "measured CAD-normal excess (95th percentile) "
                    f"{height:.3f} mm exceeds the "
                    f"{CELL_DETECTOR_CONFIG.review_height_mm:.3f} mm "
                    "deburring review envelope; no automatic machining path"
                ),
            )
        protected.append(detection)
    return protected


def exclude_review_footprint(paths, reference_points, footprint_radius):
    """Keep the entire configured tool footprint away from measured review stock."""
    if not len(reference_points):
        return paths, []
    tree = cKDTree(reference_points)
    admitted, blocked = [], []
    for path in paths:
        distance = float(tree.query(np.asarray(path["nominal_xyz"]))[0].min())
        if distance <= footprint_radius + 0.001:
            blocked.append(
                {
                    "detection_id": path["detection_id"],
                    "distance_to_review_mm": distance * 1000,
                    "reason": "configured tool footprint intersects measured review region",
                }
            )
        else:
            admitted.append(path)
    return admitted, blocked


def observed_paths(detection, observation):
    """Ordered local contact paths with observed normals and bounded smoothing."""
    if not machining_required(detection, observation):
        return []
    rows, cols = detection.pixels.T
    nominal = observation["reference_xyz"][rows, cols]
    raw = observation["xyz"][rows, cols]
    normals = observation["normals"][rows, cols]
    normal = np.median(normals, axis=0)
    normal /= max(np.linalg.norm(normal), 1e-12)
    if np.linalg.norm(normal) < 0.9:
        return []
    center = nominal.mean(axis=0)
    tangent_points = nominal - center
    tangent_points -= np.outer(tangent_points @ normal, normal)
    _, _, axes = np.linalg.svd(tangent_points, full_matrices=False)
    tangent = axes[0]
    coordinates = tangent_points @ tangent
    if np.ptp(coordinates) < 0.0015:
        return []
    samples = np.arange(coordinates.min(), coordinates.max() + 0.0001, 0.0015)
    nominal_path, observed_path = [], []
    for value in samples:
        selected = np.abs(coordinates - value) < 0.0017
        if selected.sum() < 2:
            continue
        base = np.mean(nominal[selected], axis=0)
        height = np.quantile((raw[selected] - nominal[selected]) @ normal, 0.8)
        nominal_path.append(base)
        observed_path.append(base + normal * max(0, height))
    if len(nominal_path) < 2:
        return []
    nominal_path = np.asarray(nominal_path)
    observed_path = np.asarray(observed_path)
    # Preserve all defect pixels; only the surrounding context is subsampled.
    patch_mask = np.zeros(observation["mask"].shape, bool)
    patch_mask[rows, cols] = True
    patch_mask = ndimage.binary_dilation(patch_mask, iterations=16)
    context = np.argwhere(
        patch_mask & observation["surface"] & ~observation["mask"]
    )
    if len(context) > 1800:
        context = context[np.linspace(0, len(context) - 1, 1800, dtype=int)]
    context_points = (
        observation["xyz"][tuple(context.T)]
        if len(context)
        else np.empty((0, 3))
    )
    cloud = np.concatenate([raw, context_points])
    cloud_roi = np.r_[
        np.ones(len(raw), bool), np.zeros(len(context_points), bool)
    ]
    filtered, report = filter_path(
        nominal_path, cloud, observation["T"][:3, 3], cloud_roi
    )
    # Upstream smoothing does not confer machining safety. Reproject to the
    # measured nominal chart before the full carried-object collision check.
    tree = cKDTree(nominal)
    _, nearest = tree.query(filtered)
    correction = np.einsum("ij,j->i", nominal[nearest] - filtered, normal)
    filtered += correction[:, None] * normal
    tool = observation["tool"]
    allowed = constrain_roi(
        filtered, observation, margin_m=tool.footprint_radius + 0.001
    )
    paths = []
    for part in allowed:
        jumps = (
            np.flatnonzero(
                np.linalg.norm(np.diff(part, axis=0), axis=1) > 0.005
            )
            + 1
        )
        for continuous in np.split(part, jumps):
            if len(continuous) < 2:
                continue
            arc = np.r_[
                0,
                np.cumsum(np.linalg.norm(np.diff(continuous, axis=0), axis=1)),
            ]
            boundaries = np.arange(0, arc[-1] + 0.032, 0.032)
            for start, end in zip(boundaries[:-1], boundaries[1:]):
                chosen = (arc >= start - 0.0016) & (arc <= end + 0.0016)
                points = continuous[chosen]
                if len(points) < 2:
                    continue
                paths.append(
                    {
                        "detection_id": detection.id,
                        "kind": detection.kind,
                        "xyz": points.tolist(),
                        "nominal_xyz": points.tolist(),
                        "observed_xyz": observed_path.tolist(),
                        "normal": normal.tolist(),
                        "normal_height_mm": observed_normal_height_mm(
                            detection, observation
                        ),
                        "machining_required": True,
                        "coordinate_frame": "workpiece_097",
                        "clearance_mm": 3,
                        "material_allowance_mm": CELL_CONFIG["planning"][
                            "material_allowance_mm"
                        ],
                        "tool_diameter_mm": tool.footprint_radius * 2000,
                        "tool_width_mm": tool.half_size[1] * 2000,
                        "recipe": {
                            "tool": tool.key,
                            "feed_mm_s": 10 if detection.kind == "seam" else 7,
                            "force_target_n": None,
                            "overlap": None,
                        },
                        "filter": {
                            "implementation": "original WeldGrind-V1 CPU filter",
                            **report,
                        },
                        "root_source": "registered CAD depth at detected pixels; no generator coordinates",
                    }
                )
    return paths


def anchors_overlap(first, second):
    """Check common CAD support before merging centroids in hybrid mode."""
    if not len(first[0]) or not len(second[0]):
        return False
    small, large = (
        (first, second) if len(first[0]) <= len(second[0]) else (second, first)
    )
    distance, nearest = cKDTree(large[0]).query(small[0])
    aligned = np.einsum("ij,ij->i", small[1], large[1][nearest]) > 0.8
    return bool(np.mean((distance <= 0.003) & aligned) >= 0.4)


def hybrid_candidates(geometric, learned, shape):
    """Keep geometric seams and review regions; admit distinct network defects."""
    preferred = [d for d in geometric if d.kind in {"seam", "review"}]
    protected = np.zeros(shape, dtype=bool)
    for candidate in preferred:
        protected[tuple(candidate.pixels.T)] = True
    occupied = ndimage.binary_dilation(protected, iterations=1)
    candidates = preferred + [
        d for d in learned if not occupied[tuple(d.pixels.T)].any()
    ]
    return [replace(d, id=index) for index, d in enumerate(candidates, 1)]


class Inspection:
    """Incremental region prediction and independent scoring at each table view."""

    def __init__(
        self,
        sim,
        seed,
        roi=None,
        selected_ids=None,
        revision=0,
        prefix="",
        publish=None,
        learned_detector=None,
        hybrid=False,
        targets=None,
        permitted_paths=None,
        whole_workpiece=True,
        targeted_rescan=False,
        removal_rate_mm_s=None,
    ):
        self.sim, self.seed, self.roi = sim, seed, roi
        self.selected_ids = selected_ids
        self.revision = revision
        self.prefix = prefix
        self.publish = publish or (lambda state: None)
        self.learned_detector = learned_detector
        self.hybrid = hybrid
        self.whole_workpiece = whole_workpiece
        self.targeted_rescan = targeted_rescan
        self.removal_rate_mm_s = removal_rate_mm_s
        self.reinspection = (
            Reinspection(targets) if targets is not None else None
        )
        self.permitted_paths = permitted_paths or []
        self.geometric_review = []
        self.review_points = []
        self.protected_observations = []
        self.regions = []
        self.views = []
        self.scores = []
        self.first_projection = None
        self.started = time.monotonic()
        self.reference_points = []

    def rotate(self, angle):
        """Integrate the turntable servo and read its encoder; keep camera fixed."""
        sim = self.sim
        start = float(sim.data.qpos[sim.table_qadr])
        target = np.deg2rad(angle)
        duration = max(
            0.25,
            abs(target - start)
            / np.deg2rad(CELL_CONFIG["turntable"]["speed_deg_s"])
            * 1.875,
        )
        begin = float(sim.data.time)
        next_publish = begin
        while sim.data.time - begin < duration + 0.25:
            amount = np.clip((sim.data.time - begin) / duration, 0, 1)
            blend = amount**3 * (10 - 15 * amount + 6 * amount**2)
            velocity = (
                30 * amount**2 * (1 - amount) ** 2 / duration * (target - start)
            )
            sim.data.ctrl[sim.table_actuator] = (
                start + (target - start) * blend + 120 / 4000 * velocity
            )
            sim.data.ctrl[sim.arm_actuators] = sim.home
            sim.apply_gravity_feedforward()
            mujoco.mj_step(sim.model, sim.data)
            if sim.data.time >= next_publish:
                mujoco.mj_forward(sim.model, sim.data)
                state = sim.state(
                    "scan",
                    prediction_revision=self.revision,
                    view_count=len(self.views),
                )
                self.publish(state)
                sim.replay_recording.append(state)
                next_publish += 0.2
        mujoco.mj_forward(sim.model, sim.data)
        error = abs(float(sim.data.qpos[sim.table_qadr]) - target)
        if error > np.deg2rad(0.1):
            raise RuntimeError(
                f"转台编码器误差 {np.rad2deg(error):.3f}°，拒绝使用该观测"
            )

    def _merge(self, detection, paths, observation, view_index):
        center = detection.xyz.mean(axis=0)
        matching = None
        anchors = surface_anchors(detection, observation)
        for entry in self.regions:
            previous = entry["detection"]
            if detection.kind != previous.kind:
                continue
            threshold = (
                0.012
                if detection.kind != "seam"
                else max(0.014, previous.length_mm / 1000 * 0.55)
            )
            if np.linalg.norm(
                center - previous.xyz.mean(axis=0)
            ) < threshold and (
                not self.hybrid or anchors_overlap(anchors, entry["anchors"])
            ):
                matching = entry
                break
        normals = observation["normals"][tuple(detection.pixels.T)]
        direction = observation["T"][:3, 3] - center
        direction /= np.linalg.norm(direction)
        view_quality = len(detection.pixels) * max(
            0.1, float(np.median(normals @ direction))
        )
        if matching is None:
            identity = len(self.regions) + 1
            matching = {"id": identity, "quality": -1, "view_indices": []}
            self.regions.append(matching)
        detection = replace(detection, id=matching["id"])
        for path in paths:
            path["detection_id"] = detection.id
            path["view_index"] = view_index
        matching["view_indices"].append(view_index)
        if view_quality > matching["quality"]:
            matching.update(
                detection=detection,
                paths=paths,
                quality=view_quality,
                best_view=view_index,
                normal_height_mm=observed_normal_height_mm(
                    detection, observation
                ),
                machining_required=machining_required(detection, observation),
                anchors=anchors,
            )

    def registered_targets(self, *, include_unselected=False):
        return [
            {
                "id": e["id"],
                "kind": e["detection"].kind,
                "points": e["anchors"][0],
                "normals": e["anchors"][1],
            }
            for e in self.regions
            if e["detection"].kind in {"seam", "burr", "review"}
            and (
                e["detection"].kind == "review"
                or include_unselected
                or self.selected_ids is None
                or e["id"] in self.selected_ids
            )
        ]

    def paths(self):
        paths = []
        for entry in self.regions:
            if self.selected_ids is None or entry["id"] in self.selected_ids:
                paths.extend(entry["paths"])
        review_points = (
            np.concatenate(self.review_points)
            if self.review_points
            else np.empty((0, 3))
        )
        paths, blocked = exclude_review_footprint(
            paths, review_points, self.sim.tool.footprint_radius
        )
        self.review_path_guard = {
            "blocked_paths": blocked,
            "blocked_paths_count": len(blocked),
            "protected_observations": self.protected_observations.copy(),
        }
        for index, path in enumerate(paths):
            path["segment_id"] = index + 1
        return paths

    def summaries(self):
        return [
            {
                **entry["detection"].summary(),
                "best_view": entry["best_view"],
                "view_indices": sorted(set(entry["view_indices"])),
                "normal_height_mm": entry["normal_height_mm"],
                "machining_required": entry["machining_required"],
                "normal_height_quantile": NORMAL_HEIGHT_QUANTILE,
                "target_remaining_height_mm": CELL_CONFIG["planning"][
                    "target_remaining_height_mm"
                ],
            }
            for entry in self.regions
        ]

    def capture(self, index):
        sim = self.sim
        rgb = sim.render()
        ideal = sim.render(mode="depth")
        reference = sim.render(mode="depth", reference=True)
        nominal_segmentation = sim.render(mode="segmentation", reference=True)
        nominal_geoms = [
            identity
            for identity in range(sim.model.ngeom)
            if sim.model.geom(identity).name.startswith("wp_")
        ]
        reference_mask = np.isin(nominal_segmentation[..., 0], nominal_geoms)
        intrinsics, camera = sim.camera_info()
        rng = np.random.default_rng(
            self.seed + 15191 + self.revision * 991 + index * 33
        )
        stress = sim.case == "noisy"
        sensor = sim.sensor_prior or CELL_CONFIG["camera"]
        sigma = 0.00025 if stress else sensor["depth_noise_sigma_mm"] / 1000
        dropout = 0.025 if stress else sensor["dropout_probability"]
        frame_count = REINSPECTION_DEPTH_FRAMES if self.reinspection else 1
        depth, depth_frames, sample_counts = sample_depth(
            ideal, rng, sigma, dropout, frame_count
        )
        selection_mask = None
        if self.first_projection is None:
            self.first_projection = (
                intrinsics.copy(),
                camera.copy(),
                reference.copy(),
                roi_mask(depth.shape, self.roi),
            )
        if self.roi is not None and index > 0:
            first_k, first_t, first_depth, permission = self.first_projection
            points = unproject(reference, intrinsics, camera)
            selection_mask = surface_permission(
                points, first_k, first_t, first_depth, permission
            )

        def inspect_depth(depth, sample_counts, frame_count):
            candidates, measured = detect_thin_reference(
                rgb,
                depth,
                reference,
                intrinsics,
                camera,
                inspection_bounds=BOUNDS,
                roi=self.roi if index == 0 else None,
                reference_object_mask=reference_mask,
                selection_mask=selection_mask,
                allow_sloped_surface=True,
                config=CELL_DETECTOR_CONFIG,
            )
            measured["normals"] = surface_normals(
                measured["reference_xyz"], camera
            )
            observed_counts = sample_counts[measured["surface"]]
            if len(observed_counts):
                # Noise is estimated on the averaged depth. Account for the
                # least repeated valid pixel without smoothing across positions.
                measured["noise_sigma_mm"] *= np.sqrt(
                    frame_count / observed_counts.min()
                )
            measured["depth_sample_counts"] = sample_counts
            measured["depth_frame_count"] = frame_count
            measured["tool"] = sim.tool
            return candidates, measured

        detections, observation = inspect_depth(
            depth, sample_counts, frame_count
        )
        acquisition_reason = (
            "routine_reinspection"
            if self.reinspection
            else ("initial_observation")
        )
        if self.reinspection is not None:
            preview = copy.deepcopy(self.reinspection)
            preview.observe(observation, index)
            quality = preview.report(
                CELL_CONFIG["planning"]["target_remaining_height_mm"]
            )
            if (
                (quality["uncertain"] or quality["needs_rework"])
                and not quality["overgrind"]
                and not quality["invalid_observations"]
            ):
                frame_count = MAX_REINSPECTION_DEPTH_FRAMES
                depth, depth_frames, sample_counts = sample_depth(
                    ideal,
                    rng,
                    sigma,
                    dropout,
                    frame_count,
                    previous_frames=depth_frames,
                )
                detections, observation = inspect_depth(
                    depth, sample_counts, frame_count
                )
                acquisition_reason = "signed_interval_confirmation"
        geometric = detections
        if self.reinspection is not None:
            self.reinspection.observe(observation, index)
            self.geometric_review.extend(
                {
                    "view": index,
                    "kind": candidate.kind,
                    "normal_height_mm": measured,
                }
                for candidate in geometric
                if candidate.kind in {"seam", "burr", "review"}
                and (
                    measured := observed_normal_height_mm(
                        candidate, observation
                    )
                )
                is not None
                and measured
                > CELL_CONFIG["planning"]["target_remaining_height_mm"]
            )
        if self.learned_detector is not None:
            detections, probabilities = self.learned_detector.detect(
                rgb, depth, reference, observation
            )
            if self.hybrid:
                detections = hybrid_candidates(
                    geometric, detections, depth.shape
                )
            name = f"{self.prefix}view_{index:02d}_model_prediction"
            np.savez_compressed(
                sim.output / f"{name}.npz",
                probability=probabilities.astype(np.float16),
                semantic=probabilities.argmax(axis=0).astype(np.uint8),
                measurable_mask=observation["mask"],
            )
            write_json(
                sim.output / f"{name}.json", observation["learned_detector"]
            )
        detections = apply_review_envelope(detections, observation)
        protected_candidates = apply_review_envelope(geometric, observation)
        if self.learned_detector is not None:
            protected_candidates.extend(detections)
        seen_protection = set()
        for candidate in protected_candidates:
            if candidate.kind != "review":
                continue
            key = candidate.pixels.tobytes()
            if key in seen_protection:
                continue
            seen_protection.add(key)
            height = observed_normal_height_mm(
                candidate, observation, quantile=0.95
            )
            if (
                height is not None
                and height > CELL_DETECTOR_CONFIG.review_height_mm
            ):
                points = observation["reference_xyz"][tuple(candidate.pixels.T)]
                points = points[np.isfinite(points).all(axis=1)]
                self.review_points.append(points)
                self.protected_observations.append(
                    {
                        "view": index,
                        "normal_height_p95_mm": height,
                        "reference_points": len(points),
                    }
                )
        local_paths = []
        for detection in detections:
            paths = observed_paths(detection, observation)
            self._merge(detection, paths, observation, index)
            local_paths.extend(paths)
        if sim.sensor_prior is not None:
            from .workstation_reuse import backproject_observation

            foreground = observation["surface"] | observation["mask"]
            arrays, cloud_metadata = backproject_observation(
                rgb,
                np.where(foreground, depth, np.nan),
                intrinsics,
                sim.camera_info(frame="world")[1],
                observation["mask"],
                self.seed + index,
            )
            arrays["T_world_workpiece_m"] = sim.body_transform(sim.workpiece)
            name = f"{self.prefix}view_{index:02d}_weldgrind_cloud"
            np.savez_compressed(sim.output / f"{name}.npz", **arrays)
            write_json(sim.output / f"{name}.json", cloud_metadata)
        # Labels are obtained only after detector and path prediction return.
        score, labels = evaluate_view(
            sim, detections, observation, ideal, reference
        )
        self.scores.append(score)
        name = f"{self.prefix}view_{index:02d}"
        Image.fromarray(rgb).save(sim.output / f"{name}_rgb.png")
        Image.fromarray(
            overlay(rgb, detections, local_paths, intrinsics, camera)
        ).save(sim.output / f"{name}_paths.png")
        Image.fromarray(labels).save(sim.output / f"{name}_truth.png")
        np.savez_compressed(
            sim.output / f"{name}_observation.npz",
            rgb=rgb,
            depth=depth.astype(np.float32),
            reference_depth=reference,
            K=intrinsics,
            T_workpiece_camera=camera,
            T_world_camera=sim.camera_info(frame="world")[1],
            encoder_angle_deg=float(np.rad2deg(sim.data.qpos[sim.table_qadr])),
            synthetic_noise_sigma_mm=float(sigma * 1000),
            synthetic_dropout_probability=float(dropout),
            depth_frame_count=frame_count,
            depth_sample_counts=sample_counts.astype(np.uint8),
            surface=observation["surface"],
            selection=observation["selection"],
            reference_object_mask=reference_mask,
            **(
                {"depth_frames": depth_frames.astype(np.float32)}
                if frame_count > 1
                else {}
            ),
        )
        if index == 0:
            Image.fromarray(rgb).save(
                sim.output / f"{self.prefix}camera_rgb.png"
            )
            Image.fromarray(
                overlay(rgb, detections, local_paths, intrinsics, camera)
            ).save(sim.output / f"{self.prefix}detection_paths.png")
            Image.fromarray(sim.render(reference=True)).save(
                sim.output / f"{self.prefix}reference_rgb.png"
            )
        points = observation["xyz"][observation["surface"]]
        if len(points) > 5000:
            points = points[np.linspace(0, len(points) - 1, 5000, dtype=int)]
        self.reference_points.append(points)
        self.views.append(
            {
                "index": index,
                "encoder_angle_deg": float(
                    np.rad2deg(sim.data.qpos[sim.table_qadr])
                ),
                "simulation_time_s": float(sim.data.time),
                "rgb": f"{name}_rgb.png",
                "overlay": f"{name}_paths.png",
                "point_count": int(observation["surface"].sum()),
                "noise_sigma_mm": observation["noise_sigma_mm"],
                "depth_frame_count": frame_count,
                "depth_acquisition_reason": acquisition_reason,
                "detections": len(detections),
                "paths": len(local_paths),
                "metrics": score,
                "T_world_camera": sim.camera_info(frame="world")[1].tolist(),
                "T_workpiece_camera": camera.tolist(),
                "detector": observation.get("learned_detector"),
            }
        )
        paths, summaries = self.paths(), self.summaries()
        record = {
            "revision": self.revision,
            "paths": paths,
            "detections": summaries,
            "views": self.views,
            "camera_fixed": True,
            "coordinate_frame": "workpiece_097",
            "source": "new fixed-camera RGB-D at measured encoder angle, registered nominal CAD reference",
            "truth_used_for_inference": False,
            "machining_eligibility_source": "positive measured displacement projected onto visible registered CAD normals, quantile 0.8",
            "learned_detector": self.learned_detector.metadata()
            if self.learned_detector is not None
            else None,
        }
        folder = sim.output / "predictions"
        folder.mkdir(exist_ok=True)
        write_json(folder / f"revision_{self.revision:03d}.json", record)
        write_json(sim.output / f"{self.prefix}detections.json", summaries)
        write_json(sim.output / f"{self.prefix}paths.json", paths)
        write_json(sim.output / f"{self.prefix}views.json", self.views)
        self.publish(
            sim.state(
                "inspect",
                prediction_revision=self.revision,
                view_count=len(self.views),
                detection_count=len(summaries),
                path_count=len(paths),
                observation_image=f"{name}_rgb.png",
                prediction_image=f"{name}_paths.png",
            )
        )
        self.revision += 1
        return observation

    def scan(self, angles=None):
        angles = (
            CELL_CONFIG["turntable"]["angles_deg"] if angles is None else angles
        )
        for index, angle in enumerate(angles):
            self.rotate(angle)
            self.capture(index)
        extra_view = None
        if self.targeted_rescan and len(angles) == 6 and self.reinspection:
            quality = self.reinspection.report(
                CELL_CONFIG["planning"]["target_remaining_height_mm"]
            )
            if quality["insufficient"] and not quality["needs_rework"]:
                # One intermediate view, acquired on the return to fixture zero.
                extra_view = self.reinspection.supplementary_angle(
                    angles[-1], (315, 330, 345)
                )
                if extra_view is not None:
                    self.rotate(extra_view)
                    self.capture(len(angles))
        self.rotate(360 if len(angles) > 1 else 0)
        points = np.concatenate(self.reference_points)
        # Export a display cloud. Full resolution per-view depth stays in NPZ.
        write_json(
            self.sim.output / f"{self.prefix}pointcloud.json",
            {
                "frame": "workpiece_097",
                "points": points.round(6).tolist(),
                "source": "camera observations transformed by measured encoder pose; no ICP or mesh point substitution",
            },
        )
        visible = {
            item for score in self.scores for item in score["visible_defects"]
        }
        active = {
            item["geom"]
            for item in self.sim.private_defects
            if getattr(self.sim, "evaluation_stock", {}).get(
                item["geom"], float("inf")
            )
            > 0.1
        }
        paths = self.paths()
        result = {
            "metrics": aggregate_views(self.scores),
            "view_count": len(self.views),
            "supplementary_view_deg": extra_view,
            "seconds": time.monotonic() - self.started,
            "prediction_revision": self.revision - 1,
            "camera_fixed": True,
            "encoder_fusion": True,
            "review_path_guard": self.review_path_guard,
            "generated_defects": len(self.sim.private_defects),
            "active_defects_for_evaluation": len(active),
            "unobserved_defects": len(active - visible),
            "learned_detector": self.learned_detector.metadata()
            if self.learned_detector is not None
            else None,
        }
        if self.reinspection is not None:
            quality = self.reinspection.report(
                CELL_CONFIG["planning"]["target_remaining_height_mm"],
                whole_workpiece_scan=(
                    len(angles) == 6
                    and self.roi is None
                    and self.whole_workpiece
                ),
            )
            quality["independent_geometric_signals"] = self.geometric_review
            quality["observational_release"] = (
                quality["observational_release"] and not self.geometric_review
            )
            local_paths, regrind = regrind_paths(
                self.reinspection.targets,
                quality,
                self.permitted_paths,
                start_segment_id=len(paths) + 1,
                rate_mm_s=self.removal_rate_mm_s,
            )
            local_paths, blocked = exclude_review_footprint(
                local_paths,
                (
                    np.concatenate(self.review_points)
                    if self.review_points
                    else np.empty((0, 3))
                ),
                self.sim.tool.footprint_radius,
            )
            additional = additional_regrind_paths(paths, local_paths)
            paths.extend(additional)
            for index, path in enumerate(paths):
                path["segment_id"] = index + 1
            self.review_path_guard["blocked_paths"].extend(blocked)
            self.review_path_guard["blocked_paths_count"] += len(blocked)
            quality["local_regrind"] = {
                **regrind,
                "review_blocked_paths": blocked,
                "duplicate_trace_count": len(local_paths) - len(additional),
                "candidate_path_count": len(additional),
            }
            result["region_reinspection"] = quality
            write_json(self.sim.output / f"{self.prefix}paths.json", paths)
            if additional:
                write_json(
                    self.sim.output
                    / "predictions"
                    / f"revision_{self.revision:03d}.json",
                    {
                        "revision": self.revision,
                        "paths": paths,
                        "detections": self.summaries(),
                        "views": self.views,
                        "camera_fixed": True,
                        "coordinate_frame": "workpiece_097",
                        "source": "current signed anchor measurements and new fixed-camera detections",
                        "truth_used_for_inference": False,
                        "region_reinspection": quality,
                    },
                )
                self.publish(
                    self.sim.state(
                        "inspect",
                        prediction_revision=self.revision,
                        view_count=len(self.views),
                        path_count=len(paths),
                    )
                )
                self.revision += 1
                result["prediction_revision"] = self.revision - 1
        write_json(
            self.sim.output / f"{self.prefix}perception_metrics.json", result
        )
        return self.summaries(), paths, result
