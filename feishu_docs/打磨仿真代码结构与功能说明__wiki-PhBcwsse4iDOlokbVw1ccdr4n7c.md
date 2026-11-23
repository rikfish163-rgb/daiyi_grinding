# 打磨仿真代码结构与功能说明

# 点云打磨仿真代码结构与功能说明



核对日期：2026 年 11 月 4 日。本文用于项目组快速阅读代码、定位模块和交接维护，覆盖当前打磨仿真目录。



**当前系统根据已指定的加工区域生成表面候选路径，再求解机器人关节轨迹，经过运动学和碰撞检查后在 RViz 回放。自主缺陷识别尚未接入。**



项目绝对路径：



```Plain Text
C:\Users\15540\Documents\Codex\2026-09-26\new-chat\scan_n_plan_physics\estun_ier220_2700
```



下文文件名均相对于该目录。本说明按当前源码整理，没有重新运行整套仿真，不据此新增成功率或加工质量结论。



## 1 两层规划的区别



|层次|输入|输出|核心文件|
|---|---|---|---|
|工件表面路径规划|点云、目标区域掩码、扫描行间距|表面路径、法向、预接触参考点、候选转场线|surface\_candidate\_planner\.py|
|机械臂运动规划|表面候选路径、磨具、机器人及工位模型|关节轨迹、时间信息、通过和拒绝段、修正记录|plan\_robot\_path\.py|



工件上出现一条线，只说明几何候选已生成。是否能够回放，还取决于机械臂连续可达性、工具及机器人净空、进退刀、转场和返回安全位的检查结果。



## 2 一次任务如何流转



```Plain Text
网页选择工件或上传点云
  → workpiece_selector_server.py 接收请求
  → selector_request.json 保存任务和 request_id
  → workpiece_selection_node.py 读取准备后的点云与目标掩码
  → surface_candidate_planner.py 生成表面候选路径
  → tool_schedule.py 安排磨具和加工遍数
  → fixture_yaw_adaptation.py 筛选仿真装夹候选
  → surface_plans/<request_id>/plan.json
  → robot_path_node.py 发起机器人规划
  → plan_robot_path.py 组织完整运动检查
       ↔ robot_path_engine.py 运动学和碰撞
       ↔ automatic_path_repair.py 失败后有界修正
  → robot_plan.json / robot_plan_gate.json / automatic_repair_report.json
  → robot_path_node.py 核对结果及哈希，回放通过段
  → /joint_states → robot_state_publisher → RViz
  → 网页显示状态、修正过程及拒绝原因
```



装夹筛选只用于选择更有希望的仿真放置姿态。代表点或代表段通过不等于全路径通过，也不表示真实夹具已完成自动重新装夹。



## 3 主要文件结构



```Plain Text
estun_ier220_2700/
├─ start_collision_guarded.ps1    推荐启动入口，先检查再启动
├─ start_workcell.ps1             启动 ROS 节点和 RViz
├─ start_selector.ps1             启动网页服务
├─ workpiece_selector.html        工件选择、上传和状态页面
├─ workpiece_selector_server.py   网页接口与请求持久化
├─ upload_preflight.js            网页上传前检查
├─ upload_cloud.py                点云解析、目标选择及抽样
├─ build_workpiece_library.py     内置工件库准备
├─ workpiece_selection_node.py    任务组织和候选路径发布
├─ surface_candidate_planner.py   表面候选路径生成
├─ tool_schedule.py               工具与加工遍数安排
├─ fixture_pose_search.py         装夹候选及坐标变换
├─ fixture_yaw_adaptation.py       仿真装夹候选筛选
├─ plan_robot_path.py             完整机器人运动规划
├─ automatic_path_repair.py       自动修正、预算控制及转人工
├─ path_posture_repair.py          绕工具轴姿态候选
├─ robot_path_engine.py           运动学和碰撞引擎
├─ cloud_collision_geometry.py    扫描点云碰撞实体重建
├─ joint_coordinates.py           关节坐标映射
├─ plan_contract.py               规划策略哈希绑定
├─ robot_path_node.py             轨迹验收结果加载和运动回放
├─ cad_motion_node.py             兼容入口，调用 robot_path_node
├─ robot_route_node.py            完整机器人路线显示
├─ robot_route_geometry.py        路线几何计算
├─ workcell_node.py               工位场景发布
├─ workcell_cloud_node.py         工位点云发布
├─ cad_articulated.urdf           CAD 派生的关节化机器人模型
├─ workcell.rviz                  RViz 配置
├─ workpiece_library/             工件目录、准备后的点云和缩略图
├─ uploads/                       上传原文件和准备后的点云
├─ surface_plans/                 按任务保存计划及报告
├─ meshes/                       模型网格
├─ clouds/                       工位点云与清单
└─ test_artifacts/                测试和审计产物
```



