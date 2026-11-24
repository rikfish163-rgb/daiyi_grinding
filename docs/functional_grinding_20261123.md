# 离线识别、磨削与复检功能样例

当前主站由 ESTUN iER220-2700 携带 097 工件到固定砂带，使用合成 RGB-D、
正常 CAD 参考、连续动力学积分和磨后重复量测完成加工闭环。
有限去除率主样例已依据磨后正超差完成局部回磨，随后通过区域复检和完整仿真任务。
薄批缝、团队先验批缝和毛刺还有独立完成例；正常件观测后保持零加工。
低位相机补采与共享退让已使新毛刺的外壁实际加工并通过区域复检。

## 有限去除率下的完整回磨

[完整加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_finite_03/index.html)
来自 [thin_120483_finite_03 原始结果](../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_03/result.json)，
[调用记录](../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_03.invocation.json)
保存实际参数与运行退出状态，
[导出摘要](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_finite_03/summary.json)
保留各轮候选与实际接纳段数。首次加工后确认正超差，控制器从许可的表面轨迹
选局部子段、去除重复覆盖，并按观测上界与名义去除率安排接触时长。
实际回磨后的注册区域全部验证，最终停在 `observed_regions_verified`。

当前代码已通过两项 Ruff 检查、完整 368 项测试及 mixed/normal 感知入口验证。
实际命令、退出码与输出见
[规定检查回执](../runs/enterprise_offline_20261123/functional_resume/verification_functional_01/required_checks.json)
和[完整验证日志](../runs/enterprise_offline_20261123/functional_resume/verification_functional_01/verify_after_rss.log)。

| 项目 | 实际结果 |
| --- | --- |
| 假定法向去除率 | 0.009 mm/s |
| 首次候选 / 实际接纳段 | 4 / 4 |
| 首轮确认正超差锚点 | 24 |
| 回磨映射 / 重复覆盖 / 去重后候选段 | 4 / 1 / 3 |
| 回磨实际接纳段 | 3 / 3 |
| 执行 / 复检轮数 | 2 / 2 |
| 最终验证区域 / 注册区域 | 2 / 2 |
| 最终区域 1 已观察 / 注册锚点 | 96 / 96 |
| 最终区域 2 已观察 / 注册锚点 | 49 / 49 |
| 最终观察锚点 / 注册锚点 | 145 / 145 |
| 最大磨后观测上界 | 0.077113 mm |
| 仿真运动 / 磨后观测 / 整件仿真任务 | 通过 / 放行 / 完成 |
| 企业实物验收 | 待独立现场量测 |

| 实际加工轮次 | 接纳 / 候选段 | 磨后深度帧数 | 区域 1 有符号区间 mm | 区域 2 有符号区间 mm | 验证区域 / 注册区域 | 表面跟踪 P95 mm | 当轮停因 |
| --- | ---: | ---: | --- | --- | ---: | ---: | --- |
| 首次加工 | 4 / 4 | 150 | −0.032703 至 +0.435841 | −0.034924 至 +0.349115 | 0 / 2 | 0.118536 | `followup_planning` |
| 局部回磨 | 3 / 3 | 54 | −0.076476 至 +0.077113 | −0.074245 至 +0.073929 | 2 / 2 | 0.339466 | `observed_regions_verified` |

各区域区间取当轮全部注册锚点量测的外包络，区间两端可来自不同锚点。

![有限去除率 finite_01 与 finite_03 的逐轮观测与实际执行段数](../runs/enterprise_offline_20261123/functional_resume/figures_finite_01_03_01/finite_observation_progress.png)

图中的上界来自原始逐轮注册区域报告，柱图只统计已经发生的执行。
回磨报告的 `path_count` 保留去重前的映射数；
`candidate_path_count` 记录去重后的候选，实际接纳数读取对应执行轮次。
绘图数据保存在 [plot_data.json](../runs/enterprise_offline_20261123/functional_resume/figures_finite_01_03_01/plot_data.json)，
[reproduce_plot.py](../runs/enterprise_offline_20261123/functional_resume/figures_finite_01_03_01/reproduce_plot.py)
从 `finite_01` 与 `finite_03` 的原始结果和逐视角采集记录重建图表。

