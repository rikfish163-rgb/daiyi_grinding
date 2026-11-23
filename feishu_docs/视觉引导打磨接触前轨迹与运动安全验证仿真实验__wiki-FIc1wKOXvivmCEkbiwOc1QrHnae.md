# 视觉引导打磨接触前轨迹与运动安全验证仿真实验

## 1\. 实验目的

本实验面向具身打磨系统的前期验证：建立“感知—理解—规划—执行—反馈”的闭环接口，使机器人能够依据工件点云和缺陷信息生成加工策略，并将执行状态反馈给后续复扫和补磨模块。在控制器接入和真机调试前，验证视觉/点云生成的打磨轨迹是否满足六轴机器人基本运动约束，重点检查：



1. URDF 关节限位与独立 FK 的数值一致性；

2. Jacobian 奇异姿态筛查；

3. 路径端点、相邻状态和加密插值状态的碰撞；

4. 五次时间参数化的速度、加速度和 jerk 约束；

5. 真机部署所缺少的机器人、工具、标定、夹具和工艺资料。

    

同时整理点云输入、表面重建、法向估计、砂轮偏置和轨迹规划的技术提升，为后续毛刺识别接口和控制器接入保留边界清晰的数据契约。



## 2\. 实验原理



系统将扫描点云或工件表面转换为局部加工表面，经过点云区域提取、MLS 局部拟合、平滑高度场、法向估计和三角网格重建，使用 Noether 组织往复路径，再由 MoveIt 完成 UR5e 笛卡尔路径和六轴逆解。



工具目标位姿考虑砂轮轮缘到 `tool0` 的偏置：



`tool0位置 = 表面点 + 8 mm × 表面法向 − 工具旋转矩阵 × 轮缘局部偏置`



当前轮缘局部偏置为 `(0, 55, 150) mm`。在规划结果上增加关节加密采样、独立 URDF FK、Jacobian 筛查、关节限位检查和碰撞检查；随后采用 stop\-at\-knot 五次时间参数化检查速度、加速度与 jerk。`preflight` 根据证据门禁区分“仿真通过”和“具备真机调试资格”。



## 3\. 实验环境



|项目|配置|
|---|---|
|主机|Windows|
|容器环境|Docker Desktop / Docker Engine|
|ROS|ROS 2 Humble|
|机器人模型|UR5e 六轴、URDF|
|运动规划|MoveIt 2|
|可视化|RViz 2；通过 VcXsrv 显示|
|ROS 域|77|
|速度上限|≤ 0\.3 rad/s|
|加速度上限|≤ 0\.6 rad/s²|
|段内 jerk 上限|≤ 3 rad/s³|
|名义间隙|8 mm，属于预接触仿真间隙|
|执行状态|关节状态回放，不连接真实机器人|
|算法闭环|闭环算法可不依赖 RViz|



本轮未重新检测 CPU、GPU、内存、Docker 镜像摘要和精确软件包版本，因此不对这些信息推测填写。当前没有连接真实机械臂、相机或力传感器。



## 4\. 实验内容



### 4\.1 点云与表面建模



接入 PCL 官方公开的 `table_scene_lms400.pcd`，完成滤波、RANSAC 平面/区域提取和 ROI 选择；再通过 MLS 拟合、平滑高度场、梯度法向和闭合三角网格生成可规划的局部表面。



### 4\.2 工具几何与路径规划



使用 Noether 生成往复/栅格路径，将表面点、法向、砂轮轮缘偏置和名义间隙转换为 `tool0` 目标位姿，再调用 MoveIt 进行笛卡尔路径和 UR5e 六轴逆解。



### 4\.3 运动安全验证



1. 检查 URDF 关节限位；

2. 使用独立 FK 复核规划位姿；

3. 计算 Jacobian 奇异值并筛查不利姿态；

4. 对相邻状态进行中点和自适应关节加密碰撞检查；

5. 对轨迹进行 stop\-at\-knot 五次时间参数化；

6. 使用 `preflight` 检查真机部署资料是否完整；

7. 通过网页和 RViz 保存可视化及报告结果。

    

### 4\.4 识别接口预留



预留 `burr-defect/v1` 契约，字段包括点云坐标系、采集时间、缺陷类型、置信度、中心、法向、尺寸、高度和建议加工段。当前未接入真实毛刺识别模型。



