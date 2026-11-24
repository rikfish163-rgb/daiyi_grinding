# 戴屹企业对标离线验证与经验库

日期：2026-11-23。对象是 ESTUN iER220-2700 持 097 工件到固定砂带的当前主站。
本轮按企业需求和学长成果设置九个研发角色，把场景、标定、识别、规划、工艺、
复检、控制可靠性、验收和统一证据运行连接起来。

数值目标来自[愿景技术指标](../feishu_docs/具身打磨项目愿景技术指标__wiki-CZfWwvWKsimxGNkRFsHcSuvjncV.md)；
[项目需求说明](../feishu_docs/上海戴屹具身AI检测打磨项目需求说明__wiki-TJ5Rwkla8i9zNgkV2zKchYuAn7g.md)
仍是草稿。软件测试和仿真分别提供对应范围的证据，企业实物验收以独立现场记录为依据。

## 学长成果如何进入本轮工作

| 来源 | 已取得并实际复用 | 本轮补充 | 仍缺的输入 |
| --- | --- | --- | --- |
| 杨的铸件飞边研究 | 文档中的带状、变厚和断续飞边参数 | 同一 097 CAD 的多形态、遮挡、缺测和换型拒绝演练 | 可确认同件的原始点云、单位、正常区与真实根部 |
| 曹的 WeldGrind | 已校验 190 个基础几何、2,800 合成状态；原 CPU 轨迹滤波和 RGB-D 反投影 | 原数据身份、0.5 像素中心、坐标变换、公开样件换型绑定核验 | 完整 SAM 和动作权重、真实铸件泛化证据 |
| 王的规划路线 | 筛查、按原因修复、局部修复、全段验收的路线 | 连续曲线途中障碍负例、预算检查、旧计划失效和新留出执行 | 实机控制器、真实装夹和碰撞几何的确认 |
| 当前专用模型与整站 | 已冻结的 RGB-D 模型、六视角观测、携件执行、复检和有限回磨 | 独立故障压力、signed 容差、执行质量绑定和统一可复现证据 | 相机/TCP 标定、真实去除率、力、温升和表面质量 |

来源追查见[团队数据复用](team_data_reuse_20261121.md)和
[专用模型训练](learned_grinding_20261121.md)。曹原论文动作准确率不能充当本项目的
缺陷识别准确率；二值分割、类别实例、根部定位、运动跟踪和整件复检分别统计。

## 并行角色与交付

