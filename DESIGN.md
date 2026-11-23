# ESTUN 整站打磨仿真控制台设计规范

## 适用范围与定位

本规范面向戴屹 ESTUN iER220-2700 整站视觉打磨仿真 Web 界面（`web/index.html`、`web/style.css`、`web/workstation-app.js`、`web/workstation-panels.js`）。
界面定位为工业机器人仿真与工艺验证控制台，服务于研发工程师和组会评审人员，强调状态无歧义、数据可快速扫描、操作链路与工艺流程一致。

## 信息架构

页面按照工程执行与诊断顺序分为四个明确分区：

1. **顶栏与统一状态区**：左侧展示设备标识，右侧并排区分“流程状态”与“任务结果”，避免“运行完成”与“部分完成”混淆。
2. **工况命令栏（位于 3D 视口正上方）**：五项工况参数采用等齐网格排列，右侧放置“更新观测与预测”与“运行整站仿真 / 再次运行本工况”主按钮，紧贴下方一行展示实时系统提示与参数校验错误。
3. **数字孪生主舞台**：将 3D CAD 视口、相机视角切换、辅助图层开关、14 类场景部件树，以及底部“回放控制 + 夹持/编码器/预测/误差遥测 + J1–J6 关节角”整合为单一控制台，回放时无需滚动即可同步观察机械臂姿态与关节读数。
4. **验证指标带与工艺分析工作台**：将 13 项指标从窄侧栏解放，按“视觉识别 / 路径验收 / 执行安全 / 闭环结果”四组横向展开；下方支持按全部展开或分阶段聚焦浏览六视角分度观测、ROI 选区、加工区域清单、逐段安全验收表与加工后复检对比。

## 视觉令牌（与 `web/style.css` 保持一致）

- `--surface-canvas: #edf1f4`：冷静机加工灰蓝画布底色
- `--surface-panel: #ffffff`：主仪器面板表面
- `--surface-subtle: #f5f7f9`：次级遥测带、表头与详情底色
- `--surface-viewport: #d7e1ea`：3D WebGL 视口背景
- `--ink-primary: #13222d`：主文字（与白色对比度 > 13:1）
- `--ink-secondary: #4c6170`：次级标签与工程说明（与白色对比度 > 6:1）
- `--border-subtle: #d4dde3`：1px 结构分隔线
- `--accent-primary: #0a6f5e`：主操作与验收通过色
- `--accent-soft: #e4f2ee`：选中与激活浅底色
- `--semantic-trace: #d6336c`：工件表面预测加工路径
- `--semantic-motion: #0e8596`：已验收抓手转场路线
- `--semantic-warn: #b86500`：部分完成、待复核与参数待更新提示
- `--semantic-danger: #b8323a`：安全受阻、拒绝原因与错误提示

## 排版与数据规范

- 正文与中文标签使用 `"Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`。
- 关节角、编码器、误差、尺寸、进度、时钟等全部数值使用等宽数字 `font-variant-numeric: tabular-nums`，保证回放与刷新时无横向抖动。
- 不使用装饰性营销大标题或多余编号，所有状态色均配合文字标签或图标共同表达。

## 交互与可访问性契约

- 所有可交互元素使用原生 `<button>`、`<a>`、`<select>`、`<input>`，支持键盘焦点环（`:focus-visible`）与完整 ARIA 标签。
- 表单校验使用内联提示与 `aria-invalid`、`aria-describedby`，不使用浏览器原生弹窗。
- 响应式覆盖桌面（≥1280px）、平板（768px–1200px）与窄屏（≤640px）；支持 `prefers-reduced-motion` 与 `forced-colors`。
