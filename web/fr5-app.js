"use strict";
const $ = (id) => document.getElementById(id);
const labels = {seam:"薄批缝",burr:"局部毛刺",preserve:"保留结构",review:"待复核"};
const stages = {queued:"等待启动",capturing:"采集相机观测",planning:"路径与运动检查",executing:"仿真执行中",finalizing:"保存运行记录",complete:"运行完成",failed:"运行失败",empty:"尚无运行记录"};
let current = null, roi = null, draft = [], tool = "none", rectStart = null, busy = false, dirty = false, selected = new Set(), selectionRunId = null, pollTimer = null;
let videoRunId = null, videoTimer = null;
const canvas = $("roi-canvas"), ctx = canvas.getContext("2d");
function notice(message, type="") { $("notice").textContent=message; $("notice").className=type; }
function controls() {
  $("plan").disabled=busy;
  $("execute").disabled=busy || dirty || !current?.result || !selected.size || !current.paths?.length;
  [$("case"),$("seed"),$("select-auto"),$("select-none"),$("clear-roi"),$("finish-roi")].forEach(el=>el.disabled=busy);
  document.querySelectorAll("[data-tool],.region input").forEach(el=>el.disabled=busy || (el.dataset.supported==="false"));
  $("plan").textContent=busy ? "正在运行…" : "重新拍照与规划";
  $("execute").textContent=busy && current?.request?.execute ? "仿真执行中…" : "验证并仿真执行";
}
function modified(message="选区已修改，请重新拍照与规划。") { dirty=true; controls(); notice(message); }
function point(event) { const r=canvas.getBoundingClientRect(); return [Math.min(767,Math.max(0,Math.round((event.clientX-r.left)*768/r.width))),Math.min(575,Math.max(0,Math.round((event.clientY-r.top)*576/r.height)))]; }
function draw() {
  ctx.clearRect(0,0,768,576); const points=draft.length?draft:roi;
  if (points?.length) {ctx.strokeStyle="#ffcf6b";ctx.fillStyle="#ffcf6b24";ctx.lineWidth=3;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));if(roi || tool==="rect"){ctx.closePath();ctx.fill();}ctx.stroke();for(const p of points){ctx.beginPath();ctx.arc(...p,4,0,Math.PI*2);ctx.fillStyle="#ffcf6b";ctx.fill();}}
  $("roi-status").textContent=roi?`${roi.length} 点 ROI`:(draft.length?`绘制中 · ${draft.length} 点`:"全图自动选区");
}
document.querySelectorAll("[data-tool]").forEach(button=>button.addEventListener("click",()=>{tool=button.dataset.tool;draft=[];rectStart=null;canvas.classList.toggle("drawing",tool!=="none");document.querySelectorAll("[data-tool]").forEach(b=>b.classList.toggle("active",b===button));draw();}));
canvas.addEventListener("pointerdown",event=>{if(busy || tool==="none" || !current)return;if(tool==="rect"){rectStart=point(event);canvas.setPointerCapture(event.pointerId);}else if(draft.length<100){draft.push(point(event));draw();}});
canvas.addEventListener("pointermove",event=>{if(!rectStart || tool!=="rect")return;const p=point(event);draft=[rectStart,[p[0],rectStart[1]],p,[rectStart[0],p[1]]];draw();});
canvas.addEventListener("pointerup",event=>{if(!rectStart)return;const p=point(event);if(Math.abs(p[0]-rectStart[0])<3 || Math.abs(p[1]-rectStart[1])<3){draft=[];notice("选区过小，请稍扩大后重试。");}else{roi=[rectStart,[p[0],rectStart[1]],p,[rectStart[0],p[1]]];draft=[];modified();}rectStart=null;draw();});
canvas.addEventListener("dblclick",()=>finishPolygon());
function finishPolygon(){if(tool!=="polygon" || draft.length<3){notice("多边形至少需要三个顶点。");return;}roi=draft.slice();draft=[];modified();draw();}
$("finish-roi").addEventListener("click",finishPolygon);
$("clear-roi").addEventListener("click",()=>{roi=null;draft=[];rectStart=null;draw();modified("已清除 ROI，请重新拍照与规划全图。");});
$("case").addEventListener("change",()=>{roi=null;draft=[];draw();modified("场景已修改，请重新拍照与规划。");});
$("seed").addEventListener("input",()=>modified("场景种子已修改，请重新拍照与规划。"));
function regions(){
  const list=$("region-list");list.replaceChildren();const dets=current?.detections || [];
  $("region-count").textContent=`${selected.size} / ${dets.length} 已选`;
  if(!dets.length){const p=document.createElement("p");p.className="empty";p.textContent="未发现可处理缺陷。正常结构场景不会生成运动路径。";list.appendChild(p);}
  for(const d of dets){const supported=["seam","burr"].includes(d.kind);const label=document.createElement("label");label.className="region";label.style.flexDirection="row";const check=document.createElement("input");check.type="checkbox";check.checked=selected.has(d.id);check.disabled=!supported || busy;check.dataset.supported=String(supported);check.setAttribute("aria-label",`选择区域 ${d.id} ${labels[d.kind] || d.kind}`);check.addEventListener("change",()=>{check.checked?selected.add(d.id):selected.delete(d.id);$("region-count").textContent=`${selected.size} / ${dets.length} 已选`;controls();notice("已更新区域选择；执行时将对所选区域重新规划并检查。");});const content=document.createElement("div");content.className="region-content";const title=document.createElement("div");title.className="region-title";const name=document.createElement("span");name.textContent=`区域 ${String(d.id).padStart(2,"0")}`;const tag=document.createElement("span");tag.className=`tag ${d.kind}`;tag.textContent=labels[d.kind] || d.kind;title.append(name,tag);const dimensions=document.createElement("div");dimensions.className="dimensions";dimensions.textContent=`长 ${d.length_mm.toFixed(1)} · 宽 ${d.width_mm.toFixed(1)} · 高 ${d.height_mm.toFixed(2)} mm`;content.append(title,dimensions);label.append(check,content);list.append(label);}
}
$("select-auto").addEventListener("click",()=>{selected=new Set((current?.detections || []).filter(d=>["seam","burr"].includes(d.kind)).map(d=>d.id));regions();controls();});
$("select-none").addEventListener("click",()=>{selected.clear();regions();controls();notice("已清空选择，不会提交运动执行。");});
function metrics(result){const root=$("metrics");root.replaceChildren();const rows=[
  ["可处理区域",String(result?.perception?.auto_regions ?? "—"),"个"],
  ["规划轨迹",String(result?.path_count ?? "—"),"条"],
  ["处理路径长度",result?.path_length_mm?.toFixed(1) ?? "—","mm"],
  ["沿轨迹误差 P95",result?.execution?.tracking_trace_p95_mm?.toFixed(3) ?? "—","mm"],
  ["非预期接触",String(result?.execution?.unexpected_contact_count ?? "—"),"次"],
  ["运动验证",result?.motion_gate_passed===true?"通过":result?.motion_gate_passed===false?"未通过":result?.execution?"已执行":"待执行",""]];
  for(const [name,value,unit] of rows){const cell=document.createElement("div");cell.className="metric"+(value==="通过"?" pass":"");const title=document.createElement("span");title.textContent=name;const strong=document.createElement("strong");strong.textContent=value;const small=document.createElement("small");small.textContent=unit;strong.append(small);cell.append(title,strong);root.append(cell);}}
