"""Ground truth is used only HERE, after the detector and planner have finished."""

import numpy as np
from scipy.ndimage import map_coordinates
from scipy.spatial import cKDTree

from .perception import unproject


def truth_image(coupon, depth, K, T):
    p = unproject(depth, K, T) - coupon.center
    ny, nx = coupon.labels.shape
    ix = (p[:, :, 0] / coupon.halfsize[0] + 1) * (nx - 1) / 2
    iy = (p[:, :, 1] / coupon.halfsize[1] + 1) * (ny - 1) / 2
    return map_coordinates(
        coupon.labels, [iy, ix], order=0, mode="constant", cval=0
    )


def score(detections, truth, paths, K, T, depth):
    pred = np.zeros(truth.shape, np.uint8)
    for d in detections:
        if d.kind in ("seam", "burr"):
            pred[tuple(d.pixels.T)] = 1 if d.kind == "seam" else 2
    target = np.isin(truth, [1, 2])
    mask = pred > 0
    tp = int((mask & target).sum())
    fp = int((mask & ~target).sum())
    fn = int((~mask & target).sum())
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    result = dict(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=2 * precision * recall / max(precision + recall, 1e-9),
        normal_structure_selected_pixels=int((mask & (truth == 3)).sum()),
        selected_pixels=int(mask.sum()),
        unsupported_stump_selected_pixels=int((mask & (truth == 4)).sum()),
        auto_regions=sum(d.kind in ("seam", "burr") for d in detections),
        review_regions=sum(d.kind == "review" for d in detections),
        wrong_type_pixels=int((mask & target & (pred != truth)).sum()),
    )
    if paths and target.any():
        pts = np.vstack([p["xyz"] for p in paths])
        t = unproject(depth, K, T)[target]
        result["defect_footprint_coverage_3mm"] = float(
            (cKDTree(pts[:, :2]).query(t[:, :2])[0] <= 0.003).mean()
        )
    else:
        result["defect_footprint_coverage_3mm"] = (
            None if not target.any() else 0.0
        )
    result["coverage_definition"] = (
        "XY footprint within nominal 3 mm tool radius; not removal or finish quality"
    )
    return result
