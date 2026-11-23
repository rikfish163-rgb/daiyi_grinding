# 双机衔接：Linux 主机放 main，GPU 任务留服务器

日期：2026-11-15。本文件是本次双机分工的接手入口，取代上一版“整项目迁移”的使用方式。

## 1. 两端分别做什么

| 内容 | 另一台 Linux 主机 | 当前 GPU 服务器 |
| --- | --- | --- |
| 主工程编辑、Git、网页 | 在这里进行 | 按需接收源码副本 |
| RGB-D 渲染、检测、路径、MuJoCo 仿真 | 在这里运行 | 不需要替主机运行仿真 |
| 标定、ROI、运动与结果审计 | 在这里运行 | 可保留历史记录 |
| torch / CUDA 模型环境 | 不安装 | 留在服务器 |
| VLA 权重、训练数据集、checkpoint | 不复制 | 留在服务器 |
| GPU 训练、模型推理 | 需要时提交输入 | 在服务器执行 |
| 原始完整历史结果与失败记录 | 仅带必需基线摘要 | 保留现有完整目录 |

本包就是“main 主工程”。它不包含 `hy_vla_repro/`、模型权重、训练环境、训练缓存、大数据集、旧版源码快照和完整历史 results。`vla_experiment.py` 保留在主源码中：主机可运行 `prepare` 和 `audit`，`infer` 只在服务器模型环境中运行。

MuJoCo 动力学在主机上计算；EGL 图形渲染也使用主机自己的显卡/驱动。主机不需要训练级 CUDA 环境，但当前渲染配置需要可工作的 EGL。基础仿真断开服务器也能运行。

## 2. 主机侧目录

解压后的根目录可以放在任何开发路径；完整保留以下结构：

```text
daiyi_grinding_main_20261115/
  daiyi_grinding/             # 唯一主源码、网页、文档
    results/calibration/     # 当前生效外参
    results/demo_v3/         # 网页初始演示需要的少量文件
    results/benchmark_v3/    # 正式评估摘要与协议
    results/vla/shadow_v1/   # 现有 VLA 负结果的少量审计摘要
    deliverables/           # PDF、精简演示材料 ZIP
  assets/fairino5_v6/        # FR5 网格，避免基础仿真重新下载
  scene.py                  # 主源码引用的父目录依赖
  fetch_assets.py
  scripts/                  # 主机安装、验证、按需双机同步
  handoff/                  # 固定依赖、验证记录、校验清单
  gpu.env.example           # 服务器连接配置模板，不含密码
  HANDOFF.md
```

继续开发修改 `daiyi_grinding/*.py` 与 `web/`。源码保留原实现；新增的是本次交接脚本和文档。`build_delivery.py` 也保留，重建报告时按需要安装 matplotlib/reportlab 与中文字体；基础仿真不依赖这些报告工具。

## 3. Linux 主机安装与启动

```bash
unzip daiyi_grinding_main_20261115.zip -d ~/work
cd ~/work/daiyi_grinding_main_20261115
sha256sum -c handoff/SHA256SUMS
bash scripts/bootstrap.sh --validated
bash scripts/verify.sh
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.serve --port 8848
```

在这台主机浏览器打开 `http://127.0.0.1:8848`。如果你从第三台电脑通过 SSH 编辑这台主机，可转发这台仿真主机的 8848 端口；这条连接与 GPU 服务器无关：

```bash
ssh -N -L 8848:127.0.0.1:8848 user@simulation-host
```

`bootstrap.sh --validated` 安装上次独立验证实际使用的依赖组合，固定在 `handoff/requirements-validated.txt`，不含 torch。它优先选择 `python3.12`，也可用 `DAIYI_PYTHON=/path/to/python3` 指定解释器。已实测的组合使用 Python 3.14.4 / MuJoCo 3.17.0；目标主机仍需验证。Python 需要 venv/ensurepip，安装需要 Python 包源网络。

