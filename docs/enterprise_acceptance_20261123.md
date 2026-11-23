# 企业要求、离线证据与验收演练

2026-11-23 完成可机器执行的要求评估、现场输入校验和 27 个 synthetic
接口演练。另只读评估 42 组同输入矩阵、七组主入口运行及当前现场清单示例，
合计 50 份已有证据评估。没有重新运行这些磨削仿真，也没有操作实体机器人。
本轮全部新产物在
[acceptance](../runs/enterprise_offline_20261123/acceptance/)。
最终代码的复核产物在
[recheck_02](../runs/enterprise_offline_20261123/acceptance/recheck_02/)；
首轮产物保留，供追查报告字段变化。

[主证据](../runs/enterprise_offline_20261123/acceptance/evidence.json)记录实际命令、
输入路径/SHA256/来源、指标、具体失败、产物及真实数据待补项。
[最终矩阵](../runs/enterprise_offline_20261123/acceptance/recheck_02/acceptance_matrix.json)
保存逐份记录、逐项要求的判定和对应输入引用。

**指标来源及结论边界。**
[企业需求说明](../feishu_docs/上海戴屹具身AI检测打磨项目需求说明__wiki-TJ5Rwkla8i9zNgkV2zKchYuAn7g.md)
明确标注“草稿（基于项目需求交流会议及前期资料整理，待双方确认后更新）”，
整理日期为 2026-09-10。其中提出自主识别、自动规划执行和换型无需改代码。
定位 ±0.1 mm、型面 ±0.3 mm、换型 ≤30 分钟、新品占机 ≤2 小时、
连续稳定 ≥800 小时这五项数值来自
[愿景技术指标](../feishu_docs/具身打磨项目愿景技术指标__wiki-CZfWwvWKsimxGNkRFsHcSuvjncV.md)。
[要求配置](../config/enterprise_requirements_20261123.json)绑定上述来源及学长、
当前复用文档的 SHA256；没有把草稿解释为双方签署的现场验收规程。

每项输出 `evidence_status` 和 `status` 两列。前者回答已给出的离线/实测证据
是否满足该项门槛，后者回答该项在对应证据层级是否得到支持。企业目标只有
相应真实来源及所需覆盖条件齐全才可变为 `pass`；synthetic/example 的
企业目标始终为 `unknown`。`unknown`、缺少角色、哈希损坏及未知健康状态均
不能自动通过。二值 F1 没有来源约定的百分比门槛，因此输出数值并保持诊断
状态 `unknown`。

`enterprise_targets_evidenced` 只在所有企业目标有匹配的合格真实证据且
输入完整性通过时才可能为 true。当前
`enterprise_acceptance_passed=false`、`formal_acceptance_status=
pending_confirmed_acceptance_protocol`；正式验收还需要确认采样、统计和误差定义。
哈希校验保证读取的字节与清单绑定一致，不能认证提供者的实测来源声明。

| 独立量 | 本地评估口径 | 当前证据边界 |
| --- | --- | --- |
| 缺陷二值 | 由 TP/FP/FN 重算 F1，无正样本时保留 null | 多视角像素相关，F1 不是类别正确率或毫米精度 |
| 缺陷类别/实例 | matched ≤ visible ≤ generated；全部生成实例正确匹配才通过离线类别门槛 | 正常零缺陷对照单独计数 |
| 三维定位 | 每点 reference/predicted 三维毫米坐标的欧氏误差加 uncertainty；最大值 ≤0.1 mm | 须有独立参考、标定、flash/burr/casting_root 覆盖；欧氏定义是保守本地约定 |
| 运动跟踪 | 每一执行轮的抓手和加工点 P95 均严格 <1 mm，非预期接触为 0 | FK 相对目标关节曲线；无执行时为 unknown |
| 仿真正余料 | 合成余料 positive peak ≤0.3 mm | 不量测负余量、真实过磨、力、热或粗糙度 |
| signed 型面 | 同件同区同点 after ± uncertainty 的整个区间在双边公差内 | 加工区及保护区均须登记角色、独立覆盖 |
| 换型 | changeover_ready − changeover_start 的完整单调墙钟 | 须确认首件合格；不使用运行/计划/仿真时长 |
| 新品占机 | 同一设备、导入墙钟范围内 occupied_intervals 的区间并集 | 重叠区间只计一次；不能拼接规划耗时 |
| 控制器/工艺 | 程序、计划、反馈哈希及同件/request/execution 绑定；完成、零接触和全部标定为 true | 只读取回执，不下发控制程序 |
| 连续可靠性 | 同一 boot_id、严格递增心跳、完整健康状态、无故障及连续 ≥800 h | 真实日志还须有一致的带时区墙钟；本地最大心跳间隔 1 h |
| 虚拟可靠性 | 虚拟时钟、同次启动、同样的故障/间隔检查 | 本轮生成 800 个虚拟小时的 trace 并验证；不是 800 个真实墙钟小时 |

