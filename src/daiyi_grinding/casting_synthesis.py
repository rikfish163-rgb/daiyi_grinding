"""Export research casting morphology, current-cell RGB-D and private labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import mujoco
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial.transform import Rotation

from . import project as workspace
from .casting_defects import (
    CASE_LABELS,
    CASTING_CASES,
    CONFIG_FILE,
    casting_appearance,
)
from .casting_profiles import (
    BASE_PROFILE,
    PROFILE_FILES,
    available_profiles,
    load_profile,
)
from .perception import project
from .workstation_inspection import Inspection
from .workstation_model import (
    CELL_CONFIG_FILE,
    SOURCE_FILES,
    Workstation,
    source_hashes,
    write_json,
)

FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
COLORS = np.array(
    [[20, 24, 28], [24, 184, 158], [245, 162, 48], [115, 126, 140]],
    np.uint8,
)


def _root_arrays(items):
    points, offsets, identities = [], [0], []
    for identity, item in enumerate(items, 1):
        for curve in item.get("root_curves_m", []):
            curve = np.asarray(curve)
            points.extend(curve)
            identities.extend([identity] * len(curve))
            offsets.append(len(points))
    return (
        np.asarray(points, dtype=np.float32).reshape(-1, 3),
        np.asarray(offsets, dtype=np.int32),
        np.asarray(identities, dtype=np.uint16),
    )


def project_roots(points, intrinsics, transform, ideal_depth, measured_depth):
    """Label root depth consistency; hidden roots remain explicitly in truth."""
    camera_points = (points - transform[:3, 3]) @ transform[:3, :3]
    depth = camera_points[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        uv = project(points, intrinsics, transform)
    finite = np.isfinite(uv).all(axis=1)
    pixels = np.rint(np.where(finite[:, None], uv, -1)).astype(np.int32)
    rows, cols = ideal_depth.shape
    inside = (
        finite
        & (depth > 0)
        & (pixels[:, 0] >= 0)
        & (pixels[:, 0] < cols)
        & (pixels[:, 1] >= 0)
        & (pixels[:, 1] < rows)
    )
    visible, valid = np.zeros(len(points), bool), np.zeros(len(points), bool)
    u, v = pixels[inside].T
    visible[inside] = np.abs(ideal_depth[v, u] - depth[inside]) <= 0.00015
    valid[inside] = np.isfinite(measured_depth[v, u])
    return {
        "root_pixels": pixels,
        "root_camera_depth_m": depth.astype(np.float32),
        "root_in_image": inside,
        "root_depth_consistent": visible,
        "root_sensor_depth_valid": valid,
    }


class LabelledInspection(Inspection):
    """Run the existing detector first, then separately export supervision."""

    def __init__(self, sim, seed):
        super().__init__(sim, seed)
        self.label_views = []
        self.root_points = None

    def capture(self, index):
        observation = super().capture(index)
        sim = self.sim
        if self.root_points is None:
            (
                self.root_points,
                self.root_offsets,
                self.root_instances,
            ) = _root_arrays(sim.private_defects)
            self.root_seen = np.zeros(len(self.root_points), bool)
        public, evaluator = sim.output / "public", sim.output / "evaluator"
        public.mkdir(exist_ok=True)
        evaluator.mkdir(exist_ok=True)
        ideal = sim.render(mode="depth")
        geom_ids = sim.render(mode="segmentation")[..., 0]
        semantic = np.zeros(ideal.shape, np.uint8)
        instances = np.zeros(ideal.shape, np.uint16)
        baseline_ids = [
            i
            for i in range(sim.model.ngeom)
            if sim.model.geom(i).name.startswith("wp_")
        ]
        semantic[np.isin(geom_ids, baseline_ids)] = 3
        object_pixels = []
        for identity, item in enumerate(sim.private_defects, 1):
            selected = geom_ids == sim.model.geom(item["geom"]).id
            semantic[selected] = item["label"]
            instances[selected] = identity
            object_pixels.append(int(selected.sum()))
        with np.load(sim.output / f"view_{index:02d}_observation.npz") as raw:
            observed = {key: raw[key] for key in raw.files}
        observed["T_world_workpiece"] = sim.body_transform(sim.workpiece)
        np.savez_compressed(public / f"view_{index:02d}.npz", **observed)
        for extension in ("npz", "json"):
            source = (
                sim.output / f"view_{index:02d}_weldgrind_cloud.{extension}"
            )
            if source.is_file():
                shutil.copyfile(source, public / source.name)
        Image.fromarray(observed["rgb"]).save(
            public / f"view_{index:02d}_rgb.png"
        )
        root_view = project_roots(
            self.root_points,
            observed["K"],
            observed["T_workpiece_camera"],
            ideal,
            observed["depth"],
        )
        self.root_seen |= (
            root_view["root_depth_consistent"]
            & root_view["root_sensor_depth_valid"]
        )
        np.savez_compressed(
            evaluator / f"view_{index:02d}_labels.npz",
            semantic=semantic,
            instance=instances,
            ideal_depth_m=ideal,
            root_points_workpiece_m=self.root_points,
            root_curve_offsets=self.root_offsets,
            root_instances=self.root_instances,
            **root_view,
        )
        annotated = observed["rgb"].copy()
        selected = instances > 0
        annotated[selected] = (
            annotated[selected] * 0.35 + COLORS[semantic[selected]] * 0.65
        ).astype(np.uint8)
        image = Image.fromarray(annotated)
        draw = ImageDraw.Draw(image)
        for point, inside, visible in zip(
            root_view["root_pixels"],
            root_view["root_in_image"],
            root_view["root_depth_consistent"],
        ):
            if inside:
                u, v = point
                color = (32, 245, 98) if visible else (205, 112, 240)
                draw.ellipse((u - 1, v - 1, u + 1, v + 1), fill=color)
        image.save(evaluator / f"view_{index:02d}_annotated.png")
        Image.fromarray(semantic).save(
            evaluator / f"view_{index:02d}_semantic.png"
        )
        self.label_views.append(
            {
                "index": index,
                "encoder_angle_deg": self.views[-1]["encoder_angle_deg"],
                "public_observation": f"public/view_{index:02d}.npz",
                "private_labels": f"evaluator/view_{index:02d}_labels.npz",
                "visible_object_pixels": object_pixels,
                "root_depth_consistent_points": int(
                    root_view["root_depth_consistent"].sum()
                ),
                "root_points": len(self.root_points),
                "root_depth_consistency_tolerance_mm": 0.15,
            }
        )
        return observation


def macro_preview(sim, item, destination):
    """A separate CAD close-up, never substituted for the fixed-camera data."""
    root = np.vstack(item["root_curves_m"])
    pose = sim.body_transform(sim.workpiece)
    center = root.mean(axis=0)
    normal = np.asarray(item["surface_normal"])
    tangent = root[-1] - root[0]
    if np.linalg.norm(tangent) < 1e-5:
        tangent = root[len(root) // 4] - center
    tangent /= np.linalg.norm(tangent)
    lateral = np.cross(normal, tangent)
    lateral /= np.linalg.norm(lateral)
    direction = 0.75 * normal + 0.65 * lateral + 0.15 * tangent
    direction /= np.linalg.norm(direction)
    target = pose[:3, :3] @ center + pose[:3, 3]
    span = np.linalg.norm(root.max(axis=0) - root.min(axis=0))
    distance = max(0.043, span * 1.8)
    position = target + pose[:3, :3] @ direction * distance
    camera_z = (position - target) / distance
    camera_x = np.cross([0.0, 0.0, 1.0], camera_z)
    camera_x /= np.linalg.norm(camera_x)
    camera_y = np.cross(camera_z, camera_x)
    matrix = np.column_stack([camera_x, camera_y, camera_z])
    camera = sim.model.camera("cell_overview").id
    original_pos = sim.model.cam_pos[camera].copy()
    original_quat = sim.model.cam_quat[camera].copy()
    original_fovy = float(sim.model.cam_fovy[camera])
    original_near = float(sim.model.vis.map.znear)
    try:
        sim.model.cam_pos[camera] = position
        sim.model.cam_quat[camera] = Rotation.from_matrix(matrix).as_quat()[
            [3, 0, 1, 2]
        ]
        sim.model.cam_fovy[camera] = 40
        # MuJoCo scales this distance by the complete station's extent.
        # The station's normal near plane otherwise clips these close-ups.
        sim.model.vis.map.znear = min(original_near, 0.0002)
        mujoco.mj_forward(sim.model, sim.data)
        Image.fromarray(
            sim.render(camera="cell_overview", width=900, height=600)
        ).save(destination)
        Image.fromarray(
            sim.render(
                camera="cell_overview", width=900, height=600, reference=True
            )
        ).save(destination.with_name(destination.stem + "_reference.png"))
        segmentation = sim.render(
            camera="cell_overview", width=900, height=600, mode="segmentation"
        )
        return {
            "defect_visible_pixels": int(
                (segmentation[..., 0] == sim.model.geom(item["geom"]).id).sum()
            ),
            "fixed_inspection_camera_used": False,
            "near_plane_world_mm": float(
                sim.model.vis.map.znear * sim.model.stat.extent * 1000
            ),
        }
    finally:
        sim.model.cam_pos[camera] = original_pos
        sim.model.cam_quat[camera] = original_quat
        sim.model.cam_fovy[camera] = original_fovy
        sim.model.vis.map.znear = original_near
        mujoco.mj_forward(sim.model, sim.data)


def _gallery(out, records):
    labels = {
        "variable_flash": "变厚弯曲批缝",
        "interrupted_flash": "断续飞边：片段保留根部",
        "curled_flash": "卷曲薄片：自由边向下弯折",
        "torn_flash": "破裂薄片：断口与不规则缺口",
        "irregular_burr": "局部毛刺：不对称多峰",
    }
    chosen = {}
    for record in records:
        for item in record["objects"]:
            chosen.setdefault(item["morphology"], (record, item))
    font = ImageFont.truetype(FONT_FILE, 24)
    small = ImageFont.truetype(FONT_FILE, 18)
    canvas = Image.new("RGB", (1920, 1040), (241, 243, 244))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (28, 14),
        "CAD 097 · 研究形态合成 / 原始材质三维近景",
        fill=(25, 36, 44),
        font=font,
    )
    draw.text(
        (28, 52),
        "实际固定相机 RGB-D 单独保存；近景用于几何检查，未模拟真实铸造机理。",
        fill=(75, 89, 98),
        font=small,
    )
    tiles = []
    for morphology, (record, item) in chosen.items():
        image = out / record["sample_directory"] / item["macro_preview"]
        tiles.append(
            (
                image,
                labels[morphology],
                f"法向峰值 {item['height_peak_mm']:.2f} mm · 根部片段 {item['geometry_validation']['connected_rooted_fragments']}",
            )
        )
    if tiles:
        image = tiles[0][0]
        tiles.append(
            (
                image.with_name(image.stem + "_reference.png"),
                "正常 CAD 对照：设计结构保留",
                "与第一幅同一位置、同一材质；只隐藏附加余料",
            )
        )
    for index, (path, title, detail) in enumerate(tiles):
        x, y = (index % 3) * 640, 98 + (index // 3) * 465
        image = Image.open(path).convert("RGB").resize((620, 413))
        canvas.paste(image, (x + 10, y))
        draw.text((x + 16, y + 416), title, fill=(25, 36, 44), font=font)
        draw.text((x + 16, y + 447), detail, fill=(75, 89, 98), font=small)
    canvas.save(out / "morphology_gallery.png")


def generate_dataset(
    output,
    seeds=(9301, 9302),
    cases=CASTING_CASES,
    casting_profile=BASE_PROFILE,
):
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    protocol = {
        "schema_version": 1,
        "profile": load_profile(casting_profile),
        "seeds": list(seeds),
        "cases": list(cases),
        "split": "development",
        "source_hashes": {
            **source_hashes(),
            "casting_synthesis.py": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        },
        "real_scans_used": False,
        "trained_model_used": False,
        "dynamics_executed": False,
        "normal_CAD_modified": False,
        "part_identity_rule": "all views, cases, crops and augmentations of a synthetic097_seed entity belong to the same data split",
        "public": "RGB, measured depth, nominal reference depth, intrinsics, transforms and integrated encoder",
        "evaluator_only": "semantic/instance masks, complete roots including hidden portions, depth consistency and generator parameters",
        "root_visibility_scope": "0.15 mm depth consistency on ideal rasterized images, not calibrated real-camera root observability",
    }
    write_json(out / "protocol.json", protocol)
    snapshot = out / "source_snapshot"
    snapshot.mkdir()
    for name in (*SOURCE_FILES, "casting_synthesis.py"):
        shutil.copyfile(workspace.SOURCE / name, snapshot / name)
    shutil.copyfile(CONFIG_FILE, snapshot / CONFIG_FILE.name)
    for profile_file in PROFILE_FILES.values():
        if profile_file.is_file() and profile_file != CONFIG_FILE:
            shutil.copyfile(profile_file, snapshot / profile_file.name)
    shutil.copyfile(CELL_CONFIG_FILE, snapshot / CELL_CONFIG_FILE.name)
    records = []
    for case in cases:
        for seed in seeds:
            folder = out / f"{case}_seed{seed}"
            sim = Workstation(
                folder, seed, case, casting_profile=casting_profile
            )
            try:
                inspection = LabelledInspection(sim, seed)
                _, paths, perception = inspection.scan()
                evaluator = folder / "evaluator"
                evaluator.mkdir(exist_ok=True)
                objects = []
                for identity, item in enumerate(sim.private_defects, 1):
                    macro = evaluator / f"object_{identity:02d}_macro.png"
                    macro_check = macro_preview(sim, item, macro)
                    visible = [
                        row["visible_object_pixels"][identity - 1]
                        for row in inspection.label_views
                    ]
                    item["macro_preview"] = str(macro.relative_to(folder))
                    item["macro_validation"] = macro_check
                    item["visible_pixels_by_view"] = visible
                    item["root_points_depth_consistent_some_view"] = int(
                        inspection.root_seen[
                            inspection.root_instances == identity
                        ].sum()
                    )
                    write_json(evaluator / f"object_{identity:02d}.json", item)
                    objects.append(
                        {
                            key: item[key]
                            for key in (
                                "geom",
                                "morphology",
                                "kind",
                                "region",
                                "height_peak_mm",
                                "geometry_validation",
                                "macro_preview",
                                "macro_validation",
                                "visible_pixels_by_view",
                                "root_points_depth_consistent_some_view",
                            )
                        }
                    )
                record = {
                    "part_id": f"synthetic097_seed{seed}",
                    "case": case,
                    "case_label": CASE_LABELS[case],
                    "appearance": casting_appearance(seed),
                    "seed": seed,
                    "casting_profile": casting_profile,
                    "synthetic_sensor_prior": sim.sensor_prior,
                    "sample_directory": folder.name,
                    "objects": objects,
                    "view_count": len(inspection.label_views),
                    "views": inspection.label_views,
                    "visible_objects": sum(
                        max(item["visible_pixels_by_view"]) >= 6
                        for item in objects
                    ),
                    "root_points": len(inspection.root_points),
                    "root_points_depth_consistent_some_view": int(
                        inspection.root_seen.sum()
                    ),
                    "legacy_detector_metrics": perception["metrics"],
                    "legacy_predicted_paths": len(paths),
                    "inference_truth_used": False,
                }
                write_json(folder / "sample.json", record)
                records.append(record)
                write_json(
                    out / "manifest.json",
                    {"protocol": protocol, "samples": records},
                )
                print(
                    json.dumps(
                        {
                            "case": case,
                            "seed": seed,
                            "objects": len(objects),
                            "visible": record["visible_objects"],
                            "views": record["view_count"],
                            "old_detector_f1": perception["metrics"]["f1"],
                            "predicted_paths": len(paths),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                sim.close()
    _gallery(out, records)
    lines = [
        "# 研究形态合成：当前 ESTUN 工作站",
        "",
        f"参数版本：`{casting_profile}`。完整参数及来源见 [protocol.json](protocol.json)。",
        "依据杨国翱的带状法向合成、实物照片的卷曲/破裂形态及 AF3AD 的局部参数化思想，自行实现 CAD 上有根部的闭合余料。",
        "未使用真实扫描拟合，没有训练模型，也没有运行动力学；真实模具分型线和加工许可仍需工艺确认。",
        "",
        f"CAD：097 一种；开发实体种子：{list(seeds)}；工况数：{len(records)}；相关分度帧：{sum(r['view_count'] for r in records)}。",
        "同一 seed 的所有工况、角度、局部块和增强数据应进入同一划分；此处没有独立测试集。",
        "",
        "![三维形态近景](morphology_gallery.png)",
        "",
        "| 工况 | seed | 合成对象 / 可见对象 | 根部采样 / 某视角深度一致 | 原几何检测 F1 | 原预测路径 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for record in records:
        f1 = record["legacy_detector_metrics"]["f1"]
        lines.append(
            f"| {record['case_label']} | {record['seed']} | "
            f"{len(record['objects'])} / {record['visible_objects']} | "
            f"{record['root_points']} / {record['root_points_depth_consistent_some_view']} | "
            f"{'—' if f1 is None else f'{f1 * 100:.2f}%'} | {record['legacy_predicted_paths']} |"
        )
    lines.extend(
        [
            "",
            "根部深度一致按理想深度图与投影点相差不超过 0.15 mm 统计；没有把遮挡的根部变成已观测点。标签文件保留完整合成根部，用于监督和评价。",
            "原检测 F1 沿用可见正余料评价口径；它与完整语义标签、三维根部误差及安全加工成功率分别计算，不能互相替代。",
            "",
            "每个 sample 的 public/ 保存未经语义着色的 RGB-D、K、工件/世界变换和编码器；evaluator/ 保存整数语义/实例标签、完整根部、可见性、几何参数及近景。",
            "团队先验版本还保存原曹代码从当前几何预测 ROI 生成的毫米点云；没有运行 SAM 模型。该点云用于候选轨迹接口，训练新识别模型应读取完整 RGB-D，避免继承原检测器的漏检。",
            "evaluator 的标注预览：绿色为满足深度一致的根部投影，紫色为其余投影；包括隐藏根部，是合成标签而不是模型预测。",
            "研究工况与同种子的正常对照使用相同灰色铸件材质，颜色及镜面参数与缺陷类别无关。正常 CAD 几何未修改，原孔口、筋、台阶及口沿保留；材质未由实测图像标定，尚未加入已标定的表面粗糙度、反光解码或砂带去除机理。",
            "",
            f"可复现入口：`MUJOCO_GL=egl daiyi-casting-synthesis --casting-profile {casting_profile} --output <新目录> --seeds {' '.join(map(str, seeds))} --cases {' '.join(cases)}`。",
        ]
    )
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seeds", nargs="+", type=int, default=[9301, 9302])
    parser.add_argument(
        "--casting-profile", choices=available_profiles(), default=BASE_PROFILE
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=CASTING_CASES,
        default=list(CASTING_CASES),
    )
    args = parser.parse_args()
    if any(not 0 <= seed <= 100000 for seed in args.seeds):
        parser.error("seed must be between 0 and 100000")
    generate_dataset(args.output, args.seeds, args.cases, args.casting_profile)


if __name__ == "__main__":
    main()
