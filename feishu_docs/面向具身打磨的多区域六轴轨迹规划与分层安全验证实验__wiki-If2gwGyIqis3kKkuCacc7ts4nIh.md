# 面向具身打磨的多区域六轴轨迹规划与分层安全验证实验

## **1\. 实验目的**



本实验面向具身打磨系统的前期验证，建立“外部感知接口—几何理解—局部规划—六轴运动—执行反馈—复扫与补磨”的可审计数据链。物体检测和毛刺识别由外部模块承担，本项目保留接口，不将识别算法作为本任务的工作内容。根据网上公开资料查询，推测公司是采用的ABB机械臂，故使用ABB替换UR5e继续仿真。



本轮重点回答以下研发问题：



1. 噪声点云带来的接触曲线和刀轴抖动，能否在给定采样偏差约束内降低？

2. 同一路径的绕刀轴姿态变化，能否改善可达性、方向灵巧度和假定刚度模型下的变形预测？

3. 多处、大小不一且分布在器件不同侧面的加工区域，能否生成带进刀、加工、退刀和转场标签的统一序列？

4. 新序列能否通过关节限位、FK/Jacobian、离散碰撞以及 MoveIt 服务复核？

这些问题支持具身闭环的工程实现，但仿真中的接触、观测与材料去除模型仍需真实数据标定。本实验不以动画播放成功代替工业加工验收。



## **2\. 实验原理**



### **2\.1 从外部区域接口到局部几何**



早期工程通过 PCL 公开点云验证了滤波、RANSAC、ROI、MLS、高度场与三角网格流程，并使用 Noether 组织表面加工路径。本轮 T\-RO 核心适配使用现有三维圆柱器件的五个有序 ROI，生成带可控噪声的局部点云，采用邻域 PCA 估计法向。它不调用物体检测模型，也不声称输入来自真实工业扫描。



器件主体的径向尺度为 0\.30 m，中心高度约 0\.88 m。五个缺陷区域分布在顶部及两侧，长度约 65–130 mm、宽度约 35–85 mm、名义高度约 0\.2–0\.6 mm，使用固定随机种子生成。



### **2\.2 接触曲线与刀轴曲线平滑**



建立接触位置曲线 P 与刀轴点曲线 Q：



```Plain Text
Q(u) = P(u) + b × n(u)
S(u,v) = (1 − v) × P(u) + v × Q(u)
```



其中 n 为表面法向，b=50 mm。P、Q 使用三次 B 样条表示，以直纹面的薄板能量抑制路径和刀轴变化，并用偏差惩罚保持对原始数据的贴合。本实现检查的是指定采样参数处的偏差，不是连续区间误差的数学上界。



### **2\.3 工具位姿、六轴逆解与姿态候选**



通用的工具目标关系为：



```Plain Text
p_tool0 = p_contact − R_tool0 × r_tool0_to_contact
```



本轮论文适配使用虚拟工具接触偏置 `(0, −300, 300) mm`，由配置统一提供给 FK、Jacobian、路径和虚拟工具模型。该工具用于研究姿态选择，不是现有直主轴砂轮的真实 TCP。



### **2\.4 多区域序列与分层碰撞验证**



对五区域访问顺序及方向穷举，共 `5! × 2^5 = 3,840` 种组合；目标函数使用直接关节五次转场时间代理，随后用关节 RRT \+ TCP 代理连接转场。该顺序是在代理代价下选出的候选，不是考虑全部工艺约束后的全局最优节拍证明。



本轮进一步对输出的同一条完整序列进行了两个层次的检查：



1. 隔离 Bullet 场景：虚拟工具、活动机械臂、工件网格、夹具和工作台；按最大关节间隔 0\.008 rad 加密，记录各阶段及失败原因。

2. 隔离 ROS 域 87 的 MoveIt：调用 `GetParameters`、`GetPositionIK`、`GetCartesianPath`、`GetStateValidity` 和 `ApplyPlanningScene`，检查每个阶段的 IK、路径完整率、状态与中点。





### **2\.5 时间化与连续进给候选**



已通过完整序列验证的轨迹使用停点五次插值：每个原始节点处速度归零，逐段限制关节速度、加速度和 jerk。单调五次时间律与原关节直线具有相同几何像，因此碰撞采样按关节距离控制，无需把所有导数采样点再次重复展开成几何路径。





