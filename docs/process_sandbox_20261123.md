# 粗磨、精磨与测量反馈沙盒

2026 年 11 月 23 日。沙盒用九个固定点、120 mm 假定加工长度演练粗磨、精磨和复检回磨。运行依赖 NumPy，所有力、去除、温升、磨损和净空均来自合成代理。097 铸件的工艺参数、实际磨削节拍及型面精度仍待独立实测。

## 闭环与来源

- 首检和每次执行接触后的复检都量测同一组点，覆盖加工区和保护区。正值高于名义表面，负值低于名义表面。每个点的整个 `读数 ± 声明半宽` 区间都落在 `[-0.3, +0.3] mm` 内才满足测量门槛。缺测、保护点区间出界、已确认过磨，或没有足够证据支持下一道均停机复核。[企业愿景指标](../feishu_docs/具身打磨项目愿景技术指标__wiki-CZfWwvWKsimxGNkRFsHcSuvjncV.md) · [企业需求表](../config/enterprise_requirements_20261123.json) · [整站复检](workstation_full_chain_20261122.md)
- 每个工况的两组共享初始型面、按同一扫描序号取样的噪声流、停机判据与加工预算。固定组按阶段使用预设进给和力。自适应组按阶段选进给，再通过当前量测峰值与名义去除系数计算受限的指令力。粗磨余量目标为 0.55 mm，精磨目标偏置为 +0.05 mm。转入精磨后维持该阶段，默认最多三道粗磨、四道精磨，其中精磨首道之后最多三道回磨。[配置](../config/process_sandbox_20261123.json)
- 去除代理按接触时长、力、材质系数和磨损降效计算有符号变化。磨损指数随力与接触时间的乘积累积。同一力和进给下，旧模型的磨损增力与降效恰好抵消，本轮使磨损通过降效改变净去除。以力乘进给速度估算供热，采用一阶散热。柔顺量记录为力除以代理刚度。基准力、进给、刚度、去除和热参数沿用或扩展自合成假设。[配置](../config/process_sandbox_20261123.json) · [接触探针边界](contact_probe.md)
- 接触前检查保护点交集、标量净空、力信号和剩余时间。超力在接触开始时触发，去除量为零。实际进给偏差造成超时，以及接触中的力掉线或温升跨限，均只累计首个故障发生前的接触。故障锁存后即使随后量测合格也拒绝放行。净空借用学长 11.12 报告记录的 1.5 mm 作标量预检阈值，其作用域仅为沙盒输入。[学长 11.12 报告](../feishu_docs/11.12代码结构、模块功能变化__wiki-H5fxwWJ3Yi5sHVkPRYTcYWc8nCb.md)
- 初始余量参考杨国翱的合成飞边高度量级、曹正恒 WeldGrind 合成凸起的量级。高噪声样例参考曹公开合成 RGB-D 中约 0.12 mm 的深度噪声，扫描偏差、缺测位置和声明的不确定度独立设置。合成高斯扰动截断在三倍标准差，区间半宽按样例声明。[团队先验及来源](team_data_reuse_20261121.md)

## 实跑结果

原 24 组先跑了一遍，曾出现掉线后放行、固定组超热门限后继续加工，以及到时限后才停止。本轮保留原样例的余量和扰动输入，加入 12 组挑战明确停机路径。原 `budget_stop`、`thermal_stress` 等复合工况会先触发超力，隔离样例另行观察时间和温升。两组采用同一复检和保护规则后的结果如下。

| 36 组配对工况 | 自适应 | 固定 |
| --- | ---: | ---: |
| 控制器放行 | 9 | 6 |
| 放行且合成型面合格 | 8 | 5 |
| 隐藏偏差导致的误放行 | 1 | 1 |
| 拒绝放行 | 27 | 30 |
| 全组平均接触时长（s） | 3.006139 | 2.459622 |
| 全组平均能量代理（J） | 3.622018 | 4.077451 |

平均值包含未开始接触的拒绝。仅自适应放行有 4 组，仅固定放行有 1 组。[全量摘要](../runs/enterprise_offline_20261123/process/paired_01/summary.json) · [配对 CSV](../runs/enterprise_offline_20261123/process/paired_01/case_comparison.csv)

在原 24 组上将扫描种子改为 `process_sandbox_sensitivity_00` 至 `process_sandbox_sensitivity_15` 分别复算。每次的合成型面合格放行数，自适应范围为 5–9，固定为 2–5。该复算量化了当前样例对扫描扰动的敏感性。

| 样例 | 观察结果 |
| --- | --- |
| `baseline_moderate` | 自适应两道粗磨和两道精磨后放行，最终合成余量 +0.125 mm。固定组三道后最终余量 +0.184 mm，量测区间仍待复核。 |
| `thin_stock` | 固定组两道精磨放行，自适应组一道后因测量不确定度转复核。 |
| `combined_undercut` | 一道精磨后分别留下 −0.809 mm 和 −0.373 mm，两组均拒绝且停止追加加工。 |
| `force_dropout_after_engagement` 与 `runtime_stop_isolated` | 两组均分别在接触满 1.0 s 与 2.0 s 时停止，仅保留已执行部分。 |
| `thermal_guard_isolated` | 温升代理达到 12 K 时停止。自适应 4.118 s，固定 2.600 s。 |
| `missing_protected_measurement` | 完成首道后，保护点 2 在复检中缺测，两组停止。其合成表面保持原值。 |
| `hidden_measurement_bias` | +0.42 mm 合成余量被读成 +0.22 ±0.02 mm，导致两次误放行。离线审计逐点记为正超差。 |

单次运行以测量与保护条件给出 `passed`。合成真值只在控制器停止后用于审计，`conforming_release_count` 和 `false_release_count` 保留实质差异。全部 72 次运行约束检查通过，57 次工艺拒绝和 2 次刻意注入的误放行均写入证据。[逐类失败与来源哈希](../runs/enterprise_offline_20261123/process/paired_01/evidence.json)

## 复现与实测输入

在仓库根目录运行，输出目录名须新建。

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.process_sandbox --output paired_new_01
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  .venv/bin/python -m unittest discover -s tests -p test_process_sandbox.py -v
.venv/bin/python -m ruff check src/daiyi_grinding/process_sandbox.py tests/test_process_sandbox.py
.venv/bin/python -m ruff format --check src/daiyi_grinding/process_sandbox.py tests/test_process_sandbox.py
```

结果落在 `runs/enterprise_offline_20261123/process/<run>`。逐案例 JSON 保存停止前后的同点量测、各道命令和反馈，CSV 保留阶段、时间和有符号残余对照。CLI 在契约不符时写出失败证据并返回 1，意外异常返回非零。故障测试覆盖保护区缺测、负余量、力掉线后量测恰好合格、时间/道次/回磨限制及故障退出码。[逐道 CSV](../runs/enterprise_offline_20261123/process/paired_01/process_trajectories.csv) · [测试](../tests/test_process_sandbox.py)

现场标定需要同件、同装夹版本和同点位的独立磨前磨后量测及不确定度，配对记录指令与实际进给、法向力、材料与去除体积或质量、磨具磨损、温度、粗糙度及 ESTUN 控制器回执。拟合输入、辨识试验和按工件与磨具留出的验证字段见计划。[标定计划](../runs/enterprise_offline_20261123/process/paired_01/calibration_plan.json)
