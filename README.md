# 戴屹视觉打磨

当前默认是公司 CAD 的 **ESTUN iER220-2700 完整工位**：固定相机按转盘编码器
采集六视角，自动检测批缝/毛刺、实时预测路径；抓手夹持 097 铸件送到固定砂带，
完成携件避障、实际动力学执行、余料几何去除、复检与补磨。
磨具、安装底座、定位夹具、抓手和工件均为独立对象。

主工程位于 `/home/dianrobot/daiyi_grinding/`。工况根据 `INDEX_LOCAL.md`
和 11/19 最新飞书组会材料确定；具体见
[当前仿真说明](docs/workstation_simulation.md)、
[组会成果接入与验证](docs/workstation_upgrade_20261119.md)。
已实际复用曹正恒公开的 CPU 轨迹滤波代码；缺失的 SAM/动作权重未被冒充为
已复现的模型。历史 FR5 结果仍在原目录中保存。

2026-11-20 已加入[有根部的研究缺陷合成](docs/casting_synthesis_20261120.md)：
变厚、断续、卷曲、破裂飞边与不规则毛刺，附着到当前 097 CAD。
首批 14 组工况、84 帧 RGB-D 及独立根部/实例标签已保存；
网页研究合成分组与 `daiyi-casting-synthesis` 可直接使用。

2026-11-21 已取得并完整校验曹的 2,800 状态公开合成数据，新增
[团队数据复用与 097 参数先验](docs/team_data_reuse_20261121.md)：
杨文档的带状飞边参数、曹训练集的成对凸起尺寸和深度噪声接入当前 CAD，
可在网页“缺陷构造来源”切换。杨的原始真实点云尚未取得，
因此当前仍是团队先验，097 实测参数校准待补。

2026-11-21 已完成[专用 RGB-D 模型训练与自主打磨验证](docs/learned_grinding_20261121.md)。
网页可切换新训练模型和原几何规则；冻结测试原始分割 F1 为 94.01%，
同输入的可测候选 F1 从原规则 53.50% 提升至 91.91%。
当前 ESTUN 的基准毛刺闭环已将三处生成余料降至 0.3 mm 以下；
类别匹配仍需复核，团队先验复杂工况仍有可达性和跟踪限制。

2026-11-23 新增[企业对标离线验证与经验库](docs/enterprise_offline_20261123.md)。
九个研发角色覆盖场景换型、标定误差预算、识别压力、规划、工艺、复检、
控制可靠性、企业验收和统一运行器。八类独立套件通过同一入口运行，
每次保存实际命令、退出码、耗时、源码与输入哈希、产物和失败处置记录。
软件套件通过、仿真运动通过和企业实物验收各自记录。

```bash
# 使用新目录；运行器串行调度，并管理共享 MuJoCo 锁。
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/daiyi-enterprise-offline \
  --include-main-chain --output runs/enterprise_offline_20261123/suite_new
```

重新安装入口可运行 `.venv/bin/python -m pip install --no-deps --no-build-isolation -e .`。
模块方式为 `.venv/bin/python -m daiyi_grinding.enterprise_offline`。
视觉定位 ±0.1 mm、型面 ±0.3 mm、换型 30 分钟、新品占机 2 小时和连续
800 小时仍需现场独立证据。当前签名复检已经区分正余量与负向过磨；
过磨、负边界不确定或非法复检会停止自动回磨。

## 启动

在本工程目录执行：

```bash
bash scripts/bootstrap.sh
bash scripts/verify.sh
MUJOCO_GL=egl .venv/bin/daiyi-serve --port 8848
```