| 角色 | 交付入口 | 证据或说明 |
| --- | --- | --- |
| 企业要求与验收 | `daiyi-enterprise-acceptance` | [要求映射和证据 schema](enterprise_acceptance_20261123.md) |
| 场景与换型 | `daiyi-offline-scenarios` | [场景矩阵与来源核验](offline_scenarios_20261123.md) |
| 标定与误差预算 | `daiyi-registration-stress` | [标定压力与误差传播](registration_stress_20261123.md) |
| 识别压力与根部指标 | `daiyi-perception-stress` | [识别压力验证](perception_stress_20261123.md) |
| 携件规划与运动 | `daiyi-planning-stress` | [连续曲线和留出记录](planning_stress_20261123.md) |
| 粗磨、精磨和回磨 | `daiyi-process-sandbox` | [配对工艺沙盒](process_sandbox_20261123.md) |
| 磨后双边容差 | `daiyi-reinspection-stress` | [signed 复检与停止](reinspection_stress_20261123.md) |
| 控制与可靠性 | `daiyi-reliability-stress` | [控制故障与回执](reliability_stress_20261123.md) |
| 统一运行与经验导出 | `daiyi-enterprise-offline` | [运行器与证据读取](#复现与证据读取) |

模型调度按用户最新要求优先 anyrouter 的 6.1 Sol，其次 Gemini 4、其他来源的
DeepSeek Flash、Kimi 和 GLM。收尾任务使用 6.1 Sol；场景与工艺的两项 6.2 Sol
接续在用户降低 Codex 优先级前已经完成。权限或凭据失败的通道已停止并转接，
失败记录保留，后续不反复重试同一不可用通道。

## 已完成的专项实验

以下是各角色的独立专项记录；最终统一运行会另外生成新证据，历史输出不覆盖。
案例、配对运行、图像帧、运动段和故障循环使用各自的分母。

| 环节 | 实际规模与结果 | 结论边界 |
| --- | --- | --- |
| 场景 | 10 组、60 帧；传感器与几何核验全部通过；15 对象、23 闭合片段；正常零误检/零路径 | 开发种子 97101；97111 仅登记，240 行扩展矩阵仅规划 |
| 标定 | 4 例完成、3 例按指定原因拒绝、意外失败 0；20,000 次误差预算抽样 | 误差预算 P95 0.399752 mm，不支持本轮 0.1 mm 诊断目标 |
| 识别 | 24 帧基线、30 次压力观测；候选二值 F1：几何 21.77%、学习 92.16%、组合 40.29%；21 项专项测试通过 | 已见训练回归；32 个实例观测含重复视角，根部是合成质心代理 |
| 规划 | 开发 thin120483 完成 6/6 段；两个冻结新留出样本完成 13/13 段 | 新留出 2/2 运动通过，0/2 观测与整件放行 |
| 工艺 | 36 组配对、72 次运行；自适应合格放行 8，固定合格放行 5；契约意外失败 0 | 两策略各有 1 次隐藏偏差误放行；57 次工艺拒绝 |
| 复检 | 12 个 sensor-only 用例；2 verified、10 保守拒绝、意外失败 0 | 负向过磨、近线不确定、缺测和非法输入分别保留 |
| 验收 | 27/27 演练复现预期；只读评价历史 49 份运行及一份现场示例 | 42 组主矩阵为 6 个正常完整对照、0 个非平凡完整加工；执行运动 17/18 通过 |

专项来源：
[场景](../runs/enterprise_offline_20261123/scenarios/small_development_02/evidence.json)、
[标定](../runs/enterprise_offline_20261123/registration/evidence.json)、
[识别](../runs/enterprise_offline_20261123/perception/stress_sol_02/evidence.json)、
[规划](../runs/enterprise_offline_20261123/planning/evidence.json)、
[工艺](../runs/enterprise_offline_20261123/process/paired_01/evidence.json)、
[复检交付时快照](../runs/enterprise_offline_20261123/reinspection/sensor_only_003/evidence.json)、
[验收](../runs/enterprise_offline_20261123/acceptance/evidence.json)。
复检交付后的输入保护和主流程停止已增加，最终结果使用冻结后重跑的证据。

两个规划新留出种子为 2026112307、2026112311，在修复前登记。它们的表面跟踪
P95 都约 0.11749 mm，非预期接触为零；仿真余料峰值分别为 0.19357 mm 和
0.13688 mm。原始独立运行见
[留出 1](../runs/enterprise_offline_20261123/planning/closed_loop_holdout_01/closed_loop/result.json)和
[留出 2](../runs/enterprise_offline_20261123/planning/closed_loop_holdout_02/closed_loop/result.json)。
这些样本使用后属于今后的回归集，后续调参需另登记新留出样本。

## 修复及失败处置经验

1. **连续轨迹途中障碍。** 原四点预检会漏掉 q0 在 0.05–0.07 rad 的窄障碍。
   新预检利用 PCHIP 导数界控制最大 0.012 rad 采样间距。独立 MuJoCo 球体
   负例中旧检查通过，新检查发现 5.824 mm 穿透。采样预算不足时整条命令拒绝。
   该离散检查仍需运动反馈，不能代表连续网格碰撞证明。
2. **旧计划与身份失配。** 装夹、坐标、场景、关节限位、工具或运动源码变化
   会使计划绑定失效。控制请求、执行编号和独立质量回执也必须绑定到当轮身份。
3. **负向过磨。** 复检在逐锚点 `±2σ` 区间上执行 ±0.30 mm 双边判定。
   过磨、负边界不确定和非法复检停止自动回磨，即使还有正余量路径和预算。
   缩短法向量、NaN 公差或追加非法视角都不能造成放行。
4. **标定退化与 CAD 面投影。** 单面正常点不能约束六自由度。
   三角形投影增加保守候选补查，避免大面质心较远造成误配。
   拟合、独立正常区检查和加工区测量的原始行索引互不混用。
5. **测量隐藏偏差。** 工艺两策略使用相同噪声与停止条件，但均被未建模偏差
   欺骗一次。合成真值只用于事后审计，保留该误放行；下一步通过独立校准、
   重复量测和传感器交叉核验辨识偏差。
6. **漏检与遮挡。** 场景演练中厚批缝进入复核，低矮断续飞边和缺测毛刺未检出。
   这些问题进入失败目录；异常检测和批准磨削不能合并成一个成功率。
   识别默认小目标召回三种方法均为 48.15%；附加 0.30 mm 深度噪声后，
   毛刺组学习/组合方法的 F1 降至 3.09%，各有 903 个误报像素。
   权重和阈值保持冻结，该压力失败保存供后续输入质量与训练覆盖研究。
7. **证据不完整。** 无哈希来源、文件缺失、运行中源码改变、错误执行绑定
   和未复现的预期负例均不能算软件套件通过。冻结版本以新目录运行，保留失败首轮。

## 企业目标与下一步输入

| 企业目标 | 当前可用证据 | 必须补齐或确认 |
| --- | --- | --- |
| 视觉定位 ±0.1 mm | 同源合成配准、独立正常点检查、向量误差传播 | 相机型号和工作距离、跨装夹重复定位、同件真实根部点云、量测规程 |
| 型面 ±0.3 mm | signed 区间判定、过磨负例、合成去除与观测复检 | 同件磨前磨后独立对应点、保护区、校准不确定度、粗糙度 |
| 换型 ≤30 分钟 | 工件/工具/坐标绑定与错误换型拒绝 | 真实新件 CAD、工装和相机/TCP 绑定；现场全过程墙钟 |
| 新品占机 ≤2 小时 | 离线场景矩阵与工艺演练 | 首件程序、工艺标定、试磨、复检的实际占机记录 |
| 稳定连续 ≥800 小时 | 控制 mock 故障注入、恢复、循环和独立回执 | 实机控制器连续时钟、错误/恢复日志与现场质量反馈 |

所有物理企业目标当前均保持待证。软件循环时间、渲染墙钟和虚拟累计时钟不能
替代对应的实机指标；真实数据到位后可直接进入现有严格输入与验收接口。

## 复现与证据读取

安装已声明依赖的现有环境可只刷新入口：

```bash
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .

MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/daiyi-enterprise-offline \
  --include-main-chain --task-timeout 1800 \
  --output runs/enterprise_offline_20261123/suite_new
```

运行器按模块串行生成独立证据，记录实际输出路径、命令、退出码、墙钟、
单进程峰值 RSS 和源码前后哈希。工艺输出使用 `process/` 根下的新子目录，
在统一回执中记录真实路径。所有 MuJoCo 创建模型、渲染和积分共用
`runs/enterprise_offline_20261123/mujoco.lock`，不要给自持锁模块重复外套锁。
峰值 RSS 使用 Linux `wait4(child_pid)` 的进程全生命周期统计，包含 fork/exec 前
的启动内存及已等待的后代进程峰值；多个任务的资源回执分别记录。

独立仓库检查：

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  flock runs/enterprise_offline_20261123/mujoco.lock bash scripts/verify.sh
```

入口退出 0 表示相应软件执行与契约满足，验收读取矩阵和回执的实际字段。
经验记录会保留每类成功、预期拒绝、剩余缺口及下一组实验；同一工况的像素、
实例、根部、运动、观测和企业结论分别读取。

本轮开始的源码快照为
[baseline_source_hashes.json](../runs/enterprise_offline_20261123/baseline_source_hashes.json)。
Git 尚无初始提交；代码审查以该快照与暂存内容为对照。原默认配置、冻结权重、
历史 `results/` 和历史 `runs/` 保留，新证据均写入本轮目录。

## 最终统一验证

各专项收尾完成后冻结 `src/`、`tests/`、`config/` 和 `pyproject.toml`，
执行八类套件与 thin/normal 主链。这里将登记最终统一回执、检查日志和保护审计。
