// prodagent playground frontend. Mirrors console.py's _HANDLERS dispatch —
// each HookEvent maps to {label, color, render(body, ev)} so the web stream
// matches the terminal output field-for-field.

let selectedSpec = null;
let currentThinkCard = null;
let currentEventSource = null;
let currentRunId = null;
let multiAgentMode = false;
let lastMeiMemoryPreview = "";
let maRoster = [];  // current participant roster for multi-agent runs
let currentPhaseContainer = null;  // DOM element for the active phase
let phaseElapsedStart = 0;  // performance.now() when the current phase started
const phaseContainers = [];  // all completed phase containers in order
const submittedRequestIds = new Set();
let lastApprovalRequest = null;  // cached approval.request event for modal display
const SPEAKER_AVATAR = { 大牛: "牛", 小美: "美" };

// ── HUD state (top-bar run status) ──
const hudState = {
  agent: "—",
  turn: 0,
  maxTurns: 0,
  inputTokens: 0,
  outputTokens: 0,
  costUsd: 0,
  budgetUsd: 0,
  tools: 0,
  subAgents: 0,
  pending: 0,
  sessionStartTs: 0,
};
const hudEls = {
  agent: document.querySelector(".hud-agent"),
  turn: document.querySelector(".hud-turn"),
  tokens: document.querySelector(".hud-tokens"),
  cost: document.querySelector(".hud-cost"),
  tools: document.querySelector(".hud-tools"),
  subagents: document.querySelector(".hud-subagents"),
  pending: document.querySelector(".hud-pending"),
};

function fmtNum(n) { return (n || 0).toLocaleString(); }
function fmtCost(c) { return `$${(c || 0).toFixed(4)}`; }

function updateHud() {
  hudEls.agent.textContent = hudState.agent;
  const turnStr = hudState.maxTurns
    ? `Turn ${hudState.turn}/${hudState.maxTurns}`
    : `Turn ${hudState.turn}`;
  hudEls.turn.textContent = turnStr;
  hudEls.tokens.textContent = `${fmtNum(hudState.inputTokens + hudState.outputTokens)} tok`;
  const budgetStr = hudState.budgetUsd > 0
    ? `${fmtCost(hudState.costUsd)}/${hudState.budgetUsd.toFixed(2)}`
    : fmtCost(hudState.costUsd);
  hudEls.cost.textContent = budgetStr;
  hudEls.tools.textContent = `${hudState.tools} tool${hudState.tools === 1 ? "" : "s"}`;
  hudEls.subagents.textContent = `${hudState.subAgents} sub`;
  hudEls.pending.textContent = `⏸ ${hudState.pending}`;
  hudEls.pending.classList.toggle("active", hudState.pending > 0);
}

function resetHudForRun(agentName) {
  hudState.agent = agentName || "—";
  hudState.turn = 0;
  hudState.maxTurns = 0;
  hudState.inputTokens = 0;
  hudState.outputTokens = 0;
  hudState.costUsd = 0;
  hudState.budgetUsd = 0;
  hudState.tools = 0;
  hudState.subAgents = 0;
  hudState.pending = 0;
  hudState.sessionStartTs = Date.now();
  updateHud();
}

const streamEl = document.getElementById("stream");
const streamSectionEl = document.getElementById("stream-section");
const statusEl = document.getElementById("status");
const cardsEl = document.getElementById("cards");
const taskInput = document.getElementById("task-input");
const sendBtn = document.getElementById("send-btn");
const pickerEl = document.getElementById("picker");
const pickerToggleEl = document.getElementById("picker-toggle");
const playgroundMainEl = document.getElementById("playground-main");
const rosterPanelEl = document.getElementById("roster-panel");
const snapshotPanelEl = document.getElementById("snapshot-panel");
const rosterEl = document.getElementById("roster");
const snapshotEl = document.getElementById("snapshot");
let sending = false;  // guards concurrent _startRun/_sendChat
sendBtn.textContent = "💬 Send";

pickerToggleEl.onclick = () => pickerEl.classList.toggle("collapsed");

// ── Bootstrap ──────────────────────────────────────────────────────────────
const AGENT_ICONS = {
  greeter: "👋",
  trip_planner: "🗺️",
  deep_research: "🔬",
  code_reviewer: "🔎",

  trader: "🧋",
  code_detective: "🐛",
  aiops: "🚨"
};

async function loadExamples() {
  const specs = await fetch("/api/examples").then((r) => r.json());
  specs.sort((a, b) => a.number - b.number);
  for (const spec of specs) {
    const card = document.createElement("div");
    card.className = "card";
    const icon = AGENT_ICONS[spec.name] || "🤖";
    const hitlBadge = spec.is_hitl ? '<span class="hitl-badge">HITL</span>' : "";
    card.innerHTML = `<span class="icon">${icon}</span><span class="title">${escapeHtml(spec.title)}</span>${hitlBadge}`;
    card.title = spec.description || "";
    card.onclick = () => selectExample(spec, card);
    cardsEl.appendChild(card);
  }
  statusEl.textContent = `${specs.length} 个 agent`;
}

function selectExample(spec, cardEl) {
  for (const c of cardsEl.children) c.classList.remove("selected");
  cardEl.classList.add("selected");
  if (currentEventSource) { currentEventSource.close(); currentEventSource = null; }
  selectedSpec = spec;
  multiAgentMode = !!spec.is_multiagent;
  taskInput.value = spec.default_task;
  streamEl.innerHTML = "";
  currentThinkCard = null;
  toolPairCards.clear();
  subAgentContainers.clear();
  runIdStack.length = 0;
  currentRunId = null;
  sending = false;
  sendBtn.disabled = false;
  maRoster = [];
  currentPhaseContainer = null;
  phaseElapsedStart = 0;
  phaseContainers.length = 0;
  rosterEl.innerHTML = "";
  snapshotEl.innerHTML = "";
  if (multiAgentMode) {
    playgroundMainEl.classList.remove("single-agent");
    rosterPanelEl.classList.remove("hidden");
    snapshotPanelEl.classList.remove("hidden");
    taskInput.style.display = "none";
    sendBtn.textContent = "▶ Start";
  } else {
    playgroundMainEl.classList.add("single-agent");
    rosterPanelEl.classList.add("hidden");
    snapshotPanelEl.classList.add("hidden");
    taskInput.style.display = "";
    sendBtn.textContent = "💬 Send";
  }
  hudState.agent = spec.title;
  updateHud();
  if (multiAgentMode) statusEl.textContent = `点击右下角按钮，开始 ${spec.title} 的多 Agent 协作`;
  else taskInput.focus();
}

taskInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!selectedSpec || multiAgentMode) return;
    if (currentRunId) _sendChat();
    else _startRun();
  }
});

// ── Run + Chat + SSE ───────────────────────────────────────────────────────
sendBtn.onclick = async () => {
  if (!selectedSpec) return;
  if (multiAgentMode) return _startMultiAgent();
  if (currentRunId) return _sendChat();
  return _startRun();
};

async function _startRun() {
  if (sending) return;
  sending = true;
  const task = taskInput.value.trim() || selectedSpec.default_task;
  streamEl.innerHTML = "";
  currentThinkCard = null;
  toolPairCards.clear();
  subAgentContainers.clear();
  runIdStack.length = 0;
  statusEl.textContent = "starting…";
  resetHudForRun(selectedSpec.title);
  appendUserInput(task);
  taskInput.value = "";

  let runId;
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({example: selectedSpec.name, task}),
    });
    if (!res.ok) {
      const err = await res.text();
      showError(`启动失败: ${err}`);
      sending = false;
      return;
    }
    runId = (await res.json()).run_id;
  } catch (e) {
    showError(`网络错误: ${e}`);
    sending = false;
    return;
  }
  currentRunId = runId;
  sending = false;
  statusEl.textContent = `run=${runId}  running…`;
  streamRun(runId);
}