打开 [当前整站演示](http://127.0.0.1:8848/)。网页默认载入与当前源码匹配的
ESTUN 记录；没有匹配记录时自动采集、检测和规划。点击“运行整站仿真”
执行通过验收的路径，再查看六视角证据、逐段原因、实际回放和加工后复检。
重新规划和执行各自创建独立记录，写入 `runs/`。

```bash
MUJOCO_GL=egl .venv/bin/daiyi-workstation \
  --case mixed --seed 7007 --views multi --tool belt \
  --max-passes 2 --realtime-factor 0 --output runs/current_full_new

MUJOCO_GL=egl .venv/bin/daiyi-workstation-benchmark \
  --seeds 9201,9202 --development-seeds 7007,9101,9102 \
  --output runs/current_holdout_new
```

独立验证报告：
`runs/workstation_upgrade_20261119/holdout_final_9201_9202/report.md`。
识别 F1/IoU、对象检出、段完整验收、实际运动和余料残留分别统计。
有路径、进程正常结束或部分加工均不等于完成全部打磨目标。
复检根据相机观测的法向余量决定补磨或停止，评测真值只用于判定最终成果。

要求 Python 3.12 以上及可用 EGL。Ubuntu 系统组件为 `libegl1 libgl1 fonts-noto-cjk`。安装使用本工程 `.venv`，版本固定在 [requirements.lock.txt](requirements.lock.txt)。

## 目录

```text
daiyi_grinding/
  src/daiyi_grinding/   当前 ESTUN 流程、CLI、保留的历史实验
  tests/               ROI、标定、入口和双机文件交换测试
  docs/                架构、研究、结果、真机计划和既有交接记录
  web/                 本地演示网页
  assets/fairino5_v6/   7 个官方 FR5 可视网格
  assets/ier220_workstation/ 公司整站、抓手、夹具、097 CAD 与碰撞分块
  results/             固定基线与历史结果摘要
  deliverables/        原始 PDF、视频配套演示包和图表
  runs/                新仿真、影子实验和报告输出，不进入 Git
  config/              无密钥的服务器配置模板
  scripts/             安装、验证和定向同步入口
  pyproject.toml       Python 包、命令入口和代码检查配置
```

## 历史结果与资料

| 内容 | 文件 |
| --- | --- |
| 历史阶段安排 | [status.md](docs/status.md) |
| 主工程结构与数据流 | [architecture.md](docs/architecture.md) |
| 双机分工与推理命令 | [HANDOFF.md](HANDOFF.md) |
| 正式演示：14 段路径、120.87 秒 | [运行结果](results/demo_v3/result.json)、[视频](results/demo_v3/demo.mp4)、[执行 CSV](results/demo_v3/execution.csv) |
| 42 个合成场景、两方法 84 条配对记录 | [汇总](results/benchmark_v3/summary.json)、[协议](results/benchmark_v3/protocol.json)、[逐场景记录](results/benchmark_v3/episodes.jsonl) |
| 图像标定 | [结果](results/calibration/calibration.json)、[方法](docs/calibration.md) |
| 外部 VLA 历史对照审计 | [audit.json](results/vla/shadow_v1/audit.json)、[实验说明](docs/vla_experiment.md)（只读历史对照，执行动作 0 条） |
| 甲方资料 | [PDF](deliverables/戴屹视觉打磨仿真验证与FR5计划.pdf)、[原始演示 ZIP](deliverables/戴屹视觉打磨演示包.zip) |
| 研究与真机推进 | [research.md](docs/research.md)、[fr5_real_robot_plan.md](docs/fr5_real_robot_plan.md) |
| 既有实验记录 | [experiment_journal.md](docs/experiment_journal.md) |
| 原仿真复现、毛刺/批缝算法并行比较 | [2026-11-18 实验报告](docs/defect_experiments_20261118.md)、[独立运行目录](runs/defect_study_20261118/) |
| 公开缺陷资料与照片实测 | [来源与实验说明](docs/defect_sources_20261118.md) |
| 服务器完整资料位置 | [remote-artifacts.md](docs/remote-artifacts.md) |

## 保留的历史实验入口

输出目录须使用新名字。当前 CLI 的默认输出均位于 `runs/`，不会替换固定基线。

```bash
# 感知、路径、动力学和视频。
MUJOCO_GL=egl .venv/bin/daiyi-run \
  --seed 7 --case mixed --video --output runs/demo_001

# 感知与路径投影批测；动力学单独验证。
MUJOCO_GL=egl .venv/bin/daiyi-benchmark \
  --seeds 6 --seed-start 4007 --output runs/benchmark_001

# ROI 使用原始图像中的像素坐标多边形 JSON。
MUJOCO_GL=egl .venv/bin/daiyi-run \
  --roi /absolute/path/roi.json --output runs/roi_001

# 独立标定复现，不自动替换当前有效外参。
MUJOCO_GL=egl .venv/bin/daiyi-calibrate --output runs/calibration_001
```

报告工具按需安装。重建输出使用独立目录，原始交付文件保留在 `deliverables/`。

```bash
.venv/bin/python -m pip install -e '.[reports]'
.venv/bin/python -m daiyi_grinding.build_delivery --output runs/delivery_001
```

开发规则和 Google 公开规范来源见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 毛刺检测候选实验

原 `geometry` 默认流程保持可复现。无 CAD 参考的 FR5 合成场景可使用
`robust_local` 候选；该候选在观测深度噪声超过门槛时自动使用几何基线，
输出的 `selected_backend` 记录实际后端。`multiscale` 保留为实验对照。

```bash
MUJOCO_GL=egl .venv/bin/daiyi-run --method robust_local \
  --seed 6007 --case thin --video --output runs/thin_candidate_001

# 实验扩展包含 CAD 三角网格造样与对比图依赖。
.venv/bin/python -m pip install -e '.[experiments]'
MUJOCO_GL=egl .venv/bin/daiyi-defect-study \
  --stage prepare --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/daiyi-defect-study \
  --stage evaluate --split development --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/daiyi-defect-study \
  --stage select --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/daiyi-defect-study \
  --stage evaluate --split holdout --output runs/defect_study_new
```

CAD 底座使用独立参考检测器，必须提供与观测注册到同一姿态的正常参考
深度。`cad_resolution` 生成两种像素分辨率下相同物理场景的观测；
`cad_refinement` 比较原几何、常规参考与保留细批缝的参考检测，入口可用
`.venv/bin/daiyi-cad-resolution --help` 和
`.venv/bin/daiyi-cad-refinement --help` 查看。原 CAD 版本 `097`、`021`
是文件标识，其制造阶段及原始缺陷尚未确认。

本轮记录包含完整 FR5 动力学执行、CAD 静态检测与表面轨迹预览，两类
结果分别解释。公开飞边照片为注塑塑料，不能作为真实金属铸件泛化成绩；
真实去除量、恒力加工和表面质量仍待实测。

## 适用条件

当前 ESTUN 的准确率来自同姿态正常 CAD 参考和合成缺陷 RGB-D，
不能作为真实金属铸件泛化成绩。固定相机/转台和砂带功能面有显式仿真假设；
机械臂与携件碰撞体是 CAD 派生近似。材料去除为实际工具扫掠的余料顶点几何模型，
尚无标定恒力、材料去除速率、热效应和表面质量结论。真实机器人操作未接入。

下面是先前 FR5 实验的适用条件，保留用于解释历史结果：

当前成果是合成 RGB-D 感知、轨迹规划与仿真执行。真实金属泛化、恒力磨削、材料去除和表面质量尚待实机验证。原几何基线薄批缝的正式评估召回为 28.01%；新增候选与 CAD 参考法的适用条件和独立种子结果见本轮实验报告。本工程未内置 VLA 模型或权重；历史外部 VLA 影子对照（`results/vla/shadow_v1/`）中允许执行的动作数为 0，当前主流程完全基于 RGB-D 几何检测与规划。
