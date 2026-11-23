# 双 NURBS 与 EBTSO 仿真文件结构及代码作用



本表对应当前展示 P/Q 曲线的论文对齐仿真，不是整个历史项目目录。



项目目录：`C:\Users\15540\Documents\Codex\2026-09-26\new-chat\scan_n_plan_physics\tro_paper_aligned`



## 1\. 目录结构



```Plain Text
tro_paper_aligned/
├── start.ps1                  离线实验入口
├── run.py                     合成输入、运行优化、保存结果
├── geometry.py                双曲线与 EBTSO 几何计算
├── robotics.py                六轴运动学与姿态搜索
├── config.json                实验参数和假设
├── bootstrap.py               加载工作区依赖
├── requirements.txt           Python依赖范围
├── verify.py                  独立数值核验
├── make_report.py             生成报告及网页
├── viewer_template.html       交互展示模板
├── start_rviz.ps1             RViz启动入口
├── export_rviz.py             导出展示数据和工具模型
├── launch_rviz.py             管理本仿真的ROS/RViz进程
├── rviz_node.py               发布点云、曲线和关节回放
├── paper.rviz                 RViz显示配置
├── paper_robot.urdf           机器人及可视化工具模型
├── paper_rsp.yaml             模型发布节点参数
├── rviz_data.json             RViz回放数据
├── rviz_status.json           当前回放状态
├── tool_visual_geometry.json  工具可视化几何记录
├── results/                   实验数据、指标和核验结果
│   ├── latest.json            最新实验目录指针
│   └── <run_id>/              每次实验的独立档案
├── third_party/               第三方来源与许可证记录
├── README.md                  使用说明
├── METHOD_NOTES.md            论文对照、实现差异及适用边界
├── 最新实验报告.md            最近一次结果摘要
├── playback.log               回放节点日志
├── rsp.log                    模型发布节点日志
├── rviz.log                   RViz日志
└── __pycache__/               Python自动缓存
```



## 2\. 核心计算代码



|文件|代码作用|输入|输出|
|---|---|---|---|
|`run.py`|组织完整离线实验；生成两种合成曲面与带噪路径，估计法向，调用几何优化和机器人姿态搜索|配置、机器人模型、初始关节种子|每次实验的输入、曲线、指标、姿态和关节路径档案|
|`geometry.py`|实现P/Q独立拟合、最近点求解、参数同步、直纹面能量和带偏差约束的SAM数值变体；计算工具几何偏置|已排序路径点、法向和几何参数|初始及优化后的P/Q曲线、工具轴、偏置曲线、控制点及优化历史|
|`robotics.py`|实现批量FK/IK、工具坐标变换、任务方向运动能力、准静态柔度计算和TSA姿态角搜索|工具路径、工具轴、机器人模型、刚度及载荷假设|候选姿态评价、选定绕轴角及关节路径|
|`config.json`|集中定义噪声、辅助距离、权重、迭代参数、随机种子、工具偏置、检查阈值和仿真假设|人工配置|各计算模块使用的参数；本身不执行算法|
|`bootstrap.py`|将隔离依赖目录和旧版基础代码目录加入Python模块搜索路径|工作区目录结构|可导入NumPy/SciPy及基础数值代码的运行环境|
|`requirements.txt`|声明NumPy、SciPy的版本范围|安装命令|依赖约束；精确运行版本另见实验报告|
|`start.ps1`|顺序执行实验、独立核验和报告生成；可选择安装依赖|当前配置、依赖和模型|一轮离线实验及报告；不启动机器人控制器|



`geometry.py` 是 EBTSO 几何复现的核心；`robotics.py` 是机器人姿态优化的核心；`run.py` 负责把二者串联起来。当前实现不是作者官方代码。



## 3\. RViz显示与回放代码



|文件|作用|说明|
|---|---|---|
|`start_rviz.ps1`|导出最新结果，启动容器及所需显示服务，然后调用ROS启动脚本|用于打开仿真，不重新运行完整优化实验|
|`export_rviz.py`|从最近一次实验读取点云、P/Q曲线及关节路径，生成RViz数据和工具显示模型|P/Q取自归档的同步采样结果，不是临时用方向线代替Q|
|`launch_rviz.py`|启动模型发布节点、回放节点和RViz；管理本仿真域91内匹配的旧进程|避免同一演示重复运行；生成模型发布参数|
|`rviz_node.py`|发布关节状态、工件点云、P/Q曲线和状态信息|黄线P、紫线Q、灰色P\-Q连线、红色原始路径点；回放不调用控制器|
|`paper.rviz`|设置RViz的显示项、话题、视角等|属于显示配置，不是规划代码|
|`paper_robot.urdf`|描述机器人及工具显示结构|由基础模型生成；工具显示连通不代表碰撞或实物装配验证完成|
|`paper_rsp.yaml`|向`robot_state_publisher`提供URDF和发布参数|启动时生成|
|`rviz_data.json`|保存显示所需的点云、曲线、关节角、绕轴角和运行标识|导出结果，可重新生成|
|`rviz_status.json`|保存当前面板、播放进度和仿真状态|运行中更新，不是控制器执行回执|
|`tool_visual_geometry.json`|记录主轴、连接件及刀具的显示尺寸和TCP偏置|属于可视化几何记录，不能替代实测标定|
|`playback.log`、`rsp.log`、`rviz.log`|分别记录回放、模型发布和RViz错误|用于启动与显示故障排查|