若需要源实验的历史依赖，不带参数执行 `bootstrap.sh`；若固定版本在目标 Python/包源不可用，使用 `bootstrap.sh --compatible`，然后重新验证并保存实际 freeze。三种模式只写本目录 `.venv`。网页派生任务固定使用根目录 `.venv/bin/python`，因此不能只在其他目录建立环境。

Ubuntu/Debian 缺少系统组件时，按该主机发行版安装，例如 `python3-venv libegl1 libgl1 fonts-noto-cjk`；显卡驱动在主机安装。视频/报告字体路径为 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`。旧服务器 `.venv` 当前存在解释器与依赖目录错配，不要复制它到主机。

`scripts/verify.sh` 跑 9 项 ROI/标定测试，并在唯一新目录检查 seed7 mixed 的感知和路径。完整动力学可另选新目录运行：

```bash
MUJOCO_GL=egl .venv/bin/python -m daiyi_grinding.run \
  --seed 7 --case mixed --video \
  --output daiyi_grinding/results/migration_motion_001
```

不要覆盖 demo_v3 等基线。检查 `result.json` 中 `motion_gate_passed`；视频和运行日志都生成在主机，不会提交 GPU 服务器。

## 4. 服务器侧保持原位

现有服务器工作区是 `/home/hts/demo1`；GPU 模型工程是 `/home/hts/demo1/hy_vla_repro`。模型环境入口为 `hy_vla_repro/.venv/bin/python`，权重入口为 `hy_vla_repro/checkpoints/robotwin`。这些路径没有被本次打包移动、删除或重新安装。

实际检查发现：从当前会话能访问的文件系统看，模型环境与 checkpoints 的数据盘符号链接目标暂不可访问，详情见 `handoff/server-path-check.json`。这不证明权重已删除；在启动 GPU 任务前，先核对数据盘是否挂载、原链接是否有效。不要因为链接暂不可访问而把模型权重下载到主机。

服务器检查命令：

```bash
cd /home/hts/demo1
readlink hy_vla_repro/.venv
readlink hy_vla_repro/checkpoints
python3 hy_vla_repro/scripts/bootstrap_model.py --check
```

优先恢复/确认原数据盘路径。确需复建模型环境时，在服务器按其 `hy_vla_repro/environment/README.md` 操作；主机不参与安装。现有模型是通用发布权重，不是已训练完成的 FR5 打磨模型；FR5 打磨训练数据与策略适配仍是后续工作。

## 5. 两端按需同步

主机与服务器都需要 SSH/rsync，先在主机编辑连接配置：

```bash
cp gpu.env.example .gpu.env
```

将 `DAIYI_GPU_SSH` 改成实际服务器登录名或 SSH Host 别名；`DAIYI_GPU_WORKSPACE` 是服务器现有工作区，默认 `/home/hts/demo1`。不要把仿真主机填成 GPU 服务器。认证沿用 SSH 配置，不把密码或密钥放进项目。

源码以主机为准。修改主源码后，只有需要服务器推理时才同步：

```bash
# 可先看本次将同步哪些源码。
DAIYI_SYNC_DRY_RUN=1 bash scripts/sync_gpu.sh push-code
bash scripts/sync_gpu.sh push-code
```

`push-code` 只更新服务器对应的 Python 主源码和 `scene.py` / `fetch_assets.py`，不碰模型环境、权重或 checkpoint。不要在服务器同一批推理运行中途同步源码。首次使用自定义服务器目录时，先准备与现有工作区相同的目录布局；此脚本基于已经存在的工作区，不负责部署整个 GPU 工程。

不要对 `/home/hts/demo1` 做全量双向 rsync，也不要使用 `--delete`。脚本限定同步目录，没有删除参数；主机不会拉取服务器 `checkpoints/`、`.venv/` 或训练缓存。

## 6. 现有 VLA 的双机使用流程

当前没有主机仿真到服务器的实时 VLA 控制闭环，也没有部署新 RPC 服务。本次提供可直接按步骤操作的文件交换流程：主机生成真实观测 → 服务器 GPU 推理 → 主机取回候选并审计。适用现有影子实验，不代表可以执行 VLA 动作。

**第一步，主机生成新观测并上传输入：**

```bash
MUJOCO_GL=egl .venv/bin/python daiyi_grinding/vla_experiment.py prepare \
  --output daiyi_grinding/results/vla/host_run_001 --scene-seed 7 --case mixed