**学长能力 → 当前复用 → 差距 → 本轮证据。**
[11.12 模块说明](../feishu_docs/11.12代码结构、模块功能变化__wiki-H5fxwWJ3Yi5sHVkPRYTcYWc8nCb.md)
的实际核对日期为 11-11。文档明确这次只整理说明，没有改规划代码或重跑器件；
下表据文档及当前归档建立映射，没有声称本轮重新运行学长的 Windows/ROS 系统。

| 学长已说明的能力 | 当前复用及已记录成果 | 当前差距 | 本轮实际证据 |
| --- | --- | --- | --- |
| 指定标签/ROI → 表面几何候选 → 机器人运动两层规划 | 当前 097 整站保留观测几何与独立可达性/碰撞门槛；增加本项目训练的 RGB-D U-Net | 学长当时未接自主识别；当前三类 U-Net 也没有真实铸件及浇铸残根覆盖证明 | 二值、类别、定位三列分开；`high_f1_wrong_class` 的 F1=1 仍拒绝类别/整任务 |
| 联合转台入口、工具几何、连续碰撞、进退刀和有界修正 | 当前 ESTUN 持件到固定砂带的 MuJoCo C1 回放、携件检查及两种装夹已有归档 | 当前与学长规划内核、工具及回放不同，不能称原源码整体移植；仿真跟踪不证明加工精度 | `motion_at_boundary` 正确拒绝 P95=1 mm；无动作对照的运动状态保留 unknown |
| partial 区间及全源段覆盖分别记录 | 当前完整闭环需类别、观测、运动和几何门槛；正常对照单列 | accepted 源段或局部恢复不能当整件通过；学长旧 acceptance_report 有部分覆盖高估提示 | 42 组评估只确认六个正常无动作对照，非平凡完整加工为 0；保留逐区复检和预检 status=blocked 的具体原因 |
| plan_contract 的源码/配置/轨迹哈希绑定 | 当前 source_hashes、源码快照和冻结模型登记可追溯 | 当前源码漂移需与历史快照区分；哈希不认证真实数据身份 | 49 份归档的已登记来源均通过校验；演练拒绝 CSV、计划哈希损坏和混件/反馈不一致 |
| 每请求/逐段统计、耗时与验收报告辅助 | 当前 cycle_receipt 分离观测放行、正余料、运动、运行墙钟和仿真时长 | 计划转场提速不是现场换型/占机；请求及视角不等于独立实物 | 合成换型 1200 s、占机区间并集 5500 s；`planned_time_as_wall` 被拒绝，企业两项仍 unknown |
| 曹的合成 WeldGrind 数据接口、CPU 滤波与 RGB-D 反投影 | 当前先前已实际复用锁定原版 CPU physical_trajectory_filter 和反投影；本项目另训自己的 U-Net | 曹完整 SAM 基础/动作检查点缺失；95.42% 是动作阶段准确率，非缺陷准确率 | 本轮不训练或运行这些学长模型；evidence 的 missing_senior_weights_reproduced=false |
| 11.12 当时尚未接真实接触、去除、复扫与控制器 | 当前已有合成余料几何去除与复检归档、真实量测读取接口 | 力控、材料去除率、标定、热及粗糙度没有实测，未知 097 原始点云无法补齐 | signed 过磨/保护区案例及 synthetic 控制回执实际演练；真实验收/800 h 继续待补 |