## 4\. 验证、报告与实验数据



|文件或目录|作用|能说明什么|
|---|---|---|
|`verify.py`|独立检查最近点、导数、参数同步、能量积分、运动学等，并核对已保存结果|软件数值实现的一致性；不是实物精度或完整安全认证|
|`make_report.py`|汇总结果与检查项，生成Markdown报告和交互式几何展示页|将同次实验的数据整理为可查看的证据|
|`viewer_template.html`|网页展示模板|配合报告代码展示几何对比，不计算EBTSO|
|`results/latest.json`|指向最近一次完整计算实验|RViz和报告据此选择档案；不表示控制器验收通过|
|`results/<run_id>/*_input.npz`|保存合成点云、带噪路径、法向及无噪参考路径|追溯输入；合成参考不等于实测真值|
|`results/<run_id>/*_curves.npz`|保存拟合及平滑P/Q曲线、工具轴、偏置曲线等数组|复查几何优化结果|
|`results/<run_id>/*_sam_history.json`|保存优化迭代历史|查看目标、偏差、步长及终止情况|
|`results/<run_id>/*_posture.json`、`*_gamma_grid.csv`|保存姿态优化评价及独立网格参考|核验选定角度与参考搜索的差异|
|`results/<run_id>/*_joint_path.json`|保存计算得到的关节路径和相关姿态信息|用于运动学预览；不是已完成碰撞和时间化验收的控制器程序|
|`results/<run_id>/report.json`|保存本轮配置、运行信息和汇总指标|指标的机器可读来源|
|`results/<run_id>/verification.json`|保存独立检查结果|说明哪些数值检查通过或失败|
|`results/<run_id>/manifest.json`|保存运行归档中的输入、代码及结果哈希|追溯对应版本和文件内容；后续导出文件需另行管理|
|`results/<run_id>/实验报告.md`、`viewer.html`|本轮实验报告和交互展示|用于内部分析及汇报取材|
|`third_party/`|保存TSA参考许可证、来源快照及哈希|追溯参考来源，不代表包含论文官方代码|
|`README.md`、`METHOD_NOTES.md`、`最新实验报告.md`|分别说明使用方式、方法差异和最近结果|阅读入口；历史环境状态应以当前实际运行情况为准|
|`__pycache__/`|Python自动生成的字节码缓存|不是实验源数据|



## 5\. 本目录之外的必要依赖



|工作区相对位置|作用|依赖位置|
|---|---|---|
|`scan_n_plan_physics/tro_reproduction/algorithm.py`|提供基础机器人类、B样条基函数、节点、旋转和向量工具|由`bootstrap.py`加入搜索路径，计算和验证模块导入|
|`scan_n_plan_physics/physics_robot.urdf`|提供现有ABB IRB6700基础模型|离线计算读取；导出时用于生成展示URDF|
|`scan_n_plan_paper_reproduction/results/robot/staged_joint_trajectory.json`|提供初始关节种子，确定参考工具位置与曲面摆放|`run.py`读取|
|`work/tro_deps/`|保存隔离安装的Python依赖|由`bootstrap.py`加载|
|Docker容器`robot_grinding_physics`|提供ROS 2及RViz运行环境，并访问`/physics/tro_paper_aligned`|用于ROS可视化；不是EBTSO算法本体|
|VcXsrv|在Windows显示容器内的RViz窗口|仅用于图形显示|



因此，只复制`tro_paper_aligned`一个目录不能保证完整运行，交接时还需携带上述工作区依赖，并说明容器、目录映射和运行环境。



## 6\. 数据流与阅读顺序



```Plain Text
config.json + 机器人模型 + 初始关节种子
                      ↓
                    run.py
                      ├─ 生成两种合成曲面和路径
                      ├─ geometry.py：双NURBS + EBTSO
                      └─ robotics.py：姿态搜索 + FK/IK
                      ↓
                results/<run_id>/
                      ├─ verify.py：独立核验
                      ├─ make_report.py：报告与网页
                      └─ export_rviz.py：展示数据和模型
                                      ↓
                         launch_rviz.py + rviz_node.py
                                      ↓
                           RViz点云、P/Q及机械臂回放
```



理解算法时先读`run.py`、`geometry.py`和`robotics.py`；修改画面时主要看`export_rviz.py`、`rviz_node.py`及`paper.rviz`。RViz回放不代表已完成控制器执行、真实接触或材料去除验证。

