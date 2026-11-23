# 工程开发

本工程参考 Google 公开的 [eng-practices](https://github.com/google/eng-practices) 和 [Python Style Guide](https://github.com/google/styleguide/blob/gh-pages/pyguide.md)。架构、功能、复杂度、测试、命名和文档随同一项改动一起审阅；明确改善可维护性的改动可以交付，范围外的泛化留到真实需求出现时处理。

`src/daiyi_grinding/` 是唯一主源码目录。测试放 `tests/`，网页静态文件放 `web/`，文档放 `docs/`。`results/` 保存固定基线与原始结果摘要；实验输出放 `runs/`。Python 包使用可编辑安装，模块导入不依赖当前工作目录。

新的 Python 代码使用四空格缩进、明确的模块名和 Google 风格 docstring。注释解释单位、坐标和设计原因。设备和模型依赖按执行阶段导入：基础感知、仿真和 VLA prepare/audit 不导入 torch，报告工具按需安装。

在本工程目录运行：

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
bash scripts/verify.sh
```

修改软件入口或目录结构时，同时更新 README、HANDOFF、CLI 和对应测试。检测路径、坐标、仿真模型或执行代码变化，需要重新生成独立运行记录。测试应能发现具体故障；已有基线和历史失败记录保留原值。

提交和审阅围绕一项明确变化组织。说明行为、验证和已知问题，引用真实产物。机器人动作与成功认定遵循仓库根目录 AGENTS.md；离线测试、仿真和接口返回分别记录。
