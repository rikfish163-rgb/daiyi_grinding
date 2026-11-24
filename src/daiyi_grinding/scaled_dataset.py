"""Parallel multi-master 10x-20x synthetic casting dataset generator.

Builds thousands of diverse RGB-D + nominal-CAD + semantic + residual + root
bundles (plus dense 3D clouds at configurable stride) across real station CADs
and procedural parametric casting CADs under 5 physically motivated sensor,
registration-misalignment, casting-warpage and RGB domain-randomization regimes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import dataset_pipeline as dp
from . import project
from .procedural_castings import materialize_procedural_castings

SCHEMA = "daiyi-scaled-dataset-v2"
REGIME_NAMES = tuple(dp.SCALED_REGIME_PROFILES.keys())
NORMAL_REGIME_ORDER = (
    "high_drift_warped",
    "harsh_specular_oxide",
    "standard_shop",
    "grazing_noisy",
    "nominal_clean",
)

# Per-process cache so each worker extracts CAD features and mesh indices once.
_WORKER_CACHE: dict[str, tuple] = {}


def _worker_load_cad(package_source, config_dict):
    key = json.dumps(package_source, sort_keys=True)
    cached = _WORKER_CACHE.get(key)
    if cached is not None:
        return cached
    pkg = dp.load_workpiece_package(package_source)
    mesh = dp.load_mesh(pkg.mesh_path)
    config = dp.FeatureConfig(**config_dict)
    features = dp.extract_features(
        mesh, pkg.workpiece_id, pkg.mesh_path, config
    )
    neighbourhood = dp.MeshNeighbourhood.build(mesh)
    surface_index = dp.DraftSurfaceIndex.build(mesh, config.draft_direction)
    value = (pkg, mesh, features, neighbourhood, surface_index)
    _WORKER_CACHE[key] = value
    return value


def _build_one_seed_job(job):
    """Generate all views for one (workpiece, seed) inside a worker process."""
    (
        package_source,
        output_str,
        split,
        seed,
        image_size,
        cloud_spacing_m,
        point_cloud_stride,
        defect_kinds,
        camera_kinds,
        intermediate_states,
        normal_views,
        config_dict,
    ) = job
    output = Path(output_str)
    pkg, mesh, features, neighbourhood, surface_index = _worker_load_cad(
        package_source, config_dict
    )
    defects = dp.generate_defects(
        mesh,
        features,
        seed,
        kinds=defect_kinds,
        neighbourhood=neighbourhood,
        surface_index=surface_index,
    )
    scene = dp.assemble_scene(mesh, defects)
    defect_records = [defect.record() for defect in defects]
    samples = []
    include_cloud_seed = (seed % max(int(point_cloud_stride), 1)) == 0

    for identity, defect in enumerate(defects, 1):
        center = np.vstack(defect.roots_m).mean(axis=0)
        for kind_index, sample_kind in enumerate(camera_kinds):
            regime_name = REGIME_NAMES[
                (seed + identity * 3 + kind_index) % len(REGIME_NAMES)
            ]
            profile = dp.SCALED_REGIME_PROFILES[regime_name]
            sample_seed = dp._derived_seed(
                f"{pkg.workpiece_id}:{seed}:{sample_kind}:{identity}:{regime_name}"
            )
            rng = np.random.default_rng(sample_seed)
            profile_sample = profile.draw(rng)
            encoder_angle = dp.ENCODER_ANGLES_DEG[
                (seed + identity * 2 + kind_index) % len(dp.ENCODER_ANGLES_DEG)
            ]
            cameras = dp._sample_cameras(
                defect,
                features.config.draft_direction,
                encoder_angle,
                rng,
                image_size,
            )
            camera, _, weak = cameras[sample_kind]
            want_cloud = include_cloud_seed and (kind_index == 0)
            arrays, rgb, cloud, metadata = dp._render_sample(
                scene,
                camera,
                profile,
                profile_sample,
                rng,
                sample_kind=sample_kind,
                encoder_angle_deg=encoder_angle,
                weak_alignment=weak,
                defect_identity=identity,
                defect_center_m=center,
                supervision={
                    "semantic": dp.SEMANTIC_LABELS,
                    "instance": f"{identity} labels the current defect",
                    "residual_mm": "face-mean signed height above nominal CAD",
                    "protection": "untouched nominal pixels next to the defect",
                    "root": "3D root polyline, tangent and normal per instance",
                },
                defect_records=defect_records,
                cloud_spacing_m=cloud_spacing_m,
                nominal_mesh=mesh,
                include_point_cloud=want_cloud,
            )
            visible_instances = metadata.get("visible_instances") or [identity]
            roots = dp._root_bundle(
                [defects[value - 1] for value in visible_instances],
                identities=visible_instances,
            )
            if cloud is not None:
                visible = dp._visible_cloud_points(
                    cloud["points_m"], camera, arrays["depth_ideal_m"]
                )
                cloud = {
                    key: value[visible]
                    if key in dp.CLOUD_POINT_FIELDS
                    else value
                    for key, value in cloud.items()
                }
                cloud["count"] = int(len(cloud["points_m"]))
            sample_id = f"{pkg.workpiece_id}_{split}_{sample_kind}_d{identity:02d}_s{seed}"
            folder = output / "samples" / sample_id
            metadata = {
                **metadata,
                "sample_id": sample_id,
                "workpiece_id": pkg.workpiece_id,
                "package_id": pkg.package_id,
                "split": split,
                "seed": seed,
                "regime": regime_name,
                "cad_sha256": features.cad_sha256,
                "point_count": int(cloud["count"]) if cloud is not None else 0,
                "provenance": dp._provenance(pkg, features, seed, sample_kind),
            }
            dp._write_sample(folder, arrays, rgb, cloud, roots, metadata)
            samples.append(
                {
                    "sample_id": sample_id,
                    "directory": str(folder.relative_to(output)),
                    "workpiece_id": pkg.workpiece_id,
                    "package_id": pkg.package_id,
                    "split": split,
                    "seed": seed,
                    "regime": regime_name,
                    "sample_kind": sample_kind,
                    "instance": identity,
                    "defect_kind": defect.kind,
                    "defect_style": defect.style,
                    "intermediate_state": None,
                    "has_point_cloud": cloud is not None,
                    "point_count": int(cloud["count"])
                    if cloud is not None
                    else 0,
                    "mean_sigma_mm": metadata["mean_sigma_mm"],
                    "depth_valid_fraction": metadata["depth_valid_fraction"],
                    "peak_residual_mm": metadata["peak_residual_mm"],
                }
            )

        for state_idx, state in enumerate(intermediate_states):
            regime_name = REGIME_NAMES[
                (seed + identity + state_idx + 1) % len(REGIME_NAMES)
            ]
            profile = dp.SCALED_REGIME_PROFILES[regime_name]
            sample_seed = dp._derived_seed(
                f"{pkg.workpiece_id}:{seed}:{state}:{identity}:{regime_name}"
            )
            rng = np.random.default_rng(sample_seed)
            profile_sample = profile.draw(rng)
            arrays, rgb, cloud, metadata = dp._intermediate_bundle(
                features,
                defect,
                identity,
                state,
                split,
                seed,
                profile,
                profile_sample,
                image_size,
                rng,
            )
            if not (include_cloud_seed and state_idx == 0):
                cloud = None
            sample_id = f"{pkg.workpiece_id}_{split}_inter_d{identity:02d}_{state}_s{seed}"
            folder = output / "samples" / sample_id
            metadata = {
                **metadata,
                "sample_id": sample_id,
                "package_id": pkg.package_id,
                "regime": regime_name,
                "cad_sha256": features.cad_sha256,
                "point_count": int(cloud["count"]) if cloud is not None else 0,
                "provenance": dp._provenance(
                    pkg, features, seed, "intermediate_state"
                ),
            }
            dp._write_sample(
                folder,
                arrays,
                rgb,
                cloud,
                dp._root_bundle([defect], identities=[identity]),
                metadata,
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "directory": str(folder.relative_to(output)),
                    "workpiece_id": pkg.workpiece_id,
                    "package_id": pkg.package_id,
                    "split": split,
                    "seed": seed,
                    "regime": regime_name,
                    "sample_kind": "intermediate_state",
                    "instance": identity,
                    "defect_kind": defect.kind,
                    "defect_style": defect.style,
                    "intermediate_state": state,
                    "has_point_cloud": cloud is not None,
                    "point_count": int(cloud["count"])
                    if cloud is not None
                    else 0,
                    "mean_sigma_mm": metadata["mean_sigma_mm"],
                    "depth_valid_fraction": metadata["depth_valid_fraction"],
                    "peak_residual_mm": metadata["peak_residual_mm"],
                }
            )

    for normal_idx in range(int(normal_views)):
        regime_name = NORMAL_REGIME_ORDER[
            (seed + normal_idx) % len(NORMAL_REGIME_ORDER)
        ]
        profile = dp.SCALED_REGIME_PROFILES[regime_name]
        sample_seed = dp._derived_seed(
            f"{pkg.workpiece_id}:{seed}:normal:{normal_idx}:{regime_name}"
        )
        rng = np.random.default_rng(sample_seed)
        profile_sample = profile.draw(rng)
        arrays, rgb, metadata = dp.render_normal_sample(
            mesh,
            features,
            profile,
            profile_sample,
            rng,
            regime_name=regime_name,
            image_size=image_size,
            draft_direction=features.config.draft_direction,
        )
        sample_id = (
            f"{pkg.workpiece_id}_{split}_normal_n{normal_idx:02d}_s{seed}"
        )
        folder = output / "samples" / sample_id
        metadata = {
            **metadata,
            "sample_id": sample_id,
            "workpiece_id": pkg.workpiece_id,
            "package_id": pkg.package_id,
            "split": split,
            "seed": seed,
            "cad_sha256": features.cad_sha256,
            "provenance": dp._provenance(
                pkg, features, seed, "normal_hard_negative"
            ),
        }
        dp._write_sample(folder, arrays, rgb, None, None, metadata)
        samples.append(
            {
                "sample_id": sample_id,
                "directory": str(folder.relative_to(output)),
                "workpiece_id": pkg.workpiece_id,
                "package_id": pkg.package_id,
                "split": split,
                "seed": seed,
                "regime": regime_name,
                "sample_kind": "normal_hard_negative",
                "instance": 0,
                "defect_kind": "normal",
                "defect_style": "defect_free",
                "intermediate_state": None,
                "has_point_cloud": False,
                "point_count": 0,
                "mean_sigma_mm": metadata["mean_sigma_mm"],
                "depth_valid_fraction": metadata["depth_valid_fraction"],
                "peak_residual_mm": 0.0,
            }
        )

    return {
        "workpiece_id": pkg.workpiece_id,
        "package_id": pkg.package_id,
        "split": split,
        "seed": seed,
        "samples": samples,
    }


def _render_preview_grid(output, samples, count=16):
    """Render a compact 4x4 RGB + residual preview sheet across CADs/regimes."""
    rng = np.random.default_rng(20261124)
    chosen = []
    by_cad = {}
    for row in samples:
        by_cad.setdefault(row["workpiece_id"], []).append(row)
    cads = sorted(by_cad)
    idx = 0
    while len(chosen) < min(count, len(samples)):
        cad = cads[idx % len(cads)]
        pool = by_cad[cad]
        chosen.append(pool[int(rng.integers(0, len(pool)))])
        idx += 1
    tile_w, tile_h = 240, 180
    cols = 4
    rows = (len(chosen) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * tile_w, rows * tile_h), (18, 20, 24))
    draw = ImageDraw.Draw(canvas)
    for k, item in enumerate(chosen):
        rgb = Image.open(output / item["directory"] / "rgb.png").resize(
            (tile_w, tile_h)
        )
        r, c = divmod(k, cols)
        canvas.paste(rgb, (c * tile_w, r * tile_h))
        label = (
            f"{item['workpiece_id']}[{item['split']}] "
            f"{item['defect_kind']}/{item['regime'][:10]}"
        )
        draw.rectangle(
            [c * tile_w, r * tile_h, (c + 1) * tile_w, r * tile_h + 18],
            fill=(0, 0, 0),
        )
        draw.text((c * tile_w + 4, r * tile_h + 3), label, fill=(240, 240, 240))
    preview_path = output / "sample_preview_grid.png"
    canvas.save(preview_path)
    return preview_path


def generate_scaled_dataset(
    output,
    *,
    train_097_seeds=range(2001, 2061),
    val_097_seeds=range(5001, 5011),
    train_proc_seeds=range(3001, 3031),
    val_proc_seeds=range(4001, 4011),
    test_021_seeds=range(6001, 6026),
    image_size=(384, 288),
    cloud_spacing_m=0.00035,
    point_cloud_stride=4,
    workers=16,
):
    """Generate the 10x-20x scaled multi-CAD casting dataset in parallel."""
    started = time.perf_counter()
    target = Path(output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    (target / "samples").mkdir()
    proc_dir = target / "procedural_cads"
    proc_records = materialize_procedural_castings(proc_dir)
    proc_by_id = {
        rec["workpiece_id"]: rec["package_path"] for rec in proc_records
    }

    pkg_097 = str((project.ROOT / "config/workpieces/wp_097.json").resolve())
    pkg_021 = str((project.ROOT / "config/workpieces/wp_021.json").resolve())

    # Guarantee zero overlap with historical frozen 097 test seeds (9601, 9602).
    forbidden_097 = {9601, 9602}
    if (set(train_097_seeds) | set(val_097_seeds)) & forbidden_097:
        raise ValueError(
            "097 train/val seeds must not overlap frozen test 9601/9602"
        )
    if set(train_097_seeds) & set(val_097_seeds):
        raise ValueError("097 train and val seeds must be disjoint")

    defect_kinds = ("flash", "burr", "casting_root", "burr")
    camera_kinds = ("overview", "local_fov", "oblique_fov")
    intermediate_states = ("rough_residual", "thin_stock")
    normal_views = 4
    config_dict = {}

    jobs = []

    def add_jobs(package_source, split, seeds):
        for seed in seeds:
            jobs.append(
                (
                    package_source,
                    str(target),
                    split,
                    int(seed),
                    tuple(image_size),
                    float(cloud_spacing_m),
                    int(point_cloud_stride),
                    defect_kinds,
                    camera_kinds,
                    intermediate_states,
                    normal_views,
                    config_dict,
                )
            )

    # Train: real 097 (train seeds) + 6 distinct procedural casting topologies
    add_jobs(pkg_097, "train", train_097_seeds)
    for key in ("p01", "p02", "p03", "p04", "p05", "p06"):
        add_jobs(proc_by_id[key], "train", train_proc_seeds)

    # Val: 2 completely unseen procedural casting topologies + disjoint 097 val seeds
    for key in ("p07", "p08"):
        add_jobs(proc_by_id[key], "val", val_proc_seeds)
    add_jobs(pkg_097, "val", val_097_seeds)

    # Test: 100% unseen real factory CAD 021 (never seen in train or val)
    add_jobs(pkg_021, "test", test_021_seeds)

    env_backup = {}
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        env_backup[var] = os.environ.get(var)
        os.environ[var] = "1"

    all_samples = []
    completed_jobs = 0
    try:
        max_workers = max(1, min(int(workers), len(jobs)))
        if max_workers == 1:
            for job in jobs:
                result = _build_one_seed_job(job)
                all_samples.extend(result["samples"])
                completed_jobs += 1
        else:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers
            ) as executor:
                for result in executor.map(_build_one_seed_job, jobs):
                    all_samples.extend(result["samples"])
                    completed_jobs += 1
                    if completed_jobs % 30 == 0 or completed_jobs == len(jobs):
                        elapsed = time.perf_counter() - started
                        print(
                            json.dumps(
                                {
                                    "jobs_done": completed_jobs,
                                    "jobs_total": len(jobs),
                                    "samples": len(all_samples),
                                    "elapsed_s": round(elapsed, 1),
                                }
                            ),
                            flush=True,
                        )
    finally:
        for var, old in env_backup.items():
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old

    # Verify zero CAD leakage for held-out unseen CADs (p07, p08, 021).
    by_cad_splits = {}
    for row in all_samples:
        by_cad_splits.setdefault(row["workpiece_id"], set()).add(row["split"])
    for unseen_id, expected_split in (
        ("021", {"test"}),
        ("p07", {"val"}),
        ("p08", {"val"}),
    ):
        if by_cad_splits.get(unseen_id) != expected_split:
            raise ValueError(
                f"Split leakage detected for {unseen_id}: {by_cad_splits.get(unseen_id)}"
            )

    split_counts = dict(Counter(row["split"] for row in all_samples))
    cad_counts = dict(Counter(row["workpiece_id"] for row in all_samples))
    kind_counts = dict(Counter(row["defect_kind"] for row in all_samples))
    sample_kind_counts = dict(
        Counter(row["sample_kind"] for row in all_samples)
    )
    regime_counts = dict(Counter(row["regime"] for row in all_samples))
    cloud_count = sum(1 for row in all_samples if row["has_point_cloud"])
    elapsed = time.perf_counter() - started

    preview_path = _render_preview_grid(target, all_samples)
    manifest = {
        "schema": SCHEMA,
        "elapsed_seconds": round(elapsed, 2),
        "total_samples": len(all_samples),
        "total_point_clouds": cloud_count,
        "image_size": list(image_size),
        "baseline_comparisons": {
            "v1_total_views": 420,
            "v1_train_views": 252,
            "total_views_multiplier_vs_v1": round(len(all_samples) / 420.0, 2),
            "train_views_multiplier_vs_v1": round(
                split_counts.get("train", 0) / 252.0, 2
            ),
            "prev_multimaster_bundles": 30,
            "bundles_multiplier_vs_prev": round(len(all_samples) / 30.0, 2),
            "point_clouds_multiplier_vs_prev": round(cloud_count / 30.0, 2),
        },
        "counts": {
            "by_split": split_counts,
            "by_cad": cad_counts,
            "by_defect_kind": kind_counts,
            "by_sample_kind": sample_kind_counts,
            "by_regime": regime_counts,
        },
        "split_policy": {
            "train_cads": ["097", "p01", "p02", "p03", "p04", "p05", "p06"],
            "val_unseen_cads": ["p07", "p08"],
            "val_in_family_097_seeds": list(val_097_seeds),
            "test_unseen_real_cad": ["021"],
            "forbidden_historical_097_test_seeds": sorted(forbidden_097),
            "zero_unseen_cad_leakage": True,
        },
        "procedural_cads": proc_records,
        "regimes": {
            name: profile.record()
            for name, profile in dp.SCALED_REGIME_PROFILES.items()
        },
        "preview_grid": str(preview_path.relative_to(target)),
        "scope": {
            "offline_only": True,
            "mujoco_used": False,
            "real_scans_used": False,
            "truth_used_for_control": False,
            "labels_are_supervision_only": True,
        },
        "samples": all_samples,
    }
    dp.write_json(target / "manifest.json", manifest)
    summary = {k: v for k, v in manifest.items() if k != "samples"}
    dp.write_json(target / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--point-cloud-stride", type=int, default=4)
    parser.add_argument("--width", type=int, default=384)
    parser.add_argument("--height", type=int, default=288)
    args = parser.parse_args(argv)
    summary = generate_scaled_dataset(
        args.output,
        image_size=(args.width, args.height),
        point_cloud_stride=args.point_cloud_stride,
        workers=args.workers,
    )
    print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
    print(
        json.dumps(
            summary["baseline_comparisons"], ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