前期数据身份及模型复用边界继续以
[团队复用说明](team_data_reuse_20261121.md)、
[本项目训练说明](learned_grinding_20261121.md)和
[整站闭环说明](workstation_full_chain_20261122.md)为准。本轮没有取得新的真实工件
数据，没有运行学长缺失的 SAM/动作权重，也没有更新冻结 U-Net 权重或默认配置。

**本轮真实执行结果。**

| 读取/执行对象 | 实际结果 |
| --- | --- |
| 42 组同输入归档 | 42 份评估；6 个正常无动作对照；0 个非平凡完整加工；18 份有执行，其中 17 份通过运动门槛 |
| 七组主入口归档 | 7 份评估；1 个正常无动作对照；5 份有执行且通过运动门槛；0 个非平凡完整加工 |
| 当前 site_acceptance.example.json | example 来源及空身份保持待补，被严格校验拒绝；没有转为实测 |
| 合成接口 suite | 27/27 复现预期判定，所有企业目标均保持 unknown |
| 针对性 unittest | 34 项通过：本轮 25 项及既有 workstation_quality 9 项 |
| Ruff | 三个独占 Python 文件 check 和 format --check 均通过 |

普通批缝 `seam_9903` 的归档正余料为 0.206590 mm、加工点跟踪 P95 为
0.123726 mm，类别匹配 1/1；两处复检峰值约 0.425 mm，第三处区间上界约
0.399 mm，因此 `simulation_cycle=fail`。记录保留被预检阻断段的预算上限和
干涉原因。毛刺 `burr_9903` 正余料为 0.248748 mm，抓手/加工点 P95 分别
1.750340/2.722325 mm，非预期接触为 0，类别匹配 0/3，运动与完整任务均失败。
这些是已有仿真记录的实际评估结果，不是本轮真实工件试磨。

27 个演练按用途保留独立目录：正常/无动作、正超差/负过磨/不确定度跨界、
保护点缺失/保护登记删除、CSV 哈希损坏/混件/NaN、JSON NaN、
定位超差/类别缺失、运动超差/等于边界、二值完美但类别错误、
墙钟超时/把计划时间当墙钟、控制未完成/计划哈希损坏/反馈缺失/控制混件、
cycle receipt 不一致、虚拟故障/重启/心跳大间隔、全部证据缺失。
每例有 `input.json`、原始 labeled 输入、`assessment.json`；
[suite_summary.json](../runs/enterprise_offline_20261123/acceptance/recheck_02/synthetic_suite/suite_summary.json)
记录预期与实际判定及具体失败。程序/计划示例写明 `not_executable=true`、
`simulation_only=true`，全部演练 `source_kind=synthetic`。

**复现与主 agent 集成。**
默认入口只读上述已有归档，生成 suite、矩阵及 evidence；输出目录必须不存在：

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.enterprise_acceptance \
  --output runs/enterprise_offline_20261123/acceptance_reproduction_001

# 单独读取其他 agent 的一个 run；或用 --bundle 读取附加离线回执。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.enterprise_acceptance \
  --run /absolute/path/to/new_run \
  --bundle /absolute/path/to/input.json \
  --skip-suite \
  --output runs/enterprise_offline_20261123/acceptance_integration_001

# 仅演练，不读取归档。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.enterprise_acceptance \
  --suite-only \
  --output runs/enterprise_offline_20261123/acceptance_suite_001

MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m unittest \
  tests.test_enterprise_acceptance tests.test_workstation_quality -v

.venv/bin/python -m ruff check \
  src/daiyi_grinding/enterprise_acceptance.py \
  src/daiyi_grinding/workstation_delivery.py tests/test_enterprise_acceptance.py
.venv/bin/python -m ruff format --check \
  src/daiyi_grinding/enterprise_acceptance.py \
  src/daiyi_grinding/workstation_delivery.py tests/test_enterprise_acceptance.py
