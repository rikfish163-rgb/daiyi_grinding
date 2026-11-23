# 097 磨后 signed 复检压力回归

日期：2026-11-23。本回归只使用合成 sensor 数组，不加载 MuJoCo、不控制设备，也不读取生成缺陷真值来放行。

## 改动

- `Reinspection` 保留原构造、`observe`、`supplementary_angle` 和 `report` 调用面。
- 残差改为 signed 判定：正值为名义面以上余量，负值为过磨；公差仍为 ±0.30 mm。
- 每个区域同时保存逐锚点 `±2σ` 下界、上界、覆盖率和最终判定。缺测、NaN 非法输入、背面/斜角遮挡、参考错配和 ROI 受限均继续 hold。
- 补充视角的选择同时考虑上界和下界已测锚点；漏测锚点不会被邻点推断放行。
- `report` 明确记录不确定度来源为输入声明的 `noise_sigma_mm`，倍数为 2，且 `calibrated=false`。
- 法向量统一归一化，避免缩短法向量导致余量被低估。刚体相机、内参、布尔掩码和噪声声明均校验；非有限、非正或布尔公差直接拒绝。
- 已有完整覆盖后追加非法视角，整件仍保持 hold。逐区区分 `positive_excess`、`overgrind` 与 `negative_limit_risk`，整件报告保存自动回磨是否允许。

## 压力用例

| 类别 | 案例 | 预期 |
| --- | --- | --- |
| 普通 | `nominal_signed` | verified |
| 正向近线 | `positive_near_line_uncertain` | uncertain |
| 正向超差 | `positive_excess` | needs_rework |
| 负向过磨 | `negative_overgrind` | needs_rework |
| 负向近线 | `negative_near_line_uncertain` | uncertain |
| 噪声 | `noisy_near_line_uncertain` | uncertain |
| 缺点/NaN | `dropout_and_nan`, `invalid_nan_noise` | insufficient |
| 遮挡/错配/ROI | `backface_oblique_occlusion`, `registered_view_mismatch`, `limited_roi` | insufficient |
| 补扫覆盖 | `supplementary_coverage` | verified |

低资源 suite 共 12 例。实际结果为 2 例通过、10 例保守拒绝，其中负向过磨 1 例、漏测/非法/遮挡/错配/ROI 5 例、近线不确定 3 例、正向超差 1 例。未出现与预期不一致的案例。

## 输出

[sensor_only_003](../runs/enterprise_offline_20261123/reinspection/sensor_only_003/report.md)
保存子任务交付时的 12 例证据。其后增加了主流程停止与输入保护规则；最终集成证据需在全部源码冻结后写入新的统一运行目录，历史目录不覆盖。

入口：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.reinspection_stress \
  --output runs/enterprise_offline_20261123/reinspection/<新目录>
```

每个案例保存 `input.npz`、`case.json`；根目录保存 `summary.json`、`case_results.json`、`report.md` 和 `evidence.json`。`evidence.json` 包含任务、范围、命令、源码与输入哈希、指标、预期拒绝、异常 suite 失败、产物哈希和真实数据待办。公差没有放宽，保留区域与整件六视角范围检查没有删除。若存在未预期失败，CLI 返回非零退出码；`evidence.json` 自身不参与自身哈希。

## 验证

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest tests.test_workstation_quality -v

MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest \
  tests.test_reinspection_stress tests.test_workstation_stopping -v

.venv/bin/python -m ruff check \
  src/daiyi_grinding/workstation_quality.py \
  src/daiyi_grinding/workstation_run.py \
  src/daiyi_grinding/reinspection_stress.py \
  tests/test_reinspection_stress.py \
  tests/test_workstation_stopping.py
```

相关 22 项测试通过：原质量测试 9 项、压力与输入保护 10 项、主流程停止 3 项。覆盖 signed 过磨、法向量缩放、非法公差、覆盖完成后的非法视角、NaN 噪声、遮挡/错配/ROI、补扫覆盖、无效目标数组与完整 evidence 契约。

## 主链集成

`workstation_inspection.Inspection` 保持调用面：首检继续通过 `surface_anchors` 登记目标，磨后构造 `Reinspection(targets)`，每个视角调用 `observe(observation, view_index)`，`scan()` 调用 `report(0.3, whole_workpiece_scan=...)`。signed 下界与逐锚点边界进入复检回执。

`workstation_run.continuation_reason()` 根据观测、运动检查与剩余预算选择后续动作。发现过磨时停止为 `overgrind_requires_disposition`；负边界不确定时为 `negative_limit_requires_review`；非法复检时为 `invalid_reinspection_requires_review`。即使其他区域仍有正余量路径、还有回磨预算，这三类情况也会停止自动回磨，等待处置。纯正余量回磨和预算停止保留原行为；评价真值不参与继续或停止决策。

## 限制

- 合成数组只代表同一相机/参考面/锚点模型下的决策逻辑，不代表真实相机精度。
- 未取得同件磨前磨后独立实测、真实过磨样本、标定后的不确定度和真实 097 磨后扫描。
- 补扫覆盖用例不验证转台运动、机器人路径或 MuJoCo 渲染；这些仍由主链和共享锁约束的既有流程验证。
