# 连续曲线预检、预算和装夹绑定离线验证

2026 年 11 月 23 日。当前模型仍是 ESTUN iER220-2700 持 097 工件到固定砂带加工。本次修复了一处确定可复现的连续曲线预检漏检，保留原四点策略和原源码对照。没有修改配置、碰撞几何、保护区、1 mm 运动门或企业 ±0.3 mm 型面要求。

本次沿用已有的观测路径接口、曹的 CPU 路径后处理、杨的合成几何先验与王的“逐段诊断、有界修复、完整验收”流程。公司 CAD、公开的关节限位和王的工站布置提供当前模型依据；实机零位、TCP、力控和装夹标定仍待测量。阅读依据包括 [整站主链说明](/home/dianrobot/daiyi_grinding/docs/workstation_full_chain_20261122.md)、[学习模型与闭环说明](/home/dianrobot/daiyi_grinding/docs/learned_grinding_20261121.md)、[团队数据复用说明](/home/dianrobot/daiyi_grinding/docs/team_data_reuse_20261121.md)、[11.12 模块说明](/home/dianrobot/daiyi_grinding/feishu_docs/11.12代码结构、模块功能变化__wiki-H5fxwWJ3Yi5sHVkPRYTcYWc8nCb.md)和 [11.19 阶段报告](/home/dianrobot/daiyi_grinding/feishu_docs/阶段进展汇报11.19__wiki-FCaxwgXVui5vZQky5CPceyAsnYg.md)。

旧 `preflight` 对每个 PCHIP 区间仅采样时间比例 0.25、0.5、0.75、1.0，采样间距没有随关节位移增大。最小开发用例是一条从 0 到 0.4 rad 的连续转场，障碍位于 0.05–0.07 rad；四点检查错误批准整条曲线。同一接触分类器在 0.06 rad 能检测到障碍，排除了参考曲线不同或接触豁免的解释。针对性测试先失败，再修复通过，原始 [失败日志](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/diagnosis_01/red_test.log)和 [诊断记录](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/diagnosis_01/diagnosis.json)均保存。

新默认策略对每段三次多项式求出各关节绝对速度上界，据此加密，保持每次采样之间的单轴位移不超过原 Cartesian leg 检查使用的 **0.012 rad**，并补查区间起点。预检最多检查 200,000 个样本；超限明确拒绝整条命令。`sampling_policy="legacy_four"` 保留原四点采样，供同输入消融使用。加密仍是离散几何检查，不能证明任意薄小障碍的连续净空，也不能代替实际跟踪验收。

独立的真实 MuJoCo 小场景使用一个转动球体和一个固定球体，两个策略使用同一模型、同一关节曲线和同一碰撞几何。旧策略检查 4 点并错误通过；新策略检查 35 点，发现 **5.824 mm** 穿透后拒绝。[真实 MuJoCo 负例](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/closed_loop_regression_01/mujoco_fixture/result.json)记录了场景哈希、命中位置和穿透量。这是仿真碰撞验证，不是一次 097 磨削成功率试验。

`PlanningBudget` 在 IK、碰撞和连续曲线检查的前后检查原有每段 **12 s** 预算，并支持运行器指定调用数预算。预算耗尽不会进入无预算的转场回退，也不会保留已生成的半条 leg。RRT 原有 220 节点/3 s 上限继续保留；转场 shortcut 的完整构造也受 3 s 预算限制。底层一次有界求解可能越过墙钟截止点，后续检查会拒绝保留结果；这里没有宣称精确秒数的硬中断。

新 `build_plan` 在第一条命令登记 `motion_binding`，绑定装夹名称、夹持和装载坐标、关节限位、场景文件、内存中的碰撞模型数组及相关源码/配置哈希。`preflight`、`execute` 和转场优化在使用已绑定计划前拒绝哈希失效。对侧装夹变化需要重新观测、规划和绑定。`bridge` 继续要求与原目标完全相同的关节分支，并保留原端点；相同 Cartesian 姿态但不同关节分支不算有效连接。历史没有此字段的命令可作离线回归输入，新的 `build_plan` 输出全部绑定，已有网页总来源哈希门仍应继续使用。

默认压力 suite 只构造解析软件夹具，不加载 torch、不创建完整 MuJoCo 模型。覆盖两组途中碰撞、整段不可达、错误关节分支、IK 预算耗尽、连续曲线采样预算耗尽、装夹变化导致旧计划失效，共 **7/7** 保护检查通过。每个途中碰撞工件都保留两个原始段：一段无障碍，一段完整曲线穿过障碍。没有裁出其中可行区间替代源段。

| 软件注入同输入结果 | 原四点策略 | 新有界采样 |
| --- | ---: | ---: |
| 原始段数 | 4 | 4 |
| 被批准的完整源段 | 4，其中 2 段误批准 | 2 |
| 所有原段均通过的工件 | 2，其中 2 件含漏检 | 0 |
| 实物放行 | 0 | 0 |

