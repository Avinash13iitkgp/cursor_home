const EVENT_TYPES = [
  "session.created",
  "session.claimed",
  "session.status",
  "session.completed",
  "session.failed",
  "llm.started",
  "llm.retry",
  "assistant.message",
  "assistant.delta",
  "assistant.reasoning",
  "assistant.tool",
  "tool.pending",
  "tool.started",
  "tool.succeeded",
  "tool.failed",
  "tool.retrying",
  "host.healthy",
  "host.suspect",
  "host.down",
  "host.replacing",
  "host.replaced",
  "session.migrated",
  "backend.started",
];

const MUTATING = new Set(["write_file", "replace_in_file", "delete_path"]);
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STATUS_ICON = {
  pending: "○",
  running: "◐",
  succeeded: "●",
  failed: "✕",
  retrying: "↻",
};

const transcriptEl = document.getElementById("transcript");
const bannersEl = document.getElementById("banners");
const timelineEl = document.getElementById("timeline");
const hostsEl = document.getElementById("hosts");
const changedEl = document.getElementById("changed");
const sessionsEl = document.getElementById("sessions");
const promptEl = document.getElementById("prompt");
const formErrorEl = document.getElementById("form-error");
const connectionEl = document.getElementById("connection");
const finalEl = document.getElementById("final");

const cards = new Map();
const cardsBySlot = new Map();
const slotArgs = new Map();
const counted = new Set();
const changed = new Map();
const applied = new Set();
const timeline = [];
const bannerEvents = [];

let sessions = [];
let hosts = [];
let selected = "";
let floor = 0;
let toolOnly = false;
let openTimelineSeq = 0;
let trajectoryOpen = false;
let sessionMessages = [];
let liveText = "";
let liveReasoning = "";
const toolDetails = new Map();
let submitKey = "";
let submitting = false;
let down = false;
let reconnects = 0;
let selectToken = 0;
let sessionSource = null;
let snapshotHistory = [];
let sessionsChain = Promise.resolve();
let hostsChain = Promise.resolve();

function supervisorUrl(path) {
  return "http://" + location.hostname + ":8090" + path;
}

function slotKey(step, index) {
  return step + ":" + index;
}

function parsedArgs(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : {};
  } catch (_err) {
    return {};
  }
}

function argPath(raw) {
  const path = parsedArgs(raw).path;
  return typeof path === "string" ? path : "";
}

function streamingArgs(raw) {
  const text = typeof raw === "string" ? raw : "";
  if (!text) return "";
  return text.length > 120 ? "\u2026" + text.slice(-117) : text;
}

function shortArgs(name, raw) {
  const args = parsedArgs(raw);
  if (name === "write_file" && args.path) {
    const bytes = new TextEncoder().encode(String(args.content || "")).length;
    return args.path + " (" + bytes + " bytes)";
  }
  if (args.path && name !== "search") return String(args.path);
  if (name === "search" && args.pattern) {
    const pattern = String(args.pattern);
    return pattern.length > 60 ? pattern.slice(0, 57) + "..." : pattern;
  }
  const text = typeof raw === "string" && raw ? raw : JSON.stringify(args);
  return text.length > 80 ? text.slice(0, 77) + "..." : text;
}

function hostChip(hostId, generation) {
  if (!hostId) return "";
  return generation ? hostId + " g" + generation : hostId;
}

function formatElapsed(created, updated, terminal) {
  const end = terminal ? updated : Date.now() / 1000;
  const total = Math.max(0, Math.floor(end - created));
  const minutes = Math.floor(total / 60);
  const hours = Math.floor(minutes / 60);
  if (hours) return hours + "h " + String(minutes % 60).padStart(2, "0") + "m";
  if (minutes) return minutes + "m " + String(total % 60).padStart(2, "0") + "s";
  return total + "s";
}

function formatClock(ts) {
  const date = new Date(ts * 1000);
  const pad = (value, width) => String(value).padStart(width, "0");
  return (
    pad(date.getHours(), 2) +
    ":" +
    pad(date.getMinutes(), 2) +
    ":" +
    pad(date.getSeconds(), 2) +
    "." +
    pad(date.getMilliseconds(), 3)
  );
}

function formatDuration(ms) {
  if (ms < 1000) return ms + " ms";
  return (ms / 1000).toFixed(1) + " s";
}

function setPill(state, text) {
  connectionEl.dataset.state = state;
  connectionEl.textContent = text;
}

function noteDisconnect() {
  if (!down) {
    down = true;
    reconnects += 1;
  }
  setPill("reconnecting", "reconnecting (" + reconnects + ")");
}

function noteConnect() {
  if (down) {
    down = false;
    setPill("resumed", "resumed");
    reloadSelected();
    refreshHosts();
    refreshSessions();
    return;
  }
  if (connectionEl.dataset.state !== "resumed") setPill("connected", "connected");
}

function formError(message) {
  if (!message) {
    formErrorEl.hidden = true;
    formErrorEl.textContent = "";
    return;
  }
  formErrorEl.hidden = false;
  formErrorEl.textContent = message;
}

async function errorText(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    if (typeof body.detail === "string") return body.detail;
  } catch (_err) {
    /* response was not JSON */
  }
  return text || response.statusText;
}

function nearBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 48;
}

function addUser(text) {
  const bubble = document.createElement("div");
  bubble.className = "bubble user";
  bubble.textContent = text;
  transcriptEl.appendChild(bubble);
}

function addAssistant(text, draft) {
  const bubble = document.createElement("div");
  bubble.className = draft ? "bubble assistant draft" : "bubble assistant";
  if (draft) bubble.id = "assistant-draft";
  bubble.textContent = text;
  transcriptEl.appendChild(bubble);
  return bubble;
}

function clearWaiting() {
  const waiting = transcriptEl.querySelector(":scope > .empty");
  if (waiting) waiting.remove();
}

function ensureDraft() {
  clearWaiting();
  let bubble = document.getElementById("assistant-draft");
  if (bubble) return bubble;
  bubble = document.createElement("div");
  bubble.id = "assistant-draft";
  bubble.className = "bubble assistant draft";
  const thinking = document.createElement("div");
  thinking.className = "thinking";
  thinking.textContent = "Thinking\u2026";
  const answer = document.createElement("div");
  answer.className = "answer";
  bubble.append(thinking, answer);
  const streaming = transcriptEl.querySelector(".tool-card.streaming");
  if (streaming) transcriptEl.insertBefore(bubble, streaming);
  else transcriptEl.appendChild(bubble);
  return bubble;
}

function openModelCall() {
  let open = null;
  let closedAt = 0;
  for (const event of timeline) {
    if (event.session_id !== selected) continue;
    if (event.type === "llm.started") {
      if (!open || event.seq > open.seq) open = event;
    } else if (event.type === "assistant.message" || event.type === "session.completed" || event.type === "session.failed") {
      closedAt = Math.max(closedAt, event.seq);
    }
  }
  return open && open.seq > closedAt ? open : null;
}

// Grok does not stream its reasoning, so the only live signal before the first
// token is how long ago llm.started was logged. The timeline is the source so a
// reload mid-call, which skips events below floor, still shows the timer.
function renderThinking() {
  const session = sessions.find((item) => item.session_id === selected);
  if (!session || TERMINAL.has(session.status)) return;
  const open = openModelCall();
  if (!open || open.ts == null) return;
  const bubble = ensureDraft();
  const thinking = bubble.querySelector(".thinking");
  if (!thinking || thinking.dataset.live === "1" || thinking.hidden) return;
  const step = open.payload && open.payload.step != null ? " on step " + open.payload.step : "";
  thinking.textContent = "Model thinking" + step + "\u2026 " + formatSpan(open.ts, null);
}

