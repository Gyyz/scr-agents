/* SCR Agents console: a conversation with the review agents.
   The chat feed (/api/chats/<id>/events) carries messages, the lead agent's own steps and markers for the
   reviews it starts; each review streams its events (/api/runs/<id>/events) into an inline run card and
   into the activity list in the corner. Every row, finding and card opens the detail drawer. */
"use strict";

const STAGES = [["inputs", "Inputs", "ext"], ["graph", "Code graph", "ws2"], ["scanners", "Scanners", "ws2"],
  ["bench", "Test bench", "ws3"], ["review", "Review", "ws1"], ["validation", "Validation", "ws1"],
  ["report", "Report", "ws3"]];
const STAGE_LABEL = Object.fromEntries(STAGES.map(([k, l]) => [k, l]));
const TOOL_WS = { read_file: "ext", get_diff: "ext", list_changed_files: "ext", search_code: "ext", git_log: "ext",
  code_graph_query: "ws2", scanner_findings: "ws2", run_tests: "ws3", run_command: "ws3", write_file: "ws3",
  web_search: "ws4", kb_lookup: "ws5", memory_read: "ws5", memory_note: "ws5", start_review: "ws1",
  list_findings: "ws5", finding_detail: "ws5", project_info: "ext" };
const KIND_ICON = { agent: "◆", validator: "✓", tool: "⌘", llm: "✎", plan: "☰", stage: "⚙", lead: "●", mcp: "⇄" };
const KIND_LABEL = { agent: "Agent", validator: "Validator", tool: "Tool call", llm: "Model call", plan: "Plan",
  stage: "Stage", lead: "Lead agent", mcp: "MCP call" };
const ORDER = { confirmed: 0, unverified: 1, rejected: 2 };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cssVar = (ws) => (ws === "bad" ? "var(--bad)" : ws === "ok" ? "var(--ok)" : `var(--${ws})`);
const short = (s, n = 140) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n) + "…" : s; };
const fmtArgs = (inp, n = 60) => short(Object.entries(inp || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" "), n);
const fmtDur = (s) => (s == null ? "" : s < 10 ? `${s.toFixed(1)}s` : s < 90 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`);
const secs = (a, b) => (a && b ? Math.max(0, (new Date(b) - new Date(a)) / 1000) : null);
const toolWs = (t) => (String(t).startsWith("mcp_") ? "ws4" : TOOL_WS[t] || "ws1");
const api = (path, body) => fetch(path, body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

const state = {
  config: null, chats: [], runsList: [], view: null, // view: {type:"chat", id} | {type:"run", id}
  orchestrator: "deterministic", // the welcome form's choice for the next chat (review.orchestrator)
  chatES: null, chatSeq: 0, busy: false, lastLead: "",
  runs: new Map(),   // runId -> {id, es, seq, stages, status, counts, now, started, ended, report, card, errors}
  tasks: new Map(),  // `${runId}|key` -> task
  selected: null,    // {type:"task"|"finding"|"run", ...}
  activityCollapsed: false, orchCollapsed: false, mcpCollapsed: false, memCollapsed: false, renderQueued: false,
  convMemory: null,  // the last memory_used of the lead agent: what of the conversation its answer saw
  openKeys: new Set(),   // tool blocks and step lists the reader expanded (kept across live re-renders)
};

// ---------------------------------------------------------------- tiny, safe markdown
const MD_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/; // a table's delimiter row: | --- | :-: |
const TABLE_ROWS = 200; // rows shown before "… more"
function dataTable(head, rows, cell = esc) {
  const more = rows.length > TABLE_ROWS ? `<div class="muted" style="font-size:11px">… ${rows.length - TABLE_ROWS} more rows</div>` : "";
  return `<div class="tbl-wrap"><table class="data-tbl"><thead><tr>${head.map((h) => `<th>${cell(h)}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, TABLE_ROWS).map((r) => `<tr>${head.map((_, j) => `<td>${cell(r[j] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>${more}`;
}
// CSV / TSV with quoted fields ("a, b" and "" for a quote inside one)
function parseDelimited(text, sep) {
  const rows = [];
  let rowCells = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false; } else cell += c; }
    else if (c === '"' && cell === "") quoted = true;
    else if (c === sep) { rowCells.push(cell); cell = ""; }
    else if (c === "\n") { rowCells.push(cell.replace(/\r$/, "")); rows.push(rowCells); rowCells = []; cell = ""; }
    else cell += c;
  }
  if (cell || rowCells.length) { rowCells.push(cell); rows.push(rowCells); }
  return rows.filter((r) => r.some((c) => c.trim()));
}
// soft: hard-wrapped source (skills, docs) where a single newline continues the line, not a break
function md(src, { soft = false } = {}) {
  const blocks = [];
  const text = String(src ?? "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\b(F-[0-9a-f]{8})\b/g, '<span class="fid" data-fid="$1">$1</span>');
  const out = [];
  let list = null;
  let para = [];
  const lines = text.split("\n");
  const row = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join(soft ? " " : "<br>")}</p>`); para = []; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*\|.*\|\s*$/.test(line) && MD_SEP.test(lines[i + 1] || "")) { // a table: header, delimiter row, rows
      flushPara(); flushList();
      const body = [];
      for (i += 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) body.push(row(lines[i]));
      i--;
      out.push(dataTable(row(line), body, inline));
      continue;
    }
    const blk = line.match(/^\u0000(\d+)\u0000$/);
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (blk) { flushPara(); flushList(); out.push(blocks[+blk[1]]); }
    else if (ul || ol) { flushPara(); const tag = ul ? "ul" : "ol"; if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; } list.items.push((ul || ol)[1]); }
    else if (h) { flushPara(); flushList(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); }
    else if (!line.trim()) { flushPara(); flushList(); }
    else if (list && (soft || /^\s+\S/.test(line))) list.items[list.items.length - 1] += " " + line.trim(); // a wrapped list item
    else { flushList(); para.push(line); }
  }
  flushPara(); flushList();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

// ---------------------------------------------------------------- bootstrap
async function init() {
  const cfgRes = await fetch("/api/config");
  if (cfgRes.status === 401) {
    document.body.innerHTML = '<main class="auth-error"><h1>SCR Agents console</h1><p>Not authorised. Open the console through the URL printed by <code>scr-agents serve</code> (it carries the access token), or append <code>?token=&lt;token&gt;</code> to this address once.</p></main>';
    return;
  }
  state.config = await cfgRes.json();
  renderConfig();
  $("start-form").addEventListener("submit", onStart);
  $("start-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("start-form").requestSubmit(); } });
  $("demo-btn").addEventListener("click", () => startChat({ demo: true }));
  initOrchestratorSwitch();
  $("new-chat").addEventListener("click", showWelcome);
  $("composer").addEventListener("submit", onSend);
  const ci = $("composer-input");
  ci.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
  ci.addEventListener("input", () => { ci.style.height = "auto"; ci.style.height = Math.min(ci.scrollHeight, 200) + "px"; });
  $("rail-toggle").addEventListener("click", () => {
    const sh = document.querySelector(".shell");
    if (window.innerWidth <= 900) sh.classList.toggle("rail-open"); else sh.classList.toggle("rail-hidden");
  });
  $("activity-toggle").addEventListener("click", () => { state.activityCollapsed = !state.activityCollapsed; renderActivity(); });
  $("orch-toggle").addEventListener("click", () => { state.orchCollapsed = !state.orchCollapsed; renderOrchestrator(); renderActivity(); });
  $("mcp-toggle").addEventListener("click", () => { state.mcpCollapsed = !state.mcpCollapsed; renderMcp(); renderActivity(); });
  $("mem-toggle").addEventListener("click", () => { state.memCollapsed = !state.memCollapsed; renderMem(); renderActivity(); });
  for (const body of ["mcp-body", "mem-body"]) {
    $(body).addEventListener("toggle", (ev) => {
      const d = ev.target;
      if (!(d instanceof HTMLDetailsElement) || !d.dataset.key) return;
      if (d.open) state.openKeys.add(d.dataset.key); else state.openKeys.delete(d.dataset.key);
    }, true);
  }
  // expanding a tool block or a step list renders its body; the open state survives live updates
  $("thread").addEventListener("toggle", (ev) => {
    const d = ev.target;
    if (!(d instanceof HTMLDetailsElement) || !d.dataset.key || d.open === state.openKeys.has(d.dataset.key)) return;
    if (d.open) state.openKeys.add(d.dataset.key); else state.openKeys.delete(d.dataset.key);
    const card = d.closest(".run-card");
    if (card) renderRunCard(state.runs.get(card.dataset.run));
    const lead = d.closest("[data-lead-tool]");
    if (lead) renderLeadTool(lead.dataset.leadTool);
  }, true);
  $("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("cfg-menu").hidden) return toggleCfgMenu(false);
    if (!$("drawer").classList.contains("open") && !$("settings").hidden) return closeSettings();
    closeDrawer();
  });
  initSettings();
  $("thread").addEventListener("click", onThreadClick);
  state.activityCollapsed = window.innerWidth < 1200;
  state.orchCollapsed = window.innerWidth < 900;
  state.mcpCollapsed = window.innerWidth < 900 || !(state.config.mcp_servers || []).length; // nothing to show without servers
  state.memCollapsed = window.innerWidth < 1200;
  await refreshLists();
  setInterval(refreshLists, 6000);
  window.addEventListener("focus", refreshLists);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshLists(); });
  setInterval(() => { tickPrep(); if ([...state.tasks.values()].some((t) => t.status === "running") || [...state.runs.values()].some((r) => r.status === "running")) scheduleRender(); }, 1000);
  const last = state.chats[0];
  if (last && last.live) openChat(last.chat_id); else showWelcome();
}

function renderConfig() {
  const c = state.config, l = c.local || {};
  let html;
  if (c.mode === "local") html = `<span class="badge ok">local · <b>${esc(l.model || "?")}</b></span><span class="badge">validator <b>${esc(l.validator_model || "?")}</b></span>`;
  else if (c.mode === "anthropic") html = `<span class="badge warn">online · <b>${esc(c.reviewer_model)}</b></span>`;
  else html = `<span class="badge warn">offline · model server ${l.reachable ? "up" : "not reachable"}</span>`;
  $("config-badges").innerHTML = html;
}

let listsInFlight = null, listsAgain = false;
function refreshLists() {
  // coalesce: at most one request in flight plus one queued, so slow responses never pile up
  if (listsInFlight) { listsAgain = true; return listsInFlight; }
  listsInFlight = loadLists().finally(() => {
    listsInFlight = null;
    if (listsAgain) { listsAgain = false; refreshLists(); }
  });
  return listsInFlight;
}

async function loadLists() {
  try {
    const [chats, runs] = await Promise.all([api("/api/chats").then((r) => r.json()), api("/api/runs").then((r) => r.json())]);
    state.chats = chats; state.runsList = runs; renderOrchSwitch();
  } catch { return; }
  const cur = state.view;
  $("chat-list").innerHTML = state.chats.length ? state.chats.map((c) => `
    <li data-chat="${esc(c.chat_id)}" class="${cur && cur.type === "chat" && cur.id === c.chat_id ? "active" : ""}">
      <div class="c1"><span class="dot ${c.busy ? "busy" : "idle"}"></span>${esc(c.title)}</div>
      <div class="c2">${esc(c.preparing ? "getting the repository…" : c.prepare_error ? "could not get the repository" : c.first_message || "no messages yet")}</div>
      <button class="del" data-del-chat="${esc(c.chat_id)}" title="Delete this conversation" aria-label="Delete conversation ${esc(c.title)}">×</button></li>`).join("")
    : '<li class="c2 muted" style="cursor:default">none yet</li>';
  const chatRuns = new Set(state.chats.flatMap((c) => c.runs || []));
  const loose = state.runsList.filter((r) => !chatRuns.has(r.run_id));
  $("runs-count").textContent = loose.length;
  $("run-list").innerHTML = loose.map((r) => {
    const c = r.counts ? `${r.counts.confirmed}✓ ${r.counts.unverified}?` : r.status;
    return `<li data-run="${esc(r.run_id)}" class="${cur && cur.type === "run" && cur.id === r.run_id ? "active" : ""}">
      <div class="c1"><span><span class="dot ${r.status}"></span> ${esc(r.run_id)}</span><span class="muted">${esc(c)}</span></div>
      <div class="c2">${esc(r.repo.split("/").slice(-1)[0])} · ${esc(r.task)}</div>
      <button class="del" data-del-run="${esc(r.run_id)}" title="Delete this review" aria-label="Delete review ${esc(r.run_id)}">×</button></li>`;
  }).join("");
  const closeRail = () => document.querySelector(".shell").classList.remove("rail-open"); // phones: the rail is an overlay
  $("chat-list").querySelectorAll("li[data-chat]").forEach((li) => li.addEventListener("click", () => { closeRail(); openChat(li.dataset.chat); }));
  $("chat-list").querySelectorAll("[data-del-chat]").forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); deleteChat(b.dataset.delChat); }));
  $("run-list").querySelectorAll("[data-del-run]").forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); deleteRun(b.dataset.delRun); }));
  $("run-list").querySelectorAll("li[data-run]").forEach((li) => li.addEventListener("click", () => { closeRail(); openRunView(li.dataset.run); }));
}

// ---------------------------------------------------------------- deleting history
const apiDelete = (path) => fetch(path, { method: "DELETE", headers: { "content-type": "application/json" }, body: "{}" });

async function deleteChat(chatId) {
  const c = state.chats.find((x) => x.chat_id === chatId);
  const n = (c?.runs || []).length;
  const what = `the conversation "${c?.title || chatId}"${n ? ` and its ${n} review${n > 1 ? "s" : ""}` : ""}`;
  if (!confirm(`Delete ${what}?\n\nThis removes its messages, reports and traces from disk and cannot be undone. The repository's own project memory (.scr_agents/) is kept.`)) return;
  const res = await apiDelete(`/api/chats/${encodeURIComponent(chatId)}`);
  if (!res.ok) { alert((await res.json().catch(() => ({}))).error || `could not delete (${res.status})`); return; }
  if (state.view?.type === "chat" && state.view.id === chatId) showWelcome(); else refreshLists();
}

async function deleteRun(runId) {
  if (!confirm(`Delete review ${runId}?\n\nThis removes its report, activity log and traces from disk and cannot be undone.`)) return;
  const res = await apiDelete(`/api/runs/${encodeURIComponent(runId)}`);
  if (!res.ok) { alert((await res.json().catch(() => ({}))).error || `could not delete (${res.status})`); return; }
  if (state.view?.type === "run" && state.view.id === runId) showWelcome(); else refreshLists();
}

// ---------------------------------------------------------------- views
function resetView() {
  if (state.chatES) { state.chatES.close(); state.chatES = null; }
  for (const r of state.runs.values()) if (r.es) r.es.close();
  Object.assign(state, { runs: new Map(), tasks: new Map(), chatSeq: 0, busy: false, lastLead: "", selected: null, openKeys: new Set(), preparing: false, prepEl: null, orchRun: null, convMemory: null });
  closeDrawer();
  $("thread").innerHTML = "";
}

function showWelcome() {
  resetView();
  state.view = null;
  $("welcome").hidden = false; $("thread").hidden = true; $("composer").hidden = true;
  $("start-input").value = ""; $("start-error").hidden = true;
  $("start-input").focus();
  renderOrchSwitch();
  refreshLists(); renderActivity();
}

// The repository can be anywhere in the text: "check https://github.com/x/y for bugs" works too.
const REPO_TOKEN = /^(?:https?:\/\/|git@|ssh:\/\/|git:\/\/|file:\/\/|~\/|\.{0,2}\/)\S+$|^[\w.-]+\/[\w.-]+(?:\.git)?$/;
function splitStart(raw) {
  const tokens = raw.split(/\s+/);
  const i = tokens.findIndex((t) => REPO_TOKEN.test(t));
  if (i < 0) return { repo: tokens[0], text: tokens.slice(1).join(" ") };
  return { repo: tokens[i].replace(/[),.;]+$/, ""), text: [...tokens.slice(0, i), ...tokens.slice(i + 1)].join(" ").trim() };
}

