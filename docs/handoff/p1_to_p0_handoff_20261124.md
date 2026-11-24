# P1 → P0 交接：数据管线与工艺/复检适配

2026-11-24。本文给主控、P0-A（`workstation_session.py`）和 P0-B（`workpiece_packages.py` + `workstation_planning.py`）提供可直接落地的接口契约。P1 交付的新文件：

| 文件 | 作用 |
| --- | --- |
| `src/daiyi_grinding/dataset_pipeline.py` | 多母件特征提取、三类缺陷合成、结构光退化、局部/中间态样本包 |
| `src/daiyi_grinding/adaptive_process.py` | 工艺扰动 plant、在线去除率辨识、局部基准去偏置复检、P0-A 适配器 |
| `tests/test_dataset_pipeline.py` | 097/021 真实网格、退化模型、样本包与 P0-B 契约 |
| `tests/test_adaptive_process.py` | 0.4×–1.8× 收敛、隐藏偏置拦截、P0-A 协议 |

P1 没有修改任何既有源码。`truth_used_for_control`、`truth_used_for_decision` 在所有控制与判定记录中恒为 `False`；生成标签只作监督。

## 1. 与 P0-A 的对接

### 1.1 已落地的协议

`workstation_session.py` 定义：

```python
class ProcessAdapter(Protocol):
    def __call__(self, segment: dict, session_state: dict) -> dict | None: ...

class ReinspectionCalibrator(Protocol):
    def __call__(self, report: dict, session_state: dict) -> dict: ...
```

构造函数接受插件：

```python
session = create_session(
    ..., workpiece_id="097",
    process_adapter=process_adapter,
    reinspection_calibrator=calibrator,
)
session = load_session(
    session_dir,
    process_adapter=process_adapter,
    reinspection_calibrator=calibrator,
)
```

`load_session` 会重新注入插件，不重建 MuJoCo；插件状态不写入检查点。P1 的适配器实例可直接作为这两个参数：

```python
from daiyi_grinding.adaptive_process import (
    AdaptiveProcessController, LocalBaselineReinspector,
)

controller = AdaptiveProcessController()
calibrator = LocalBaselineReinspector()
process_adapter = controller.as_session_adapter()
reinspection_calibrator = calibrator.as_session_calibrator()
```

`as_session_adapter()` 和 `as_session_calibrator()` 返回的对象同时实现 `__call__`，控制器与复检器本身也可直接调用。

### 1.2 状态机节点的调用

P0-A 当前的真实接线（`workstation_session.py`）：

| 会话阶段 | 观测证据 | P0-A 内部调用 | 持久化 |
| --- | --- | --- | --- |
| `IDLE` / `SCANNED` | 无 | 仅注入插件 | `plugins.process_adapter` / `plugins.reinspection_calibrator` |
| `DETECTED` | `snapshot.detections`、`registered_regions` | 不调用；段尚未建立 | — |
| `PLANNED` → `EXECUTING` | `segment.path.normal_height_mm`、`material_allowance_mm`、`recipe`、`nominal_xyz` | `_execute_next` 在 `completed_passes == 0` 时先调 `_apply_process_adapter(segment)`，再调 `_prepare_segment` | `segments[i].params`、`process_state` |
| `EXECUTING` | 执行记录 | 执行后写 `execution_history`，含 `engaged_duration_s`、`commanded_force_n` | `execution_history[]` |
| `RESCANNED` | `reinspection` 报告；`local_baseline` | `_scan` 在已有注册区域时调 `_apply_calibration()`；`step_next` 用 `_calibration_stop_reason()` 收紧放行 | `calibration`、`local_baseline` |
| `DONE` / `PAUSED` / `STOPPED_SAFETY` | — | 不再调用 | 保留最后证据 |

