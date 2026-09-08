/* Android 自动化脚本编辑器 - 前端逻辑 (原生 JS, 无构建) */
"use strict";

// ---------- 状态 ----------
const state = {
  script: { name: "未命名脚本", device: "", step_delay_ms: 300, steps: [] },
  devices: [],
  mode: "coord",        // coord | widget
  widgets: [],          // uidump 节点
  shotScale: 1,         // 截图显示缩放比 (设备px → 屏幕px)
  shotNatural: null,    // 设备分辨率 {w, h}
  shotImg: null,        // HTMLImageElement, 用于精确缩放计算
  selectedIdx: -1,      // 步骤列表中当前选中项（用于"在此插入"）
  editorAutoRun: true,  // 编辑确认后自动执行该动作并刷新截图
  running: false,
  abortCtl: null,
};

const $ = (id) => document.getElementById(id);

// ---------- 工具 ----------
function log(msg, cls = "info") {
  const line = document.createElement("div");
  line.className = cls;
  const t = new Date().toTimeString().slice(0, 8);
  line.textContent = `[${t}] ${msg}`;
  $("logBody").appendChild(line);
  $("logBody").scrollTop = 1e9;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${r.status}`);
  }
  return r.json();
}

function shQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

function currentDevice() { return $("deviceSelect").value || state.script.device || ""; }

// ---------- 步骤定义 ----------
const STEP_META = {
  tap:        { label: "点击",     icon: "👆" },
  tap_id:     { label: "点击控件", icon: "🎯" },
  tap_clear:  { label: "点击并清空", icon: "🗑" },
  tap_input:  { label: "点击清空并输入", icon: "⌨️" },
  input:      { label: "输入文本", icon: "✍️" },
  clear_text: { label: "清空输入", icon: "🧹" },
  key:        { label: "按键",     icon: "🎹" },
  swipe:      { label: "滑动",     icon: "↕️" },
  wait:       { label: "等待",     icon: "⏱" },
  start_app:  { label: "启动App",  icon: "🚀" },
  back:       { label: "返回",     icon: "◀️" },
  home:       { label: "Home",     icon: "🏠" },
};

function targetLabel(step) {
  if (step.x != null && step.y != null) return `(${step.x}, ${step.y})`;
  if (step.resource_id) return step.resource_id;
  if (step.text) return `文字"${step.text}"`;
  return "(未指定)";
}

function stepSummary(step) {
  switch (step.action) {
    case "tap": return targetLabel(step);
    case "tap_id": return targetLabel(step);
    case "tap_clear": return targetLabel(step) + " → 清空";
    case "tap_input": return targetLabel(step) + ` → 输入"${step.text}"`;
    case "input": return `"${step.text}"`;
    case "key": return step.key;
    case "swipe": return `(${step.x1},${step.y1})→(${step.x2},${step.y2})`;
    case "wait": return `${step.ms}ms`;
    case "start_app": return step.package + (step.activity ? "/" + step.activity : "");
    default: return "";
  }
}

// ---------- 步骤列表渲染 ----------
function renderSteps() {
  // 选中索引越界(删/移后)时复位，避免“加到选中的后面”落到错误的中间位置
  if (state.selectedIdx >= state.script.steps.length) state.selectedIdx = -1;
  const list = $("stepList");
  list.innerHTML = "";
  if (!state.script.steps.length) {
    list.innerHTML = '<div class="empty-tip">还没有步骤。<br>右侧点截图添加「点击」，或从下方下拉框添加其他步骤。</div>';
    return;
  }
  state.script.steps.forEach((step, i) => {
    const card = document.createElement("div");
    card.className = "step-card";
    card.dataset.idx = i;

    const meta = STEP_META[step.action] || { label: step.action, icon: "❓" };
    if (state.selectedIdx === i) card.classList.add("selected");

    // 双行：第一行动作+参数+操作，第二行可点击的插入位
    const head = document.createElement("div");
    head.className = "step-head";
    head.innerHTML = `
      <span class="step-idx">${i + 1}</span>
      <span class="step-type">${meta.icon} ${meta.label}</span>
      <span class="step-params">${escapeHtml(stepSummary(step))}</span>
      <span class="step-actions">
        <button data-op="run" title="单步试跑">▶</button>
        <button data-op="edit" title="编辑">✏️</button>
        <button data-op="up" title="上移">⬆</button>
        <button data-op="down" title="下移">⬇</button>
        <button data-op="copy" title="复制">⧉</button>
        <button data-op="del" title="删除">🗑</button>
      </span>`;
    card.appendChild(head);

    if (step.note) {
      const note = document.createElement("div");
      note.className = "step-note";
      note.textContent = "📝 " + step.note;
      card.appendChild(note);
    }

    const status = document.createElement("div");
    status.className = "step-status";
    card.appendChild(status);

    // 插入位：点击在【此步骤之后】插入新动作
    const slot = document.createElement("div");
    slot.className = "insert-slot";
    slot.innerHTML = '<span class="insert-line"></span><button data-op="insert" title="在这一步骤之后插入新动作">＋ 在此后插入</button>';
    card.appendChild(slot);
    slot.addEventListener("click", (e) => {
      const op = e.target.dataset && e.target.dataset.op;
      if (op === "insert") handleStepOp("insert", i);
    });

    // 点击卡片选中（便于"在选中步骤后插入"）
    card.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      state.selectedIdx = state.selectedIdx === i ? -1 : i;
      renderSteps();
    });

    head.querySelector(".step-actions").addEventListener("click", (e) => {
      const op = e.target.dataset && e.target.dataset.op;
      if (!op) return;
      handleStepOp(op, i);
    });
    list.appendChild(card);
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function handleStepOp(op, i) {
  const steps = state.script.steps;
  if (op === "del") { steps.splice(i, 1); }
  else if (op === "up" && i > 0) { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; state.selectedIdx = -1; }
  else if (op === "down" && i < steps.length - 1) { [steps[i + 1], steps[i]] = [steps[i], steps[i + 1]]; state.selectedIdx = -1; }
  else if (op === "copy") { const c = structuredClone(steps[i]); c.note = (c.note ? c.note + " " : "") + "(副本)"; steps.splice(i + 1, 0, c); }
  else if (op === "edit") { openStepModal({ index: i }); return; }
  else if (op === "insert") { openStepModal({ insertAfter: i }); return; }
  else if (op === "run") { runSingleStep(i); return; }
  renderSteps();
}

// ---------- 添加步骤（在选中项后 / 或末尾）----------
$("btnAddStep").addEventListener("click", () => {
  const prefill = state.selectedIdx >= 0 ? { insertAfter: state.selectedIdx } : {};
  openStepModal(prefill);
});

// ---------- 设备 ----------
async function loadDevices() {
  try {
    state.devices = await api("/api/devices");
    const sel = $("deviceSelect");
    sel.innerHTML = "";
    const online = state.devices.filter(d => d.status === "device");
    if (!online.length) {
      sel.innerHTML = '<option value="">⚠ 无设备连接</option>';
    } else {
      online.forEach(d => {
        const o = document.createElement("option");
        o.value = d.serial;
        o.textContent = `${d.model || d.serial} (${d.serial})`;
        sel.appendChild(o);
      });
    }
    loadImeStatus();
  } catch (e) {
    log("获取设备失败: " + e.message, "err");
  }
}
$("deviceSelect").addEventListener("change", () => {
  state.script.device = currentDevice();
  loadImeStatus();
  refreshScreenshot(false);
});

// ---------- ADBKeyboard ----------
async function loadImeStatus() {
  const dot = $("adbkbdDot");
  try {
    const st = await api(`/api/adbkeyboard/status?device=${encodeURIComponent(currentDevice())}`);
    if (st.installed) {
      dot.textContent = "IME ✓";
      dot.className = "dot on";
      dot.title = "ADBKeyboard 已安装（中文输入可用）";
    } else {
      dot.textContent = "IME ✗";
      dot.className = "dot off";
      dot.title = st.apk_ready
        ? "点击安装并启用 ADBKeyboard（中文输入需要）"
        : "未找到 ADBKeyboard.apk。请下载放入 adbkeyboard/ 目录后点击。下载: " + st.url;
    }
  } catch { dot.textContent = "IME ?"; }
}
$("adbkbdDot").addEventListener("click", async () => {
  try {
    const r = await api("/api/adbkeyboard/setup", {
      method: "POST", body: JSON.stringify({ device: currentDevice() }),
    });
    log(r.message, "ok");
    loadImeStatus();
  } catch (e) {
    log("ADBKeyboard 安装失败: " + e.message, "err");
  }
});

// ---------- 截图与坐标 ----------
function refreshScreenshot(alsoWidgets) {
  const img = $("screenshot");
  const qs = `?t=${Date.now()}&device=${encodeURIComponent(currentDevice())}`;
  img.src = "/api/screenshot" + qs;
  img.onload = () => {
    state.shotNatural = { w: img.naturalWidth, h: img.naturalHeight };
    updateScale();
    if (alsoWidgets || state.mode === "widget") loadWidgets();
  };
  img.onerror = () => log("截图失败，请确认设备已连接", "err");
}

function updateScale() {
  const img = $("screenshot");
  state.shotScale = img.clientWidth / (img.naturalWidth || 1);
}

$("btnShot").addEventListener("click", () => {
  refreshScreenshot(true);
  log("已刷新截图" + (state.mode === "widget" ? "和控件树" : ""), "info");
});

// 截图交互：按下后拖动=录制滑动轨迹；松手时基本没动=单击→弹步骤编辑窗
let gesture = null;   // {downX,downY,t0,points[],wasDrag}
const SHOT = $("screenshot");

function devPoint(clientX, clientY) {
  const r = SHOT.getBoundingClientRect();
  return [Math.round((clientX - r.left) / state.shotScale),
          Math.round((clientY - r.top) / state.shotScale)];
}
function drawGestureDot(devX, devY) {
  const imgRect = SHOT.getBoundingClientRect();
  const wrapRect = $("shotWrap").getBoundingClientRect();
  const d = document.createElement("div");
  d.className = "gesture-dot";
  d.style.left = (imgRect.left - wrapRect.left + $("shotWrap").scrollLeft + devX * state.shotScale) + "px";
  d.style.top  = (imgRect.top  - wrapRect.top  + $("shotWrap").scrollTop  + devY * state.shotScale) + "px";
  $("overlay").appendChild(d);
}
function clearGestureDots() {
  $("overlay").querySelectorAll(".gesture-dot").forEach(x => x.remove());
}

SHOT.addEventListener("pointerdown", (e) => {
  if (state.mode !== "coord") return;
  if (e.button !== 0) return;   // 仅左键
  const [x, y] = devPoint(e.clientX, e.clientY);
  gesture = { downX: e.clientX, downY: e.clientY, t0: performance.now(),
              points: [[x, y]], wasDrag: false };
  SHOT.setPointerCapture(e.pointerId);
});
SHOT.addEventListener("pointermove", (e) => {
  if (!gesture) return;
  if (Math.hypot(e.clientX - gesture.downX, e.clientY - gesture.downY) > 6) gesture.wasDrag = true;
  const last = gesture.points[gesture.points.length - 1];
  const [x, y] = devPoint(e.clientX, e.clientY);
  if (Math.hypot(x - last[0], y - last[1]) >= 3 && gesture.points.length < 300) {
    gesture.points.push([x, y]);
    drawGestureDot(x, y);
  }
});
SHOT.addEventListener("pointerup", (e) => {
  if (!gesture) return;
  const g = gesture; gesture = null;
  clearGestureDots();
  if (g.wasDrag && g.points.length >= 2) {
    const dur = Math.round(performance.now() - g.t0);
    const p0 = g.points[0], pl = g.points[g.points.length - 1];
    state.script.steps.push({
      action: "swipe", x1: p0[0], y1: p0[1], x2: pl[0], y2: pl[1],
      path: g.points.slice(), duration: Math.max(100, dur), note: "滑动手势录制",
    });
    renderSteps();
    $("stepList").scrollTop = 1e9;
    log(`已添加滑动轨迹(${g.points.length} 点, ${Math.max(100, dur)}ms)`, "ok");
  } else {
    const [x, y] = devPoint(e.clientX, e.clientY);
    openStepModal({ target: { x, y } });
  }
});

// 悬停显示坐标
$("screenshot").addEventListener("mousemove", (e) => {
  if (state.mode !== "coord") { $("coordTip").classList.add("hidden"); return; }
  const img = $("screenshot");
  const wrap = $("shotWrap");
  const rect = img.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) / state.shotScale);
  const y = Math.round((e.clientY - rect.top) / state.shotScale);
  const tip = $("coordTip");
  tip.classList.remove("hidden");
  tip.textContent = `${x}, ${y}`;
  tip.style.left = (e.clientX - wrap.getBoundingClientRect().left) + "px";
  tip.style.top = (e.clientY - wrap.getBoundingClientRect().top) + "px";
});
$("screenshot").addEventListener("mouseleave", () => $("coordTip").classList.add("hidden"));

// ---------- 控件模式 ----------
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  state.mode = t.dataset.mode;
  $("widgetPanel").classList.toggle("hidden", state.mode !== "widget");
  $("coordHint").textContent = state.mode === "coord"
    ? "点截图 → 弹出步骤编辑窗口，选动作/填名称内容后确认"
    : "点蓝框或控件列表项 → 弹出步骤编辑窗口（目标已自动带入）";
  if (state.mode === "widget" && !state.widgets.length) loadWidgets();
  renderOverlay();
}));

async function loadWidgets() {
  try {
    state.widgets = await api(`/api/uidump?device=${encodeURIComponent(currentDevice())}`);
    renderOverlay();
    renderWidgetList();
    log(`控件树已刷新: ${state.widgets.length} 个节点`, "info");
  } catch (e) {
    log("控件树获取失败: " + e.message, "err");
  }
}

function renderOverlay() {
  const overlay = $("overlay");
  overlay.innerHTML = "";
  if (state.mode !== "widget" || !state.shotNatural) return;
  const img = $("screenshot");
  state.shotScale = img.clientWidth / (img.naturalWidth || 1);   // 每次用当前实际尺寸，避免初始错位
  const imgRect = img.getBoundingClientRect();
  const wrapRect = $("shotWrap").getBoundingClientRect();
  const offX = imgRect.left - wrapRect.left + $("shotWrap").scrollLeft;
  const offY = imgRect.top - wrapRect.top + $("shotWrap").scrollTop;

  state.widgets.forEach((w) => {
    const [x1, y1, x2, y2] = w.bounds;
    const box = document.createElement("div");
    box.className = "wgt-box";
    box.style.left = (offX + x1 * state.shotScale) + "px";
    box.style.top = (offY + y1 * state.shotScale) + "px";
    box.style.width = ((x2 - x1) * state.shotScale) + "px";
    box.style.height = ((y2 - y1) * state.shotScale) + "px";
    const label = document.createElement("span");
    label.className = "wgt-label";
    label.textContent = (w.resource_id || w.text || w.class).split(".").pop();
    box.appendChild(label);
    box.addEventListener("click", (ev) => { ev.stopPropagation(); pickWidget(w); });
    overlay.appendChild(box);
  });
}

function renderWidgetList(filter = "") {
  const list = $("widgetList");
  list.innerHTML = "";
  const f = filter.toLowerCase();
  state.widgets
    .filter(w => !f || (w.resource_id + " " + w.text).toLowerCase().includes(f))
    .forEach(w => {
      const row = document.createElement("div");
      row.className = "wgt-row";
      row.innerHTML = `
        <span class="rid">${escapeHtml(w.resource_id || "—")}</span>
        <span class="txt">${escapeHtml(w.text || "")}</span>
        <span class="cls">${escapeHtml(w.class.split(".").pop())}${w.clickable ? " 👆" : ""}</span>`;
      row.addEventListener("click", () => pickWidget(w));
      list.appendChild(row);
    });
}
$("widgetSearch").addEventListener("input", (e) => renderWidgetList(e.target.value));

function pickWidget(w) {
  openStepModal({ target: { resource_id: w.resource_id || "", text: w.resource_id ? "" : w.text } });
}

window.addEventListener("resize", () => { updateScale(); renderOverlay(); });
// 监听截图容器尺寸变化(初载/布局稳定/栏宽变化时)自动重排蒙层，避免控件框与截图错位
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => { updateScale(); renderOverlay(); }).observe($("shotWrap"));
}

// ---------- 步骤编辑弹窗（添加/编辑/插入统一入口）----------
const TARGET_ACTIONS = ["tap", "tap_id", "tap_clear", "tap_input"];
const modalCtx = { mode: "add", index: -1, insertAfter: -1 };

function openStepModal(opts = {}) {
  modalCtx.mode = opts.index >= 0 ? "edit" : "add";
  modalCtx.index = opts.index >= 0 ? opts.index : -1;
  modalCtx.insertAfter = opts.insertAfter >= 0 ? opts.insertAfter : -1;
  const step = modalCtx.index >= 0 ? state.script.steps[modalCtx.index]
    : { action: "tap", x: null, y: null, resource_id: "", text: "", input: "", note: "" };

  // 预填：来自截图/控件的目标
  const tgt = opts.target || {};
  $("stepModalTitle").textContent = modalCtx.mode === "edit"
    ? `编辑步骤 ${modalCtx.index + 1}` : "添加步骤";
  $("sm_action").value = step.action || "tap";
  $("sm_note").value = step.note || "";
  $("sm_x").value = (step.x != null ? step.x : tgt.x) ?? "";
  $("sm_y").value = (step.y != null ? step.y : tgt.y) ?? "";
  $("sm_rid").value = step.resource_id || tgt.resource_id || "";
  $("sm_ftext").value = step.text || tgt.text || "";
  $("sm_retry").value = step.retry || "";
  const inText = step.action === "input" ? (step.text || "") : (step.input || "");
  $("sm_input").value = inText;
  // 其它参数
  $("sm_key").value = step.key || "TAB";
  $("sm_s1").value = step.x1 ?? 500; $("sm_s2").value = step.y1 ?? 1500;
  $("sm_s3").value = step.x2 ?? 500; $("sm_s4").value = step.y2 ?? 600;
  $("sm_sdur").value = step.duration ?? 300;
  $("sm_swipePath").textContent = (step.action === "swipe" && step.path)
    ? `已录制轨迹 ${step.path.length} 点 / 时长 ${step.duration || ""}ms（坐标模式下在截图直接拖动可重新录制）`
    : "";
  $("sm_wms").value = step.ms ?? 500;
  $("sm_pkg").value = step.package || "";
  $("sm_act").value = step.activity || "";
  $("sm_del").classList.toggle("hidden", modalCtx.mode !== "edit");
  syncModalFields();
  $("stepModal").classList.remove("hidden");
  setTimeout(() => $("sm_note").focus(), 30);
}

function syncModalFields() {
  const a = $("sm_action").value;
  $("sm_target").classList.toggle("hidden", !TARGET_ACTIONS.includes(a));
  $("sm_inputRow").classList.toggle("hidden", !["input", "tap_input"].includes(a));
  $("sm_keyRow").classList.toggle("hidden", a !== "key");
  $("sm_swipeRow").classList.toggle("hidden", a !== "swipe");
  $("sm_waitRow").classList.toggle("hidden", a !== "wait");
  $("sm_appRow").classList.toggle("hidden", a !== "start_app");
  $("sm_input").readOnly = !["input", "tap_input"].includes(a);
}
$("sm_action").addEventListener("change", syncModalFields);

function readModalStep() {
  const a = $("sm_action").value;
  const st = { action: a, note: $("sm_note").value.trim() };
  const x = $("sm_x").value, y = $("sm_y").value;
  const rid = $("sm_rid").value.trim(), ftext = $("sm_ftext").value.trim();
  if (TARGET_ACTIONS.includes(a)) {
    if (x !== "" && y !== "") { st.x = Number(x); st.y = Number(y); st.resource_id = ""; st.text = ""; }
    else if (rid) { st.x = null; st.y = null; st.resource_id = rid; st.text = ""; }
    else { st.x = null; st.y = null; st.resource_id = ""; st.text = ftext; }
    const r2 = $("sm_retry").value;
    if (r2 !== "") st.retry = Number(r2);   // 单步重试覆盖
  }
  if (a === "input") st.text = $("sm_input").value;
  if (a === "tap_input") st.input = $("sm_input").value;
  if (a === "key") st.key = $("sm_key").value;
  if (a === "swipe") { st.x1 = Number($("sm_s1").value); st.y1 = Number($("sm_s2").value);
    st.x2 = Number($("sm_s3").value); st.y2 = Number($("sm_s4").value); st.duration = Number($("sm_sdur").value) || 300;
    // 编辑已录制的滑动时保留其轨迹点
    if (modalCtx.mode === "edit" && state.script.steps[modalCtx.index].path) st.path = state.script.steps[modalCtx.index].path; }
  if (a === "wait") st.ms = Number($("sm_wms").value) || 500;
  if (a === "start_app") { st.package = $("sm_pkg").value.trim(); st.activity = $("sm_act").value.trim(); }
  return st;
}

function saveStepFromModal() {
  const st = readModalStep();
  let affIdx;
  if (modalCtx.mode === "edit") {
    affIdx = modalCtx.index;
    state.script.steps[affIdx] = st;
    log(`已更新步骤 ${affIdx + 1}`, "ok");
  } else {
    affIdx = modalCtx.insertAfter >= 0 ? modalCtx.insertAfter + 1 : state.script.steps.length;
    state.script.steps.splice(affIdx, 0, st);
    log(`已添加「${STEP_META[st.action].label}」步骤${st.note ? "：" + st.note : ""}`, "ok");
  }
  state.selectedIdx = -1;
  $("stepModal").classList.add("hidden");
  renderSteps();
  $("stepList").scrollTop = 1e9;
  // 编辑完成：开 → 自动执行该动作并刷新；关 → 仅刷新
  if (state.editorAutoRun) runSingleStep(affIdx);
  else refreshScreenshot(state.mode === "widget");
}
$("sm_ok").addEventListener("click", saveStepFromModal);
$("autoRunEdit").addEventListener("change", (e) => {
  state.editorAutoRun = e.target.checked;
  try { localStorage.setItem("autoRunEdit", state.editorAutoRun ? "1" : "0"); } catch (_) {}
});
$("sm_cancel").addEventListener("click", () => $("stepModal").classList.add("hidden"));
$("sm_del").addEventListener("click", () => {
  if (modalCtx.mode === "edit") {
    state.script.steps.splice(modalCtx.index, 1);
    $("stepModal").classList.add("hidden");
    state.selectedIdx = -1;
    renderSteps();
    log(`已删除步骤`, "info");
  }
});

// ---------- 执行 ----------
function markCard(idx, cls, text) {
  const card = document.querySelector(`.step-card[data-idx="${idx}"] .step-status`);
  if (card) { card.textContent = text; card.style.color = cls === "err" ? "#cf222e" : "#1a7f37"; }
}

async function runSingleStep(idx) {
  const step = state.script.steps[idx];
  if (!step) return;
  const card = document.querySelector(`.step-card[data-idx="${idx}"]`);
  if (card) card.classList.add("running");
  markCard(idx, "run", "执行中...");
  try {
    const r = await api("/api/run_step", {
      method: "POST",
      body: JSON.stringify({ device: currentDevice(), step }),
    });
    if (r.ok) {
      markCard(idx, "ok", `✓ ${r.detail} (${r.ms}ms)`);
      log(`步骤 ${idx + 1} ✓ ${r.detail} (${r.ms}ms)`, "ok");
    } else {
      markCard(idx, "err", `✗ ${r.detail}`);
      log(`步骤 ${idx + 1} ✗ ${r.detail}`, "err");
    }
    refreshScreenshot(state.mode === "widget");   // 执行后刷新；控件模式同步刷新控件树
  } catch (e) {
    markCard(idx, "err", "✗ " + e.message);
    log(`步骤 ${idx + 1} ✗ ${e.message}`, "err");
  } finally {
    if (card) card.classList.remove("running");
  }
}

async function runWholeScript() {
  if (state.running || !state.script.steps.length) return;
  state.running = true;
  state.abortCtl = new AbortController();
  $("btnRun").disabled = true;
  $("btnStop").disabled = false;
  // 清除旧状态标记
  document.querySelectorAll(".step-card").forEach(c => c.classList.remove("ok", "error"));
  document.querySelectorAll(".step-status").forEach(s => s.textContent = "");
  log(`▶ 开始运行「${state.script.name}」共 ${state.script.steps.length} 步`, "info");
  try {
    const r = await fetch("/api/run_script", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: currentDevice(), script: state.script }),
      signal: state.abortCtl.signal,
    });
    const data = await r.json();
    let stopped = false;
    data.logs.forEach(l => {
      markCard(l.index, l.ok ? "ok" : "err", l.ok ? `✓ ${l.detail} (${l.ms}ms)` : `✗ ${l.detail}`);
      log(`步骤 ${l.index + 1} ${l.ok ? "✓" : "✗"} ${l.detail} (${l.ms}ms)${l.note ? " — " + l.note : ""}`,
          l.ok ? "ok" : "err");
      if (l.stopped) stopped = true;
    });
    log(stopped ? "⏹ 已停止" : "✔ 脚本执行结束", stopped ? "err" : "ok");
    refreshScreenshot();
  } catch (e) {
    if (e.name === "AbortError") log("⏹ 已停止运行", "err");
    else log("运行出错: " + e.message, "err");
  } finally {
    state.running = false;
    $("btnRun").disabled = false;
    $("btnStop").disabled = true;
  }
}
$("btnRun").addEventListener("click", runWholeScript);
$("btnStop").addEventListener("click", () => {
  if (state.abortCtl) state.abortCtl.abort();
  state.running = false;
});

// ---------- 保存 / 打开 ----------
function collectScript() {
  state.script.name = $("scriptName").value || "未命名脚本";
  state.script.step_delay_ms = Number($("stepDelay").value) || 0;
  state.script.hide_keyboard_after_input = $("hideKbd").checked;
  state.script.control_retry = Number($("controlRetry").value) || 0;
  state.script.device = currentDevice();
  return state.script;
}

$("btnSave").addEventListener("click", async () => {
  try {
    const r = await api("/api/scripts", {
      method: "POST", body: JSON.stringify(collectScript()),
    });
    log(`已保存: scripts/${r.file}`, "ok");
  } catch (e) { log("保存失败: " + e.message, "err"); }
});

$("btnOpen").addEventListener("click", async () => {
  try {
    const files = await api("/api/scripts");
    if (!files.length) { log("scripts/ 目录还没有保存的脚本", "err"); return; }
    const pick = files.map(f => `${f.name} (${f.steps}步)`).join("\n");
    // 简易选择
    const idxStr = prompt("选择要打开的脚本（输入序号）:\n" +
      files.map((f, i) => `${i + 1}. ${f.name} (${f.steps}步)`).join("\n"));
    const idx = Number(idxStr) - 1;
    if (isNaN(idx) || !files[idx]) return;
    const data = await api(`/api/scripts/${encodeURIComponent(files[idx].file)}`);
    // 用磁盘上 steps 的原始顺序重建，并清空所有临时选择/弹窗状态
    state.script = { ...data, steps: (data.steps || []).slice() };
    modalCtx.index = -1; modalCtx.insertAfter = -1;
    $("scriptName").value = data.name || "";
    $("stepDelay").value = data.step_delay_ms ?? 300;
    $("hideKbd").checked = data.hide_keyboard_after_input !== false;
    $("controlRetry").value = data.control_retry ?? 3;
    if (data.device) $("deviceSelect").value = data.device;
    state.selectedIdx = -1;
    renderSteps();
    log(`已打开: ${files[idx].file}`, "ok");
  } catch (e) { log("打开失败: " + e.message, "err"); }
});

// ---------- 导出 ----------
$("btnExport").addEventListener("click", async () => {
  try {
    const r = await api("/api/export", {
      method: "POST", body: JSON.stringify(collectScript()),
    });
    $("exportPath").textContent = r.path;
    $("exportContent").textContent = r.content;
    $("exportModal").classList.remove("hidden");
    log(`已导出: ${r.path}`, "ok");
  } catch (e) { log("导出失败: " + e.message, "err"); }
});
$("btnCloseExport").addEventListener("click", () => $("exportModal").classList.add("hidden"));
$("btnCopySh").addEventListener("click", () => {
  navigator.clipboard.writeText($("exportContent").textContent)
    .then(() => log("已复制到剪贴板", "ok"));
});

// ---------- scrcpy ----------
$("btnScrcpy").addEventListener("click", async () => {
  try {
    const r = await api("/api/scrcpy/start", {
      method: "POST", body: JSON.stringify({ device: currentDevice() }),
    });
    if (r.ok) log("scrcpy 已启动", "ok");
    else log("scrcpy 启动失败: " + r.error, "err");
  } catch (e) { log("scrcpy 启动失败: " + e.message, "err"); }
});

// ---------- 其他 ----------
$("btnClearLog").addEventListener("click", () => $("logBody").innerHTML = "");
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  $("exportModal").classList.add("hidden");
  $("stepModal").classList.add("hidden");
});

// ---------- 初始化 ----------
(async function init() {
  // 读取“编辑后自动执行”开关的持久化状态
  try { state.editorAutoRun = localStorage.getItem("autoRunEdit") !== "0"; } catch (_) {}
  $("autoRunEdit").checked = state.editorAutoRun;
  await loadDevices();
  refreshScreenshot(true);
  renderSteps();
  log("编辑器就绪。点击右侧截图添加点击/滑动步骤，或从左下角添加其他类型步骤。", "info");
})();