async function onStart(ev) {
  ev.preventDefault();
  const raw = $("start-input").value.trim();
  if (!raw) { $("start-error").textContent = "Paste a git URL first, e.g. https://github.com/org/repo"; $("start-error").hidden = false; return; }
  await startChat(splitStart(raw));
}

// the top bar's orchestrator switch. On the welcome page it picks the mode of the next
// conversation (the server's default, or this browser's last choice); a conversation keeps the
// mode it started with, so while one is open the switch shows that mode and is locked.
const ORCH_KEY = "scr-agents.orchestrator";
const ORCH_NAME = { deterministic: "Deterministic", openflow: "OpenFlow" };
function openChatOrchestrator() {
  if (state.view?.type !== "chat") return null;
  return state.chats.find((c) => c.chat_id === state.view.id)?.orchestrator || null;
}
function renderOrchSwitch() {
  const locked = openChatOrchestrator(), value = locked || state.orchestrator, sw = $("orch-switch");
  for (const b of sw.querySelectorAll("[data-orch]")) {
    b.setAttribute("aria-checked", String(b.dataset.orch === value));
    b.disabled = Boolean(locked);
  }
  sw.classList.toggle("locked", Boolean(locked));
  sw.title = locked
    ? `This conversation runs its reviews with the ${ORCH_NAME[locked] || locked} orchestrator; start a new chat to change it`
    : "Orchestrator of the next conversation you start";
}
function setOrchestrator(value) {
  state.orchestrator = value;
  try { localStorage.setItem(ORCH_KEY, value); } catch { /* storage blocked */ }
  renderOrchSwitch();
}
function initOrchestratorSwitch() {
  const allowed = state.config.orchestrators || ["deterministic", "openflow"];
  let saved = null;
  try { saved = localStorage.getItem(ORCH_KEY); } catch { /* storage blocked */ }
  state.orchestrator = allowed.includes(saved) ? saved : state.config.orchestrator || "deterministic";
  renderOrchSwitch();
  const sw = $("orch-switch");
  sw.addEventListener("click", (e) => {
    const b = e.target.closest("[data-orch]");
    if (b && !openChatOrchestrator()) setOrchestrator(b.dataset.orch);
  });
  sw.addEventListener("keydown", (e) => { // arrow keys move between the two options
    if (!["ArrowLeft", "ArrowRight"].includes(e.key) || openChatOrchestrator()) return;
    const next = state.orchestrator === "openflow" ? "deterministic" : "openflow";
    setOrchestrator(next);
    sw.querySelector(`[data-orch="${next}"]`).focus();
  });
}

let starting = false;
async function startChat(body) {
  body = { ...body, orchestrator: state.orchestrator };
  if (starting) return; // one request at a time: repeated presses used to create duplicate conversations
  starting = true;
  const btn = $("start-btn"), demo = $("demo-btn"), label = btn.textContent;
  btn.disabled = demo.disabled = true;
  btn.textContent = body.demo ? "Preparing…" : "Starting…";
  $("start-error").hidden = true;
  try {
    const res = await api("/api/chats/new", body);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { $("start-error").textContent = data.error || `could not start (${res.status})`; $("start-error").hidden = false; return; }
    state.chats = [data, ...state.chats.filter((c) => c.chat_id !== data.chat_id)]; // show it in the sidebar now
    $("start-input").value = "";
    openChat(data.chat_id);
  } catch (err) {
    $("start-error").textContent = `could not reach the server: ${err.message || err}`; $("start-error").hidden = false;
  } finally {
    starting = false; btn.disabled = demo.disabled = false; btn.textContent = label;
  }
}

function openChat(chatId) {
  resetView();
  state.view = { type: "chat", id: chatId };
  $("welcome").hidden = true; $("thread").hidden = false; $("composer").hidden = false;
  renderOrchSwitch();
  state.chatES = new EventSource(`/api/chats/${chatId}/events`);
  state.chatES.onmessage = (m) => onChatEvent(JSON.parse(m.data));
  refreshLists(); renderActivity();
  $("composer-input").focus();
}

async function openRunView(runId) {
  resetView();
  state.view = { type: "run", id: runId };
  $("welcome").hidden = true; $("thread").hidden = false; $("composer").hidden = true;
  const s = await api(`/api/runs/${runId}`).then((r) => r.json());
  appendMessage({ role: "assistant", text: `Review **${s.run_id}** of \`${s.repo.split("/").slice(-1)[0]}\` (${s.base.slice(0, 10)}..${s.head.slice(0, 10)}, task ${s.task}). It was started outside a conversation; its result is below.` });
  attachRun(runId, { files: s.files, scope: s.base === s.head ? "all" : "range" });
  refreshLists(); renderActivity();
}

async function onSend(ev) {
  ev.preventDefault();
  const ci = $("composer-input");
  const text = ci.value.trim();
  if (!text || !state.view || state.view.type !== "chat") return;
  ci.value = ""; ci.style.height = "auto";
  const res = await api(`/api/chats/${state.view.id}/messages`, { text });
  if (!res.ok) { appendMessage({ role: "assistant", text: (await res.json()).error || "could not send", error: true }); return; }
  if (!state.chatES) openChat(state.view.id); // the server resumed this chat: follow its live feed again
}

// ---------------------------------------------------------------- chat feed
function onChatEvent(e) {
  if (e.event === "replay_done") {
    // a chat that is not loaded on the server ends its stream after the replay; do not let EventSource retry
    if (!e.live && state.chatES) { state.chatES.close(); state.chatES = null; }
    scrollDown(true);
    return;
  }
  if (e.seq != null) { if (e.seq <= state.chatSeq) return; state.chatSeq = e.seq; }
  switch (e.event) {
    case "message": appendMessage(e); if (e.role === "user") refreshLists(); break;
    case "repo_preparing": case "repo_ready": case "repo_failed": state.preparing = e.event === "repo_preparing"; renderPrep(e); refreshLists(); break;
    case "chat_status":
      state.busy = !!e.busy;
      if (e.phase === "preparing") state.lastLead = "Getting the repository";
      if (!state.busy) state.lastLead = "";
      renderTyping(); refreshLists();
      break;
    case "llm_start":
      if (e.agent === "lead") {
        state.lastLead = "Thinking"; state.leadLlmKey = `llm:${e.seq}`;
        task("lead", state.leadLlmKey, { kind: "llm", title: `lead · turn ${e.turn}`, subtitle: e.model || "lead agent", agent: "lead", ws: "ws1", status: "running", started: e.ts, seq: e.seq, result: { model: e.model } });
        renderTyping();
      }
      break;
    case "llm_call": case "llm_error":
      if (e.agent === "lead" && state.leadLlmKey) {
        const ok = e.event === "llm_call";
        task("lead", state.leadLlmKey, { subtitle: ok ? `${e.model || ""} · ${e.stop_reason}${e.tool_uses?.length ? " → " + e.tool_uses.join(", ") : ""}` : "failed", status: ok ? "done" : "failed", ended: e.ts, duration: e.duration_s, result: strip(e) });
      }
      break;
    case "tool_start":
      if (e.agent === "lead") {
        state.lastLead = cap(phrase(e.tool, e.input, e));
        const id = `tool:${e.call_id || e.seq}`;
        task("lead", id, { kind: "lead", title: toolTitle(e), subtitle: toolSub(e, "lead agent"), ws: toolWs(e.tool), status: "running", started: e.ts, seq: e.seq, hidden: e.tool === "start_review", result: { tool: e.tool, input: e.input, origin: e.origin, remote_name: e.remote_name } });
        renderLeadTool(`lead|${id}`);
        renderTyping();
      }
      break;
    case "tool_call":
      if (e.agent === "lead") {
        const id = `tool:${e.call_id || e.seq}`;
        task("lead", id, { kind: "lead", title: toolTitle(e), subtitle: toolSub(e, "lead agent"), ws: toolWs(e.tool), status: e.is_error ? "failed" : "done", ended: e.ts, started: state.tasks.get(`lead|${id}`)?.started || e.ts, duration: e.duration_s, seq: state.tasks.get(`lead|${id}`)?.seq || e.seq, hidden: e.tool === "start_review", result: strip(e) });
        renderLeadTool(`lead|${id}`);
      }
      break;
    case "memory_used": if (e.agent === "lead") state.convMemory = e; break;
    case "run_started": state.orchRun = null; attachRun(e.run, e); break; // a new review: the panel follows it again
    case "error": appendMessage({ role: "assistant", text: e.error || "error", error: true }); break;
    default: break;
  }
  scheduleRender();
}

function appendMessage(e) {
  const div = document.createElement("div");
  div.className = `msg ${e.role === "user" ? "user" : "assistant"}${e.error && e.role !== "user" ? " error" : ""}`;
  div.innerHTML = e.role === "user" ? `<div class="bubble">${esc(e.text)}</div>`
    : `<div class="avatar">◎</div><div class="bubble md">${md(e.text)}</div>`;
  $("thread").insertBefore(div, $("typing") || null);
  scrollDown();
}