bash scripts/sync_gpu.sh push-code
bash scripts/sync_gpu.sh push-input host_run_001
```

换实验使用新的 run ID；`prepare` 会拒绝覆盖既有输入。输入只有当前三路图像、状态、参考路径和元数据，不含模型权重。

**第二步，在服务器 SSH 终端执行推理：**

```bash
cd /home/hts/demo1
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  hy_vla_repro/.venv/bin/python -u daiyi_grinding/vla_experiment.py infer \
  --output daiyi_grinding/results/vla/host_run_001 --seeds 17 29 43
```

若配置了其他服务器工作区，修改上述 `cd`。GPU 编号按服务器空闲卡选择。这个阶段只消费主机生成的输入并进行模型推理，不在服务器启动 MuJoCo 仿真。

**第三步，主机取回输出并在主机审计：**

```bash
bash scripts/sync_gpu.sh pull-output host_run_001
MUJOCO_GL=egl .venv/bin/python daiyi_grinding/vla_experiment.py audit \
  --output daiyi_grinding/results/vla/host_run_001
```

回传仅限该 run 的 JSON/NPZ/图像/日志。`audit` 调用主机的 FR5 IK 与场景。模型原始输出不是 FR5 关节指令；保留 `execution_allowed=False`。未来做 GPU 训练时也沿用“主机提供数据、服务器训练、返回指标或经适配的策略接口”的分工；当前没有已验证的打磨训练流程或实时跨机闭环。

## 7. 当前项目进度和接下来做什么

- 主链已完成 RGB-D 几何检测、ROI、路径姿态、图像标定、FR5 MuJoCo 执行和网页演示。历史正式 demo_v3 约 120.87 秒、14 段路径，运动门槛通过。
- 检测正式批测是 42 个合成场景、两方法共 84 条记录。薄批缝召回约 28.01%，漏检未解决；合成成绩不能代表真实金属泛化。
- 现有 VLA 六组影子候选全部拒绝执行，执行动作 0 条。
- 真机、恒力磨削、材料去除、过切和表面质量尚未验证。
- 先读 `daiyi_grinding/docs/experiment_journal.md`、`fr5_real_robot_plan.md`，再推进真实样件、联合标定、主轴关闭空跑和受控小样试磨。
- 完整早期失败、原始标定图像、VLA 输入输出及源码快照仍在服务器；主机只保留基本运行所需文件和摘要。历史文档指向未带入主机的记录时，到服务器同路径查看。

已有完整迁移包中的安装与运动验证是此前在独立环境完成的，日志随包保存；本精简包自身的验证看 `handoff/package-validation.json`。它不声称新目标主机、服务器 GPU 环境或 SSH 网络已验证。

## 8. 给新主机聊天的接手说明

> 当前目录是戴屹打磨 main 主工程，在这台 Linux 主机开发、渲染、规划并运行 MuJoCo 仿真。GPU 训练/推理、模型权重和训练环境留在另一台服务器，不要下载到本机。先读 HANDOFF.md、handoff/package-validation.json、daiyi_grinding/README.md 和 docs/experiment_journal.md。根目录 .venv 仅安装仿真依赖；scripts/bootstrap.sh --validated 与 scripts/verify.sh 是本机入口。服务器配置在 .gpu.env，scripts/sync_gpu.sh 只按需同步代码及指定影子实验输入输出。VLA 当前没有实时跨机闭环，候选必须保留 execution_allowed=False。不要覆盖历史基线，按我后续需求继续开发。
