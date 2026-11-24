# 企业离线打磨工程接续

## 当前功能结果

097 薄批缝已完成有限去除率下的两轮实际加工闭环。首轮独立复检确认正超差，
局部回磨保持此前许可的 CAD 子段，去除同轮重复路径，并按观测上界和声明的
名义率降低短段进给。第二轮复检全部放行，控制器依据观测停止。

| 运行 | 实际结果 |
| --- | --- |
| `functional_resume/thin_120483_finite_03` | 初始 4 段、局部回磨 3 段实际执行；2 个区域、145 个锚点放行 |
| 首轮 / 回磨后独立深度帧 | 150 / 54 |
| 首轮 / 最终最大观测上界 | 0.435841 / 0.077113 mm |
| 运动与停止 | 两轮运动通过，`observed_regions_verified` |
| 工艺假设 | 恒定法向率 0.009 mm/s；名义规划率与 plant 率相同，未标定 |
| 保留的对照 | `thin_120483_finite_02` 完成；`thin_120483_finite_01` 两轮后正超差与不确定并存，停止待复核 |
| 其他完整例 | 新薄批缝、团队先验批缝和毛刺六个 `complete` 记录 |
| 正常件 | `normal_9904_01` 零候选、零加工，`no_defect_detected` |

主入口与可复跑命令见 [README](../../README.md#离线加工与结果示例)；
完整加工页为
[有限率回磨示例](../../runs/enterprise_offline_20261123/functional_resume/examples_success_thin_120483_finite_03/index.html)，
原始记录为
[result.json](../../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_03/result.json)，
[调用记录](../../runs/enterprise_offline_20261123/functional_resume/thin_120483_finite_03.invocation.json)
保存当前实现的独立复跑。
[功能说明](../functional_grinding_20261123.md)保存各工况、逐轮区间、实际段数与限制。

源代码已接入像素中心深度/分割渲染、全部 GL 上下文的磨后网格同步、重复量测
正下界确认、局部回磨去重和有限率接触时间规划。导出工具保存去除率和相机布局，
区分映射、去重、候选与实际接纳段数。旧结果与源码快照保留原值。

独立低位固定相机已实际补见新毛刺的外壁。单姿态与分段规划共用既有法向退让，
新外壁段已完成识别、规划、实际磨削和独立复检；顶面 `review` 仍有正超差，
整件保持 `partial`，见
[低位毛刺加工页](../../runs/enterprise_offline_20261123/functional_resume/examples_partial_burr_2026112397_low_camera_02/index.html)。
断续目标的高分辨率补采已恢复 CAD 支持，初始法向 P80 低于门限；
局部名义轨迹依据历史独立重复量测的正下界，按全件离线语义重新准入，
因加工姿态夹爪与砂带前部结构干涉而拒绝。本次没有磨削命令及磨后观测，
新加工前量测没有提供新的超容差下界确认，见
[实体准入报告](../../runs/enterprise_offline_20261123/functional_resume/perception_01/local_trace_admission_01/report.md)。

| 扩展工况 | 实际执行与量测 |
| --- | --- |
| 新低位毛刺 | 候选 2、实际执行 2；独立磨后深度帧 150；区域 3/4 验证；`reinspection_requires_review` |
| 原低位毛刺对照 | 候选 2、实际执行 1；区域 1/4 验证；`followup_safety_rejected`，原值保留 |
| 顶面 `review` | 最大观测上界 2.971267 mm；许可轨迹回磨候选为 0 |
| 共用法向退让 | 60 mm；真实模型回归由原构造拒绝转为完整回位通过 |
| 断续局部轨迹 | 1 个独立空间位置、9 帧历史重复量测；30 点 CAD 支持、局部选取 16 点 |
| 断续准入 | 候选 1、接纳 0、命令 0；360 个组合在 12 秒预算内结束，`motion_admission_blocked` |
| 断续历史 / 新最大正向下界 | 0.306162 / 0.280571 mm |

运动通过、注册区域观测放行、整件仿真完成与企业实物验收分别记录。
真实工件型面、相机/TCP 标定、材料去除率、力、磨耗、温升及连续生产证据仍待现场数据。

## 当前版本验证

| 检查 | 实际结果 |
| --- | --- |
| `ruff check src tests` | 通过，退出 0 |
| `ruff format --check src tests` | 99 个文件通过，退出 0 |
| 128 MiB 父进程内存下的 RSS 定向复现 | 通过；保留原大小任务差值断言 |
| `scripts/verify.sh` | 368 项测试通过，退出 0 |
| 感知与软件入口 | mixed 14 条路径、normal 0 条路径 |

[规定检查回执](../../runs/enterprise_offline_20261123/functional_resume/verification_functional_01/required_checks.json)
及[完整验证日志](../../runs/enterprise_offline_20261123/functional_resume/verification_functional_01/verify_after_rss.log)
保存实际运行结果。入口产物位于
[mixed](../../runs/verification-J3yJfXqP/mixed/result.json)与
[normal](../../runs/verification-J3yJfXqP/normal/result.json)。

Linux `wait4` 的 RSS 是进程全生命周期峰值，包含 fork/exec 前的启动内存。
大小任务的比较测试从轻量独立解释器启动，与统一运行器 CLI 的运行方式一致。
修改限于测试环境和资源口径说明，加工、复检和验收门限保持原判定。
本轮五项功能任务的现有成果已经收齐；结果页与新图已完成，不重新启动旧任务。

用户原目标：结合学长工作和当前不足，5–10 个子代理并行，把暂缺真实工件数据的环节实际跑通，对标企业要求并积累丰富、可复现的经验。此前已开展 9 路研发。

用户最新指令：“停下来，现在你做的事情完全没有达到我的预期。仔细阅读 https://github.com/rikfish163-rgb/nooverkill 的 agents.md 按照这个更新，更新完了，上下文压缩了重新载入，继续做这个工程。”

## 已更新的工作方式

已阅读上游 `AGENTS.md`、`README.md`、完整 `RULES.md` 和安装器。上游明确 `RULES.md` 是完整规则，适配器不得改写。已用官方安装器将其原文安装到工程根 `AGENTS.md`，来源版本 `11abac281b3d68e1d73549a895918ff7a703641a`。工程约定单独保留在文件末尾。

重新载入后先读根 `AGENTS.md` 和 `CONTRIBUTING.md`。工作重心转向功能与实际结果。每项检查前说明具体会发现什么故障、失败后如何改变动作；已通过且无相关变化的检查不重复。已有审计、证据与守卫保留，按实际功能需要使用。

## 当前工程状态

目录 `/home/dianrobot/daiyi_grinding`，Python `.venv/bin/python`。主站 ESTUN iER220-2700 持 097 工件到固定砂带。仅做离线、仿真、mock；不控制实体机器人、不发外部消息、不创建 PR、不启动服务器。

9 路成果已写入共享工作区，源码在 `src/daiyi_grinding/`，实验在 `runs/enterprise_offline_20261123/`。总说明为 `docs/enterprise_offline_20261123.md`，统一入口为 `python -m daiyi_grinding.enterprise_offline`，但最终统一运行尚未执行。验收、可靠性、运行器的源码需要按当前实际文件使用，取消不回滚已写内容。

用户要求停止时，已取消可靠性和统一运行器的剩余任务；验收任务已完成。最后任务清单只有当前主线程运行，没有运行中的子代理或仿真进程。此时不要直接重启上一轮审阅工作。

| 分支 | 已取得的实际结果 |
| --- | --- |
| 场景 | 10 组、60 帧；正常样例零误检与零路径，低矮断续飞边和缺测毛刺仍漏检 |
| 标定 | 4 例完成、3 例指定拒绝；误差预算 P95 约 0.400 mm |
| 识别 | 24 帧基线、30 次压力观测；learned 候选 F1 92.16%，正确类别实例 30/32；小目标召回 48.15%，噪声压力仍失败 |
| 规划 | 两个新薄批缝样例共 13/13 段实际磨削、运动 2/2 通过、整件观测放行 0/2；原毛刺 9903 表面跟踪 P95 2.722 mm |
| 工艺沙盒 | 36 组配对、72 次运行；自适应合格放行 8，固定 5；两者各 1 次隐藏测量偏差误放行 |
| 复检 | 12 个 sensor 案例，2 放行、10 保守拒绝；已有双边容差与过磨停止 |
| 控制可靠性 | checkpoint、序号、时钟与质量绑定已修；42 项定向测试通过，最终 120 循环状态以现有文件为准 |
| 验收与集成 | 已实现企业指标评价与统一串行入口；最终统一运行未完成 |

详细内容与路径见总说明和 `HANDOFF.md`。上述是运行经验，不是企业实物验收。企业目标为定位 ±0.1 mm、型面 ±0.3 mm、换型 ≤30 分钟、新品占机 ≤2 小时、连续稳定 ≥800 小时；真实工件量测、现场标定和工艺数据仍缺。

## 接续动作

薄批缝有限率闭环以 `thin_120483_finite_03` 为当前实现的已完成样例继续使用。
扩展工况时，读取新观测、路径准入、实际执行和停止原因，针对真实未完成环节修复。
合成真值继续只用于事后评价，控制决定依据当前轮的观测与执行结果。

交付要能让用户实际运行，并明确看到处理前后、成功样例、失败原因及下一次处置。已有 9 路成果用于这个目的；通过数量与文档数量不能替代功能进展。

确有源码变化时运行相关验证；项目要求的 Ruff 和 `scripts/verify.sh` 仍执行，并说明用途。`pyproject.toml` 的新 CLI 尚未做 editable install；需要新命令时使用 `.venv/bin/python -m pip install --no-deps --no-build-isolation -e .`。统一运行器可使用 `--include-main-chain --task-timeout 1800 --output <新目录>`。

当前最终规定检查已完成，结果见“当前版本验证”。后续仅在相关源码变化、新失败、
未解决问题或明确要求时重跑。历史物理结果属于各运行目录保存的源码版本。

## 运行与模型

保留历史 `results/`、历史 `runs/`、权重和默认配置；新输出使用新目录。仓库无初始提交，保留已有暂存内容，禁止 commit、reset 和 shell worktree。

MuJoCo 共用 `runs/enterprise_offline_20261123/mujoco.lock`；不要给已持锁模块再次外套锁。环境 `MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`，学习推理优先 CPU。

代理模型优先 `anyrouter/gpt-6.1-sol` max，其次用户允许的 Gemini 4、其他来源 DeepSeek Flash、Kimi、GLM，减少 Codex，必要时可用；额度或鉴权失败已授权自动转接。用户先前的模型列表图片在 `/home/dianrobot/.t3/userdata/attachments/0f8433e9-d4cc-41d5-9ec7-63337e2cdbf4-6ea8d423-d1dc-4311-93ea-8c05167abd5d.png`，选择新模型前按需读取。