// "Cloning https://… (1m12s)" while it runs, then "✓ Cloned … · 2,537 files · 3m12s" or the error
function renderPrep(e) {
  let el = state.prepEl && state.prepEl.isConnected ? state.prepEl : null;
  if (e.event === "repo_preparing" || !el) {
    el = document.createElement("div");
    el.className = "prep";
    $("thread").insertBefore(el, $("typing") || null);
    state.prepEl = el;
  }
  el.dataset.started = el.dataset.started || e.ts || new Date().toISOString();
  const target = esc(String(e.target || el.dataset.target || "").replace(/^https?:\/\//, ""));
  if (e.target) el.dataset.target = e.target;
  if (e.event === "repo_preparing") {
    el.className = "prep running"; el.dataset.action = e.action;
    const verb = { clone: "Cloning", update: "Updating", open: "Opening" }[e.action] || "Getting";
    el.innerHTML = `<span class="pg-ic">◌</span><span>${verb} <b>${target}</b>…</span><span class="pg-t" data-since="${esc(el.dataset.started)}"></span>`;
  } else if (e.event === "repo_ready") {
    el.className = "prep done";
    const verb = { clone: "Cloned", update: "Updated", open: "Opened" }[el.dataset.action] || "Got";
    el.innerHTML = `<span class="pg-ic">✓</span><span>${verb} <b>${target || esc(String(e.repo || "").split("/").pop())}</b> · ${esc(e.branch || "")} @ <code>${esc(String(e.head || "").slice(0, 10))}</code> · ${plural(e.files ?? 0, "file")}</span><span class="pg-t">${e.duration_s >= 0.5 ? fmtDur(e.duration_s) : ""}</span>`;
  } else {
    el.className = "prep failed";
    el.innerHTML = `<span class="pg-ic">✕</span><span>Could not get <b>${target}</b></span><span class="pg-t">${e.duration_s >= 0.5 ? fmtDur(e.duration_s) : ""}</span>`;
  }
  scrollDown();
}

function tickPrep() {
  document.querySelectorAll(".prep.running .pg-t[data-since]").forEach((t) => { t.textContent = fmtDur(secs(t.dataset.since, new Date().toISOString())); });
}

function renderTyping() {
  let el = $("typing");
  if (!state.busy) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("div"); el.id = "typing"; el.className = "typing"; $("thread").appendChild(el); }
  el.innerHTML = `<span class="dots"><span></span><span></span><span></span></span><span>${esc(state.lastLead || "Working")}…</span>`;
  scrollDown();
}

function scrollDown(force) {
  const t = $("thread");
  if (force || t.scrollHeight - t.scrollTop - t.clientHeight < 160) t.scrollTop = t.scrollHeight;
}

// ---------------------------------------------------------------- runs: inline card + tasks
function attachRun(runId, info) {
  if (state.runs.has(runId)) return;
  const run = { id: runId, seq: 0, stages: {}, stageTimes: {}, agentNow: {}, status: "running", counts: null, now: "", started: null, ended: null, report: null, info: info || {}, errors: [] };
  state.runs.set(runId, run);
  const card = document.createElement("div");
  card.className = "run-card"; card.dataset.run = runId;
  $("thread").insertBefore(card, $("typing") || null);
  run.card = card;
  renderRunCard(run);
  run.es = new EventSource(`/api/runs/${runId}/events`);
  run.es.onmessage = (m) => onRunEvent(run, JSON.parse(m.data));
  scrollDown();
}

function onRunEvent(run, e) {
  if (e.seq != null) { if (e.seq <= run.seq) return; run.seq = e.seq; }
  const agent = e.agent || "pipeline", R = run.id;
  switch (e.event) {
    case "request": run.started = e.ts; run.info.files = e.files; break;
    case "stage": {
      run.stages[e.stage] = e;
      const t = run.stageTimes[e.stage] || (run.stageTimes[e.stage] = {});
      if (e.status === "running") { t.start = e.ts; run.now = `${esc(STAGE_LABEL[e.stage] || e.stage)}…`; } else { t.start = t.start || e.ts; t.end = e.ts; }
      break;
    }
    case "code_graph": run.graphSummary = e.summary; break;
    case "scanners": run.scan = { findings: e.findings, run: e.run || [], missing: e.missing || [] }; break;
    case "plan": task(R, "plan", { kind: "plan", title: "Review plan", subtitle: `${(e.plan.hypotheses || []).length} hypotheses`, ws: "ws1", status: "done", started: e.ts, ended: e.ts, seq: e.seq, result: e.plan }); break;
    case "subagent_start": task(R, `agent:${agent}`, { kind: "agent", title: agent, subtitle: e.model || "", agent, ws: "ws1", status: "running", started: e.ts, seq: e.seq, result: { model: e.model } }); break;
    case "validate_start": { const n = `validator:${e.finding}`; task(R, `agent:${n}`, { kind: "validator", title: n, subtitle: e.model || "", agent: n, ws: "ws1", status: "running", started: e.ts, seq: e.seq, result: { model: e.model, finding: e.finding } }); break; }
    case "llm_start": run.agentNow[agent] = "thinking"; run.now = `<span class="who">${esc(agent)}</span> thinking`; task(R, `llm:${agent}:${e.turn}`, { kind: "llm", title: `${agent} · turn ${e.turn}`, subtitle: e.model || "", agent, ws: "ws1", status: "running", started: e.ts, seq: e.seq, hidden: true, result: { model: e.model } }); break;
    case "llm_call": { const id = `llm:${agent}:${e.turn}`; task(R, id, { kind: "llm", title: `${agent} · turn ${e.turn}`, subtitle: `${e.stop_reason}${e.tool_uses?.length ? " → " + e.tool_uses.join(", ") : ""}`, agent, ws: "ws1", status: "done", ended: e.ts, started: state.tasks.get(`${R}|${id}`)?.started || e.ts, seq: e.seq, hidden: true, result: strip(e) }); break; }
    case "tool_start": run.agentNow[agent] = phrase(e.tool, e.input, e); run.now = `<span class="who">${esc(agent)}</span> → ${e.origin ? '<span class="mcp-badge">MCP</span>' : ""}${esc(toolTitle(e, 50))}`; task(R, `tool:${agent}:${e.call_id || e.seq}`, { kind: "tool", title: toolTitle(e), subtitle: toolSub(e, agent), agent, ws: toolWs(e.tool), status: "running", started: e.ts, seq: e.seq, result: { tool: e.tool, input: e.input, origin: e.origin, remote_name: e.remote_name } }); break;
    case "tool_call": { const id = `tool:${agent}:${e.call_id || e.seq}`; task(R, id, { kind: "tool", title: toolTitle(e), subtitle: `${toolSub(e, agent)} · ${e.output_chars} chars`, agent, ws: toolWs(e.tool), status: e.is_error ? "failed" : "done", ended: e.ts, started: state.tasks.get(`${R}|${id}`)?.started || e.ts, duration: e.duration_s, seq: state.tasks.get(`${R}|${id}`)?.seq || e.seq, result: strip(e) }); break; }
    case "candidates": run.candidates = e.count; run.cand = { ...e }; break;
    case "round": run.info.round = e.round; break;
    case "round_status": run.round = strip(e); break;
    case "mcp_servers": run.mcp = { servers: e.servers || [], webSearch: e.web_search || null }; break;
    case "mode": run.mode = e.mode; break;
    case "memory": runMem(run).info = strip(e); break;
    case "memory_used": runMem(run).used.push(e); break;
    case "memory_ignored": runMem(run).ignored.push(e); break;
    case "memory_recorded": runMem(run).recorded = e.findings; break;
    case "memory_error": runMem(run).errors.push(e.error); break;
    case "kv_cache": run.kv = e; break;
    case "mcp_start": case "mcp_call": mcpLeg(run, e); break;
    case "subagent_done": delete run.agentNow[agent]; task(R, `agent:${agent}`, { subtitle: `${e.findings} findings · ${e.turns} turns`, status: e.error ? "failed" : "done", ended: e.ts, result: strip(e) }); break;
    case "verdict": delete run.agentNow[`validator:${e.finding}`]; task(R, `agent:validator:${e.finding}`, { subtitle: `verdict ${e.verdict}`, status: e.error ? "failed" : "done", ended: e.ts, result: strip(e) }); break;
    case "llm_error": task(R, `llm:${agent}:${e.turn}`, { kind: "llm", title: `${agent} · turn ${e.turn}`, agent, status: "failed", ended: e.ts, hidden: true, result: { error: e.error } }); break;
    case "run_finished": run.status = "finished"; run.ended = e.ts; run.counts = e.counts; run.errors = e.errors || []; finishRun(run); loadReport(run); break;
    case "run_failed": case "run_error": run.status = "failed"; run.ended = e.ts; run.error = e.error; finishRun(run); break;
    default: break;
  }
  scheduleRender();
}

function finishRun(run) {
  if (run.es) { run.es.close(); run.es = null; }
  for (const t of state.tasks.values()) if (t.run === run.id && t.status === "running") Object.assign(t, { status: run.status === "failed" ? "failed" : "done", ended: t.ended || run.ended });
  run.now = "";
}

async function loadReport(run) {
  const res = await api(`/api/runs/${run.id}/report`);
  if (!res.ok) return;
  run.report = (await res.json()).report;
  if (!run.counts) run.counts = countVerdicts(run.report);
  await loadRound(run);
  renderRunCard(run);
  if (state.selected) renderDrawer();
}

// where the review round stands now: the human verdicts recorded since the run change it
async function loadRound(run) {
  const res = await api(`/api/runs/${run.id}/round`);
  if (res.ok) run.round = await res.json();
}

function roundHtml(run) {
  const r = run.round;
  if (!r) return "";
  const pending = (r.pending || []).length;
  const where = r.passed ? "passed: every finding is settled"
    : !r.can_continue ? `${pending} pending · round limit reached`
    : r.auto ? `${pending} not verified`
    : `${pending} pending (${(r.disputed || []).length} disputed, ${(r.awaiting_verdict || []).length} without your verdict)`;
  const next = r.can_continue && !r.auto && state.view?.type === "chat"
    ? `<button class="link strong" data-next-round="${esc(run.id)}" title="Reviews the pending findings again from the planner, with the ${esc(r.label_a)} and the ${esc(r.label_b)} as context">Review again (round ${r.round + 1}) ›</button>` : "";
  return `<div class="rc-round"><span class="chip${r.passed ? "" : " warn"}">round ${r.round} of ${r.max_rounds}</span><span class="muted">${esc(where)}</span>${next}</div>`;
}

function countVerdicts(report) {
  const c = { confirmed: 0, unverified: 0, rejected: 0 };
  for (const vf of report.findings || []) c[vf.verdict] = (c[vf.verdict] || 0) + 1;
  return c;
}

function renderRunCard(run) {
  if (!run.card) return;
  const dur = fmtDur(secs(run.started, run.status === "running" ? new Date().toISOString() : run.ended));
  const scope = run.info.scope === "all" ? "whole repository" : run.info.base ? `${run.info.base}..${run.info.head}` : "change";
  const title = run.status === "running" ? "Reviewing" : run.status === "failed" ? "Review failed" : "Review finished";
  const stages = STAGES.map(([k, l, ws]) => `<span class="rc-stage ${run.stages[k]?.status || ""}" style="--s-color:${cssVar(ws)}" title="${esc(l)}${run.stages[k] ? " · " + run.stages[k].status : ""}"></span>`).join("");
  let body = progressHtml(run);
  if (run.status === "failed") body += `<div class="rc-now" style="color:var(--bad)">${esc(run.error || "failed")}</div>`;
  if (run.report) {
    const c = run.counts || {};
    const bench = run.report.bench?.delta;
    body += roundHtml(run);
    body += `<div class="rc-counts"><span class="chip confirmed">${c.confirmed ?? 0} confirmed</span><span class="chip unverified">${c.unverified ?? 0} unverified</span><span class="chip">${c.rejected ?? 0} rejected</span>${bench ? `<span class="chip">bench: ${esc(bench)}</span>` : ""}${run.errors.length ? `<span class="chip warn" title="${esc(run.errors.join("\n"))}">incomplete: ${run.errors.length} agent${run.errors.length > 1 ? "s" : ""} failed</span>` : ""}</div>`;
    const fs = [...run.report.findings].sort((a, b) => ORDER[a.verdict] - ORDER[b.verdict]);
    body += `<div class="rc-findings">${fs.map((vf) => `<div class="rc-finding" data-run="${esc(run.id)}" data-finding="${esc(vf.finding.id)}">
        <span class="verdict ${esc(vf.verdict)}">${esc(vf.verdict)}</span><span class="sev">${esc(vf.finding.severity)}</span>
        <span>${esc(vf.finding.title)}<span class="loc">${esc(vf.finding.location.path)}:${vf.finding.location.line ?? "?"}</span></span></div>`).join("") || '<div class="muted">No findings.</div>'}</div>`;
  }
  const n = [...state.tasks.values()].filter((t) => t.run === run.id && !t.hidden).length;
  body += agentStepsHtml(run);
  run.card.innerHTML = `<div class="rc-head"><span class="dot ${run.status}"></span><span class="rc-title">${title}</span><span class="muted">· ${esc(scope)}${run.info.files ? ` · ${run.info.files} files` : ""}</span><span class="rc-meta">${esc(run.id)} · ${dur}</span></div>
    <div class="rc-stages">${stages}</div>${body}
    <div class="rc-foot">${run.report ? reportLinks(run.id) : ""}<button class="link" data-run-detail="${esc(run.id)}">Run details</button><button class="link" data-run-activity="${esc(run.id)}">${n} steps</button></div>`;
}

// the detailed HTML report: open it here (behind the console token), or download a copy to send to others
function reportLinks(runId) {
  const base = `/api/runs/${encodeURIComponent(runId)}`, u = `${base}/report.html`;
  const r = state.runs.get(runId)?.round, a = r?.label_a || "Review Report", b = r?.label_b || "Validator Report";
  return `<a class="link strong" href="${u}" target="_blank" rel="noopener">Open report ↗</a><a class="link" href="${base}/report_a.html" target="_blank" rel="noopener" title="This round's review report, with the validator's comment on each finding">${esc(a)} ↗</a><a class="link" href="${base}/report_b.html" target="_blank" rel="noopener" title="The validator's comment report on the ${esc(a)}: verdicts, settled and pending findings">${esc(b)} ↗</a><a class="link" href="${u}?download=1" download title="A self-contained page anyone can open; it leaves out paths on this machine">Download HTML</a>`;
}

// the lead agent starts the round, so the conversation keeps the new report as its last review
async function askNextRound(run) {
  if (!run?.round || state.view?.type !== "chat") return;
  const text = `Start review round ${run.round.round + 1}: review the pending findings of ${run.id} again.`;
  const res = await api(`/api/chats/${state.view.id}/messages`, { text });
  if (!res.ok) appendMessage({ role: "assistant", text: (await res.json()).error || "could not send", error: true });
  else if (!state.chatES) openChat(state.view.id);
}

function onThreadClick(ev) {
  const f = ev.target.closest("[data-finding]");
  if (f) return openFinding(f.dataset.run, f.dataset.finding);
  const fid = ev.target.closest(".fid");
  if (fid) { for (const r of state.runs.values()) if (r.report?.findings.some((v) => v.finding.id === fid.dataset.fid)) return openFinding(r.id, fid.dataset.fid); return; }
  const ot = ev.target.closest("[data-open-task]");
  if (ot) { ev.preventDefault(); return openTask(ot.dataset.openTask); }
  const d = ev.target.closest("[data-run-detail]");
  if (d) { state.orchRun = d.dataset.runDetail; return openRun(d.dataset.runDetail); }
  if (ev.target.closest("[data-run-activity]")) { state.activityCollapsed = false; renderActivity(); }
  const nr = ev.target.closest("[data-next-round]");
  if (nr) return askNextRound(state.runs.get(nr.dataset.nextRound));
}

// ---------------------------------------------------------------- tasks + activity corner
function task(runId, key, patch) {
  const id = `${runId}|${key}`;
  let t = state.tasks.get(id);
  if (!t) { t = { id, run: runId, kind: "tool", title: key, subtitle: "", status: "running", started: null, ended: null, result: {}, ws: "ws1", seq: 0 }; state.tasks.set(id, t); }
  const result = patch.result ? { ...t.result, ...patch.result } : t.result;
  Object.assign(t, patch, { result });
  return t;
}
// One MCP tools/call. An agent's call is already a tool step (tool_start / tool_call, matched by call_id):
// the server's own reply is attached to it as `result.mcp`. A call outside any agent becomes an "mcp" step.
function mcpLeg(run, e) {
  const agent = e.agent || `mcp:${e.server}`, live = e.event === "mcp_start";
  const leg = { ...strip(e), status: live ? "running" : e.is_error ? "failed" : "done", [live ? "started" : "ended"]: e.ts };
  const toolId = e.call_id ? `${run.id}|tool:${agent}:${e.call_id}` : null;
  const t = toolId && state.tasks.get(toolId);
  if (t) {
    t.result = { ...t.result, origin: t.result.origin || `mcp:${e.server}`, remote_name: t.result.remote_name || e.tool, mcp: { ...(t.result.mcp || {}), ...leg } };
    return;
  }
  run.mcpOpen = run.mcpOpen || {};
  const slot = `${agent}|${e.server}|${e.tool}|${e.call_id || ""}`;
  const key = live ? (run.mcpOpen[slot] = `mcp:${e.call_id || e.seq}`) : run.mcpOpen[slot] || `mcp:${e.call_id || e.seq}`;
  if (!live) delete run.mcpOpen[slot];
  const prev = state.tasks.get(`${run.id}|${key}`);
  task(run.id, key, { kind: "mcp", title: `${e.tool}(${fmtArgs(e.input, 60)})`, subtitle: `MCP ${e.server} · ${agent}`, agent, ws: "ws4",
    status: leg.status, started: prev?.started || e.ts, ended: live ? null : e.ts, duration: live ? null : e.duration_s, seq: prev?.seq || e.seq,
    result: { tool: e.tool, input: e.input, origin: `mcp:${e.server}`, remote_name: e.tool, mcp: { ...(prev?.result?.mcp || {}), ...leg } } });
}
const strip = (e, keys = []) => Object.fromEntries(Object.entries(e).filter(([k]) => !["seq", "ts", "run_id", "commit_ids", "event", "agent", ...keys].includes(k)));
const taskDur = (t) => (t.duration != null ? t.duration : secs(t.started, t.status === "running" ? new Date().toISOString() : t.ended));

function renderActivity() {
  const all = [...state.tasks.values()].filter((t) => !t.hidden);
  const running = all.filter((t) => t.status === "running").sort((a, b) => a.seq - b.seq);
  const done = all.filter((t) => t.status !== "running").sort((a, b) => (b.ended || "").localeCompare(a.ended || "") || b.seq - a.seq);
  const box = $("activity");
  $("side-stack").hidden = !state.view;
  box.classList.toggle("collapsed", state.activityCollapsed);
  box.classList.toggle("has-running", running.length > 0);
  document.querySelector(".shell").classList.toggle("side-open", !!state.view && (!state.activityCollapsed || !state.orchCollapsed || !state.mcpCollapsed || !state.memCollapsed));
  $("activity-toggle").setAttribute("aria-expanded", String(!state.activityCollapsed));
  $("activity-dot").className = "activity-dot" + (running.length ? " running" : "");
  $("activity-title").textContent = running.length ? `Activity · ${running.length} running` : all.length ? `Activity · ${all.length} steps` : "Activity";
  if (state.activityCollapsed) return;
  const row = (t) => `<div class="act ${t.status}" data-task="${esc(t.id)}" style="--k-color:${cssVar(isMcpTask(t) ? "ws4" : t.ws)}" title="${esc(t.title)}">
      <span class="a-ic">${t.status === "running" ? "◌" : isMcpTask(t) ? KIND_ICON.mcp : KIND_ICON[t.kind] || "•"}</span>
      <span style="min-width:0"><div class="a-t">${esc(t.title)}</div><div class="a-s">${esc(t.subtitle || KIND_LABEL[t.kind] || "")}</div></span>
      <span class="a-d">${t.status === "failed" ? "failed" : fmtDur(taskDur(t))}</span></div>`;
  const list = $("activity-list");
  // a step that just started must be visible: jump to the top when the set of running steps grows
  const runningKey = running.map((t) => t.id).join(",");
  const grew = running.some((t) => !(state.seenRunning || new Set()).has(t.id));
  state.seenRunning = new Set(running.map((t) => t.id));
  const keep = grew && runningKey ? 0 : list.scrollTop;
  list.innerHTML = ((running.length ? `<div class="act-sep running">running · ${running.length}</div>${running.map(row).join("")}` : "")
    + (done.length ? `<div class="act-sep">completed · ${done.length}</div>${done.map(row).join("")}` : ""))
    || '<div class="act-empty">The agents\' steps appear here while they work.</div>';
  list.scrollTop = keep;
  list.querySelectorAll("[data-task]").forEach((el) => el.addEventListener("click", () => openTask(el.dataset.task)));
}

function scheduleRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  setTimeout(() => {
    state.renderQueued = false;
    for (const r of state.runs.values()) renderRunCard(r);
    renderOrchestrator();
    renderMcp();
    renderMem();
    renderActivity();
    if (state.selected) renderDrawer();
  }, 60);
}