## **3\. 实验环境**



|项目|本轮配置或状态|
|---|---|
|主机|Windows；工程位于 `C:/Users/15540/Documents/Codex/2026-09-26/new-chat`|
|容器|Docker Desktop / Docker Engine；使用已有 `robot_grinding_physics`、`robot_grinding_ros2`|
|ROS|ROS 2 Humble|
|机器人模型|ABB IRB6700\-200/2\.6 URDF；不是本轮 UR5e 验收|
|物理原型|PyBullet；估计质量、惯量、接触和经验去除参数|
|运动服务|MoveIt 2，禁用真实轨迹执行|
|可视化|RViz 2，经 Windows VcXsrv 显示|
|当前可见仿真域|86|
|新序列 MoveIt 验证域|87，审计完成后清理自身节点|
|假控制器计划域|88；实际验收尚未完成|
|数值依赖|物理容器 NumPy 1\.26\.4、SciPy 1\.15\.3|
|关节速度上限|0\.3 rad/s|
|关节加速度上限|0\.6 rad/s²|
|段内 jerk 上限|3 rad/s³|
|连续进给候选目标|6 mm/s|
|T\-RO 法向进退刀距离|50 mm|
|Bullet 环境裕量|1 mm；虚拟刀尖指定接触规则另列|
|输入噪声|高斯 σ=0\.05 mm；固定种子 670019/670020/670021|



15 N 是物理原型及方法适配使用的目标/假设载荷，不是企业现场标定工艺参数。



## **4\. 实验内容**



### **4\.1 点云、法向与路径平滑**



生成五区域 × 三种噪声种子的 15 组几何数据。每个 ROI 使用三维采样，依据邻域法向建立 P/Q 曲线，比较独立拟合基线与能量平滑结果，记录偏差、能量及导出点云的来源。



### **4\.2 六轴姿态候选及运动学核查**



每区域首个噪声种子进行 12 姿态候选搜索，共 60 候选。记录 IK 残差、关节限位、相邻增量、MVR、预测变形与拒绝原因。独立 FK/Jacobian 通过有限差分验证，同时加入不可达目标、变形超限和空候选集负例。



### **4\.3 完整多区域序列验证**



固定序列：`R3（反向）→ R4（反向）→ R1 → R2 → R5`。



五个区域各包含进刀、加工、退刀，区域之间加入四个转场，共 19 个阶段。检查阶段覆盖是否完整且不重复，绑定同一 `trajectory_sha256`，在 Bullet 与 MoveIt 中分别复核。



### **4\.4 连续加工进给试验**



保留原停点轨迹，另生成 5 条连续加工候选，每区域按 1,001 个时间点计算关节导数、TCP 速度和相对原始 TCP 折线的偏离。

### **4\.5 交接门禁与回执更新**



新增 T\-RO 序列契约适配，直接沿用源序列哈希，并验证仿真开关、时间递增、六关节数据、阶段覆盖及碰撞报告哈希。缺少通过的 MoveIt 证据时默认拒绝交接。



复用保守回执 Ledger，测试：取消段不计完成、未执行段不更新、匹配且已确认的加工段只更新一次、重复回执不重复扣减。测试回执是构造用例，不是实际 GenericSystem 执行证据。



### **4\.6 识别接口与职责边界**



沿用外部 `burr-defect/v1` 接口：点云坐标系、单位、时间、缺陷 ID/类型、置信度、中心、法向、尺寸、高度和建议加工区域。





## **5\. 实验结果**



### **5\.1 仿真展示**

1\.原速

[20261019\-1635\-06\.9701995\.mp4](assets/面向具身打磨的多区域六轴轨迹规划与分层安全验证实验__wiki-If2gwGyIqis3kKkuCacc7ts4nIh/图片和附件/20261019-1635-06.9701995.mp4)

2\.三倍速

[20261019\-1621\-48\.0030611\.mp4](assets/面向具身打磨的多区域六轴轨迹规划与分层安全验证实验__wiki-If2gwGyIqis3kKkuCacc7ts4nIh/图片和附件/20261019-1621-48.0030611.mp4)





当前可见 RViz 的主体是三维圆柱类器件，五处不规则缺陷分布于顶部和两侧。点云、过程颜色及复扫点分别用于显示工件采样、经验去除模型状态和虚拟观测。





