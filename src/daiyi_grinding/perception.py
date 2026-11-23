"""RGB-D-only geometric baseline. No simulator object IDs or defect metadata."""

from dataclasses import dataclass

import cv2
import numpy as np
from scipy import ndimage as ndi


@dataclass
class Detection:
    id: int
    kind: str
    pixels: np.ndarray
    xyz: np.ndarray
    length_mm: float
    width_mm: float
    height_mm: float
    confidence: float
    reason: str

    def summary(self):
        return dict(
            id=self.id,
            kind=self.kind,
            pixels=len(self.pixels),
            length_mm=self.length_mm,
            width_mm=self.width_mm,
            height_mm=self.height_mm,
            confidence=self.confidence,
            reason=self.reason,
            bbox=[
                int(self.pixels[:, 1].min()),
                int(self.pixels[:, 0].min()),
                int(self.pixels[:, 1].max()),
                int(self.pixels[:, 0].max()),
            ],
        )


def unproject(depth, K, T):
    yy, xx = np.indices(depth.shape)
    p = np.stack(
        [
            (xx - K[0, 2]) * depth / K[0, 0],
            (yy - K[1, 2]) * depth / K[1, 1],
            depth,
        ],
        axis=-1,
    )
    return p @ T[:3, :3].T + T[:3, 3]


def project(points, K, T):
    p = (np.asarray(points) - T[:3, 3]) @ T[:3, :3]
    return np.stack(
        [
            p[..., 0] / p[..., 2] * K[0, 0] + K[0, 2],
            p[..., 1] / p[..., 2] * K[1, 1] + K[1, 2],
        ],
        axis=-1,
    )


def observe_noise(rgb, depth, seed, noise_mm=0.06, dropout=0.003):
    rng = np.random.default_rng(seed + 9183)
    d = depth.copy() + rng.normal(0, noise_mm / 1000, depth.shape)
    d[rng.random(d.shape) < dropout] = np.nan
    return rgb.copy(), d.astype(np.float32)


def roi_mask(shape, roi):
    if roi is None:
        return np.ones(shape, bool)
    a = np.asarray(roi, float)
    if a.ndim != 2 or a.shape[1] != 2 or len(a) < 3 or not np.isfinite(a).all():
        raise ValueError(
            "ROI must contain at least three finite [x,y] pixel vertices"
        )
    if (
        np.any(a[:, 0] < 0)
        or np.any(a[:, 0] >= shape[1])
        or np.any(a[:, 1] < 0)
        or np.any(a[:, 1] >= shape[0])
    ):
        raise ValueError("ROI is outside the captured image")
    m = np.zeros(shape, np.uint8)
    cv2.fillPoly(m, [np.rint(a).astype(np.int32)], 1)
    return m.astype(bool)