async function _sendChat() {
  if (!selectedSpec || !currentRunId) return;
  if (sending) return;
  const message = taskInput.value.trim();
  if (!message) return;
  sending = true;
  statusEl.textContent = "chatting…";
  appendUserInput(message);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({example: selectedSpec.name, run_id: currentRunId, message}),
    });
    if (!res.ok) {
      const err = await res.text();
      showError(`chat 失败: ${err}`);
      sending = false;
      return;
    }
    taskInput.value = "";
  } catch (e) {
    showError(`网络错误: ${e}`);
    sending = false;
    return;
  }
  sending = false;
  streamRun(currentRunId);
}

function appendUserInput(text) {
  const ts = hudState.sessionStartTs ? `+${((Date.now() - hudState.sessionStartTs) / 1000).toFixed(1)}s` : "";
  const card = makeCard("USER INPUT", "blue", escapeHtml(text), "👤", ts);
  streamEl.appendChild(card.el);
  streamEl.scrollTop = streamEl.scrollHeight;
}

function streamRun(runId) {
  if (currentEventSource) currentEventSource.close();
  const es = new EventSource(`/api/stream/${runId}`);
  currentEventSource = es;
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "hook") {
      renderHook(ev);
    } else if (ev.type === "suspended") {
      showSuspended(ev);
      showApprovalModal(ev);
      statusEl.textContent = "⏸ suspended — awaiting approval";
    } else if (ev.type === "completed") {
      closeAllApprovalModals();
      showFinal(ev.final_output);
      statusEl.textContent = "completed";
      taskInput.focus();
      es.close();
    } else if (ev.type === "failed") {
      closeAllApprovalModals();
      showError(ev.error);
      es.close();
      statusEl.textContent = "failed";
    }
    // heartbeat: keep-alive, no-op
  };
  es.onerror = () => {
    statusEl.textContent = "连接断开";
    es.close();
  };
}

// ── Multi-agent: generic event stream rendered into the three-column layout ──
async function _startMultiAgent() {
  if (sending) return;
  sending = true;
  sendBtn.disabled = true;
  statusEl.textContent = "协作进行中…";
  streamEl.innerHTML = "";
  rosterEl.innerHTML = "";
  snapshotEl.innerHTML = "";
  maRoster = [];
  currentPhaseContainer = null;
  phaseElapsedStart = 0;
  phaseContainers.length = 0;
  lastMeiMemoryPreview = "";
  resetHudForRun(selectedSpec.title);

  let runId;
  try {
    const res = await fetch(`/api/multiagent/${selectedSpec.name}/start`, { method: "POST" });
    if (!res.ok) throw new Error(`start failed: ${res.status}`);
    ({ run_id: runId } = await res.json());
  } catch (e) {
    statusEl.textContent = `启动失败：${e.message}`;
    sendBtn.disabled = false;
    sending = false;
    return;
  }

  currentRunId = runId;
  if (currentEventSource) currentEventSource.close();
  const es = new EventSource(`/api/multiagent/${selectedSpec.name}/stream/${runId}`);
  currentEventSource = es;

  es.onmessage = (evt) => {
    const env = JSON.parse(evt.data);
    if (env.type === "heartbeat") return;
    renderMultiAgentEvent(env);
  };

  es.onerror = () => {
    statusEl.textContent = "连接中断";
    sendBtn.disabled = false;
    sending = false;
    es.close();
  };
}

function renderMultiAgentEvent(env) {
  // Terminal events re-enable the start button.
  if (env.kind === "completed") {
    closeCurrentPhase();
    statusEl.textContent = "协作结束";
    sendBtn.disabled = false;
    sending = false;
    if (currentEventSource) currentEventSource.close();
    return;
  }
  if (env.kind === "failed") {
    closeCurrentPhase();
    statusEl.textContent = `出错：${(env.payload && env.payload.error) || "未知"}`;
    sendBtn.disabled = false;
    sending = false;
    if (currentEventSource) currentEventSource.close();
    return;
  }
  if (env.kind === "started") {
    statusEl.textContent = "协作进行中…";
    return;
  }
  if (env.kind === "roster") {
    maRoster = (env.payload && env.payload.participants) || [];
    renderRoster();
    return;
  }
  // Snapshot panel: always show the latest event's snapshot.
  if (env.snapshot && Object.keys(env.snapshot).length > 0) {
    renderSnapshot(env.snapshot, env.phase);
  }
  // Dispatch to the right center-column renderer.
  switch (env.kind) {
    case "turn": renderTurnBubble(env); break;
    case "write": renderTimelineWrite(env); break;
    case "claim": renderTimelineClaim(env); break;
    case "complete": renderTimelineComplete(env); break;
    case "requeue": renderTimelineRequeue(env); break;
    case "dead_letter": renderTimelineDeadLetter(env); break;
    case "phase_started": renderPhaseContainerStart(env); break;
    case "phase_completed": renderPhaseContainerEnd(env); break;
    default:
      renderGenericCard(env);
  }
  scrollDown();
}

// ── Phase container management ───────────────────────────────────────────────
function closeCurrentPhase() {
  if (!currentPhaseContainer) return;
  currentPhaseContainer.classList.remove("active");
  currentPhaseContainer.classList.add("completed");
  // Auto-collapse after a short delay so the user can see the final state.
  setTimeout(() => {
    if (currentPhaseContainer && currentPhaseContainer.classList.contains("completed")) {
      currentPhaseContainer.classList.add("collapsed");
    }
  }, 1500);
  phaseContainers.push(currentPhaseContainer);
  currentPhaseContainer = null;
}

function ensurePhaseContainer(env) {
  if (!currentPhaseContainer) {
    // Phase started without a phase_started event — create a bare container.
    const el = document.createElement("div");
    el.className = "phase-container active";
    const hdr = document.createElement("div");
    hdr.className = "phase-header";
    hdr.innerHTML = `<span class="ph-icon">⚙</span><div class="ph-info"><div class="ph-name">${escapeHtml(PHASE_LABELS[env.phase] || env.phase || "运行中")}</div></div><span class="ph-toggle">▾</span>`;
    hdr.onclick = () => el.classList.toggle("collapsed");
    const body = document.createElement("div");
    body.className = "phase-body";
    el.appendChild(hdr);
    el.appendChild(body);
    streamEl.appendChild(el);
    currentPhaseContainer = el;
    phaseElapsedStart = performance.now();
  }
  return currentPhaseContainer;
}

function getPhaseBody() {
  const c = ensurePhaseContainer({phase: ""});
  return c.querySelector(".phase-body");
}

function updatePhaseHeaderCounters(stats) {
  if (!currentPhaseContainer) return;
  const hdr = currentPhaseContainer.querySelector(".phase-header");
  let existing = hdr.querySelector(".ph-stats");
  if (!existing) {
    existing = document.createElement("div");
    existing.className = "ph-stats";
    hdr.appendChild(existing);
  }
  const parts = [];
  for (const [k, v] of Object.entries(stats)) {
    const cls = k === "dead_lettered" && v > 0 ? "bad" : (k === "requeued" && v > 0 ? "warn" : "ok");
    parts.push(`<span class="ph-stat ${cls}">${escapeHtml(String(k))}: ${v}</span>`);
  }
  existing.innerHTML = parts.join("");
}

function renderPhaseContainerStart(env) {
  closeCurrentPhase();
  const el = document.createElement("div");
  el.className = "phase-container active";
  const phaseName = PHASE_LABELS[env.phase] || env.phase || "?";
  const detail = (env.payload && env.payload.detail) ? escapeHtml(env.payload.detail) : "";
  const hdr = document.createElement("div");
  hdr.className = "phase-header";
  hdr.innerHTML = `
    <span class="ph-icon">▶</span>
    <div class="ph-info">
      <div class="ph-name">${escapeHtml(phaseName)}</div>
      ${detail ? `<div class="ph-detail">${detail}</div>` : ""}
    </div>
    <span class="ph-toggle">▾</span>
  `;
  hdr.onclick = () => el.classList.toggle("collapsed");
  const body = document.createElement("div");
  body.className = "phase-body";
  el.appendChild(hdr);
  el.appendChild(body);
  streamEl.appendChild(el);
  currentPhaseContainer = el;
  phaseElapsedStart = performance.now();
}

