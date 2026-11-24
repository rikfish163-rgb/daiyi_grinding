# 当前 ESTUN 工位与本机工程交接

当前薄批缝主链已完成有限去除率下的实际局部回磨和整件观测放行：
[回磨加工示例](runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_finite_03/index.html)
及其[原始运行结果](runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_03/result.json)
保存首次加工、重复量测确认、局部回磨和最终复检。
首次加工后确认正超差，局部回磨去除重复覆盖并安排足够接触时长；
最终注册区域及锚点均放行，仿真运动通过，完整仿真任务完成。
控制器停在 `observed_regions_verified`，决策的 `truth_used_for_control` 为 `false`。

| 阶段 | 实际结果 |
| --- | ---: |
| 首次候选 / 执行段 | 4 / 4 |
| 回磨映射 / 去重后候选 / 执行段 | 4 / 3 / 3 |
| 首轮 / 回磨后独立深度帧 | 150 / 54 |
| 最终验证区域 / 注册区域 | 2 / 2 |
| 最终观察锚点 / 注册锚点 | 145 / 145 |
| 首轮 / 最终最大观测上界 mm | 0.435841 / 0.077113 |

当前实现已通过两项 Ruff 检查及完整 `scripts/verify.sh`：
368 项测试通过，mixed 入口为 14 条路径，normal 入口为 0 条路径。
[规定验证记录](runs/enterprise_offline_20261123/functional_resume/verification_functional_01/required_checks.json)
与[完整验证日志](runs/enterprise_offline_20261123/functional_resume/verification_functional_01/verify_after_rss.log)
保存实际命令、退出码和结果。