`_observed_context()` 只提供白名单观测：`segments`、`registered_regions`、`reinspection`、`execution_feedback`、`local_baseline`、`process_state`、`calibration`，以及恒为 `False` 的 `truth_used_for_control` / `truth_used_for_decision`。`_apply_process_adapter` 接受 `PARAMETER_DEFAULTS` 四个键加 `process_state` / `reason` / 真值标记；未给 `process_state` 时调用 `adapter.last_evidence()`。`_apply_calibration` 只接受 JSON 字典，并把 `bias_veto` 或非 `pass` 状态转成 `reinspection_calibration_*` 停因，`observational_release` 保持 `False`。

适配器返回字典只含四个可应用键：`feed_scale`、`normal_offset_mm`、`passes`、`dwell_scale`，另带 `truth_used_for_control: False`。P0-A 只把前四个键写入 `segment["params"]`；真值标记不进入参数。

`normal_offset_mm` 受 P0-A 的 `_prepare_segment` 硬门约束：

```python
path["material_allowance_mm"] += segment["params"]["normal_offset_mm"]
if not 0 <= path["material_allowance_mm"] <= CELL_CONFIG["planning"]["target_remaining_height_mm"]:
    ...  # parameter_gate_passed=False, 段被拒绝
```

P1 适配器把单步 `normal_offset_mm` 限制在 ±0.20 mm，并保证 `material_allowance_mm + normal_offset_mm` 落在 ±0.30 mm 内。超出部分由 `offset_limited_mm` 报告，后续磨后观测的下一道次继续切除；绝不输出会触发该硬门的单步偏置。

### 1.3 需持久化的字段

```json
{
  "process_state": {
    "estimate": {
      "relative_rate": 0.94,
      "relative_sigma": 0.02,
      "lower_95": 0.90,
      "upper_95": 0.99,
      "updates": 3,
      "last_snr": 4.8,
      "bounded": false,
      "low_snr_freezes": 0,
      "truth_used_for_control": false
    },
    "last_decision": { "...": "EngagementPlan.record() without the truth field" },
    "identification_events": [
      {"region_id": 1, "matched_anchors": 96, "engaged_seconds": 1.8,
       "force_n": 60.0, "estimate": { "...": "..." }}
    ],
    "truth_used_for_control": false
  },
  "calibration": {
    "status": "positive_excess",
    "bias_mm": -0.22,
    "bias_sigma_mm": 0.003,
    "anchor_bounds_mm": [[0.31, 0.39]],
    "anchor_states": ["excess"],
    "bias_veto": true,
    "naive_status": "pass",
    "naive_false_release": true,
    "gate_unchanged": true,
    "truth_used_for_decision": false
  },
  "segments": [
    {
      "segment_id": 1,
      "params": {"feed_scale": 1.75, "normal_offset_mm": 0.20,
                 "passes": 1, "dwell_scale": 1.0}
    }
  ]
}
```

`load_session` 会递归检查 `truth_used_for_control` / `truth_used_for_decision` 必须为 `False`；P1 返回的记录满足该检查。`process_state` 与 `calibration` 都存在 `session_state.json` 中，重载后由 `status()` 原样恢复；插件实例不序列化，`load_session` 需重新注入同一适配器。

P0-A 传出的手工锚点使用 `point` 键；P1 校准器同时接受 `point` / `point_m` / `xyz_m`，并从 `local_baseline["machined"]` 读取加工锚点（其次才是报告顶层的 `machined`）。

### 1.4 人工介入协议

会话现有 `_log(action, target_id, before, after, reason)` 会写 `diff`。建议人工参数修改统一使用：

| 字段 | 要求 |
| --- | --- |
| `action` | `set_params`（参数）、`switch_stage`（粗/精磨）、`approve_regions`、`pause`、`abort` |
| `target_id` | 段编号或区域编号 |
| `before` / `after` | 四个参数键的完整字典，缺失键按当前值补齐 |
| `reason` | 操作者给出的原因，不能为空字符串以外的自由文本 |

优先级：适配器先给出默认覆盖，人工操作后写入的值覆盖适配器值；人工值只对当前段生效，下一段重新从观测规划。人工修改不重置 `RelativeRate` 估计，也不改变 `calibration` 的偏置证据；`switch_stage` 只改变后续段的 `stage` 标签和力/进给预算。

人工介入回流样本：