这些是解析软件夹具的计数，说明段数与全件计数不同，不是企业 CAD 通过率。[suite 原始结果](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/software_suite_02/suite.json)同时记录每个输入、失败原因、预算和两个策略结果。

在修复前冻结了 [新 holdout 登记](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/holdout_registration_01.json)，SHA256 为 `cbc5d9b9703344be7fdc62d8a3e915f30edaf7e2c9e80ad7035a4704b87c0663`。薄批缝种子 `2026112307`、`2026112311` 用于新的独立规划验证；每件全部六个视角属于同一实体样本。压力种子为 `2026112321`、`2026112327`。`120483` 和 `9903` 是已见开发回归，不混算 holdout。没有根据 holdout 结果调整采样间距、预算、姿态、进给、控制器或验收门。

本轮完成后，上述新种子也已被观测，后续使用只能计已见回归。运行器的 `sample_role` 标识该冻结批次的登记身份，不使重复运行变成新的独立证据；下一轮策略选择应另行冻结新种子。

实际串行运行了三组当前整站、原夹持、固定砂带、完整六视角的一轮有磨削闭环，使用现有 `workstation_run`，没有替换质量或复检 API。每组都保存关节命令、预检、逐步实际反馈 CSV、执行/余量 JSON、登记区域、磨后六视角、复检与 `cycle_receipt.json`。

| 工况角色 / 种子 | 完整段准入及执行 | 抓手 P95 mm | 表面点 P95 mm | 仿真峰余量：磨前 → 磨后 mm | 磨后复检 | 全件 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| 开发回归 / 120483 | 6/6 | 0.0750 | 0.1182 | 0.5210 → 0.1369 | 3 需回磨、2 不确定 | hold |
| 新 holdout / 2026112307 | 7/7 | 0.0727 | 0.1175 | 0.9176 → 0.1936 | 1 验证、4 需回磨 | hold |
| 新 holdout / 2026112311 | 6/6 | 0.0752 | 0.1175 | 0.7982 → 0.1369 | 3 需回磨、3 不确定 | hold |

三组非预期接触均为 **0**，实际运动均通过当前 1 mm 门，均记录 `truth_used_for_control=false`。两个新 holdout 共 **13/13 段**完整执行、**2/2 件**运动通过，但 **0/2 件**观测/整件放行。`2026112307` 在一轮预算结束后停止；另外两组在复检待复核条件下停止。余量是实际工件位姿驱动的仿真几何裁切评分，不是双边型面实测，也没有校准力、速率、热或粗糙度。

[可导出的计数图](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/planning_counts.png)将软件负例误批准和冻结工件的整段/运动/整件计数分开展示。

开发回归 `120483` 的 `paths.json` 与历史文件逐值相同，1,159 条关节命令的 `q`、`t` 与阶段序列也逐值相同，最大差均为零。本次只增加预检和绑定元数据，没有用新速度曲线制造正例提升。[命令同输入回归](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/diagnosis_01/thin_120483_command_regression.json)保留了双方输入哈希。

毛刺 `9903` 的已知 **2.722 mm** 表面跟踪 P95 没有被本次修复解决。历史实际反馈中 trace 单步关节变化最大仅 0.00180 rad，没有错分支跳变；第 3 区域抓持挠度 P95 约 0.00019 mm，而表面跟踪误差持续增大。该例应继续诊断伺服/姿态误差，而不是放宽运动门或改称型面通过。[历史反馈审计](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/diagnosis_01/burr_9903_feedback_audit.json)保留原值。旧复杂工况的 0/33、对侧装夹 23/33 和 8/12 全件计数也未被本次软件压力测试替代。

本次实跑墙钟分别为 85.54、96.23、84.73 s，最大 RSS 为 **2,313,236 KiB，约 2.21 GiB**，无交换。三个完整模型串行运行，统一使用 `runs/enterprise_offline_20261123/mujoco.lock` 协作锁；默认纯软件 suite 用时 **0.42 s**，峰 RSS **168,808 KiB**。没有覆盖历史 `results/`、其他历史 `runs/`、默认配置或冻结模型。

```bash
# 默认只跑低资源软件压力 suite，输出目录必须是新的。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.planning_stress \
  --output runs/enterprise_offline_20261123/planning/reproduction_suite_01

# 显式运行真实 MuJoCo 碰撞负例与一轮整站磨削回归。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.planning_stress \
  --mujoco-fixture --closed-loop --seed 120483 \
  --output runs/enterprise_offline_20261123/planning/reproduction_cell_01

# 对侧装夹必须使用新目录，运行器会生成新的模型、观察和计划。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.planning_stress \
  --closed-loop --seed 2026112307 --grasp-setup opposite_simulation \
  --output runs/enterprise_offline_20261123/planning/reproduction_opposite_01
```