|RViz / 状态字段|含义与边界|
|---|---|
|机器人模型与关节运动|域 86 物理关节状态；不是 ABB 控制器反馈|
|点云与过程颜色|合成器件采样及模型余量；不代表真实测量精度|
|接触力箭头|PyBullet 引擎法向接触反力；没有接触时不显示|
|phase|当前流程阶段；`review` 表示加工结束、等待质量复核|
|progress\_percent|演示流程进度；100% 不等于质量合格或门禁全部通过|
|completed\_regions|演示中已完成区域的记录；不能代替新路径控制器回执|
|real\_time\_factor|仿真时间/墙钟时间；低于 1 表示慢于实时|



### **5\.2 本轮保存报告中的验证结果**



|指标|结果|证据|
|---|---|---|
|几何试验组合|5 ROI × 3 噪声种子 = 15 组|`report.json`|
|薄板能量下降|约 4\.13%–63\.60%|`report.json`；相对本实现独立拟合基线|
|最大位置采样偏差|约 0\.1312 mm|`report.json`；不等于连续误差上界|
|FK/Jacobian 有限差分误差|最大约 3\.55×10⁻¹⁰|`numerical_verification.json`|
|Bullet 加密检查|770 次采样检查；19/19 阶段通过|`full_sequence_validation.json`|
|全序列最小关节限位余量|约 0\.2334 rad|同上；包含转场，不与早期局部路径直接横比|
|MoveIt IK 请求|179/179 成功|`moveit_validation.json`；含阶段共享端点|
|MoveIt 笛卡尔路径完整率|19 个阶段均为 100%|同上；MoveIt 输出路径并非直接替换原序列|
|GenericSystem 新序列实际执行|尚未完成；自动审批拦截启动|不能将脚本存在写成验收通过|



770 次碰撞采样、179 次 IK、339 次状态检查不是独立实验次数，也不构成统计重复试验。不同报告只在哈希、模型和运行来源匹配时共同使用。



### **5\.3 连续加工进给候选结果**



各区域目标平台进给为 6 mm/s，采样关节速度、加速度与 jerk 均低于本轮门限：



|区域|FK 计算的平台平均速度（mm/s）|平台峰峰波动 / 目标速度|相对源 TCP 折线最大距离（mm）|
|---|---|---|---|
|R3|6\.0154|1\.7388%|0\.3582|
|R4|6\.0141|1\.2728%|0\.3525|
|R1|6\.0105|0\.9783%|0\.3353|
|R2|6\.0079|0\.6233%|0\.3124|
|R5|6\.0094|1\.3720%|0\.3177|



最大采样关节速度约 0\.02283 rad/s，最大采样加速度约 0\.07353 rad/s²，最大采样 jerk 约 0\.68453 rad/s³。进给改进消除了原始加工节点的逐点停车，但引入约 0\.31–0\.36 mm 的曲线偏离，必须依据实际打磨允许误差、接触几何和复检要求评估，不能只看速度变平稳。这些速度由模型求得，未由控制器执行反馈测得；不证明真实 TCP 恒速或稳定恒力。





### **5\.4 结果结论**



本轮取得了新的、与具体轨迹哈希绑定的完整多区域离散碰撞及 MoveIt 服务证据，并验证了交接与回执逻辑。连续进给候选显示模型计算速度明显接近目标平台值，但仍需重新验证几何误差、碰撞、阶段衔接和假控制器执行。



## **6\. 技术提升与开源框架**



