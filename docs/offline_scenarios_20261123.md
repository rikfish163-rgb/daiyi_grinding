# 097 离线场景与换型演练

## 复现入口

默认入口按开发种子 `97101` 生成 10 个 097 工况。每组用固定相机读取原夹持工装在 0°、60°、120°、180°、240°、300° 的 RGB-D 与实际仿真编码器反馈。入口在 `runs/enterprise_offline_20261123/mujoco.lock` 内串行创建模型、渲染、积分转台并关闭环境。现有结果目录会被拒绝。

```bash
MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.offline_scenarios \
  --output runs/enterprise_offline_20261123/scenarios/reproduction_01

MUJOCO_GL=egl OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  .venv/bin/python -m daiyi_grinding.offline_scenarios \
  --output runs/enterprise_offline_20261123/scenarios/matrix_plan_01 \
  --matrix full --dry-plan
```

同一 `part_id + cad_id + entity_seed` 下的形态、视角、裁剪和传感器扰动属于同一实体组。开发批次的 10 个工况共用一个实体组。`97111` 的 10 个场景评价工况已登记，保留给后续同 CAD 仿真评价。原 `9601/9602` 冻结评估及历史回归继续沿用各自的口径。扩展矩阵有 240 行，覆盖 4 个种子、10 个工况、2 个夹持配置、3 个传感器设置。对侧夹持行属于仿真规划条件。 [场景配置](../config/offline_scenarios_20261123.json)

## 默认批次实跑

`small_development_02` 实际生成 10 组、60 帧，传感器和几何审计全部通过。15 个余料对象共 23 个闭合片段，六视角可见 12 个对象。1,194 个根部采样点中有 354 个至少在一帧满足深度一致且传感器有返回。正常对照为零误检像素、零候选路径。单进程运行 94.97 秒，峰值 RSS 约 3.88 GiB。 [批次证据](../runs/enterprise_offline_20261123/scenarios/small_development_02/evidence.json) · [资源记录](../runs/enterprise_offline_20261123/scenarios/small_development_02_resources.log)

| 工况 | 观测噪声 MAD 标准差（mm） | 观测缺测比例 |
| --- | ---: | ---: |
| 常规合成先验 | 0.11965–0.12009 | 0.293%–0.308% |
| 毛刺缺测压力 | 0.24956–0.25044 | 2.470%–2.502% |
| 强缺测代理 | 0.49903–0.50079 | 7.903%–8.007% |

厚批缝生成一个复核区域，候选路径为零。低矮断续飞边和缺测毛刺在当前检测器下均无候选，低薄批缝和强缺测批缝各出现一个归为毛刺的候选。混合组 5 个余料中有 4 个达到可见阈值，生成 11 条候选。上述结果保留了遮挡、漏检、误分类和复核记录。 [逐场景报告](../runs/enterprise_offline_20261123/scenarios/small_development_02/report.md) · [独立核对日志](../runs/enterprise_offline_20261123/scenarios/small_development_02_independent_audit.log)

前序 `failed_wrapper_01` 保留了子清单路径错误时的部分产物。`small_development_01` 完成了渲染，但共享工作树在同批运行中发生修改，各场景的源码快照不一致。最终交付采用生成前已统一冻结的 `small_development_02`。独立核对覆盖了 1,487 个产物哈希和全部场景快照。 [失败批次清单](../runs/enterprise_offline_20261123/scenarios/failed_wrapper_01/scenario_manifest.json) · [中间批次记录](../runs/enterprise_offline_20261123/scenarios/small_development_01/evidence.json)

## 输入、产物与审计