### finite_02 保留对照

[finite_02 原始结果](../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_02/result.json)
和[加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_finite_02/index.html)
保留该完整加工记录。同锚点量测来自 `finite_02`，接触时间报告比较
`finite_01` 与 `finite_02`，引用与数值按各自原始运行保留。
`finite_02` 的同锚点量测与处置见
[量测对照摘要](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_02_comparison_01/summary.json)、
[逐锚点表](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_02_comparison_01/anchors.csv)
和[同锚点变化表](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_02_comparison_01/anchor_changes.csv)。

该对照的局部回磨机会来自其加工 `trace`，接触时间解释见
[finite_01/finite_02 原始接触时间报告](../runs/enterprise_offline_20261123/functional_resume/regrind_01/contact_time_report_01.md)。
报告分别统计加工段与总命令时长，保留同点几何去除的事后评价。

| 有限率运行 | 实际回磨段数 | 回磨 trace 命令合计 s | 回磨总命令时长 s |
| --- | ---: | ---: | ---: |
| 保留完成例 `finite_02` | 3 | 160.633651 | 306.948347 |
| 保留待复核例 `finite_01` | 4 | 6.332812 | 163.153190 |

## 低位相机下的外壁毛刺加工

[加工页](../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_02/index.html)
来自[低位毛刺原始结果](../runs/enterprise_offline_20261123/functional_resume/burr_2026112397_low_camera_02/result.json)，
[导出摘要](../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_02/summary.json)
保留实际执行与复检记录。固定相机使用 `low_side_simulation`，原夹持携件到固定砂带，
共享退让采用现有距离；初始获准路径均实际执行，磨后独立观测验证了外壁区域。

| 项目 | 实际结果 |
| --- | --- |
| 工况 / 种子 | `burr` / 2026112397 |
| 初始候选 / 接纳 / 实际加工段 | 2 / 2 / 2 |
| 执行 / 复检轮数 | 1 / 1 |
| 共享退让距离 | 60 mm |
| 磨后独立深度帧数 | 150 |
| 验证区域 / 注册区域 | 3 / 4 |
| 外壁关联区域观测上界 | 0.067546 mm |
| 未观测生成缺陷 | 0 |
| 表面跟踪 P95 | 0.085095 mm |
| 非预期接触 | 0 |
| 外壁生成余料峰值，事后评价 | 0.024926 mm |
| 仿真运动 / 磨后整件观测 | 通过 / 未放行 |
| 整件仿真任务 | `partial` |
| 最终停因 | `reinspection_requires_review` |