### 4\.5 复现实验入口



以下命令是复现入口，不表示本轮已经重新执行：



```PowerShell
Set-ExecutionPolicy -Scope Process Bypass
& 'E:\robot_grinding\scan_n_plan_precontact\start.ps1'
```



ROS 版本需要先启动 Docker Desktop 和 VcXsrv；不要在同一 ROS 域重复启动旧进程。复跑可能重新生成 JSON 和日志，应先备份原有结果。



## 5\. 实验结果

### 5\.1仿真展示

[屏幕录制 2026\-10\-11 160508\.mp4](assets/视觉引导打磨接触前轨迹与运动安全验证仿真实验__wiki-FIc1wKOXvivmCEkbiwOc1QrHnae/图片和附件/屏幕录制%202026-10-11%20160508.mp4)



这是 RViz 中的**打磨轨迹回放状态文字**，逐行含义如下：





### 5\.2 保存报告中的验证结果



|指标|结果|证据|
|---|---|---|
|MoveIt 笛卡尔路径完整率|100%|`validation.json`|
|轨迹关节样本|584|`validation.json`|
|相邻路径中点检查|583 个通过|`validation.json`|
|密集插值碰撞采样|1,905 个通过|`validation.json`|
|最小关节限位裕量|约 0\.612 rad|`kinematic_validation.json`|
|独立 FK 检查|通过|`kinematic_validation.json`|
|Jacobian 奇异姿态筛查|通过设定的仿真阈值|`kinematic_validation.json`|
|轮缘位置误差最大值|约 1\.267 mm|`validation.json`|
|轮缘法向误差最大值|约 0\.324°|`validation.json`|
|时间参数化约束|通过|`validation.json`|



另有历史记录：早期验证曾记录 836 状态、3,075 加密点、最小限位余量 1\.350 rad、时间化长度 458\.625 s；后续重规划记录为 584 状态、1,905 加密点、回放审计 6,413 状态、循环约 304\.8 s。两次记录不是同一次实验，不能混合计算。



### 5\.3 结果结论



指定仿真轨迹的软件运动学、离散碰撞和时间参数门禁通过；`real_machine_motion_enabled=false`，`ready_for_real_machine_commissioning=false`。因此，本实验证明了规划软件链路具备继续接入控制器的基础，但没有证明真实机械臂可以安全加工或能够去除毛刺。



## 6\. 技术提升与开源框架



|技术模块|主要提升|使用的开源框架/项目|本项目中的作用|
|---|---|---|---|
|点云输入|从手工示例扩展到公开扫描数据|PCL 数据仓库|提供 `table_scene_lms400.pcd`；数据来源保留 BSD 许可说明|
|点云处理|滤波、RANSAC、支持域和高度场计算|NumPy、SciPy|数值计算、邻域处理、平滑和后备实现；不是完整调用 PCL C\+\+ 算法库|
|表面路径|从固定点列扩展到往复/栅格路径|Noether|通过 `/plan_tool_path` 生成表面加工段|
|六轴规划|将表面位姿转换为 UR5e 关节轨迹|ROS 2 Humble、MoveIt 2|ROS 通信、逆解、笛卡尔路径和碰撞规划|
|机器人模型|统一关节、连杆和 TF|URDF、robot\_state\_publisher|关节限位、模型发布和 RViz 显示|
|可视化|显示点云、工件、砂轮、路径和机器人|RViz 2|轨迹回放和状态核查|
|运行环境|固定 ROS 和规划依赖|Docker Desktop / Docker Engine|Windows 主机运行 Linux ROS 仿真|



主要来源：



- ROS 2 Humble：https://docs\.ros\.org/en/humble/

- MoveIt 2：https://moveit\.picknik\.ai/

- Noether：https://github\.com/ros\-industrial/noether

- PCL 点云数据：https://github\.com/PointCloudLibrary/data/blob/master/tutorials/table\_scene\_lms400\.pcd

- NumPy：https://numpy\.org/

- SciPy：https://scipy\.org/

- Docker：https://www\.docker\.com/

    

## 7\. 思考问题



1. 停点五次插值不等于工业连续进给，段内导数有界也不自动保证节点间 jerk 连续。

2. 加密采样只能降低离散碰撞漏检，不能替代连续扫掠体安全验证。

3. 8 mm 间隙是预接触条件，不是砂轮真实接触、力控或材料去除模型。