async function compatibleVideo(id){
  try{const data=await fetchJSON(`/api/video?id=${encodeURIComponent(id)}`);if(current?.id!==id)return;if(data.stage==="complete"){$("video").src=data.url;$("video").hidden=false;$("video-empty").hidden=true;$("video-state").textContent="仿真录像 · 浏览器兼容格式";}else{$("video-empty").textContent="正在生成本地浏览器兼容录像…";videoTimer=setTimeout(()=>compatibleVideo(id),1600);}}
  catch(error){if(current?.id===id){$("video-empty").textContent=error.message;$("video-state").textContent="可下载原始录像";}}
}
function updateVideo(data){
  if(videoRunId===data.id)return;videoRunId=data.id;clearTimeout(videoTimer);$("video").poster=data.artifact_base+"scene.png";
  if($("video").canPlayType("video/mp4")){$("video").src=data.artifact_base+"demo.mp4";$("video").hidden=false;$("video-empty").hidden=true;$("video-state").textContent=data.saved_demo?"已保存的仿真录像":"本次仿真录像";}
  else{$("video").hidden=true;$("video-empty").hidden=false;compatibleVideo(data.id);}
}
$("video").addEventListener("error",()=>{if(current?.id && $("video").getAttribute("src")?.endsWith(".mp4")){$("video").hidden=true;$("video-empty").hidden=false;compatibleVideo(current.id);}});
function render(data, initialize=false){
  const newRun=current?.id!==data.id;current=data;
  const stage=data.status?.stage || "empty";busy=!["complete","failed","empty"].includes(stage);
  $("record-kind").textContent=data.saved_demo?"已保存的演示记录":"本次运行 · 新观测";
  $("run-state").textContent=stages[stage] || stage;
  $("run-detail").textContent=stage==="executing"?`仿真时间 ${(data.status.simulation_time_s || 0).toFixed(1)} s`:`球磨头 Ø6 mm · 间隙 ${data.result?.clearance_mm ?? 3} mm`;
  if(initialize || (selectionRunId!==data.id && data.detections?.length)){selected=new Set((data.detections || []).filter(d=>["seam","burr"].includes(d.kind) && (!data.request?.ids || data.request.ids.includes(d.id))).map(d=>d.id));selectionRunId=data.id;}
  if(initialize && data.result){$("seed").value=data.result.seed;$("case").value=data.result.case;roi=data.result.roi || null;draw();}
  if(newRun || initialize || stage==="complete" || data.files?.includes("camera_rgb.png")){
    for(const [id,file] of [["raw","camera_rgb.png"],["overlay","detection_paths.png"],["scene","scene.png"]]){if(data.files?.includes(file))$(id).src=data.artifact_base+file;}
    if(data.files?.includes("demo.mp4") && stage==="complete"){updateVideo(data);}else{videoRunId=null;clearTimeout(videoTimer);$("video").hidden=true;$("video-empty").hidden=false;$("video-empty").textContent="完成仿真执行后生成录像。当前规划不产生机器人运动。";$("video-state").textContent=busy?"运行完成后生成录像":"当前记录仅完成感知与规划";}
    $("download-path").href=data.artifact_base+"fr5_path_preview.csv";$("download-result").href=data.artifact_base+"result.json";regions();metrics(data.result);
  }
  if(stage==="failed")notice(data.status.error || "运行失败，请重试。","error");
  else if(stage==="complete" && !data.saved_demo){dirty=false;const n=data.result?.path_count ?? 0;notice(n?`已完成${data.request?.execute?"仿真验证":"拍照与规划"}，生成 ${n} 条路径。`:(roi?"当前 ROI 内无可用路径。请扩大选区以容纳磨头包络，或清除 ROI 后重试。":"未发现可处理缺陷，未产生运动。"),"success");}
  controls();
}
async function fetchJSON(url, options){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.error || `请求失败 ${response.status}`);return data;}
async function poll(id){clearTimeout(pollTimer);try{const data=await fetchJSON(`/api/status?id=${encodeURIComponent(id)}`);render(data);if(busy)pollTimer=setTimeout(()=>poll(id),1100);}catch(error){busy=false;controls();notice(error.message,"error");}}
async function run(execute){
  if(busy)return;const seed=Number($("seed").value);if(!Number.isInteger(seed) || seed<0 || seed>100000){notice("请输入 0–100000 的整数种子。","error");return;}
  if(draft.length){notice("请先完成或清除正在绘制的多边形。","error");return;}
  const body={scene:"fr5",seed,case:$("case").value,roi,execute};
  if(execute){body.ids=[...selected];body.source_id=current.id;}
  busy=true;$("run-state").textContent=stages.queued;$("record-kind").textContent="本次运行 · 新观测";controls();notice(execute?"正在重新规划所选区域并检查运动；通过后开始仿真。":"正在采集新图像并根据当前 ROI 规划路径…");
  try{const started=await fetchJSON("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});selected.clear();current={id:started.id,request:body};for(const id of ["raw","overlay","scene"])$(id).removeAttribute("src");await poll(started.id);}catch(error){busy=false;controls();notice(error.message,"error");}
}
$("plan").addEventListener("click",()=>run(false));$("execute").addEventListener("click",()=>run(true));
fetchJSON("/api/initial?scene=fr5").then(data=>{render(data,true);if(busy && data.id)poll(data.id);}).catch(error=>{busy=false;controls();notice(error.message,"error");});