外壁的观测区间与生成余料统计分别保存在区域报告和 `final_stock`。
控制器停止依据仍来自当前观测和已有许可轨迹，真值未用于控制。
剩余顶面复核目标与回磨条件见[已知问题与限制](#已知问题与限制)。

## 其他完整加工样例

各例均满足 `motion_gate_passed`、`observational_release`、
`geometric_goal_complete` 和 `goal_complete`，任务状态为 `complete`，
控制器停在 `observed_regions_verified`。加工、复检和控制决定保存在原始运行目录。

| 样例与原始结果 | 初始候选 / 接纳段 | 最终验证区域 / 注册区域 | 执行 / 复检轮数 | 磨后深度帧数 | 最大观测上界 mm | 表面跟踪 P95 mm |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [薄批缝 120483](../runs/enterprise_offline_20261123/functional_resume/thin_120483_fixed_03/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_01/index.html) | 4 / 4 | 2 / 2 | 1 / 1 | 54 | 0.078114 | 0.118642 |
| [薄批缝 2026112391](../runs/enterprise_offline_20261123/functional_resume/thin_2026112391_01/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_2026112391_01/index.html) | 3 / 3 | 2 / 2 | 1 / 1 | 54 | 0.083553 | 0.107423 |
| [薄批缝 2026112393](../runs/enterprise_offline_20261123/functional_resume/thin_2026112393_01/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_2026112393_01/index.html) | 3 / 3 | 1 / 1 | 1 / 1 | 54 | 0.074314 | 0.116648 |
| [薄批缝 2026112307](../runs/enterprise_offline_20261123/functional_resume/thin_2026112307_regrind_01/result.json) | 4 / 4 | 1 / 1 | 1 / 1 | 54 | 0.075821 | 0.117497 |
| [团队先验批缝 9902，learned](../runs/enterprise_offline_20261123/functional_resume/seam_9902_regrind_01/result.json) | 4 / 4 | 1 / 1 | 1 / 1 | 54 | 0.065658 | 0.119619 |
| [团队先验毛刺 9903，geometric](../runs/enterprise_offline_20261123/functional_resume/burr_9903_geometric_01/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_success_burr_9903_01/index.html) | 3 / 3 | 4 / 4 | 1 / 1 | 54 | 0.078071 | 0.090518 |

加工页嵌入同视角前后图、各轮表面路径、实际接纳段数、量测帧数、区域区间和停因，
可直接打开。注册区域用于复检，数量与加工段、生成缺陷实例分别统计；
毛刺样例的注册区域包含初始 `review` 区域，磨后也须通过量测。

## 功能闭环与保存内容

固定相机按转盘编码器位姿采集多视角观测，检测器从 RGB-D 和正常 CAD 参考
生成候选及复核区域。获准加工的候选映射到名义 CAD 表面轨迹，再进行携件运动规划。
积分执行依据实际工件位姿记录跟踪与接触，并更新工具扫掠范围内的生成余料。

磨后返回观测位姿，重新采集独立深度帧。均值仅使用同一像素的有效采样，
跨视角有符号区间保留极值。深度与分割标签按像素中心采样，磨后网格同步到
各渲染上下文，复检读取当前几何。区间仍不确定或确认正超差时，采集保留已有帧后追加。

有符号下界确认持续正超差，且复检完整、无负向风险时，回磨从此前获准的
CAD 表面轨迹选择局部子段，去除重复覆盖，再重新规划完整运动。
设置有限去除率时，局部回磨进给还依据观测上界和名义率安排接触时长。
负向过磨、负边界不确定、非法观测或无法确认的余量停止自动磨削。

| 采集或执行项 | 当前值 | 含义 |
| --- | ---: | --- |
| 多视角扫描 | 6 视角 | 固定相机与编码器位姿融合 |
| 初始深度观测 | 1 帧 / 视角 | 用于初始识别与路径 |
| 常规磨后采集 | 9 帧 / 视角 | 独立帧的同像素有效均值 |
| 有界追加采集 | 最多 25 帧 / 视角 | 确认有符号区间，保留已有帧 |
| 复检容差 | ±0.30 mm | CAD 法向的逐锚点双边区间 |
| 量测区间 | ±2σ | 平均深度的有效噪声投影到 CAD 法向 |
| 正超差独立支持 | 至少 2 次 | 同视角重复有效采样支持正余量 |
| 执行预算 | 最多 3 轮 | 包括首次加工，由 `--max-passes` 选择 |
| 仿真跟踪门 | P95 小于 1.0 mm | 同时检查轨迹与工件表面跟踪 |

| 保存内容 | 用途 |
| --- | --- |
| `config.json`、`result.json` | 原始运行参数、各轮执行及完整任务结论 |
| `paths.json`、`admitted_paths.json` | 初始路径候选与获准路径 |
| `pass_XX_execution.json`、`pass_XX_execution.csv` | 积分执行、跟踪、接触与去除记录 |
| `registered_regions.json` | 加工前登记的 CAD 复检锚点 |
| `rescan_XX_view_YY_observation.npz` | 原始 `depth_frames`、有效采样数、均值和相机变换 |
| `rescan_XX_views.json` | 实际采集帧数、位姿和追加原因 |
| `reinspections[].regrind` | 持续正超差、局部回磨候选、未映射与复核原因 |
| `reinspections[].followup_preflight` | 下一轮实际获准的路径与运动 |
| `controller_stopping.reason` | 最终停止原因 |
| `index.html`、`summary.json` | 可直接查看的加工示例与分层结果摘要 |
| `summary.rounds[].candidate_path_count`、`summary.rounds[].admitted_path_count` | 每个实际执行轮次的候选与接纳段数 |

## 结果口径

| 层次 | 结果字段 | 读取含义 |
| --- | --- | --- |
| 仿真运动 | `motion_gate_passed` | 已执行各轮的实际积分跟踪与非预期接触满足运动条件 |
| 磨后观测 | `observational_release` | 当前观测范围与已注册区域满足有符号量测条件，并满足运动条件 |
| 事后几何与识别覆盖 | `geometric_goal_complete` | 生成余料和初始识别覆盖的事后评价 |
| 整件仿真任务 | `goal_complete`、`task_status` | 观测放行与事后评价共同满足完整仿真任务 |
| 企业实物验收 | 独立现场记录 | 使用真实工件、实测标定、工艺和连续运行记录确认 |

控制器及回磨的 `truth_used_for_control`、区域判定的 `truth_used_for_decision`
均为 `false`。生成余料和标签用于事后评价；下一次磨削依据当前观测、实际运动、
可用路径与执行预算决定。未观测的生成余料可以使完整仿真任务保持 `partial`，
即使已经登记的区域通过磨后量测。

## 正常件零加工

[正常件 9904](../runs/enterprise_offline_20261123/functional_resume/normal_9904_01/result.json)
完成初始观测后未生成加工候选，未执行磨削。任务状态是 `no_defect_detected`；
运动门未执行，磨后复检未发生，完整加工任务字段保持未完成。

| 项目 | 实际结果 |
| --- | ---: |
| 初始候选 / 接纳段 | 0 / 0 |
| 执行 / 磨后复检轮数 | 0 / 0 |
| 生成缺陷 / 检测信号 | 0 / 0 |
| 修改余料顶点 | 0 |
| 最终停因 | `no_admitted_path` |

## 可复跑命令

在 `/home/dianrobot/daiyi_grinding` 执行，输出目录使用尚不存在的新路径。
单件运行使用共享 MuJoCo 锁和单线程环境；统一运行器管理该锁时不再重复外套。

有限去除率与局部回磨主样例：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --workpiece-profile 097_thin_seam --seed 120483 --grasp-setup original \
  --camera-setup original --detector geometric --views multi --tool belt \
  --casting-profile research_casting_v1 \
  --mode removal --max-passes 3 --removal-rate-mm-s 0.009 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/finite_functional_new
```

薄批缝完整加工：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --workpiece-profile 097_thin_seam --seed 120483 --grasp-setup original \
  --detector geometric --views multi --tool belt \
  --mode removal --max-passes 3 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/thin_functional_new
```

其余薄批缝可使用成功表中的种子，并同步更换输出目录。

团队先验批缝与已冻结的 learned 权重：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  CUDA_VISIBLE_DEVICES="" \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --case seam --seed 9902 --casting-profile team_prior_20261121 \
  --grasp-setup original --detector learned \
  --checkpoint runs/daiyi_full_chain_20261122/same_input_42/source/runs/learned_grinding_20261121/training_01/model.pt \
  --views multi --tool belt --mode removal --max-passes 3 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/seam_functional_new
```

团队先验毛刺：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --case burr --seed 9903 --casting-profile team_prior_20261121 \
  --grasp-setup original --detector geometric --views multi --tool belt \
  --mode removal --max-passes 3 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/burr_functional_new
```

低位相机外壁毛刺：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --case burr --seed 2026112397 --casting-profile research_casting_v1 \
  --grasp-setup original --camera-setup low_side_simulation \
  --detector geometric --views multi --tool belt \
  --mode removal --max-passes 3 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/burr_low_side_functional_new
```

正常件观测与零加工：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock \
  .venv/bin/python -m daiyi_grinding.workstation_run \
  --case normal --seed 9904 --casting-profile research_casting_v1 \
  --grasp-setup original --detector geometric --views multi --tool belt \
  --mode removal --max-passes 3 --realtime-factor 0 \
  --output runs/enterprise_offline_20261123/functional_resume/normal_functional_new
```

从保存结果导出加工页：

```bash
.venv/bin/python -m daiyi_grinding.processing_example \
  runs/enterprise_offline_20261123/functional_resume/finite_functional_new \
  --output runs/enterprise_offline_20261123/functional_resume/example_functional_new
```

从保存的原始报告重新生成独立图表：

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python \
  runs/enterprise_offline_20261123/functional_resume/figures_finite_01_03_01/reproduce_plot.py \
  --output runs/enterprise_offline_20261123/functional_resume/figures_finite_replay_new
```

## 已知问题与限制

### 保留的部分完成记录

| 原始样例 | 首次候选 / 接纳段 | 实际执行段，按轮 | 最终验证区域 / 注册区域 | 磨后深度帧数，按轮 | 磨后观测 | 整件仿真任务 |
| --- | ---: | --- | ---: | --- | --- | --- |
| [重复量测薄批缝](../runs/enterprise_offline_20261123/functional_resume/thin_120483_fixed_02/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_failure_fixed_02/index.html) | 6 / 6 | 6 | 3 / 4 | 102 | 待复核 | `partial` |
| [有限率局部回磨](../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_01/result.json) | 4 / 4 | 4、4 | 0 / 2 | 150、150 | 待复核 | `partial` |
| [外壁未观测毛刺](../runs/enterprise_offline_20261123/functional_resume/burr_2026112397_01/result.json) | 2 / 2 | 2 | 2 / 2 | 54 | 已注册区域放行 | `partial` |
| [低位外壁补采毛刺](../runs/enterprise_offline_20261123/functional_resume/burr_2026112397_low_camera_01/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_01/index.html) | 2 / 1 | 1 | 1 / 4 | 150 | 回磨规划受阻 | `partial` |
| [共享退让后的低位毛刺](../runs/enterprise_offline_20261123/functional_resume/burr_2026112397_low_camera_02/result.json) · [加工页](../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_02/index.html) | 2 / 2 | 2 | 3 / 4 | 150 | 顶面复核目标未完成 | `partial` |

重复量测薄批缝的区域上界仍跨越容差，控制器停在 `reinspection_requires_review`，
回磨原因是 `uncertain_reinspection`。[原始基线](../runs/enterprise_offline_20261123/functional_resume/examples_baseline_01/index.html)
保留了运动通过、观测未放行的结果；该旧记录没有保存实际深度帧数字段。

有限率例从磨后观测确认持续正超差，实际重新规划并执行了局部路径。
第二轮同时存在正超差和上边界不确定，整体停在 `reinspection_requires_review`，
回磨原因是 `uncertain_reinspection`。已有正余量采样支持与当轮选入回磨的锚点
分别记录，不能用前者替代实际控制决定。

| 有限率例的磨后轮次 | 区域 1 上界 mm | 区域 2 上界 mm | 正超差区域 | 不确定区域 | 当轮停因 |
| --- | ---: | ---: | ---: | ---: | --- |
| 首次加工后 | 0.435841 | 0.349115 | 2 | 0 | `followup_planning` |
| 局部回磨后 | 0.362172 | 0.306061 | 1 | 1 | `reinspection_requires_review` |

同一注册锚点的跨轮对照见
[逐锚点量测表](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_01_comparison_01/anchors.csv)、
[同锚点变化表](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_01_comparison_01/anchor_changes.csv)
和[区域处置表](../runs/enterprise_offline_20261123/functional_resume/measurement_01/finite_01_comparison_01/regions.csv)。
区域上界峰值可能由不同锚点取得；同锚点变化读取配对表。
峰值差表示两轮量测上界的变化；同 CAD 点净去除应使用同点法向量测与实际接触时长。

原相机毛刺例的现有观测和已注册区域通过复检，事后评价仍发现未观测外壁余料。
低位补采例 `low_camera_01` 使用 `low_side_simulation` 固定相机，使外壁缺陷进入观测。
其实际加工后，登记的 `review` 区域与未完成毛刺仍有正超差；对应工件 CAD 代理
与砂带机结构干涉，后续路径未获准执行，停在 `followup_safety_rejected`。
具体受阻段保存在该运行的 `reinspections[].followup_preflight`。
共享退让后的 `low_camera_02` 已实际执行外壁段，外壁关联的加工区域及复核区域
均验证，顶面 `review` 目标仍为 `needs_rework`。

| 外壁毛刺例的观测、规划与事后评价项 | 实际值 |
| --- | ---: |
| 原相机例生成缺陷 | 3 |
| 原相机例未观测缺陷 | 1 |
| 原相机例生成余料最大剩余高度 | 2.552875 mm |
| 低位 `01` 未观测缺陷 | 0 |
| 低位 `01` 生成余料最大剩余高度 | 3.431232 mm |
| 低位 `01` `review` 区域 / 仍需加工 | 2 / 2 |
| 低位 `01` 后续规划候选 / 接纳段 | 2 / 0 |
| 低位 `01` 追加实际执行轮数 | 0 |
| 低位 `02` `review` 区域 / 仍需加工 | 2 / 1 |
| 低位 `02` 顶面复核区域观测上界 | 2.971267 mm |
| 低位 `02` 生成余料最大剩余高度 | 3.431232 mm |
| 低位 `02` 回磨确认锚点 / 候选段 | 0 / 0 |

低位 `01` 的[导出摘要](../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_01/summary.json)
保留实际执行与复检轮次；页面中的复跑命令保留相机布局。后续规划候选属于
被拒绝的下一轮，不计入已经发生的磨削。

低位 `02` 的回磨入口从已有许可轨迹未得到确认正余量的候选，报告原因是
`no_confirmed_positive_excess`。顶面复核区域的正超差与许可轨迹上的回磨候选
分别记录，不能把区域状态当成已经允许执行的路径。该目标仍需依据实际 RGB-D
与 CAD 支持建立新的许可路径，再通过完整携件准入与磨后复检。

### 断续目标补采与实体准入

断续飞边从原独立重复量测恢复局部名义 CAD 轨迹，并完成新模型中的目标 RGB-D
补采及整站规划。轨迹满足当前全件离线许可，但加工姿态的夹爪与砂带前部发生
实体干涉，停在 `motion_admission_blocked`。实际路径、量测和拒绝记录见
[实体准入报告](../runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_admission_01/report.md)
与[准入摘要](../runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_admission_01/summary.json)。

| 目标与采集项 | 实际结果 |
| --- | --- |
| 工件 / 工况 / 种子 | CAD 097 / `casting_interrupted` / 97101 |
| 原量测 CAD 代表点 XYZ（m） | (0.0860784203, 0.1459700600, 0.3609997475) |
| 原独立空间量测位置 | 1 |
| 原同像素独立深度帧 | 9 |
| 新加工前目标图像 | 3200 × 2400，转台 180° |
| 新同位姿独立深度帧 | 9 |
| 新观测原组 / 候选 CAD 支持覆盖 | 4 / 4、30 / 30 |
| 局部选取实测 CAD 支持 | 16 点 |
| 名义轨迹点数 / 长度 | 3 点 / 1.043804 mm |
| 当前足迹许可 | `whole_workpiece`，`review` 足迹拒绝为 0 |

| 量测依据 | 原独立重复量测 | 新加工前候选 CAD 支持 |
| --- | ---: | ---: |
| 正向余量估计 / 观测峰值 mm | 0.365334 | 0.352911 |
| 最大正向下界 mm | 0.306162 | 0.280571 |
| 最大观测上界 mm | — | 0.425251 |

原初始法向 P80 保留原值，原路径按该入口的判定返回为空。恢复局部轨迹的依据
是原未加工工况的独立正下界；新观测的峰值超出目标，当前最大正下界仍未越过目标。
同一像素关联多个 CAD 锚点只提供一处空间量测，两批重复采样分别保存。

| 规划与执行项 | 实际结果 |
| --- | --- |
| 原初始法向 P80 / 加工目标 | 0.274231 / 0.300000 mm |
| 候选 / 接纳轨迹 | 1 / 0 |
| 导出机器人命令 | 0 |
| 主臂磨削 / 材料去除 | 未执行 / 未发生 |
| 磨后观测 / 整件观测放行 | 无 / `false` |
| 姿态与接触组合尝试 | 360 |
| IK / 碰撞检查调用 | 360 / 225 |
| 搜索时间 / 段级预算 | 3.655614 / 12.000000 s，预算未耗尽 |
| 最终保存的实体拒绝 | `gripper_proxy_1` / `belt_front_proxy_93`，穿透 26.855 mm |
| 最终停因 / 运行状态 | `motion_admission_blocked` / `finished` |
| 合成真值用于控制 | `false` |

拒绝发生在加工目标的携件碰撞检查，尚未进入该段的接近与局部 trace 规划。
当前刚体夹持下，关节分支变化不改变给定工件目标所对应的夹爪位姿；后续需从
夹持或接触布局的具体调整继续准入。报告保存前五种去重原因和最终原因，
这些例子不能代表全部搜索组合。转台积分与加工前采集已完成，完整磨削和复检闭环
仍待实际获准的路径。

### 去除模型与实测标定

未指定 `--removal-rate-mm-s` 的完成例使用工具实际扫掠的几何包络裁剪余料，
未建立经实测标定的材料去除效率。设置有限率后使用 `constant` 等效率假定：
实际工具足迹内的接触积分时长限制法向去除，回磨接触预算使用同一名义率。
当前 `nominal = plant`，没有独立估计现场率偏差；`rate_calibrated` 为 `false`。
力、材料、砂带磨损、温升及表面粗糙度尚未形成实测工艺模型。
`planned_trace_seconds` 保存缩放前的名义时间，导出命令使用缩放后的 trace 时长。
夹取与转运计入总执行时间，去除接触预算按高点的实际工具足迹计算。

| 工艺或量测项 | 当前状态 |
| --- | --- |
| 有限率例的假定法向去除率 | 0.009 mm/s |
| 回磨名义时间到导出 trace 的全局缩放 | 1.5 |
| 力控与材料去除率 | 未经现场标定 |
| 有效噪声与 ±2σ 量测区间 | 仿真假定，不确定度模型未校准 |
| 工件 / 相机 / 装夹 / TCP | 使用 CAD 与虚拟工位参数，现场标定待补 |
| 几何去除统计 | 生成余料顶点统计，不等价于实测体积或表面质量 |

### 运行复现与企业实物验收

主样例 `finite_03` 尚未独立导出同锚点配对表和接触时间统计。
现有同锚点变化表及接触时间回放的保留对照对应 `finite_01`、`finite_02`；
主样例的逐轮观测与实际执行依据其独立保存的原始运行记录。

样例种子属于开发运行。原始结果对应各目录保存的输入与源码版本；
相同参数在当前实现重跑时，以新目录的 `result.json` 和停止原因为准，
历史部分完成结果保持原值。表面跟踪 P95、虚拟相机观测及仿真壁钟耗时
不能直接换算为企业定位精度、换型时间或连续生产能力。

| 企业指标 | 目标 | 当前证据 |
| --- | ---: | --- |
| 视觉定位 | ±0.1 mm | 待真实工件独立量测 |
| 型面误差 | ±0.3 mm | 待真实工件双边型面量测 |
| 换型时间 | ≤30 分钟 | 待现场换型流程计时 |
| 新品占机时间 | ≤2 小时 | 待现场新品导入计时 |
| 连续稳定运行 | ≥800 小时 | 待连续实机运行记录 |
