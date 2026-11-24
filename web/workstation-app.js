import { WorkstationView } from "/workstation-view.js";
import { evidencePanels, sessionEditor, sessionPanels } from "/workstation-panels.js";

const $ = (id) => document.getElementById(id);
const kinds = { seam: "批缝", burr: "毛刺", preserve: "保留结构", review: "待复核" };
const machiningSupported = (item) => ["seam", "burr"].includes(item.kind) && item.machining_required !== false;
const stages = { queued: "等待启动", capturing: "采集整站观测", planning: "预测轨迹与整站检查", executing: "整站仿真运行中", rescanning: "加工后复检与重新预测", finalizing: "保存反馈记录", complete: "本次运行完成", failed: "仿真未完成", empty: "等待工件观测" };
const phases = { inspection: "检查工件", scan: "转台分度采集", inspect: "更新缺陷与路径预测", ready: "等待仿真执行", grasp: "抓取工件", lift: "抬起工件", transfer: "搬运与调整工件姿态", approach: "接近固定砂带", trace: "沿预测路径加工", retract: "退出磨具", return: "返回装夹区", release: "放回定位夹具", home: "机械臂回到初始姿态", complete: "加工后复检完成" };
let current = null, busy = false, dirty = false, selected = new Set(), selectionId = null;
let view = null, loadedId = null, loadingId = null, loadController = null, pollTimer = null, renderSequence = 0;
let replay = null, replaying = false, replayIndex = 0, replayClock = 0;
let replayMeshRevision = -1;
let roi = null, draft = [], drawingTool = "none", rectangleStart = null;
let seenRegions = new Set(), overlayKey = "", cloudKey = "", motionKey = "";
const roiCanvas = $("roi-canvas"), roiContext = roiCanvas.getContext("2d");

function notice(message, tone = "") { $("notice").textContent = message; $("notice").className = tone; }
async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { ...options, signal: options.signal || AbortSignal.timeout(20000) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
  return data;
}
const workpieceProfiles = new Map();

function controls() {
  $("plan").disabled = busy || replaying;
  $("plan").classList.toggle("is-dirty", dirty && !busy && !replaying);
  const admitted = current?.result?.preflight?.orientation?.passed && current?.result?.preflight?.passed;
  $("execute").disabled = busy || replaying || dirty || !admitted || !selected.size || !current?.paths?.length;
  $("execute").textContent = current?.result?.robot_executed ? "再次运行本工况" : "运行整站仿真";
  $("replay").disabled = busy || !current?.files?.includes("replay.json");
  $("replay").textContent = replaying ? "停止回放" : "回放这次仿真";
  $("replay").classList.toggle("is-active", replaying);
  for (const id of ["case", "mode", "views", "tool", "seed", "casting-profile", "detector", "workpiece-profile", "grasp-setup", "targeted-rescan", "select-auto", "select-none", "clear-roi", "finish-roi", "add-roi-point", "roi-x", "roi-y"]) $(id).disabled = busy || replaying;
  const casting = $("case").value.startsWith("casting_");
  $("casting-profile").disabled = busy || replaying || !casting;
  $("profile-detail").textContent = !casting ? "当前为原有对照工况。选择“研究合成”工件后，可切换缺陷参数来源。" : $("casting-profile").value === "team_prior_20261121" ? "批缝构造遵循杨的文档；局部凸起尺寸和深度噪声复用曹的合成训练数据。097 实测校准待原始点云。" : "保留第一批研究合成参数，可复现原样本；尚未拟合真实 097 点云。";
  $("detector-detail").textContent = $("detector").value === "hybrid" ? "几何分支定位批缝，训练好的网络以彩色图像和深度为输入识别飞边与毛刺。" : $("detector").value === "learned" ? "使用已训练的 RGB-D 网络识别飞边与毛刺；路径仍由观测三维几何生成，并经过整站安全检查。训练和验证来自人造 097 样本。" : "使用正常 CAD 深度差、连通域与尺寸规则识别，作为同工况算法对照。";
  document.querySelectorAll("[data-tool], .region input").forEach((element) => { element.disabled = busy || replaying || element.dataset.supported === "false"; });
  $("plan").setAttribute("aria-busy", String(busy && !current?.request?.execute));
  $("execute").setAttribute("aria-busy", String(busy && !!current?.request?.execute));
}
function modified(message) { dirty = true; controls(); notice(message || "处理范围已修改，请点击“更新观测与预测”。", "warn"); }

function componentSelected(id) {
  const item = view.components.find((part) => part.id === id);
  document.querySelectorAll(".component").forEach((row) => { row.classList.toggle("selected", row.dataset.component === id); row.querySelector("button").setAttribute("aria-pressed", String(row.dataset.component === id)); });
  const strong = document.createElement("strong"), text = document.createElement("p");
  strong.textContent = item?.label || "分别点选查看";
  text.textContent = item?.role || "安装底座、定位夹具和底座铸件是三个不同对象。抓手负责搬运，固定磨具负责加工。";
  $("component-detail").replaceChildren(strong, text);
}
function components(manifest) {
  $("component-count").textContent = `${manifest.components.length} 类独立对象`;
  $("component-list").replaceChildren();
  for (const item of manifest.components) {
    const row = document.createElement("div"); row.className = "component"; row.dataset.component = item.id;
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = view.visibility.get(item.id); checkbox.setAttribute("aria-label", `显示${item.label}`);
    checkbox.addEventListener("change", () => view.visible(item.id, checkbox.checked));
    const button = document.createElement("button"); button.type = "button"; button.setAttribute("aria-pressed", "false");
    const dot = document.createElement("span"); dot.className = "component-dot"; dot.style.background = item.color;
    const name = document.createElement("span"); name.textContent = item.label;
    button.append(dot, name); button.addEventListener("click", () => view.select(item.id));
    row.append(checkbox, button); $("component-list").append(row);
  }
}
$("show-all-components").addEventListener("click", () => {
  if (!view?.components?.length) return;
  for (const item of view.components) view.visible(item.id, true);
  document.querySelectorAll("#component-list input[type='checkbox']").forEach((box) => { box.checked = true; });
});
try { view = new WorkstationView($("scene3d"), componentSelected); }
catch (error) { $("scene-loading").textContent = "当前浏览器无法启动 3D。请启用硬件加速后重新打开页面。"; notice(error.message, "error"); }

