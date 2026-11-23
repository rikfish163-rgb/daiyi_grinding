# MuJoCo 图像驱动的 FR5 眼在手外标定

这是纯仿真实验，没有机器人连接、控制柜通信或真机运动。固定 inspection 相机观察安装在 FR5 TCP 上的棋盘。标定场景只保留官方 FR5 关节链、相机和棋盘，避免工件/机器人 mesh 遮挡；主去毛刺场景另行执行动力学。FR5 关节原点、转轴及范围直接复用 `scene.JOINTS`，末端 site 与 `daiyi_grinding/sim.py` 的 TCP 相同，位于 wrist3 局部 z=0.166 m。各次姿态通过 IK 生成、通过 MuJoCo FK 记录，使用理想且无误差的关节测量。

## 复现

在 `/home/hts/demo1` 运行：

```bash
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.calibration \
  --output daiyi_grinding/results/calibration --seed 7 --poses 30
.venv/bin/python -m unittest daiyi_grinding.calibration_test -v
```

依赖为 MuJoCo 3.14、OpenCV 4.14、NumPy、SciPy。成功运行输出 `acceptance_passed: true`，不达标返回退出码 2。每张成功捕获的 RGB 图保存在 `daiyi_grinding/results/calibration/images/`，成功检测另存 `_corners.png`。`observations.json` 包含关节角、FK、检测像素角点、PnP 姿态及 train/holdout 分组，`scene.xml` 保存独立场景，`calibration.json` 保存估计外参和评分。

## 实际图像流程与真值隔离

棋盘由 MuJoCo 几何体渲染，8×5 内角点、格长 20 mm。`findChessboardCornersSB` 从 RGB 图像检测角点，图中红色标记识别棋盘起始角，以解决检测器行/列顺序不确定性。PnP 与 LM refinement 只接收检测到的像素点、棋盘实体尺寸及已知内参；不接收渲染真值像素。训练姿态以 `calibrateHandEye(..., CALIB_HAND_EYE_PARK)` 求固定相机外参，同时估计未知的 TCP 到棋盘安装变换。每五张的一张保留为 holdout，不参与求解。

内参是配置已知值，**没有宣称完成内参估计**：768×576，垂直 FOV=26°，`fx=fy=0.5*576/tan(13°)`，主点为 `(383.5,287.5)`，畸变为零。FK 为理想值，虚拟棋盘尺寸与刚性安装精确，图像没有额外传感器噪声；本实验误差**不是实际工位精度**。真值相机外参和棋盘安装量只用于渲染与最终评分；不得用评分真值替换求解外参。真实硬件使用前仍需实测内参、棋盘格长、关节/FK误差、安装刚性、同步和重复性。

## 坐标约定

所有矩阵 `T_A_B` 把 B 系列向量变换到 A 系，`p_A = T_A_B @ [p_B,1]`；长度单位米。camera 是 OpenCV 光学系：+X 图像右、+Y 图像下、+Z 朝前。MuJoCo 相机为 +Y 朝上、-Z 朝前，因此相机旋转需乘 `diag(1,-1,-1)`。机器人基座与世界同旋转，`T_world_base` 平移为 `[0,0,0.72]`。

训练数据为 FK 的 `T_base_gripper` 与 PnP 的 `T_camera_target`。眼在手外约束为：

```text
inverse(T_base_gripper[i]) @ T_base_camera @ T_camera_target[i]
    = T_gripper_target  （所有姿态相同）
T_world_camera = T_world_base @ T_base_camera
```

因此传给 OpenCV `R_gripper2base/t_gripper2base` 参数位置的是 **inverse(T_base_gripper)**，返回值解释为 `T_base_camera`。这对应官方文档的眼在手外 AX=XB 方程。[OpenCV 官方标定文档](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html) 说明至少需要两次具有不平行旋转轴的运动。本实验成功姿态覆盖 roll 43.57°、pitch 40.22°、yaw 67.31° 的范围。MuJoCo 的 mocap 棋盘通过 `mocap_pos/mocap_quat` 设置刚体姿态，摄影机坐标采用官方相机约定。[MuJoCo XML 官方参考](https://mujoco.readthedocs.io/en/stable/XMLreference.html)

## seed=7 实测结果

| 项目 | 结果 |
|---|---:|
| 尝试 / 成功图像 | 30 / 27 |
| 求解 / 留出姿态 | 21 / 6 |
| 检测后 PnP 平均重投影 RMS | 0.03174 px |
| 相机平移真值误差 | 0.13946 mm |
| 相机旋转真值误差 | 0.01318° |
| 棋盘安装平移 / 旋转误差 | 0.04124 mm / 0.00987° |
| 留出姿态最差三维角点误差 | 0.11415 mm |
| 验收 | 通过 |

三维留出误差把 PnP 棋盘角点通过**估计的**相机外参变换到基座系，与独立 FK 加物理棋盘安装量得到的评分参考角点比较。阈值为相机平移 <2 mm、旋转 <0.3°、留出最差点 <2 mm，固定在代码中，未根据结果调整。

图 8 棋盘检测失败，图 16、26 红色标记未检出；这些图像均未用于求解。第一次验证误以为角点顺序只有 180° 翻转，导致棋盘安装旋转评分约 180°、留出最差误差 80.05 mm，即使相机误差很小仍判定失败。随后改为依据图中标记识别四个角起点，重新完整检测求解；首次失败摘要留在 `initial_failed_validation.json`。这也说明重投影误差不能单独证明坐标约定正确。四项 unittest 已通过：变换逆与光轴方向、基座/世界 0.72 m 差、精确方程求解、空图拒绝与错误相机位置拒绝（前两项同一个 test）。

## 主 demo 读取

此结果严格匹配最初 `generate(seed=7)` 的 inspection 相机：配置位置 `[-0.486997708801488,-0.120466868776730,1.13]`、`xyaxes="1 0 0 0 1 0"`、FOV 26°、分辨率 768×576。主 demo 将相机统一固定在此位置，工件 seed 改变只移动工件，因此可共用本外参。标定命令的 `--seed` 只改变采集姿态随机种子，相机工位保持上述固定配置。如果实际改变相机安装或成像配置，应重新标定。

```python
from daiyi_grinding.calibration import load_inspection_calibration
camera_id = sim.model.camera("inspection").id
K, T_world_camera = load_inspection_calibration(
    "daiyi_grinding/results/calibration/calibration.json",
    width=768, height=576,
    camera_position=sim.data.cam_xpos[camera_id],
)
```

读取器拒绝失败结果、分辨率不符及配置相机位置不符。相机旋转/FOV也必须保持上述配置；当前读取器只做位置与尺寸匹配，调用方负责配置旋转/FOV匹配。主流程的 `unproject` 所需正是 `T_world_camera`，不要传入 `T_base_camera`。当前估计相机位置 world z=1.13000706 m，而 base z=0.41000706 m。