- 飞边采用杨文档的合成方法。低薄、粗厚和断续参数在生成前锁定。卷曲和破裂薄片沿用原研究的弧长、厚度和曲率假设。小毛刺从曹公开训练划分 `602` 类的 150 条低矮尺寸记录中成对抽样。实际根部仍为 097 CAD 支持面上的候选位置。 [团队数据复用记录](team_data_reuse_20261121.md)
- 八个常规工况复用团队合成深度先验。毛刺缺测压力使用 0.25 mm 与 2.5% 缺测，强缺测代理使用 0.50 mm 与 8.0% 缺测。每帧用理想深度和输出深度核对实际 MAD 噪声与缺测比例。随机缺测代理保留自然自遮挡，相机安装及编码器分度沿用主站设置。
- `public/` 保存 RGB-D、名义参考深度、内参、变换、编码器读数及由几何检测器预测 ROI 导出的 CPU 点云。`evaluator/` 保存语义、实例、全根部、理想深度与近景。宏观近景单独用于几何审计。旧几何检测器统计供开发诊断，候选路径仍须经过整站预检、运动与复检门槛。 [整站验证口径](workstation_full_chain_20261122.md)
- 批次在首帧前冻结共用输入，各工况目录的 `manifest.json`、`source_snapshot/` 和 `validation.json` 可独立核对。添加余料的场景沿用导出 STL 的闭合、绕序、正体积、逐片嵌根和原始 CAD 支持检查。正常对照检查原 097 网格绑定、空缺陷与根部标签、空候选点云及零误检路径。根部可见指标要求理想深度一致且该像素有传感器深度，表示合成可见性。
- `evidence.json` 登记参数、源码、输入和产物的路径及 SHA256。渲染或校验程序异常写入 `failures`，传感器与几何校验异常写入 `validation_failures`。预期的换型拒绝记录在 `changeover_rehearsal.json`。批次异常时入口给出非零退出码，保留已生成的目录。

## 换型演练与现场证据

021 的 CAD 文件可读取且哈希已核对。主站模型固定绑定 097 的 CAD、实体名、坐标、夹持和夹具，021 请求停在导入校验。021 的工序阶段、企业确认的工件坐标基准和加工许可仍待补。 [换型记录](../runs/enterprise_offline_20261123/scenarios/small_development_02/changeover_rehearsal.json)

曹 `primitive_box_001_state_01` 属于公开合成训练划分。程序输入的 10,258 个点采样匹配原 RGB 和带噪深度，采样使用 0.5 像素中心。097 仿真沿用整数像素坐标。原始标定 `world_from_camera_cv_mm` 数值等于该程序输入中 `camera_from_world` 的逆，使用它复算世界坐标的最大误差约 0.00000352 mm。公开包在此样例提供可读 RGB-D 和 1,024 点的世界毫米轨迹，声明的原始名义 `mesh_target.ply` 缺失。导入请求保留自己的 ID 与坐标系，主站拒绝该换型。 [曹公开接口核对](../runs/enterprise_offline_20261123/scenarios/small_development_02/cao_public_interface.json)

实物导入仍需按新件补齐 CAD 身份与基准、`T_workpiece_gripper`、夹具与抓手、碰撞代理、相机及编码器标定、批准的分型根部与加工区、工具接触和全路径验收。杨的原始实采点云需补齐单位、哈希、工件身份和同 CAD 证据。企业定位 ±0.1 mm、型面 ±0.3 mm、换型 30 分钟、新品占机 2 小时及连续稳定运行 800 小时均待现场证据。 [企业需求与指标](../config/enterprise_requirements_20261123.json)

## 代码回归

Ruff 检查和此次修改文件的格式检查通过。基线已跟踪测试与场景新增测试共 119 项通过，其中场景测试 14 项。旧入口 `mixed` 和 `normal` 对照分别复现 14 条和 0 条路径。该回归以同一 MuJoCo 锁运行，测试进程未加载 Torch。 [单元测试日志](../runs/enterprise_offline_20261123/scenarios/workspace_regression_01/unittest.log) · [旧入口回归](../runs/enterprise_offline_20261123/scenarios/workspace_regression_01/entry_point_verification.log)