function appendReasoning(text) {
  if (!text) return;
  const stick = nearBottom(transcriptEl);
  const thinking = ensureDraft().querySelector(".thinking");
  thinking.hidden = false;
  if (thinking.dataset.live !== "1") {
    thinking.dataset.live = "1";
    thinking.textContent = "";
    thinking.classList.add("live");
  }
  thinking.textContent += text;
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function appendDelta(text) {
  if (!text) return;
  const stick = nearBottom(transcriptEl);
  const bubble = ensureDraft();
  const thinking = bubble.querySelector(".thinking");
  if (thinking && thinking.dataset.live !== "1") thinking.hidden = true;
  bubble.querySelector(".answer").textContent += text;
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function clearStreamingTools() {
  for (const [key, state] of [...cardsBySlot.entries()]) {
    if (!state.streaming) continue;
    cardsBySlot.delete(key);
    slotArgs.delete(key);
    const node = document.getElementById("tool-slot-" + state.step + "-" + state.call_index);
    if (node) node.remove();
  }
}

function clearDraft() {
  const draft = document.getElementById("assistant-draft");
  if (draft) draft.remove();
  clearStreamingTools();
}

function finalizeAssistant(text) {
  const stick = nearBottom(transcriptEl);
  const bubble = document.getElementById("assistant-draft");
  if (!text) {
    if (bubble) bubble.remove();
    return;
  }
  clearWaiting();
  const node = bubble || addAssistant("", false);
  node.id = "";
  node.className = "bubble assistant";
  node.textContent = text;
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function appendToolDelta(step, index, nameChunk, argChunk) {
  if (step == null || index == null || (!nameChunk && !argChunk)) return;
  const stick = nearBottom(transcriptEl);
  clearWaiting();
  const key = slotKey(step, index);
  let state = cardsBySlot.get(key);
  if (state && !state.streaming) return;
  if (!state) {
    state = upsertCard({
      step: step,
      call_index: index,
      name: nameChunk || "tool",
      args: argChunk || "",
      status: "pending",
      streaming: true,
    });
  } else {
    if (nameChunk) state.name = !state.name || state.name === "tool" ? nameChunk : state.name + nameChunk;
    if (argChunk) state.args += argChunk;
    paintCard(state);
  }
  slotArgs.set(key, { name: state.name, args: state.args });
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function emptyNote(parent, text) {
  const note = document.createElement("p");
  note.className = "empty";
  note.textContent = text;
  parent.appendChild(note);
}

function showFinal(session) {
  const title = finalEl.querySelector("h3");
  const body = finalEl.querySelector("p");
  if (session.status === "completed" && session.final_answer) {
    finalEl.hidden = false;
    title.textContent = "Final answer";
    body.textContent = session.final_answer;
    return;
  }
  if (session.status === "failed" && session.error) {
    finalEl.hidden = false;
    title.textContent = "Failed";
    body.textContent = session.error;
    return;
  }
  finalEl.hidden = true;
  body.textContent = "";
}

function renderHeader(session) {
  const title = document.getElementById("session-title");
  const meta = document.getElementById("session-meta");
  const cancel = document.getElementById("cancel");
  if (!session) {
    title.textContent = "No session";
    meta.textContent = "";
    cancel.disabled = true;
    document.getElementById("live-code").disabled = true;
    document.getElementById("commit").disabled = true;
    setPageLink("trajectory", "");
    setPageLink("timeline-open", "");
    showFinal({ status: "" });
    return;
  }
  title.textContent = session.title || session.session_id;
  const chip = hostChip(session.host_id, session.host_generation);
  const bits = [session.status];
  if (chip) bits.push(chip);
  bits.push("step " + session.step + " / " + session.max_steps);
  meta.textContent = bits.join(" · ") + " · ";
  const elapsed = document.createElement("span");
  elapsed.dataset.created = String(session.created_at);
  elapsed.dataset.updated = String(session.updated_at);
  elapsed.dataset.terminal = TERMINAL.has(session.status) ? "1" : "0";
  elapsed.textContent = formatElapsed(session.created_at, session.updated_at, TERMINAL.has(session.status));
  meta.appendChild(elapsed);
  cancel.disabled = TERMINAL.has(session.status);
  document.getElementById("live-code").disabled = false;
  document.getElementById("commit").disabled = false;
  setPageLink("trajectory", trajectoryUrl(session.session_id));
  setPageLink("timeline-open", timelineUrl(session.session_id));
  showFinal(session);
}

function renderSessionList() {
  sessionsEl.replaceChildren();
  if (!sessions.length) {
    emptyNote(sessionsEl, "No tasks yet.");
    renderHosts();
    return;
  }
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item" + (session.session_id === selected ? " selected" : "");
    button.dataset.session = session.session_id;
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = session.title || session.session_id;
    const row = document.createElement("span");
    row.className = "row";
    const badge = document.createElement("span");
    badge.className = "badge " + session.status;
    badge.textContent = session.status;
    row.appendChild(badge);
    const chip = hostChip(session.host_id, session.host_generation);
    if (chip) row.appendChild(document.createTextNode(" · " + chip));
    row.appendChild(document.createTextNode(" · step " + session.step + " / " + session.max_steps));
    const elapsed = document.createElement("span");
    elapsed.dataset.created = String(session.created_at);
    elapsed.dataset.updated = String(session.updated_at);
    elapsed.dataset.terminal = TERMINAL.has(session.status) ? "1" : "0";
    elapsed.textContent = formatElapsed(session.created_at, session.updated_at, TERMINAL.has(session.status));
    row.appendChild(elapsed);
    button.append(title, row);
    const wrap = document.createElement("div");
    wrap.className = "session-row";
    wrap.appendChild(button);
    if (!TERMINAL.has(session.status)) {
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "stop-task";
      stop.dataset.cancel = session.session_id;
      stop.title = "Cancel";
      stop.setAttribute("aria-label", "Cancel " + (session.title || session.session_id));
      stop.textContent = "■";
      wrap.appendChild(stop);
    }
    sessionsEl.appendChild(wrap);
  }
  renderHosts();
}

function cardFromRow(call) {
  let result = "";
  if (call.result_json) {
    try {
      const parsed = JSON.parse(call.result_json);
      result = parsed && typeof parsed.text === "string" ? parsed.text : call.result_json;
    } catch (_err) {
      result = call.result_json;
    }
  }
  if (call.error) result = call.error;
  let duration = null;
  if (call.started_at && call.finished_at) duration = Math.round((call.finished_at - call.started_at) * 1000);
  return {
    op_id: call.op_id,
    step: call.step,
    call_index: call.call_index,
    name: call.tool_name,
    args: call.args_json,
    status: call.status,
    attempts: call.attempts,
    host_id: call.host_id || "",
    host_generation: call.host_generation,
    replayed: Boolean(call.replayed),
    duration_ms: duration,
    result: result,
  };
}

function mergeCard(state, partial) {
  if (partial.op_id) state.op_id = partial.op_id;
  if (partial.step != null) state.step = partial.step;
  if (partial.call_index != null) state.call_index = partial.call_index;
  if (partial.name) state.name = partial.name;
  if (partial.args) state.args = partial.args;
  if (partial.status) state.status = partial.status;
  if (typeof partial.attempts === "number") state.attempts = partial.attempts;
  if (partial.host_id) state.host_id = partial.host_id;
  if (typeof partial.host_generation === "number") state.host_generation = partial.host_generation;
  if (typeof partial.replayed === "boolean") state.replayed = partial.replayed;
  if (typeof partial.duration_ms === "number") state.duration_ms = partial.duration_ms;
  if (typeof partial.result === "string" && partial.result) state.result = partial.result;
  if (typeof partial.streaming === "boolean") state.streaming = partial.streaming;
}

function paintCard(state) {
  const stick = nearBottom(transcriptEl);
  clearWaiting();
  let node = state.op_id ? document.getElementById("tool-" + state.op_id) : null;
  if (!node && state.step != null) node = document.getElementById("tool-slot-" + state.step + "-" + state.call_index);
  if (!node) {
    node = document.createElement("article");
    node.className = "tool-card";
    node.append(
      Object.assign(document.createElement("div"), { className: "tool-top" }),
      Object.assign(document.createElement("div"), { className: "tool-args" }),
      Object.assign(document.createElement("div"), { className: "tool-meta" }),
    );
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Result";
    const pre = document.createElement("pre");
    details.append(summary, pre);
    node.appendChild(details);
    const top = node.querySelector(".tool-top");
    top.append(document.createElement("span"), document.createElement("span"));
    top.children[0].className = "tool-name";
    top.children[1].className = "tool-status";
    transcriptEl.appendChild(node);
  }
  node.id = state.op_id ? "tool-" + state.op_id : "tool-slot-" + state.step + "-" + state.call_index;
  node.dataset.status = state.status || "pending";
  node.classList.toggle("streaming", Boolean(state.streaming));
  node.querySelector(".tool-name").textContent = state.name || "tool";
  node.querySelector(".tool-status").textContent = STATUS_ICON[state.status] || "○";
  node.querySelector(".tool-args").textContent = state.streaming ? streamingArgs(state.args) : shortArgs(state.name, state.args);
  const meta = node.querySelector(".tool-meta");
  meta.replaceChildren();
  const bits = [];
  if (state.attempts) bits.push("attempts " + state.attempts);
  const chip = hostChip(state.host_id, state.host_generation);
  if (chip) bits.push(chip);
  if (typeof state.duration_ms === "number") bits.push(formatDuration(state.duration_ms));
  bits.forEach((text, index) => {
    if (index) meta.appendChild(document.createTextNode(" · "));
    meta.appendChild(document.createTextNode(text));
  });
  if (state.replayed) {
    if (bits.length) meta.appendChild(document.createTextNode(" "));
    const badge = document.createElement("span");
    badge.className = "badge replayed";
    badge.textContent = "replayed";
    meta.appendChild(badge);
  }
  const details = node.querySelector("details");
  if (state.result) {
    details.hidden = false;
    setHighlighted(details.querySelector("pre"), state.result);
  } else {
    details.hidden = true;
  }
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function upsertCard(partial) {
  let state = null;
  if (partial.op_id && cards.has(partial.op_id)) state = cards.get(partial.op_id);
  if (!state && partial.step != null && partial.call_index != null) {
    state = cardsBySlot.get(slotKey(partial.step, partial.call_index)) || null;
  }
  if (!state) {
    state = {
      op_id: "",
      step: null,
      call_index: null,
      name: "tool",
      args: "",
      status: "pending",
      attempts: 0,
      host_id: "",
      host_generation: null,
      replayed: false,
      duration_ms: null,
      result: "",
    };
  }
  mergeCard(state, partial);
  if (state.op_id) cards.set(state.op_id, state);
  if (state.step != null && state.call_index != null) cardsBySlot.set(slotKey(state.step, state.call_index), state);
  paintCard(state);
  return state;
}

function rebuildChanged(calls) {
  changed.clear();
  counted.clear();
  for (const call of calls) {
    if (call.status !== "succeeded" || !MUTATING.has(call.tool_name)) continue;
    const path = argPath(call.args_json);
    if (!path) continue;
    changed.set(path, (changed.get(path) || 0) + 1);
    counted.add(call.op_id);
  }
  renderChanged();
}

function noteChanged(state) {
  if (!state || state.status !== "succeeded" || !MUTATING.has(state.name)) return;
  if (!state.op_id || counted.has(state.op_id)) return;
  const path = argPath(state.args);
  if (!path) return;
  counted.add(state.op_id);
  changed.set(path, (changed.get(path) || 0) + 1);
  renderChanged();
}

function renderChanged() {
  changedEl.replaceChildren();
  if (!changed.size) {
    emptyNote(changedEl, "No files changed.");
    return;
  }
  const rows = Array.from(changed.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  for (const [path, count] of rows) {
    const row = document.createElement("div");
    row.className = "file-row";
    const name = document.createElement("span");
    name.textContent = path;
    const tally = document.createElement("span");
    tally.className = "count";
    tally.textContent = String(count);
    row.append(name, tally);
    changedEl.appendChild(row);
  }
}

function rememberTool(opId, partial) {
  if (!opId) return;
  const current = toolDetails.get(opId) || { name: "", args: "", result: "", status: "" };
  if (partial.name) current.name = partial.name;
  if (partial.args) current.args = partial.args;
  if (typeof partial.result === "string" && (partial.status === "failed" || partial.result.length >= current.result.length)) {
    current.result = partial.result;
  }
  if (partial.status) current.status = partial.status;
  toolDetails.set(opId, current);
}

function rememberCall(call) {
  let result = "";
  if (call.result_json) {
    try {
      const parsed = JSON.parse(call.result_json);
      result = parsed && typeof parsed.text === "string" ? parsed.text : "";
    } catch (_err) {
      result = "";
    }
  }
  if (call.error) result = call.error;
  rememberTool(call.op_id, {
    name: call.tool_name,
    args: call.args_json,
    result: result,
    status: call.status,
  });
}

function toolLabel(event) {
  if (!event.type.startsWith("tool.")) return event.type;
  const detail = event.op_id ? toolDetails.get(event.op_id) : null;
  const name = (detail && detail.name) || (event.payload && event.payload.name) || "tool";
  return name + " · " + event.type.slice("tool.".length);
}

function timelineMark(event) {
  if (event.type === "host.down") return "stop";
  if (event.type === "host.replacing" || event.type === "host.replaced") return "restart";
  if (event.type === "session.migrated") return "migrate";
  return "";
}

function timelineText(event) {
  if (event.type !== "session.migrated") return toolLabel(event);
  const payload = event.payload || {};
  const generation = payload.generation ? " g" + payload.generation : "";
  return (payload.from || "?") + " → " + (payload.to || "?") + generation;
}

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlightPython(source) {
  const re = /(#.*)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(\b(?:def|class|return|if|elif|else|for|while|import|from|as|try|except|finally|with|yield|lambda|pass|break|continue|in|not|and|or|is|None|True|False|raise|async|await)\b)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*(?=\s*\())|([\s\S])/g;
  let out = "";
  let match;
  while ((match = re.exec(source))) {
    if (match[1]) out += '<span class="tok-comment">' + escapeHtml(match[1]) + "</span>";
    else if (match[2]) out += '<span class="tok-string">' + escapeHtml(match[2]) + "</span>";
    else if (match[3]) out += '<span class="tok-keyword">' + escapeHtml(match[3]) + "</span>";
    else if (match[4]) out += '<span class="tok-number">' + escapeHtml(match[4]) + "</span>";
    else if (match[5]) out += '<span class="tok-fn">' + escapeHtml(match[5]) + "</span>";
    else out += escapeHtml(match[6] || "");
  }
  return out;
}

function highlightJson(source) {
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+(?:\.\d+)?\b)|(\btrue\b|\bfalse\b|\bnull\b)|([\s\S])/g;
  let out = "";
  let match;
  while ((match = re.exec(source))) {
    if (match[1] && match[2]) out += '<span class="tok-key">' + escapeHtml(match[1]) + "</span>" + match[2];
    else if (match[1]) out += '<span class="tok-string">' + escapeHtml(match[1]) + "</span>";
    else if (match[3]) out += '<span class="tok-number">' + escapeHtml(match[3]) + "</span>";
    else if (match[4]) out += '<span class="tok-keyword">' + escapeHtml(match[4]) + "</span>";
    else out += escapeHtml(match[5] || "");
  }
  return out;
}

function highlightCode(text) {
  const trimmed = text.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      JSON.parse(trimmed);
      return highlightJson(text);
    } catch (_err) {
      /* not JSON */
    }
  }
  if (/\b(?:def|class|import|from|return)\b/.test(text)) return highlightPython(text);
  return escapeHtml(text);
}

function setHighlighted(node, text) {
  node.innerHTML = highlightCode(text || "");
}

function prettyArgs(raw) {
  const parsed = parsedArgs(raw);
  if (!raw) return "";
  if (!Object.keys(parsed).length) return String(raw);
  return JSON.stringify(parsed, null, 2);
}

function addRequest(parent, toolName, raw) {
  const args = parsedArgs(raw);
  if (toolName === "write_file" && typeof args.content === "string") {
    addBlock(parent, args.path || "file", args.content, "request");
    return;
  }
  if (toolName === "replace_in_file") {
    if (args.path) {
      const path = document.createElement("p");
      path.className = "traj-path";
      path.textContent = args.path;
      parent.appendChild(path);
    }
    if (typeof args.old === "string") addBlock(parent, "Replace", args.old, "request");
    if (typeof args.new === "string") addBlock(parent, "With", args.new, "response");
    return;
  }
  if (toolName === "shell" && typeof args.command === "string") {
    if (typeof args.cwd === "string" && args.cwd && args.cwd !== ".") {
      const cwd = document.createElement("p");
      cwd.className = "traj-path";
      cwd.textContent = args.cwd;
      parent.appendChild(cwd);
    }
    addBlock(parent, "Command", args.command, "request");
    return;
  }
  if (typeof args.path === "string" && typeof args.content !== "string") {
    const lines = [args.path];
    if (args.start_line) lines.push("lines " + args.start_line + (args.end_line ? "–" + args.end_line : ""));
    if (args.pattern) lines.push(String(args.pattern));
    if (args.glob) lines.push(String(args.glob));
    addBlock(parent, "Request", lines.join("\n"), "request");
    return;
  }
  addBlock(parent, "Request", prettyArgs(raw), "request");
}

function renderTimelineDetail() {
  const panel = document.getElementById("timeline-detail");
  const event = timeline.find((item) => item.seq === openTimelineSeq);
  if (!event || !event.op_id || !event.type.startsWith("tool.")) {
    panel.hidden = true;
    return;
  }
  const detail = toolDetails.get(event.op_id) || { name: "", args: "", result: "", status: "" };
  panel.hidden = false;
  document.getElementById("timeline-detail-title").textContent = toolLabel(event);
  const request = document.getElementById("timeline-detail-request");
  const response = document.getElementById("timeline-detail-response");
  request.replaceChildren();
  response.replaceChildren();
  addRequest(request, detail.name, detail.args);
  if (!request.childElementCount) request.textContent = "(no request yet)";
  setHighlighted(response, detail.result || "(no response yet)");
}

async function openTimelineEvent(event) {
  openTimelineSeq = openTimelineSeq === event.seq ? 0 : event.seq;
  renderTimeline();
  renderTimelineDetail();
  if (!openTimelineSeq || !event.op_id || !selected) return;
  const response = await fetch("/api/sessions/" + encodeURIComponent(selected));
  if (!response.ok || openTimelineSeq !== event.seq) return;
  const body = await response.json();
  const call = (body.tool_calls || []).find((item) => item.op_id === event.op_id);
  if (call) rememberCall(call);
  if (openTimelineSeq === event.seq) renderTimelineDetail();
}

function renderSnapshot(detail) {
  cards.clear();
  cardsBySlot.clear();
  toolDetails.clear();
  slotArgs.clear();
  openTimelineSeq = 0;
  sessionMessages = detail.messages || [];
  liveText = "";
  liveReasoning = "";
  for (const call of detail.tool_calls || []) rememberCall(call);
  transcriptEl.replaceChildren();
  const byStep = new Map();
  for (const call of detail.tool_calls) {
    const list = byStep.get(call.step) || [];
    list.push(call);
    byStep.set(call.step, list);
  }
  let sawUser = false;
  for (const message of detail.messages) {
    if (message.role === "user") {
      addUser(message.content || "");
      sawUser = true;
    } else if (message.role === "assistant") {
      if (message.content) addAssistant(message.content, false);
      for (const call of byStep.get(message.step) || []) upsertCard(cardFromRow(call));
      byStep.delete(message.step);
    }
  }
  for (const list of byStep.values()) {
    for (const call of list) upsertCard(cardFromRow(call));
  }
  if (!sawUser && detail.session.prompt) addUser(detail.session.prompt);
  const hasAssistant = detail.messages.some((message) => message.role === "assistant");
  if (!transcriptEl.childElementCount) emptyNote(transcriptEl, "Waiting for the agent.");
  else if (!hasAssistant && detail.session && !TERMINAL.has(detail.session.status)) {
    emptyNote(transcriptEl, "Waiting for the agent.");
  }
  rebuildChanged(detail.tool_calls);
  renderHeader(detail.session);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function timelineVisible(event) {
  if (toolOnly && !event.type.startsWith("tool.")) return false;
  if (!event.session_id) return true;
  if (event.type === "session.migrated") return !selected || event.session_id === selected;
  return event.session_id === selected;
}

function renderTimeline() {
  timelineEl.replaceChildren();
  const rows = timeline.filter(timelineVisible).sort((a, b) => a.seq - b.seq);
  if (!rows.length) {
    emptyNote(timelineEl, "No events yet.");
    return;
  }
  const stick = nearBottom(timelineEl);
  for (const event of rows) {
    const isTool = event.type.startsWith("tool.") && event.op_id;
    const mark = timelineMark(event);
    const row = document.createElement(isTool ? "button" : "div");
    if (isTool) row.type = "button";
    row.className = "tl-row" + (isTool ? " tool" : "") + (mark ? " mark-" + mark : "") + (event.seq === openTimelineSeq ? " open" : "");
    const main = document.createElement("div");
    main.className = "tl-main";
    const time = document.createElement("span");
    time.className = "tl-time";
    time.textContent = formatClock(event.ts);
    const type = document.createElement("span");
    type.className = "type";
    type.textContent = timelineText(event);
    main.appendChild(time);
    if (mark) {
      const badge = document.createElement("span");
      badge.className = "tl-mark " + mark;
      badge.textContent = mark;
      main.appendChild(badge);
    }
    main.appendChild(type);
    const extra = [event.host_id].filter(Boolean).join("  ");
    if (extra) {
      const line = document.createElement("span");
      line.className = "tl-extra";
      line.textContent = extra;
      main.appendChild(line);
    }
    row.appendChild(main);
    if (isTool) {
      row.addEventListener("click", () => openTimelineEvent(event));
    }
    timelineEl.appendChild(row);
  }
  if (stick) timelineEl.scrollTop = timelineEl.scrollHeight;
  renderTimelineDetail();
}

function rememberTimeline(event) {
  if (!event.seq || timeline.some((item) => item.seq === event.seq)) return;
  timeline.push(event);
  renderTimeline();
}

function bannerText(event) {
  const payload = event.payload || {};
  if (event.type === "session.migrated") {
    const generation = payload.generation ? " g" + payload.generation : "";
    return "migrated " + (payload.from || "?") + " → " + (payload.to || "?") + generation;
  }
  if (event.type === "session.claimed" && payload.resumed) return "session resumed";
  return "";
}

function bannerVisible(event) {
  if (!selected) return false;
  return event.session_id === selected;
}

function renderBanners() {
  bannersEl.replaceChildren();
  for (const event of bannerEvents.filter(bannerVisible).sort((a, b) => a.seq - b.seq)) {
    const text = bannerText(event);
    if (!text) continue;
    const row = document.createElement("div");
    row.className = "banner";
    row.textContent = text;
    bannersEl.appendChild(row);
  }
}

function rememberBanner(event) {
  const text = bannerText(event);
  if (!text || bannerEvents.some((item) => item.seq === event.seq)) return;
  bannerEvents.push(event);
  renderBanners();
}

function patchSession(event) {
  if (!event.session_id) return;
  const row = sessions.find((session) => session.session_id === event.session_id);
  if (!row) return;
  const payload = event.payload || {};
  if (event.type === "session.status") {
    if (payload.to) row.status = payload.to;
    if (payload.host_id) row.host_id = payload.host_id;
    if (payload.generation) row.host_generation = payload.generation;
  } else if (event.type === "session.migrated") {
    if (payload.to) row.host_id = payload.to;
    if (payload.generation) row.host_generation = payload.generation;
    row.status = "running";
  } else if (event.type === "session.completed") {
    row.status = "completed";
    if (payload.final_answer) row.final_answer = payload.final_answer;
  } else if (event.type === "session.failed") {
    row.status = "failed";
    if (payload.error) row.error = payload.error;
  } else {
    return;
  }
  renderSessionList();
  if (row.session_id === selected) renderHeader(row);
}

function applyTranscript(event) {
  const payload = event.payload || {};
  if (event.type === "llm.started") {
    const stick = nearBottom(transcriptEl);
    ensureDraft();
    renderThinking();
    if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return;
  }
  if (event.type === "assistant.message") {
    finalizeAssistant(payload.content || "");
    (payload.tool_calls || []).forEach((call, index) => {
      const fn = call.function || {};
      upsertCard({
        step: payload.step,
        call_index: index,
        name: fn.name || "tool",
        args: fn.arguments || "",
        status: "pending",
        streaming: false,
      });
      slotArgs.set(slotKey(payload.step, index), { name: fn.name || "tool", args: fn.arguments || "" });
      if (fn.name === "write_file") noteCode(fn.name, fn.arguments || "", "pending");
    });
    const waiting = transcriptEl.querySelector(".empty");
    if (waiting) waiting.remove();
    liveText = "";
    liveReasoning = "";
    return;
  }
  if (event.type === "tool.pending") {
    upsertCard({
      op_id: event.op_id,
      step: payload.step,
      call_index: payload.call_index,
      name: payload.name,
      status: "pending",
      streaming: false,
    });
    const staged = slotArgs.get(slotKey(payload.step, payload.call_index)) || {};
    rememberTool(event.op_id, {
      name: payload.name || staged.name,
      args: staged.args || "",
      status: "pending",
    });
    return;
  }
  if (event.type === "tool.started") {
    upsertCard({
      op_id: event.op_id,
      status: "running",
      attempts: payload.attempt,
      host_id: payload.host_id || event.host_id,
      host_generation: payload.generation,
    });
    return;
  }
  if (event.type === "tool.succeeded") {
    const state = upsertCard({
      op_id: event.op_id,
      status: "succeeded",
      replayed: Boolean(payload.replayed),
      duration_ms: payload.duration_ms,
      result: payload.summary || "",
    });
    noteChanged(state);
    noteCode(state.name, state.args, "succeeded");
    if (state.name === "shell") refreshCodeFiles();
    rememberTool(event.op_id, {
      name: state.name,
      args: state.args,
      status: "succeeded",
      result: payload.summary || "",
    });
    return;
  }
  if (event.type === "tool.failed") {
    const state = upsertCard({
      op_id: event.op_id,
      status: "failed",
      result: payload.error || "",
    });
    noteCode(state.name, state.args, "failed");
    rememberTool(event.op_id, {
      name: state.name,
      args: state.args,
      status: "failed",
      result: payload.error || state.result || "",
    });
    return;
  }
  if (event.type === "tool.retrying") {
    upsertCard({
      op_id: event.op_id,
      status: "retrying",
      result: payload.reason ? "retrying: " + payload.reason : "retrying",
    });
  }
}

function apply(event) {
  if (!event || !event.type) return;
  event.payload = event.payload || {};
  if (event.type === "assistant.delta" || event.type === "assistant.reasoning" || event.type === "assistant.tool") {
    if (event.session_id === selected) {
      if (event.type === "assistant.delta") {
        const text = event.payload.text || "";
        appendDelta(text);
        liveText += text;
      } else if (event.type === "assistant.reasoning") {
        const text = event.payload.text || "";
        appendReasoning(text);
        liveReasoning += text;
      } else {
        appendToolDelta(event.payload.step, event.payload.index, event.payload.name || "", event.payload.arguments || "");
      }
      if (trajectoryOpen) renderTrajectory();
    }
    return;
  }
  if (!event.seq || applied.has(event.seq)) return;
  applied.add(event.seq);
  rememberTimeline(event);
  rememberBanner(event);
  patchSession(event);
  if (event.type.startsWith("host.")) scheduleHostRefresh();
  if (event.session_id && event.session_id === selected && (event.type.startsWith("session.") || event.type.startsWith("tool.") || event.type === "assistant.message")) {
    scheduleSessionRefresh();
  }
  if (!selected || (event.session_id && event.session_id !== selected)) return;
  if (event.type === "llm.retry" && event.seq > floor) {
    liveText = "";
    liveReasoning = "";
    clearDraft();
    if (!transcriptEl.querySelector(":scope > .empty")) emptyNote(transcriptEl, "Waiting for the agent.");
  }
  if (trajectoryOpen) renderTrajectory();
  if (event.seq <= floor) return;
  applyTranscript(event);
}

function listen(source, options) {
  const includeDeltas = !options || options.deltas !== false;
  for (const type of EVENT_TYPES) {
    if ((type === "assistant.delta" || type === "assistant.reasoning" || type === "assistant.tool") && !includeDeltas) continue;
    source.addEventListener(type, (message) => {
      try {
        apply(JSON.parse(message.data));
      } catch (_err) {
        /* ignore a malformed frame */
      }
    });
  }
}

function closeSessionStream() {
  const source = sessionSource;
  sessionSource = null;
  if (source) source.close();
}

function openSessionStream(id) {
  const source = new EventSource("/api/sessions/" + encodeURIComponent(id) + "/events?after=0");
  sessionSource = source;
  source.onopen = () => {
    if (sessionSource === source) noteConnect();
  };
  source.onerror = () => {
    if (sessionSource === source) noteDisconnect();
  };
  listen(source);
}

function openGlobalStream() {
  const source = new EventSource("/api/events?after=0");
  source.onopen = () => noteConnect();
  source.onerror = () => noteDisconnect();
  listen(source, { deltas: false });
}

async function fetchSessions() {
  const response = await fetch("/api/sessions");
  if (!response.ok) return;
  const body = await response.json();
  sessions = body.sessions || [];
  renderSessionList();
  const current = sessions.find((session) => session.session_id === selected);
  if (current) renderHeader(current);
}

function refreshSessions() {
  sessionsChain = sessionsChain.then(fetchSessions).catch(() => {});
  return sessionsChain;
}

function renderHosts() {
  hostsEl.replaceChildren();
  if (!hosts.length) {
    emptyNote(hostsEl, "No hosts yet.");
    return;
  }
  for (const host of hosts) {
    const assigned = sessions.filter((session) => {
      if (session.host_id !== host.host_id) return false;
      if (session.status === "running" || session.status === "waiting_host") return true;
      return session.session_id === selected;
    });
    const runsSelected = assigned.some((session) => session.session_id === selected);
    const card = document.createElement("article");
    card.className = "host-card" + (runsSelected ? " running-selected" : assigned.length ? " busy" : "");
    const top = document.createElement("div");
    top.className = "host-top";
    const title = document.createElement("div");
    title.className = "host-title";
    const dot = document.createElement("span");
    dot.className = "dot " + (host.status || "unknown");
    const name = document.createElement("span");
    name.textContent = host.host_id;
    title.append(dot, name);
    const generation = document.createElement("span");
    generation.textContent = "g" + host.generation;
    top.append(title, generation);
    const meta = document.createElement("div");
    meta.className = "host-meta";
    const boot = host.boot_id ? String(host.boot_id).slice(0, 8) : "—";
    meta.textContent = "boot " + boot + " · :" + host.port + " · " + (host.active_sessions || 0) + " active · ";
    const age = document.createElement("span");
    age.dataset.heartbeat = host.last_heartbeat_at ? String(host.last_heartbeat_at) : "";
    age.textContent = host.last_heartbeat_at ? "heartbeat" : "no heartbeat";
    meta.appendChild(age);
    const kill = document.createElement("button");
    kill.type = "button";
    kill.className = "danger";
    kill.dataset.kill = host.host_id;
    kill.textContent = "Kill host";
    card.append(top, meta);
    if (assigned.length) {
      const list = document.createElement("div");
      list.className = "host-sessions";
      for (const session of assigned) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "host-session" + (session.session_id === selected ? " selected" : "");
        row.dataset.session = session.session_id;
        const label = session.title || session.session_id;
        row.textContent = label;
        row.title = label;
        list.appendChild(row);
      }
      card.appendChild(list);
    } else {
      const idle = document.createElement("div");
      idle.className = "host-idle";
      idle.textContent = "No session";
      card.appendChild(idle);
    }
    card.appendChild(kill);
    hostsEl.appendChild(card);
  }
  renderHeartbeatAges();
}

async function fetchHosts() {
  const response = await fetch("/api/hosts");
  if (!response.ok) return;
  const body = await response.json();
  hosts = body.hosts || [];
  renderHosts();
}

function refreshHosts() {
  hostsChain = hostsChain.then(fetchHosts).catch(() => {});
  return hostsChain;
}

let hostTimer = null;
function scheduleHostRefresh() {
  if (hostTimer) return;
  hostTimer = setTimeout(() => {
    hostTimer = null;
    refreshHosts();
  }, 50);
}

let sessionTimer = null;
function scheduleSessionRefresh() {
  if (sessionTimer) return;
  sessionTimer = setTimeout(() => {
    sessionTimer = null;
    refreshSessions();
  }, 200);
}

function renderHeartbeatAges() {
  document.querySelectorAll("[data-heartbeat]").forEach((node) => {
    if (!node.dataset.heartbeat) {
      node.textContent = "no heartbeat";
      return;
    }
    const age = Math.max(0, Date.now() / 1000 - Number(node.dataset.heartbeat));
    node.textContent = "heartbeat " + (age < 10 ? age.toFixed(1) + "s" : Math.round(age) + "s");
  });
}

function renderElapsed() {
  document.querySelectorAll("[data-created]").forEach((node) => {
    node.textContent = formatElapsed(
      Number(node.dataset.created),
      Number(node.dataset.updated || node.dataset.created),
      node.dataset.terminal === "1",
    );
  });
}

async function pollSupervisor() {
  const node = document.getElementById("supervisor");
  try {
    const response = await fetch(supervisorUrl("/status"));
    if (!response.ok) throw new Error("status " + response.status);
    const body = await response.json();
    node.textContent = "pid " + (body.pid == null ? "—" : body.pid) + " · restarts " + body.restart_count;
  } catch (_err) {
    node.textContent = "supervisor unreachable";
  }
}

let loadedImport = null;
let liveOn = true;
let openFile = "";
let codeListToken = 0;
const codeFiles = new Map();
const unresolvedFiles = new Set();

function setFolderLabel() {
  const node = document.getElementById("folder-label");
  const clear = document.getElementById("clear-folder");
  if (!loadedImport) {
    node.firstChild.textContent = "Workspace: empty ";
    clear.hidden = true;
    return;
  }
  const count = loadedImport.files.length;
  node.firstChild.textContent = "Workspace: " + loadedImport.name + " (" + count + (count === 1 ? " file) " : " files) ");
  clear.hidden = false;
}

async function chooseFolder(fileList) {
  const skip = new Set([".git", "node_modules", "__pycache__", ".venv", "dist", ".next"]);
  const files = [];
  let name = "folder";
  let total = 0;
  for (const file of fileList) {
    const relative = file.webkitRelativePath || file.name;
    const parts = relative.split("/");
    if (parts.length > 1) name = parts[0];
    if (parts.some((part) => skip.has(part))) continue;
    const path = parts.slice(parts[0] === name && parts.length > 1 ? 1 : 0).join("/");
    if (!path || file.size > 256 * 1024) continue;
    const content = await file.text();
    if (content.includes("\u0000")) continue;
    total += content.length;
    if (total > 2 * 1024 * 1024 || files.length >= 200) break;
    files.push({ path: path, content: content });
  }
  if (!files.length) {
    formError("That folder has no text files to load.");
    return;
  }
  formError("");
  const response = await fetch("/api/imports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name, files: files }),
  });
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const saved = await response.json();
  loadedImport = { id: saved.import_id, name: saved.name, files: files };
  setFolderLabel();
  clearSampleSelection();
}

function noteCode(name, args, phase, options) {
  if (!MUTATING.has(name)) return;
  const parsed = parsedArgs(args);
  const path = typeof parsed.path === "string" ? parsed.path : "";
  if (!path) return;
  const allowFetch = !options || options.fetch !== false;
  const current = codeFiles.get(path) || { text: "", status: "" };
  if (name === "delete_path") {
    if (phase !== "succeeded") return;
    codeFiles.set(path, { text: "", status: "deleted" });
  } else if (name === "replace_in_file") {
    if (phase !== "succeeded") return;
    const oldText = typeof parsed.old === "string" ? parsed.old : "";
    const newText = typeof parsed.new === "string" ? parsed.new : "";
    if (oldText && current.text.includes(oldText)) {
      codeFiles.set(path, { text: current.text.replace(oldText, newText), status: "updated" });
    } else if (allowFetch) {
      refreshWorkspaceFile(path);
      return;
    } else {
      unresolvedFiles.add(path);
      return;
    }
  } else if (name === "write_file") {
    const content = typeof parsed.content === "string" ? parsed.content : "";
    if (phase === "failed") {
      codeFiles.set(path, { text: current.previous == null ? "" : current.previous, status: "failed" });
    } else {
      codeFiles.set(path, {
        text: content,
        status: phase === "succeeded" ? "updated" : "writing",
        previous: phase === "pending" ? current.text : current.previous,
      });
    }
  }
  openFile = path;
  if (liveOn) renderCode();
}

async function refreshWorkspaceFile(path) {
  if (!selected) return;
  const response = await fetch(
    "/api/sessions/" + encodeURIComponent(selected) + "/file?path=" + encodeURIComponent(path),
  );
  if (!response.ok) return;
  const body = await response.json();
  codeFiles.set(path, { text: body.content || "", status: "updated" });
  openFile = path;
  if (liveOn) renderCode();
}

function writeFileStillOwns(path) {
  let open = false;
  let done = false;
  const consider = (state) => {
    if (!state || state.name !== "write_file" || argPath(state.args) !== path) return;
    if (state.status === "succeeded" || state.status === "failed") done = true;
    else open = true;
  };
  for (const state of cardsBySlot.values()) consider(state);
  for (const state of cards.values()) consider(state);
  return open || !done;
}

async function refreshCodeFiles() {
  const sessionId = selected;
  if (!sessionId) return;
  const token = ++codeListToken;
  let response;
  try {
    response = await fetch("/api/sessions/" + encodeURIComponent(sessionId) + "/files");
  } catch (_err) {
    return;
  }
  if (!response.ok || token !== codeListToken || selected !== sessionId) return;
  let body;
  try {
    body = await response.json();
  } catch (_err) {
    return;
  }
  if (token !== codeListToken || selected !== sessionId) return;
  for (const file of body.files || []) {
    if (!file || typeof file.path !== "string" || !file.path) continue;
    const content = file.content || "";
    const current = codeFiles.get(file.path);
    if (current && current.status === "writing" && writeFileStillOwns(file.path)) continue;
    if (current && current.text === content) continue;
    codeFiles.set(file.path, { text: content, status: "updated" });
  }
  if (liveOn && token === codeListToken && selected === sessionId) renderCode();
}

async function hydrateCode(detail) {
  codeFiles.clear();
  unresolvedFiles.clear();
  openFile = "";
  const listed = await fetch("/api/sessions/" + encodeURIComponent(detail.session.session_id) + "/files");
  if (listed.ok) {
    const body = await listed.json();
    for (const file of body.files || []) codeFiles.set(file.path, { text: file.content || "", status: "" });
  }
  if (!codeFiles.size && detail.session.import_id) {
    const response = await fetch("/api/imports/" + encodeURIComponent(detail.session.import_id));
    if (response.ok) {
      const body = await response.json();
      for (const file of body.files || []) codeFiles.set(file.path, { text: file.content || "", status: "" });
    }
  }
  const calls = (detail.tool_calls || []).slice().sort((a, b) => a.step - b.step || a.call_index - b.call_index);
  for (const call of calls) {
    if (call.status !== "succeeded" || !MUTATING.has(call.tool_name)) continue;
    const path = argPath(call.args_json);
    if (path && codeFiles.has(path)) codeFiles.get(path).status = "updated";
    else noteCode(call.tool_name, call.args_json, "succeeded", { fetch: false });
  }
  for (const path of unresolvedFiles) await refreshWorkspaceFile(path);
  if (!openFile && codeFiles.size) openFile = Array.from(codeFiles.keys()).sort()[0];
  if (liveOn) renderCode();
}

function renderCode() {
  const list = document.getElementById("code-files");
  const view = document.getElementById("code-view");
  const status = document.getElementById("code-status");
  list.replaceChildren();
  const paths = Array.from(codeFiles.keys()).sort();
  if (!paths.length) {
    status.textContent = "No files yet";
    view.textContent = "This workspace is empty. Files show up here as the agent writes them.";
    return;
  }
  if (!openFile || !codeFiles.has(openFile)) openFile = paths[0];
  for (const path of paths) {
    const file = codeFiles.get(path);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = path;
    button.className = (path === openFile ? "selected " : "") + (file.status || "");
    button.addEventListener("click", () => {
      openFile = path;
      renderCode();
    });
    list.appendChild(button);
  }
  const file = codeFiles.get(openFile);
  status.textContent = openFile + (file.status ? " · " + file.status : "");
  setHighlighted(view, file.status === "deleted" ? "" : file.text);
}

function formatSpan(start, end) {
  if (start == null) return "";
  const stop = end == null ? Date.now() / 1000 : end;
  return formatDuration(Math.max(0, Math.round((stop - start) * 1000)));
}

function contextMessages(step) {
  const assistant = sessionMessages
    .filter((message) => message.role === "assistant" && message.step === step)
    .sort((a, b) => a.idx - b.idx)[0];
  const prior = assistant ? sessionMessages.filter((message) => message.idx < assistant.idx) : sessionMessages;
  return prior.map((message) => ({ role: message.role, content: message.content || "" }));
}

function buildTrajectory() {
  const events = timeline.filter((event) => event.session_id === selected).sort((a, b) => a.seq - b.seq);
  const steps = [];
  const byStep = new Map();
  const tools = new Map();
  let current = 0;

  function bucket(step) {
    const key = step == null ? current : step;
    current = key;
    if (!byStep.has(key)) {
      const row = {
        step: key,
        started: null,
        finished: null,
        retries: [],
        reasoning: "",
        content: "",
        tools: [],
      };
      byStep.set(key, row);
      steps.push(row);
    }
    return byStep.get(key);
  }

  function toolRow(opId, step) {
    let tool = opId ? tools.get(opId) : null;
    if (!tool) {
      tool = { opId: opId || "", name: "tool", args: "", result: "", status: "", started: null, finished: null, attempt: 0, host: "", failures: [] };
      if (opId) tools.set(opId, tool);
      bucket(step).tools.push(tool);
    }
    return tool;
  }

  for (const event of events) {
    const payload = event.payload || {};
    if (event.type === "llm.started") {
      const row = bucket(payload.step);
      if (row.started == null) row.started = event.ts;
    } else if (event.type === "llm.retry") {
      bucket(payload.step).retries.push({
        ts: event.ts,
        attempt: payload.attempt,
        reason: payload.reason || "",
        backoff: payload.backoff_s,
      });
    } else if (event.type === "assistant.message") {
      const row = bucket(payload.step);
      row.content = payload.content || "";
      row.reasoning = payload.reasoning || "";
      row.finished = event.ts;
    } else if (event.type === "tool.pending") {
      const tool = toolRow(event.op_id, payload.step);
      tool.name = payload.name || tool.name;
      const known = event.op_id ? toolDetails.get(event.op_id) : null;
      if (known) {
        tool.name = known.name || tool.name;
        tool.args = known.args || tool.args;
        tool.result = known.result || tool.result;
        tool.status = known.status || tool.status;
      }
    } else if (event.type === "tool.started" || event.type === "tool.succeeded" || event.type === "tool.failed" || event.type === "tool.retrying") {
      const tool = toolRow(event.op_id, null);
      const known = event.op_id ? toolDetails.get(event.op_id) : null;
      if (known) {
        tool.name = known.name || tool.name;
        tool.args = known.args || tool.args;
        if (known.result) tool.result = known.result;
        tool.status = known.status || tool.status;
      }
      if (event.type === "tool.started") {
        tool.started = event.ts;
        tool.attempt = payload.attempt || tool.attempt;
        tool.host = payload.host_id || event.host_id || tool.host;
        tool.status = tool.status || "running";
      } else if (event.type === "tool.succeeded") {
        tool.finished = event.ts;
        tool.status = "succeeded";
        if (!tool.result && payload.summary) tool.result = payload.summary;
      } else if (event.type === "tool.failed") {
        tool.finished = event.ts;
        tool.status = "failed";
        tool.failures.push(payload.error || "failed");
        if (payload.error) tool.result = payload.error;
      } else if (event.type === "tool.retrying") {
        tool.failures.push(payload.reason || "retrying");
        tool.status = "retrying";
      }
    }
  }

  const open = [...byStep.values()].reverse().find((row) => row.started != null && !row.content);
  if (open && (liveText || liveReasoning)) {
    open.reasoning = liveReasoning || open.reasoning;
    open.content = liveText || open.content;
  } else if ((liveText || liveReasoning) && !steps.length) {
    steps.push({
      step: 0,
      started: null,
      finished: null,
      retries: [],
      reasoning: liveReasoning,
      content: liveText,
      tools: [],
    });
  }
  return steps;
}

function addBlock(parent, title, text, kind) {
  if (!text) return;
  const section = document.createElement("section");
  section.className = "traj-block" + (kind ? " " + kind : "");
  const heading = document.createElement("h4");
  heading.textContent = title;
  const body = document.createElement("pre");
  setHighlighted(body, text);
  section.append(heading, body);
  parent.appendChild(section);
}

function addContext(parent, step) {
  const messages = contextMessages(step);
  if (!messages.length) return;
  const section = document.createElement("section");
  section.className = "traj-block context";
  const heading = document.createElement("h4");
  heading.textContent = "Context sent to the model";
  section.appendChild(heading);
  for (const message of messages) {
    const row = document.createElement("div");
    row.className = "traj-msg role-" + message.role;
    const label = document.createElement("span");
    label.className = "traj-role";
    label.textContent = message.role;
    const body = document.createElement("pre");
    setHighlighted(body, message.content);
    row.append(label, body);
    section.appendChild(row);
  }
  parent.appendChild(section);
}

function renderTrajectory() {
  const body = document.getElementById("trajectory-body");
  const title = document.getElementById("trajectory-title");
  const session = sessions.find((item) => item.session_id === selected);
  title.textContent = session ? "Trajectory · " + (session.title || session.session_id) : "Trajectory";
  body.replaceChildren();
  const steps = buildTrajectory();
  if (!steps.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No model steps yet.";
    body.appendChild(empty);
    return;
  }
  const live = Boolean(session) && ["queued", "running", "waiting_host"].includes(session.status);
  for (const step of steps) {
    const streaming = step.started != null && !step.finished && (liveText || liveReasoning);
    const failed = step.retries.length || step.tools.some((tool) => tool.status === "failed" || tool.failures.length);
    const card = document.createElement("article");
    card.className = "step-card" + (streaming ? " streaming" : "") + (failed ? " has-failure" : "");
    const heading = document.createElement("h3");
    const stepName = document.createElement("span");
    stepName.textContent = "Step " + step.step;
    const state = document.createElement("span");
    const thinking = live && !streaming && step.started != null && !step.finished;
    state.className = "traj-status " + (streaming ? "running" : thinking ? "thinking" : failed ? "failed" : step.finished ? "succeeded" : "pending");
    state.textContent = streaming ? "streaming" : thinking ? "thinking" : failed ? "failed" : step.finished ? "done" : "pending";
    heading.append(stepName, state);
    const meta = document.createElement("p");
    meta.className = "step-meta";
    const bits = [];
    if (step.started != null) bits.push(formatClock(step.started));
    if (step.finished != null) bits.push("done " + formatClock(step.finished));
    if (step.started != null) bits.push(formatSpan(step.started, step.finished));
    meta.textContent = bits.join(" · ");
    card.append(heading, meta);
    for (const retry of step.retries) {
      const line = document.createElement("p");
      line.className = "step-failure";
      line.textContent = formatClock(retry.ts) + " · model retry " + retry.attempt + " · " + retry.reason + (retry.backoff != null ? " · backoff " + retry.backoff + "s" : "");
      card.appendChild(line);
    }
    addContext(card, step.step);
    addBlock(card, "Reasoning", step.reasoning, "reasoning");
    addBlock(card, "Model response", step.content, streaming ? "response streaming" : "response");
    for (const tool of step.tools) {
      const block = document.createElement("div");
      block.className = "tool-block status-" + (tool.status || "pending");
      const name = document.createElement("h4");
      const toolName = document.createElement("span");
      toolName.className = "traj-tool";
      toolName.textContent = tool.name;
      const badge = document.createElement("span");
      badge.className = "traj-status " + (tool.status || "pending");
      badge.textContent = tool.status || "pending";
      const timing = document.createElement("span");
      timing.className = "traj-timing";
      const bits = [];
      if (tool.started != null) bits.push(formatSpan(tool.started, tool.finished));
      if (tool.host) bits.push(tool.host);
      if (tool.attempt) bits.push("attempt " + tool.attempt);
      timing.textContent = bits.join(" · ");
      name.append(toolName, badge, timing);
      block.appendChild(name);
      for (const failure of tool.failures) {
        const line = document.createElement("p");
        line.className = "step-failure";
        line.textContent = failure;
        block.appendChild(line);
      }
      addRequest(block, tool.name, tool.args);
      addBlock(block, "Response", tool.result, tool.status === "failed" ? "response failed" : "response");
      card.appendChild(block);
    }
    body.appendChild(card);
  }
}

function trajectoryUrl(sessionId) {
  return "/sessions/" + encodeURIComponent(sessionId) + "/trajectory";
}

function timelineUrl(sessionId) {
  return "/sessions/" + encodeURIComponent(sessionId) + "/timeline";
}

function setPageLink(id, url) {
  const node = document.getElementById(id);
  if (!url) {
    node.setAttribute("aria-disabled", "true");
    node.href = "/sessions/";
    return;
  }
  node.removeAttribute("aria-disabled");
  node.href = url;
}

function pageView() {
  const match = location.pathname.match(/^\/sessions\/([^/]+)\/(trajectory|timeline)\/?$/);
  if (!match) return { kind: "", id: "" };
  return { kind: match[2], id: decodeURIComponent(match[1]) };
}

function showTrajectory() {
  hideTimeline();
  trajectoryOpen = true;
  document.getElementById("trajectory").setAttribute("aria-pressed", "true");
  document.getElementById("trajectory-panel").hidden = false;
  const link = document.getElementById("trajectory-link");
  link.href = location.origin + trajectoryUrl(selected);
  link.textContent = link.href;
  renderTrajectory();
}

let timelineOpen = false;

function showTimeline() {
  if (trajectoryOpen) hideTrajectory();
  timelineOpen = true;
  document.getElementById("timeline-open").setAttribute("aria-pressed", "true");
  const panel = document.getElementById("timeline-panel");
  panel.hidden = false;
  document.getElementById("timeline-panel-body").appendChild(document.querySelector(".timeline-wrap"));
  const session = sessions.find((item) => item.session_id === selected);
  document.getElementById("timeline-title").textContent = session
    ? "Timeline · " + (session.title || session.session_id)
    : "Timeline";
  const link = document.getElementById("timeline-page-link");
  link.href = location.origin + timelineUrl(selected);
  link.textContent = link.href;
  renderTimeline();
}

function hideTimeline() {
  timelineOpen = false;
  const open = document.getElementById("timeline-open");
  if (open) open.setAttribute("aria-pressed", "false");
  const panel = document.getElementById("timeline-panel");
  if (panel) panel.hidden = true;
  const wrap = document.querySelector(".timeline-wrap");
  const system = document.querySelector(".col.system");
  if (wrap && system) system.appendChild(wrap);
}

function hideTrajectory() {
  trajectoryOpen = false;
  document.getElementById("trajectory").setAttribute("aria-pressed", "false");
  document.getElementById("trajectory-panel").hidden = true;
}

async function refreshHistory() {
  const response = await fetch("/api/snapshot/history");
  if (!response.ok) return;
  const body = await response.json();
  snapshotHistory = body.entries || [];
  document.getElementById("revert").disabled = snapshotHistory.length === 0;
}

function resetCommitForm() {
  document.getElementById("commit-check").hidden = false;
  document.getElementById("commit-confirm").checked = false;
  document.getElementById("commit-save").hidden = false;
  document.getElementById("commit-save").disabled = true;
  document.getElementById("commit-save").textContent = "Save";
  document.getElementById("commit-llm").hidden = true;
  document.getElementById("commit-summary").hidden = true;
  document.getElementById("commit-summary").textContent = "";
  document.getElementById("commit-dismiss").textContent = "Cancel";
}

function openCommit() {
  if (!selected) return;
  const dialog = document.getElementById("commit-dialog");
  document.getElementById("commit-form").dataset.mode = "commit";
  resetCommitForm();
  document.getElementById("commit-title").textContent = "Commit snapshot";
  document.getElementById("commit-copy").textContent = "This snapshot will replace the existing code in the base directory.";
  document.getElementById("commit-check-label").textContent = "Replace the base snapshot with this session's files";
  dialog.hidden = false;
}

function openRevert() {
  const latest = snapshotHistory[0];
  if (!latest) return;
  const dialog = document.getElementById("commit-dialog");
  document.getElementById("commit-form").dataset.mode = "revert";
  resetCommitForm();
  document.getElementById("commit-save").textContent = "Revert";
  document.getElementById("commit-title").textContent = "Revert base snapshot";
  const when = latest.saved_at ? " Saved " + formatClock(latest.saved_at) + "." : "";
  document.getElementById("commit-copy").textContent = "This puts the base directory back to the snapshot saved before the last change." + when + " Revert again to undo it.";
  document.getElementById("commit-check-label").textContent = "Restore that saved snapshot over the current base";
  const pre = document.getElementById("commit-summary");
  if (latest.summary) {
    pre.hidden = false;
    pre.textContent = latest.summary;
  }
  dialog.hidden = false;
}

function closeCommit() {
  document.getElementById("commit-dialog").hidden = true;
}

function showConflict(summary) {
  document.getElementById("commit-title").textContent = "Base snapshot changed";
  document.getElementById("commit-copy").textContent = "The base directory was edited after this session started. Saving now would overwrite those changes. You can let the model reconcile the files instead.";
  const pre = document.getElementById("commit-summary");
  pre.hidden = false;
  pre.textContent = summary || "The fingerprints do not match.";
  document.getElementById("commit-save").hidden = true;
  document.getElementById("commit-llm").hidden = false;
}

async function saveCommit(event) {
  event.preventDefault();
  if (document.getElementById("commit-form").dataset.mode === "revert") {
    await saveRevert(event);
    return;
  }
  if (!selected || !document.getElementById("commit-confirm").checked) return;
  const save = document.getElementById("commit-save");
  save.disabled = true;
  save.textContent = "Saving…";
  document.getElementById("commit-copy").textContent = "Replacing the base snapshot…";
  try {
    const response = await fetch("/api/sessions/" + encodeURIComponent(selected) + "/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    if (response.status === 409) {
      const body = await response.json();
      const detail = body.detail || {};
      showConflict(typeof detail === "string" ? detail : detail.summary || "");
      return;
    }
    if (!response.ok) {
      const message = await errorText(response);
      document.getElementById("commit-copy").textContent = message;
      formError(message);
      return;
    }
    closeCommit();
    formError("");
    refreshHistory();
  } finally {
    if (!document.getElementById("commit-dialog").hidden && document.getElementById("commit-form").dataset.mode === "commit") {
      save.disabled = !document.getElementById("commit-confirm").checked;
      save.textContent = "Save";
    }
  }
}

async function saveRevert(event) {
  event.preventDefault();
  if (!document.getElementById("commit-confirm").checked) return;
  const save = document.getElementById("commit-save");
  save.disabled = true;
  save.textContent = "Reverting…";
  document.getElementById("commit-copy").textContent = "Restoring the saved base snapshot…";
  try {
    const response = await fetch("/api/snapshot/revert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    if (!response.ok) {
      const message = await errorText(response);
      document.getElementById("commit-copy").textContent = message;
      formError(message);
      return;
    }
    await refreshHistory();
    document.getElementById("commit-title").textContent = "Base snapshot restored";
    document.getElementById("commit-copy").textContent = snapshotHistory.length
      ? "The previous snapshot is the base directory again. Revert again to undo this."
      : "The previous snapshot is the base directory again.";
    document.getElementById("commit-check").hidden = true;
    document.getElementById("commit-save").hidden = true;
    document.getElementById("commit-summary").hidden = true;
    document.getElementById("commit-dismiss").textContent = "Close";
    formError("");
  } finally {
    if (!document.getElementById("commit-save").hidden) {
      save.disabled = !document.getElementById("commit-confirm").checked;
      save.textContent = "Revert";
    }
  }
}

async function resolveCommit() {
  if (!selected) return;
  const response = await fetch("/api/sessions/" + encodeURIComponent(selected) + "/commit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true, allow_llm: true }),
  });
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const body = await response.json();
  const known = sessions.find((session) => session.session_id === selected);
  if (known && body.session) Object.assign(known, body.session);
  closeCommit();
  renderSessionList();
  if (body.session) renderHeader(body.session);
}

function showLiveCode() {
  liveOn = true;
  const button = document.getElementById("live-code");
  button.setAttribute("aria-pressed", "true");
  button.disabled = false;
  document.getElementById("code-panel").hidden = false;
  renderCode();
}

function toggleLiveCode() {
  if (!selected) return;
  liveOn = !liveOn;
  document.getElementById("live-code").setAttribute("aria-pressed", liveOn ? "true" : "false");
  document.getElementById("code-panel").hidden = !liveOn;
  if (liveOn) renderCode();
}

async function selectSession(id) {
  const token = ++selectToken;
  closeSessionStream();
  selected = id;
  const view = pageView();
  if (view.id && view.id !== id) {
    if (view.kind === "trajectory") hideTrajectory();
    if (view.kind === "timeline") hideTimeline();
  }
  const stay = view.id === id && (view.kind === "trajectory" || view.kind === "timeline");
  if (!stay && (location.hash !== "#" + id || location.pathname !== "/")) {
    history.replaceState(null, "", "/#" + id);
  }
  renderSessionList();
  renderBanners();
  renderTimeline();
  const response = await fetch("/api/sessions/" + encodeURIComponent(id));
  if (token !== selectToken) return;
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const detail = await response.json();
  if (token !== selectToken) return;
  floor = detail.latest_event_seq || 0;
  const known = sessions.find((session) => session.session_id === id);
  if (known) Object.assign(known, detail.session);
  else sessions.unshift(detail.session);
  renderSnapshot(detail);
  renderSessionList();
  await hydrateCode(detail);
  if (token !== selectToken) return;
  showLiveCode();
  openSessionStream(id);
}

async function reloadSelected() {
  if (!selected) return;
  const token = selectToken;
  const response = await fetch("/api/sessions/" + encodeURIComponent(selected));
  if (!response.ok || token !== selectToken) return;
  const detail = await response.json();
  if (token !== selectToken) return;
  floor = detail.latest_event_seq || 0;
  renderSnapshot(detail);
  renderBanners();
  await hydrateCode(detail);
}

async function startTask(event) {
  event.preventDefault();
  if (submitting) return;
  const prompt = promptEl.value.trim();
  if (!prompt) {
    formError("Prompt is required.");
    return;
  }
  submitting = true;
  if (!submitKey) submitKey = crypto.randomUUID();
  document.getElementById("start").disabled = true;
  formError("");
  try {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": submitKey },
      body: JSON.stringify(loadedImport ? { prompt: prompt, import_id: loadedImport.id } : { prompt: prompt }),
    });
    if (!response.ok) throw new Error(await errorText(response));
    const body = await response.json();
    submitKey = "";
    await fetchSessions();
    await selectSession(body.session.session_id);
  } catch (err) {
    formError(err.message || "Could not start the task.");
  } finally {
    submitting = false;
    document.getElementById("start").disabled = false;
  }
}

