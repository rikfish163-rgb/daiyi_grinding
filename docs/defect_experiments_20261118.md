# 戴屹毛刺与批缝检测：仿真复现与算法比较

实验日期：2026-11-18 · 同一观测配对评分 · 本地 MuJoCo

原 FR5 仿真已复现，路径、检测和动力学核心数值与 `demo_v3` 一致。在同一批新的留出场景上，`robust_local` 的薄批缝像素召回从原几何法的 29.02% 提高到 89.58%，提高 60.56 个百分点；精确率从 95.53% 降到 80.84%。当前选定它用于原场景，混合缺陷和薄批缝的两次完整轨迹执行均通过运动门槛。

用户 CAD 底座的曲面和台阶更复杂，直接迁移 `robust_local` 会误选原模型表面。第一轮 CAD 参考法混合缺陷精确率 / 召回为 97.10% / 76.56%，但薄批缝召回仅 3.39%。后续细缝与分辨率实验独立列出，避免用单帧演示替代批测结论。

新的细缝参考法在 1920×1440 配对留出场景达到精确率 96.78%、召回率 62.01%，正常 CAD 场景 6/6 不加工且无复核。它改善了细缝漏检，但仍有部分薄缝未被检出。

公开样本已取得 8 对注塑飞边照片和像素标签，实测两种 RGB 方法表现较差。这些样本材质为塑料；真实金属毛刺 RGB-D 原始数据尚未取得。当前结论限定于报告列出的仿真和照片实验，材料去除、恒力磨削和加工质量仍需实测。

## 原仿真复刻

seed 7、mixed、3 mm 名义间隙：14 段路径、280.816 mm，仿真 120.87 s / 60,435 步。轨迹位置 RMS / P95 / 最大误差为 0.0306 / 0.0546 / 0.2968 mm。预检查及运动门槛通过，工具接触和异常接触记录均为 0。

原两方法批测复刻 84 条配对记录（7 类 × 6 种子 × 2 方法），逐场景指标和 14 组汇总全部与原 `benchmark_v3` 精确一致。视频全帧解码及反馈 CSV 已核验，共有 1,451 帧视频和 12,087 条反馈。

[复刻视频](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/reproduction/demo.mp4) · [复现比较记录](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/reproduction_comparison.json) · [复现命令](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/reproduction_notes.md)

## 同一观测的五方法比较

比较方法：全局高度阈值、原局部几何、多尺度几何、鲁棒局部基面和已配准 CAD 参考差值。`robust_local` 使用正向残差重加权估计局部基面，并做噪声自适应双阈值连接；原场景的低噪声薄批缝因此可保留更多连续像素。多尺度候选和鲁棒局部候选在观测噪声 MAD >0.12 mm 时回退原几何后端，`noisy` 的相同结果来自此路由。CAD 参考法增加正常参考深度和精确配准输入，应视为条件不同的附加对照。

![五种算法留出比较](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/comparison.png)

### 原 FR5 合成工件

留出种子 6007–6012，共 42 个场景、210 条方法记录。开发集 5007–5010 只用于选择参数；同 seed 的 7 个 case 共享基底和位姿。70 个开发及留出场景来自 10 个生成基底，均为合成观测。

| 方法 | 混合精确率 | 混合召回率 | 薄缝精确率 | 薄缝召回率 | 薄缝 F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全局高度阈值 | 3.31% | 72.55% | 1.11% | 40.03% | 2.16% |
| 原局部几何 | 93.34% | 83.37% | 95.53% | 29.02% | 44.52% |
| 多尺度几何 | 58.09% | 89.98% | 49.32% | 97.63% | 65.54% |
| 鲁棒局部基面 | 83.96% | 96.27% | 80.84% | 89.58% | 84.99% |
| CAD 参考差值* | 83.70% | 97.00% | 79.04% | 93.83% | 85.80% |

* CAD 参考法额外接收同位姿正常深度；此处参考观测使用独立噪声。

| 方法 | 正常件不加工 | 正常件触发复核 | 正常件自动误选总像素 | coarse 触发复核 | coarse 余料误选像素 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全局高度阈值 | 0/6 | 0/6 | 430,950 | 0/6 | 9,106 |
| 原局部几何 | 6/6 | 0/6 | 0 | 6/6 | 0 |
| 多尺度几何 | 5/6 | 2/6 | 421 | 6/6 | 0 |
| 鲁棒局部基面 | 6/6 | 0/6 | 0 | 6/6 | 0 |
| CAD 参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |

`robust_local` 在全部 42 个留出场景上正常结构自动误选为 0，正常件 6/6 不加工且不复核，coarse 6/6 复核。多尺度方法在正常件上有 1/6 自动误加工（421 像素）以及 2/6 复核提示，因此没有入选。正常件缺陷召回与 F1 不定义，以不加工比例和误选像素评估；表中的“—”保留此分母含义。

### 用户 CAD 底座

第一轮使用版本 097 CAD 的可见顶面，留出种子 7107–7112 共 42 个场景、210 条方法记录。普通批缝峰高 1.5–3 mm，薄批缝 0.35–1 mm，局部毛刺 1.5–3.5 mm，超高余料 7–11 mm；这些是仿真设定。缺陷由嵌入原顶面 0.08 mm 的闭合网格构成，真值按可见几何事后生成。

| 方法 | 混合精确率 | 混合召回率 | 薄缝精确率 | 薄缝召回率 | 薄缝 F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全局高度阈值 | 0.18% | 29.80% | 0.02% | 6.78% | 0.04% |
| 原局部几何 | 98.77% | 24.19% | — | 0.00% | 0.00% |
| 多尺度几何 | 96.99% | 63.15% | — | 0.00% | 0.00% |
| 鲁棒局部基面 | 6.00% | 58.90% | 0.00% | 0.00% | 0.00% |
| CAD 参考差值* | 97.10% | 76.56% | 66.67% | 3.39% | 6.46% |

| 方法 | 正常件不加工 | 正常件触发复核 | 无注入缺陷时原 CAD 表面误选像素 | coarse 触发复核 | coarse 余料误选像素 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 全局高度阈值 | 0/6 | 0/6 | 558,970 | 0/6 | 2,109 |
| 原局部几何 | 6/6 | 0/6 | 0 | 6/6 | 0 |
| 多尺度几何 | 6/6 | 6/6 | 0 | 6/6 | 0 |
| 鲁棒局部基面 | 0/6 | 3/6 | 30,576 | 6/6 | 0 |
| CAD 参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |

CAD 标签 3 代表全部可见原模型表面，区别于原合成场景专门标注的正常筋和凸台。`robust_local` 在 6 个无注入缺陷 CAD 场景中误选 30,576 个原模型像素，直接迁移不通过。原几何和多尺度方法未检出第一轮的薄缝，参考法也只检出很小一部分；需要检查滤波对细缝的影响与相机采样。

## CAD 细缝保留与分辨率对照

第二轮使用新的开发种子 8207–8208 和留出种子 8307–8312。两种分辨率拍摄相同的 CAD、缺陷和位姿，112 帧观测对应 56 个物理场景状态；不能按 112 件独立工件计数。参考法均使用同位姿理想 CAD 深度。缺陷标签仍只进入事后评分。三个方法共记录 336 条开发及留出结果；其中留出为 42 个场景状态 × 2 档分辨率 × 3 方法，共 252 条记录。

![CAD 细缝与分辨率对照](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_refinement_comparison.png)

### 1024×768 留出结果

| 方法 | 混合精确率 | 混合召回率 | 薄缝精确率 | 薄缝召回率 | 薄缝 F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原局部几何 | 97.13% | 24.39% | — | 0.00% | 0.00% |
| CAD 参考差值* | 95.93% | 73.88% | 92.86% | 14.53% | 25.13% |
| 保留细缝的参考差值* | 96.17% | 83.10% | 89.02% | 55.24% | 68.18% |

| 方法 | 正常件不加工 | 正常件触发复核 | 无注入缺陷时原 CAD 表面误选像素 | coarse 触发复核 | coarse 余料误选像素 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原局部几何 | 6/6 | 0/6 | 0 | 6/6 | 0 |
| CAD 参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |
| 保留细缝的参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |

### 1920×1440 留出结果

| 方法 | 混合精确率 | 混合召回率 | 薄缝精确率 | 薄缝召回率 | 薄缝 F1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原局部几何 | 98.82% | 60.70% | — | 0.00% | 0.00% |
| CAD 参考差值* | 97.81% | 85.38% | 98.00% | 37.75% | 54.50% |
| 保留细缝的参考差值* | 97.67% | 87.02% | 96.78% | 62.01% | 75.59% |