// ---------------------------------------------------------------- progress narrative: what the agents are doing, in words
const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const REVIEWER = { bugs: "Bug reviewer", vulnerabilities: "Security reviewer", style: "Style reviewer" };
function agentName(a) {
  if (a.startsWith("subagent:")) return REVIEWER[a.slice(9)] || `${cap(a.slice(9))} reviewer`;
  if (a.startsWith("validator:")) return `Validator (${a.slice(10)})`;
  return a === "review_agent" ? "Planner" : a === "orchestrator" ? "Orchestrator (OpenFlow)" : a === "lead" ? "Lead agent" : a;
}

function phrase(tool, inp = {}, extra = {}) {
  inp = inp || {};
  const q = (v) => `"${short(v, 40)}"`;
  if (extra?.origin) return `calling ${extra.remote_name || tool} on the ${String(extra.origin).slice(4)} MCP server`;
  switch (tool) {
    case "read_file": return `reading ${inp.path}${inp.start_line ? ` (lines ${inp.start_line}-${inp.end_line ?? "end"})` : ""}`;
    case "get_diff": return `reading the diff of ${inp.path}`;
    case "list_changed_files": return "listing the changed files";
    case "search_code": return `searching the code for ${q(inp.pattern)}`;
    case "code_graph_query": return `checking where ${inp.symbol} is used`;
    case "scanner_findings": return inp.path ? `reviewing the scanner hits in ${inp.path}` : "reviewing the scanner hits";
    case "kb_lookup": return `looking up ${q(inp.query)} in the CWE / CERT knowledge base`;
    case "git_log": return inp.path ? `reading the history of ${inp.path}` : "reading the commit history";
    case "memory_read": return "reading the project memory";
    case "memory_note": return "saving a note to the project memory";
    case "run_tests": return inp.what === "build" ? `building the ${inp.ref || "head"} commit` : `running the tests on the ${inp.ref || "head"} commit`;
    case "run_command": return `running ${short((inp.argv || []).join(" "), 48)} on the ${inp.ref || "head"} commit`;
    case "write_file": return `writing a proof of concept (${inp.path})`;
    case "web_search": return `searching the web for ${q(inp.query)}`;
    case "start_review": return "starting the review";
    case "list_findings": return "looking up the findings";
    case "finding_detail": return `opening finding ${inp.id}`;
    case "project_info": return "looking at the project";
    default: return `using ${tool}`;
  }
}

function benchWords(delta) {
  const d = String(delta || "");
  if (d.startsWith("regression")) return "tests pass on the base commit but fail on the new one";
  if (d.startsWith("fixed")) return "tests fail on the base commit and pass on the new one";
  if (d === "both pass") return "tests pass on both commits";
  if (d === "both fail") return "tests fail on both commits";
  return d.replace(/^skipped: /, "");
}

function progressSteps(run) {
  const st = (k) => run.stages[k]?.status, T = run.stageTimes || {}, now = new Date().toISOString();
  const steps = [];
  const add = (key, status, label, detail, start, end, lines) =>
    steps.push({ key, status, label, detail, lines, dur: secs(start, end || (status === "running" ? now : start)) });
  const subs = tasksOf(run, "agent"), vals = tasksOf(run, "validator");

  if (st("inputs")) {
    const e = run.stages.inputs;
    add("inputs", "done", "Read the change", `${plural(e.files ?? run.info.files ?? 0, "file")} · ${plural(e.commits ?? 0, "commit")}${run.info.scope === "all" ? " · whole repository" : ""}`, T.inputs?.start, T.inputs?.end);
  }
  if (st("graph")) {
    const e = run.stages.graph, callers = /(\d+) caller site/.exec(run.graphSummary || "")?.[1];
    add("graph", st("graph"), st("graph") === "done" ? "Built the code graph" : "Building the code graph",
      st("graph") === "done" ? `${plural(e.symbols ?? 0, "changed function")}${callers ? ` · ${plural(+callers, "call site")}` : ""} · ${plural(e.impacted_files ?? 0, "file")} affected` : "mapping the changed functions and their callers",
      T.graph?.start, T.graph?.end);
  }
  if (st("scanners")) {
    const sc = run.scan || { findings: run.stages.scanners.findings, run: run.stages.scanners.run || [], missing: [] };
    const n = sc.findings ?? 0;
    add("scanners", st("scanners"), st("scanners") === "done" ? "Scanned for suspicious code" : "Scanning for suspicious code",
      st("scanners") === "done" ? `${n ? `${plural(n, "suspicious line")} flagged` : "nothing flagged"}${sc.run?.length ? ` by ${sc.run.join(", ")}` : ""}${sc.missing?.length ? ` · ${sc.missing.join(", ")} not installed` : ""}` : "running the static analyzers on the changed lines",
      T.scanners?.start, T.scanners?.end);
  }
  if (st("bench")) {
    const delta = run.stages.bench.delta || "";
    const skipped = delta.startsWith("skipped");
    add("bench", st("bench"), st("bench") !== "done" ? "Building and testing both commits" : skipped ? "Skipped the test bench" : "Built and tested both commits",
      st("bench") === "done" ? benchWords(delta) : "running the project's build and tests on the base and the new commit", T.bench?.start, T.bench?.end);
  }
  if (st("review")) {
    const plan = state.tasks.get(`${run.id}|plan`);
    if (plan) {
      const p = plan.result || {}, h = (p.hypotheses || []).length, focus = (p.focus_files || []).slice(0, 2);
      add("plan", "done", "Planned the review", `${plural(h, "hypothesis", "hypotheses")}${focus.length ? ` · focus on ${focus.join(", ")}` : ""}`, T.review?.start, plan.started);
    } else {
      add("plan", "running", "Planning the review", "deciding where to look first", T.review?.start);
    }
    if (plan || subs.length) {
      const reviewing = !run.cand && st("review") === "running";
      if (reviewing) {
        const lines = subs.map((a) => `${agentName(a.agent)}: ${isDone(a) ? `done, ${plural(a.result?.findings ?? 0, "finding")}` : run.agentNow[a.agent] || "starting"}`);
        add("analyze", "running", "Analyzing the code", `${plural(Math.max(3, subs.length), "reviewer")} working in parallel`, plan?.started || T.review?.start, null, lines);
      } else {
        const SHORT = { bugs: "bugs", vulnerabilities: "security", style: "style" };
        const counts = subs.map((a) => `${SHORT[a.agent.slice(9)] || a.agent.slice(9)} ${a.result?.findings ?? 0}`).join(" · ");
        add("analyze", "done", "Analyzed the code", counts ? `findings: ${counts}` : "", plan?.started || T.review?.start, run.cand?.ts || T.review?.end);
      }
    }
  }
  if (run.cand) {
    const c = run.cand, parts = [];
    if (c.duplicates) parts.push(`${plural(c.duplicates, "duplicate")} merged`);
    if (c.promoted) parts.push(`${c.promoted} added from scanner hits`);
    if (c.ignored) parts.push(`${c.ignored} ignored by the project memory`);
    add("filter", "done", "Filtered the findings", `${plural(c.raw ?? c.count, "raw finding")} → ${plural(c.count, "candidate")}${parts.length ? ` (${parts.join(", ")})` : ""}`, c.ts, c.ts);
  }
  if (st("validation")) {
    const verdict = { confirmed: 0, unverified: 0, rejected: 0 };
    vals.forEach((v) => { if (v.result?.verdict) verdict[v.result.verdict] = (verdict[v.result.verdict] || 0) + 1; });
    const tally = `${verdict.confirmed} confirmed · ${verdict.unverified} unverified · ${verdict.rejected} rejected`;
    if (st("validation") === "running") {
      const n = run.candidates ?? vals.length, done = vals.filter(isDone).length;
      const lines = vals.filter((v) => !isDone(v)).map((v) => `${agentName(v.agent)}: ${run.agentNow[v.agent] || "starting"}`);
      add("verify", "running", "Verifying each finding with a second model", `${done} of ${n} checked${done ? ` · ${tally}` : ""}`, T.validation?.start, null, lines);
    } else {
      add("verify", "done", `Verified ${plural(vals.length, "finding")}`, vals.length ? tally : "nothing to verify", T.validation?.start, T.validation?.end);
    }
  }
  if (st("report")) {
    const c = run.stages.report.counts || run.counts;
    add("report", st("report"), st("report") === "done" ? "Wrote the report" : "Writing the report",
      c ? `${c.confirmed} confirmed · ${c.unverified} unverified · ${c.rejected} rejected` : "", T.report?.start, T.report?.end);
  }
  if (run.status === "failed") steps.push({ key: "failed", status: "failed", label: "Review failed", detail: short(run.error || "", 120), dur: null });
  return steps;
}

function progressHtml(run) {
  const steps = progressSteps(run);
  if (!steps.length) return '<div class="rc-progress"><div class="pg running"><span class="pg-ic">◌</span><div class="pg-main"><span class="pg-l">Checking out the commits…</span></div><span class="pg-t"></span></div></div>';
  const row = (x) => `<div class="pg ${x.status}"><span class="pg-ic">${x.status === "done" ? "✓" : x.status === "failed" ? "✕" : "◌"}</span>
      <div class="pg-main"><div><span class="pg-l">${esc(x.label)}${x.status === "running" ? "…" : ""}</span>${x.detail ? `<span class="pg-d"> · ${esc(x.detail)}</span>` : ""}</div>
      ${(x.lines || []).map((l) => `<div class="pg-sub">${esc(l)}</div>`).join("")}</div>
      <span class="pg-t">${x.dur != null && x.dur >= 0.1 ? fmtDur(x.dur) : ""}</span></div>`;
  if (run.status === "running") return `<div class="rc-progress">${steps.map(row).join("")}</div>`;
  const key = `prog:${run.id}`, open = state.openKeys.has(key);
  return `<details class="rc-progress-done" data-key="${esc(key)}"${open ? " open" : ""}><summary>Show the ${steps.length} steps · ${fmtDur(secs(run.started, run.ended))}</summary><div class="rc-progress">${open ? steps.map(row).join("") : ""}</div></details>`;
}

// ---------------------------------------------------------------- tool-use blocks (chat + run steps)
const isMcp = (r) => String(r?.origin || "").startsWith("mcp:");
const isMcpTask = (t) => t.kind === "mcp" || (t.kind === "tool" && isMcp(t.result));
function toolTitle(e, n = 60) { return `${e.origin ? e.remote_name || e.tool : e.tool}(${fmtArgs(e.input, n)})`; }
function toolSub(e, who) { return e.origin ? `MCP ${String(e.origin).slice(4)} · ${who}` : who; }

function toolBlockHtml(t, key) {
  const r = t.result || {}, open = state.openKeys.has(key), mcp = isMcp(r);
  const name = mcp ? `<span class="mcp-badge">MCP · ${esc(r.origin.slice(4))}</span>${esc(r.remote_name || r.tool)}` : esc(r.tool || t.title);
  const st = t.status === "running" ? '<span class="tb-st running">running…</span>'
    : t.status === "failed" ? `<span class="tb-st failed">error · ${fmtDur(taskDur(t))}</span>` : `<span class="tb-st">${fmtDur(taskDur(t))}</span>`;
  let body = "";
  if (open) {
    const chars = r.output_chars != null ? ` · ${r.output_chars} chars${r.output_chars > (r.output_preview || "").length ? ", preview" : ""}` : "";
    const via = mcp && r.mcp ? `<div class="chip-row"><span class="chip">MCP ${esc(r.mcp.server || r.origin.slice(4))} · tools/call ${esc(r.mcp.tool || r.remote_name)}</span>${r.mcp.duration_s != null ? `<span class="chip">server ${fmtDur(r.mcp.duration_s)}</span>` : ""}${r.mcp.output_chars != null ? `<span class="chip">reply ${r.mcp.output_chars} chars</span>` : ""}</div>` : "";
    body = `<div class="tb-body">${via}${section("input", pre(r.input || {}))}`
      + (t.status === "running" ? '<div class="muted">waiting for the result…</div>' : section((r.is_error ? "error" : "result") + chars, renderOutput(r.output_preview || "")))
      + `<div><button class="link" data-open-task="${esc(t.id)}">Full trace and details ›</button></div></div>`;
  }
  return `<details class="toolblk ${t.status}${mcp ? " mcp" : ""}" data-key="${esc(key)}"${open ? " open" : ""}><summary><span class="tb-ic">${t.status === "running" ? "◌" : mcp ? "⇄" : "⌘"}</span><span class="tb-name">${name}</span><span class="tb-args">${esc(fmtArgs(r.input, 80))}</span>${st}</summary>${body}</details>`;
}

function renderLeadTool(taskId) {
  const t = state.tasks.get(taskId);
  if (!t) return;
  let el = $("thread").querySelector(`[data-lead-tool="${CSS.escape(taskId)}"]`);
  if (!el) { el = document.createElement("div"); el.className = "lead-tool"; el.dataset.leadTool = taskId; $("thread").insertBefore(el, $("typing") || null); scrollDown(); }
  el.innerHTML = toolBlockHtml(t, `lead:${taskId}`);
}

function agentStepsHtml(run) {
  const mine = [...state.tasks.values()].filter((t) => t.run === run.id);
  const agents = mine.filter((t) => t.kind === "agent" || t.kind === "validator").sort((a, b) => a.seq - b.seq);
  const tools = mine.filter((t) => t.kind === "tool").sort((a, b) => a.seq - b.seq);
  if (!agents.length && !tools.length) return "";
  const key = `steps:${run.id}`, open = state.openKeys.has(key);
  const mcp = tools.filter((t) => isMcp(t.result)).length, live = tools.filter((t) => t.status === "running").length;
  let inner = "";
  if (open) {
    const groups = agents.map((a) => ({ a, items: tools.filter((t) => t.agent === a.agent) }));
    const known = new Set(agents.map((a) => a.agent));
    const loose = tools.filter((t) => !known.has(t.agent));
    inner = groups.map(({ a, items }) => `<div class="ag-group ${a.status}"><div class="ag-head"><span class="dot ${a.status === "done" ? "finished" : a.status}"></span><b>${esc(a.agent)}</b><span class="muted">${esc(a.subtitle || "")}</span><button class="link" data-open-task="${esc(a.id)}">details</button></div>
        ${items.map((t) => toolBlockHtml(t, `tool:${t.id}`)).join("") || '<div class="muted" style="font-size:12px">no tool calls</div>'}</div>`).join("")
      + (loose.length ? `<div class="ag-group">${loose.map((t) => toolBlockHtml(t, `tool:${t.id}`)).join("")}</div>` : "");
  }
  return `<details class="rc-steps" data-key="${esc(key)}"${open ? " open" : ""}><summary>Agent steps · ${agents.length} agents · ${tools.length} tool calls${mcp ? ` · ${mcp} MCP` : ""}${live ? ` · ${live} running` : ""}</summary><div class="rc-steps-body">${inner}</div></details>`;
}