这是主流程导航树，不是全部历史文件清单。备份文件、before\_tool\_carousel 目录、fix\_\*\.py 和 integrate\_\*\.py 等迁移脚本不应仅因名字相近就当作当前运行入口。



## 4 各模块功能



### 4\.1 输入和网页接口



|文件|主要入口|职责|
|---|---|---|
|workpiece\_selector\_server\.py|Handler\.do\_GET / do\_POST|工件与磨具目录、任务提交、上传、状态和修正报告接口|
|upload\_cloud\.py|point\_chunks / prepare\_upload|解析点云、校验坐标、生成目标掩码、抽样并保存 cloud\.npz|
|workpiece\_selection\_node\.py|Selection\.load\_piece / tick / compute\_plan|校验文件哈希，读取 xyz 和 anomaly，管理异步规划，防止旧任务覆盖新选择|



上传接受 PCD、PLY、XYZ、TXT 扩展名，具体字段和编码受解析器支持范围限制。TXT 可以保存数值坐标点云，不一定是自然语言文本。



加工区域目前来自三种方式：二值标签、用户指定坐标包围盒 ROI、用户明确确认的整表面。无标签输入不会自动产生缺陷识别结果；Real3D\-AD 内置任务使用准备后的标签。



当前上传限制为单文件 256 MiB、最多 2000 万点。上传数据经过抽样，并将最大包围盒边长归一化到 0\.36 m 的显示尺度；原始单位作为元数据保留。上传任务的目标样本超过 12000 点时，进入表面规划前还会再次抽样。能导入大点云，不等于完成全分辨率覆盖验证；当前尺度也不是已标定的实物尺寸。



### 4\.2 表面路径生成



核心入口：surface\_candidate\_planner\.py 中的 plan\_surface\(xyz, anomaly, \.\.\.\)。



1. connected\_regions 将目标点分为连通区域，记录被过滤的小区域。

2. PCA/SVD 建立局部方向，将大区域划分为局部曲面块。

3. 生成往复扫描线，通过邻域插值估计曲面高度。

4. 由点云邻域估计法向，检查曲面支撑。

5. 支撑断开、点距过大或法向突变时断开路径。

6. 使用最近端点启发式排列加工段，生成偏移参考点和未验收转场示意。

    

|输出字段|含义|
|---|---|
|surface\_points|工件表面候选路径点|
|normals|局部估计法向，方向仍需后续检查|
|reference\_points|沿法向偏移的预接触参考点，默认偏移 8 mm|
|transition\_unchecked|尚未验收的几何转场示意|
|region / id|区域与加工段编号|
|metrics / failures|几何统计及局部失败原因|



**当前运行主链使用局部曲面扫描线方法。不能仅凭论文研读材料，将这条链称作完整 EBTSO 或双 NURBS 实现。**



### 4\.3 工具选择和多次加工



tool\_schedule\.py 的 add\_tool\_schedule 为各段添加 tool\_id 与 pass\_index。



自动模式目前按区域包围范围使用简单规则：区域对角线小于 35 个显示毫米时优先细磨头，否则优先粗磨砂轮；精磨轮需要显式选择。支持安排 1～3 遍，但没有材料去除模型，不能预测每遍去除量或最终加工质量。



