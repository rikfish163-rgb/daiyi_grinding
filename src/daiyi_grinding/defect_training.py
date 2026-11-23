"""Train, freeze and independently evaluate a small 097 RGB-D segmentation model."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import random
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .learned_defects import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    LearnedDetector,
    file_sha256,
    sensor_features,
    tile_origins,
)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def data_index(batches, split_plan):
    """Keep the complete synthetic entity, all cases and all views in one split."""
    groups = {
        key: set(split_plan[f"{key}_seeds"])
        for key in ("training", "validation", "test")
    }
    if any(
        groups[first] & groups[second]
        for first, second in (
            ("training", "validation"),
            ("training", "test"),
            ("validation", "test"),
        )
    ):
        raise ValueError(
            "Entity seeds overlap between training/validation/test"
        )
    index = {key: [] for key in groups}
    identities = set()
    batch_sources = []
    for batch in batches:
        batch = Path(batch).resolve()
        manifest = json.loads((batch / "manifest.json").read_text())
        protocol = manifest["protocol"]
        if protocol["real_scans_used"]:
            raise ValueError("This experiment is explicitly synthetic-only")
        batch_sources.append(
            {
                "directory": str(batch),
                "manifest_sha256": file_sha256(batch / "manifest.json"),
                "generation_source_hashes": protocol["source_hashes"],
                "profile": protocol["profile"]["profile"],
            }
        )
        for sample in manifest["samples"]:
            split = next(
                (
                    key
                    for key, seeds in groups.items()
                    if sample["seed"] in seeds
                ),
                None,
            )
            if split is None:
                raise ValueError(f"Unassigned entity seed {sample['seed']}")
            for view in sample["views"]:
                identity = (sample["seed"], sample["case"], view["index"])
                if identity in identities:
                    raise ValueError(f"Duplicate observation: {identity}")
                identities.add(identity)
                folder = batch / sample["sample_directory"]
                public = folder / view["public_observation"]
                labels = folder / view["private_labels"]
                if not public.is_file() or not labels.is_file():
                    raise ValueError(f"Incomplete observation: {identity}")
                index[split].append(
                    {
                        "part_id": sample["part_id"],
                        "seed": sample["seed"],
                        "case": sample["case"],
                        "view": view["index"],
                        "public": str(public),
                        "labels": str(labels),
                    }
                )
    for split, seeds in groups.items():
        if {row["seed"] for row in index[split]} != seeds:
            raise ValueError(f"Missing complete entity seeds in {split}")
    return index, batch_sources


def load_public(row):
    with np.load(row["public"], allow_pickle=False) as arrays:
        return {
            key: arrays[key] for key in ("rgb", "depth", "reference_depth", "K")
        }


def load_labels(row):
    with np.load(row["labels"], allow_pickle=False) as arrays:
        semantic = arrays["semantic"]
        labels = np.where((semantic == 1) | (semantic == 2), semantic, 0)
    return labels.astype(np.uint8)


def prepare_chips(index, output, seed=1121):
    """Select training chips only; threshold selection uses complete val views."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(seed)
    entries = []
    for number, row in enumerate(index["training"]):
        public = load_public(row)
        features = sensor_features(
            public["rgb"],
            public["depth"],
            public["reference_depth"],
            public["K"],
        )
        labels = load_labels(row)
        height, width = labels.shape
        origins = [
            (r, c) for r in tile_origins(height) for c in tile_origins(width)
        ]
        positive = [
            (r, c)
            for r, c in origins
            if np.count_nonzero(labels[r : r + 256, c : c + 256]) >= 3
        ]
        chosen = set()
        for kind in (1, 2):
            pixels = np.argwhere(labels == kind)
            if len(pixels):
                for pixel in pixels[
                    rng.integers(0, len(pixels), size=min(3, len(pixels)))
                ]:
                    jitter = rng.integers(-48, 49, 2)
                    r, c = np.clip(
                        pixel + jitter - 128,
                        [0, 0],
                        [height - 256, width - 256],
                    )
                    chosen.add((int(r), int(c)))
        if positive:
            chosen.add(positive[int(rng.integers(0, len(positive)))])
        # Full nominal structures, nearby hard negatives and pure normal cases
        # are included; no candidate mask from the old detector limits input.
        for position in rng.choice(len(origins), size=2, replace=False):
            chosen.add(origins[position])
        for r, c in sorted(chosen):
            identity = f"chip_{len(entries):05d}"
            x_path, y_path = (
                output / f"{identity}_x.npy",
                output / f"{identity}_y.npy",
            )
            np.save(
                x_path, features[:, r : r + 256, c : c + 256].astype(np.float16)
            )
            np.save(y_path, labels[r : r + 256, c : c + 256])
            entries.append(
                {
                    **row,
                    "x": str(x_path),
                    "y": str(y_path),
                    "row": r,
                    "col": c,
                    "foreground_pixels": int(
                        np.count_nonzero(labels[r : r + 256, c : c + 256])
                    ),
                }
            )
        if number % 24 == 0:
            print(
                json.dumps({"preparing_view": number, "chips": len(entries)}),
                flush=True,
            )
    save_json(
        output / "manifest.json",
        {
            "feature_version": FEATURE_VERSION,
            "sensor_features_sha256": hashlib.sha256(
                inspect.getsource(sensor_features).encode()
            ).hexdigest(),
            "training_chips": entries,
        },
    )
    return entries