|编号|主要进步|本轮验收与剩余边界|
|---|---|---|
|ROS 服务与导入|正确使用 `rcl_interfaces.srv.GetParameters`，已在 ROS 容器中实际导入并调用|新序列模型参数读取通过|
|工具几何与变换|论文适配的 FK、Jacobian、工具模型与导出共用偏置；验证支持读取冻结配置|虚拟工具与当前物理砂轮仍是不同研究对象，实机 TCP 尚未统一|
|碰撞场景|从局部加工姿态扩展到五区域完整 19 阶段，加入虚拟工具自碰撞范围、接触过渡与环境裕量|全序列离散检查通过；辅助平衡连杆、真实护罩、连续扫掠体未完整覆盖|
|IK / 笛卡尔路径 / 状态服务|对新序列实际调用 MoveIt，不再只引用旧路径报告|179 IK、339 状态、160 中点，19 阶段 fraction=1；全部通过|
|加密 / 限位 / FK / Jacobian|数学负例、有限差分及关节采样与运行哈希绑定；全序列报告输出阈值和覆盖范围|数值与当前离散范围通过；无连续碰撞或真机认证结论|
|工艺阶段与时间化|已验证停点序列保留四阶段；新增平滑启停、连续加工候选与目标/FK速度追踪|候选速度波动约 0\.62%–1\.74%；尚未证明控制器实际恒速及真实力控|
|轨迹交接门禁|新序列沿用唯一源哈希，校验阶段覆盖和匹配证据，默认要求 MoveIt 通过|新适配测试通过；旧完整 bundle 仍有缺失证据，不能冒充兼容旧控制器入口|
|GenericSystem|准备域 88 的纯假硬件执行脚本，包含反馈、首次进刀取消与续作验收逻辑|自动审批拦截实际启动；尚未取得执行证明|
|已执行段和失败原因|将新序列接入保守 Ledger，取消/未执行不扣减、重复回执幂等|逻辑测试通过；构造回执不等于实际反馈，待与 B8 实际结果绑定|







## **7\. 思考问题**



1. 离散候选搜索与代理顺序优化能否满足更复杂凹面、内腔和遮挡工件？目前只验证了五区域圆柱类器件。

2. 连续进给降低停车次数，同时改变几何路径；应如何共同优化速度波动、曲面偏差和接触法向误差，而不是只优化一个指标？

3. 虚拟刀尖接触规则能否迁移到真实砂轮？不能直接迁移。真实砂轮需要轮缘/端面工艺、护罩、磨损、TCP 和工具标定共同约束。

## **8\. 经验分享**



**正面经验：** 把运行 ID、轨迹哈希、模型、坐标系、工具配置和报告绑定，能避免重规划后沿用旧证据。局部通过与整条任务通过必须分开；将正例、负例、失败报告和中止日志一起保存，比只留成功动画更有研发价值。



**反面经验：** 文件存在、说明文档声称支持、动画可播放，都不能代替本轮验收。曾出现历史配置缺字段、ACM 覆盖装配排除、TF 不一致和旧交付缺证据，这些都是实际运行才发现的问题。系统 Python、工作区 Python 和容器 Python 的依赖不同，也应明确标注入口环境。



**性能经验：** 将时间导数采样与几何碰撞采样分开；碰撞采样由关节距离和模型几何控制，不应因高频导数验证重复扩增相同路径。对自建验证任务设置时间预算，避免长期占用资源影响 RViz。



**下一步顺序：** 先完成新序列 GenericSystem 反馈、取消、续作与回执验收；再把连续进给候选经过自身碰撞和 MoveIt 验证后接入控制器；随后统一真实工具几何，补齐夹具及辅助连杆；最后进行外部观测误差与工艺辨识试验。真机前仍需厂商模型、TCP/手眼标定、安全回路、急停、低速空跑与企业现场验收。



## **9\. 参考资料**

### 9\.1 方法来源



