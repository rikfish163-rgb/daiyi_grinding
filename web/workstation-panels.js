const $ = (id) => document.getElementById(id);
let galleryKey = "";
let rowsKey = "";
let returnFocus = null;
let segmentFilter = "all";
let latestData = null;

$("close-view-dialog").addEventListener("click", () => $("view-dialog").close());
$("view-dialog").addEventListener("close", () => returnFocus?.focus());
$("view-dialog").addEventListener("click", (event) => {
  if (event.target === $("view-dialog")) $("view-dialog").close();
});

function renderSegmentTable(data) {
  const views = data?.views || [];
  const plan = data?.result?.preflight || data?.planning || {};
  const allRows = plan.rows || [];
  const passedCount = allRows.filter((row) => row.status === "passed").length;
  const blockedCount = allRows.length - passedCount;

  document.querySelectorAll("[data-seg-filter]").forEach((button) => {
    const filter = button.dataset.segFilter;
    const active = filter === segmentFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    if (filter === "all") button.textContent = allRows.length ? `全部 (${allRows.length})` : "全部";
    else if (filter === "passed") button.textContent = allRows.length ? `完整通过 (${passedCount})` : "完整通过";
    else if (filter === "blocked") button.textContent = allRows.length ? `受阻 (${blockedCount})` : "受阻";
  });

  const filtered = allRows.filter((row) => {
    if (segmentFilter === "passed") return row.status === "passed";
    if (segmentFilter === "blocked") return row.status !== "passed";
    return true;
  });

  const key = JSON.stringify([segmentFilter, filtered.map((row) => [row.segment_id, row.status, row.reason])]);
  if (key === rowsKey) return;
  rowsKey = key;

  $("segment-rows").replaceChildren();
  for (const row of filtered) {
    const tr = document.createElement("tr");
    const view = views[row.view_index];
    const passed = row.status === "passed";

    const tdSeg = document.createElement("td");
    tdSeg.className = "num";
    tdSeg.textContent = `#${row.segment_id}`;

    const tdReg = document.createElement("td");
    tdReg.textContent = `区域 ${row.detection_id}`;

    const tdView = document.createElement("td");
    tdView.className = "num";
    tdView.textContent = view ? `${view.encoder_angle_deg.toFixed(0)}°` : "—";

    const tdStatus = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = passed ? "status-badge pass" : "status-badge blocked";
    badge.textContent = passed ? "完整通过" : "受阻";
    tdStatus.append(badge);

    const tdDetail = document.createElement("td");
    tdDetail.className = "segment-detail-cell";
    tdDetail.textContent = passed
      ? `工件转姿 ${row.roll_deg ?? "—"}°，检查 ${row.attempts ?? 1} 次候选`
      : row.reason;

    tr.append(tdSeg, tdReg, tdView, tdStatus, tdDetail);
    $("segment-rows").append(tr);
  }

  if (!filtered.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "table-empty";
    if (!allRows.length) {
      td.textContent = data?.result
        ? "本次没有可加工路径，请查看观测覆盖和检测结果。"
        : "逐段检查中，完成的段会在此显示。";
    } else {
      td.textContent = segmentFilter === "passed" ? "当前筛选下没有通过验收的路径段。" : "当前筛选下没有受阻路径段。";
    }
    tr.append(td);
    $("segment-rows").append(tr);
  }
}

document.querySelectorAll("[data-seg-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    segmentFilter = button.dataset.segFilter || "all";
    rowsKey = "";
    if (latestData) renderSegmentTable(latestData);
  });
});