def confusion_counts(truth, probabilities, threshold):
    prediction = probabilities.argmax(axis=0)
    prediction[probabilities[1:].max(axis=0) < threshold] = 0
    confusion = np.bincount(
        (truth.ravel().astype(np.int64) * 3 + prediction.ravel()),
        minlength=9,
    ).reshape(3, 3)
    return confusion


def score_confusion(confusion):
    results = {}
    for label, kind in ((1, "seam"), (2, "burr")):
        tp = int(confusion[label, label])
        fp = int(confusion[:, label].sum() - tp)
        fn = int(confusion[label, :].sum() - tp)
        results[kind] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
            "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
        }
    tp = int(confusion[1:, 1:].sum())
    fp = int(confusion[0, 1:].sum())
    fn = int(confusion[1:, 0].sum())
    foreground = {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        "iou": tp / (tp + fp + fn) if tp + fp + fn else None,
    }
    macro = np.mean([item["f1"] or 0 for item in results.values()])
    return {
        "foreground": foreground,
        "per_kind": results,
        "macro_class_f1": float(macro),
    }


def validation_sweep(detector, rows, thresholds):
    matrices = {
        threshold: np.zeros((3, 3), np.int64) for threshold in thresholds
    }
    normal_pixels = dict.fromkeys(thresholds, 0)
    for number, row in enumerate(rows):
        public = load_public(row)
        probabilities = detector.probabilities(
            public["rgb"],
            public["depth"],
            public["reference_depth"],
            public["K"],
        )
        # Full-view labels are read after prediction, including edges and tiny
        # parts. These are raw semantic metrics, not machining-gated metrics.
        truth = load_labels(row)
        for threshold in thresholds:
            confusion = confusion_counts(truth, probabilities, threshold)
            matrices[threshold] += confusion
            if row["case"] == "casting_normal":
                normal_pixels[threshold] += int(confusion[0, 1:].sum())
        if number % 24 == 0:
            print(json.dumps({"validation_view": number}), flush=True)
    return {
        str(threshold): {
            **score_confusion(confusion),
            "normal_false_positive_pixels": normal_pixels[threshold],
            "confusion": confusion.tolist(),
        }
        for threshold, confusion in matrices.items()
    }