// ---------------------------------------------------------------- orchestrator panel: overall progress
const STAGE_WEIGHT = { inputs: 3, graph: 5, scanners: 7, bench: 15, review: 40, validation: 25, report: 5 };
const tasksOf = (run, kind) => [...state.tasks.values()].filter((t) => t.run === run.id && t.kind === kind).sort((a, b) => a.seq - b.seq);
const isDone = (t) => t.status !== "running";

function runProgress(run) {
  if (run.status === "finished") return 100;
  let total = 0;
  for (const [k] of STAGES) {
    const s = run.stages[k]?.status;
    let f = 0;
    if (s === "done") f = 1;
    else if (s === "running") {
      if (k === "review") { const subs = tasksOf(run, "agent"); f = (state.tasks.has(`${run.id}|plan`) ? 0.15 : 0) + 0.85 * subs.filter(isDone).length / Math.max(3, subs.length); }
      else if (k === "validation") { const vals = tasksOf(run, "validator"); f = vals.filter(isDone).length / Math.max(run.candidates || 0, vals.length, 1); }
      else f = 0.4;
    }
    total += STAGE_WEIGHT[k] * f;
  }
  return Math.min(99, Math.round(total));
}

function stageDetail(k, e, run) {
  if (!e) return "";
  const x = strip(e, ["stage", "status"]);
  if (k === "inputs") return `${x.files ?? "?"} files · ${x.commits ?? "?"} commits`;
  if (k === "graph") return e.status === "done" ? `${x.symbols ?? "?"} symbols · ${x.impacted_files ?? "?"} files impacted` : "indexing the code";
  if (k === "scanners") return e.status === "done" ? `${x.findings ?? 0} hits · ${(x.run || []).join(", ")}` : "running the scanners";
  if (k === "bench") return e.status === "done" ? x.delta || "" : "building and testing both commits";
  if (k === "review") return e.status === "done" ? `${x.candidates ?? "?"} candidates` : `${tasksOf(run, "agent").filter(isDone).length}/${Math.max(3, tasksOf(run, "agent").length)} subagents done`;
  if (k === "validation") { const v = tasksOf(run, "validator"); return `${v.filter(isDone).length}/${run.candidates ?? v.length} re-checked`; }
  if (k === "report") return x.counts ? `${x.counts.confirmed} confirmed · ${x.counts.unverified} unverified` : "";
  return "";
}

function narrate(run) {
  if (run.status === "failed") return `The review failed: ${run.error || "see the run details"}.`;
  const subs = tasksOf(run, "agent"), vals = tasksOf(run, "validator");
  const verdicts = { confirmed: 0, unverified: 0, rejected: 0 };
  vals.forEach((v) => { if (v.result?.verdict) verdicts[v.result.verdict] = (verdicts[v.result.verdict] || 0) + 1; });
  if (run.status === "finished") {
    const c = run.counts || verdicts;
    return `Finished in ${fmtDur(secs(run.started, run.ended))}: <b>${c.confirmed ?? 0} confirmed</b>, ${c.unverified ?? 0} unverified, ${c.rejected ?? 0} rejected.${run.errors?.length ? ` ${run.errors.length} agent(s) failed, so the result is incomplete.` : ""}`;
  }
  const cur = STAGES.find(([k]) => run.stages[k]?.status === "running")?.[0];
  if (!cur) return "Checking out the commits…";
  if (cur === "review") {
    const plan = state.tasks.get(`${run.id}|plan`)?.result;
    const bits = subs.map((a) => `${esc(a.agent.replace("subagent:", ""))} ${isDone(a) ? `done (${a.result?.findings ?? 0})` : "working"}`);
    return `${plan ? `Planned ${plan.hypotheses?.length || 0} hypotheses. ` : "Planning the review. "}${subs.length ? `Subagents in parallel: ${bits.join(", ")}.` : "Starting the subagents."}`;
  }
  if (cur === "validation") {
    const n = run.candidates ?? vals.length;
    return `Re-checking ${n} candidate finding${n === 1 ? "" : "s"} with the second model: ${vals.filter(isDone).length} done (${verdicts.confirmed} confirmed, ${verdicts.unverified} unverified, ${verdicts.rejected} rejected).`;
  }
  return `${esc(STAGE_LABEL[cur])}: ${esc(stageDetail(cur, run.stages[cur], run))}.`;
}

// the review the side panels describe: the one picked, else the running one, else the latest
function currentRun() {
  const runs = [...state.runs.values()];
  return (state.orchRun && state.runs.get(state.orchRun)) || runs.find((r) => r.status === "running") || runs[runs.length - 1] || null;
}

function renderOrchestrator() {
  const box = $("orch");
  if (!box) return;
  const runs = [...state.runs.values()];
  const run = currentRun();
  const running = !!run && run.status === "running";
  box.classList.toggle("collapsed", state.orchCollapsed);
  box.classList.toggle("has-running", running || state.busy);
  $("orch-toggle").setAttribute("aria-expanded", String(!state.orchCollapsed));
  $("orch-dot").className = "activity-dot" + (running || state.busy ? " running" : "");
  $("orch-title").textContent = !run ? (state.busy ? "Orchestrator · thinking" : "Orchestrator")
    : running ? `Orchestrator · ${runProgress(run)}%` : `Orchestrator · ${run.status}`;
  if (state.orchCollapsed) return;
  const lead = state.busy ? `<div class="orch-lead">lead agent: ${esc(state.lastLead || "working")}…</div>` : "";
  if (!run) {
    $("orch-body").innerHTML = `<div class="orch-now muted">${state.preparing ? "Getting the repository; the review starts once it is ready." : state.busy ? "The lead agent is working out what to run." : "No review in this conversation yet. Ask for one and its progress shows here."}</div>${lead}`;
    return;
  }
  const pct = runProgress(run);
  const phases = STAGES.map(([k, l]) => { const e = run.stages[k]; const st = e?.status || "pending";
    return `<div class="ph ${st}"><span class="pi">${st === "done" ? "✓" : st === "running" ? "◌" : "○"}</span><span class="pl">${esc(l)}</span><span class="pd">${esc(stageDetail(k, e, run))}</span></div>`; }).join("");
  const plan = state.tasks.get(`${run.id}|plan`);
  const hyps = plan?.result?.hypotheses || [];
  const subs = tasksOf(run, "agent"), vals = tasksOf(run, "validator");
  const orow = (t, label, sub) => `<div class="orow ${t.status}" data-task="${esc(t.id)}"><span class="dot ${t.status === "done" ? "finished" : t.status}"></span><span class="on">${esc(label)}</span><span class="os">${esc(sub)}</span></div>`;
  const verdictOf = (v) => v.result?.verdict || (isDone(v) ? "done" : "checking");
  $("orch-body").innerHTML = `
    <div class="orch-prog"><div class="orch-bar ${run.status}"><span style="width:${pct}%"></span></div>
      <div class="pmeta"><span>${esc(run.id)}${runs.length > 1 ? ` · review ${runs.indexOf(run) + 1} of ${runs.length}` : ""}</span><span>${pct}% · ${fmtDur(secs(run.started, run.ended || new Date().toISOString()))}</span></div></div>
    <div class="orch-now">${narrate(run)}</div>
    <div class="orch-sec"><div class="kv">phases</div><div class="orch-phases">${phases}</div></div>
    ${hyps.length ? `<div class="orch-sec"><div class="kv">plan · ${hyps.length} hypotheses</div><ul class="orch-hyp">${hyps.slice(0, 4).map((h) => `<li>${esc(short(h, 110))}</li>`).join("")}</ul></div>` : ""}
    ${subs.length ? `<div class="orch-sec"><div class="kv">subagents</div><div class="orch-list">${subs.map((a) => orow(a, a.agent.replace("subagent:", ""), isDone(a) ? `${a.result?.findings ?? 0} findings` : "working")).join("")}</div></div>` : ""}
    ${vals.length ? `<div class="orch-sec"><div class="kv">validation · ${vals.filter(isDone).length}/${run.candidates ?? vals.length}</div><div class="orch-list">${vals.map((v) => orow(v, v.agent.replace("validator:", ""), verdictOf(v))).join("")}</div></div>` : ""}
    ${lead}`;
  $("orch-body").querySelectorAll("[data-task]").forEach((el) => el.addEventListener("click", () => openTask(el.dataset.task)));
}

// ---------------------------------------------------------------- MCP panel: servers and every MCP call
function renderMcp() {
  const box = $("mcp");
  if (!box) return;
  const run = currentRun(), configured = state.config?.mcp_servers || [];
  const servers = run?.mcp?.servers || null;
  const calls = run ? [...state.tasks.values()].filter((t) => t.run === run.id && isMcpTask(t)) : [];
  const running = calls.filter((t) => t.status === "running").sort((a, b) => a.seq - b.seq);
  const done = calls.filter((t) => t.status !== "running").sort((a, b) => b.seq - a.seq);
  const up = servers ? servers.filter((x) => x.status === "connected").length : 0;
  box.classList.toggle("collapsed", state.mcpCollapsed);
  box.classList.toggle("has-running", running.length > 0);
  $("mcp-toggle").setAttribute("aria-expanded", String(!state.mcpCollapsed));
  $("mcp-dot").className = "activity-dot" + (running.length ? " running" : "");
  $("mcp-title").textContent = running.length ? `MCP · ${running.length} running`
    : calls.length ? `MCP · ${plural(calls.length, "call")}`
    : servers ? `MCP · ${up}/${servers.length} connected`
    : configured.length ? `MCP · ${plural(configured.length, "server")}` : "MCP · none configured";
  if (state.mcpCollapsed) return;
  const offered = (servers || []).flatMap((x) => x.tools.filter((t) => t.agent_tool).map((t) => t.agent_tool));
  const srvRow = (x) => {
    const key = `mcp-srv:${x.name}`, ok = x.status === "connected";
    const tag = run.mcp.webSearch === x.name ? " · web search" : "";
    const tools = x.tools.map((t) => `<div class="mtool${t.agent_tool ? "" : " off"}" title="${esc(t.description)}"><span>${esc(t.name)}</span><span class="as">${t.agent_tool ? `agents: ${esc(t.agent_tool)}` : "not offered"}</span></div>`).join("");
    return `<details class="msrv" data-key="${esc(key)}"${state.openKeys.has(key) ? " open" : ""}><summary><span class="dot ${ok ? "connected" : "failed"}"></span><span class="mn">${esc(x.name)}</span><span class="ms">${ok ? `${plural(x.tools.length, "tool")}${tag}` : "failed"}${x.connect_s != null ? ` · ${fmtDur(x.connect_s)}` : ""}</span></summary>
      ${x.error ? `<div class="merr">${esc(x.error)}</div>` : ""}${tools ? `<div class="mtools">${tools}</div>` : ""}</details>`;
  };
  const callRow = (t) => {
    const r = t.result || {}, m = r.mcp || {};
    const server = m.server || String(r.origin || "").slice(4), status = m.status || t.status;
    const right = status === "running" ? "running…" : status === "failed" ? "error" : fmtDur(m.duration_s ?? taskDur(t));
    return `<div class="mcall ${status}" data-task="${esc(t.id)}" title="${esc(`${server} · ${r.remote_name || r.tool}`)}"><span class="mi">${status === "running" ? "◌" : "⇄"}</span>
      <span style="min-width:0"><div class="mt">${esc(server)} · ${esc(r.remote_name || r.tool)}(${esc(fmtArgs(m.input || r.input, 40))})</div><div class="mw">${esc(agentName(t.agent || ""))}${m.output_chars != null ? ` · ${m.output_chars} chars` : ""}</div></span>
      <span class="md">${esc(right)}</span></div>`;
  };
  const names = configured.map((n) => `<code>${esc(n)}</code>`).join(", ");
  const starting = !!run && !servers && run.status === "running" && configured.length > 0;
  let srv;
  if (servers?.length) srv = servers.map(srvRow).join("");
  else if (starting) srv = `<div class="mcp-empty">Starting ${names}…</div>`;
  else if (run) srv = `<div class="mcp-empty">No MCP servers were recorded for this review.${configured.length ? ` Configured now: ${names}.` : ""}</div>`;
  else if (configured.length) srv = `<div class="mcp-empty">Configured: ${names}. Their status shows once a review starts them.</div>`;
  else srv = '<div class="mcp-empty">No MCP servers configured. Add them under <code>mcp_servers</code> in your config (see <code>deploy/mcp/README.md</code>), then start a review.</div>';
  let list;
  if (calls.length) list = (running.length ? running.map(callRow).join("") : "") + done.map(callRow).join("");
  else if (!run) list = '<div class="mcp-empty">MCP calls appear here when a review runs.</div>';
  else if (!servers) list = `<div class="mcp-empty">${starting ? "Waiting for the servers to start." : "No MCP calls in this review."}</div>`;
  else if (!up) list = '<div class="mcp-empty">No server is connected, so the agents have no MCP tools.</div>';
  else list = `<div class="mcp-empty">${run.status === "running" ? "No MCP calls yet." : "The agents did not call an MCP tool in this review."} They were offered: ${offered.map((n) => `<code>${esc(n)}</code>`).join(", ") || "none"}.</div>`;
  $("mcp-body").innerHTML = `<div class="mcp-sec"><div class="kv">calls${calls.length ? ` · ${calls.length}` : ""}${running.length ? ` · ${running.length} running` : ""}</div>${list}</div>
    <div class="mcp-sec"><div class="kv">servers${run && servers ? ` · ${esc(run.id)}` : ""}</div>${srv}</div>`;
  $("mcp-body").querySelectorAll("[data-task]").forEach((el) => el.addEventListener("click", () => openTask(el.dataset.task)));
}