export function evidencePanels(data) {
  latestData = data;
  const views = data.views || [];
  const galleryVersion = `${data.id}:${views.length}`;
  if (galleryVersion !== galleryKey) {
    galleryKey = galleryVersion;
    $("scan-views").replaceChildren();
    for (const view of views) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "scan-view";

      const thumb = document.createElement("div");
      thumb.className = "scan-thumb";
      const image = document.createElement("img");
      image.src = data.artifact_base + view.overlay;
      image.alt = `${view.encoder_angle_deg.toFixed(0)}° 工件缺陷与路径`;
      const angleBadge = document.createElement("span");
      angleBadge.className = "scan-angle";
      angleBadge.textContent = `${view.encoder_angle_deg.toFixed(1)}°`;
      thumb.append(image, angleBadge);

      const meta = document.createElement("div");
      meta.className = "scan-meta";
      const counts = document.createElement("span");
      counts.textContent = `${view.detections} 区域 / ${view.paths} 路径`;
      const pts = document.createElement("small");
      pts.textContent = `${(view.point_count / 1000).toFixed(1)}k 点`;
      meta.append(counts, pts);

      button.append(thumb, meta);
      button.addEventListener("click", () => {
        returnFocus = button;
        $("view-dialog-title").textContent = `编码器 ${view.encoder_angle_deg.toFixed(2)}° 固定相机观测`;
        $("view-dialog-image").src = data.artifact_base + view.overlay;
        $("view-dialog-detail").textContent = `${view.point_count.toLocaleString()} 个有效表面点；${view.detections} 个检测区域，${view.paths} 段预测路径。点云与工件坐标按该帧编码器位姿关联。`;
        $("view-dialog").showModal();
      });
      $("scan-views").append(button);
    }
    if (!views.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "正在采集第一帧 RGB-D";
      $("scan-views").append(empty);
    }
  }

  const expectedFrames = data.result?.views === "single" || data.request?.views === "single" ? 1 : 6;
  $("scan-detail").textContent = `已采集 ${views.length} / ${expectedFrames} 帧，按编码器位姿统一至工件局部坐标系`;

  const plan = data.result?.preflight || data.planning || {};
  $("segment-summary").textContent = plan.segment_count
    ? `${plan.admitted_count || 0} / ${plan.segment_count} 段通过，${plan.blocked_count ?? "…"} 段受阻`
    : "等待路径验收";
  renderSegmentTable(data);
}

const sessionStageNames = { IDLE: "等待扫描", SCANNED: "扫描已保存", DETECTED: "区域待审阅", PLANNED: "逐段预检完成", EXECUTING: "执行 / 等待下一道次", RESCANNED: "磨后复检", DONE: "有界流程结束", PAUSED: "已安全暂停", ABORTED: "已中止", STOPPED_SAFETY: "安全门停止" };
const sessionStatusNames = { pending: "待执行", blocked: "预检拒绝", executing: "执行中", executed: "完整执行", skipped: "已跳过", superseded: "已切分", partial: "部分覆盖" };
const sessionActionNames = { approve_regions: "审批区域", mask_false_positive: "屏蔽误检", add_manual_region: "登记手工区域", rescan_views: "补扫", set_segment_params: "调整段参数", split_segment: "切段", switch_grasp: "切换夹持", skip_segment: "跳过段", replan_remaining: "重新预检", pause: "暂停退刀", safe_retract: "安全退刀", resume: "恢复", abort: "中止", process_skip: "观测建议零道次", process_evidence_unavailable: "插件证据待补" };
let sessionRowsVersion = "", sessionRegionsVersion = "", sessionAuditVersion = "", sessionEditorVersion = "";

export function sessionEditor(data, locked = false) {
  const state = data?.state;
  const segment = state?.segments.find((item) => String(item.segment_id) === $("session-segment-select").value);
  const editable = !!segment && ["pending", "blocked"].includes(segment.status) && !locked && !["DONE", "ABORTED"].includes(state.stage);
  const version = JSON.stringify([data?.session_id, segment?.segment_id, segment?.params, segment?.grasp_setup]);
  if (version !== sessionEditorVersion && segment) {
    sessionEditorVersion = version;
    $("session-feed").value = segment.params.feed_scale;
    $("session-offset").value = segment.params.normal_offset_mm;
    $("session-passes").value = segment.params.passes;
    $("session-dwell").value = segment.params.dwell_scale;
    $("session-grasp").value = segment.grasp_setup;
  }
  for (const id of ["session-feed", "session-offset", "session-passes", "session-dwell", "session-save-params", "session-skip", "session-grasp", "session-switch-grasp", "session-split-ratio", "session-split"]) $(id).disabled = !editable;
  $("session-switch-grasp").disabled = !editable || !state.safe_retracted;
  $("session-split").disabled = !editable || segment.completed_passes > 0;
  $("session-offset").disabled = !editable || state.config.mode === "clearance";
  $("session-preflight").textContent = !segment ? "选择路径段查看参数与预检。" : segment.status === "partial" ? `父段尚有 ${(100 * segment.uncovered_source_fraction).toFixed(1)}% 未覆盖，保留在未完成集合中。` : segment.needs_preflight ? "待强制重检：碰撞、限位、保护区与当前 PCHIP 绑定。" : segment.preflight?.passed ? "当前预检通过；实际执行前仍重查当前场景与连续曲线。" : segment.preflight?.reason || "尚未预检。";
}