| 方法 | 正常件不加工 | 正常件触发复核 | 无注入缺陷时原 CAD 表面误选像素 | coarse 触发复核 | coarse 余料误选像素 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原局部几何 | 6/6 | 6/6 | 0 | 6/6 | 0 |
| CAD 参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |
| 保留细缝的参考差值* | 6/6 | 0/6 | 0 | 6/6 | 0 |

在相同新种子和相同分辨率上，细缝参考法把薄批缝召回从 14.53% 提高到 55.24%（1024×768），从 37.75% 提高到 62.01%（1920×1440）。1920×1440 的精确率为 96.78%，仍漏检 37.99% 的可见薄缝像素，薄批缝问题尚未完全解决。

混合缺陷的路径投影覆盖在 1024×768 为 75.64%，在 1920×1440 为 100.00%。较低分辨率下的检测片段不能总被原轨迹规划器连接成有效加工轨迹。两档分辨率的正常件均 6/6 不加工且不复核；coarse 均 6/6 复核、超范围余料自动误选为 0。

高噪声下，细缝法限制误选的同时丢失部分缺陷：

| 分辨率 | CAD 参考法高噪声精确率 / 召回率 | 细缝参考法高噪声精确率 / 召回率 |
| --- | ---: | ---: |
| 1024×768 | 85.71% / 73.57% | 96.32% / 55.05% |
| 1920×1440 | 93.20% / 81.79% | 98.42% / 61.12% |

开发集选型为 `thin_reference`，记录见[selection.json](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_refinement/selection.json)。选型使用开发集，留出指标只用于确认。

各分辨率结果分别评价；没有把不同像素数的观察合并成一个泛化分数。

## 公开飞边照片实测