// ---------------------------------------------------------------- memory panel: what the agents remembered, KV cache
const runMem = (run) => (run.memory = run.memory || { info: null, used: [], ignored: [], recorded: null, errors: [] });
const fmtK = (n) => (n == null ? "?" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
const pctOf = (a, b) => (b ? Math.round((100 * a) / b) : null);
const MEMORY_TOOLS = new Set(["memory_read", "memory_note"]);

// prompt tokens served from the KV (prefix) cache, as the server reported them per call
function cacheUse(tasks) {
  const u = { input: 0, cached: 0, reported: 0, calls: 0 };
  for (const t of tasks) {
    const x = t.result?.usage;
    if (!x) continue;
    u.calls++;
    if (x.cached_input_tokens != null) { u.reported++; u.input += x.input_tokens || 0; u.cached += x.cached_input_tokens; }
  }
  return u;
}
const agentGroup = (a = "") => (a === "review_agent" ? "Planner" : a === "orchestrator" ? "Orchestrator" : a.startsWith("subagent:") ? "Reviewers" : a.startsWith("validator") ? "Validators" : a === "lead" ? "Lead agent" : "Other");

function cacheBar(name, pct, meta, err) {
  return `<div class="cache-row${err ? " err" : ""}"><div class="cache-head"><span class="n">${name}</span><span class="v">${pct == null ? "–" : `${pct}%`}</span></div>
    <div class="cache-bar"><span style="width:${pct ?? 0}%"></span></div><div class="cache-meta">${meta}</div></div>`;
}

function memConversationHtml() {
  const c = state.convMemory;
  const leadTools = [...state.tasks.values()].filter((t) => t.run === "lead" && MEMORY_TOOLS.has(t.result?.tool)).sort((a, b) => a.seq - b.seq);
  const leadCache = cacheUse([...state.tasks.values()].filter((t) => t.run === "lead" && t.kind === "llm"));
  let html = "";
  if (!c) html = '<div class="mem-note">The lead agent has not answered in this conversation yet.</div>';
  else {
    const older = c.messages_total - c.messages_used;
    if (!c.messages_total) html = `<div class="mem-line">The last answer was to the first message, so there was no earlier conversation to remember${c.last_review ? `; it had the summary of <b>${esc(c.last_review)}</b>` : ""}.</div>`;
    else html = `<div class="mem-line">The last answer saw <b>${c.messages_used} of ${c.messages_total}</b> earlier messages${c.cut ? ` (${plural(c.cut, "message")} cut at ${c.chars_cap} characters)` : ""}${c.last_review ? ` and the summary of <b>${esc(c.last_review)}</b> (${plural(c.last_review_findings, "finding")})` : ""}.${older > 0 ? ` ${plural(older, "older message")} ${older === 1 ? "is" : "are"} not in its prompt.` : ""}</div>`;
    if (c.used?.length) {
      const key = "mem:conv";
      html += `<details class="mem-det" data-key="${key}"${state.openKeys.has(key) ? " open" : ""}><summary>What it was given ›</summary><div class="mem-msgs">${c.used.map((m) => `<div class="mem-msg${m.chars > c.chars_cap ? " cut" : ""}" title="${esc(m.preview)}"><span class="r">${m.role === "user" ? "you" : "ai"}</span><span class="p">${esc(m.preview)}</span><span class="c">${m.chars > c.chars_cap ? `${c.chars_cap}/${m.chars}` : m.chars}</span></div>`).join("")}</div></details>`;
    }
  }
  html += leadTools.map((t) => `<div class="mem-row" data-task="${esc(t.id)}"><span>Lead agent ${t.result.tool === "memory_note" ? "saved a note to" : "read"} the project memory</span><span class="s">${esc(t.status)}</span></div>`).join("");
  if (leadCache.reported) html += cacheBar("Lead agent prompts from the KV cache", pctOf(leadCache.cached, leadCache.input), `${fmtK(leadCache.cached)} of ${fmtK(leadCache.input)} prompt tokens · ${plural(leadCache.reported, "call")}`);
  return html;
}

function memProjectHtml(run) {
  const m = run.memory || {}, info = m.info;
  let html = "";
  if (!info) html = `<div class="mem-note">${run.status === "running" ? "Reading .scr_agents/…" : "This review did not record its project memory."}</div>`;
  else if (!info.exists) html = '<div class="mem-line">No <code>.scr_agents/</code> in this repository yet. This review creates it, and later reviews remember its findings.</div>';
  else html = `<div class="mem-line"><code>.scr_agents/</code> read from ${esc(info.read_from)}: ${info.notes_chars ? `notes ${fmtK(info.notes_chars)} chars` : "no notes"}, ${plural(info.history, "finding")} from earlier reviews${info.overrides?.length ? `, overrides ${esc(info.overrides.join(", "))}` : ""}.</div>`;
  const given = (m.used || []).filter((u) => !u.prior);
  if (given.length) {
    const names = [...new Set(given.map((u) => agentName(u.agent || "")))];
    const notes = Math.max(...given.map((u) => u.notes_chars || 0)), rej = Math.max(...given.map((u) => (u.rejected || []).length));
    html += `<div class="mem-line">Given to ${esc(names.join(", "))}: notes (${fmtK(notes)} chars)${rej ? ` and ${plural(rej, "finding")} people rejected` : ""}.</div>`;
  }
  const hits = (m.used || []).filter((u) => u.prior?.length);
  if (hits.length) {
    html += `<div class="kv" style="margin:6px 0 2px">hits · findings seen in earlier reviews</div>` + hits.map((u) => {
      const human = u.prior.filter((p) => p.human_verdict);
      const rows = u.prior.map((p) => `${esc((p.ts || "").slice(0, 10))} ${esc(p.run_id || "")}: agent ${esc(p.verdict || "?")}${p.human_verdict ? `, <span class="${p.human_verdict === "reject" ? "rej" : ""}">human ${esc(p.human_verdict)}${p.human_note ? ` (${esc(short(p.human_note, 60))})` : ""}</span>` : ""}`).join("<br>");
      return `<div class="mem-hit" data-task="${esc(`${run.id}|agent:validator:${u.finding}`)}" title="${esc(u.title || "")}"><span class="ht">${esc(u.finding)} · ${esc(u.title || "")}</span><span class="hn">${plural(u.prior.length, "earlier review")}${human.length ? ` · ${human.length} human` : ""}</span><span class="hp">${rows}</span></div>`;
    }).join("");
  } else if (info?.exists) {
    html += `<div class="mem-note">${run.status === "running" ? "Earlier reviews of the same findings show here as the validators start." : "No finding matched one from an earlier review."}</div>`;
  }
  const ign = (stage) => (m.ignored || []).filter((e) => (e.stage === "review") === (stage === "review")).reduce((n, e) => n + (e.count || 0), 0);
  const ignHits = ign("scanners"), ignFindings = ign("review");
  if (ignHits || ignFindings) html += `<div class="mem-line">Ignored by the rules in <code>project.yaml</code>: ${[ignHits ? plural(ignHits, "scanner hit") : null, ignFindings ? plural(ignFindings, "finding") : null].filter(Boolean).join(", ")}.</div>`;
  const tools = [...state.tasks.values()].filter((t) => t.run === run.id && t.kind === "tool" && MEMORY_TOOLS.has(t.result?.tool)).sort((a, b) => a.seq - b.seq);
  html += tools.map((t) => `<div class="mem-row" data-task="${esc(t.id)}"><span>${esc(agentName(t.agent || ""))} ${t.result.tool === "memory_note" ? "saved a note" : "read the memory"}</span><span class="s">${t.status === "running" ? "running…" : fmtDur(taskDur(t))}</span></div>`).join("");
  if (m.recorded != null) html += `<div class="mem-line">Recorded ${plural(m.recorded, "finding")} for future reviews.</div>`;
  if (m.errors?.length) html += m.errors.map((x) => `<div class="mem-note" style="color:var(--bad)">${esc(x)}</div>`).join("");
  return html;
}

function memCacheHtml(run) {
  if (run.mode === "offline") return '<div class="mem-note">Offline mode: no model server, so no KV cache.</div>';
  const llm = [...state.tasks.values()].filter((t) => t.run === run.id && t.kind === "llm");
  const all = cacheUse(llm);
  let html = "";
  if (all.reported) {
    html += cacheBar("This review's prompts", pctOf(all.cached, all.input), `${fmtK(all.cached)} of ${fmtK(all.input)} prompt tokens came from the cache · ${plural(all.reported, "call")}`);
    const groups = {};
    for (const t of llm) (groups[agentGroup(t.agent)] = groups[agentGroup(t.agent)] || []).push(t);
    for (const [g, ts] of Object.entries(groups)) {
      const u = cacheUse(ts);
      if (u.reported) html += cacheBar(esc(g), pctOf(u.cached, u.input), `${fmtK(u.cached)} / ${fmtK(u.input)} tokens · ${plural(u.reported, "call")}`);
    }
  } else if (all.calls) {
    html += '<div class="mem-note">The model server does not report cache hits per call. Start vLLM with <code>--enable-prompt-tokens-details</code> (SGLang: <code>--enable-cache-report</code>) to see them per agent.</div>';
  }
  for (const x of run.kv?.servers || []) {
    const name = `${esc(x.server)} · ${esc(String(x.model || "").split("/").pop())}${x.roles?.length ? ` (${esc(x.roles.join(", "))})` : ""}`;
    if (x.error) { html += cacheBar(name, null, esc(x.error), true); continue; }
    const rate = x.run_hit_rate ?? x.hit_rate ?? null;
    const meta = [x.run_queries != null ? (x.run_queries ? `this review: ${fmtK(x.run_hits)} of ${fmtK(x.run_queries)} tokens` : "this review: no prompts yet") : null,
      x.usage_perc != null ? `KV cache ${x.usage_perc}% full` : null,
      x.lifetime_hit_rate != null ? `since start ${Math.round(x.lifetime_hit_rate * 100)}%` : null].filter(Boolean).join(" · ");
    html += cacheBar(name, rate == null ? null : Math.round(rate * 100), meta);
  }
  if (run.kv?.servers?.length) html += '<div class="mem-note">Server-wide counters from /metrics: they include other clients of the same server.</div>';
  if (!html) html = `<div class="mem-note">${run.status === "running" ? "Waiting for the first model call…" : "No cache figures were recorded for this review."}</div>`;
  return html;
}

function renderMem() {
  const box = $("mem");
  if (!box) return;
  const run = currentRun(), chat = state.view?.type === "chat";
  const hits = run ? (run.memory?.used || []).filter((u) => u.prior?.length).length : 0;
  const llm = run ? [...state.tasks.values()].filter((t) => t.run === run.id && t.kind === "llm") : [];
  const u = cacheUse(llm);
  let kv = u.reported ? pctOf(u.cached, u.input) : null;
  if (kv == null && run?.kv?.servers) {
    const q = run.kv.servers.reduce((a, x) => a + (x.run_queries || 0), 0), h = run.kv.servers.reduce((a, x) => a + (x.run_hits || 0), 0);
    kv = q ? pctOf(h, q) : null;
  }
  box.classList.toggle("collapsed", state.memCollapsed);
  box.classList.toggle("has-hits", hits > 0);
  $("mem-toggle").setAttribute("aria-expanded", String(!state.memCollapsed));
  $("mem-title").textContent = ["Memory", hits ? plural(hits, "hit") : null, kv != null ? `KV cache ${kv}%` : null].filter(Boolean).join(" · ");
  if (state.memCollapsed) return;
  const secs_ = [];
  if (chat) secs_.push(`<div class="mem-sec"><div class="kv">conversation</div>${memConversationHtml()}</div>`);
  if (run) {
    secs_.push(`<div class="mem-sec"><div class="kv">project memory · ${esc(run.id)}</div>${memProjectHtml(run)}</div>`);
    secs_.push(`<div class="mem-sec"><div class="kv">KV cache</div>${memCacheHtml(run)}</div>`);
  } else secs_.push('<div class="mem-sec"><div class="kv">project memory · KV cache</div><div class="mem-note">Shown once a review runs.</div></div>');
  $("mem-body").innerHTML = secs_.join("");
  $("mem-body").querySelectorAll("[data-task]").forEach((el) => el.addEventListener("click", () => { if (state.tasks.has(el.dataset.task)) openTask(el.dataset.task); }));
}

// ---------------------------------------------------------------- configure menu (top left): MCP servers, skills
// What a server runs comes from mcp_servers/<server>/mcp.yaml on disk; the console only switches things on and off.
const COMP_LABEL = { bugs: "Bug reviewer", vulnerabilities: "Security reviewer", style: "Style reviewer", validator: "Validator" };
const S = { tab: "mcp_servers", servers: null, skills: null, tests: {}, bodies: {}, open: new Set(), saved: "", error: "" };

function initSettings() {
  $("cfg-btn").addEventListener("click", (ev) => { ev.stopPropagation(); toggleCfgMenu(); });
  $("cfg-menu").addEventListener("click", (ev) => { const b = ev.target.closest("[data-settings]"); if (b) { toggleCfgMenu(false); openSettings(b.dataset.settings); } });
  document.addEventListener("click", (ev) => { if (!$("cfg-menu").hidden && !ev.target.closest(".cfg-wrap")) toggleCfgMenu(false); });
  document.querySelectorAll(".settings-tabs [data-tab]").forEach((b) => b.addEventListener("click", () => openSettings(b.dataset.tab)));
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-body").addEventListener("change", onSettingsChange);
  $("settings-body").addEventListener("click", onSettingsClick);
}

function toggleCfgMenu(open) {
  const menu = $("cfg-menu"), show = open ?? menu.hidden;
  menu.hidden = !show;
  $("cfg-btn").setAttribute("aria-expanded", String(show));
  if (show) menu.querySelector("button")?.focus();
}

async function openSettings(tab) {
  S.tab = tab; S.saved = ""; S.error = "";
  $("settings").hidden = false;
  document.querySelectorAll(".settings-tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  renderSettings();
  await loadSettings(tab);
}
function closeSettings() { $("settings").hidden = true; }

async function loadSettings(tab) {
  try {
    if (tab === "mcp_servers") S.servers = await (await fetch("/api/settings/mcp-servers")).json();
    if (tab === "skills") S.skills = await (await fetch("/api/settings/skills")).json();
    if (tab === "models") state.config = await (await fetch("/api/config")).json();
  } catch (err) { S.error = `could not load: ${err.message || err}`; }
  renderSettings();
}

async function saveSetting(path, body, what) {
  S.error = ""; S.saved = "";
  try {
    const res = await api(path, body);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    if (path.startsWith("/api/settings/skills")) S.skills = data; else S.servers = data;
    S.saved = `${what} · applies to reviews started from now on`;
  } catch (err) { S.error = String(err.message || err); }
  renderSettings();
}

function onSettingsChange(ev) {
  const el = ev.target;
  if (el.dataset.mcpEnabled) saveSetting(`/api/settings/mcp-servers/${encodeURIComponent(el.dataset.mcpEnabled)}`, { enabled: el.checked }, `${el.dataset.mcpEnabled} ${el.checked ? "on" : "off"}`);
  else if (el.dataset.mcpComp) {
    const name = el.dataset.mcpComp;
    const picked = [...$("settings-body").querySelectorAll(`[data-mcp-comp="${CSS.escape(name)}"]`)].filter((x) => x.checked).map((x) => x.value);
    if (!picked.length) { el.checked = true; S.error = "Leave at least one agent ticked, or switch the server off."; renderSettings(); return; }
    const all = picked.length === S.servers.components.length;
    saveSetting(`/api/settings/mcp-servers/${encodeURIComponent(name)}`, { components: all ? [] : picked }, `${name}: ${all ? "all agents" : picked.map((c) => COMP_LABEL[c]).join(", ")}`);
  } else if (el.id === "ws-server") saveSetting("/api/settings/web-search", { server: el.value || null }, el.value ? `web search: ${el.value}` : "web search off");
  else if (el.dataset.skillEnabled) saveSetting(`/api/settings/skills/${encodeURIComponent(el.dataset.skillEnabled)}`, { enabled: el.checked }, `${el.dataset.skillEnabled} ${el.checked ? "on" : "off"}`);
}

async function onSettingsClick(ev) {
  const test = ev.target.closest("[data-mcp-test]");
  if (test) {
    const name = test.dataset.mcpTest;
    S.tests[name] = { running: true };
    renderSettings();
    try {
      const res = await api(`/api/settings/mcp-servers/${encodeURIComponent(name)}/test`, {});
      S.tests[name] = await res.json();
    } catch (err) { S.tests[name] = { ok: false, error: String(err.message || err) }; }
    renderSettings();
    return;
  }
  const view = ev.target.closest("[data-skill-view]");
  if (view) {
    const name = view.dataset.skillView;
    if (S.open.has(name)) S.open.delete(name); else {
      S.open.add(name);
      if (!S.bodies[name]) { try { S.bodies[name] = (await (await fetch(`/api/settings/skills/${encodeURIComponent(name)}`)).json()).body || ""; } catch { S.bodies[name] = "(could not load)"; } }
    }
    renderSettings();
    return;
  }
  if (ev.target.closest("[data-reload]")) { S.bodies = {}; S.saved = ""; loadSettings(S.tab); }
}

const sw = (attr, value, on, label, disabled = false) => `<label class="switch"><input type="checkbox" ${attr}="${esc(value)}"${on ? " checked" : ""}${disabled ? " disabled" : ""}><span class="track"></span>${label ? `<span>${esc(label)}</span>` : ""}</label>`;

function mcpCard(x, d) {
  const cfgFile = x.source === "config";
  const comps = x.components?.length ? x.components : d.components;
  const t = S.tests[x.name];
  const status = cfgFile ? '<span class="pill">from config file</span>' : x.ready ? '<span class="pill ok">ready</span>' : '<span class="pill warn">not ready</span>';
  const isWs = d.web_search.server === x.name && d.web_search.enabled;
  let test = "";
  if (t?.running) test = '<div class="muted">starting the server…</div>';
  else if (t?.ok) test = `<div class="set-saved">Started in ${fmtDur(t.duration_s)} · ${plural(t.tools.length, "tool")}</div><div class="set-tools">${t.tools.map((x) => `<div><b>${esc(x.name)}</b><span title="${esc(x.description)}">${esc(x.description)}</span></div>`).join("")}</div>`;
  else if (t) test = `<div class="set-err">${esc(t.error || "failed")}</div>${t.install ? `<div class="set-missing">install: <code>${esc(t.install)}</code></div>` : ""}`;
  return `<div class="set-card${x.enabled ? "" : " off"}">
    <div class="set-head">${cfgFile ? "" : sw("data-mcp-enabled", x.name, x.enabled, "")}<div class="grow"><h3>${esc(x.name)}</h3></div>${isWs ? '<span class="pill ws">web search</span>' : ""}${status}
      <button class="btn small" data-mcp-test="${esc(x.name)}"${!x.ready && !cfgFile ? " disabled" : ""}>Test</button></div>
    ${x.description ? `<div class="set-desc">${esc(x.description)}${x.homepage ? ` · <a class="link" href="${esc(x.homepage)}" target="_blank" rel="noopener noreferrer">home ↗</a>` : ""}</div>` : ""}
    <div class="set-cmd" title="from ${esc(x.folder ? `mcp_servers/${x.folder}/mcp.yaml` : "the config file")}; not editable here">$ ${esc(x.command.join(" "))}</div>
    <div class="muted" style="font-size:12px">replies: ${x.result_format && x.result_format !== "auto" ? `<code>${esc(x.result_format)}</code> (<code>result_format</code> in mcp.yaml)` : "any format (JSON, CSV, Markdown, HTML, …), recognised per reply"}</div>
    ${x.env_keys?.length ? `<div class="muted" style="font-size:12px">environment: ${x.env_keys.map((k) => `<code>${esc(k)}</code>`).join(" ")} (values not shown)</div>` : ""}
    ${x.missing?.length ? `<div class="set-missing">Missing: ${x.missing.map((m) => esc(m)).join("; ")}${x.install ? `<br>Install: <code>${esc(x.install)}</code>` : ""}</div>` : ""}
    ${x.shadows ? '<div class="set-missing">A folder in mcp_servers/ has the same name; this config-file entry is used.</div>' : ""}
    <div><div class="kv">agents that get its tools</div><div class="set-comps">${d.components.map((c) => `<label class="${cfgFile ? "dis" : ""}"><input type="checkbox" data-mcp-comp="${esc(x.name)}" value="${c}"${comps.includes(c) ? " checked" : ""}${cfgFile ? " disabled" : ""}>${esc(COMP_LABEL[c] || c)}</label>`).join("")}</div></div>
    ${test}</div>`;
}

function renderMcpSettings() {
  const d = S.servers;
  if (!d) return '<div class="muted">Loading…</div>';
  const wsChoices = d.servers.filter((x) => x.web_search && x.source === "folder");
  const ws = d.web_search;
  return `<div class="set-intro">Servers live in <code>${esc(d.dir)}</code>, one folder each with an <code>mcp.yaml</code> that says how it starts; new folders start off.
      Here you switch them on, choose which agents get their tools, and pick the web search. The command itself can only be changed in the file. <button class="link" data-reload>Reload</button></div>
    <div class="set-card"><div class="set-head"><div class="grow"><h3>Web search</h3></div>${ws.enabled ? `<span class="pill ws">${esc(ws.server)} · ${esc(ws.tool)}</span>` : '<span class="pill">off</span>'}</div>
      <div class="set-row"><label for="ws-server">Server behind the agents' <code>web_search</code> tool</label>
        <select id="ws-server"><option value="">off</option>${wsChoices.map((x) => `<option value="${esc(x.name)}"${ws.chosen === x.name ? " selected" : ""}>${esc(x.name)}${x.enabled && x.ready ? "" : " (switch it on first)"}</option>`).join("")}</select></div>
      <div class="muted" style="font-size:12px">Results are kept only from: ${ws.allowlist.map((h) => `<code>${esc(h)}</code>`).join(" ") || "(nothing: the allowlist is empty)"}. The allowlist is set in the config file (<code>web_search.allowlist</code>).</div>
      ${ws.chosen && !ws.enabled ? '<div class="set-missing">The chosen server is off or not ready, so web search is off.</div>' : ""}</div>
    ${d.servers.map((x) => mcpCard(x, d)).join("") || '<div class="set-empty">No servers yet. Add a folder with an mcp.yaml (see mcp_servers/README.md).</div>'}
    ${d.errors.map((e) => `<div class="set-err">${esc(e)}</div>`).join("")}`;
}

function renderSkillSettings() {
  const d = S.skills;
  if (!d) return '<div class="muted">Loading…</div>';
  const row = (s) => `<div class="skill-row${s.enabled ? "" : " off"}">${sw("data-skill-enabled", s.name, s.enabled, "")}
      <div style="min-width:0"><div class="sn">${esc(s.name)}</div><div class="sd">${esc(s.description || "(no description)")}</div>
        <div class="chip-row">${(s.languages.length ? s.languages : ["any language"]).map((l) => `<span class="chip">${esc(l)}</span>`).join("")}${s.categories.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}<span class="chip">${fmtK(s.chars)} chars</span></div></div>
      <button class="link" data-skill-view="${esc(s.name)}">${S.open.has(s.name) ? "Hide" : "View"}</button>
      ${S.open.has(s.name) ? `<div class="skill-body">${S.bodies[s.name] == null ? '<span class="muted">loading…</span>' : md(S.bodies[s.name], { soft: true })}<div class="muted" style="font-size:11px;margin-top:6px">${esc(s.path)}</div></div>` : ""}</div>`;
  return `<div class="set-intro">Each agent gets the <b>shared</b> skills plus its own folder. To add or change a skill, edit <code>skills/&lt;folder&gt;/&lt;name&gt;/SKILL.md</code> (format in <code>skills/README.md</code>), then <button class="link" data-reload>Reload</button>.
      Loaded from ${d.paths.map((p) => `<code>${esc(p)}</code>`).join(", ")}.</div>
    ${d.folders.map((f) => `<div class="set-group"><h2>${esc(f.label)} <code>${esc(f.folder)}/</code></h2>${f.skills.map(row).join("") || '<div class="set-empty">No skills here yet.</div>'}</div>`).join("")}
    ${d.errors.map((e) => `<div class="set-err">${esc(e)}</div>`).join("")}`;
}

function renderModelSettings() {
  const c = state.config || {}, l = c.local || {};
  const rows = [["mode", `${c.mode}${c.configured_mode && c.configured_mode !== c.mode ? ` (configured: ${c.configured_mode})` : ""}`],
    ["model server", `${l.base_url || "?"} · ${l.reachable ? "reachable" : "not reachable"}`], ["reviewer model", l.model || c.reviewer_model],
    ["validator model", l.validator_model || c.validator_model], ["served models", (l.models || []).join(", ") || "–"],
    ["scanners", (c.scanners || []).join(", ")], ["MCP servers in use", (c.mcp_servers || []).join(", ") || "none"],
    ["web search", c.web_search ? "on" : "off"], ["output folder", c.output_dir]];
  return `<div class="set-intro">These are decided by the server's config file and the command line, not from the console.</div>
    <dl class="set-kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v ?? "–")}</dd>`).join("")}</dl>`;
}

function renderSettings() {
  if ($("settings").hidden) return;
  const body = S.tab === "mcp_servers" ? renderMcpSettings() : S.tab === "skills" ? renderSkillSettings() : renderModelSettings();
  const note = S.error ? `<div class="set-err" role="alert">${esc(S.error)}</div>` : S.saved ? `<div class="set-saved" role="status">✓ Saved: ${esc(S.saved)}</div>` : "";
  $("settings-body").innerHTML = note + body;
}

// ---------------------------------------------------------------- drawer
function openTask(id) { state.selected = { type: "task", id }; renderDrawer(); $("drawer").classList.add("open"); }
function openFinding(runId, fid) { state.selected = { type: "finding", run: runId, id: fid }; renderDrawer(); $("drawer").classList.add("open"); }
function openRun(runId) { state.selected = { type: "run", id: runId }; renderDrawer(); $("drawer").classList.add("open"); }
function closeDrawer() { state.selected = null; $("drawer").classList.remove("open"); }
const pre = (o) => `<pre>${esc(typeof o === "string" ? o : JSON.stringify(o, null, 1))}</pre>`;
const section = (label, html) => `<div><div class="kv">${esc(label)}</div>${html}</div>`;
const taskLink = (id) => { const t = state.tasks.get(id); return t ? `<span class="link-task" data-open="${esc(id)}">${esc(t.title)}</span>` : ""; };

// fmt: the reply's format as the MCP client recognised it (json, csv, tsv, markdown, html, xml, text, mixed);
// without one, JSON is still recognised. truncated: the text is a preview, so a last partial row is dropped.
// HTML and XML are shown as source, never rendered.
// what the agent read: HTML reaches it as plain text, every other format as is
const agentFormat = (fmt) => (fmt === "html" ? "text" : fmt);
function renderOutput(text, fmt, truncated = false) {
  const raw = String(text ?? "");
  const m = raw.match(/<untrusted([^>]*)>\n?([\s\S]*?)\n?<\/untrusted>\s*$/);
  const body = m ? m[2] : raw;
  const flags = m && /flags="([^"]*)"/.exec(m[1]);
  let parsed = null;
  try { parsed = !fmt || fmt === "json" ? JSON.parse(body) : null; } catch { parsed = null; }
  let html = flags ? `<div class="chip-row" style="margin-bottom:6px"><span class="chip warn">safety filter: ${esc(flags[1])}</span></div>` : "";
  if (fmt === "csv" || fmt === "tsv") {
    const rows = parseDelimited(truncated ? body.replace(/\n[^\n]*$/, "") : body, fmt === "tsv" ? "\t" : ",");
    if (rows.length > 1 && rows[0].length > 1) return html + dataTable(rows[0], rows.slice(1)) + (truncated ? '<div class="muted" style="font-size:11px">preview: the full table is in the trace</div>' : "");
  }
  if (fmt === "markdown") return html + `<div class="md md-out">${md(body)}</div>`;
  if (fmt === "html" || fmt === "xml") html += `<div class="muted" style="font-size:11px;margin-bottom:4px">${fmt.toUpperCase()} source, not rendered</div>`;
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    return html + Object.entries(parsed).map(([k, v]) => (typeof v === "string" && (v.includes("\n") || v.length > 120))
      ? `<div class="kv" style="margin-top:6px">${esc(k)}</div><pre>${esc(v)}</pre>`
      : `<div><span class="chip">${esc(k)}</span> ${esc(typeof v === "string" ? v : JSON.stringify(v))}</div>`).join("");
  }
  return html + pre(Array.isArray(parsed) ? parsed : body);
}

// ---------------------------------------------------------------- full traces (every API and tool call)
function traceUrl(t, trace) {
  const name = String(trace ?? t.result?.trace ?? "").replace(/^trace\//, "");
  if (!name) return null;
  return t.run === "lead" ? `/api/chats/${encodeURIComponent(state.view?.id || "")}/trace/${encodeURIComponent(name)}`
    : `/api/runs/${encodeURIComponent(t.run)}/trace/${encodeURIComponent(name)}`;
}

function traceSection(t, trace, label = "full request &amp; response") {
  const url = traceUrl(t, trace);
  if (!url) return t.status === "running" ? "" : section("full trace", '<div class="muted">no trace recorded for this step</div>');
  const cached = state.traceCache?.[url];
  const head = `<div class="trace-head"><span class="kv" style="margin:0">${label}</span><a class="link" href="${esc(url)}" target="_blank" rel="noopener">raw JSON ↗</a></div>`;
  if (cached) return `<div class="trace-box">${head}${cached}</div>`;
  return `<div class="trace-box">${head}<button class="btn small" data-load-trace="${esc(url)}">Show the full ${t.kind === "llm" ? "request and response" : "input and output"}</button></div>`;
}

async function loadTrace(url) {
  state.traceCache = state.traceCache || {};
  state.traceCache[url] = '<div class="muted">loading…</div>';
  renderDrawer();
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json()).error || res.status);
    state.traceCache[url] = renderTrace(await res.json());
  } catch (err) { state.traceCache[url] = `<div class="muted" style="color:var(--bad)">could not load the trace: ${esc(err.message || err)}</div>`; }
  renderDrawer();
}

function renderBlocks(content) {
  if (typeof content === "string") return pre(content);
  if (!Array.isArray(content)) return pre(content);
  return content.map((b) => {
    if (!b || typeof b !== "object") return pre(b);
    if (b.type === "text") return pre(b.text || "");
    if (b.type === "tool_use") return `<div class="tblk"><span class="chip">tool_use · ${esc(b.name)}</span>${pre(b.input ?? {})}</div>`;
    if (b.type === "tool_result") return `<div class="tblk"><span class="chip${b.is_error ? " warn" : ""}">tool_result${b.is_error ? " · error" : ""}</span>${renderOutput(typeof b.content === "string" ? b.content : JSON.stringify(b.content))}</div>`;
    if (b.type === "thinking") return `<div class="tblk"><span class="chip">thinking</span>${pre(b.thinking || "")}</div>`;
    return pre(b);
  }).join("");
}

function renderTrace(d) {
  if (d.kind === "mcp") {
    const content = d.response?.content || [];
    return `<div class="chip-row"><span class="chip">server ${esc(d.server)}</span><span class="chip">${esc(d.request?.method || "tools/call")} ${esc(d.tool)}</span><span class="chip">${esc(fmtDur(d.duration_s))}</span>${d.response?.isError ? '<span class="chip warn">isError</span>' : ""}</div>`
      + section("request sent to the server", pre(d.request ?? {}))
      + (d.error ? section("error", `<pre style="color:var(--bad)">${esc(d.error)}</pre>`)
        : section(`reply from the server (${content.length} content item${content.length === 1 ? "" : "s"}, secrets redacted)`, content.map((c) => `<div class="tblk"><span class="chip">${esc(c.type || "text")}</span>${renderOutput(c.text ?? "")}</div>`).join("") || '<div class="muted">empty reply</div>'));
  }
  if (d.kind === "tool") {
    return section("input", pre(d.input ?? {})) + section(`output as the model received it (${String(d.output ?? "").length} chars)`, renderOutput(d.output));
  }
  const req = d.request || {}, msgs = req.messages || [], ex = d.exchanges || [];
  const tools = (req.tools || []).map((t) => t.name).filter(Boolean);
  let html = `<div class="chip-row"><span class="chip">${esc(d.backend || "?")}</span><span class="chip">${esc(d.model || "")}</span><span class="chip">${esc(fmtDur(d.duration_s))}</span><span class="chip">max_tokens ${esc(req.max_tokens ?? "?")}</span><span class="chip">${msgs.length} messages</span>${req.output_schema ? '<span class="chip">structured output</span>' : ""}</div>`;
  if (d.error) html += section("error", `<pre style="color:var(--bad)">${esc(d.error)}</pre>`);
  if (tools.length) html += section(`tools offered (${tools.length})`, `<div class="chip-row">${tools.map((n) => `<span class="chip">${esc(n)}</span>`).join("")}</div>`);
  html += section("system prompt", `<details class="ev"><summary>${esc(short(req.system, 90))}</summary>${pre(req.system || "")}</details>`);
  html += section(`messages sent (${msgs.length})`, msgs.map((m, i) => {
    const kinds = Array.isArray(m.content) ? m.content.map((b) => b?.type || "?").join(", ") : `${String(m.content ?? "").length} chars`;
    return `<details class="ev"${i === msgs.length - 1 ? " open" : ""}><summary>#${i + 1} ${esc(m.role)} · ${esc(kinds)}</summary>${renderBlocks(m.content)}</details>`;
  }).join(""));
  if (d.response) html += section(`response · ${esc(d.response.stop_reason || "")} · in ${d.response.usage?.input_tokens ?? 0} / out ${d.response.usage?.output_tokens ?? 0} tokens`, renderBlocks(d.response.content));
  html += section(`API calls (${ex.length})`, ex.length ? ex.map((x) => `<details class="ev"><summary>${esc(x.api || "")} ${esc(x.url || x.endpoint || "")}${x.purpose ? " · " + esc(x.purpose) : ""}${x.attempt > 1 ? " · attempt " + x.attempt : ""} · status ${esc(x.status ?? "–")} · ${esc(fmtDur(x.duration_s))}</summary>
      <div class="kv" style="margin-top:6px">request body</div>${pre(x.request ?? {})}
      <div class="kv" style="margin-top:6px">${x.error ? "error" : "response body"}</div>${pre(x.error ?? x.response ?? {})}</details>`).join("")
    : '<div class="muted">none: this backend runs in-process (offline mode)</div>');
  return html;
}