tool\_catalog\.json 保存三种概念工具尺寸；实际模型变换及几何还涉及 cad\_articulated\.urdf 和 robot\_path\_engine\.py。目录中的 TCP 标定项尚未确认，改工具不能只改一个尺寸配置而不核对其余模型。



### 4\.4 装夹候选筛选



|文件|主要入口|功能|
|---|---|---|
|fixture\_pose\_search\.py|orientation\_candidates / place\_plan|生成候选姿态，对点云、路径、法向进行一致变换|
|fixture\_yaw\_adaptation\.py|adapt\_fixture\_yaw|在机器人和碰撞场景中比较候选，保存选择及检查信息|



这一步只改变仿真放置候选；真实夹持稳定性、夹具可调范围和外部轴联动仍需单独验证。



### 4\.5 机器人规划和碰撞检查



|文件或对象|主要功能|
|---|---|
|plan\_robot\_path\.py → solve|候选逆解及目标误差、碰撞判断|
|plan\_robot\_path\.py → plan\_stroke|加工段、进退刀、转场和返回安全位的完整检查|
|plan\_robot\_path\.py → plan|组织全部加工段，调用修正器，生成计划和报告|
|robot\_path\_engine\.py → Model|模型加载、正运动学、工具参考点及逆运动学|
|robot\_path\_engine\.py → Scene\.state|单个关节状态的碰撞与净空检查|
|robot\_path\_engine\.py → Scene\.edge|两个关节状态之间运动连接的检查|
|cloud\_collision\_geometry\.py|基于扫描点云构造分列碰撞实体|
|joint\_coordinates\.py|关节坐标解释与映射|



点云分列实体保留上表面凹部，但仍依赖重建假设，不证明不可见部位的真实形状。碰撞结论只对当前模型、障碍和净空策略有效，控制器零位与实际 TCP 尚未标定。



### 4\.6 自动修正和转人工



automatic\_path\_repair\.py 中的 RepairController\.repair 管理失败后的搜索；proposals 根据失败原因安排候选优先级。



修正包括替换逆解初值或分支、绕工具轴旋转、反向进入、调整进退刀距离、加密采样、关节空间绕行和按原始顶点拆分。所有子段通过后，原加工段才计为通过，不能通过删除困难点来冒充完整覆盖。



|automatic\_repair\_policy\.json 配置|当前值|
|---|---|
|max\_attempts\_per\_stroke|36 次，原段与子段共享|
|max\_seconds\_per\_stroke|360 秒|
|max\_seconds\_per\_attempt|180 秒|
|max\_seconds\_per\_job|1800 秒|
|trials\_before\_split|8 次|
|max\_split\_depth|2|
|extra\_precontact\_offsets\_m|额外退让 2、4、8 mm，仅非接触演示|



当前策略禁止移动真实表面目标、任意倾斜工具轴和增加碰撞豁免。额外退让只是预接触演示策略，不意味着仍能磨到工件。超预算或仍失败的段标记 needs\_human，保留尝试记录与人工建议。



### 4\.7 轨迹回放和可视化



robot\_path\_node\.py 的 Replay 核对源计划、轨迹、模型、场景、点云和策略哈希，加载通过的轨迹，以五次插值发布关节状态，并进行运行时碰撞检查。



cad\_motion\_node\.py 目前只是兼容入口，调用 robot\_path\_node\.main。候选路径节点的独立游标更新已提前返回，实际 TCP 显示由机器人正运动学产生。



robot\_route\_node\.py 和 robot\_route\_geometry\.py 提供完整路线叠加。候选表面线、未检查转场线、已验收路径和机器人实际 FK 路线具有不同含义，不能只凭线条存在判定任务通过。



## 5 配置和输出文件速查