```python
from daiyi_grinding.dataset_pipeline import interventions_to_hard_examples

result = interventions_to_hard_examples(session.interventions())
# result["requests"][i]["requested_kind"]
#   over_removal_risk / under_removal_risk / thick_stock / allowance_offset / nominal_replay
# result["requests"][i]["requested_intermediate_state"]
#   thin_stock / rough_residual
```

映射规则：`feed_scale` 下调 → `over_removal_risk` + `thin_stock`；上调 → `under_removal_risk` + `rough_residual`；`passes` 增加 → `thick_stock` + `rough_residual`；仅 `normal_offset_mm` 改变 → `allowance_offset` + `thin_stock`。非参数动作也保留审计请求：`mask_false_positive` → `false_positive_review`、`skip_segment` → `skipped_segment_review`、`add_manual_region` → `manual_roi`、`split_segment` → `split_segment_review`、`switch_grasp` → `grasp_switch_review`。生成的请求交给 `synthesize_defect(..., seed=新种子)` 或 `intermediate_height(chart, state, rng)`，不会回写控制参数。

真实链路：`session.set_segment_params(...)`、`session.skip_segment(...)`、`session.mask_false_positive(...)`、`session.add_manual_region(...)` 会通过 `_log` 写入 `interventions.jsonl`；`dataset_pipeline.interventions_to_hard_examples(session.interventions())` 返回每条介入一个结构化请求。集成测试 `tests/test_p1_p0_handoff.py` 覆盖这四个动作及其映射。

### 1.5 何时不要调用

- `session_state` 没有 `reinspection` 且没有 `execution_history` 时，适配器仍会返回段级覆盖（只用段自身的 `normal_height_mm`），但不会更新去除率；这是正常的冷启动。
- 复检报告没有 `local_baseline` / `baseline_anchors` 时，校准器返回 `status="baseline_unavailable"`、`bias_veto=False`。P0-A 的 `_apply_calibration` 会把该状态写成 `reinspection_calibration_baseline_unavailable` 停因并保持 `observational_release=False`；这是不放行，不是放宽门限。需要正常放行时，P0-A 应在扫描结果中提供加工带两侧未加工的正常面锚点（`local_baseline`）。

## 2. 与 P0-B 的对接

### 2.1 `WorkpiecePackage` 输入契约

P0-B 已落地 `workpiece_packages.load_workpiece_package(workpiece_id)`，返回冻结数据类，关键字段：

| 字段 | 含义 |
| --- | --- |
| `workpiece_id` | `"097"` / `"021"` 等三位 CAD 身份 |
| `mesh_path` | 绝对 CAD 路径（STL/OBJ） |
| `protected_zones` | `tuple[ProtectedZone]`，每项含 `name` 和 `bounds_m`（`(2, 3)` 米制包围盒） |
| `declaration` | 原始声明 dict，`declaration["units"]` 为 `"m"` |
| `bounds_m`、`grasp_setups`、`fixture_id` | 供轨迹/夹持对齐；P1 不修改它们 |

`config/workpieces/wp_097.json`、`wp_021.json` 使用的键是 `cad_path`（相对仓库根目录）和 `protected_zones`。P1 的适配器同时接受：

```python
from daiyi_grinding import dataset_pipeline, workpiece_packages

package = dataset_pipeline.load_workpiece_package(
    workpiece_packages.load_workpiece_package("021")
)
# 或
package = dataset_pipeline.load_workpiece_package(
    "config/workpieces/wp_021.json"
)
```

兼容规则：

- 入参可以是 P0-B 数据类实例、JSON 路径或 dict。
- CAD 键接受 `mesh_path` 或 `cad_path`；相对路径先在声明文件所在目录解析，不存在则回退到仓库根目录。
- 保护区接受 `protected_zones` 或 `protection_regions`，`list` 或 `tuple`；`ProtectedZone` 对象会被归一化为 `{"name": ..., "bounds_m": [[...], ...]}`。
- `units` 必须是 `"m"` 或 `"mm"`；`build_workpiece_samples` 只接受米制。
- 归一化结果是 `WorkpiecePackageView`：`workpiece_id`、`package_id`、`mesh_path`、`units`、`cad_sha256`、`protection_regions`、`metadata`。

