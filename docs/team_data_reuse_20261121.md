# 杨、曹的数据追查与 097 缺陷先验接入

日期：2026-11-21。目标环境仍是企业 CAD 对应的 ESTUN iER220-2700、097 铸件、固定相机与编码器转台、机器人持件到固定砂带加工。

## 数据来源与身份

| 来源 | 实际取得或确认的内容 | 可用于本轮的部分 | 尚不能支持的结论 |
| --- | --- | --- | --- |
| 曹正恒 WeldGrind Dataset | 公开的 2,800 个合成状态、190 个基础几何；RGB-D、标定、实际 SAM 区域、观测点云及接触轨迹 | 合成训练集中的局部凸起高度与成对半径、深度噪声、数据接口、原版 CPU 后处理和 RGB-D 反投影 | 真实铸件分布、097 模具分型线、实际加工精度 |
| 杨国翱《飞边异常数据合成》《飞边缺陷合成》 | 合成方法、参数表、真实扫描的文字说明与截图 | 曲线弧长采样、沿线变化的高度和厚度、厚飞边控制点、断续与局部带状构造 | 真实样本的量测分布；截图没有足够标尺，不能恢复可靠的毫米尺寸 |
| 《单侧实采样件01完整路径复查》 | `originPointCloud.pcd` 的文件名、毫米单位、文件哈希、点数及复查结果 | 精确的原文件追查线索、未知工件身份的反例 | 不能认定它属于杨或对应 097；原始 PCD 未在附件中取得 |
| 杨的几何复现实验三个本地 PCD | `sflash_decision.pcd`、`sflash_decision_edges.pcd`、`sflash_path.pcd` | 历史合成实验参考 | 文档明确是自制、无噪声的合成点云，不能作为实测校准 |