function renderPhaseContainerEnd(env) {
  if (!currentPhaseContainer) {
    // Phase completed without a start event — create a minimal container.
    ensurePhaseContainer(env);
  }
  // Update header with final stats.
  const hdr = currentPhaseContainer.querySelector(".phase-header");
  const icon = hdr.querySelector(".ph-icon");
  if (icon) icon.textContent = "✓";
  const detail = (env.payload && env.payload.detail) ? escapeHtml(env.payload.detail) : "";
  const info = hdr.querySelector(".ph-info");
  if (detail && info) {
    let detEl = info.querySelector(".ph-detail");
    if (!detEl) {
      detEl = document.createElement("div");
      detEl.className = "ph-detail";
      info.appendChild(detEl);
    }
    detEl.textContent = detail;
  }
  // Show final counters from the payload.
  if (env.payload && env.payload.counts) {
    updatePhaseHeaderCounters(env.payload.counts);
  }
  // Show elapsed time.
  const elapsed = ((performance.now() - phaseElapsedStart) / 1000).toFixed(1);
  if (info) {
    let timeEl = info.querySelector(".ph-time");
    if (!timeEl) {
      timeEl = document.createElement("div");
      timeEl.className = "ph-detail ph-time";
      info.appendChild(timeEl);
    }
    timeEl.textContent = `耗时 ${elapsed}s`;
  }
  closeCurrentPhase();
  // Scroll to show the completed phase header.
  if (phaseContainers.length > 0) {
    const last = phaseContainers[phaseContainers.length - 1];
    last.scrollIntoView({behavior: "smooth", block: "start"});
  }
}

// ── Timeline row builders ────────────────────────────────────────────────────
function appendTimelineRow(klass, actor, action, summaryHtml, payload) {
  const body = getPhaseBody();
  const row = document.createElement("div");
  row.className = `timeline-row ${klass}`;
  const summary = document.createElement("span");
  summary.className = "tl-summary";
  summary.innerHTML = summaryHtml;
  row.innerHTML = `
    <span class="tl-dot"></span>
    <span class="tl-actor">${escapeHtml(actor || "?")}</span>
    <span class="tl-action">${escapeHtml(action)}</span>
  `;
  // Insert summary after action
  const actionEl = row.querySelector(".tl-action");
  actionEl.insertAdjacentElement("afterend", summary);
  if (payload && Object.keys(payload).length > 0) {
    const payloadEl = document.createElement("pre");
    payloadEl.className = "tl-payload";
    payloadEl.textContent = JSON.stringify(payload, null, 2);
    row.appendChild(payloadEl);
    row.onclick = (e) => {
      e.stopPropagation();
      payloadEl.style.display = payloadEl.style.display === "block" ? "none" : "block";
    };
  }
  body.appendChild(row);
  return row;
}

function renderTimelineWrite(env) {
  const p = env.payload || {};
  const buzzWinner = !!p.buzz_in_winner;
  const triggerLabel = p.trigger_name === "buzz_in" ? "抢答" : (p.trigger_name === "kickoff" ? "出题" : (p.trigger_name || "写入"));
  const valStr = truncate(JSON.stringify(p.value), 60);
  const summary = `写入 <code>${escapeHtml(p.key || "?")}</code> = ${escapeHtml(valStr)}`;
  const row = appendTimelineRow("write" + (buzzWinner ? " buzz-winner" : ""), env.actor, triggerLabel, summary, p);
  if (buzzWinner) {
    // Add a small winner badge
    const badge = document.createElement("span");
    badge.style.cssText = "font-size:10px;color:var(--green);font-weight:700;margin-left:4px;";
    badge.textContent = "🏆";
    row.querySelector(".tl-actor").appendChild(badge);
  }
}

function renderTimelineClaim(env) {
  const p = env.payload || {};
  appendTimelineRow("claim", env.actor, "认领", `认领了 <code>${escapeHtml(p.item_id || "?")}</code>`, p);
}

function renderTimelineComplete(env) {
  const p = env.payload || {};
  appendTimelineRow("complete", env.actor, "完成", `完成了 <code>${escapeHtml(p.item_id || "?")}</code>`, p);
}

function renderTimelineRequeue(env) {
  const p = env.payload || {};
  const reason = p.reason ? ` — ${escapeHtml(p.reason)}` : "";
  appendTimelineRow("requeue", env.actor, "重排", `<code>${escapeHtml(p.item_id || "?")}</code> 重新入队${reason}`, p);
}

function renderTimelineDeadLetter(env) {
  const p = env.payload || {};
  const error = p.error ? ` — ${escapeHtml(p.error)}` : "";
  appendTimelineRow("dead-letter", env.actor, "死信", `<code>${escapeHtml(p.item_id || "?")}</code> 重试 ${p.attempts || "?"} 次后转入死信${error}`, p);
}

// ── Roster (left panel) — group by role, render human-readable labels ──
const ROLE_LABELS = {
  speaker: "发言者",
  expert: "专家",
  worker: "工人",
  host: "主持人",
  trigger: "触发器",
};
const ROLE_ORDER = ["host", "speaker", "expert", "worker", "trigger"];
const STATE_LABELS = {
  idle: "待命",
  computing: "计算中",
  locked_winner: "抢到锁",
  locked_loser: "未抢到",
  failed: "失败",
  completed: "已完成",
};

function rosterMetaLabel(p) {
  if (!p.meta || Object.keys(p.meta).length === 0) return "";
  const parts = [];
  if (p.meta.desc) parts.push(p.meta.desc);
  if (p.meta.specialty) parts.push(`擅长${p.meta.specialty}`);
  if (p.meta.mode) parts.push(p.meta.mode === "buzz_in" ? "抢锁模式" : "并发模式");
  return parts.join(" · ");
}

function renderRoster() {
  rosterEl.innerHTML = "";
  const groups = {};
  for (const p of maRoster) {
    const role = p.role || "other";
    (groups[role] = groups[role] || []).push(p);
  }
  const roles = Object.keys(groups).sort(
    (a, b) => ROLE_ORDER.indexOf(a) - ROLE_ORDER.indexOf(b)
  );
  for (const role of roles) {
    const groupEl = document.createElement("div");
    groupEl.className = "roster-group";
    const titleEl = document.createElement("div");
    titleEl.className = "roster-group-title";
    titleEl.textContent = ROLE_LABELS[role] || role;
    groupEl.appendChild(titleEl);
    for (const p of groups[role]) {
      const row = document.createElement("div");
      row.className = "roster-row";
      const light = document.createElement("span");
      const state = p.state || "idle";
      light.className = `roster-state-light ${state}`;
      light.title = STATE_LABELS[state] || state;
      const name = document.createElement("span");
      name.className = "roster-name";
      name.textContent = p.name;
      row.append(light, name);
      const metaStr = rosterMetaLabel(p);
      if (metaStr) {
        const meta = document.createElement("span");
        meta.className = "roster-meta";
        meta.textContent = metaStr;
        meta.title = metaStr;
        row.appendChild(meta);
      }
      groupEl.appendChild(row);
    }
    rosterEl.appendChild(groupEl);
  }
}

// ── Snapshot (right panel) — mini-card layout ──
const PHASE_LABELS = {
  backstage_review: "后台审题",
  live_quiz: "正式抢答",
};