```

CLI 退出 0 表示评估计算及所选演练完成，不表示企业验收通过。自动化消费者必须
读取 `status`、`source_integrity`、`enterprise_targets_evidenced` 和
`enterprise_acceptance_passed`，不能用进程退出码替代量测/工艺门槛。
Python 集成接口为 `evaluate_run(directory)`、`evaluate_evidence(manifest)`、
`assess_bundle(bundle, base=...)` 和 `run_synthetic_suite(new_directory)`；
不依赖当前工作目录，导入评估器不加载 torch 或 MuJoCo。

bundle 的最小外形：

```json
{
  "schema": "daiyi-enterprise-input-v1",
  "identity": {
    "part_id": "097",
    "serial": "SYNTHETIC-097-A",
    "cad_revision": "synthetic-cad-contract-v1",
    "fixture_revision": "synthetic-fixture-contract-v1"
  },
  "artifacts": {
    "site_manifest": {"path": "site_manifest.json", "sha256": "<64位小写SHA256>"},
    "localization": {"path": "localization.json", "sha256": "<64位小写SHA256>"},
    "timing_receipt": {"path": "timing_receipt.json", "sha256": "<64位小写SHA256>"},
    "controller_receipt": {"path": "controller_receipt.json", "sha256": "<64位小写SHA256>"},
    "reliability_receipt": {"path": "reliability_receipt.json", "sha256": "<64位小写SHA256>"}
  }
}
```

所有 artifact 路径相对 bundle 所在目录，或用绝对路径。完整可运行输入见
[synthetic_normal/input.json](../runs/enterprise_offline_20261123/acceptance/recheck_02/synthetic_suite/synthetic_normal/input.json)。
`artifact_reference(path, relative_to=...)` 可生成引用；其 SHA256 是读取时绑定，
不认证该文件的来源。所有量测/回执角色须带同件
`part_id,serial,cad_revision,fixture_revision`；存在不一致即拒绝。

| artifact 角色 | schema 与主要必需字段 |
| --- | --- |
| run_result / cycle_receipt | 现有 workstation v3 / daiyi-station-receipt-v1；回执 scope 必须是 simulation；双方质量/时钟一致 |
| source_hashes | 现有 filename → SHA256；优先验证 source_snapshot，CAD/登记模型可按已知来源核对，未解析来源保持 unknown |
| site_manifest | 保留原真实验收字段；本轮另接受 synthetic/example 供演练；CSV 六列及必需区域，zone_roles 映射 machined/protected |
| localization | daiyi-localization-evidence-v1；units=mm、coordinate_frame、measurement_system、reviewer、独立/标定/覆盖布尔值、对应点 samples |
| timing_receipt | daiyi-time-evidence-v1；wall_monotonic、equipment_id、两个产品、code_changed、完成/首件确认、四事件及 occupied_intervals |
| controller_receipt | daiyi-controller-evidence-v1；request/execution/controller_id、plan/program/feedback 路径哈希、plan_sha256、完成及六个标定布尔值 |
| controller feedback | daiyi-controller-feedback-v1；与回执相同身份、来源、request/execution、计划/程序哈希、完成及严格递增时间/非预期接触 samples |
| reliability_receipt | daiyi-reliability-evidence-v1；boot_id、clock_kind、complete、逐次 healthy 心跳；real_software_log 另需一致的带时区 wall_started_at/wall_finished_at |

真实来源严格按角色使用 `real_measurement`、`real_process_log`、
`real_controller_receipt`、`real_software_log`。既有 `site_acceptance(manifest)`
仍仅接受 `real_measurement`，CSV/区域返回字段兼容；新增可选清单哈希绑定和
明确的 protection_coverage_verified。旧清单不必新增 zone_roles 才能读取，
但没有角色证据时不能通过本评估器的加工/保护区要求。

实物待补仍包括可确认同件的 097 原始点云/RGB、独立缺陷位置参考、
加工区与保护区磨前/磨后点位、相机/装夹/TCP/控制器坐标标定、力控/去除工艺、
真实跨型号首件与墙钟占机日志、连续 800 小时运行日志及双方确认的验收口径。
