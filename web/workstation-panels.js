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