export function sessionPanels(data, interventions = [], requestPending = false) {
  const state = data?.state;
  const running = data?.action?.status === "running";
  const locked = requestPending || running;
  const terminal = !state || ["DONE", "ABORTED"].includes(state.stage);
  $("session-stage").textContent = state ? `${state.stage}  ${sessionStageNames[state.stage]}` : "未选择会话";
  $("session-summary").textContent = state ? `工件 ${state.workpiece_id}  已完整执行 ${state.executed_segments.length} 段  未完成 ${state.remaining_segments.length} 段  检查点 ${state.checkpoint_index}` : "逐段推进，检查点可重载";
  $("session-step").disabled = terminal || locked || ["PAUSED", "STOPPED_SAFETY"].includes(state.stage);
  $("session-advance").disabled = $("session-step").disabled;
  $("session-pause").disabled = terminal || requestPending;
  $("session-resume").disabled = terminal || locked || !["PAUSED", "STOPPED_SAFETY"].includes(state.stage) || ["motion", "retract"].includes(state.stop_kind);
  $("session-abort").disabled = terminal || requestPending;
  $("session-replan").disabled = terminal || locked || ["IDLE", "SCANNED"].includes(state.stage) || ["motion", "retract"].includes(state.stop_kind);
  $("session-approve").disabled = terminal || locked || !state.regions.some((item) => item.machining_required && !item.masked);
  $("session-rescan").disabled = terminal || locked || state.stage === "IDLE" || !state.safe_retracted;
  $("session-add-roi").disabled = terminal || locked || ["IDLE", "SCANNED"].includes(state.stage) || !state.safe_retracted;
  $("session-select").disabled = requestPending;
  $("session-create").disabled = requestPending;
  $("session-refresh").disabled = requestPending;
  const phases = ["IDLE", "SCANNED", "DETECTED", "PLANNED", "EXECUTING", "RESCANNED", "DONE"];
  const activePhase = state?.stage === "PAUSED" ? state.resume_stage : state?.stage;
  $("session-phases").replaceChildren(...phases.map((phase) => {
    const item = document.createElement("li");
    item.textContent = sessionStageNames[phase];
    if (phase === activePhase) { item.className = "is-current"; item.setAttribute("aria-current", "step"); }
    return item;
  }));
  for (const [id, file] of [["session-checkpoint", "session_state.json"], ["session-audit-download", "interventions.jsonl"]]) {
    const anchor = $(id);
    anchor.setAttribute("aria-disabled", String(!data?.artifact_base));
    if (data?.artifact_base) { anchor.href = data.artifact_base + file; anchor.target = "_blank"; anchor.rel = "noopener"; }
    else anchor.removeAttribute("href");
  }
  if (!state) return;
  const regionsVersion = JSON.stringify([data.session_id, state.regions, locked, terminal]);
  if (regionsVersion !== sessionRegionsVersion) {
    sessionRegionsVersion = regionsVersion;
    $("session-regions").replaceChildren();
    for (const region of state.regions) {
      const row = document.createElement("div"); row.className = "session-region";
      const label = document.createElement("label");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = region.id;
      const supported = region.machining_required && ["seam", "burr"].includes(region.kind) && !region.masked;
      checkbox.checked = region.approval === "approved" || (supported && region.approval === "pending");
      checkbox.disabled = !supported || locked || terminal;
      checkbox.setAttribute("aria-label", `审批区域 ${region.id}`);
      const text = document.createElement("span");
      const title = document.createElement("strong"); title.textContent = `区域 ${region.id}  ${region.kind === "seam" ? "批缝" : region.kind === "burr" ? "毛刺" : region.kind === "review" ? "待复核" : "保留结构"}`;
      const detail = document.createElement("small"); detail.textContent = `${region.masked ? "已屏蔽" : region.approval === "approved" ? "已批准" : "待审批"}${Number.isFinite(region.normal_height_mm) ? `，法向余量 ${region.normal_height_mm.toFixed(3)} mm` : ""}${region.manual ? "，手工区域" : ""}`;
      text.append(title, detail); label.append(checkbox, text);
      const mask = document.createElement("button"); mask.type = "button"; mask.textContent = "屏蔽误检"; mask.dataset.sessionMask = region.id;
      mask.disabled = locked || terminal || region.masked;
      row.append(label, mask); $("session-regions").append(row);
    }
    if (!state.regions.length) { const p = document.createElement("p"); p.className = "empty"; p.textContent = "扫描并检测后显示登记区域。"; $("session-regions").append(p); }
  }
  const rowsVersion = `${data.session_id}:${state.checkpoint_index}:${locked}`;
  if (rowsVersion !== sessionRowsVersion) {
    sessionRowsVersion = rowsVersion;
    const selected = $("session-segment-select").value;
    $("session-segment-select").replaceChildren(...state.segments.map((segment) => new Option(`#${segment.segment_id}  区域 ${segment.region_id}  ${sessionStatusNames[segment.status]}`, segment.segment_id)));
    $("session-segment-select").value = state.segments.some((item) => String(item.segment_id) === selected) ? selected : String(state.segments.find((item) => ["pending", "blocked"].includes(item.status))?.segment_id || state.segments[0]?.segment_id || "");
    $("session-segment-rows").replaceChildren();
    for (const segment of state.segments) {
      const tr = document.createElement("tr");
      const target = document.createElement("td"), button = document.createElement("button");
      button.type = "button"; button.textContent = `#${segment.segment_id} / 区域 ${segment.region_id}`; button.dataset.sessionSegment = segment.segment_id; target.append(button);
      const status = document.createElement("td"); status.textContent = sessionStatusNames[segment.status];
      const params = document.createElement("td"); params.textContent = `${segment.completed_passes}/${segment.params.passes} 道次，${segment.grasp_setup}${segment.split_from ? `，源段 #${segment.split_from}` : ""}`;
      const detail = document.createElement("td"); detail.className = "segment-detail-cell";
      detail.textContent = segment.needs_preflight ? "待重检" : segment.preflight?.passed ? `当前连续曲线通过，覆盖 ${((segment.preflight.source_coverage_ratio ?? 1) * 100).toFixed(1)}%` : segment.preflight?.reason || segment.skip_reason || "尚未预检";
      tr.append(target, status, params, detail); $("session-segment-rows").append(tr);
    }
  }
  sessionEditor(data, locked);
  const auditVersion = `${data.session_id}:${state.intervention_count}:${interventions.length}`;
  if (auditVersion !== sessionAuditVersion) {
    sessionAuditVersion = auditVersion;
    $("session-interventions").replaceChildren();
    for (const entry of interventions.slice(-30).reverse()) {
      const item = document.createElement("li");
      const header = document.createElement("strong"); header.textContent = `${new Date(entry.timestamp).toLocaleTimeString()}  ${sessionActionNames[entry.action] || entry.action}  ${entry.target_id == null ? "会话" : `目标 ${entry.target_id}`}`;
      const text = document.createElement("span"); text.textContent = `${entry.reason || ""}${Object.keys(entry.diff).length ? `  ${JSON.stringify(entry.diff)}` : ""}`;
      item.append(header, text); $("session-interventions").append(item);
    }
    if (!interventions.length) { const item = document.createElement("li"); item.className = "muted"; item.textContent = "尚无介入。"; $("session-interventions").append(item); }
  }
  const image = $("session-observation");
  image.hidden = !data.artifact_base || !state.scan_snapshot;
  if (!image.hidden) {
    const folder = state.scan_snapshot.slice(0, state.scan_snapshot.lastIndexOf("/"));
    const source = data.artifact_base + folder + "/camera_rgb.png";
    if (image.getAttribute("src") !== source) image.src = source;
  }
}