for (const name of ["overview", "loading", "process", "workpiece"]) $("view-" + name).addEventListener("click", () => {
  view?.view(name);
  document.querySelectorAll(".view-presets button").forEach((button) => button.classList.toggle("active", button.id === "view-" + name));
});
$("rotate-left").addEventListener("click", () => view?.orbit(-0.2)); $("rotate-right").addEventListener("click", () => view?.orbit(0.2));
$("zoom-in").addEventListener("click", () => view?.zoom(0.85)); $("zoom-out").addEventListener("click", () => view?.zoom(1.15));
$("semantic-colors").addEventListener("change", () => view?.colors($("semantic-colors").checked));
$("show-cloud").addEventListener("change", () => view?.showCloud($("show-cloud").checked));
$("show-motion").addEventListener("change", () => view?.showMotion($("show-motion").checked));

function feedback(state) {
  if (!state) return;
  view?.state(state);
  $("phase-name").textContent = phases[state.phase] || state.phase;
  $("sim-clock").textContent = `t = ${state.simulation_time_s.toFixed(1)} s`;
  $("grasp-state").textContent = state.grasp_attached ? "工件随抓手运动" : state.fixture_attached ? "工件在定位夹具中" : "工件自由状态";
  $("prediction-revision").textContent = replaying ? "实际反馈回放" : `预测版本 ${(state.prediction_revision || 0) + 1} · ${current?.result?.reinspections?.length ? "复检" : "初次扫描"}`;
  $("table-angle").textContent = `${(state.turntable_angle_deg || 0).toFixed(2)}°`;
  $("tracking-error").textContent = state.tracking_error_mm == null ? "—" : `${state.tracking_error_mm.toFixed(3)} mm`;
  const pct = Math.max(0, Math.min(1, state.progress || 0));
  $("progress").value = pct;
  $("progress-pct").textContent = `${Math.round(pct * 100)}%`;
  for (let index = 0; index < 6; index++) $("joint-" + index).textContent = `${state.joint_angles_deg[index].toFixed(2)}°`;
}
for (let index = 0; index < 6; index++) {
  const cell = document.createElement("div"); cell.className = "joint";
  const label = document.createElement("span"); label.textContent = `J${index + 1}`;
  const value = document.createElement("strong"); value.id = "joint-" + index; value.textContent = "—";
  cell.append(label, value); $("joint-values").append(cell);
}

function regions() {
  $("region-list").replaceChildren();
  const detections = current?.detections || [];
  $("region-count").textContent = `${selected.size} / ${detections.length} 已选`;
  if (!detections.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = busy ? "正在计算工件缺陷" : "当前工件没有检出可处理缺陷。"; $("region-list").append(empty); }
  for (const item of detections) {
    const supported = machiningSupported(item);
    const row = document.createElement("label");
    row.className = "region" + (supported ? "" : " is-muted") + (selected.has(item.id) ? " is-selected" : "");
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(item.id); checkbox.dataset.supported = String(supported); checkbox.disabled = !supported || busy; checkbox.setAttribute("aria-label", `选择区域 ${item.id} ${kinds[item.kind]}`);
    checkbox.addEventListener("change", () => { checkbox.checked ? selected.add(item.id) : selected.delete(item.id); regions(); controls(); updatePaths(current); });
    const content = document.createElement("div"); content.className = "region-content";
    const title = document.createElement("div"); title.className = "region-title";
    const name = document.createElement("strong"); name.textContent = `区域 ${item.id}`;
    const tags = document.createElement("div"); tags.className = "region-tags";
    const tag = document.createElement("span"); tag.className = `tag ${item.kind}`; tag.textContent = kinds[item.kind] || item.kind;
    tags.append(tag);
    if (item.machining_required === false && ["seam", "burr"].includes(item.kind)) {
      const skip = document.createElement("span"); skip.className = "tag skip"; skip.textContent = "无需加工";
      tags.append(skip);
    }
    title.append(name, tags);

    const specs = document.createElement("div"); specs.className = "region-specs";
    const pairs = [
      ["长", `${item.length_mm.toFixed(1)} mm`],
      ["宽", `${item.width_mm.toFixed(1)} mm`],
      ["深度差", `${item.height_mm.toFixed(2)} mm`],
      ["法向余量", item.normal_height_mm == null ? "—" : `${item.normal_height_mm.toFixed(2)} mm`],
    ];
    for (const [specLabel, specVal] of pairs) {
      const cell = document.createElement("div"); cell.className = "spec-chip";
      const lbl = document.createElement("span"); lbl.textContent = specLabel;
      const val = document.createElement("strong"); val.className = "num"; val.textContent = specVal;
      cell.append(lbl, val); specs.append(cell);
    }
    content.append(title, specs); row.append(checkbox, content); $("region-list").append(row);
  }
}
$("select-auto").addEventListener("click", () => { selected = new Set((current?.detections || []).filter(machiningSupported).map((item) => item.id)); regions(); controls(); updatePaths(current); });
$("select-none").addEventListener("click", () => { selected.clear(); regions(); controls(); updatePaths(current); notice("已清空加工区域选择。"); });

function stockTargetReached(result) {
  const stock = result?.final_stock || result?.execution?.removal;
  const target = result?.controller_stopping?.target_remaining_height_mm ?? 0.3;
  return result?.material_removal_simulated && result.motion_gate_passed && Number.isFinite(stock?.remaining_peak_mm) && stock.remaining_peak_mm <= target;
}