[SSAL 作者仓库](https://github.com/ZianYuCode/SSAL)提供 CC0-1.0 的注塑飞边照片和像素标签。本轮实际取得 8 对，4 张开发、4 张留出，统一宽度 800 像素。该划分按照片，开发/留出可能包含同一实物不同角度。

![公开塑料飞边照片实验](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/external_comparison.png)

| 方法 | 留出精确率 | 留出召回率 | 留出 F1 | TP / FP / FN |
| --- | ---: | ---: | ---: | ---: |
| 外轮廓开运算 | 15.81% | 21.97% | 18.39% | 6,450 / 34,335 / 22,904 |
| 灰度顶帽 | 11.20% | 63.24% | 19.03% | 18,564 / 147,173 / 10,790 |

背景格栅亮线和高光带来误报，宽飞边被漏检。照片结果与金属 RGB-D 仿真分别解释。[SINTEF 真实铸件研究](https://www.sintef.no/en/projects/2015/self-supervised-learning-when-you-dont-have-enough-labeled-data/)支持 CAD–RGBD 配准与毛刺高度估计路线，但本轮未核实开放原始数据下载。更多数据集的许可和获取状态见[来源核验](/home/dianrobot/daiyi_grinding/docs/defect_sources_20261118.md)。

## 选定方法的完整轨迹执行

开发集选择记录的观测方法为 `robust_local`。在两种新的留出工况上实际完成原 FR5 控制链，检测、规划、关节命令、积分反馈与视频均已保存。

| 工况 | 路径数 | 总长度（mm） | 仿真时长（s） | TCP RMS（mm） | TCP P95（mm） | 最大误差（mm） | 运动门槛 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 批缝与毛刺混合，seed 6007 | 15 | 335.373 | 133.112 | 0.0288 | 0.0461 | 0.2302 | 通过 |
| 薄批缝，seed 6007 | 1 | 207.543 | 31.556 | 0.0134 | 0.0129 | 0.2107 | 通过 |

[混合缺陷执行视频](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/selected_robust_local_mixed_6007/demo.mp4) · [薄批缝执行视频](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/selected_robust_local_thin_6007/demo.mp4) · [混合执行结果](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/selected_robust_local_mixed_6007/result.json) · [薄缝执行结果](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/selected_robust_local_thin_6007/result.json)

这两次运动沿观测面保持 3 mm 名义间隙，异常接触和工具接触记录均为 0；路径 3 mm 覆盖为 XY 投影指标，不代表材料实际去除。

## 现成 ESTUN 工站检测演示

在用户现成工站模型上复用同一混合缺陷，1600×1200 静态 RGB-D 检测出 3 个区域（1 条批缝、2 处毛刺）。精确率 96.57%、召回率 83.83%，原 CAD 表面误选 37 像素、类别错分 11 像素。生成 9 段表面轨迹预览，共 152.94 mm。

![现成工站静态检测](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_station_demo_highres/preview.png)

[实际检测图](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_station_demo_highres/detection_paths.png) · [完整记录](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_station_demo_highres/result.json)。此处机械臂未执行动作，轨迹仍按原球头工具的表面预览定义；后续需另行映射为抓工件对砂带机/主轴的运动。

## 适用边界与复现入口

当前选型分条件使用：原固定工位且无 CAD 参考时采用 `robust_local`；复杂 CAD 底座采用已配准 `thin_reference` 路线，1920×1440 的独立细缝验证优于低分辨率，仍需继续改善薄缝漏检。真实金属反光、扫描噪声、配准误差、恒力磨削、去除量、过切和表面质量尚未形成生产验收证据。

`097`、`021` 只按 CAD 文件编号引用。解析曲面/B-Spline、颜色和顶点差异均不能确定原 CAD 是否为毛坯、成品或存在真实毛刺。

```bash
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage prepare --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage evaluate --split development --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage select --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage evaluate --split holdout --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage execute --case mixed --seed 6007 --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.defect_study --stage execute --case thin --seed 6007 --output runs/defect_study_new
MUJOCO_GL=egl .venv/bin/daiyi-run --method robust_local --case thin --seed 6007 --video --output runs/thin_demo_new
MUJOCO_GL=egl .venv/bin/daiyi-cad-resolution --output runs/defect_study_new/cad_resolution_dataset
MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1024 --split development
MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1920 --split development
MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --select
MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1024 --split holdout
MUJOCO_GL=egl .venv/bin/daiyi-cad-refinement --output runs/defect_study_new --resolution 1920 --split holdout
.venv/bin/python -m daiyi_grinding.study_report --study runs/defect_study_20261118
```

现有已完成目录不覆盖。生成新数据和方法比较时使用新的输出名字。报告图直接读取本轮的评分 JSON，并校验计数与精确率、召回率和 F1 一致。

## 可检查的证据

1. [原基线复现比较](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/reproduction_comparison.json)
2. [原场景五方法留出汇总](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/coupon_comparison/holdout/summary.json)
3. [CAD 五方法留出汇总](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_comparison/holdout/summary.json)
4. [公开照片实际获取记录](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/external_samples/ssal/acquisition.json)与[两方法实验](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/external_samples/ssal/benchmark_results.json)
5. [观测方法选型记录](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/selection.json)
6. [静态工站检测记录](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/cad_station_demo_highres/result.json)
7. 独立核验记录：[independent_audit_precomparison.json](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/independent_audit_precomparison.json) · [independent_cad_audit.json](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/independent_cad_audit.json) · [independent_coupon_audit.json](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/independent_coupon_audit.json) · [audit_data_all.json](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/audit_data_all.json)
`independent_audit_precomparison.json`：预评测数据/默认检测已验证，完整比较当时尚未生成。该状态仅覆盖记录中列出的数据、预测或执行检查；不会扩展为真实加工验证。
`independent_cad_audit.json`：通过所覆盖检查。该状态仅覆盖记录中列出的数据、预测或执行检查；不会扩展为真实加工验证。
`independent_coupon_audit.json`：通过所覆盖检查。该状态仅覆盖记录中列出的数据、预测或执行检查；不会扩展为真实加工验证。
`audit_data_all.json`：通过所覆盖检查。该状态仅覆盖记录中列出的数据、预测或执行检查；不会扩展为真实加工验证。

[完整测试日志](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/tests_final.log)：53 项中 52 项通过，1 项已有网页临时目录路径测试失败（`serve.py` 中 `relative_to(ROOT)` 拒绝仓库外测试目录）。该故障未在本轮检测实验中修复，不能报告为全量测试通过。
[源码与测试 lint](/home/dianrobot/daiyi_grinding/runs/defect_study_20261118/lint_final.log)通过；报告生成器本身的 lint 和格式检查也通过。

检测输入与评分标签分文件保存。`prediction.npz`、`evaluator/truth.npz`、逐场景记录、源码快照和数据哈希支持进一步独立核验。