当前单件闭环入口是 `.venv/bin/python -m daiyi_grinding.workstation_run`。
[离线加工与结果示例](README.md#离线加工与结果示例)提供带共享锁的完整命令、
新输出目录与结果字段口径。接续加工时读取该运行的 `result.json`、
`reinspections[].stopping_reason` 和 `reinspections[].regrind`，
按当轮观测中的未放行原因推进量测、局部路径或对应运动。

`workstation_inspection` 采集固定编码器位姿的独立深度帧，只对同一像素取有效均值。
临时复检仍有不确定或正超差且没有过磨、非法输入时，保留已有帧再追加。
跨视角有符号区间保留极值，平均深度的有效噪声投影到各 CAD 法向并构成 ±2σ 区间；
不确定度模型保持未校准。

`workstation_model.render` 的深度和分割标签使用像素中心射线，
与相机标定及反投影的采样位置一致。`GeometricRemoval.upload` 将磨后余料网格
同步到所有已建立的渲染 GL 上下文，使复检读取当前几何。

| 采集或执行阶段 | 当前边界 |
| --- | --- |
| 初始观测 | 每视角 1 帧 |
| 常规磨后量测 | 每视角 9 帧 |
| 有符号区间确认 | 每视角有界追加至 25 帧 |
| 总加工预算 | `--max-passes` 选择 1、2 或 3 轮，包括首次执行 |

`rescan_XX_view_YY_observation.npz` 保存原始 `depth_frames`、
`depth_sample_counts`、平均深度、正常 CAD 参考与相机变换；
`rescan_XX_views.json` 记录实际帧数和追加原因。持续正超差的有符号下界超过
上限且有独立采样支持时，`workstation_regrind` 在此前许可的 nominal CAD 轨迹上
选局部子段，保留工具、ROI 与复核约束，再进入当轮完整运动规划。
过磨、负边界风险和非法输入停止自动回磨；缺测、不确定及未映射高点保留待处置记录。
设置 `--removal-rate-mm-s` 时，仿真去除量按实际工具足迹内的接触时间累计。
局部回磨按当轮观测上界、原许可余量和名义率降低短段进给，并去除同轮重复轨迹；
实际导出命令仍应用既有时间缩放。转运与未接触时长不能作为去除预算。
该示例的名义率与 plant 率相同，尚未完成独立材料与工艺标定。

`--camera-setup original` 保留原固定布局；
`--camera-setup low_side_simulation` 选择独立低位固定相机。
解析后的配置用于相机、外壳和标定变换，并记录到 `config.json`、`result.json`
及 live 状态。默认配置文件保持原值。功能样例与限制见
[功能加工说明](docs/functional_grinding_20261123.md)。

[低位毛刺加工页](runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_02/index.html)
保留补见外壁后的实际加工与复检。单姿态和分段规划共用法向退让，
外壁段已完成整段运动及观测放行；剩余顶面 `review` 仍有正超差。
[断续目标准入报告](runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_admission_01/report.md)
保留局部 CAD 轨迹及夹爪与砂带结构的实体拒绝。
断续目标的新加工前量测与历史独立正下界分别记录，本次没有可执行磨削命令，
后续从夹持或接触布局的具体改动继续准入。

| 扩展工况 | 当前结果 |
| --- | --- |
| 低位毛刺 | 实际 2/2 段、零非预期接触；磨后 150 帧；区域 3/4 放行，整件 `partial` |
| 顶面 `review` | 观测上界 2.971267 mm；许可路径上的回磨候选为 0 |
| 共用法向退让 | 60 mm；端点、完整进出及连续曲线均按原条件检查 |

运行结束后，用已保存结果导出可直接打开的加工示例：

```bash
.venv/bin/python -m daiyi_grinding.processing_example \
  runs/enterprise_offline_20261123/functional_resume/thin_example_new \
  --output runs/enterprise_offline_20261123/functional_resume/example_new
```

`index.html` 嵌入同视角前后图、各轮路径、运动、量测与回磨处置，
`summary.json` 保存分层结果。`motion_gate_passed` 表示仿真运动通过；
`observational_release` 表示当轮整件观测放行；`geometric_goal_complete`
是生成余料与识别覆盖的事后评价；两项质量条件共同决定 `goal_complete`。
合成真值用于事后评价，控制器依据观测、运动门、路径和预算继续或停止。
企业实物验收使用独立现场记录。
已保存的[重复量测失败比较页](runs/enterprise_offline_20261123/functional_resume/examples_failure_fixed_02/index.html)
来自 `thin_120483_fixed_02`，其磨后区域区间仍跨越上限，任务保留待复核状态。

2026-11-23 新增[企业对标离线经验库](docs/enterprise_offline_20261123.md)
和[统一运行器说明](docs/enterprise_offline_20261123.md#复现与证据读取)。
运行入口为 `daiyi-enterprise-offline`，八类套件分别验证场景、标定、识别、
规划、工艺、复检、可靠性及验收判据；`--include-main-chain` 追加当前
ESTUN 的薄批缝执行和正常无动作对照。新运行目录不得覆盖历史证据。

本轮主流程保护包括连续曲线途中碰撞检查、预检采样预算耗尽整条命令拒绝、
装夹/坐标/场景/运动源码变更后的计划失效，以及 signed 复检的负向过磨、
负边界不确定和非法输入停止。控制器使用独立执行与质量绑定校验；
历史结果只能作对应的历史证据。

所有 MuJoCo 创建模型、渲染和积分共同使用
`runs/enterprise_offline_20261123/mujoco.lock`。
统一运行器会管理锁，自持锁的模块不再重复外套。
单独执行仓库验证时使用：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock bash scripts/verify.sh
```

Git 当前没有初始提交，改动以本轮开始的
`runs/enterprise_offline_20261123/baseline_source_hashes.json`
和暂存文件为对照；继续工作时保留该快照、原 `results/`、历史运行和冻结权重。
企业愿景指标尚待双方确认实物验收规程。曹公开合成数据和 CPU 后处理已复用，
杨的同件原始点云、真实相机/装夹/TCP、力控去除率和表面质量仍待补。
软件故障注入循环不代表 800 小时连续实机运行。

2026-11-21 阶段进度为[实际训练模型与自主去除](docs/learned_grinding_20261121.md)。
`config/learned_detector.json` 登记已冻结的 387,683 参数 RGB-D U-Net，
网页学习模式与 `--detector learned --checkpoint ...` 已接入。
冻结原始分割 F1 94.01%，同输入可测候选 F1 91.91%。
`runs/learned_grinding_20261121/burr_9101_closed_loop_01` 已执行 4/4 段，
三处几何峰值均低于 0.3 mm，运动验收通过；初始类别匹配 1/3，
因此完整任务仍是识别待复核。团队参数混合工况结果和未通过原因见新报告。
以下 11/19 和团队先验章节保留各自历史口径。

2026-11-19 的默认任务为公司 CAD 的 ESTUN iER220-2700 机械臂持
097 工件到固定砂带加工。界面默认不再进入 FR5 历史演示。
当前闭环：固定相机六角扫描 → 缺陷识别/局部路径实时发布 → 携件逐段规划 →
同曲线预检/连续积分 → 余料几何扫掠 → 回位复检/有限轮补磨。

阅读顺序：`INDEX_LOCAL.md`、
[当前工况与使用](docs/workstation_simulation.md)、
[11/19 成果接入和边界](docs/workstation_upgrade_20261119.md)、
`runs/workstation_upgrade_20261119/holdout_final_9201_9202/report.md`。
具体源版本、输入图、角度、命令、实际状态和未完成原因均随运行保存。

2026-11-20 新增 [研究缺陷合成说明](docs/casting_synthesis_20261120.md)。
`casting_defects` 生成有根部的闭合薄片/余料，
`casting_synthesis` 导出六视角原始 RGB-D 和独立评价标签。
首批为 `runs/casting_synthesis_20261120/final_01`，
几何/文件验证在该目录的 `validation.json`。
该第一版 `workstation_mixed_01` 仅验证了观测和规划；
最新网页载入记录以以下团队先验闭环和 `runs/workstation/latest.json` 为准。

2026-11-21 团队数据复用进度见
[team_data_reuse_20261121.md](docs/team_data_reuse_20261121.md)。
曹完整公开包位于 `external/datasets/weldgrind_public_v1/`，整包与逐文件校验完成。
`casting_team_prior_20261121.json` 只使用训练划分，加入杨文档的飞边设置；
最终批次 `runs/data_reuse_20261121/team_prior_batch_final_02` 已验证
14 组/84 帧、22 对象/38 有根片段。原曹 RGB-D 反投影代码已实际执行，
该批 ROI 来自几何规则；新训练模型可独立产生完整图像候选，
SAM 和曹的动作网络没有运行。
杨的原始点云未取得，真实 097 拟合仍为 false；未知样件不可改名成 097。
完整动力学 `mixed_team_prior_closed_loop_02`：初始 2/5 段预检通过并执行，
加工点 P95 为 1.043017 mm，超过 1 mm 门限，停止后续补磨；
四个余料区域仍超过 0.3 mm，任务为 partial，不能标记完成。
schema 2 回放保留 1,618 帧、4 个共享余料状态，0 丢帧；
首轮 OOM 失败目录保留，最新运行峰值 RSS 约 2.23 GiB。
87 项完整测试通过，见 `runs/data_reuse_20261121/verify_memory_fix.log`。
实时网页记录以 `runs/workstation/latest.json` 为准，来源哈希须匹配当前代码。

```bash
MUJOCO_GL=egl .venv/bin/daiyi-serve --port 8848
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
bash scripts/verify.sh
```

当前主要模块是 `workstation_model`、`workstation_tooling`、
`workstation_inspection`、`workstation_planning`、`workstation_motion`、
`workstation_evaluation`、`workstation_run` 和 `workstation_benchmark`。
参数在 `config/workstation.json`；前端为 `web/workstation-*.js`。
修改相关源码后，之前的计划必须重新采集和预测，服务器校验源码哈希。
`workstation_replay` 是运行后的无损回放导出器，网页运行自动调用；
CLI 产物可用 `.venv/bin/python -m daiyi_grinding.workstation_replay 运行目录`
另行生成 `replay_compact.json`。原始完整反馈和执行 CSV 保留。

继续推进时先检查侧壁漏检、未观察对象和被夹具/工件几何限制的受阻段，
用真实标定/点云和有效夹持方案解决，不可删障碍或把部分结果标成完整完成。
当前相机/转台/磨料层是假设模型，余料去除没有标定材料速率或表面质量。
补磨和停止根据观测法向余量、实际运动安全门和轮数预算决定；
评测 `goal_complete` 不参与控制。抓手和实际加工点的跟踪误差分别记录。
开发/调试种子为 7007、9101、9102，当前最终独立种子为 9201、9202；
后续调试使用它们时须转为回归集，另选未观察的种子验证。

以下为仍保留的历史实验和远端接口说明。

主工程：`/home/dianrobot/daiyi_grinding/`。本工程为独立的戴屹视觉打磨项目，与 FR5 抓杯工程相互独立，不隶属其他项目目录，也不内置任何 VLA 模型或权重。源码以本机 `/home/dianrobot/daiyi_grinding/` 为准。

## 职责范围

| 内容 | 本机（`/home/dianrobot/daiyi_grinding/`） | 可选远端 GPU 机器 |
| --- | --- | --- |
| 编辑、Git、网页、RGB-D、ROI、标定和 MuJoCo | 本工程 `.venv` | 接收需要执行推理的源码 |
| 外部 VLA 影子实验接口（`daiyi-vla`） | `prepare` 导出观测、`audit` 离线安全审计 | 需显式提供外部 `--model-root` 才可运行 `infer` |
| 历史对照记录 | `results/vla/shadow_v1/` 仅作只读历史摘要 | — |

本机安装和入口见 [README.md](README.md)。运行说明使用可编辑安装，网页子进程沿用启动网页的 Python 解释器。

## 毛刺与批缝实验交接

2026-11-18 的复现、公开缺陷资料、算法比较、CAD 注入缺陷和选型结果见
[实验报告](docs/defect_experiments_20261118.md)。完整输出保存在
`runs/defect_study_20261118/`；生成器、算法参数、开发/留出种子和每项预测
均随实验保存。原仿真基线、观测单帧候选、已配准 CAD 参考检测各有独立
运行记录。新入口为 `daiyi-defect-study`、`daiyi-cad-defects`、
`daiyi-cad-resolution` 和 `daiyi-cad-refinement`。重跑使用新的输出目录，
参数根据开发集确定后再进行留出验证；本机 CPU/MuJoCo 即可运行，无需
GPU 模型权重。

## 定向同步（可选）

如需将本工程代码与观测同步到独立 GPU 机器，请先在 `.gpu.env` 中填写专属于打磨工程的远端路径（勿与其它项目目录混用）：

```bash
cp config/gpu.env.example .gpu.env
DAIYI_SYNC_DRY_RUN=1 bash scripts/sync_gpu.sh push-code
bash scripts/sync_gpu.sh push-code
```

`push-input RUN_ID` 只上传本机 `runs/vla/RUN_ID/` 下已准备的 NPZ、元数据和三路图像；`pull-output RUN_ID` 取回推理输出并保留本机输入文件，同步不使用 `--delete`。

## 外部策略影子接口（`daiyi-vla`）

本机生成观测和几何参考：

```bash
MUJOCO_GL=egl .venv/bin/daiyi-vla prepare \
  --scene-seed 7 --case mixed --output runs/vla/host_001
```

若后续在外部环境接入独立训练或微调好的策略模型执行 `infer`，必须显式传入 `--model-root /path/to/model`（或环境变量 `DAIYI_HY_ROOT`）与 `--checkpoint`，再于本机运行审计（默认 `execution_allowed=False`）：

```bash
MUJOCO_GL=egl .venv/bin/daiyi-vla audit --output runs/vla/host_001
```

真机、力控与加工质量按 [真机计划](docs/fr5_real_robot_plan.md) 推进。