function taskOutcome(result, stage) {
  if (stage === "failed") return { label: "任务异常", tone: "danger" };
  if (busy) return { label: "执行中", tone: "info" };
  if (!result) return { label: "等待观测", tone: "neutral" };
  if (result.goal_complete) return { label: "任务完成", tone: "pass" };
  if (result.robot_executed && !result.motion_gate_passed) return { label: "执行验收未通过", tone: "danger" };
  if (result.reinspection?.region_reinspection && !result.observational_release) return { label: "逐区复检待确认", tone: "warn" };
  if (stockTargetReached(result)) return { label: "几何余量达标 · 任务待复核", tone: "warn" };
  if (result.robot_executed) return { label: "部分完成", tone: "warn" };
  if (result.preflight?.passed && result.admitted_path_count) return { label: "规划就绪", tone: "pass" };
  if (result.task_status === "review") return { label: "初检待复核", tone: "warn" };
  if (result.task_status === "no_defect_detected") return { label: "未检测到待加工缺陷", tone: "neutral" };
  if (result.path_count === 0) return { label: "无加工路径", tone: "neutral" };
  return { label: "规划受阻", tone: "danger" };
}

function metrics(result) {
  $("metrics").replaceChildren();
  const perception = result?.perception?.metrics || current?.perception_metrics?.metrics;
  const percent = (value) => value == null ? "—" : (value * 100).toFixed(1);
  const plan = result?.preflight || current?.planning;
  const transfer = plan?.transfer_optimization;
  const executions = result?.executions?.length ? result.executions : result?.execution ? [result.execution] : [];
  const worstTracking = (key) => {
    const values = executions.map((item) => item[key]).filter(Number.isFinite);
    return values.length ? Math.max(...values).toFixed(3) : "—";
  };
  const generatedDefects = result?.perception?.generated_defects ?? current?.perception_metrics?.generated_defects ?? "—";
  const contacts = executions.length ? executions.reduce((sum, item) => sum + (item.unexpected_contact_count || 0), 0) : null;
  const outcome = taskOutcome(result, current?.status?.stage);

  const groups = [
    {
      title: "视觉识别",
      items: [
        ["本工况 F1", percent(perception?.f1), "%", true],
        ["Precision", percent(perception?.precision), "%"],
        ["Recall", percent(perception?.recall), "%"],
        ["对象匹配 / 生成", perception ? `${perception.matched_defect_count} / ${generatedDefects}` : "—", "个"],
        ["观测覆盖 / 生成", perception ? `${perception.visible_defect_count} / ${generatedDefects}` : "—", "个"],
      ],
    },
    {
      title: "路径规划与验收",
      items: [
        ["初始整段验收", plan?.segment_count ? `${plan.admitted_count || 0} / ${plan.segment_count}` : "—", "段", true],
        ["预测路径", result?.path_count ?? "—", "条"],
        ["初检保护区拦截", result?.perception?.review_path_guard?.blocked_paths_count ?? "—", "段"],
        ["首轮计划运动", transfer ? `${transfer.original_duration_s.toFixed(1)} → ${transfer.optimized_duration_s.toFixed(1)}` : "—", "s"],
        ["路径总长", result?.path_length_mm?.toFixed(1) ?? "—", "mm"],
      ],
    },
    {
      title: "执行跟踪与安全",
      items: [
        ["加工点误差 P95（最差轮）", worstTracking("surface_tracking_p95_mm"), "mm", true],
        ["抓手误差 P95（最差轮）", worstTracking("tracking_trace_p95_mm"), "mm"],
        ["非预期接触（全部轮）", contacts == null ? "—" : contacts, "次", false, contacts === 0 ? "pass" : contacts > 0 ? "danger" : ""],
      ],
    },
    {
      title: "闭环复检结果",
      items: [
        ["任务结果", outcome.label, "", true, outcome.tone],
        ["复检补磨候选路径", result?.reinspection?.remaining_paths ?? "—", "段"],
      ],
    },
  ];

  for (const group of groups) {
    const card = document.createElement("div");
    card.className = "metric-group";
    const heading = document.createElement("h3");
    heading.textContent = group.title;
    const list = document.createElement("div");
    list.className = "metric-list";
    for (const [label, value, unit, highlight, tone] of group.items) {
      const cell = document.createElement("div");
      cell.className = "metric-row" + (highlight ? " is-primary" : "") + (tone ? ` tone-${tone}` : "");
      const name = document.createElement("span");
      name.textContent = label;
      const number = document.createElement("strong");
      number.className = "num";
      number.textContent = String(value);
      if (unit) {
        const suffix = document.createElement("small");
        suffix.textContent = unit;
        number.append(suffix);
      }
      cell.append(name, number);
      list.append(cell);
    }
    card.append(heading, list);
    $("metrics").append(card);
  }
  $("preflight-state").textContent = result?.robot_executed && !result.motion_gate_passed
    ? `初始预检通过 ${result.admitted_path_count} / ${result.path_count} 段并已执行；加工后的运动验收未通过，已停止后续补磨。实际加工点误差见执行指标。`
    : !result ? "逐段检查六轴可达性、携件碰撞及进出段。" : result.preflight?.passed && result.admitted_path_count ? `已验收 ${result.admitted_path_count} / ${result.path_count} 段；执行只采用通过的路径。识别指标来自合成缺陷可见像素。` : result.path_count === 0 ? "当前没有可执行路径；请同时查看缺陷检出数和未覆盖区域。" : "存在不可达姿态或障碍干涉，未通过的路径无法执行。";
}
function image(id, base, file, files, revision = "") { const src = base + file + (revision === "" ? "" : `?revision=${revision}`); if (files.includes(file) && $(id).getAttribute("src") !== src) $(id).src = src; }
function visiblePaths(data) { return data.files?.includes("after_paths.json") ? data.after_paths || [] : data.paths || []; }
function updatePaths(data) {
  const key = `${data.id}:${data.live?.prediction_revision}:${data.files?.includes("after_paths.json")}:${[...selected].join(",")}`;
  if (key !== overlayKey) { overlayKey = key; view?.paths(visiblePaths(data), data.files?.includes("after_paths.json") ? null : selected); }
}
async function loadEvidence(data) {
  const files = data.files || [];
  const cloudFile = files.includes("after_pointcloud.json") ? "after_pointcloud.json" : "pointcloud.json";
  const nextCloudKey = `${data.id}:${cloudFile}:${cloudFile.startsWith("after") ? data.result?.prediction_revision : 0}`;
  if (files.includes(cloudFile) && nextCloudKey !== cloudKey) {
    cloudKey = nextCloudKey;
    const cloud = await fetchJSON(data.artifact_base + cloudFile + `?revision=${data.result?.prediction_revision || 0}`);
    if (current?.id === data.id && loadedId === data.id) { view?.cloud(cloud); view?.showCloud($("show-cloud").checked); }
  }
  if (files.includes("motion_preview.json") && motionKey !== data.id) {
    motionKey = data.id;
    const motion = await fetchJSON(data.artifact_base + "motion_preview.json");
    if (current?.id === data.id && loadedId === data.id) { view?.motion(motion); view?.showMotion($("show-motion").checked); }
  }
}
async function render(data, initialize = false) {
  const sequence = ++renderSequence;
  current = data;
  const stage = data.status?.stage || "empty";
  busy = !["complete", "failed", "empty"].includes(stage);
  $("run-state").textContent = stages[stage] || stage;
  const outcome = taskOutcome(data.result, stage);
  const pill = $("task-outcome-pill");
  pill.textContent = outcome.label;
  pill.className = `outcome-pill tone-${outcome.tone}`;
  $("record-kind").textContent = "当前 ESTUN 整站（独立运行记录）";
  $("run-detail").textContent = busy ? (data.live ? `${phases[data.live.phase] || "观测与规划"}（${data.live.simulation_time_s.toFixed(1)} s）` : "加载现有 CAD 并生成工件观测") : `${data.result?.tool?.label || "固定磨具"}，抓手搬运工件`;
  if (initialize && data.result) { $("workpiece-profile").value = data.result.workpiece_profile?.id || ""; $("grasp-setup").value = data.result.grasp_setup || "original"; $("targeted-rescan").value = data.result.targeted_rescan ? "on" : "off"; $("seed").value = data.result.seed; $("case").value = data.result.case; $("casting-profile").value = data.result.casting_profile || "research_casting_v1"; $("detector").value = data.result.detector_kind || "geometric"; $("mode").value = data.result.mode; $("views").value = data.result.views || "multi"; $("tool").value = data.result.tool?.key || "belt"; roi = data.result.roi; drawRoi(); }
  if (selectionId !== data.id) { selected.clear(); seenRegions.clear(); selectionId = data.id; }
  for (const item of data.detections || []) {
    if (!seenRegions.has(item.id) && machiningSupported(item) && (!data.request?.ids || data.request.ids.includes(item.id))) selected.add(item.id);
    seenRegions.add(item.id);
  }
  const files = data.files || [], base = data.artifact_base;
  if (base) {
    image("raw", base, "camera_rgb.png", files); image("overlay", base, "detection_paths.png", files);
    for (const [id, file] of [["download-path", "estun_path_preview.csv"], ["download-result", "result.json"], ["download-cycle", "cycle_receipt.json"], ["download-scene", "scene.xml"], ["download-metrics", "perception_metrics.json"]]) {
      const enabled = files.includes(file); $(id).setAttribute("aria-disabled", String(!enabled));
      if (enabled) { $(id).href = base + file; $(id).target = "_blank"; $(id).rel = "noopener"; } else $(id).removeAttribute("href");
    }
  }
  regions(); metrics(data.result); feedback(data.live); evidencePanels(data);
  $("path-count").textContent = `${data.paths?.length || 0} 条预测路径`;
  const hasAfter = files.includes("after_camera_rgb.png");
  $("after-panel").dataset.available = String(hasAfter);
  $("stage-btn-after").hidden = !hasAfter;
  applyStageFilter(hasAfter ? activeStageFilter : activeStageFilter === "after" ? "all" : activeStageFilter);
  if (hasAfter) {
    image("after-raw", base, "after_camera_rgb.png", files, data.result?.prediction_revision || 0); image("after-overlay", base, "after_detection_paths.png", files, data.result?.prediction_revision || 0);
    $("after-state").textContent = `第 ${data.result?.reinspections?.length || 1} 轮加工后复检`;
    const recheck = data.result?.reinspection;
    $("remaining-regions").textContent = `补磨候选 ${recheck?.remaining_paths ?? "—"} 段${recheck?.detected_defect_signals == null ? "" : ` · 检出 ${recheck.detected_defect_signals} 个余量信号`}`;
    const stock = data.result?.final_stock || data.result?.execution?.removal;
    $("removal-detail").textContent = stock ? `生成缺陷峰值 ${stock.initial_peak_mm.toFixed(2)} → ${stock.remaining_peak_mm.toFixed(2)} mm；实际磨具扫掠更新 ${stock.modified_vertices} 个余料顶点。` : "";
    const quality = recheck?.region_reinspection;
    const qualityRows = $("reinspection-region-rows");
    qualityRows.replaceChildren();
    $("reinspection-region-table").hidden = !quality?.regions?.length;
    if (quality) {
      $("remaining-regions").textContent += ` · 判定达标 ${quality.verified}/${quality.regions.length} 区 · ${quality.needs_rework} 区超差 · ${quality.insufficient} 区证据不足`;
      if (Number.isFinite(recheck.supplementary_view_deg)) {
        $("after-state").textContent += ` · 补拍 ${recheck.supplementary_view_deg}°`;
      }
      const labels = { seam: "批缝", burr: "飞边／毛刺", review: "人工复核" };
      const statuses = { verified: "达标", needs_rework: "测得超差", insufficient_observation: "采样不足", uncertain_measurement: "测量不确定" };
      for (const region of quality.regions) {
        const row = document.createElement("tr");
        for (const value of [region.id, labels[region.kind], `${region.observed_anchors} / ${region.anchors}`, region.peak_mm?.toFixed(3) ?? "—", region.upper_peak_mm?.toFixed(3) ?? "—", statuses[region.status]]) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        }
        qualityRows.append(row);
      }
    }
    const residual = stock?.residual_regions?.filter((item) => item.remaining_peak_mm > 0.3).length;
    const followup = data.result?.reinspection?.followup_preflight;
    const stopping = data.result?.controller_stopping;
    const stopMessages = {
      pass_budget_exhausted: "已达到本次加工轮数上限，保留待加工区域。",
      motion_safety_gate: "加工后的实际运动验收未通过，已停止后续补磨；复检候选尚未进行补磨验收。",
      observed_regions_without_candidate_paths: "仍有待加工区域，但当前观测无法生成加工候选路径。",
      followup_safety_rejected: "补磨路径未通过安全验收，本次停止。",
      reinspection_requires_review: "已登记区域仍有超差或观测证据不足，转人工复核。",
      observed_regions_verified: "本轮逐区复检达到观测验收条件。",
    };
    const observedStop = stopping?.reason === "no_observed_excess_above_tolerance" ? `复检未观测到法向余量超过 ${stopping.target_remaining_height_mm} mm 的待加工区域，本次停止。` : stopMessages[stopping?.reason] || "";
    const recognition = data.result?.perception;
    const recognitionReview = stockTargetReached(data.result) && !data.result.goal_complete && recognition?.metrics ? ` 合成几何余量已达标。初始识别匹配 ${recognition.metrics.matched_defect_count} / ${recognition.generated_defects}，观测覆盖 ${recognition.metrics.visible_defect_count} / ${recognition.generated_defects}。` : "";
    $("followup-detail").textContent = (followup ? `补磨验收 ${followup.admitted_count} / ${followup.segment_count} 段；${followup.passed ? "可执行段已进入下一轮。" : "本轮安全检查拒绝这些路径，任务保持部分完成。"}${residual == null ? "" : ` 几何余料评估仍有 ${residual} 个区域超过 0.3 mm。`}` : `${observedStop}${residual == null ? "" : ` 几何余料评估：${residual} 个区域超过 0.3 mm。`}`) + recognitionReview;
  }
  if (stage === "failed") notice(data.status.error || "本次仿真未完成，可调整参数后重新预测。", "error");
  if (stage === "complete") {
    dirty = false;
    if (data.result?.robot_executed) {
      const outcome = taskOutcome(data.result, stage);
      notice(`${outcome.label}。初始验收 ${data.result.admitted_path_count} / ${data.result.path_count} 段，复检生成 ${data.result.reinspection?.remaining_paths ?? "—"} 段补磨候选。`, data.result.motion_gate_passed ? (data.result.goal_complete ? "success" : "warn") : "error");
    }
    else if (!data.paths?.length) {
      const noDefect = data.result?.task_status === "no_defect_detected";
      notice(noDefect ? "本次观测未发现需要加工的区域。" : "当前观测未生成可执行路径，请核对待复核区域和观测覆盖。", noDefect ? "success" : "warn");
    }
    else notice(data.result?.preflight?.orientation?.passed && data.result?.preflight?.passed ? `观测生成 ${data.paths.length} 条候选路径，其中 ${data.result.admitted_path_count} 条通过完整安全验收；其余路径见逐段拒绝原因。` : "已生成工件路径，但整站运动检查未通过。请查看运行记录。", data.result?.preflight?.orientation?.passed && data.result?.preflight?.passed ? "success" : "error");
  }
  controls();
  if (view && base && files.includes("model.json") && files.includes("model.bin") && loadedId !== data.id && loadingId !== data.id) {
    loadController?.abort(); loadController = new AbortController(); loadingId = data.id; $("scene-loading").hidden = false;
    try {
      const manifest = await view.load(base, loadController.signal);
      if (current.id !== data.id || !manifest) return;
      loadedId = data.id; loadingId = null; replay = null; overlayKey = ""; cloudKey = ""; motionKey = "";
      components(manifest); $("model-state").textContent = `${manifest.meshes.length} 组 CAD 网格 · ${(manifest.display_triangle_count / 10000).toFixed(1)} 万三角面`;
      $("scene-loading").hidden = true; view.state(current.live, true); updatePaths(current); await loadEvidence(current);
    } catch (error) { if (error.name !== "AbortError" && current.id === data.id) { loadingId = null; $("scene-loading").textContent = "模型读取失败。点击更新观测与预测重试。"; notice(error.message, "error"); } }
  } else if (sequence === renderSequence && loadedId === data.id) { updatePaths(data); loadEvidence(data).catch((error) => notice(`观测辅助图层读取失败：${error.message}`, "error")); }
}
async function poll(id) {
  clearTimeout(pollTimer);
  try { const data = await fetchJSON(`/api/status?id=${encodeURIComponent(id)}`); if (current?.id !== id) return; render(data); if (!["complete", "failed", "empty"].includes(data.status.stage)) pollTimer = setTimeout(() => poll(id), 250); }
  catch (error) { if (current?.id === id) { notice(`连接中断：${error.message}。正在重连当前仿真。`, "error"); pollTimer = setTimeout(() => poll(id), 2000); } }
}
async function start(execute) {
  if (busy || replaying) return;
  const seed = Number($("seed").value);
  $("parameter-error").hidden = true; $("seed").removeAttribute("aria-invalid");
  if (!Number.isInteger(seed) || seed < 0 || seed > 1000000) { $("parameter-error").textContent = "场景种子请输入 0–1000000 的整数。"; $("parameter-error").hidden = false; $("seed").setAttribute("aria-invalid", "true"); $("seed").focus(); return; }
  if (draft.length) { notice("请先完成或清除正在绘制的选区。", "error"); return; }
  const body = { scene: "workstation", seed, case: $("case").value, casting_profile: $("casting-profile").value, detector: $("detector").value, mode: $("mode").value, views: $("views").value, tool: $("tool").value, workpiece_profile: $("workpiece-profile").value || null, grasp_setup: $("grasp-setup").value, targeted_rescan: $("targeted-rescan").value === "on", roi, execute };
  if (execute) { body.ids = [...selected]; body.source_id = current.id; }
  busy = true; controls(); notice(execute ? "检查所选区域，随后运行抓取、搬运、加工和复检。" : "重新生成工件观测，并预测当前缺陷的打磨轨迹。");
  try {
    const data = await fetchJSON("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    clearTimeout(pollTimer); selected.clear(); current = { id: data.id, request: body }; selectionId = null; await poll(data.id);
  } catch (error) { busy = false; controls(); notice(error.message, "error"); }
}
$("plan").addEventListener("click", () => start(false)); $("execute").addEventListener("click", () => start(true));
$("workpiece-profile").addEventListener("change", () => {
  const profile = workpieceProfiles.get($("workpiece-profile").value);
  if (profile) {
    for (const key of ["case", "detector", "views", "tool"]) $(key).value = profile[key];
    roi = null; draft = []; drawRoi();
  }
  modified("工件配置已切换，请更新观测与预测。");
});
for (const id of ["case", "casting-profile", "detector", "mode", "views", "tool", "grasp-setup", "targeted-rescan"]) $(id).addEventListener("change", () => { if (["case", "detector", "views", "tool"].includes(id)) $("workpiece-profile").value = ""; if (id === "case") { roi = null; draft = []; drawRoi(); } modified("工件、缺陷参数来源、识别模型、工况或采集方式已修改，请更新观测与预测。"); });
$("seed").addEventListener("input", () => modified("种子已修改，将生成新的工件缺陷。"));

function canvasPoint(event) { const box = roiCanvas.getBoundingClientRect(); return [Math.min(1023, Math.max(0, Math.round((event.clientX - box.left) * 1024 / box.width))), Math.min(767, Math.max(0, Math.round((event.clientY - box.top) * 768 / box.height)))]; }
function drawRoi() {
  roiContext.clearRect(0, 0, 1024, 768);
  const points = draft.length ? draft : roi;
  if (points?.length) { roiContext.strokeStyle = "#ffcc59"; roiContext.fillStyle = "#ffcc5928"; roiContext.lineWidth = 4; roiContext.beginPath(); points.forEach((point, index) => index ? roiContext.lineTo(...point) : roiContext.moveTo(...point)); if (roi || drawingTool === "rect") { roiContext.closePath(); roiContext.fill(); } roiContext.stroke(); for (const point of points) { roiContext.beginPath(); roiContext.arc(...point, 5, 0, Math.PI * 2); roiContext.fill(); } }
  $("roi-status").textContent = roi ? `${roi.length} 个顶点的处理范围` : draft.length ? `绘制中 · ${draft.length} 个顶点` : "全工件检测";
}
function setDrawingTool(name) { drawingTool = name; draft = []; rectangleStart = null; roiCanvas.classList.toggle("drawing", name !== "none"); document.querySelectorAll("[data-tool]").forEach((button) => button.classList.toggle("active", button.dataset.tool === name)); drawRoi(); }
document.querySelectorAll("[data-tool]").forEach((button) => button.addEventListener("click", () => setDrawingTool(button.dataset.tool)));
roiCanvas.addEventListener("pointerdown", (event) => { if (busy || drawingTool === "none") return; if (drawingTool === "rect") { rectangleStart = canvasPoint(event); roiCanvas.setPointerCapture(event.pointerId); } else if (draft.length < 100) { draft.push(canvasPoint(event)); drawRoi(); } });
roiCanvas.addEventListener("pointermove", (event) => { if (!rectangleStart) return; const point = canvasPoint(event); draft = [rectangleStart, [point[0], rectangleStart[1]], point, [rectangleStart[0], point[1]]]; drawRoi(); });
roiCanvas.addEventListener("pointerup", (event) => { if (!rectangleStart) return; const point = canvasPoint(event); if (Math.abs(point[0] - rectangleStart[0]) > 3 && Math.abs(point[1] - rectangleStart[1]) > 3) { roi = [rectangleStart, [point[0], rectangleStart[1]], point, [rectangleStart[0], point[1]]]; modified(); } else notice("选区过小，请扩大范围。", "error"); rectangleStart = null; draft = []; drawRoi(); });
$("finish-roi").addEventListener("click", () => { if (draft.length < 3) { notice("选区至少需要三个顶点。", "error"); return; } roi = draft.slice(); draft = []; modified(); drawRoi(); });
$("clear-roi").addEventListener("click", () => { roi = null; draft = []; rectangleStart = null; drawRoi(); modified("已清除处理范围，请更新全工件预测。"); });
$("add-roi-point").addEventListener("click", () => { const x = Number($("roi-x").value), y = Number($("roi-y").value); if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || x > 1023 || y < 0 || y > 767 || draft.length >= 100) { notice("顶点须为 1024 × 768 图像内的整数像素坐标。", "error"); return; } if (drawingTool !== "polygon") setDrawingTool("polygon"); draft.push([x, y]); drawRoi(); });