`build_workpiece_samples(package, output, split=..., seed=...)` 读取 `mesh_path`、`workpiece_id` 和保护区，并把 `protection_regions` 写入每个样本的 `meta.json.provenance`。它不读取 P0-B 的夹持/碰撞字段，也不产生机器人命令。

### 2.2 `plan_staged_passes` 与多段/多夹持轨迹

`AdaptiveProcessController.plan_staged_passes(measured_mm)` 返回 `StagingPlan`：

```python
plan = controller.plan_staged_passes(measured_mm)   # per region / segment
plan.status        # ready / already_within_target / overgrind_hold /
                   # overgrind_limited / insufficient_budget / insufficient_observation
plan.passes        # tuple[StagedPass]
# StagedPass: stage ("roughing"|"finishing"), target_residual_mm,
#             planned_cut_mm, force_n, note
```

与 P0-B 轨迹的衔接：

1. 每个区域的 `measured_mm` 是该区域注册锚点的有符号余量；多段/多夹持时按 `region_id` 分别调用，不要跨夹持合并余量。
2. `plan.passes` 的第一道是下一段的 `stage` 与 `planned_cut_mm`；P0-A/B 可以按 `grasp_setup` 分组，把同一抓取内所有区域的同阶段段排在一起。
3. 段的进给/余量覆盖使用 P0-A 的四个参数键，不需要改 `path` 结构：
   - `recipe.feed_mm_s *= params["feed_scale"]`
   - `material_allowance_mm += params["normal_offset_mm"]`
   - `params["passes"]` 控制同一段重复次数；`dwell_scale` 当前恒为 1。
   - `normal_offset_mm` 单步限幅 ±0.20 mm，且叠加 `material_allowance_mm` 后必须落在 ±0.30 mm 内；0.8 mm 级厚余量的 0.20 mm 单步偏置对应 `offset_limited_mm≈0.50 mm`，由后续磨后观测的下一道次继续切除，不靠单步越界偏置。
4. 多道次预算：`ProcessLimits.max_roughing_passes`（默认 7）与 `max_finishing_passes`（默认 3）；超出时 `plan.status` 为 `insufficient_budget`，应复核而不是静默减少道次。
5. `overgrind_limited` 表示均匀道次会在最低锚点越下限；计划只安排安全切除量，剩余高点需要人工/非均匀策略，不能把状态当作已放行。
6. 所有 `StagedPass.record()` 带 `truth_used_for_control: False`；计划不含 plant 真值。

`workstation_planning.build_plan` 仍负责单段几何、碰撞与连续性；P1 不替代它。P0-B 只需把 `stage`、`planned_cut_mm` 和四个参数写入段元数据，轨迹点仍由既有规划器从 `path["nominal_xyz"]` 生成。

段元数据映射使用 `adaptive_process.segment_process_context(segment, plan)`：

```python
context = segment_process_context(segment, plan)
# {"region_id": 3, "grasp_setup": "original", "split_from": 7,
#  "workpiece_id": "097", "stage": "roughing", "planned_cut_mm": 0.45,
#  "target_residual_mm": ..., "planned_passes": 7, "plan_status": "ready",
#  "plan_reason": ..., "truth_used_for_control": False}
```

`region_id`、`grasp_setup`、`split_from`、`workpiece_id` 原样保留（缺失的 `split_from` 不凭空生成）；多夹持时按 `grasp_setup` 分组，按 `region_id` 分别调用 `plan_staged_passes`。`tests/test_p1_p0_handoff.py` 同时用真实 `split_segment` 产生的子段验证 `split_from` 保留。

### 2.3 数据集样本包

每个样本目录：