function renderSnapshot(snapshot, phase) {
  snapshotEl.innerHTML = "";

  // Phase label
  if (phase) {
    const card = document.createElement("div");
    card.className = "snapshot-mini";
    const label = PHASE_LABELS[phase] || phase;
    const isRunning = currentPhaseContainer && currentPhaseContainer.classList.contains("active");
    const elapsed = isRunning ? ((performance.now() - phaseElapsedStart) / 1000).toFixed(1) : "—";
    card.innerHTML = `
      <div class="sm-title">阶段</div>
      <div style="font-weight:700;font-size:12px;margin-bottom:4px;">${escapeHtml(label)}</div>
      <div style="font-size:10px;color:var(--dim);">状态: ${isRunning ? "🟢 运行中" : "✓ 已完成"}</div>
      <div style="font-size:10px;color:var(--dim);">用时: ${elapsed}s</div>
    `;
    snapshotEl.appendChild(card);
  }

  // Board slots (blackboard)
  if (snapshot.slots && typeof snapshot.slots === "object") {
    const card = document.createElement("div");
    card.className = "snapshot-mini";
    card.innerHTML = `<div class="sm-title">看板字段</div>`;
    const tbl = document.createElement("table");
    tbl.className = "sm-table";
    tbl.innerHTML = "<thead><tr><th>字段</th><th>值</th><th class='sm-ver'>版本</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const [k, v] of Object.entries(snapshot.slots)) {
      const tr = document.createElement("tr");
      const valStr = typeof v === "object" && v !== null ? JSON.stringify(v.value) : String(v);
      const ver = typeof v === "object" && v !== null ? v.version : "";
      tr.innerHTML = `<td>${escapeHtml(k)}</td><td>${escapeHtml(truncate(valStr, 50))}</td><td class="sm-ver">${escapeHtml(String(ver))}</td>`;
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    card.appendChild(tbl);
    snapshotEl.appendChild(card);
  }

  // Queue counters (work queue)
  if (typeof snapshot.pending === "number") {
    const card = document.createElement("div");
    card.className = "snapshot-mini";
    const total = (snapshot.pending || 0) + (snapshot.claimed || 0) + (snapshot.completed || 0) + (snapshot.dead_lettered || 0);
    const done = (snapshot.completed || 0) + (snapshot.dead_lettered || 0);
    const pct = total > 0 ? Math.round(done / total * 100) : 0;
    card.innerHTML = `
      <div class="sm-title">队列进度</div>
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
        <span style="font-size:14px;font-weight:700;">${done}/${total}</span>
        <span style="font-size:10px;color:var(--dim);">(${pct}%)</span>
      </div>
      <div class="snapshot-phase-bar">
        <div class="fill" style="width:${pct}%;${pct >= 100 ? 'background:var(--green);' : ''}"></div>
      </div>
    `;
    const grid = document.createElement("div");
    grid.className = "sm-counters";
    grid.style.marginTop = "6px";
    for (const [k, label, cls] of [
      ["pending", "待处理", ""],
      ["claimed", "已认领", ""],
      ["completed", "已完成", "ok"],
      ["dead_lettered", "已死信", "bad"],
    ]) {
      if (typeof snapshot[k] === "number") {
        grid.innerHTML += `<div class="sm-counter ${cls}"><div class="sm-val">${snapshot[k]}</div><div class="sm-label">${label}</div></div>`;
      }
    }
    card.appendChild(grid);
    snapshotEl.appendChild(card);
  }

  // Floor counters (ensemble/dating_chat)
  if (typeof snapshot.turn_count === "number" || typeof snapshot.round_count === "number") {
    const card = document.createElement("div");
    card.className = "snapshot-mini";
    card.innerHTML = `<div class="sm-title">对话区</div>`;
    const grid = document.createElement("div");
    grid.className = "sm-counters";
    for (const [k, label] of [["turn_count", "发言数"], ["round_count", "轮次"], ["member_count", "在场人数"]]) {
      if (typeof snapshot[k] === "number") {
        grid.innerHTML += `<div class="sm-counter"><div class="sm-val">${snapshot[k]}</div><div class="sm-label">${label}</div></div>`;
      }
    }
    card.appendChild(grid);
    if (snapshot.topic) {
      const t = document.createElement("div");
      t.style.cssText = "font-size:11px;color:var(--dim);margin-top:6px;";
      t.textContent = `话题: ${snapshot.topic}`;
      card.appendChild(t);
    }
    snapshotEl.appendChild(card);
  }

  if (typeof snapshot.elapsed_s === "number") {
    const card = document.createElement("div");
    card.className = "snapshot-mini";
    card.innerHTML = `<div class="sm-title">用时</div><div style="font-size:18px;font-weight:700;font-family:SF Mono,Menlo,monospace;">${snapshot.elapsed_s.toFixed(2)}s</div>`;
    snapshotEl.appendChild(card);
  }
}

// ── Chat bubble rendering (dating_chat turn events) ──
function renderTurnBubble(env) {
  const p = env.payload || {};
  const speaker = p.speaker || env.actor || "?";
  const isNiu = p.is_niu !== undefined ? p.is_niu : speaker === "大牛";
  const sideClass = isNiu ? "niu" : "mei";

  const row = document.createElement("div");
  row.className = `chat-row ${sideClass}`;

  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.textContent = SPEAKER_AVATAR[speaker] || speaker.slice(0, 1);

  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.textContent = p.text || "";

  if (isNiu) row.append(avatar, bubble);
  else row.append(bubble, avatar);
  streamEl.appendChild(row);

  const memoryHits = p.memory_hits || 0;
  const memoryPreviews = p.memory_previews || [];
  if (memoryHits > 0) {
    const preview = memoryPreviews.length > 0 ? memoryPreviews[0] : "";
    if (!isNiu && preview && preview === lastMeiMemoryPreview) {
      appendBadge(sideClass, `🧠 命中记忆 ${memoryHits} 条（与上一轮相同）`, "memory");
    } else {
      appendBadge(sideClass, `🧠 命中记忆 ${memoryHits} 条 · ${preview}`, "memory");
      if (!isNiu) lastMeiMemoryPreview = preview;
    }
  }
  const toolCalls = p.tool_calls || [];
  if (Array.isArray(toolCalls) && toolCalls.length > 0) {
    appendBadge(sideClass, `🔧 ${toolCalls.join(", ")} 已调用`);
  }
  if (p.compression && p.compression !== "NONE") {
    appendBadge(sideClass, `⚡ 触发上下文压缩：${p.compression}（超出预算的工具结果/历史被压缩）`, "compress");
    if (p.tool_compress_sample) {
      appendBadge(sideClass, `📦 压缩后仍保留：${p.tool_compress_sample}`, "compress");
    }
    if (p.history_summary) {
      appendBadge(sideClass, `📝 摘要：${p.history_summary}`, "compress");
    }
  }
  if (p.niu_note) {
    appendBadge(sideClass, `⚠️ ${p.niu_note}`, "toy");
  }
  scrollDown();
}

function appendBadge(sideClass, label, variant = "") {
  const row = document.createElement("div");
  row.className = `chat-row ${sideClass}`;
  const badge = document.createElement("div");
  badge.className = variant ? `chat-badge ${variant}` : "chat-badge";
  badge.textContent = label;
  row.appendChild(badge);
  streamEl.appendChild(row);
}

// ── Generic card (for unknown event kinds) ──
function renderGenericCard(env) {
  const card = document.createElement("div");
  card.className = "ma-card";
  card.innerHTML = `
    <div class="ma-header">
      <span class="ma-actor">${escapeHtml(env.actor || "system")}</span>
      <span class="ma-trigger">${escapeHtml(env.kind || "?")}</span>
      <span class="ma-ts">${phaseLabel(env.phase)}</span>
    </div>
    <div class="ma-body">${escapeHtml(JSON.stringify(env.summary || {}))}</div>
  `;
  attachPayload(card, env.payload || {});
  streamEl.appendChild(card);
}

function attachPayload(card, payload) {
  if (!payload || Object.keys(payload).length === 0) return;
  const el = document.createElement("div");
  el.className = "ma-payload hidden";
  el.textContent = JSON.stringify(payload, null, 2);
  card.appendChild(el);
  card.onclick = () => el.classList.toggle("hidden");
}

function phaseLabel(phase) {
  return phase ? escapeHtml(PHASE_LABELS[phase] || phase) : "";
}

// ── Approval flow ──────────────────────────────────────────────────────────
function showSuspended(ev) {
  const el = document.createElement("div");
  el.className = "suspended-card";
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = `⏸ SUSPENDED — request=${short(ev.request_id)}`;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = "等待人工审批。弹出框中选择 Approve 或 Reject。";
  el.appendChild(label);
  el.appendChild(body);
  streamEl.appendChild(el);
  scrollDown();
}

function closeAllApprovalModals() {
  document.querySelectorAll(".modal-overlay").forEach((el) => el.remove());
}