$("replay").addEventListener("click", async () => {
  if (replaying) { replaying = false; feedback(current.live); controls(); return; }
  try { const file = current.files.includes("replay_compact.json") ? "replay_compact.json" : "replay.json"; replay ||= await fetchJSON(current.artifact_base + file); view?.resetStock(); replayMeshRevision = -1; replaying = true; replayIndex = 0; replayClock = performance.now(); controls(); notice("正在回放实际积分得到的关节与工件反馈。"); replayFrame(); }
  catch (error) { notice(error.message, "error"); }
});
function replayFrame() {
  if (!replaying) return;
  const elapsed = (replay.frames[0]?.simulation_time_s || 0) + (performance.now() - replayClock) / 1000 * 2;
  while (replayIndex < replay.frames.length - 1 && replay.frames[replayIndex].simulation_time_s < elapsed) replayIndex++;
  const frame = replay.frames[replayIndex];
  feedback(replay.mesh_states ? { ...frame, mesh_updates: frame.mesh_revision === replayMeshRevision ? [] : replay.mesh_states[frame.mesh_revision] || [] } : frame);
  replayMeshRevision = frame.mesh_revision;
  if (replayIndex === replay.frames.length - 1) { replaying = false; feedback(current.live); controls(); notice("本次仿真反馈回放完成。", "success"); } else requestAnimationFrame(replayFrame);
}