const SHELL_CLOCK_PROMPT = [
  "Use only the shell tool. Do not create or edit files.",
  "",
  "Call shell with timeout_s 10 and this command:",
  "",
  "python3 -c 'import datetime; current_time = datetime.datetime.now().astimezone().isoformat(timespec=\"seconds\"); print(current_time)'",
  "",
  "When the result comes back, call that same command again immediately. Keep going until the timestamps span 100 seconds from the first print. One print per call, so each time shows up on its own tool card.",
  "",
  "The shell runs on the host this session is assigned to. The card shows that host. If the session moves to the other host, keep the same loop so both hosts print the time.",
].join("\n");

const NEW_CODEBASE_PROMPT = [
  "Create a new Python package named splitbill.",
  "",
  "splitbill.ledger.settle(expenses) takes a list of {payer, amount, participants} dicts and returns transfers of {from, to, amount} that clear the group. Amounts are integer cents. Ignore a person who paid and is the only participant. Reject a non-positive amount and an empty participant list.",
  "",
  'Add python -m splitbill.cli, which reads a JSON list of expenses on stdin and prints one transfer per line: "alice -> bob 500".',
  "",
  "Write tests/test_ledger.py first, covering an even split, uneven payers, and the two rejections. Run run_tests and implement until that suite passes. Add a short README with one example.",
].join("\n");