`--mujoco-lock` 可指定主 agent 的同一协作锁。锁忙时运行器在构建模型前明确失败，不并发新建模型。输出目录已存在时拒绝覆盖。正常完成但验收 hold 的运行退出 0，并在 `evidence.json.failures` 中明确记录 `recorded_hold`；保护测试失败或运行错误退出非零。默认冻结登记缺失时会记录证据错误，不会把已知种子冒称新 holdout。

接口保持 `build_plan(sim, paths, mode, publish)`、`preflight(sim, commands)`、`execute(...)` 和 `shorten_transfers(...)` 的原位置参数兼容。新增预检参数均为关键字参数；新增命令字段只在第一条出现；非碰撞的预检拒绝也有 `reason`，不会再误取空碰撞列表。修改仅在本 agent 指定的三个原模块、新压力模块、对应测试和本文档内。

[验证回执](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/verification_01/checks.json)记录 **14 个规划故障测试 + 4 个既有运动/余料接口回归**全部通过，限定文件 Ruff check 和 format check 通过。主 agent 集成其他并行改动后应重新跑主链，不能把本次源码版本的结果改标为集成后结果。

[汇总 evidence.json](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/evidence.json)提供 `task, scope, commands, inputs(path/hash), metrics, failures, artifacts, real_data_pending`；每个独立运行目录也有自己的证据文件。尚缺同件杨 097 原始扫描、确认根部、实机坐标映射、相机/TCP 与装夹重复定位标定、力/磨具/去除标定，以及带不确定度的独立磨前磨后双边型面量测。

[最终冻结清单](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/final_manifests_01/freeze_manifest.json)登记六个本 agent 所有文件、汇总证据和三份闭环逐文件清单的 SHA256。汇总的闭环输入引用这些带哈希的清单；目录字段仅说明输出位置。逐文件清单包含运行时源码快照，原始记录不改标为主线程最终集成版本。

## 预算输入校验修复

只读交叉审查发现，NaN 秒数和 NaN 调用上限会使比较始终为假，未触发预算耗尽；NaN 采样上限也能绕过 `required_samples > sample_budget`。修复前契约反例及源码字节保存在独立的 [budget_validation_01](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/)；原规划汇总、冻结清单和三组闭环目录均保留原值。

`PlanningBudget(seconds, *, max_ik_calls, max_collision_checks, now)` 保持原位置参数及默认值。`seconds=None` 继续表示未设置墙钟截止；显式秒数必须是有限、非布尔且非负的实数。两个调用上限必须是 `None` 或非布尔、非负整数，`4.0` 也会拒绝。零秒数或零调用数合法，在首次对应检查时停止。全部限值在读取时钟前完成校验。

`preflight(sim, commands, *, sample_budget=200_000, ...)` 的采样上限必须是非布尔、非负整数；NaN、Inf、浮点数、字符串和 `None` 均拒绝，即使命令为空也不会跳过校验。校验在关节曲线和碰撞工作开始前执行。200,000 默认上限、0.012 rad 间距、12 s 每段预算、RRT/shortcut 上限、门限和轨迹生成均保持原值。

新增纯软件契约测试覆盖非法预算、空命令入口、默认 `None`、零预算、原截止边界、整数调用数和样本数边界。测试使用解析软件夹具，执行守卫阻止 MuJoCo 模型创建、渲染及积分调用；没有运行真实 MuJoCo 负例或整站主链。

修复前新增的 11 个契约测试方法记录了 78 个失败子测试；修复后这 11 个方法与原有 14 个规划故障测试共 **25/25 通过**。[green 回执](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/green_checks.json)绑定本次使用的两份源码与测试文件精确字节 SHA256，记录 MuJoCo 库已导入、受守卫的执行调用为零、torch 未加载。[Ruff 回执](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/ruff_checks.json)记录限定文件 check 与 format check 均退出 0。[历史保护核对](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/history_verification.json)确认 428 个受保护文件的集合、大小及 SHA256 与修复前一致，四份修复前源码/测试/文档快照也未变。

```bash
.venv/bin/python -B \
  runs/enterprise_offline_20261123/planning/budget_validation_01/run_budget_checks.py \
  --phase green

.venv/bin/python -B -m ruff check \
  src/daiyi_grinding/workstation_planning.py \
  src/daiyi_grinding/workstation_motion.py tests/test_planning_stress.py

.venv/bin/python -B -m ruff format --check \
  src/daiyi_grinding/workstation_planning.py \
  src/daiyi_grinding/workstation_motion.py tests/test_planning_stress.py
```

检查脚本拒绝覆盖同名回执；复现时应将该脚本复制到新的独立运行目录。此次 [修复证据](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/evidence.json)和 [新源码冻结清单](/home/dianrobot/daiyi_grinding/runs/enterprise_offline_20261123/planning/budget_validation_01/freeze_manifest.json)只支持预算输入契约及纯软件回归。旧 `final_manifests_01/freeze_manifest.json` 中的源码路径现在对应历史版本，其登记哈希不能改写成新版本哈希。主线程将在统一冻结后独立重跑 minimal MuJoCo fixture 与 thin/normal 主链，再形成当前集成版本的物理仿真证据。