let activeStageFilter = "all";
function applyStageFilter(stage) {
  activeStageFilter = stage;
  document.querySelectorAll("[data-stage-filter]").forEach((button) => {
    const active = button.dataset.stageFilter === stage;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelectorAll("[data-stage-section]").forEach((section) => {
    const key = section.dataset.stageSection;
    if (key === "after") {
      const available = section.dataset.available === "true";
      section.hidden = !available || (stage !== "all" && stage !== "after");
    } else {
      section.hidden = stage !== "all" && stage !== key;
    }
  });
}
document.querySelectorAll("[data-stage-filter]").forEach((button) => {
  button.addEventListener("click", () => applyStageFilter(button.dataset.stageFilter || "all"));
});

const requestedRun = new URL(location.href).searchParams.get("run");
const loadProfiles = fetchJSON("/api/workpiece-profiles").then((profiles) => {
  for (const [id, profile] of Object.entries(profiles)) {
    workpieceProfiles.set(id, profile);
    $("workpiece-profile").add(new Option(profile.label, id));
  }
}).catch(() => {});
Promise.all([loadProfiles, fetchJSON(requestedRun ? `/api/status?id=${encodeURIComponent(requestedRun)}` : "/api/initial")]).then(([, data]) => { render(data, true); if (data.id && !["complete", "failed", "empty"].includes(data.status.stage)) pollTimer = setTimeout(() => poll(data.id), 350); }).catch((error) => { busy = false; controls(); notice(`无法连接本地仿真：${error.message}`, "error"); });

let activeSession = null, sessionPending = false, sessionHistory = [], sessionHistoryKey = "", sessionTimer = null;
let selectedSessionId = new URL(location.href).searchParams.get("session") || "";

function sessionNotice(message, tone = "") {
  $("session-notice").textContent = message;
  $("session-notice").className = tone;
}

function drawSession() {
  sessionPanels(activeSession, sessionHistory, sessionPending);
}

async function showSession(data) {
  const previousStage = activeSession?.state.stage;
  activeSession = data;
  const historyKey = `${data.session_id}:${data.state.intervention_count}`;
  if (historyKey !== sessionHistoryKey) {
    const history = await fetchJSON(`/api/sessions/${encodeURIComponent(data.session_id)}/interventions`);
    if (selectedSessionId !== data.session_id) return;
    sessionHistory = history.interventions;
    sessionHistoryKey = historyKey;
  }
  if (previousStage !== data.state.stage && ["DETECTED", "PAUSED", "STOPPED_SAFETY"].includes(data.state.stage)) $("session-workspace").open = true;
  drawSession();
  const state = data.state;
  const motion = state.motion_gate_passed === true ? "仿真运动通过" : state.motion_gate_passed === false ? "仿真运动未通过" : "尚未完成运动验收";
  const release = state.observational_release ? "磨后观测放行" : "磨后观测未放行";
  const pendingStop = state.pause_requested || state.abort_requested;
  const message = data.action?.status === "failed" ? data.action.error : pendingStop ? "已请求暂停/中止；执行器将在安全边界退出并保存真实回执。" : data.action?.status === "running" ? `后台${state.operation || data.action.name}进行中；状态查询与暂停保持可用。` : `${motion}；${release}。${state.stop_reason ? `停因：${state.stop_reason}` : "下一步按阶段或当前段一道次推进。"}`;
  sessionNotice(message, data.action?.status === "failed" || state.stop_kind === "motion" || state.stop_kind === "retract" ? "error" : state.stage === "STOPPED_SAFETY" ? "warn" : "");
}

async function pollSession(identity) {
  clearTimeout(sessionTimer);
  if (!identity || selectedSessionId !== identity) return;
  try {
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(identity)}`);
    if (selectedSessionId !== identity) return;
    await showSession(data);
    if (data.action?.status === "running" || data.state.pause_requested || data.state.abort_requested) sessionTimer = setTimeout(() => pollSession(identity), 750);
  } catch (error) {
    if (selectedSessionId === identity) { sessionNotice(`会话连接中断：${error.message}`, "error"); sessionTimer = setTimeout(() => pollSession(identity), 2500); }
  }
}

async function refreshSessionList() {
  const data = await fetchJSON("/api/sessions");
  const select = $("session-select");
  select.replaceChildren(new Option("选择已有会话", ""));
  for (const item of data.sessions) select.add(new Option(`${item.session_id}  ${item.stage}${item.action?.status === "running" ? "（后台运行）" : ""}`, item.session_id));
  select.value = selectedSessionId;
}

async function chooseSession(identity) {
  clearTimeout(sessionTimer);
  selectedSessionId = identity;
  activeSession = null; sessionHistory = []; sessionHistoryKey = "";
  sessionPending = true; drawSession();
  try {
    if (!identity) { sessionNotice("选择已有会话，或用当前工况新建会话。"); return; }
    sessionNotice("正在载入持久化会话与介入记录。");
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(identity)}`);
    if (selectedSessionId !== identity) return;
    await showSession(data);
    const url = new URL(location.href); url.searchParams.set("session", identity); history.replaceState(null, "", url);
    if (data.action?.status === "running") sessionTimer = setTimeout(() => pollSession(identity), 750);
  } catch (error) { sessionNotice(error.message, "error"); }
  finally { sessionPending = false; drawSession(); }
}