let sampleToken = 0;

function selectSample(id) {
  document.querySelectorAll(".sample").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.sample === id ? "true" : "false");
  });
}

function clearSampleSelection() {
  selectSample("");
}

async function useDummy() {
  const token = ++sampleToken;
  formError("");
  const response = await fetch("/api/task");
  if (token !== sampleToken) return;
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const body = await response.json();
  const imported = await fetch("/api/imports/dummy", { method: "POST" });
  if (token !== sampleToken) return;
  if (!imported.ok) {
    formError(await errorText(imported));
    return;
  }
  const saved = await imported.json();
  promptEl.value = body.text || "";
  submitKey = "";
  loadedImport = { id: saved.import_id, name: saved.name, files: saved.files || [] };
  setFolderLabel();
  selectSample("dummy");
}

function useNewCodebase() {
  sampleToken += 1;
  formError("");
  loadedImport = null;
  setFolderLabel();
  promptEl.value = NEW_CODEBASE_PROMPT;
  submitKey = "";
  selectSample("new");
}

function useShellClock() {
  sampleToken += 1;
  formError("");
  loadedImport = null;
  setFolderLabel();
  promptEl.value = SHELL_CLOCK_PROMPT;
  submitKey = "";
  selectSample("clock");
}

async function cancelSession(sessionId) {
  const id = typeof sessionId === "string" ? sessionId : selected;
  if (!id) return;
  const response = await fetch("/api/sessions/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const body = await response.json();
  const known = sessions.find((session) => session.session_id === id);
  if (known) Object.assign(known, body.session);
  renderSessionList();
  if (id === selected) renderHeader(body.session);
}

async function killHost(hostId) {
  const response = await fetch("/api/hosts/" + encodeURIComponent(hostId) + "/kill", { method: "POST" });
  if (!response.ok) formError(await errorText(response));
}

document.getElementById("task-form").addEventListener("submit", startTask);
document.getElementById("dummy").addEventListener("click", useDummy);
document.getElementById("new-codebase").addEventListener("click", useNewCodebase);
document.getElementById("shell-clock").addEventListener("click", useShellClock);
promptEl.addEventListener("input", () => {
  submitKey = "";
  clearSampleSelection();
});
document.getElementById("cancel").addEventListener("click", () => cancelSession());
document.getElementById("open-folder").addEventListener("click", () => {
  document.getElementById("folder-input").click();
});
document.getElementById("folder-input").addEventListener("change", (event) => {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (files.length) chooseFolder(files);
});
document.getElementById("clear-folder").addEventListener("click", () => {
  loadedImport = null;
  setFolderLabel();
  clearSampleSelection();
});
document.getElementById("live-code").addEventListener("click", toggleLiveCode);
document.getElementById("commit").addEventListener("click", openCommit);
document.getElementById("revert").addEventListener("click", openRevert);
document.getElementById("commit-confirm").addEventListener("change", (event) => {
  document.getElementById("commit-save").disabled = !event.target.checked;
});
document.getElementById("commit-dismiss").addEventListener("click", closeCommit);
document.getElementById("commit-form").addEventListener("submit", saveCommit);
document.getElementById("commit-llm").addEventListener("click", resolveCommit);
document.getElementById("trajectory").addEventListener("click", (event) => {
  if (event.currentTarget.getAttribute("aria-disabled") === "true") event.preventDefault();
});
document.getElementById("timeline-open").addEventListener("click", (event) => {
  if (event.currentTarget.getAttribute("aria-disabled") === "true") event.preventDefault();
});
document.getElementById("trajectory-close").addEventListener("click", () => {
  if (selected) location.href = "/#" + selected;
  else location.href = "/";
});
document.getElementById("timeline-close").addEventListener("click", () => {
  if (selected) location.href = "/#" + selected;
  else location.href = "/";
});
document.getElementById("kill-backend").addEventListener("click", async () => {
  try {
    const response = await fetch(supervisorUrl("/kill-backend"), { method: "POST" });
    if (!response.ok) throw new Error("kill failed");
  } catch (_err) {
    document.getElementById("supervisor").textContent = "kill failed";
  }
  pollSupervisor();
});
document.getElementById("tool-only").addEventListener("change", (event) => {
  toolOnly = event.target.checked;
  renderTimeline();
});
sessionsEl.addEventListener("click", (event) => {
  const cancel = event.target.closest("button[data-cancel]");
  if (cancel) {
    cancelSession(cancel.dataset.cancel);
    return;
  }
  const button = event.target.closest("button[data-session]");
  if (button) selectSession(button.dataset.session);
});
hostsEl.addEventListener("click", (event) => {
  const sessionButton = event.target.closest("button[data-session]");
  if (sessionButton) {
    selectSession(sessionButton.dataset.session);
    return;
  }
  const button = event.target.closest("button[data-kill]");
  if (button) killHost(button.dataset.kill);
});

renderChanged();
renderTimeline();
renderHosts();
openGlobalStream();
refreshSessions().then(async () => {
  const view = pageView();
  const id = view.id || location.hash.replace(/^#/, "");
  if (!id) return;
  await selectSession(id);
  if (view.id !== selected) return;
  if (view.kind === "trajectory") showTrajectory();
  if (view.kind === "timeline") showTimeline();
});
refreshHosts();
refreshHistory();
pollSupervisor();
setInterval(() => {
  renderElapsed();
  renderHeartbeatAges();
  renderThinking();
  if (trajectoryOpen && document.querySelector("#trajectory-body .traj-status.thinking")) renderTrajectory();
}, 1000);
setInterval(() => {
  refreshSessions();
  refreshHosts();
  pollSupervisor();
}, 2000);