```text
samples/<sample_id>/
  arrays.npz      depth_m, depth_ideal_m, depth_nominal_m, depth_sigma_mm, incidence_deg,
                  semantic(uint8), instance(uint16), residual_mm,
                  protection, surface, dropout, specular, oxide, flying
  points.npz      points_m, normals, semantic, instance, residual_mm,
                  root_distance_mm, root_tangent, incidence_deg, sigma_mm,
                  protection, spacing_mm, local_area_mm2, count
  roots.npz       root_points_m, root_tangent, root_normal, root_offsets,
                  root_instance, boundary_points_m
  rgb.png  semantic.png  protection.png  meta.json
```

`depth_ideal_m` 始终是**当前真实状态**在同一相机下的无噪深度（含缺陷或中间态）；`depth_nominal_m` 是同一相机下的名义 CAD/零余量参考深度。两者不混用：训练与复检读取 `depth_ideal_m` 作为当前状态的理想观测，读取 `depth_nominal_m` 计算有符号余量。

语义编号：`0 background`、`1 flash`、`2 burr`、`3 nominal_cad`、`4 review_excess`、`5 casting_root`、`6 intermediate_residual`。局部点云目标间距 0.35 mm，样本记录 `spacing_mm`；没有使用粗体素下采样。样本 `meta.json` 含相机 `K`/`T`、编码器角、弱对齐误差、退化参数、漂移、CAD SHA-256 和 `provenance`（`mujoco_used=False`、`real_scans_used=False`、`labels_are_supervision_only=True`）。

`rgb.png` 只由几何与退化观测生成：统一铸金属灰基色、法向 Lambertian 漫反射、宽高光波瓣以及实测 `specular` / `oxide` / `dropout` 掩码；`semantic` 参数仅为调用兼容保留，绝不参与成像。同一观测换任意语义标签，RGB 逐像素不变。

`roots.npz` 的 `root_instance` 与同一样本 `arrays["instance"]`、`points["instance"]` 使用完全相同的编号：场景样本覆盖该帧可见的全部缺陷实例，中间态样本只含本缺陷实例。密集点云是可见实例集合的局部子集，按 45 mm 局部球采样；实例编号不重新编号。中间态样本的 `arrays["protection"]` 与 `points["protection"]` 标出残留加工带两侧 2–3 mm 内的名义本体，二者物理一致且非全零。

unseen-CAD 划分按 `workpiece_id` 整体分配：`unseen_cad_split(ids)` 用于 ≥2 个 CAD；`assign_splits(ids)` 对单个 CAD 返回 `train` 且 `unseen_cad_evaluation_possible=False`。同一工件的所有视角、风格、局部裁剪和中间态只进入一个划分，`assert_samples_respect_split` 会拒绝泄漏。

## 3. 可复跑命令与实测

在 `/home/dianrobot/daiyi_grinding` 运行；两个入口都不使用 MuJoCo。

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.dataset_pipeline \
  --output dataset_097_021_new --workpieces 097 021 --seeds 20261124

OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.adaptive_process \
  --output process_stress_new --seeds 0