|文件或路径|用途与阅读重点|
|---|---|
|selector\_request\.json|工件、工具、遍数、间距、request\_id|
|selector\_status\.json|网页汇总和候选路径状态；ready 不是机器人全任务通过|
|surface\_plans/\<request\_id\>/plan\.json|区域、候选路径、法向、世界变换、输入来源|
|同任务目录 robot\_plan\.json|关节节点、时间、通过段、拒绝段、误差和修正结果|
|同任务目录 robot\_plan\_gate\.json|哈希及验收摘要；passed 当前表示至少有段通过|
|同任务目录 automatic\_repair\_report\.json|尝试次数、参数、覆盖情况和 needs\_human|
|robot\_planning\_progress\.json|当前段、通过和拒绝数量、规划心跳|
|robot\_execution\_status\.json|当前阶段、工具、转盘角度、错误及执行心跳|
|automatic\_repair\_policy\.json|修正预算和允许调整范围|
|collision\_geometry\_policy\.json|点云碰撞重建策略|
|workcell\_layout\.json|工位和障碍布局|
|joint\_coordinate\_mapping\.json|模型关节约定及待核实映射|
|plan\_contract\.py|参与规划策略哈希计算的文件集合|



robot\_plan\.json 中 validated 表示本次全部加工段通过，partial 表示部分通过，blocked 表示没有段通过。当前允许回放已通过的部分。判断全任务完成应同时检查 automatic\_repair\.whole\_task\_verified 和 rejected\_strokes，不能只看门禁 passed 或看到机器人运动。



## 6 启动和排查



推荐启动命令：



```PowerShell
& 'C:\Users\15540\Documents\Codex\2026-09-26\new-chat\scan_n_plan_physics\estun_ier220_2700\start_collision_guarded.ps1'
```



脚本先检查 Docker、容器及自动修正、装夹坐标、碰撞几何和规划合约等测试与审计，再停止被替换的进程并调用 start\_workcell\.ps1。检查失败会停止启动。



|现象|优先查看|对应模块|
|---|---|---|
|上传提示没有标签|目标模式与文件字段|upload\_cloud、网页接口|
|点云出现但没有候选线|selection\_node\.log、selector\_status\.json|任务节点、表面路径|
|有线但机器人不动|robot\_execution\_status\.json、robot\_planner\.log|回放节点、机器人规划|
|目标位姿无合格逆解|位置和轴误差、失败样本、修正记录|Model\.ik、自动修正|
|碰撞拒绝|对象名、重叠深度、净空、几何策略|Scene、点云重建|
|只有部分区域运动|accepted\_strokes、rejected\_strokes、whole\_task\_verified|机器人规划|
|改代码后旧计划失效|模型、策略、场景及源计划哈希|plan\_contract、回放节点|



测试入口包括 test\_surface\_candidate\_planner\.py、test\_upload\_tools\.py、test\_fixture\_pose\_search\.py、test\_cloud\_collision\_geometry\.py、test\_robot\_planning\_contract\.py、test\_automatic\_path\_repair\.py 和 test\_automatic\_repair\_handoff\.py。整体审计包括 validate\_robot\_paths\.py 与 validate\_column\_collisions\.py。



## 7 自主缺陷识别应接在哪里



建议在点云准备与 plan\_surface 调用之间加入预测及工艺判断：



```Plain Text
原始 XYZ
  → 缺陷识别输出分数与区域
  → 工艺判断筛选可打磨区域
  → 生成与 xyz 对齐的目标掩码
  → plan_surface(xyz, target_mask, ...)
  → 工具安排 → 机器人规划 → 修正和碰撞验收
```



优先接入 workpiece\_selection\_node\.py 的 compute\_plan 中表面规划调用之前，或在工件预处理时生成独立预测结果。需要保存预测模型版本、阈值、数据来源，以及预测点和抽样点之间的映射。不能把 GT 改名为预测；凹陷异常也不能直接判定能通过打磨修复。



## 8 推荐阅读顺序



1. workpiece\_selection\_node\.py：任务如何组织。

2. surface\_candidate\_planner\.py：工件路径如何生成。

3. plan\_robot\_path\.py：路径如何变成机器人运动。

4. automatic\_path\_repair\.py：失败如何自动调整。

5. robot\_path\_engine\.py：运动学和碰撞依据。

6. robot\_path\_node\.py：如何加载、回放和反馈状态。

    

当前交付是概念工具与 CAD 派生机器人上的预接触仿真，不包含真实去除量预测、力控、磨损补偿、控制器程序适配或真机加工质量验收。