function showApprovalModal(ev) {
  // If a modal is already open for this request, don't stack.
  if (document.querySelector(".modal-overlay")) return;

  const req = lastApprovalRequest;
  lastApprovalRequest = null;
  const toolName = req ? req.name : "?";
  const toolParams = req && req.params ? JSON.stringify(req.params, null, 2) : "";
  const toolLevel = req ? (req.level || "HIGH") : "HIGH";

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `
    <h3>⛔ HITL Approval Required</h3>
    <div class="modal-body">
      <div class="modal-run-info">run: ${escapeHtml(short(ev.run_id || ""))} · request: ${escapeHtml(short(ev.request_id || ""))}</div>
      <div class="modal-tool-info">
        <span class="modal-tool-label">工具</span>
        <span class="modal-tool-name">${escapeHtml(toolName)}</span>
        <span class="level-badge ${toolLevel.toLowerCase()}">write ${escapeHtml(toolLevel)}</span>
      </div>
      ${toolParams ? `<pre class="modal-tool-params">${escapeHtml(toolParams)}</pre>` : ""}
      <div class="modal-hint">Approve 执行此写操作；Reject 否决本步，系统保留已完成步骤并增量重规划。</div>
    </div>
    <div class="modal-actions">
      <button class="btn-reject">✗ Reject</button>
      <button class="btn-approve">✓ Approve</button>
    </div>
  `;
  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  const approveBtn = modal.querySelector(".btn-approve");
  const rejectBtn = modal.querySelector(".btn-reject");

  const close = () => overlay.remove();

  const submit = async (decision) => {
    if (submittedRequestIds.has(ev.request_id)) return;
    submittedRequestIds.add(ev.request_id);
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      const res = await fetch("/api/approve", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          run_id: ev.run_id,
          request_id: ev.request_id,
          decision,
        }),
      });
      if (!res.ok) {
        const err = await res.text();
        showError(`审批提交失败: ${err}`);
        submittedRequestIds.delete(ev.request_id);  // allow retry on server error
      } else {
        statusEl.textContent = `approved → resuming…`;
        if (hudState.pending > 0) {
          hudState.pending--;
          updateHud();
        }
        streamRun(ev.run_id);
      }
    } catch (e) {
      showError(`网络错误: ${e}`);
      submittedRequestIds.delete(ev.request_id);
    }
    close();
  };

  approveBtn.onclick = () => submit("approve");
  rejectBtn.onclick = () => submit("reject");
}