4. 点云重建结果依赖扫描噪声、密度和遮挡，不能直接代表工件真实几何。

5. 当前实验没有重复统计，采样点数、反馈条数和窗口数量不等于独立实验次数。

    

## 8\. 经验分享



轨迹、验证报告、运行 ID、坐标单位和数据源必须绑定保存；重规划后不能沿用旧轨迹的样本数或验证结果。实验记录要明确区分“仿真软件通过”“假控制器通过”和“真机具备调试资格”。后续接入真机前，必须替换厂商机器人模型，重新完成 TCP/手眼标定、工装碰撞建模、速度限制、急停和低速空跑验证。



## 9\. 参考资料

1. [六轴视觉随形打磨：真机接触前验证报告](六轴视觉随形打磨：真机接触前验证报告__wiki-Qs5zwI3XgiQlr0kT9incdK4unge.md)

2. [六轴回放一致性升级](六轴回放一致性升级__wiki-KBTTwtkgriVkIjkpMvkc3YcXn6c.md)

3. [preflight\_report\.json](preflight_report.json__wiki-UfitwgrxciAGA2k9sDacPq3Tnqh.md)

4. [智能视觉引导打磨仿真技术提升对照表](智能视觉引导打磨仿真技术提升对照表__wiki-DEwPwhKrZiLHpskmnlocGXy4nHg.md)

### 开源框架与数据

5. [ROS 2 Humble Documentation](https://docs.ros.org/en/humble/)：机器人节点、消息、TF 和生命周期管理。

6. [MoveIt 2 Documentation](https://moveit.picknik.ai/)：运动学、笛卡尔路径、规划场景和碰撞检测。

7. [ROS\-Industrial Noether](https://github.com/ros-industrial/noether)：曲面切片和工业加工路径生成。

8. [RViz 2 Documentation](https://github.com/ros2/rviz)：机器人、点云、路径和规划场景可视化。

9. [Point Cloud Library Data Repository](https://github.com/PointCloudLibrary/data)：本实验使用的 `table_scene_lms400.pcd` 公开点云来源。

10. [NumPy](https://numpy.org/) 与 [SciPy](https://scipy.org/)：点云数值处理、邻域计算、平滑和高度场计算。

11. [Docker Engine](https://docs.docker.com/engine/)：固定 ROS 2、MoveIt 2 和 Noether 的运行环境。

12. https://github\.com/ros\-industrial\-consortium/scan\_n\_plan\_workshop :提供完整的工业机器人扫描、表面处理、工具路径规划和仿真工作流。

13. https://github\.com/ros\-industrial\-consortium/snp\_automate\_2023 ：Scan\-N\-Plan 的具体工业打磨示例，包含 HC10 六轴机械臂、扫描网格、工具路径、运动规划和 RViz 仿真。

### 具身机器人与视觉引导参考

14. Hutchinson, S\., Hager, G\. D\., \& Corke, P\. I\. [A Tutorial on Visual Servo Control](https://doi.org/10.1109/70.538972)\. \*IEEE Transactions on Robotics and Automation\*, 1996\.：视觉观测到机器人运动控制的基础理论。

15. Chaumette, F\. \& Hutchinson, S\. [Visual Servo Control\. I\. Basic Approaches](https://doi.org/10.1109/MRA.2006.250573)\. \*IEEE Robotics \& Automation Magazine\*, 2006\.：基于视觉误差的机器人运动控制方法。

16. Chaumette, F\. \& Hutchinson, S\. [Visual Servo Control\. II\. Advanced Approaches](https://doi.org/10.1109/MRA.2007.339609)\. \*IEEE Robotics \& Automation Magazine\*, 2007\.：视觉伺服的高级方法与约束处理。

17. [ROS\-Industrial](https://rosindustrial.org/)：工业机器人 ROS 驱动、工具链和部署生态参考。

18. [MoveIt Servo](https://moveit.picknik.ai/main/doc/examples/realtime_servo/realtime_servo_tutorial.html)：实时笛卡尔伺服和后续视觉/力反馈接入的实现参考。

上述资料用于说明技术来源和后续扩展方向；本实验当前只使用其中的 ROS 2、MoveIt 2、Noether、RViz 2、公开点云和数值计算工具，具身闭环相关内容仍属于接口设计和仿真规划阶段。