def train(args):
    import torch
    from torch.utils.data import DataLoader, Dataset

    from .defect_network import DefectUNet, segmentation_loss

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    split_plan = json.loads(Path(args.split_plan).read_text())
    index, sources = data_index(args.batch, split_plan)
    save_json(
        output / "split_index.json", {"splits": index, "sources": sources}
    )
    save_json(output / "config.json", vars(args))
    if args.chip_cache:
        cached = json.loads(
            (Path(args.chip_cache) / "manifest.json").read_text()
        )
        if (
            cached["feature_version"] != FEATURE_VERSION
            or cached["sensor_features_sha256"]
            != hashlib.sha256(
                inspect.getsource(sensor_features).encode()
            ).hexdigest()
        ):
            raise ValueError("Prepared chip feature implementation mismatch")
        chips = cached["training_chips"]
        observed_views = {
            (row["seed"], row["case"], row["view"]) for row in chips
        }
        expected_views = {
            (row["seed"], row["case"], row["view"]) for row in index["training"]
        }
        if observed_views != expected_views:
            raise ValueError(
                "Prepared chips must contain training entities only"
            )
    else:
        chips = prepare_chips(index, output / "chips", args.seed)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.set_num_threads(4)
    torch.set_float32_matmul_precision("high")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        torch.cuda.manual_seed_all(args.seed)
        torch.backends.cudnn.benchmark = True

    class Chips(Dataset):
        def __init__(self):
            self.cache = OrderedDict()

        def __len__(self):
            return len(chips)

        def __getitem__(self, identity):
            if identity not in self.cache:
                row = chips[identity]
                self.cache[identity] = (
                    np.load(row["x"], allow_pickle=False),
                    np.load(row["y"], allow_pickle=False),
                )
                if len(self.cache) > 96:
                    self.cache.popitem(last=False)
            x, y = self.cache[identity]
            x, y = x.astype(np.float32), y.astype(np.int64)
            if random.random() < 0.5:
                x, y = x[:, :, ::-1].copy(), y[:, ::-1].copy()
            if random.random() < 0.5:
                x, y = x[:, ::-1].copy(), y[::-1].copy()
            turns = random.randrange(4)
            x = np.rot90(x, turns, axes=(1, 2)).copy()
            y = np.rot90(y, turns).copy()
            # Normals enter as magnitudes; rotating the image also swaps x/y
            # components. RGB exposure and depth noise are independent of class.
            if turns % 2:
                x[[6, 7]] = x[[7, 6]]
            x[:3] = np.clip(
                x[:3] * random.uniform(0.85, 1.15)
                + random.uniform(-0.08, 0.08),
                -1,
                1,
            )
            return torch.from_numpy(x), torch.from_numpy(y)

    model = DefectUNet(width=args.width).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=0.01
    )
    loader = DataLoader(
        Chips(), batch_size=args.batch_size, shuffle=True, num_workers=0
    )
    started = time.monotonic()
    history = []
    best_score = -1.0
    parameter_count = sum(parameter.numel() for parameter in model.parameters())

    def package(threshold, step):
        return {
            "state_dict": {
                key: value.detach().cpu()
                for key, value in model.state_dict().items()
            },
            "model_config": model.config,
            "parameter_count": parameter_count,
            "feature_version": FEATURE_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "probability_threshold": threshold,
            "training_step": step,
            "training_seeds": split_plan["training_seeds"],
            "validation_seeds": split_plan["validation_seeds"],
            "test_seeds": split_plan["test_seeds"],
            "test_used_for_selection": False,
            "real_scans_used": False,
        }

    # Verify gradients and that a fixed batch is learnable before the full run.
    fixed_x, fixed_y = next(iter(loader))
    fixed_x, fixed_y = fixed_x.to(device), fixed_y.to(device)
    fixed_losses = []
    for step in range(80):
        logits = model(fixed_x)
        loss = segmentation_loss(logits, fixed_y)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        fixed_losses.append(float(loss.item()))
    save_json(
        output / "one_batch_check.json",
        {
            "losses": fixed_losses,
            "loss_decreased": fixed_losses[-1] < fixed_losses[0] * 0.7,
        },
    )
    if (
        not np.isfinite(fixed_losses).all()
        or fixed_losses[-1] >= fixed_losses[0] * 0.7
    ):
        raise RuntimeError("One-batch learning check failed")

    iterator = iter(loader)
    running = []
    for step in range(1, args.steps + 1):
        try:
            x, y = next(iterator)
        except StopIteration:
            iterator = iter(loader)
            x, y = next(iterator)
        x, y = x.to(device), y.to(device)
        model.train()
        progress = step / args.steps
        lr = args.lr * (0.1 + 0.9 * (1 + math.cos(math.pi * progress)) / 2)
        for group in optimizer.param_groups:
            group["lr"] = lr
        with torch.autocast(
            device_type=device, dtype=torch.bfloat16, enabled=device == "cuda"
        ):
            loss = segmentation_loss(model(x), y)
        if not torch.isfinite(loss):
            raise RuntimeError(f"Nonfinite training loss at step {step}")
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        running.append(float(loss.item()))
        if step % 50 == 0:
            print(
                json.dumps(
                    {
                        "step": step,
                        "loss": float(np.mean(running[-50:])),
                        "seconds": time.monotonic() - started,
                    }
                ),
                flush=True,
            )
        if step % args.validate_every == 0 or step == args.steps:
            model.eval()
            candidate = output / "candidate.pt"
            torch.save(package(0.5, step), candidate)
            detector = LearnedDetector(candidate, device=device)
            sweep = validation_sweep(
                detector, index["validation"], [0.35, 0.5, 0.65, 0.8, 0.9]
            )
            # Select on validation only, penalizing normal-piece false positives.
            best_threshold, metrics = max(
                sweep.items(),
                key=lambda pair: (
                    pair[1]["macro_class_f1"]
                    - min(0.2, pair[1]["normal_false_positive_pixels"] / 3000)
                ),
            )
            score = metrics["macro_class_f1"] - min(
                0.2, metrics["normal_false_positive_pixels"] / 3000
            )
            item = {
                "step": step,
                "training_loss": float(np.mean(running[-50:])),
                "threshold": float(best_threshold),
                "validation": metrics,
                "sweep": sweep,
            }
            history.append(item)
            save_json(output / "history.json", history)
            if score > best_score:
                best_score = score
                torch.save(
                    package(float(best_threshold), step), output / "model.pt"
                )
                save_json(output / "selected_validation.json", item)
            print(
                json.dumps(
                    {
                        "step": step,
                        "validation_f1": metrics["foreground"]["f1"],
                        "macro_class_f1": metrics["macro_class_f1"],
                        "normal_fp": metrics["normal_false_positive_pixels"],
                        "threshold": best_threshold,
                    }
                ),
                flush=True,
            )
            del detector
    receipt = {
        "trained": True,
        "device": device,
        "torch_version": torch.__version__,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "parameter_count": parameter_count,
        "steps": args.steps,
        "seconds": time.monotonic() - started,
        "checkpoint_sha256": file_sha256(output / "model.pt"),
        "feature_version": FEATURE_VERSION,
        "split_counts": {key: len(rows) for key, rows in index.items()},
        "training_chips": len(chips),
        "test_read_during_training": False,
        "real_scans_used": False,
        "sources": sources,
    }
    save_json(output / "training_receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


def evaluate(args):
    """Read frozen test labels only after the frozen model predicts each view."""
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    split = json.loads(Path(args.split_index).read_text())["splits"]
    detector = LearnedDetector(args.checkpoint)
    rows = split["test"]
    total = np.zeros((3, 3), np.int64)
    by_case = {}
    normal_fp = 0
    previews = []
    records = []
    for number, row in enumerate(rows):
        public = load_public(row)
        probabilities = detector.probabilities(
            public["rgb"],
            public["depth"],
            public["reference_depth"],
            public["K"],
        )
        labels = load_labels(row)
        confusion = confusion_counts(labels, probabilities, detector.threshold)
        total += confusion
        by_case.setdefault(row["case"], np.zeros((3, 3), np.int64))
        by_case[row["case"]] += confusion
        if row["case"] == "casting_normal":
            normal_fp += int(confusion[0, 1:].sum())
        record = {
            **row,
            "metrics": score_confusion(confusion),
            "confusion": confusion.tolist(),
        }
        records.append(record)
        if row["view"] == 0:
            predicted = probabilities.argmax(axis=0)
            predicted[probabilities[1:].max(axis=0) < detector.threshold] = 0
            image = public["rgb"].copy()
            for label, color in ((1, (26, 190, 149)), (2, (242, 161, 45))):
                selected = predicted == label
                image[selected] = (
                    image[selected] * 0.35 + np.asarray(color) * 0.65
                ).astype(np.uint8)
            identity = f"{row['case']}_seed{row['seed']}"
            Image.fromarray(image).save(output / f"{identity}_prediction.png")
            np.savez_compressed(
                output / f"{identity}_prediction.npz",
                probability=probabilities.astype(np.float16),
                semantic=predicted.astype(np.uint8),
            )
            if row["seed"] == min(item["seed"] for item in rows):
                previews.append(
                    (
                        image,
                        row["case"],
                        score_confusion(confusion)["foreground"]["f1"],
                    )
                )
        if number % 24 == 0:
            print(json.dumps({"test_view": number}), flush=True)
    result = {
        "checkpoint": detector.metadata(),
        "split": "frozen test entities",
        "seeds": sorted({row["seed"] for row in rows}),
        "view_count": len(rows),
        "cad_count": 1,
        "raw_semantic_metrics": score_confusion(total),
        "normal_false_positive_pixels": normal_fp,
        "per_case": {
            key: score_confusion(value) for key, value in by_case.items()
        },
        "confusion": total.tolist(),
        "scope": "full-view synthetic semantic masks, including tiny and edge pixels; repeated views are correlated; not real performance or machining success",
        "test_used_for_model_selection": False,
        "truth_used_for_inference": False,
        "views": records,
    }
    save_json(output / "evaluation.json", result)
    if previews:
        gallery = Image.new(
            "RGB", (1080, math.ceil(len(previews) / 2) * 450), "#eef2f3"
        )
        draw = ImageDraw.Draw(gallery)
        for index, (image, case, f1) in enumerate(previews):
            x, y = (index % 2) * 540, (index // 2) * 450
            gallery.paste(
                Image.fromarray(image).resize((520, 390)), (x + 10, y + 10)
            )
            draw.text(
                (x + 15, y + 405),
                f"{case} | F1 {f1:.3f}"
                if f1 is not None
                else f"{case} | normal",
                fill="#24333a",
            )
        gallery.save(output / "prediction_gallery.png")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "views"},
            ensure_ascii=False,
        ),
        flush=True,
    )


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    training = commands.add_parser("train")
    training.add_argument("--batch", action="append", required=True)
    training.add_argument("--split-plan", required=True)
    training.add_argument("--output", required=True)
    training.add_argument("--seed", type=int, default=1121)
    training.add_argument("--width", type=int, default=16)
    training.add_argument("--batch-size", type=int, default=8)
    training.add_argument("--steps", type=int, default=1200)
    training.add_argument("--validate-every", type=int, default=400)
    training.add_argument("--lr", type=float, default=0.001)
    training.add_argument("--chip-cache")
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--batch", action="append", required=True)
    preparation.add_argument("--split-plan", required=True)
    preparation.add_argument("--output", required=True)
    preparation.add_argument("--seed", type=int, default=1121)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--checkpoint", required=True)
    evaluation.add_argument("--split-index", required=True)
    evaluation.add_argument("--output", required=True)
    return cli


def main():
    args = parser().parse_args()
    if args.command == "prepare":
        index, _ = data_index(
            args.batch, json.loads(Path(args.split_plan).read_text())
        )
        prepare_chips(index, args.output, args.seed)
    elif args.command == "train":
        train(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
