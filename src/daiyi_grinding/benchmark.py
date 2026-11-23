"""Fixed held-out seeds, paired baselines, negative and stress cases."""

import argparse
import json
import shutil
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from . import project
from .calibration import load_inspection_calibration
from .evaluation import score, truth_image
from .geometry import generate
from .perception import detect, observe_noise, overlay
from .planning import plan
from .run import write_json
from .sim import Simulation


def benchmark(out, seeds=4, seed_start=2007):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        project.SOURCE,
        out / "source_snapshot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    records = []
    protocol = dict(
        seeds=list(range(seed_start, seed_start + seeds)),
        development_seeds=[7],
        prior_audit_seeds=[1007, 1008, 1009, 1010, 2007, 2008, 2009, 2010],
        cases=["mixed", "seam", "burr", "normal", "thin", "coarse", "noisy"],
        methods=["threshold", "geometry"],
        noise_mm=0.06,
        stress_noise_mm=0.25,
        dropout=0.003,
        stress_dropout=0.025,
        scope="paired perception and generated-path footprint only; dynamics assessed separately",
        frozen_before_run=True,
        source_directory="source_snapshot",
    )
    write_json(out / "protocol.json", protocol)
    for case in protocol["cases"]:
        for seed in protocol["seeds"]:
            coupon = generate(seed, case)
            sim = Simulation(coupon)
            try:
                rgb = sim.render()
                depth = sim.render(depth=True)
                trueK, trueT = sim.camera_info()
                K, T = load_inspection_calibration(
                    project.ROOT / "results/calibration/calibration.json",
                    camera_position=sim.data.cam_xpos[
                        sim.model.camera("inspection").id
                    ],
                )
                noisy, d = observe_noise(
                    rgb,
                    depth,
                    seed,
                    0.25 if case == "noisy" else 0.06,
                    0.025 if case == "noisy" else 0.003,
                )
                truth = truth_image(coupon, depth, trueK, trueT)
                for method in protocol["methods"]:
                    started = time.perf_counter()
                    dets, aux = detect(noisy, d, K, T, method)
                    # Do not spend time planning the knowingly unsafe global-height ablation.
                    paths = (
                        plan(dets, aux, clearance_mm=3)
                        if method == "geometry"
                        else []
                    )
                    m = score(dets, truth, paths, trueK, trueT, depth)
                    row = dict(
                        seed=seed,
                        case=case,
                        method=method,
                        perception_and_plan_s=time.perf_counter() - started,
                        **m,
                    )
                    records.append(row)
                    with (out / "episodes.jsonl").open("a") as f:
                        f.write(json.dumps(row) + "\n")
                    if seed == protocol["seeds"][0]:
                        Image.fromarray(overlay(noisy, dets, paths, K, T)).save(
                            out / f"{case}_{method}.png"
                        )
                    print(
                        json.dumps(
                            {
                                k: row[k]
                                for k in (
                                    "seed",
                                    "case",
                                    "method",
                                    "precision",
                                    "recall",
                                    "normal_structure_selected_pixels",
                                    "auto_regions",
                                )
                            }
                        ),
                        flush=True,
                    )
            finally:
                sim.close()
    grouped = defaultdict(list)
    for r in records:
        grouped[(r["case"], r["method"])].append(r)
    summary = []
    for (case, method), rs in grouped.items():
        tp = sum(r["tp"] for r in rs)
        fp = sum(r["fp"] for r in rs)
        fn = sum(r["fn"] for r in rs)
        p = tp / max(tp + fp, 1)
        r = tp / max(tp + fn, 1)
        summary.append(
            dict(
                case=case,
                method=method,
                episodes=len(rs),
                precision=p,
                recall=r,
                f1=2 * p * r / max(p + r, 1e-9),
                normal_structure_selected_pixels=sum(
                    x["normal_structure_selected_pixels"] for x in rs
                ),
                unsupported_stump_selected_pixels=sum(
                    x["unsupported_stump_selected_pixels"] for x in rs
                ),
                no_action_episodes=sum(x["auto_regions"] == 0 for x in rs),
                review_episodes=sum(x["review_regions"] > 0 for x in rs),
                mean_coverage_3mm=float(
                    np.mean(
                        [
                            x["defect_footprint_coverage_3mm"]
                            for x in rs
                            if x["defect_footprint_coverage_3mm"] is not None
                        ]
                    )
                )
                if case != "normal"
                else None,
            )
        )
    write_json(
        out / "summary.json",
        dict(protocol=protocol, summary=summary, completed=len(records)),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default=str(project.RUNS / "benchmark_new"))
    p.add_argument("--seeds", type=int, default=6)
    p.add_argument("--seed-start", type=int, default=3007)
    a = p.parse_args()
    benchmark(a.output, a.seeds, a.seed_start)


if __name__ == "__main__":
    main()