function renderDrawer() {
  const sel = state.selected;
  if (!sel) return;
  if (sel.type === "finding") return renderFindingDrawer(sel.run, sel.id);
  if (sel.type === "run") return renderRunDrawer(sel.id);
  const t = state.tasks.get(sel.id);
  if (!t) return;
  $("drawer-kind").textContent = isMcpTask(t) ? KIND_LABEL.mcp : KIND_LABEL[t.kind] || t.kind;
  $("drawer-title").textContent = t.title;
  const meta = [`status ${t.status}`];
  if (t.started) meta.push(`started ${t.started.slice(11, 19)}`);
  const d = taskDur(t); if (d != null) meta.push(`took ${fmtDur(d)}`);
  if (t.agent) meta.push(t.agent);
  $("drawer-meta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");
  const r = t.result || {}, parts = [];
  if (t.status === "running") parts.push('<div class="muted">still running…</div>');
  if (r.error) parts.push(section("error", `<pre style="color:var(--bad)">${esc(r.error)}</pre>`));
  if (isMcpTask(t)) {
    const m = r.mcp || {}, server = m.server || String(r.origin || "").slice(4);
    parts.push(section("MCP call", `<div class="chip-row"><span class="chip">server ${esc(server)}</span><span class="chip">tool ${esc(m.tool || r.remote_name || r.tool)}</span>${t.kind === "tool" ? `<span class="chip">agents call it ${esc(r.tool)}</span>` : ""}${m.duration_s != null ? `<span class="chip">server ${fmtDur(m.duration_s)}</span>` : ""}${m.format ? `<span class="chip" title="${esc((m.parts || []).map((p) => `${p.type}: ${p.format}${p.mime ? ` (${p.mime})` : ""}`).join(", "))}">reply ${esc(m.format)}</span>` : ""}${m.status ? `<span class="chip${m.status === "failed" ? " warn" : ""}">${esc(m.status)}</span>` : ""}</div>`));
    parts.push(section("arguments sent to the server", pre(m.input || r.input || {})));
    if (m.error) parts.push(section("MCP error", `<pre style="color:var(--bad)">${esc(m.error)}</pre>`));
    else if (m.status && m.status !== "running") parts.push(section(`server reply (${m.output_chars ?? "?"} chars${m.output_chars > (m.output_preview || "").length ? ", preview" : ""}; before any filtering, secrets redacted)`, renderOutput(m.output_preview || "", m.format, m.output_chars > (m.output_preview || "").length)));
    else if (!r.mcp && t.status !== "running") parts.push('<div class="muted">This step was recorded before MCP calls were logged on their own; the result below is what the agent received.</div>');
    if (t.kind === "tool" && t.status !== "running") parts.push(section(r.is_error ? "what the agent received (error)" : `what the agent received (${r.output_chars ?? "?"} chars, after the safety filter${r.tool === "web_search" ? " and the allowlist" : ""}${m.format === "html" ? "; HTML as text" : ""})`, renderOutput(r.output_preview || "", r.tool === "web_search" ? "json" : agentFormat(m.format), r.output_chars > (r.output_preview || "").length)));
    if (m.trace) parts.push(traceSection(t, m.trace, "MCP request &amp; reply"));
    if (t.kind === "tool") parts.push(traceSection(t, undefined, "tool call as the agent made it"));
  } else if (t.kind === "tool" || t.kind === "lead") {
    parts.push(section("input", pre(r.input || {})));
    if (t.status !== "running") parts.push(section(r.is_error ? "error output" : `output (preview, ${r.output_chars ?? "?"} chars)`, renderOutput(r.output_preview || "")));
    parts.push(traceSection(t));
  } else if (t.kind === "llm") {
    parts.push(section("model", `<div class="chip-row"><span class="chip">${esc(r.model || "")}</span>${r.stop_reason ? `<span class="chip">stop ${esc(r.stop_reason)}</span>` : ""}${r.usage ? `<span class="chip">in ${r.usage.input_tokens ?? 0} · out ${r.usage.output_tokens ?? 0}</span>` : ""}</div>`));
    if (r.text_preview) parts.push(section("model output (preview)", pre(r.text_preview)));
    parts.push(traceSection(t));
  } else if (t.kind === "agent" || t.kind === "validator") {
    const tools = [...state.tasks.values()].filter((x) => x.run === t.run && x.kind === "tool" && x.agent === t.agent).sort((a, b) => a.seq - b.seq);
    const turns = [...state.tasks.values()].filter((x) => x.run === t.run && x.kind === "llm" && x.agent === t.agent).sort((a, b) => a.seq - b.seq);
    parts.push(section("summary", `<div class="chip-row"><span class="chip">${esc(r.model || "")}</span><span class="chip">${turns.length} turns</span><span class="chip">${tools.length} tool calls</span>${r.verdict ? `<span class="chip">verdict ${esc(r.verdict)}</span>` : ""}${r.findings != null ? `<span class="chip">${r.findings} findings</span>` : ""}</div>`));
    if (t.kind === "validator" && r.finding) parts.push(`<div><button class="link" data-open-finding="${esc(t.run)}|${esc(r.finding)}">Open the finding</button></div>`);
    if (tools.length) parts.push(section("tool calls", `<table><tr><th>#</th><th>tool</th><th>status</th><th>time</th></tr>${tools.map((x, i) => `<tr><td>${i + 1}</td><td>${taskLink(x.id)}</td><td>${esc(x.status)}</td><td>${fmtDur(taskDur(x))}</td></tr>`).join("")}</table>`));
    if (turns.length) parts.push(section("model calls", `<div class="chip-row">${turns.map((x) => taskLink(x.id)).join(" ")}</div>`));
  } else if (t.kind === "plan") {
    if (r.focus_files?.length) parts.push(section("focus files", `<div class="chip-row">${r.focus_files.map((f) => `<span class="chip">${esc(f)}</span>`).join("")}</div>`));
    if (r.hypotheses?.length) parts.push(section("hypotheses", `<ul>${r.hypotheses.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>`));
    if (r.notes) parts.push(section("notes", `<div>${esc(r.notes)}</div>`));
  } else parts.push(section("result", pre(r)));
  setDrawerBody(parts);
}

