# 戴屹铸件批缝与局部毛刺视觉打磨原型

RGB-D 观测 → 自动检测和选区 → 轨迹与工具姿态 → 图像标定外参 → FR5 MuJoCo 动力学执行。默认自动运行，保留人工矩形/多边形 ROI 和候选区域选择。首版是固定工位、可见上表面、机器人持小工具的研究原型。

## 查看交付

- `deliverables/戴屹视觉打磨仿真验证与FR5计划.pdf`：面向甲方的实测简报。
- `deliverables/戴屹视觉打磨演示包.zip`：离线视频、简报、指标和真机计划。
- `results/demo_v3/demo.mp4`：自动生成路径后的完整空跑录像。
- `docs/research.md`：需求、公司与行业调研、VLA 路线和结论边界。
- `docs/experiment_journal.md`：保留成功、失败、修复与实测证据。
- `docs/fr5_real_robot_plan.md`：真实工件测量到 FR5 初步试磨的阶段门槛。

## 交互运行

在 `/home/hts/demo1` 执行：

```bash
.venv/bin/python -m daiyi_grinding.serve --port 8848
```

打开 `http://127.0.0.1:8848`。初始展示保存的实测记录；重新拍照/规划及仿真执行会创建新的独立运行。可更换工件种子和场景，也可在原始相机图上绘制 ROI。ROI 先沿用完整几何分类，再限制候选区域；最终路径按球半径 3 mm 加 1 mm 实验余量检查边界。选区太小时可能没有可执行路径。

## 复现实验

已有 MuJoCo 环境位于父目录 `.venv`，依赖见 `requirements.txt`。本包复用父目录 `scene.py` 中的 FR5 关节参数、`fetch_assets.py` 和官方 FR5 mesh 资产。新机器需要保留这些文件及资产来源，安装支持 EGL 的图形驱动。VLA 使用另一个现有模型环境，不属于基础演示依赖。

```bash
# 输出目录必须为空；以下新名字避免覆盖交付记录。
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.run \
  --seed 7 --case mixed --output daiyi_grinding/results/demo_new --video

# 7 类 × 6 新场景 × 2 方法，只有感知与路径投影评分，不是84次真机加工。
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.benchmark \
  --seeds 6 --seed-start 3007 --output daiyi_grinding/results/benchmark_new

# 人工选区：JSON为像素坐标多边形，例如 [[90,105],[660,105],[660,180],[90,180]]。
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.run \
  --roi /absolute/path/roi.json --output daiyi_grinding/results/manual_new

# 标定、回归测试、接触敏感性试验。
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.calibration --output daiyi_grinding/results/calibration_new
.venv/bin/python -m unittest daiyi_grinding.test_roi daiyi_grinding.calibration_test -v
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.contact_probe --output daiyi_grinding/results/contact_new

# 重建PDF和可离线分享的演示包。
.venv/bin/python -m daiyi_grinding.build_delivery
```

当前主链从 `results/calibration/calibration.json` 读取独立图像求出的外参；上面的 calibration_new 用于复现比对，不会自动替换已验收参数。

## 输出与坐标约定

`observation.npz` 是检测器的观测边界，只含 RGB、深度、K、T_world_camera。缺陷生成信息只供渲染和事后评价，未传入检测或规划。深度是理想 MuJoCo 针孔渲染再叠加显式高斯噪声和缺失，不等于某款真实工业相机的光学模型。当前算法主要使用深度几何；未声称训练了 RGB 分割网络。

`paths.json` 使用世界坐标米、3×3 工具旋转矩阵，包含进退刀、类型和实验工艺元数据。`fr5_path_preview.csv` 转为机器人基座坐标毫米与 XYZ 欧拉角度，仅供接口审阅；它不是可直接上线的控制器程序。`path_contract.json` 记录完整单位、坐标和工具约定。机器人基座在 MuJoCo 世界坐标 z=0.72m，磨头 TCP 为名义 6mm 球底端。

执行用 IK 产生路径并连续推进 `mj_step`，不是逐帧改 qpos 的动画。控制为模型重力和期望速度前馈的位置伺服，使用简化惯性及连杆碰撞代理；没有复制 FAIRINO 实际控制器。CSV 使用积分后 FK 与同一时刻目标，接触每2ms统计、位置与姿态误差每10ms采样。默认3mm空跑要求接触积分步数为0。

## 当前不能据此认定的能力

材料去除、恒力磨削、表面粗糙度、磨损/热效应、真实反光金属测量、多面深腔和生产安全验收均未完成。薄批缝是已知漏检边界。形状规则依赖当前试样族，正常结构语义需用真实工件、质量知识和独立测试继续确认。

Hy-VLA 已做真实预训练权重影子推理，候选因动作/视图/坐标/工具适配缺口被拒绝，执行数0；不将几何基线成果计为 VLA 成功。详见 `docs/vla_experiment.md`。工程中没有连接真实机器人或启动真实主轴的代码。