```

正式产物：`runs/enterprise_offline_20261123/p1_data_process_01/dataset_097_021_05/` 与 `process_stress_06/`。`dataset_097_021_01`–`04`、`process_stress_01`–`05` 保留为早期复跑记录。

### 3.1 数据管线（097/021）

| 工件 | 分型候选 | 锐棱 | 孔口边缘 | 浇口附着区 | 合成缺陷 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 097 | 7 | 26 | 2 | 2 | 3 |
| 021 | 7 | 16 | 2 | 2 | 3 |

| 工件 | 类别 | 形态 | 峰值 mm | 体积 mm³ | 根部最大 CAD 距离 mm | 闭合 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| 097 | flash | thin_curled_sheet | 2.832 | 62.79 | 0.00 | 是 |
| 097 | burr | asymmetric_lobed | 0.916 | 45.05 | 0.00 | 是 |
| 097 | casting_root | gate_stub | 3.887 | 132.54 | 0.00 | 是 |
| 021 | flash | interrupted | 2.087 | 20.63 | 0.00 | 是 |
| 021 | burr | asymmetric_lobed | 2.651 | 73.96 | 0.00 | 是 |
| 021 | casting_root | gate_stub | 4.374 | 236.61 | 0.00 | 是 |

共 30 个样本（每件 3 缺陷 ×（overview + local_fov + 3 中间态））。划分 `train=["021"]`、`test=["097"]`、`val=[]`（两个工件时的开发划分；`unseen_cad_evaluation_possible=true`）。

退化参数：基础轴向 σ 0.05 mm、距离项 0.35 mm/m、入射角增益 0.9 mm、飞点比例 0.25、高反光斑 120 个/m²、氧化斑 60 个/m²、配准漂移 ±0.15 mm / ±0.6 mrad、法向偏置 0.03 mm。

### 3.2 工艺扰动与复检

| 工况 | 真实倍率（审计） | 最终估计 | 95% 区间 | 误差 | 道次 | 停因 | 放行 |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- |
| rate_040 | 0.40 | 0.323 | 0.286–0.361 | −0.077 | 4 | pass | 是 |
| rate_070 | 0.70 | 0.640 | 0.611–0.670 | −0.060 | 3 | pass | 是 |
| rate_100 | 1.00 | 0.942 | 0.898–0.986 | −0.058 | 3 | pass | 是 |
| rate_130 | 1.30 | 1.247 | 1.188–1.306 | −0.053 | 3 | pass | 是 |
| rate_180 | 1.80 | 1.753 | 1.673–1.834 | −0.047 | 3 | pass | 是 |
| wear_severe | 1.00 | 0.274 | 0.257–0.291 | −0.726 | 5 | thermal_guard | 否 |
| uneven_stock | 1.00 | 0.970 | 0.906–1.034 | −0.030 | 3 | pass | 是 |
| hidden_bias_negative | 1.00 | 0.952 | 0.843–1.061 | −0.048 | 7 | pass | 是 |
| hidden_bias_positive | 1.00 | 0.937 | 0.898–0.976 | −0.063 | 3 | pass | 是 |

8/9 运行收敛到 ±0.15 倍率，平均 3.78 道次，误放行 0。`wear_severe` 是刻意设置的强磨损 + 温升工况：辨识量收敛到磨损后的有效倍率（约 0.27），并在温升门处停止，保留正余量而不放行；它不计入倍率收敛。

| 隐藏偏置工况 | 真实余量 mm | 注入偏置 mm | 朴素判定 | 校准判定 | 期望 | 校准正确 | naive 误判 |
| --- | ---: | ---: | --- | --- | --- | --- | --- |
| hidden_negative_false_release | +0.36 | −0.18 | pass | positive_excess | positive_excess | 是 | 是 |
| hidden_positive_false_release | −0.36 | +0.18 | pass | overgrind | overgrind | 是 | 是 |
| hidden_positive_false_reject | +0.10 | +0.25 | uncertain | pass | pass | 是 | 是 |
| hidden_negative_false_reject | −0.13 | −0.22 | overgrind | pass | pass | 是 | 是 |

四个构造场景全部正确：前两例 `bias_veto=True`（朴素门限会误放行），后两例 `bias_veto=False`（校准把误拒正确放行）。两条误放行场景的校准判定都不是 `pass`，全套 `false_release_count=0`。

### 3.3 验收边界

| 项目 | 门槛 | 实测 |
| --- | --- | --- |
| 倍率扰动 | 0.4 / 0.7 / 1.0 / 1.3 / 1.8 | 5 组全部覆盖 |
| 相对误差 | ≤20%，≤10 个有效观测对 | 2.6%–19.3%，3–4 道次 |
| 伪更新 | 无接触、零时间、重复观测对均为 0 | `invalid_updates_ignored` / `duplicate_updates_ignored` 单独计数，`updates` 不变 |
| 真值读取 | 控制器/判定记录 `truth_used_for_control` 与 `truth_used_for_decision` 恒 `False` | 全部记录断言 |
| 隐藏偏置 | +0.36 / −0.18 与 −0.36 / +0.18 | 朴素均 pass，校准分别 positive_excess / overgrind |
| 参考点缺失/重叠/不一致 | 不得放行 | `baseline_insufficient` / `baseline_overlaps_target` / `baseline_inconsistent` → `hold` |

### 3.4 跨模块交接集成测试

`tests/test_p1_p0_handoff.py` 使用纯 Python `StubBackend`（不构造、不积分 MuJoCo，不触碰 `mujoco.lock`），真实注入 P1 适配器并跑通：

- `create_session` → `step_next`（scan）→ `approve_regions` → `step_next`（plan）→ `step_next`（adapter + execute）→ `step_next`（rescan + calibration）→ `step_next`（DONE）→ `load_session` 重载；
- `normal_offset_mm=0.20`（叠加 0.08 余量 = 0.28）通过 P0-A 的 `0 <= allowance + offset <= 0.3` 硬门，`parameter_gate_passed=True`；
- `process_state`（估计、最后决策、识别事件）与 `calibration` 写入检查点，重载后逐字段一致；
- `bias_veto=True` → `reinspection_calibration_bias_veto`；`hold` → `reinspection_calibration_hold`；`baseline_unavailable` → `reinspection_calibration_baseline_unavailable`；三种情况 `observational_release=False`；
- 在段/状态/报告中注入 `truth_used_for_control=True`、`truth_residual_mm`、`plant` 等毒化字段，适配器与校准器输出逐字段不变，真值标记仍为 `False`；
- 真实 `set_segment_params` / `skip_segment` / `mask_false_positive` / `add_manual_region` 写入 `interventions.jsonl`，映射为 4 条结构化难例请求；
- `workpiece_packages.load_workpiece_package("097"/"021")` 映射到 P1 后 CAD 路径、单位、保护区名称与边界、坐标系/夹具字段一致；`plan_staged_passes` + `segment_process_context` 保留 `region_id` / `grasp_setup` / `split_from`。

```bash
.venv/bin/python -m unittest tests/test_p1_p0_handoff.py -v
```

## 4. 不变量与限制

- 在线辨识只使用 `AnchorObservation`；前后差使常量测量偏置抵消。`SessionProcessAdapter.absorb_session_state` 只读取 `reinspection.regions[].anchor_estimates_mm` 与执行时长/力，不读取生成余料。同一观测对（高度、σ、角色、时长、力完全一致）只消费一次，重复调用只增加 `duplicate_updates_ignored`；零时长或零力只增加 `invalid_updates_ignored`。
- 规划使用去除率**上界**安排接触时长：实际率偏快时只会少去除，不会过磨。
- 低信噪比冻结估计（`low_snr_freezes`），不放大增益；非正去除（`observed ≤ 0`，含负去除/漂移噪声）单独计入 `nonpositive_removal_freezes` 并冻结，绝不把倍率裁到下界。已确认过磨或最低锚点越下限时停止。
- 局部基准要求 ≥12 个基线锚点，且两侧各 ≥3；缺基线时 `hold` 且 `bias_veto=False`。`require_both_sides=True` 而 `sides` 缺失时也 `hold`/`baseline_sides_missing`，不得跳过检查。基线离散过大或偏置超过 0.6 mm 时 `hold` 且 `bias_veto=True`。若基线锚点与加工锚点的 `points_m` 距离小于 1 mm（用目标区自身做参考），直接 `hold`/`baseline_overlaps_target`，不得以自参考消偏。校准器从 list/dict 输入保留 `point_m` / `xyz_m` / `points_m`，确保该重合检查在会话路径上生效。参考点缺失、重叠、缺少两侧标记或不一致一律不放行。
- 校准器与控制器都拒绝把 `tolerance_mm` 设到 0.3 mm 以上；`gate_unchanged: True`。
- plant、磨损、温升、柔顺、隐藏偏置均为合成代理；实机标定需要同件、同夹持、同点的磨前/磨后配对量测及砂带磨损、法向力、温度记录。
- 097/021 的分型线是 CAD 拔模轮廓/二面角候选，不是已确认的模具分型线；浇口附着区是按可达性和平坦度打分的候选窗口。
- 传感器退化参数是物理代理；`SensorDegradationProfile.calibrate_from_residuals` 在取得真实扫描残差后按角度×距离二维分箱拟合，当前结果标记为 `calibrated_from_real_scans=False`。