function findFinding(runId, fid) { return state.runs.get(runId)?.report?.findings.find((v) => v.finding.id === fid); }

function renderFindingDrawer(runId, fid) {
  const vf = findFinding(runId, fid);
  if (!vf) return;
  const f = vf.finding;
  $("drawer-kind").textContent = `Finding · ${vf.verdict}`;
  $("drawer-title").textContent = f.title;
  $("drawer-meta").innerHTML = [`${f.severity} ${f.category}`, `${f.location.path}:${f.location.line ?? "?"}`, f.cwe?.length ? f.cwe.join(", ") : "", vf.evidence_strength ? `evidence ${vf.evidence_strength}` : "", f.id].filter(Boolean).map((m) => `<span>${esc(m)}</span>`).join("");
  const parts = [];
  parts.push(section("what is wrong", `<div class="md">${md(f.description || "")}</div>`));
  if (f.rationale) parts.push(section("reviewer's reasoning", `<div class="md">${md(f.rationale)}</div>`));
  if (vf.validator_notes) parts.push(section("validator", `<div class="md">${md(vf.validator_notes)}</div>`));
  if (vf.evidence.length) parts.push(section("evidence", vf.evidence.map((e) => `<details class="ev"><summary>${esc(e.kind)}${e.ref ? " · " + esc(e.ref) : ""}: ${esc(e.summary)}${e.command ? " · " + esc(e.command) : ""}${e.exit_code != null ? " · exit " + e.exit_code : ""}</summary>${e.output ? pre(e.output) : ""}</details>`).join("")));
  const fix = vf.fix || f.suggested_fix;
  if (fix) parts.push(section("suggested fix", pre(fix)));
  if (vf.prior?.length) parts.push(section("seen before", `<div class="chip-row">${vf.prior.map((p) => `<span class="chip">${esc(p.run_id)} · agent ${esc(p.verdict)}${p.human_verdict ? " · human " + esc(p.human_verdict) : ""}</span>`).join("")}</div>`));
  const vt = `${runId}|agent:validator:${fid}`;
  if (state.tasks.has(vt)) parts.push(`<div><span class="link-task" data-open="${esc(vt)}">How the validator checked it ›</span></div>`);
  parts.push(section("your verdict", `<div class="chip-row" id="verdict-row"><button class="btn small accept" data-v="accept">accept</button><button class="btn small reject" data-v="reject">reject</button></div>`));
  setDrawerBody(parts);
  $("drawer-body").querySelectorAll("#verdict-row [data-v]").forEach((b) => b.addEventListener("click", async () => {
    const note = prompt(`Note for ${b.dataset.v} (optional)`) || "";
    const res = await api(`/api/runs/${runId}/verdict`, { finding: fid, verdict: b.dataset.v, note });
    $("verdict-row").innerHTML = res.ok ? `<span class="chip">recorded: ${esc(b.dataset.v)}</span>` : '<span class="chip warn">could not record</span>';
    const run = state.runs.get(runId);
    if (res.ok && run) { await loadRound(run); renderRunCard(run); }
  }));
}

function renderRunDrawer(runId) {
  const run = state.runs.get(runId);
  if (!run) return;
  $("drawer-kind").textContent = "Review run";
  $("drawer-title").textContent = runId;
  $("drawer-meta").innerHTML = [run.status, fmtDur(secs(run.started, run.ended || new Date().toISOString()))].map((m) => `<span>${esc(m)}</span>`).join("");
  const parts = [];
  parts.push(section("stages", `<table>${STAGES.map(([k, l]) => { const s = run.stages[k]; const extra = s ? Object.entries(strip(s, ["stage", "status"])).map(([a, b]) => `${a}=${typeof b === "string" ? b : JSON.stringify(b)}`).join(" ") : ""; return `<tr><td>${esc(l)}</td><td>${esc(s?.status || "pending")}</td><td>${esc(short(extra, 90))}</td></tr>`; }).join("")}</table>`));
  const agents = [...state.tasks.values()].filter((t) => t.run === runId && (t.kind === "agent" || t.kind === "validator" || t.kind === "plan")).sort((a, b) => a.seq - b.seq);
  if (agents.length) parts.push(section("agents", `<table>${agents.map((t) => `<tr><td>${taskLink(t.id)}</td><td>${esc(t.subtitle || "")}</td><td>${fmtDur(taskDur(t))}</td></tr>`).join("")}</table>`));
  if (run.report?.stats) { const s = run.report.stats; parts.push(section("statistics", `<div class="chip-row"><span class="chip">mode ${esc(s.mode)}</span><span class="chip">${esc(s.duration_s)}s</span><span class="chip">review calls ${esc(s.review_llm_calls ?? "?")}</span><span class="chip">validator calls ${esc(s.validator_llm_calls ?? "?")}</span></div>`)); }
  if (run.report) parts.push(section("report", `<div class="chip-row">${reportLinks(runId)}</div><div class="muted" style="font-size:12px;margin-top:4px">Every finding with its evidence and fix, and how the review was done. The download is one file you can send or put on a shared drive.</div>`));
  if (run.report?.scanner) parts.push(section(`scanners (${run.report.scanner.findings.length} raw hits)`, `<div class="muted">ran ${esc((run.report.scanner.scanners_run || []).join(", ") || "none")}${run.report.scanner.scanners_missing?.length ? " · missing " + esc(run.report.scanner.scanners_missing.join(", ")) : ""}</div>`));
  setDrawerBody(parts);
}

function setDrawerBody(parts) {
  const body = $("drawer-body");
  const keep = body.scrollTop;
  const open = new Set([...body.querySelectorAll("details[open]")].map((d) => d.querySelector("summary")?.textContent));
  body.innerHTML = parts.join("");
  // live updates re-render the drawer: keep the reader's place and the sections they opened
  body.querySelectorAll("details").forEach((d) => { if (open.has(d.querySelector("summary")?.textContent)) d.open = true; });
  body.scrollTop = keep;
  body.querySelectorAll("[data-load-trace]").forEach((el) => el.addEventListener("click", () => loadTrace(el.dataset.loadTrace)));
  body.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => openTask(el.dataset.open)));
  body.querySelectorAll("[data-open-finding]").forEach((el) => el.addEventListener("click", () => { const [r, f] = el.dataset.openFinding.split("|"); openFinding(r, f); }));
}

init();
