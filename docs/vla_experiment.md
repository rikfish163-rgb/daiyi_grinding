# Hy-VLA 在 FR5 铸件场景的真实 shadow 实验

2026-10-07（用户客户端日期）实际运行完成发布权重推理、对照和适配审计。结果为 **6 组候选均拒绝执行，VLA 执行动作 0 条**。这是有真实模型输出的负迁移检查，不是 FR5 零样本打磨成功，也不是打磨闭环。

## 模型来源与范围

本次选取已在本机备好的 [Hy-Embodied-0.5-VLA](https://github.com/Tencent-Hunyuan/Hy-Embodied-0.5-VLA)。[Hy 论文](https://arxiv.org/abs/2606.14409)首次提交于 2026-06-12，当前 v2 为 2026-07-20；官方仓库于 2026-06-15 发布代码和 UMI/RoboTwin 权重。[π0.7 论文](https://arxiv.org/abs/2604.15483)首次提交于 2026-04-16，当前 v2 为 2026-04-24。本次实际运行 Hy，π0.7 只用于确认同期研究背景，未下载、训练或运行其模型。这里不声称 Hy 是截至实验日期所有 VLA 中最新或最优的模型。

- Checkpoint：`tencent/Hy-Embodied-0.5-VLA-RoboTwin`。
- 权重 revision：`bd7bba6f5934ad62293a2a34f74760c6a3ef2ff8`。
- 实际代码 commit：`af57e7507ec5964b52fdf6296741e553cbcd3288`。
- 实际完整 9,053,587,008 字节权重 SHA256：`3bd6c16225f905a298340489d519498d4e5ecf5bcdd28a5c1df63e29894fef60`。运行时重新读取整个权重校验。
- 使用既有 `hy_vla_repro/extension/loading.py`，只容许不参与动作路径的 expert LM head 缺项；其余缺失/多余权重都会报错。本次审计记录全部动作路径权重成功加载。
- 未微调，训练更新数 0。模型 Python 环境与 MuJoCo 环境分开，未向主环境安装 torch。
- 固定 `CUDA_VISIBLE_DEVICES=1`，RTX 4090；加载 11.864 秒，显存峰值 allocated 9.139 GiB、reserved 9.289 GiB。独立本地进程已退出，不占用既有模型服务。

## 冻结输入和明确的替代假设

用当前主工程只读导入的 `Simulation(generate(7, "mixed"))` 渲染 768×576 RGB，保持真实初始 FR5 TCP 位姿。RGB-D 几何基线在检查相机中检测 8 个连通特征，生成 14 条可处理的参考路径；参考路径只用于空间距离审计，没有作为 Hy 的监督标签或隐含控制目标。

| Hy 输入通道 | 实际来源 | 缺口 |
| --- | --- | --- |
| `top_head` | MuJoCo overview | 观察视角与 RoboTwin 训练分布不同 |
| `hand_left` | FR5 的实际 wrist 相机 | 单臂与工具配置不同 |
| `hand_right` | MuJoCo 固定 inspection 相机 | 用检查视图替代不存在的右腕相机 |
| 左臂状态 | 当前 FR5 球磨头 tip TCP，米、wxyz，gripper=0 | 未标定 RoboTwin EEF 到磨头 TCP 的转换 |
| 右臂状态 | 复制左臂 TCP 的虚拟状态 | 场景没有第二臂，复制只为满足固定 16 维输入 |
| RGB-D 深度 | 几何基线与审计 | Hy 的 RGB/state 接口未消费深度 |

Hy 的世界坐标数值直接接收 MuJoCo world 坐标，没有跨平台标定。尤其官方解码会融合 relative 与 absolute 分支，absolute 分支带有原训练坐标分布，直接把结果当作 FR5 TCP 没有依据。官方 wrapper 输出 wxyz，实验按相同顺序记录，再显式转换为 xyzw 供 SciPy/IK 使用。

历史为 K=6、间隔 5 的启动观测；只有当前真实帧，过去槽按既有官方兼容逻辑做零像素 padding。没有复制一段伪造的运动历史。每个输入数组的 dtype/shape/内容哈希、相机映射、状态含义、源码哈希均保存在 `input_manifest.json`；原始 RGB/depth、内参、OpenCV 相机到 world 的变换和关节状态保存在 `input.npz`。

## 实验设置与实际结果

指令原文保存在每组 `inference.json` 记录中。打磨指令要求使用左臂球磨头沿窄批缝和局部毛刺移动并保留设计特征；hold 指令要求双臂保持不动。运行 seeds 17、29、43，对照固定 seed17。黑图对照只将三路 RGB 全置零，状态与指令保持同组设置。

| 组 | seed | 推理秒 | 首步左 TCP 平移 mm | 20 点中 IK 可解 | 参考路径最近距离范围 mm |
| --- | --- | ---: | ---: | ---: | ---: |
| grind / observed | 17 | 1.617 | 73.54 | 20 | 328.10–341.19 |
| grind / observed | 29 | 1.194 | 78.04 | 7 | 326.60–347.72 |
| grind / observed | 43 | 1.210 | 70.96 | 20 | 329.05–347.65 |
| hold / observed | 17 | 1.037 | 79.15 | 20 | 330.95–350.04 |
| grind / black images | 17 | 1.055 | 66.21 | 20 | 317.76–325.70 |
| grind / observed repeat | 17 | 1.029 | 73.54 | 20 | 328.10–341.19 |

每组保留完整 `raw_normalized` 40×20（官方归一化输出、relative/absolute 两半，尚未解码）和 `decoded_world_eef` 20×16（双臂 xyz+wxyz+gripper）。它们都不是 FR5 六关节角。原始动作未裁剪、未投影回参考轨迹，便于检查失败来源。

- 同 seed17、同观测、同指令重跑，完整 decoded 动作逐元素一致，最大绝对差 0。
- 相对首组，seed29 与 seed43 的左 XYZ 逐坐标 RMS 差为 5.554 与 4.082 mm。
- hold 指令的同种子左 XYZ 逐坐标 RMS 差为 6.524 mm；此输入下它仍提出约 79 mm 的首步移动。
- 黑图同种子左 XYZ 逐坐标 RMS 差为 11.274 mm。

这些差异证明本次采样对种子、提示和图像有响应，不证明识别了毛刺、执行了指令或具备跨平台打磨能力。

## 拒绝规则和审计含义

所有组都有无条件执行阻断：未标定双臂到单臂映射、右臂和右腕缺失、EEF 到球磨头 TCP 未标定、无铸件打磨微调/任务验证、没有动作时间尺度/力控制/材料去除模型。候选接口 `HyShadowCandidatePolicy.propose(...)` 返回 `execution_allowed=False`；代码没有调用真实机器人、模型服务或 `execute(...)`。

另保存逐点诊断：xyz 工作范围、首步及相邻点步长、单位四元数、工具朝下角、距 RGB-D 参考路径/表面点距离、虚拟右臂移动、gripper 输出、FR5 IK 与端点 primitive proxy 接触。诊断范围为 x∈[-0.67,-0.31]、y∈[-0.27,0.20]、z∈[0.612,0.95] m，未计时点间平移门槛 20 mm，工具向下偏角 12°。这是本原型配置的检查范围，不能解释成认证工艺或安全规范，也不能从未计时动作计算执行速度。

前三个打磨组、hold 组和重复组的全部 20 点超出 x 工作范围。黑图组虽在此宽工作范围内，工具向下偏角约 66.8–68.1°；其他组约 39.1–42.4°。所有组首步平移超 20 mm。候选点距测得参考路径在 0.318–0.350 m 范围，未到达处理表面。

IK 只是将候选左 EEF **假定**为 FR5 球磨头 TCP 后的离线可达性检查。某些点能求出关节解不能修复语义、视角、坐标和工艺缺口；seed29 的 13 个点不能在当前严格误差阈值内求解，也不能据此证明全局数学不可达。没有动态执行、连续碰撞认证、去毛刺/去除量或实物验证。VLA 任务成功值为 null。

## 重现

从 `/home/hts/demo1` 运行，选择新的输出目录保留已有证据。`prepare` 会拒绝覆盖既有输入。

```bash
.venv/bin/python daiyi_grinding/vla_experiment.py prepare \
  --output daiyi_grinding/results/vla/reproduce --scene-seed 7 --case mixed
CUDA_VISIBLE_DEVICES=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  hy_vla_repro/.venv/bin/python -u daiyi_grinding/vla_experiment.py infer \
  --output daiyi_grinding/results/vla/reproduce --seeds 17 29 43
.venv/bin/python daiyi_grinding/vla_experiment.py audit \
  --output daiyi_grinding/results/vla/reproduce
```

首次结果目录为 `daiyi_grinding/results/vla/shadow_v1/`：`input_manifest.json`、`input.npz`、三视角 PNG、`baseline.json`、`reference_points.npz`、六组 NPZ、`inference.json`、`audit.json`、两份进程日志和 `provenance.json`。`audit.json` 包含每步的关节解/失败原因和原始诊断值，可直接审查，不需要相信摘要。

下一步若继续使用发布 VLA，应先建立真实单臂输入与 TCP/坐标/相机映射，再收集仿真的检查与工具运动示范进行针对性适配、独立场景验证和重新观测的闭环评测。本次几何轨迹主流程与 shadow VLA 分开，不能把几何基线的成功归为 Hy 的成功。