def detect(
    rgb, depth, K, T, method="geometry", roi=None, inspection_bounds=None
):
    if depth.ndim != 2 or rgb.shape[:2] != depth.shape:
        raise ValueError("RGB/depth shape mismatch")
    valid = np.isfinite(depth) & (depth > 0.1) & (depth < 2)
    if valid.mean() < 0.7:
        raise ValueError("Too few valid depth measurements; reacquire")
    filled = depth.copy()
    if not valid.all():
        inds = ndi.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled[~valid] = depth[tuple(inds[:, ~valid])]
    xyz = unproject(filled, K, T)
    # Configured inspection volume, shared across every part/seed.
    bounds = np.asarray(
        inspection_bounds
        if inspection_bounds is not None
        else ((-0.67, -0.27, 0.612), (-0.31, 0.03, 0.655)),
        dtype=float,
    )
    if (
        bounds.shape != (2, 3)
        or not np.isfinite(bounds).all()
        or np.any(bounds[0] >= bounds[1])
    ):
        raise ValueError("Inspection bounds require finite ordered XYZ limits")
    surface = np.all((xyz > bounds[0]) & (xyz < bounds[1]), axis=-1)
    surface = ndi.binary_erosion(surface, iterations=3)
    regions, count = ndi.label(surface)
    if count:
        sizes = np.bincount(regions.ravel())
        sizes[0] = 0
        surface = regions == int(sizes.argmax())
    h = cv2.medianBlur(xyz[:, :, 2].astype(np.float32), 3)
    scale = (
        float(np.median(filled[surface]) / K[0, 0]) if surface.any() else 0.0004
    )
    if not surface.any():
        return [], dict(
            xyz=xyz,
            height=h,
            residual=np.zeros_like(h),
            mask=surface,
            scale=scale,
        )
    if method == "threshold":
        residual = h - np.median(h[surface])
    elif method == "geometry":
        radius = max(3, int(round(0.006 / scale)))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1)
        )
        base = cv2.morphologyEx(h, cv2.MORPH_OPEN, kernel)
        residual = h - base
    else:
        raise ValueError(method)
    if method == "geometry":
        # Suppress discontinuities adjacent to a recessed opening; these are not
        # evidence of excess material. The depth comparison uses this observation.
        below = h < np.median(h[surface]) - 0.004
        edge_guard = ndi.binary_dilation(below, iterations=7)
        surface = surface & ~edge_guard
    selection = roi_mask(depth.shape, roi)
    # Classify full features BEFORE applying human ROI. Cropping a design rib
    # must never turn its small visible fragment into a new 'burr'.
    mask = (residual > 0.00065) & surface & valid
    mask = ndi.binary_closing(mask, iterations=2) & surface
    labels, n = ndi.label(mask)
    dets = []
    for i in range(1, n + 1):
        pix = np.argwhere(labels == i)
        if len(pix) < 10:
            continue
        p = xyz[pix[:, 0], pix[:, 1]]
        q = p[:, :2] - p[:, :2].mean(axis=0)
        _, _, v = np.linalg.svd(q, full_matrices=False)
        bounds = np.quantile(q @ v.T, [0.025, 0.975], axis=0)
        length, width = np.maximum(bounds[1] - bounds[0], scale) * 1000
        if method == "geometry":
            r0, c0 = pix.min(axis=0)
            r1, c1 = pix.max(axis=0) + 1
            local_mask = np.pad((labels[r0:r1, c0:c1] == i), 1)
            distances = ndi.distance_transform_edt(local_mask)
            # Thickness along a curved centerline, unlike global PCA width.
            width = float(
                2 * np.quantile(distances[local_mask], 0.90) * scale * 1000
            )
        height = float(np.quantile(residual[pix[:, 0], pix[:, 1]], 0.95) * 1000)
        if method == "threshold":
            kind = "seam" if length / width > 4 else "burr"
            reason = "global height baseline"
        elif length > 18 and width < 4.2 and length / width > 5:
            kind = "seam"
            reason = "narrow elongated positive ridge"
        elif length < 14 and width < 10 and height > 0.85 and len(pix) >= 60:
            kind = "burr"
            reason = "localized positive protrusion"
        else:
            kind = "preserve"
            reason = "broad/design-like geometry or ambiguous feature"
        # Apply the machining envelope to candidate cuts. A broad feature already
        # left unselected is not reclassified as a defect solely for being tall.
        if (
            method == "geometry"
            and kind in ("seam", "burr")
            and (
                height > 5.0
                or np.quantile(p[:, 2], 0.95) - np.median(h[surface]) > 0.006
            )
        ):
            kind = "review"
            reason = "candidate above the light-deburring height envelope"
        chosen = selection[tuple(pix.T)]
        if chosen.sum() < 10:
            continue
        pix = pix[chosen]
        p = p[chosen]
        dets.append(
            Detection(
                i,
                kind,
                pix,
                p,
                float(length),
                float(width),
                height,
                min(0.98, max(0.4, height / 2.5)),
                reason,
            )
        )
    return dets, dict(
        xyz=xyz,
        height=h,
        residual=residual,
        mask=mask,
        scale=scale,
        selection=selection,
        K=K,
        T=T,
    )


COLORS = {
    "seam": (38, 225, 191),
    "burr": (255, 178, 68),
    "preserve": (87, 152, 246),
    "review": (246, 89, 105),
}


def overlay(rgb, detections, paths=None, K=None, T=None):
    image = rgb.copy()
    for d in detections:
        c = COLORS[d.kind]
        rr, cc = d.pixels.T
        image[rr, cc] = (0.3 * image[rr, cc] + 0.7 * np.array(c)).astype(
            np.uint8
        )
        x0, y0, x1, y1 = d.summary()["bbox"]
        cv2.rectangle(image, (x0 - 3, y0 - 3), (x1 + 3, y1 + 3), c, 1)
        cv2.putText(
            image,
            f"{d.id}: {d.kind}",
            (x0, max(15, y0 - 7)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43,
            c,
            1,
            cv2.LINE_AA,
        )
    if paths and K is not None:
        for path in paths:
            p = project(np.array(path["xyz"]), K, T)
            cv2.polylines(
                image,
                [p.round().astype(np.int32)],
                False,
                (255, 76, 125),
                1,
                cv2.LINE_AA,
            )
    return image
