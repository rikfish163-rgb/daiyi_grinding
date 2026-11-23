# 本地与服务器资料

远程来源为 `4090:/home/hts/demo1/packages/daiyi_grinding_main_20261115/`，对照工作区为 `/home/hts/demo1/daiyi_grinding/`。2026-11-17 核对时，两处主源码和原始正式结果一致。

## 本地内容

| 内容 | 本地位置 | 服务器原位置 |
| --- | --- | --- |
| 主源码、网页、FR5 网格 | `src/`、`web/`、`assets/` | 交接包内对应文件及 `scene.py` / `fetch_assets.py` 中的打磨依赖 |
| 研究、实验记录与真机方案 | `docs/` | `/home/hts/demo1/daiyi_grinding/docs/` |
| 正式演示及逐时刻执行反馈 | `results/demo_v3/` | `/home/hts/demo1/daiyi_grinding/results/demo_v3/` |
| 三轮感知评估 | `results/benchmark_v1/` 至 `benchmark_v3/` 的 JSON/JSONL | `/home/hts/demo1/daiyi_grinding/results/` |
| 历史运动、接触和网页记录 | 对应结果目录的 JSON 摘要 | 同上 |
| VLA | `results/vla/shadow_v1/` 的准备、推理和审计摘要 | 同上；原始 NPZ 在服务器 |
| 甲方报告和原始演示包 | `deliverables/` | 交接包中的精简演示材料 |
| 既有交接环境与验证记录 | `docs/handoff/` | 交接包 `handoff/`、`HANDOFF.md` |

完整文件清单、两端目录规模和路径状态见 [remote-inventory.json](remote-inventory.json)。

## 服务器完整记录

原始标定图像、全部观测 NPZ、历史视频、旧源码快照和完整执行 CSV 保留在 `4090:/home/hts/demo1/daiyi_grinding/results/`。按明确路径读取，例如：

```bash
ssh 4090 'cat /home/hts/demo1/daiyi_grinding/results/demo_v2/result.json'
rsync -a 4090:/home/hts/demo1/daiyi_grinding/results/demo_v3/source_snapshot/ \
  runs/history/demo_v3/source_snapshot/
```

GPU 主工程位于 `/home/hts/demo1/hy_vla_repro/`；新 GPU 作业数据位置为 `/home/data4/hts/demo1/fr5-learning/grinding/`。原交接包、完整历史记录和模型链接保留在服务器。