// ── Event rendering — mirrors console.py _HANDLERS ─────────────────────────
const RENDER = {
  "session.start": (ev) => {
    hudState.sessionStartTs = Date.now();
    if (runIdStack.length === 0 && ev.run_id) {
      pushRunId(ev.run_id, 0, hudState.agent);
    }
    return {label: "SESSION ▶", color: "blue",
      body: `Starting  run=${short(ev.run_id)}  ${dim(ev.task?.slice(0, 60))}`};
  },
  "session.end": (ev) => ({label: "SESSION ■", color: ev.state === "completed" ? "green" : (ev.state === "failed" ? "red" : "blue"),
    body: `End  state=${ev.state || "?"}  ${ev.turns || 0} turns  $${ev.cost_usd || 0}`}),
  "context.build": (ev) => {
    const lt = ev.layer_tokens || {};
    const l0 = lt.L0 ?? ev.system_tokens ?? 0;
    const l1 = lt.L1 ?? 0;
    const l2 = lt.L2 ?? 0;
    const l3 = lt.L3 ?? 0;
    const max = ev.max_tokens || 0;
    const total = ev.total_tokens || 0;
    const pct = max > 0 ? Math.min(100, total / max * 100) : 0;
    let compress = `Compress: ${ev.compression || "NONE"}`;
    if (ev.compression && ev.compression !== "NONE" && ev.pre_history_tokens > l3) {
      compress += `  ${ev.pre_history_tokens}→${l3} (−${ev.pre_history_tokens - l3})`;
    }
    const ref = Math.max(total, 1);
    const layerRow = (label, val, cls) => {
      const w = Math.min(100, val / ref * 100);
      return `<div class="ctx-layer"><span class="ctx-layer-label">${label}</span><div class="ctx-layer-bar ${cls}"><div class="ctx-layer-fill" style="width:${w}%"></div></div><span class="ctx-layer-tok">${val.toLocaleString()}</span></div>`;
    };
    const layers = [
      layerRow("sys", l0, "ctx-l0"),
      layerRow("state", l1, "ctx-l1"),
      layerRow("mem", l2, "ctx-l2"),
      layerRow("hist", l3, "ctx-l3"),
    ].join("");
    const ringDeg = (pct / 100 * 360).toFixed(0);
    const ring = `<div class="ctx-ring" style="background:conic-gradient(var(--cyan) ${ringDeg}deg, var(--border) 0deg)"><span>${pct.toFixed(0)}%</span></div>`;
    const compressCls = (ev.compression && ev.compression !== "NONE") ? " ctx-compress-active" : "";
    return {
      label: "CONTEXT", color: "blue",
      body: `<div class="ctx-header">${ring}<div class="ctx-total"><div>${total.toLocaleString()} / ${max.toLocaleString()} tok</div><div class="ctx-compress${compressCls}">${escapeHtml(compress)}</div></div></div><div class="ctx-layers">${layers}</div>`,
    };
  },
  "memory.recall": (ev) => {
    const previews = ev.previews || [];
    const previewHtml = previews.length
      ? `<div class="mem-previews collapsible-body collapsed">${previews.map((p) => `<div class="mem-preview">${escapeHtml(p)}</div>`).join("")}</div>`
      : "";
    return {
      label: "MEMORY", color: "blue", collapsible: previews.length > 0,
      body: `<span class="mem-hits">${ev.hits || 0}</span> <span class="dim">hits for ${escapeHtml(repr(ev.query))}</span>${previewHtml}`,
    };
  },
  "memory.classify": (ev) => ({label: "MEMORY", color: "dim",
    body: `scanned ${ev.scanned || 0} → wrote ${ev.written || 0}`}),
  "injection.failed": (ev) => ({label: "INJECT", color: "red",
    body: `FAILED at ${ev.point || "?"}  ${dim(ev.injector || "")}`}),
  "skills.ready": (ev) => {
    const names = ev.names || [];
    const badges = names.map((n) => `<span class="skill-badge" title="${escapeHtml(n)}">${escapeHtml(n)}</span>`).join("");
    return {
      label: "SKILLS", color: "blue",
      body: `<div class="skill-badges"><span class="dim">${names.length} runbooks</span>${badges}</div>`,
    };
  },
  "turn.start": (ev) => {
    hudState.turn = ev.turn || hudState.turn;
    hudState.maxTurns = ev.max_turns || hudState.maxTurns;
    updateHud();
    return null;
  },
  "llm.request": (ev) => {
    const planning = ev.phase === "planning";
    const sysLen = ev.system_len || ev.system?.length || 0;
    const sysPreview = ev.system ? escapeHtml(ev.system.slice(0, 120)) + (ev.system.length > 120 ? "…" : "") : "";
    const sysHtml = sysPreview ? `<div class="llm-sys collapsible-body collapsed"><div class="dim">system:</div><pre>${sysPreview}</pre></div>` : "";
    return {
      label: planning ? "PLANNING" : "LLM CALL",
      color: planning ? "yellow" : "cyan",
      collapsible: !!sysPreview,
      body: `calling LLM  ${dim(`(${ev.msg_count || 0} msgs, system≈${sysLen} chars)`)}`,
      bodyExtra: sysHtml,
    };
  },
  "llm.think": (ev) => null,  // handled specially — streaming append
  "tool.call": (ev) => {
    if (ev.name === "spawn_agent" || (ev.name || "").startsWith("handoff_to_")) {
      hudState.tools++;
      updateHud();
      return null;
    }
    hudState.tools++;
    updateHud();
    const level = (ev.side_effect_level || "low").toLowerCase();
    const kind = ev.readonly ? "readonly" : "write";
    return {
      label: "TOOL CALL", color: "cyan",
      body: `${ev.name || "?"} ${levelBadge(kind, level)}`,
      payload: ev.params,
      toolPair: {callId: ev.call_id, kind, level, readonly: !!ev.readonly},
    };
  },
  "approval.request": (ev) => {
    hudState.pending++;
    updateHud();
    lastApprovalRequest = ev;  // cache for modal display
    return {label: "APPROVAL", color: "red",
      body: `REQUEST  ${ev.name || "?"}  ${levelBadge("write", ev.level || "HIGH")}`,
      payload: ev.params};
  },
  "tool.result": (ev) => {
    // Suppress spawn_agent / handoff_to_* results — covered by SUB-AGENT / HANDOFF cards.
    if (ev.name === "spawn_agent" || (ev.name || "").startsWith("handoff_to_")) {
      return null;
    }
    return {
      label: "← RESULT", color: "green",
      body: `${ev.name || "?"}  ${dim(truncate(JSON.stringify(ev.result), 80))}  [${(ev.elapsed_ms || 0).toFixed(2)}ms]`,
      payload: ev.result,
      toolResult: {callId: ev.call_id, elapsedMs: ev.elapsed_ms || 0, result: ev.result},
    };
  },
  "plan.ready": (ev) => {
    const steps = ev.nodes || [];
    return {label: "PLAN", color: "yellow",
      body: `Agent=${ev.agent || "?"} v${ev.version || 1}  ${steps.length} steps\n${renderPlanDag(steps)}`,
      payload: steps, planDag: true};
  },
  "plan.replanned": (ev) => ({label: "REPLAN", color: "yellow",
    body: `#${ev.replan_count || 1} v${ev.version || 1}  failed=${ev.failed_step || "?"}  new=${dim((ev.new_nodes || []).map((s) => s.id).join(", "))}`}),
  "node.started": (ev) => {
    setPlanNodeStatus(ev.node_id, "running");
    return {label: "STEP", color: "yellow", body: `▶ ${ev.node_id}:${ev.action}`};
  },
  "node.completed": (ev) => {
    setPlanNodeStatus(ev.node_id, "done");
    return {label: "STEP", color: "green", body: `✓ ${ev.node_id}:${ev.action}  ${dim(truncate(JSON.stringify(ev.result), 60))}`};
  },
  "node.failed": (ev) => {
    setPlanNodeStatus(ev.node_id, "failed");
    return {label: "STEP", color: "red", body: `✗ ${ev.node_id}:${ev.action}  ${ev.error || ""}`};
  },
  "skill.load": (ev) => ({label: "SKILL", color: "magenta", body: `Loaded ${repr(ev.name)} (${ev.chars || 0} chars)`}),
  "budget.token_update": (ev) => {
    // Sync HUD — single source of truth for run-wide counters.
    hudState.turn = ev.turn || hudState.turn;
    hudState.maxTurns = ev.max_turns || hudState.maxTurns;
    hudState.inputTokens = ev.input_tokens ?? hudState.inputTokens;
    hudState.outputTokens = ev.output_tokens ?? hudState.outputTokens;
    hudState.costUsd = ev.cost_usd ?? hudState.costUsd;
    hudState.budgetUsd = ev.budget_usd ?? hudState.budgetUsd;
    updateHud();
    return {label: "BUDGET", color: "dim",
      body: `Tokens +${ev.output_tokens || 0} (Σ${(ev.input_tokens || 0) + (ev.output_tokens || 0)}) | ${fmtCost(ev.cost_usd)}${ev.budget_usd ? "/" + fmtCost(ev.budget_usd) : ""}`};
  },
  "agent.spawn": (ev) => {
    hudState.subAgents++;
    updateHud();
    let container = null;
    if (ev.child_run_id) {
      pushRunId(ev.child_run_id, ev.depth || runIdStack.length, ev.name);
      if (!subAgentContainers.has(ev.child_run_id)) {
        const hue = hueForSubAgent(ev.name);
        container = makeSubAgentContainer(ev.name, hue);
        subAgentContainers.set(ev.child_run_id, container);
      }
    }
    return {label: "SUB-AGENT", color: "magenta", subAgentName: ev.name, childRunId: ev.child_run_id,
      mountContainer: container,
      body: `Spawning ${ev.name || "?"}  ${dim(repr(ev.task?.slice(0, 80)))}`};
  },
  "agent.result": (ev) => {
    const container = ev.child_run_id ? subAgentContainers.get(ev.child_run_id) : null;
    if (container) {
      const statusEl = container.el.querySelector(".subagent-status");
      if (statusEl) {
        statusEl.textContent = ev.state || "?";
        statusEl.className = `subagent-status ${ev.state || ""}`;
      }
    }
    if (ev.child_run_id) {
      popRunId(ev.child_run_id);
    }
    return {label: "SUB-AGENT", color: ev.state === "completed" ? "green" : "red",
      subAgentName: ev.name, childRunId: ev.child_run_id,
      body: `${ev.name || "?"} → ${(ev.state || "?").toUpperCase()}  ${ev.turns || 0} turns`};
  },
  "peer.handoff": (ev) => {
    hudState.subAgents++;
    updateHud();
    let container = null;
    if (ev.child_run_id) {
      pushRunId(ev.child_run_id, ev.depth || runIdStack.length, ev.to_agent);
      if (!subAgentContainers.has(ev.child_run_id)) {
        const hue = hueForSubAgent(ev.to_agent);
        container = makeSubAgentContainer(ev.to_agent, hue);
        subAgentContainers.set(ev.child_run_id, container);
      }
    }
    return {label: "HANDOFF", color: "magenta",
      subAgentName: ev.to_agent, childRunId: ev.child_run_id,
      mountContainer: container,
      body: `${ev.from_agent || "?"} ⇄ ${ev.to_agent || "?"}  ${dim(repr(ev.task?.slice(0, 80)))}`};
  },
  "run.complete": (ev) => {
    // Clear any remaining peer/sub-agent frames — the root run is done.
    while (runIdStack.length > 1) runIdStack.pop();
    return {label: "RUN", color: ev.state === "completed" ? "green" : "red",
      body: `${(ev.state || "?").toUpperCase()}  ${ev.turns || 0} turns | ${(ev.total_tokens || 0).toLocaleString()} tokens | $${ev.cost_usd || 0}`};
  },
  "learning.synthesize": (ev) => ({label: "LEARNING", color: "magenta", body: `${ev.action || "?"} ${repr(ev.name || "")}`}),
};

// ── Plan DAG rendering ────────────────────────────────────────────────────
function renderPlanDag(steps) {
  if (!steps || !steps.length) return "";
  const byId = {};
  steps.forEach((s) => { byId[s.id] = s; });
  // Longest-path layering: layer(n) = 1 + max(layer(dep)) over deps in this plan.
  const layer = {};
  function getLayer(id) {
    if (layer[id] !== undefined) return layer[id];
    const deps = (byId[id]?.depends_on || []).filter((d) => byId[d]);
    layer[id] = deps.length ? 1 + Math.max(...deps.map(getLayer)) : 0;
    return layer[id];
  }
  steps.forEach((s) => getLayer(s.id));
  const rows = {};
  steps.forEach((s) => {
    const l = layer[s.id];
    (rows[l] = rows[l] || []).push(s);
  });
  const maxLayer = Math.max(...Object.keys(rows).map(Number));
  const rowHtml = [];
  for (let l = 0; l <= maxLayer; l++) {
    const row = rows[l] || [];
    const nodes = row.map((s) => {
      const term = s.is_terminal ? " plan-node-term" : "";
      return `<span class="plan-node pending${term}" data-step-id="${escapeHtml(s.id)}" title="${escapeHtml(s.action)}">${escapeHtml(s.id)}</span>`;
    }).join("");
    rowHtml.push(`<div class="plan-row">${nodes}</div>`);
  }
  const arrow = maxLayer > 0 ? '<div class="plan-arrow">→</div>' : "";
  // Join rows with arrows using flex direction column; here we inline them.
  return `<div class="plan-dag">${rowHtml.join(arrow)}</div>`;
}

function setPlanNodeStatus(stepId, status) {
  const node = document.querySelector(`.plan-node[data-step-id="${cssEsc(stepId)}"]`);
  if (!node) return;
  node.classList.remove("pending", "running", "done", "failed");
  node.classList.add(status);
}