曹数据地址：[charlesH777/WeldGrind-Dataset](https://huggingface.co/datasets/charlesH777/WeldGrind-Dataset)。版本锁定为 `f422bf01fe770718a703ef6d4744cf2b9cea0b81`，原始 ZIP 长度 10,599,528,450 字节，整包 SHA256 已匹配 `30ff4e48f1ebde8a3b74845403ea06df5822def7a2ee01607811b71d888e228a`。安全提取、ZIP 成员 CRC 和 29,840 个文件的逐文件哈希校验已完成，见 [local_verification.json](../external/datasets/weldgrind_public_v1/local_verification.json)。

完整数据在本机 `/home/dianrobot/daiyi_grinding/external/datasets/weldgrind_public_v1/weldgrind_dataset_20261110_public_v1/`。原 ZIP、来源卡、原版验证器和校验结果均保留。该数据包含六类合成基础几何：对接板、T 接头、L 接头、盒、圆柱、球；不是 097 的真实铸件扫描库。

来源留痕见 [source_audit.json](../runs/data_reuse_20261121/source_audit.json)。本机文件搜索、杨当前文档及其前置文档、飞书所有可访问知识库的文件搜索、实采报告文末附件均已检查。未发送消息、改变共享权限或申请额外访问。

未知实采样件的原文件 SHA256 为 `01aa4c4a02ec347d7e21dca434bde26d3bcaf39ea85f9ad5bbe4dbc2e9f2cbde`，独立编号 `unidentified_sample_20261116_01`，文档记录工件候选 135,096 点、1 mm 网格取样后 32,074 点。[原报告](../feishu_docs/单侧实采样件01完整路径复查：间隙与工具倾角的影响__wiki-GVaTwa8WEisdBZkQJPRc2UuFnwc.md)明确型号未知，没有对应 CAD。

## 参数如何进入 097

新增 `team_prior_20261121`，原 `research_casting_v1` 保留，可在工作站“缺陷构造来源”中切换。新版本的真实扫描拟合标记仍为 `false`。

| 参数 | 新版本的依据 | 适用范围 |
| --- | --- | --- |
| 连续、断续批缝的外伸高度 | 杨文档 `bmin=0.3`、`bmax=5.0 mm`；约四分之一控制点采用厚飞边增强 | 文档合成设置，当前探索上限取最新文档描述的 6 mm；不是实测分位数 |
| 批缝厚度 | 杨文档厚度半值 `0.3–0.8 mm`，全宽 `0.6–1.6 mm` | 当前 CAD 局部带状构造；真实模具开合方向仍未知 |
| 局部毛刺高度与两个足迹半径 | 曹优化器训练划分、`new` 队列、正向可磨 `602` 凸起元数据 | 成对抽样保留尺寸关联；铸件形状仍使用不对称多峰代理，不宣称焊接飞溅与铸件毛刺同分布 |
| 深度噪声、缺测 | 曹训练样本所提供的 clean/noisy depth 差值 | 合成传感器先验；同一实体 seed 的各缺陷工况共用同一分量，当前相机位置、分辨率与内外参不变 |
| 卷曲、破裂薄片的弧长、厚度、弯曲角 | 第一版研究假设 | 杨尚未提供可量测薄片背面的原始数据，曹局部凸起数据也不覆盖这些形态，因此没有假装完成拟合 |
| 分型根部、位置、长度、间断、表面外观 | 当前 CAD 支持面与第一版假设 | 仍是候选曲线，尚未得到实际模具分型线和加工许可确认 |

拟合只读取当前公开包的 **2,260 个优化器训练样本**：1,000 个已见历史状态回放、1,260 个新状态。历史 `val/test` 字样是来源记录；这些已见状态在公开包中属于训练回放。新验证 360 个、新测试 180 个样本不用于拟合。对新队列，训练、验证和测试基础几何分别为 63、18、9 个，原版校验器会检查互不重叠。

原始包与提取文件已完成整包 SHA256、逐成员 CRC、逐文件 SHA256、来源路径及划分核对。审计入口为 [verify_weldgrind_archive.py](../scripts/verify_weldgrind_archive.py)，统计入口为 [team_data_reuse.py](../src/daiyi_grinding/team_data_reuse.py)。

训练元数据分析见 [analysis.json](../runs/data_reuse_20261121/cao_training_analysis/analysis.json)，精确训练 ID 见 [calibration_training_ids.json](../runs/data_reuse_20261121/cao_training_analysis/calibration_training_ids.json)。实际用于紧凑凸起尺寸的 1,260 个 `new/train/602` 实例统计为：

| 尺寸（mm） | 最小 | 中位数 | P95 | 最大 |
| --- | ---: | ---: | ---: | ---: |
| 名义表面上方高度 | 0.8007 | 1.0767 | 1.3249 | 1.3499 |
| 足迹长径 | 3.6030 | 4.8993 | 6.0709 | 6.1972 |
| 足迹短径 | 2.1004 | 2.8606 | 3.5115 | 3.5991 |

生成器补偿了实体根部的嵌入量，使新先验毛刺的实际法向峰高与抽中的源实例高度一致；这项数值约束已在生成的 097 几何上验证。椭圆足迹与高度成组抽样，不使用独立边缘分布拼出源数据中没有的尺寸组合。余料的多峰表面仍是铸件代理，没有把源 `602` 的焊接飞溅语义直接等同于真实铸件毛刺。

六类形状各取前三个训练 RGB-D 对，共 18 对，用 clean/noisy 差值核验深度噪声。当前得到 MAD 标准差中位数 **0.119883 mm**、有效深度上的缺测比例中位数 **0.297489%**，作为合成传感器先验写入 [casting_team_prior_20261121.json](../config/casting_team_prior_20261121.json)。这是对该部分合成数据的核验，不能当作真实 ALSON 相机的噪声模型。

## 实际复用的代码与观测约束

曹的代码仍锁定 [WeldGrind-V1](https://github.com/CharlesH777/WeldGrind-V1) 的 `0e110bf14952cf89ef127342d6a10932d151fa62`。当前路径预测继续执行原版 `physical_trajectory_filter`；新先验工况另执行原版 `predicted_rgbd_cloud.backproject_predicted_rgbd`，导出毫米世界坐标点云、RGB、像素索引与预测 ROI。

当前 ROI 来自现有几何差异检测器，**不是本轮运行 SAM 得到的结果**。适配器输入只有实际 RGB、带噪深度、相机标定及预测区域；点云导出发生在评价读取缺陷标签之前。原曹数据采用显式 `0.5` 像素中心约定，当前 MuJoCo 内参使用整数像素坐标；适配器分别记录，避免半像素位移或米/毫米混用。

新公开观测文件不会包含真实语义、实例、根部或轨迹标签。正常工况没有预测 ROI 时不会捏造缺陷点云。监督文件仍位于独立 `evaluator/` 目录。

这些候选点云用于曹的轨迹输入接口，采样范围受当前几何检测器影响。后续训练新缺陷模型须读取完整 `public/view_XX.npz` 中的 RGB-D，避免把旧检测器遗漏的区域排除在模型输入之外。正常对照的完整 RGB-D 同样存在。

原轨迹标签是毫米世界坐标中的接触参考，含阶段与道次；不能直接当作当前 ESTUN 的 TCP 或关节指令。本轮复用数据接口和原版滤波，不迁移其机器人运动轨迹或已训练动作策略。

公开包包含原实际 SAM 预测和选定的 SAM delta 文件，没有完整 SAM 基础权重或动作模型检查点。本轮没有训练、运行或宣称复现这些模型的准确率。原 95.42% 数字是动作阶段预测准确率，不是缺陷识别准确率。

## 真实点云接入与量测

[casting_scan_calibration.py](../src/daiyi_grinding/casting_scan_calibration.py)支持 PCD（ASCII、binary、PCL LZF binary_compressed）、PLY 与 NPY。保留原始行号与缺测行，单位必须明确为 `mm` 或 `m`。

真实 097 量测要求：

1. 原文件 SHA256、工件编号、确认同件的证据和当前 097 CAD 哈希。
2. 已知初始 `T_workpiece_scan_m`；平移单位固定为米。
3. 与待处理区分开的正常区域原始行号，用于有限幅度的刚体点到面配准。异常点不参与对齐，不能非刚体扭曲掉飞边。
4. 正常锚点约束全部六个位姿自由度，正常区域残差 P95 不超过 0.3 mm；超出则拒绝拟合。
5. 明确批准的根部观测及加工区。根部与已配准 CAD 最大距离超过 0.3 mm 时拒绝继续。

当前入口量测可见局部的法向外伸与根部横向分布，输出原始行号、配准矩阵、残差及来源哈希。不会自动填补不可见薄片背面厚度、覆盖不全的整件分布，不覆盖仿真参数或生成实机命令。这里的残差门限是本轮数据质检门限，不能当作已达到视觉 ±0.1 mm 的证明。

```bash
# 只检查原文件格式、单位、有效点数与范围；不会推断工件型号。
.venv/bin/daiyi-casting-scan --inspect /path/to/scan.pcd --units mm

# 文件身份、正常区、根部和初始位姿齐备后的局部量测。
.venv/bin/daiyi-casting-scan \
  --manifest /path/to/097_scan_manifest.json \
  --output runs/real_scan_097_measurement_001
```

示例配置中的身份和根部确认默认关闭，见 [real_scan_097_manifest.example.json](../config/real_scan_097_manifest.example.json)。目前未取得可确认同件的杨原始点云，因此 **097 实测缺陷参数校准尚未完成**。待补的是可下载的原始 PCD/PLY、单位和工件身份，以及对应的正常区域、初始位姿与获确认的分型根部。已找到的截图和未知型号实采报告不能补足这些量测输入。

## 已生成的 097 团队先验样本

最终批次为 [team_prior_batch_final_02](../runs/data_reuse_20261121/team_prior_batch_final_02/report.md)，使用开发种子 9301、9302，七种工况，共 **14 组、84 帧、22 个余料对象、38 个闭合有根片段**。固定相机六视角可见 18 个对象，其余 4 个侧壁对象被遮挡。完整根部采样 1,856 点，其中 308 点至少在一视角满足理想深度一致与有效传感器深度；这是合成可见性代理，不能作为真实根部量测精度。

独立文件审计 [validation.json](../runs/data_reuse_20261121/team_prior_batch_final_02/validation.json) 已通过，检查了全部 STL 的闭合性、绕序、正体积、逐片根部嵌入，根部与原始 CAD 的一致性，固定世界相机和编码器分度、米/毫米与像素约定、共享材质及公开观测中无标签。84 帧导出 68 个非空候选点云与 16 个无预测 ROI 的空接口文件；完整 RGB-D 均保留。两个正常对照的误检像素和自动路径均为零。

审计器为 [verify_casting_synthesis.py](../scripts/verify_casting_synthesis.py)。原版 14 组、22 个对象的顶点、名义基底和根部数组仍逐元素一致，见 [legacy_geometry_validation.json](../runs/data_reuse_20261121/legacy_geometry_validation.json)。首轮数据与中间结果也保留，没有覆盖原研究批次。

![097 参数更新对照](../runs/data_reuse_20261121/parameter_comparison.png)

相同种子 9301 的近景中，连续批缝实际几何峰高由 1.934 mm 变为 5.303 mm；断续飞边由 1.238 mm 变为 5.227 mm；顶部毛刺由 2.132 mm 变为源训练实例的 1.237 mm。近景用于展示几何；推理依然使用固定相机的六视角。

| 新输入工况 | 原几何规则 F1：9301 | 原几何规则 F1：9302 | 当前暴露的问题 |
| --- | ---: | ---: | --- |
| 变厚批缝 | 0.00% | 0.00% | 被送入 `review`；未自动认定为可磨批缝 |
| 断续飞边 | 54.69% | 47.15% | 强弱高度变化、片段合并与复核分类 |
| 卷曲薄片 | 41.35% | 82.67% | 遮挡、翻卷与深度差形态不完整 |
| 破裂飞边 | 63.49% | 75.64% | 断裂、小片与边界完整性 |
| 局部毛刺 | 80.00% | 76.74% | 小足迹、带噪深度及侧壁遮挡 |
| 混合缺陷 | 30.84% | 32.82% | 厚批缝复核与多形态分类叠加 |

这里的 F1 沿用可见正余料及正确 `seam/burr` 类型的评价口径，`review` 不算可磨类型命中。9301 厚批缝已被找到，但光学深度差 5.485 mm 触发原 5 mm 加工包络复核规则，法向余量估计约 2.893 mm。这个案例既说明“发现异常”与“批准自动加工”不同，也说明光学深度差不能替代法向高度。本轮没有修改门限以抬高分数。

这些是改变合成输入后的开发结果，不能与原输入分数解释成算法提升或真实准确率。后续分割模型需要专门数据、根部标签与独立实体测试；同时仍需处理覆盖不足及携件可达性。

## 当前工站接入与验证

网页新增“缺陷构造来源”，可切换团队先验和原始研究参数，来源说明明确显示 097 实测拟合仍待补。切换参数使原计划失效，服务器也检查所选版本及源码/配置，避免拿旧场景路径执行新缺陷。

当前入口为 [本机工站](http://127.0.0.1:8848/)，载入种子 9301、`casting_mixed`、`team_prior_20261121`。最新记录登记在 [ui_record.json](../runs/data_reuse_20261121/ui_record.json)。已核对独立部件的选择与隐藏、六视角详情、旧计划失效，以及全部实际反馈回放至 180 s 末态；回放不会启动新的动力学运行。界面将初始 2/5 段预检与加工后的运动验收分别显示，复检候选没有标作已批准补磨路径，见 [浏览器验证](../runs/data_reuse_20261121/browser_verification.json) 和 [界面截图](../runs/data_reuse_20261121/workstation_team_prior_ui.png)。

[最新混合工况闭环记录](../runs/data_reuse_20261121/mixed_team_prior_closed_loop_02/result.json) 已完成观测、规划、实际连续积分、余料更新和回位六视角复检。种子 9301 初始得到 14 个观测区域、5 条候选路径，2 条通过整站预检并在 MuJoCo 中执行。一次执行后的实际加工点 P95 误差为 **1.043017 mm**，超过当前 1 mm 的运动验收门限，控制器停止后续补磨；`task_status=partial`、`goal_complete=false`、`motion_gate_passed=false`。这是加工后的运动验收，不能描述为误差跨门限时立即急停。

| 当前闭环量 | 已记录结果 | 范围与含义 |
| --- | ---: | --- |
| 初始预检 / 执行 | 2 / 5 段 | 其余候选不进入执行 |
| 抓手跟踪 RMS / P95 | 0.413899 / 0.628956 mm | 实际反馈相对同一目标关节曲线的正运动学 |
| 实际加工点 P95 / 最大误差 | 1.043017 / 1.165160 mm | 包含抓手与工件姿态的误差；P95 未通过门限 |
| 非预期接触 | 0 次 | 仿真接触检查；力控制没有经过标定 |
| 全件合成余料峰高 | 5.897768 → 5.303353 mm | 几何余料统计，名义 CAD 未修改 |
| 顶点完成比例 | 3.866% | 顶点指标，不能解释为体积去除率或整件完成率 |
| 复检候选 | 3 条 | 未进行补磨验收或执行，不能当作已批准路径 |

侧壁断续余料的几何峰高降为零；其余四个区域分别残留 5.303353、2.753556、4.379958、1.171038 mm，均高于 0.3 mm。当前仍未完成整件加工。力、速率、砂带柔顺性、热与粗糙度不在已校准范围内。

[单独毛刺工况](../runs/data_reuse_20261121/burr_team_prior_closed_loop_01/result.json) 检出 1 条路径，被工件/抓手/机械臂加工姿态干涉检查拒绝，0/1 放行、没有执行。早期 [混合工况观测与规划](../runs/data_reuse_20261121/workstation_team_prior_final_01/result.json) 和该毛刺记录保留为历史结果；源码发生变化后不作为当前可执行计划。没有删除障碍来提高放行数。

首轮完整动力学在导出逐帧网格时耗尽内存，失败目录与 [interruption.json](../runs/data_reuse_20261121/mixed_team_prior_closed_loop_01/interruption.json) 保留，未计入完成记录。回放现使用不可变余料状态共享和流式 JSON 写入，最新运行保留 **1,618 个反馈帧、4 个余料状态、0 丢帧**；进程峰值 RSS 为 2,336,240 KiB，约 2.23 GiB，见 [资源记录](../runs/data_reuse_20261121/mixed_team_prior_closed_loop_02_resources.log)。该修复没有删除反馈、平滑误差或改变机器人指令。

项目完整检查为 **87 项测试通过**，包含大网格回放内存、不可变加工前后状态及回放往返校验；原混合对照仍 14 条路径、正常对照 0 条。Ruff 与前端语法检查通过，记录见 [verify_memory_fix.log](../runs/data_reuse_20261121/verify_memory_fix.log)、[final_checks.json](../runs/data_reuse_20261121/final_checks.json)。

## 复现入口

```bash
# 原公开包校验、提取完成后，仅使用训练划分建立参数先验。
.venv/bin/daiyi-team-data \
  --root external/datasets/weldgrind_public_v1/weldgrind_dataset_20261110_public_v1 \
  --output runs/data_reuse_20261121/cao_training_analysis_02 \
  --profile-output runs/data_reuse_20261121/candidate_profile_02.json

# 将团队先验映射到当前 097 CAD 并生成实际固定相机六视角数据。
.venv/bin/daiyi-casting-synthesis \
  --casting-profile team_prior_20261121 \
  --seeds 9301 9302 \
  --output runs/data_reuse_20261121/team_prior_batch_new

# 当前 ESTUN 工作站：观测、规划、执行、复检与有限轮补磨。
.venv/bin/daiyi-workstation \
  --case casting_mixed --seed 9301 \
  --casting-profile team_prior_20261121 \
  --views multi --tool belt --max-passes 2 --realtime-factor 0 \
  --output runs/data_reuse_20261121/mixed_team_prior_closed_loop_new

# 校验刚生成的完整 RGB-D 与独立标签、几何和单位。
.venv/bin/python scripts/verify_casting_synthesis.py \
  runs/data_reuse_20261121/team_prior_batch_new
```

该更新改变的是合成输入与数据接口。现有缺陷检测仍是配准 CAD 深度差、滞回分割、连通域及几何分类规则；此处没有替换为已训练网络。新旧参数版本的检测分数对应不同缺陷与噪声输入，不能据此宣称检测算法提升。

材料去除仍是对合成余料的几何裁剪，名义 CAD 保持不变。力、去除速率、砂带柔顺性、温升、粗糙度及真实过磨尚未校准。

## 项目原始资料

- [杨：飞边异常数据合成](../feishu_docs/飞边异常数据合成__docx-BsOsdBM5collgPxMkdAcaywfnZf.md)
- [杨：飞边缺陷合成，11月19日更新](../feishu_docs/飞边缺陷合成__wiki-LcFzw9XUoiiPGrkdFzTc9TT0n5e.md)
- [工件样品概况](../feishu_docs/工件样品概况__wiki-PgR0wKBsZitEVtk7LSzchUaKn5c.md)
- [3D相机的使用](../feishu_docs/3D相机的使用__wiki-SNEFwM9pEitxaykEJfQcDZGnnwb.md)
- [第一批 097 合成缺陷](casting_synthesis_20261120.md)
- [缺陷模型路线分析](defect_model_strategy_20261120.md)
