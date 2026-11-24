"""Scaled multi-CAD DefectUNet v2 training and v1-vs-v2 unseen-CAD evaluation.

Consumes ``scaled_dataset`` bundles (plus optional historical v1 MuJoCo train
views, strictly excluding frozen test seeds) using on-the-fly random 256x256
cropping, DAS3D-style RGB modality dropout, and online residual-tilt
augmentation. Produces a checkpoint 100% compatible with ``LearnedDetector``
(``rgbd_registered_v1``, 11 channels) without touching frozen v1 weights.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import defect_training as legacy_training
from . import project
from .learned_defects import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    LearnedDetector,
    file_sha256,
    sensor_features,
)

DEFAULT_V1_CHECKPOINT = (
    project.ROOT / "runs/learned_grinding_20261121/training_01/model.pt"
)
DEFAULT_V1_SPLIT = (
    project.ROOT / "runs/learned_grinding_20261121/split_plan.json"
)
DEFAULT_V1_BATCHES = (
    project.ROOT / "runs/data_reuse_20261121/team_prior_batch_final_02",
    project.ROOT / "runs/learned_grinding_20261121/synthetic_extension_01",
)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def map_semantic_to_3class(semantic, residual_mm, defect_kind):
    """Map dataset_pipeline semantics to DefectUNet's 3-class contract.

    Class 0: background, nominal CAD, review_excess, or non-positive residual.
    Class 1: flash (1), casting_root (5), or positive non-burr intermediate residual.
    Class 2: compact burr (2), or positive burr intermediate residual.
    """
    sem = np.asarray(semantic, dtype=np.uint8)
    labels = np.zeros(sem.shape, dtype=np.uint8)
    labels[(sem == 1) | (sem == 5)] = 1
    labels[sem == 2] = 2
    inter = sem == 6
    if inter.any():
        pos = inter & np.isfinite(residual_mm) & (residual_mm > 0.05)
        labels[pos] = 2 if defect_kind == "burr" else 1
    return labels


def load_scaled_bundle(dataset_root, row):
    """Load RGB, observed depth, nominal depth, intrinsics and 3-class labels."""
    folder = Path(dataset_root) / row["directory"]
    rgb = np.asarray(
        Image.open(folder / "rgb.png").convert("RGB"), dtype=np.uint8
    )
    with np.load(folder / "arrays.npz", allow_pickle=False) as arrays:
        depth = arrays["depth_m"].astype(np.float32)
        nominal = arrays["depth_nominal_m"].astype(np.float32)
        semantic = arrays["semantic"].astype(np.uint8)
        residual = arrays["residual_mm"].astype(np.float32)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    intrinsics = np.asarray(meta["camera"]["K"], dtype=np.float32)
    labels = map_semantic_to_3class(semantic, residual, row.get("defect_kind"))
    return {
        "rgb": rgb,
        "depth": depth,
        "reference_depth": nominal,
        "K": intrinsics,
        "labels": labels,
    }


def _sample_crop(features, labels, rng, tile=256):
    """Extract one random 256x256 crop, focusing on foreground/burr when present."""
    _, height, width = features.shape
    if height < tile or width < tile:
        features = np.pad(
            features,
            ((0, 0), (0, max(0, tile - height)), (0, max(0, tile - width))),
            mode="edge",
        )
        labels = np.pad(
            labels,
            ((0, max(0, tile - height)), (0, max(0, tile - width))),
            mode="constant",
        )
        _, height, width = features.shape
    max_r = height - tile
    max_c = width - tile
    r = int(rng.integers(0, max_r + 1)) if max_r > 0 else 0
    c = int(rng.integers(0, max_c + 1)) if max_c > 0 else 0

    if rng.random() < 0.75:
        burr_px = np.argwhere(labels == 2)
        if len(burr_px) > 0 and rng.random() < 0.70:
            center = burr_px[int(rng.integers(0, len(burr_px)))]
        else:
            fg_px = np.argwhere(labels > 0)
            center = (
                fg_px[int(rng.integers(0, len(fg_px)))]
                if len(fg_px) > 0
                else None
            )
        if center is not None:
            jitter = rng.integers(-64, 65, size=2)
            r = int(np.clip(center[0] + jitter[0] - tile // 2, 0, max_r))
            c = int(np.clip(center[1] + jitter[1] - tile // 2, 0, max_c))
    return (
        features[:, r : r + tile, c : c + tile].copy(),
        labels[r : r + tile, c : c + tile].copy(),
    )


def augment_chip(
    x,
    y,
    rng,
    *,
    rgb_dropout_prob=0.35,
    residual_tilt_prob=0.40,
):
    """Apply geometric flips/rotations, DAS3D RGB dropout, and smooth residual tilt."""
    x = x.astype(np.float32, copy=True)
    y = y.astype(np.int64, copy=True)
    if rng.random() < 0.5:
        x = x[:, :, ::-1].copy()
        y = y[:, ::-1].copy()
    if rng.random() < 0.5:
        x = x[:, ::-1, :].copy()
        y = y[::-1, :].copy()
    turns = int(rng.integers(0, 4))
    if turns:
        x = np.rot90(x, turns, axes=(1, 2)).copy()
        y = np.rot90(y, turns).copy()
        if turns % 2:
            x[[6, 7]] = x[[7, 6]]

    # DAS3D-style RGB modality dropout + strong photometric domain randomization.
    draw = float(rng.random())
    if draw < rgb_dropout_prob * 0.7:
        x[:3] = 0.0
    elif draw < rgb_dropout_prob:
        tint = rng.uniform(-0.85, 0.85, size=(3, 1, 1)).astype(np.float32)
        noise = rng.normal(0.0, 0.12, size=x[:3].shape).astype(np.float32)
        x[:3] = np.clip(tint + noise, -1.0, 1.0)
    else:
        gain = rng.uniform(0.65, 1.35, size=(3, 1, 1)).astype(np.float32)
        bias = rng.uniform(-0.22, 0.22, size=(3, 1, 1)).astype(np.float32)
        noise = rng.normal(0.0, 0.04, size=x[:3].shape).astype(np.float32)
        x[:3] = np.clip(x[:3] * gain + bias + noise, -1.0, 1.0)

    # Online low-frequency residual bias/tilt simulating unmodeled warpage/drift.
    if rng.random() < residual_tilt_prob:
        h, w = y.shape
        yy = np.linspace(-1.0, 1.0, h, dtype=np.float32)[:, None]
        xx = np.linspace(-1.0, 1.0, w, dtype=np.float32)[None, :]
        bias_mm = float(rng.uniform(-0.30, 0.30))
        gx_mm = float(rng.uniform(-0.22, 0.22))
        gy_mm = float(rng.uniform(-0.22, 0.22))
        shift_norm = (bias_mm + gx_mm * xx + gy_mm * yy) / 6.0
        valid = x[10] > 0.5
        x[3] = np.where(valid, np.clip(x[3] + shift_norm, -2.0, 4.0), x[3])
        x[4] = np.where(valid, np.clip(x[4] + shift_norm, -2.0, 4.0), x[4])

    return x, y


def _weighted_segmentation_loss(logits, labels):
    import torch.nn.functional as F

    weights = logits.new_tensor([0.5, 2.0, 3.8], dtype=logits.dtype)
    ce = F.cross_entropy(logits.float(), labels, weight=weights.float())
    prob = logits.float().softmax(dim=1)
    truth = F.one_hot(labels, 3).permute(0, 3, 1, 2).float()
    dims = (0, 2, 3)
    inter = (prob * truth).sum(dim=dims)
    denom = prob.sum(dim=dims) + truth.sum(dim=dims)
    dice = 1.0 - ((2.0 * inter + 1.0) / (denom + 1.0))[1:].mean()
    return ce + dice


def evaluate_scaled_rows(
    detector,
    dataset_root,
    rows,
    thresholds,
    *,
    zero_rgb=False,
    max_views=None,
):
    """Evaluate a LearnedDetector on scaled_dataset bundles."""
    selected = rows if max_views is None else rows[:max_views]
    matrices = {t: np.zeros((3, 3), np.int64) for t in thresholds}
    normal_fp = dict.fromkeys(thresholds, 0)
    by_kind_matrices = {}
    by_regime_matrices = {}
    for row in selected:
        item = load_scaled_bundle(dataset_root, row)
        rgb = np.zeros_like(item["rgb"]) if zero_rgb else item["rgb"]
        probs = detector.probabilities(
            rgb, item["depth"], item["reference_depth"], item["K"]
        )
        truth = item["labels"]
        sample_kind = row["sample_kind"]
        regime = row.get("regime", "unknown")
        if sample_kind not in by_kind_matrices:
            by_kind_matrices[sample_kind] = {
                t: np.zeros((3, 3), np.int64) for t in thresholds
            }
        if regime not in by_regime_matrices:
            by_regime_matrices[regime] = {
                t: np.zeros((3, 3), np.int64) for t in thresholds
            }
        for t in thresholds:
            conf = legacy_training.confusion_counts(truth, probs, t)
            matrices[t] += conf
            by_kind_matrices[sample_kind][t] += conf
            by_regime_matrices[regime][t] += conf
            if sample_kind == "normal_hard_negative":
                normal_fp[t] += int(conf[0, 1:].sum())
    return {
        str(t): {
            **legacy_training.score_confusion(matrices[t]),
            "view_count": len(selected),
            "normal_false_positive_pixels": normal_fp[t],
            "confusion": matrices[t].tolist(),
            "by_sample_kind": {
                k: legacy_training.score_confusion(m[t])
                for k, m in sorted(by_kind_matrices.items())
            },
            "by_regime": {
                k: legacy_training.score_confusion(m[t])
                for k, m in sorted(by_regime_matrices.items())
            },
        }
        for t in thresholds
    }


def evaluate_legacy_test(
    detector, batches=DEFAULT_V1_BATCHES, split_path=DEFAULT_V1_SPLIT
):
    """Evaluate a detector on the frozen historical 097 test split (seeds 9601/9602)."""
    split_plan = json.loads(Path(split_path).read_text(encoding="utf-8"))
    index, _ = legacy_training.data_index(batches, split_plan)
    rows = index["test"]
    sweep = legacy_training.validation_sweep(
        detector, rows, [detector.threshold]
    )
    return {
        "view_count": len(rows),
        "seeds": split_plan["test_seeds"],
        "threshold": detector.threshold,
        "metrics": sweep[str(detector.threshold)],
    }


def train_scaled_detector(
    dataset_root,
    output_dir,
    *,
    width=24,
    steps=3600,
    batch_size=24,
    lr=1.5e-3,
    workers=8,
    seed=20261124,
    include_v1_train=True,
    rgb_dropout_prob=0.35,
    residual_tilt_prob=0.40,
):
    """Train DefectUNet v2 on the scaled multi-CAD dataset and select threshold on val."""
    import torch
    from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

    from .defect_network import DefectUNet

    dataset_root = Path(dataset_root).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)

    manifest = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8")
    )
    train_scaled = [
        row for row in manifest["samples"] if row["split"] == "train"
    ]
    val_scaled = [row for row in manifest["samples"] if row["split"] == "val"]

    legacy_train = []
    if include_v1_train and DEFAULT_V1_SPLIT.is_file():
        split_plan = json.loads(DEFAULT_V1_SPLIT.read_text(encoding="utf-8"))
        if all(Path(b).is_dir() for b in DEFAULT_V1_BATCHES):
            legacy_index, _ = legacy_training.data_index(
                DEFAULT_V1_BATCHES, split_plan
            )
            legacy_train = legacy_index["training"]

    # Build unified training source list with balanced class/domain weights.
    sources = []
    weights = []
    for row in train_scaled:
        sources.append(("scaled", row))
        if row["sample_kind"] == "normal_hard_negative":
            weights.append(1.1)
        elif row["defect_kind"] == "burr":
            weights.append(1.8)
        elif row["sample_kind"] == "intermediate_state":
            weights.append(1.2)
        else:
            weights.append(1.0)
    if legacy_train:
        # Allocate ~16% of training sampling weight to historical 097 MuJoCo train views.
        scaled_total = sum(weights)
        legacy_w = (scaled_total * 0.19) / len(legacy_train)
        for row in legacy_train:
            sources.append(("legacy", row))
            weights.append(
                legacy_w * (1.8 if row["case"] == "casting_burr" else 1.0)
            )

    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.set_float32_matmul_precision("high")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.benchmark = True

    class ScaledChipDataset(Dataset):
        def __init__(self):
            self._rng = None

        def __len__(self):
            return len(sources)

        def __getitem__(self, idx):
            kind, row = sources[idx]
            if self._rng is None:
                worker_info = torch.utils.data.get_worker_info()
                w_seed = (
                    seed
                    if worker_info is None
                    else int(worker_info.seed) % (2**32 - 1)
                )
                self._rng = np.random.default_rng(w_seed)
            rng = self._rng
            if kind == "scaled":
                item = load_scaled_bundle(dataset_root, row)
                features = sensor_features(
                    item["rgb"],
                    item["depth"],
                    item["reference_depth"],
                    item["K"],
                )
                labels = item["labels"]
            else:
                public = legacy_training.load_public(row)
                features = sensor_features(
                    public["rgb"],
                    public["depth"],
                    public["reference_depth"],
                    public["K"],
                )
                labels = legacy_training.load_labels(row)
            x, y = _sample_crop(features, labels, rng, tile=256)
            x, y = augment_chip(
                x,
                y,
                rng,
                rgb_dropout_prob=rgb_dropout_prob,
                residual_tilt_prob=residual_tilt_prob,
            )
            return torch.from_numpy(x), torch.from_numpy(y)

    sampler = WeightedRandomSampler(
        weights=torch.as_tensor(weights, dtype=torch.double),
        num_samples=steps * batch_size,
        replacement=True,
    )
    loader = DataLoader(
        ScaledChipDataset(),
        batch_size=batch_size,
        sampler=sampler,
        num_workers=workers,
        pin_memory=(device == "cuda"),
        persistent_workers=(workers > 0),
        drop_last=True,
    )

    model = DefectUNet(channels=11, width=width).to(device)
    param_count = sum(p.numel() for p in model.parameters())
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-2)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=steps, eta_min=lr * 0.03
    )
    use_amp = device == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    # Validation subset for fast mid-training selection (unseen p07/p08 + 097 val).
    val_rng = np.random.default_rng(seed + 77)
    val_indices = sorted(
        val_rng.choice(
            len(val_scaled), size=min(180, len(val_scaled)), replace=False
        ).tolist()
    )
    val_eval_rows = [val_scaled[i] for i in val_indices]

    def build_package(threshold, step):
        return {
            "state_dict": {
                k: v.detach().cpu() for k, v in model.state_dict().items()
            },
            "model_config": model.config,
            "parameter_count": param_count,
            "feature_version": FEATURE_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "probability_threshold": float(threshold),
            "training_step": int(step),
            "dataset_root": str(dataset_root),
            "train_scaled_views": len(train_scaled),
            "train_legacy_views": len(legacy_train),
            "val_views": len(val_scaled),
            "rgb_dropout_prob": rgb_dropout_prob,
            "residual_tilt_prob": residual_tilt_prob,
            "test_used_for_selection": False,
            "real_scans_used": False,
        }

    started = time.perf_counter()
    running_loss = 0.0
    history = []
    best_score = -1.0
    best_threshold = 0.45
    best_step = 0
    best_path = output / "model.pt"
    eval_every = max(600, steps // 4)
    thresholds = (0.35, 0.45, 0.55, 0.65)

    model.train()
    for step, (batch_x, batch_y) in enumerate(loader, 1):
        batch_x = batch_x.to(device, non_blocking=True)
        batch_y = batch_y.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        with torch.amp.autocast("cuda", enabled=use_amp):
            logits = model(batch_x)
            loss = _weighted_segmentation_loss(logits, batch_y)
        scaler.scale(loss).backward()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        scaler.step(optimizer)
        scaler.update()
        scheduler.step()
        running_loss += float(loss.item())

        if step % 200 == 0:
            avg_loss = running_loss / 200.0
            running_loss = 0.0
            print(
                json.dumps(
                    {
                        "step": step,
                        "steps": steps,
                        "loss": round(avg_loss, 4),
                        "lr": round(float(scheduler.get_last_lr()[0]), 6),
                        "elapsed_s": round(time.perf_counter() - started, 1),
                    }
                ),
                flush=True,
            )

        if step % eval_every == 0 or step == steps:
            tmp_ckpt = output / f"ckpt_step_{step}.pt"
            torch.save(build_package(0.45, step), tmp_ckpt)
            detector = LearnedDetector(tmp_ckpt, device=device, threshold=0.45)
            sweep = evaluate_scaled_rows(
                detector, dataset_root, val_eval_rows, thresholds
            )
            best_t = max(
                thresholds,
                key=lambda t: (
                    sweep[str(t)]["macro_class_f1"]
                    - min(
                        sweep[str(t)]["normal_false_positive_pixels"] / 25000.0,
                        0.15,
                    )
                ),
            )
            score = sweep[str(best_t)]["macro_class_f1"]
            history.append(
                {
                    "step": step,
                    "selected_threshold": best_t,
                    "val_macro_f1": score,
                    "val_fg_f1": sweep[str(best_t)]["foreground"]["f1"],
                    "val_burr_f1": sweep[str(best_t)]["per_kind"]["burr"]["f1"],
                    "val_normal_fp": sweep[str(best_t)][
                        "normal_false_positive_pixels"
                    ],
                }
            )
            print(
                json.dumps({"validation": history[-1]}),
                flush=True,
            )
            if score > best_score:
                best_score = score
                best_threshold = best_t
                best_step = step
                torch.save(build_package(best_t, step), best_path)
            tmp_ckpt.unlink(missing_ok=True)
            model.train()

    elapsed = time.perf_counter() - started
    receipt = {
        "trained": True,
        "device": device,
        "parameter_count": param_count,
        "width": width,
        "steps": steps,
        "batch_size": batch_size,
        "chip_presentations": steps * batch_size,
        "train_scaled_views": len(train_scaled),
        "train_legacy_views": len(legacy_train),
        "total_train_views": len(train_scaled) + len(legacy_train),
        "val_views": len(val_scaled),
        "selected_step": best_step,
        "selected_threshold": best_threshold,
        "best_val_macro_f1": best_score,
        "seconds": round(elapsed, 2),
        "checkpoint": str(best_path),
        "checkpoint_sha256": file_sha256(best_path),
        "feature_version": FEATURE_VERSION,
        "rgb_dropout_prob": rgb_dropout_prob,
        "residual_tilt_prob": residual_tilt_prob,
        "test_used_for_selection": False,
        "real_scans_used": False,
        "history": history,
    }
    save_json(output / "training_receipt.json", receipt)
    return receipt


def run_head_to_head_benchmark(
    dataset_root,
    v2_checkpoint,
    output_dir,
    *,
    v1_checkpoint=DEFAULT_V1_CHECKPOINT,
):
    """Compare frozen v1 (1-CAD, 252 views) vs v2 (scaled multi-CAD) on held-out test sets."""
    import torch

    dataset_root = Path(dataset_root).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    manifest = json.loads(
        (dataset_root / "manifest.json").read_text(encoding="utf-8")
    )
    test_021_rows = [
        row for row in manifest["samples"] if row["split"] == "test"
    ]
    normal_021_rows = [
        row
        for row in test_021_rows
        if row["sample_kind"] == "normal_hard_negative"
    ]
    inter_021_rows = [
        row
        for row in test_021_rows
        if row["sample_kind"] == "intermediate_state"
    ]
    high_drift_normal_021 = [
        row for row in normal_021_rows if row["regime"] == "high_drift_warped"
    ]

    detectors = {
        "v1_frozen_single_cad": LearnedDetector(v1_checkpoint, device=device),
        "v2_scaled_multicad": LearnedDetector(v2_checkpoint, device=device),
    }

    report = {
        "dataset_root": str(dataset_root),
        "unseen_real_cad_021_views": len(test_021_rows),
        "unseen_021_normal_hard_negatives": len(normal_021_rows),
        "unseen_021_high_drift_normals": len(high_drift_normal_021),
        "unseen_021_intermediate_views": len(inter_021_rows),
        "models": {},
    }

    for name, det in detectors.items():
        t0 = time.perf_counter()
        unseen_021 = evaluate_scaled_rows(
            det, dataset_root, test_021_rows, [det.threshold]
        )[str(det.threshold)]
        unseen_021_zero_rgb = evaluate_scaled_rows(
            det,
            dataset_root,
            test_021_rows,
            [det.threshold],
            zero_rgb=True,
        )[str(det.threshold)]
        high_drift_normal_eval = evaluate_scaled_rows(
            det, dataset_root, high_drift_normal_021, [det.threshold]
        )[str(det.threshold)]
        legacy_097 = evaluate_legacy_test(det)
        report["models"][name] = {
            "metadata": det.metadata(),
            "eval_seconds": round(time.perf_counter() - t0, 2),
            "unseen_real_cad_021": unseen_021,
            "unseen_021_zero_rgb_ablation": unseen_021_zero_rgb,
            "unseen_021_high_drift_normal_stress": {
                "view_count": len(high_drift_normal_021),
                "false_positive_pixels": high_drift_normal_eval[
                    "normal_false_positive_pixels"
                ],
                "fp_pixels_per_view": round(
                    high_drift_normal_eval["normal_false_positive_pixels"]
                    / max(len(high_drift_normal_021), 1),
                    2,
                ),
            },
            "frozen_historical_097_test": legacy_097,
        }
        print(
            json.dumps(
                {
                    "evaluated": name,
                    "021_macro_f1": round(unseen_021["macro_class_f1"], 4),
                    "021_fg_f1": round(
                        unseen_021["foreground"]["f1"] or 0.0, 4
                    ),
                    "021_burr_f1": round(
                        unseen_021["per_kind"]["burr"]["f1"] or 0.0, 4
                    ),
                    "021_normal_fp": unseen_021["normal_false_positive_pixels"],
                    "021_zero_rgb_macro_f1": round(
                        unseen_021_zero_rgb["macro_class_f1"], 4
                    ),
                    "097_legacy_macro_f1": round(
                        legacy_097["metrics"]["macro_class_f1"], 4
                    ),
                    "097_legacy_burr_recall": round(
                        legacy_097["metrics"]["per_kind"]["burr"]["recall"]
                        or 0.0,
                        4,
                    ),
                }
            ),
            flush=True,
        )

    save_json(output / "benchmark_v1_vs_v2.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    train_p = sub.add_parser("train")
    train_p.add_argument("--dataset", required=True)
    train_p.add_argument("--output", required=True)
    train_p.add_argument("--width", type=int, default=24)
    train_p.add_argument("--steps", type=int, default=3600)
    train_p.add_argument("--batch-size", type=int, default=24)
    train_p.add_argument("--lr", type=float, default=1.5e-3)
    train_p.add_argument("--workers", type=int, default=8)
    train_p.add_argument("--seed", type=int, default=20261124)
    train_p.add_argument("--no-v1-train", action="store_true")

    eval_p = sub.add_parser("benchmark")
    eval_p.add_argument("--dataset", required=True)
    eval_p.add_argument("--v2-checkpoint", required=True)
    eval_p.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    if args.command == "train":
        receipt = train_scaled_detector(
            args.dataset,
            args.output,
            width=args.width,
            steps=args.steps,
            batch_size=args.batch_size,
            lr=args.lr,
            workers=args.workers,
            seed=args.seed,
            include_v1_train=not args.no_v1_train,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        run_head_to_head_benchmark(
            args.dataset, args.v2_checkpoint, args.output
        )


if __name__ == "__main__":
    main()