function cssEsc(s) {
  // Minimal CSS escape for attribute selector (quotes & backslash).
  return String(s).replace(/["\\]/g, "\\$&");
}


// call_id → tool pair card, so tool.result can fill the right side.
const toolPairCards = new Map();

const subAgentContainers = new Map();  // child_run_id → {el, body, name, hue}

function makeSubAgentContainer(name, hue) {
  const el = document.createElement("div");
  el.className = `subagent-group accent-${hue}`;
  const header = document.createElement("div");
  header.className = "subagent-header";
  header.innerHTML = `<span class="subagent-icon">⋐</span><span class="subagent-name">${escapeHtml(name || "?")}</span><span class="subagent-status">running</span>`;
  const body = document.createElement("div");
  body.className = "subagent-body";
  el.appendChild(header);
  el.appendChild(body);
  header.onclick = () => el.classList.toggle("collapsed");
  return {el, body, name, hue};
}

// ── Event icon mapping (visual scan aid) ──
const EVENT_ICON = {
  "session.start":   "▶",
  "session.end":     "■",
  "context.build":   "◆",
  "memory.recall":   "◇",
  "memory.classify": "◇",
  "injection.failed":"⚠",
  "skills.ready":    "⚙",
  "skill.load":      "⚙",
  "llm.request":     "💭",
  "llm.think":       "💭",
  "tool.call":       "🔧",
  "tool.result":     "✓",
  "approval.request":"⏸",
  "plan.ready":      "▦",
  "plan.replanned":  "▦",
  "node.started":    "▶",
  "node.completed":  "✓",
  "node.failed":     "✗",
  "agent.spawn":     "⋐",
  "agent.result":    "⋑",
  "peer.handoff":    "⇄",
  "budget.token_update": "○",
  "run.complete":    "✓",
  "run.failed":      "✗",
  "learning.synthesize": "✦",
};

function iconFor(eventName) { return EVENT_ICON[eventName] || "·"; }

// ── Sub-agent color coding — hash name to a stable hue ──
const SUB_AGENT_HUES = ["magenta", "yellow", "green", "cyan", "blue", "red"];
const subAgentHueMap = new Map();
function hueForSubAgent(name) {
  if (!name) return "magenta";
  if (subAgentHueMap.has(name)) return subAgentHueMap.get(name);
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const hue = SUB_AGENT_HUES[h % SUB_AGENT_HUES.length];
  subAgentHueMap.set(name, hue);
  return hue;
}

function tsLabel(ev) {
  // Relative seconds since session.start — uses frontend receive time.
  if (!hudState.sessionStartTs) return "";
  const dt = (ev._receivedTs - hudState.sessionStartTs) / 1000;
  if (dt < 0) return "";
  return `+${dt.toFixed(1)}s`;
}

// ── Sub-agent depth tracking via run_id stack ──
const runIdStack = [];

function pushRunId(runId, depth, name) {
  if (!runId) return;
  // Avoid double-push (e.g. session.start after agent.spawn for same child).
  if (runIdStack.some((e) => e.runId === runId)) return;
  runIdStack.push({runId, depth, name});
}

function popRunId(runId) {
  const idx = runIdStack.findIndex((e) => e.runId === runId);
  if (idx >= 0) runIdStack.splice(idx, 1);
}

function depthForRunId(runId) {
  if (!runId || runIdStack.length === 0) return 0;
  // Exact match.
  for (const entry of runIdStack) {
    if (entry.runId === runId) return entry.depth;
  }
  // No match — assume root (event fired before session.start registered).
  return 0;
}

function subAgentNameForRunId(runId) {
  if (!runId || runIdStack.length === 0) return null;
  for (const entry of runIdStack) {
    if (entry.runId === runId) return entry.name;
  }
  return null;
}

function mountTarget(ev) {
  if (ev.run_id && subAgentContainers.has(ev.run_id)) {
    return subAgentContainers.get(ev.run_id).body;
  }
  return streamEl;
}

function renderHook(ev) {
  ev._receivedTs = Date.now();
  const icon = iconFor(ev.event_name);
  const ts = tsLabel(ev);
  // llm.think streams tokens into the current THINK card.
  if (ev.event_name === "llm.think") {
    if (!currentThinkCard) {
      const depth = depthForRunId(ev.run_id);
      const subName = subAgentNameForRunId(ev.run_id);
      const accent = depth > 0 ? hueForSubAgent(subName) : null;
      const card = makeCard("THINK", "magenta", "", icon, ts, accent);
      card.bodyEl.classList.add("markdown-body", "think-body", "collapsed");
      const target = mountTarget(ev);
      if (target === streamEl && depth > 0) card.el.style.marginLeft = `${depth * 20}px`;
      card.el.classList.add("think-card");
      card.el.onclick = () => {
        card.bodyEl.classList.toggle("collapsed");
      };
      target.appendChild(card.el);
      currentThinkCard = card;
      currentThinkCard._rawText = "";
    }
    currentThinkCard._rawText += ev.text || "";
    // Plain-text during streaming (markdown would flicker on partial tables).
    currentThinkCard.bodyEl.textContent = currentThinkCard._rawText;
    scrollDown();
    return;
  }
  // Any non-think event ends the current think card — re-render as markdown.
  if (currentThinkCard) {
    currentThinkCard.bodyEl.innerHTML = renderMarkdown(currentThinkCard._rawText || "");
    currentThinkCard = null;
  }

  const renderer = RENDER[ev.event_name];
  if (!renderer) return;
  const r = renderer(ev);
  if (!r) return;

  // Tool result — try to fill the paired call card's right side.
  if (r.toolResult && r.toolResult.callId && toolPairCards.has(r.toolResult.callId)) {
    fillToolResultSide(toolPairCards.get(r.toolResult.callId), r.toolResult);
    scrollDown();
    return;
  }

  // Tool call — create a paired card with pending result side.
  if (r.toolPair && r.toolPair.callId) {
    const pairEl = makeToolPairCard(r.toolPair, r.body, r.payload, icon, ts);
    toolPairCards.set(r.toolPair.callId, pairEl);
    const depth = depthForRunId(ev.run_id);
    const subName = subAgentNameForRunId(ev.run_id);
    const target = mountTarget(ev);
    if (target === streamEl && depth > 0) {
      pairEl.el.style.marginLeft = `${depth * 20}px`;
      pairEl.el.classList.add(`accent-${hueForSubAgent(subName)}`);
    }
    target.appendChild(pairEl.el);
    scrollDown();
    return;
  }

  const depth = depthForRunId(ev.run_id);
  const subName = subAgentNameForRunId(ev.run_id);
  const accent = depth > 0 ? hueForSubAgent(subName) : (r.subAgentName ? hueForSubAgent(r.subAgentName) : null);
  const card = makeCard(r.label, r.color, r.body, icon, ts, accent, r.collapsible, r.bodyExtra);
  const target = mountTarget(ev);
  if (target === streamEl && depth > 0) {
    card.el.style.marginLeft = `${depth * 20}px`;
    card.el.classList.add(`depth-${depth}`);
  }
  if (r.payload !== undefined) {
    card.el.classList.add("collapsible");
    const payloadEl = document.createElement("div");
    payloadEl.className = "payload hidden";
    payloadEl.textContent = JSON.stringify(r.payload, null, 2);
    card.el.appendChild(payloadEl);
    card.el.onclick = () => payloadEl.classList.toggle("hidden");
  }
  target.appendChild(card.el);
  if (r.mountContainer) {
    target.appendChild(r.mountContainer.el);
  }
  scrollDown();
}

function makeToolPairCard(pair, callBodyHtml, params, icon, ts) {
  const el = document.createElement("div");
  el.className = "tool-pair-card";

  // Header row: icon + TOOL label + timestamp (spans both columns).
  if (icon || ts) {
    const header = document.createElement("div");
    header.className = "tool-pair-header";
    if (icon) {
      const iconEl = document.createElement("span");
      iconEl.className = "icon";
      iconEl.textContent = icon;
      header.appendChild(iconEl);
    }
    const lbl = document.createElement("span");
    lbl.className = "side-label";
    lbl.style.color = "var(--cyan)";
    lbl.textContent = "TOOL";
    header.appendChild(lbl);
    if (ts) {
      const tsEl = document.createElement("span");
      tsEl.className = "ts";
      tsEl.textContent = ts;
      header.appendChild(tsEl);
    }
    el.appendChild(header);
  }

  const callSide = document.createElement("div");
  callSide.className = "tool-side tool-call-side";
  const callLabel = document.createElement("div");
  callLabel.className = "side-label";
  callLabel.textContent = "▸ CALL";
  const callBody = document.createElement("div");
  callBody.className = "side-body";
  callBody.innerHTML = callBodyHtml;
  callSide.appendChild(callLabel);
  callSide.appendChild(callBody);

  // Params payload: collapsed by default — click the card to expand.
  const payloadEl = document.createElement("div");
  payloadEl.className = "payload hidden";
  payloadEl.textContent = JSON.stringify(params, null, 2);
  callSide.appendChild(payloadEl);

  const resultSide = document.createElement("div");
  resultSide.className = "tool-side tool-result-side pending";
  const resultLabel = document.createElement("div");
  resultLabel.className = "side-label";
  resultLabel.textContent = "⏳ RESULT";
  const resultBody = document.createElement("div");
  resultBody.className = "side-body";
  resultBody.innerHTML = `<span class="dim">running…</span>`;
  resultSide.appendChild(resultLabel);
  resultSide.appendChild(resultBody);

  el.appendChild(callSide);
  el.appendChild(resultSide);
  el.classList.add("collapsible");
  el.onclick = () => payloadEl.classList.toggle("hidden");
  return {el, callSide, resultSide, payloadEl, pair};
}

function fillToolResultSide(pairCard, toolResult) {
  const {resultSide, pair} = pairCard;
  resultSide.classList.remove("pending");
  const label = resultSide.querySelector(".side-label");
  label.textContent = "✓ RESULT";
  const body = resultSide.querySelector(".side-body");
  const ms = toolResult.elapsedMs;
  body.innerHTML = `${dim(truncate(JSON.stringify(toolResult.result), 100))}<div class="elapsed-bar"><div class="elapsed-fill" style="width:${elapsedBarWidth(ms)}%"></div></div>${dim(`[${ms.toFixed(1)}ms]`)}`;

  // Result payload: collapsed by default — click the card to expand.
  const payloadEl = document.createElement("div");
  payloadEl.className = "payload hidden";
  payloadEl.textContent = JSON.stringify(toolResult.result, null, 2);
  resultSide.appendChild(payloadEl);
  // Click toggles both payloads.
  pairCard.el.onclick = () => {
    pairCard.payloadEl.classList.toggle("hidden");
    payloadEl.classList.toggle("hidden");
  };
}

function elapsedBarWidth(ms) {
  // Log scale: 1ms→10%, 10ms→30%, 100ms→60%, 1000ms→90%, saturate at 100%.
  if (ms <= 0) return 0;
  const w = 10 + 30 * Math.min(1, Math.log10(ms + 1));
  return Math.min(100, Math.round(w));
}

function makeCard(label, color, bodyHtml, icon, ts, accentColor, collapsible, bodyExtra) {
  const el = document.createElement("div");
  el.className = `event-card ${color}`;
  if (accentColor) el.classList.add(`accent-${accentColor}`);
  if (collapsible) el.classList.add("collapsible-card");
  if (icon) {
    const iconEl = document.createElement("span");
    iconEl.className = "icon";
    iconEl.textContent = icon;
    el.appendChild(iconEl);
  }
  const labelEl = document.createElement("div");
  labelEl.className = "label";
  labelEl.textContent = label;
  const bodyEl = document.createElement("div");
  bodyEl.className = "body";
  bodyEl.innerHTML = bodyHtml;
  if (bodyExtra) bodyEl.innerHTML += bodyExtra;
  el.appendChild(labelEl);
  el.appendChild(bodyEl);
  if (ts) {
    const tsEl = document.createElement("span");
    tsEl.className = "ts";
    tsEl.textContent = ts;
    el.appendChild(tsEl);
  }
  if (collapsible) {
    el.onclick = (e) => {
      // Toggle only the collapsible-body children, not the whole card.
      const targets = el.querySelectorAll(".collapsible-body");
      targets.forEach((t) => t.classList.toggle("collapsed"));
    };
  }
  return {el, bodyEl};
}

function showFinal(finalOutput) {
  const el = document.createElement("div");
  el.className = "final-card";
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = "✓ FINAL OUTPUT";
  const body = document.createElement("div");
  body.className = "markdown-body";
  body.style.marginTop = "4px";
  body.innerHTML = renderMarkdown(finalOutput || "(空)");
  el.appendChild(label);
  el.appendChild(body);
  streamEl.appendChild(el);
  scrollDown();
}

function showError(error) {
  const el = document.createElement("div");
  el.className = "error-card";
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = "✗ ERROR";
  const body = document.createElement("div");
  body.style.marginTop = "4px";
  body.textContent = error || "未知错误";
  el.appendChild(label);
  el.appendChild(body);
  streamEl.appendChild(el);
  scrollDown();
}

// ── helpers ────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return (s || "").replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
}

// ── minimal markdown renderer ──────────────────────────────────────────────
// Supports: headings, code fences, tables, ordered/unordered lists, blockquotes,
// bold/italic/inline-code, hr, paragraphs. No HTML pass-through (XSS-safe).
function renderMarkdown(md) {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;

  const inline = (s) => escapeHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[^_])_([^_]+)_/g, '$1<em>$2</em>');

  while (i < lines.length) {
    const line = lines[i];

    // Code fence
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // closing fence
      out.push(`<pre><code class="lang-${escapeHtml(lang)}">${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // Blank line
    if (/^\s*$/.test(line)) { i++; continue; }

    // Heading
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = h[1].length;
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`);
      i++;
      continue;
    }

    // Horizontal rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    // Blockquote
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`);
      continue;
    }

    // Table — header row + separator + body rows.
    if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && /\|/.test(lines[i + 1])) {
      const splitRow = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const headers = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const thead = `<thead><tr>${headers.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead>`;
      const tbody = `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody>`;
      out.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    // Unordered list
    if (/^\s*([-*+])\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*([-*+])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+])\s+/, ""));
        i++;
      }
      out.push(`<ul>${items.map((it) => `<li>${inline(it)}</li>`).join("")}</ul>`);
      continue;
    }

    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      out.push(`<ol>${items.map((it) => `<li>${inline(it)}</li>`).join("")}</ol>`);
      continue;
    }

    // Paragraph — gather consecutive non-blank, non-block lines.
    const buf = [line];
    i++;
    while (i < lines.length
           && !/^\s*$/.test(lines[i])
           && !/^```/.test(lines[i])
           && !/^(#{1,6})\s+/.test(lines[i])
           && !/^\s*([-*+])\s+/.test(lines[i])
           && !/^\s*\d+\.\s+/.test(lines[i])
           && !/^>\s?/.test(lines[i])
           && !(/^\s*([-*_])\1{2,}\s*$/.test(lines[i]))) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${inline(buf.join(" "))}</p>`);
  }

  return out.join("\n");
}
function dim(s) { return `<span class="dim">${escapeHtml(s || "")}</span>`; }
function repr(s) { return JSON.stringify(s || ""); }
function truncate(s, n) { return s && s.length > n ? s.slice(0, n) + "…" : (s || ""); }
function short(s) { return (s || "").slice(0, 16); }
function levelBadge(kind, level) {
  const lvl = (level || "low").toLowerCase();
  return `<span class="level-badge ${lvl}">${kind} ${lvl.toUpperCase()}</span>`;
}
function scrollDown() {
  streamSectionEl.scrollTop = streamSectionEl.scrollHeight;
  requestAnimationFrame(() => {
    streamSectionEl.scrollTop = streamSectionEl.scrollHeight;
    setTimeout(() => {
      streamSectionEl.scrollTop = streamSectionEl.scrollHeight;
    }, 0);
  });
}

loadExamples().catch((e) => { statusEl.textContent = "加载失败: " + e; });