async function sessionCommand(route, body = {}) {
  if (!activeSession || sessionPending) return;
  const identity = selectedSessionId;
  sessionPending = true; drawSession();
  try {
    const data = await fetchJSON(`/api/sessions/${encodeURIComponent(identity)}/${route}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (selectedSessionId !== identity) return;
    await showSession(data);
    if (data.action?.status === "running" || data.state.pause_requested || data.state.abort_requested) sessionTimer = setTimeout(() => pollSession(identity), 400);
  } catch (error) { sessionNotice(error.message, "error"); }
  finally { sessionPending = false; drawSession(); }
}

$("session-select").addEventListener("change", () => chooseSession($("session-select").value));
$("session-refresh").addEventListener("click", () => { refreshSessionList().then(() => selectedSessionId && pollSession(selectedSessionId)).catch((error) => sessionNotice(error.message, "error")); });
$("session-create").addEventListener("click", async () => {
  const seed = Number($("seed").value);
  if (!Number.isInteger(seed) || seed < 0 || seed > 1000000) { sessionNotice("种子须为 0–1000000 的整数。", "error"); return; }
  if (draft.length) { sessionNotice("请先完成或清除工况 ROI 草稿。", "error"); return; }
  sessionPending = true; drawSession();
  try {
    const data = await fetchJSON("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seed, case: $("case").value, mode: $("mode").value, views: $("views").value, tool: $("tool").value, casting_profile: $("casting-profile").value, detector: $("detector").value, workpiece_profile: $("workpiece-profile").value || null, grasp_setup: $("grasp-setup").value, targeted_rescan: $("targeted-rescan").value === "on", roi }) });
    selectedSessionId = data.session_id;
    await refreshSessionList();
    await showSession(data);
    const url = new URL(location.href); url.searchParams.set("session", data.session_id); history.replaceState(null, "", url);
  } catch (error) { sessionNotice(error.message, "error"); }
  finally { sessionPending = false; drawSession(); }
});
for (const [id, route] of [["session-step", "step"], ["session-advance", "advance"], ["session-pause", "pause"], ["session-resume", "resume"], ["session-replan", "replan"]]) $(id).addEventListener("click", () => sessionCommand(route));
$("session-abort").addEventListener("click", () => { if (window.confirm("中止后此会话不能继续执行。是否安全退刀并中止？")) sessionCommand("abort"); });
$("session-approve").addEventListener("click", () => {
  const region_ids = [...$("session-regions").querySelectorAll("input:checked:not(:disabled)")].map((input) => Number(input.value));
  if (!region_ids.length) { sessionNotice("请选择可加工观测区域。", "error"); return; }
  sessionCommand("regions/approve", { region_ids });
});
$("session-regions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-session-mask]");
  if (button && !button.disabled) sessionCommand(`regions/${button.dataset.sessionMask}/mask`, { reason: $("session-reason").value.trim() });
});
$("session-segment-select").addEventListener("change", () => sessionEditor(activeSession, sessionPending || activeSession?.action?.status === "running"));
$("session-segment-rows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-session-segment]");
  if (button) { $("session-segment-select").value = button.dataset.sessionSegment; sessionEditor(activeSession, sessionPending || activeSession?.action?.status === "running"); }
});
const currentSessionSegment = () => $("session-segment-select").value;
$("session-save-params").addEventListener("click", () => {
  const body = { feed_scale: Number($("session-feed").value), normal_offset_mm: Number($("session-offset").value), passes: Number($("session-passes").value), dwell_scale: Number($("session-dwell").value) };
  if (Object.values(body).some((value) => !Number.isFinite(value)) || !Number.isInteger(body.passes)) { sessionNotice("段参数须为有限数，目标道次须为整数。", "error"); return; }
  sessionCommand(`segments/${currentSessionSegment()}/params`, body);
});
$("session-skip").addEventListener("click", () => sessionCommand(`segments/${currentSessionSegment()}/skip`, { reason: $("session-reason").value.trim() }));
$("session-switch-grasp").addEventListener("click", () => sessionCommand(`segments/${currentSessionSegment()}/grasp`, { grasp_setup: $("session-grasp").value }));
$("session-split").addEventListener("click", () => {
  const ratio = Number($("session-split-ratio").value);
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio >= 1) { sessionNotice("切段比例须在 0 与 1 之间。", "error"); return; }
  sessionCommand(`segments/${currentSessionSegment()}/split`, { ratio });
});
$("session-rescan").addEventListener("click", () => {
  const text = $("session-angles").value.trim();
  const angles_deg = text ? text.split(/[,，\s]+/).filter(Boolean).map(Number) : null;
  const extra_frames = Number($("session-extra-frames").value);
  if ((angles_deg && angles_deg.some((angle) => !Number.isFinite(angle) || angle < 0 || angle > 360)) || !Number.isInteger(extra_frames) || extra_frames < 0 || extra_frames > 8) { sessionNotice("补扫角度须为 0–360°，额外帧须为 0–8 的整数。", "error"); return; }
  sessionCommand("rescan", { angles_deg, extra_frames });
});
$("session-add-roi").addEventListener("click", () => {
  try { const polygon = JSON.parse($("session-manual-roi").value); sessionCommand("regions/manual", { roi_spec: { polygon, view_index: 0, reason: $("session-reason").value.trim() } }); }
  catch { sessionNotice("ROI 须为 JSON 像素顶点数组，例如 [[300,250],[600,250],[600,400]]。", "error"); }
});
drawSession();
refreshSessionList().then(() => selectedSessionId && chooseSession(selectedSessionId)).catch((error) => sessionNotice(`会话接口暂不可用：${error.message}`, "error"));