\[1\] WANG Gang, LI Wenlong, JIANG Cheng, ZHU Dahu, LI Zhongwei, XU Wei, ZHAO Huan, DING Han\. Trajectory Planning and Optimization for Robotic Machining Based On Measured Point Cloud\[J\]\. IEEE Transactions on Robotics, 2022, 38\(3\): 1621–1637\. DOI: [10\.1109/TRO\.2021\.3108506](https://doi.org/10.1109/TRO.2021.3108506)\. [出版页面](https://ieeexplore.ieee.org/document/9642395)。正式卷期年为 2022，在线发表年为 2021。本文依据公式独立实现方法变体，未取得作者官方代码和完整原始实验数据；本轮不是论文实物结果的逐数值复现。



### 9\.2 本轮采用的软件、模型与运行工具



\[2\] ROS 2 contributors\. ROS 2 Humble Documentation\[EB/OL\]\. [官方文档](https://docs.ros.org/en/humble/)。用于 ROS 通信、节点、消息和 TF；发行版为 Humble，未冻结全部二进制包版本。



\[3\] ROS 2 contributors\. rcl\_interfaces\[CP/OL\]\. [官方仓库](https://github.com/ros2/rcl_interfaces)。本轮 `GetParameters` 的服务类型来自 `rcl_interfaces.srv`，不是 `std_srvs` 或 `moveit_msgs`。



\[4\] MoveIt contributors\. MoveIt 2; moveit\_msgs\[CP/OL\]\. [MoveIt 2 仓库](https://github.com/moveit/moveit2)；[Humble 文档](https://moveit.picknik.ai/humble/index.html)；[moveit\_msgs 仓库](https://github.com/moveit/moveit_msgs)。用于 PlanningScene、IK、笛卡尔路径和状态有效性服务。本轮服务请求可见 `audit_moveit.py`；未将其表述为 ABB 控制器或真机执行。



\[5\] ROS 2 RViz contributors\. RViz\[CP/OL\]\. [官方仓库](https://github.com/ros2/rviz)。用于机器人、点云、轨迹和 Marker 显示；可视化本身不是物理仿真或安全验收。



\[6\] robot\_state\_publisher maintainers\. robot\_state\_publisher\[CP/OL\]\. [官方仓库](https://github.com/ros/robot_state_publisher)。依据 URDF 和关节状态发布机器人连杆 TF；与 RViz 的显示职责分开。



\[7\] RIPPERGER Michael, ARMSTRONG Levi; ROS\-Industrial community\. abb: abb\_irb6700\_support, version 1\.5\.0\[CP/OL\]\. [固定提交下的模型包](https://github.com/ros-industrial/abb/tree/eefdf01c956a6f2129aacb88d598201feff3cf19/abb_irb6700_support)；[ROS Index](https://index.ros.org/p/abb_irb6700_support/)。本地来源记录提交为`eefdf01c956a6f2129aacb88d598201feff3cf19`，型号为 `irb6700_200_260`，许可证 Apache\-2\.0。原包是 ROS 1/Catkin 支持包，本项目复用几何与 URDF 并作 ROS 2 加载适配；这不意味着采用了 ABB ROS 2 真机驱动。物理原型另对惯性等作估计修改，不等于厂商动力学标定模型。



\[8\] NumPy developers\. NumPy, version 1\.26\.4\[CP/OL\]\. [官方仓库](https://github.com/numpy/numpy)；[1\.26 系列文档](https://numpy.org/doc/1.26/)。用于数组、线性代数及数值计算。1\.26\.4 为本地物理镜像构建配方固定版本，本次未重新读取历史容器环境。



\[9\] SciPy developers\. SciPy, version 1\.15\.3\[CP/OL\]\. [官方仓库](https://github.com/scipy/scipy)；[1\.15\.3 文档](https://docs.scipy.org/doc/scipy-1.15.3/)。用于样条、邻域搜索、旋转和数值方法；连续进给候选直接使用 `scipy.interpolate.CubicSpline`。版本依据同上。



\[10\] Bullet Physics contributors\. Bullet Physics SDK / PyBullet\[CP/OL\]\. [官方仓库](https://github.com/bulletphysics/bullet3)。本地物理镜像构建配方固定 Python 包 `pybullet==3.2.7`。用于刚体、接触反力原型及独立碰撞查询；本项目的经验材料去除模型、假设刚度和工艺参数并非 Bullet 提供的工业打磨标定模型。



\[11\] PyYAML maintainers\. PyYAML, version 6\.0\.2\[CP/OL\]\. [官方仓库](https://github.com/yaml/pyyaml)；[官方文档](https://pyyaml.org/wiki/PyYAMLDocumentation)。用于 YAML 配置解析；固定版本来自物理镜像构建配方。



\[12\] Open Source Robotics Foundation\. Docker Images\[CP/OL\]\. [官方仓库](https://github.com/osrf/docker_images)。本地物理镜像基于 `osrf/ros:humble-desktop-full` 构建；该标签未固定到不可变镜像摘要，不能仅凭标签还原历史镜像内容。



\[13\] Docker; Moby contributors\. Docker Engine / Moby\[CP/OL\]\. [Engine 文档](https://docs.docker.com/engine/)；[Moby 仓库](https://github.com/moby/moby)。用于 Linux 容器运行。Windows 上的 Docker Desktop 是另行分发的桌面产品，不能把 Docker Desktop 整体写成开源算法框架。



\[14\] VcXsrv maintainers\. VcXsrv Windows X Server\[CP/OL\]\. [项目主页](https://sourceforge.net/projects/vcxsrv/)。用于 Windows 显示容器中的 RViz 窗口；未冻结本轮安装程序版本。



### 9\.3 已准备、尚未取得本轮完整执行证据的组件



\[15\] ros\-controls contributors\. ros2\_control: Mock Components / Generic System\[EB/OL\]\. [Humble 官方文档](https://control.ros.org/humble/doc/ros2_control/hardware_interface/doc/mock_components_userdoc.html)。准备脚本使用 `mock_components/GenericSystem`。其基本机制是理想化命令到状态的映射，用于离线接口测试，不代表 ABB 伺服动力学。本文所记新序列的实际执行验收尚未完成。



\[16\] ros\-controls contributors\. ros2\_controllers: Joint Trajectory Controller\[EB/OL\]\. [Humble 官方文档](https://control.ros.org/humble/doc/ros2_controllers/joint_trajectory_controller/doc/userdoc.html)。准备脚本配置轨迹控制器与关节状态广播器；反馈、取消及恢复是否完成仍应以该轨迹的实际执行报告为准。



### 9\.4 早期流程采用或借鉴的项目，不作为本轮算法调用证据



\[17\] ROS\-Industrial contributors\. Noether\[CP/OL\]\. [核心库仓库](https://github.com/ros-industrial/noether)。工业表面工具路径规划库，属于早期表面路径流程；本轮五 ROI 的双曲线平滑及顺序规划由本项目代码完成。



\[18\] ROS\-Industrial contributors\. noether\_ros2\[CP/OL\]\. [ROS 2 接口仓库](https://github.com/ros-industrial/noether_ros2)。应与 Noether 核心库分别列出。早期 `export_noether.py` 导入 `noether_ros.srv.PlanToolPath` 并调用 `/plan_tool_path`；仓库名和其中 ROS 包名不同，不应据此误判为其他项目。



\[19\] ROS\-Industrial Consortium\. Scan\-N\-Plan Workshop\[CP/OL\]\. [官方仓库](https://github.com/ros-industrial-consortium/scan_n_plan_workshop)；[工作坊文档](https://ros-industrial-consortium.github.io/scan_n_plan_workshop/)。用于早期扫描—表面路径—运动规划的系统工作流参考；不是本轮 EBTSO 独立实现的作者代码。



\[20\] ROS\-Industrial Consortium\. snp\_automate\_2023\[CP/OL\]\. [官方仓库](https://github.com/ros-industrial-consortium/snp_automate_2023)。工业扫描和表面处理示例，原示例采用 Motoman HC10 相关配置；不是直接适配本项目 ABB 的完整仿真包。



\[21\] Point Cloud Library contributors\. PCL Data: table\_scene\_lms400\.pcd\[DS/OL\]\. [原始数据文件](https://github.com/PointCloudLibrary/data/blob/master/tutorials/table_scene_lms400.pcd)。仅作为早期公开扫描点云来源；本轮五 ROI 数据为合成数据，不来自此 PCD。数据许可说明已留存于 \[scan\_n\_plan\_precontact/data/PCL\_DATA\_LICENSE\.txt\]\(C:/Users/15540/Documents/Codex/2026\-09\-26/new\-chat/scan\_n\_plan\_precontact/data/PCL\_DATA\_LICENSE\.txt\)。引用 PCL 数据不等于本项目所有滤波、MLS 和法向计算直接调用了 PCL C\+\+ 库。



### 9\.5 后续研究参考，不属于本轮已完成能力



\[22\] HUTCHINSON Seth, HAGER Gregory D, CORKE Peter I\. A Tutorial on Visual Servo Control\[J\]\. IEEE Transactions on Robotics and Automation, 1996, 12\(5\): 651–670\. DOI: [10\.1109/70\.538972](https://doi.org/10.1109/70.538972)。用于视觉反馈与机器人运动控制的理论背景；不作为本轮已实现视觉伺服的证据。



\[23\] MoveIt contributors\. MoveIt Servo\[EB/OL\]\. [Humble 官方教程](https://moveit.picknik.ai/humble/doc/examples/realtime_servo/realtime_servo_tutorial.html)。后续实时笛卡尔伺服接口参考；本轮未据此完成真实视觉/力反馈闭环。



### 9\.6 内部代码与实验资料

[V4\.0 预接触验证版两人协同开发与合并方案](V4.0_预接触验证版两人协同开发与合并方案__wiki-TtYJwkyMkiW11WkZONncpCianQg.md)

