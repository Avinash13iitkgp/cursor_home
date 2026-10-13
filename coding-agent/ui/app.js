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
let submitKey = "";
let submitting = false;
let down = false;
let reconnects = 0;
let selectToken = 0;
let sessionSource = null;
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

function appendDelta(text) {
  const stick = nearBottom(transcriptEl);
  clearWaiting();
  let bubble = document.getElementById("assistant-draft");
  if (!bubble) bubble = addAssistant("", true);
  bubble.textContent += text;
  if (stick) transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function finalizeAssistant(text) {
  const stick = nearBottom(transcriptEl);
  let bubble = document.getElementById("assistant-draft");
  if (!text && !bubble) return;
  clearWaiting();
  if (!bubble) bubble = addAssistant("", false);
  bubble.id = "";
  bubble.className = "bubble assistant";
  bubble.textContent = text;
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
  showFinal(session);
}

function renderSessionList() {
  sessionsEl.replaceChildren();
  if (!sessions.length) {
    emptyNote(sessionsEl, "No tasks yet.");
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
    sessionsEl.appendChild(button);
  }
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
  node.querySelector(".tool-name").textContent = state.name || "tool";
  node.querySelector(".tool-status").textContent = STATUS_ICON[state.status] || "○";
  node.querySelector(".tool-args").textContent = shortArgs(state.name, state.args);
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
    details.querySelector("pre").textContent = state.result;
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

function renderSnapshot(detail) {
  cards.clear();
  cardsBySlot.clear();
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
  if (!transcriptEl.childElementCount) emptyNote(transcriptEl, "Waiting for the agent.");
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
  for (const event of rows) {
    const row = document.createElement("div");
    row.className = "tl-row";
    const main = document.createElement("div");
    main.className = "tl-main";
    const time = document.createElement("span");
    time.textContent = formatClock(event.ts);
    const type = document.createElement("span");
    type.className = "type";
    type.textContent = event.type;
    main.append(time, type);
    row.appendChild(main);
    const extra = [event.op_id, event.host_id].filter(Boolean).join("  ");
    if (extra) {
      const line = document.createElement("div");
      line.className = "tl-extra";
      line.textContent = extra;
      row.appendChild(line);
    }
    timelineEl.appendChild(row);
  }
  timelineEl.scrollTop = timelineEl.scrollHeight;
}

function rememberTimeline(event) {
  if (!event.seq || timeline.some((item) => item.seq === event.seq)) return;
  timeline.push(event);
  renderTimeline();
}

function bannerText(event) {
  const payload = event.payload || {};
  if (event.type === "host.down") return (event.host_id || "host") + " down";
  if (event.type === "host.replaced") {
    return (event.host_id || "host") + " replaced" + (payload.generation ? " g" + payload.generation : "");
  }
  if (event.type === "session.migrated") {
    const generation = payload.generation ? " g" + payload.generation : "";
    return "migrated " + (payload.from || "?") + " → " + (payload.to || "?") + generation;
  }
  if (event.type === "session.claimed" && payload.resumed) return "session resumed";
  return "";
}

function bannerVisible(event) {
  if (event.type === "host.down" || event.type === "host.replaced") return true;
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
      });
      if (fn.name === "write_file") noteCode(fn.name, fn.arguments || "", "pending");
    });
    const waiting = transcriptEl.querySelector(".empty");
    if (waiting) waiting.remove();
    return;
  }
  if (event.type === "tool.pending") {
    upsertCard({
      op_id: event.op_id,
      step: payload.step,
      call_index: payload.call_index,
      name: payload.name,
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
    return;
  }
  if (event.type === "tool.failed") {
    const state = upsertCard({
      op_id: event.op_id,
      status: "failed",
      result: payload.error || "",
    });
    noteCode(state.name, state.args, "failed");
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
  if (event.type === "assistant.delta") {
    if (event.session_id === selected) appendDelta(event.payload.text || "");
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
  if (event.seq <= floor) return;
  applyTranscript(event);
}

function listen(source, options) {
  const includeDeltas = !options || options.deltas !== false;
  for (const type of EVENT_TYPES) {
    if (type === "assistant.delta" && !includeDeltas) continue;
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
    const card = document.createElement("article");
    card.className = "host-card";
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
    card.append(top, meta, kill);
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
let liveOn = false;
let openFile = "";
const codeFiles = new Map();
const unresolvedFiles = new Set();

function setFolderLabel() {
  const node = document.getElementById("folder-label");
  const clear = document.getElementById("clear-folder");
  if (!loadedImport) {
    node.firstChild.textContent = "Workspace: dummy repo ";
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

async function hydrateCode(detail) {
  codeFiles.clear();
  unresolvedFiles.clear();
  openFile = "";
  if (detail.session.import_id) {
    const response = await fetch("/api/imports/" + encodeURIComponent(detail.session.import_id));
    if (response.ok) {
      const body = await response.json();
      for (const file of body.files || []) codeFiles.set(file.path, { text: file.content || "", status: "" });
    }
  }
  const calls = (detail.tool_calls || []).slice().sort((a, b) => a.step - b.step || a.call_index - b.call_index);
  for (const call of calls) {
    if (call.status === "succeeded") noteCode(call.tool_name, call.args_json, "succeeded", { fetch: false });
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
    view.textContent = "Open a folder before starting, or wait for the agent to edit a file.";
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
  view.textContent = file.status === "deleted" ? "" : file.text;
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
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
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

async function useDummy() {
  formError("");
  const response = await fetch("/api/task");
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const body = await response.json();
  promptEl.value = body.text || "";
  submitKey = "";
}

async function cancelSession() {
  if (!selected) return;
  const response = await fetch("/api/sessions/" + encodeURIComponent(selected) + "/cancel", { method: "POST" });
  if (!response.ok) {
    formError(await errorText(response));
    return;
  }
  const body = await response.json();
  const known = sessions.find((session) => session.session_id === selected);
  if (known) Object.assign(known, body.session);
  renderSessionList();
  renderHeader(body.session);
}

async function killHost(hostId) {
  const response = await fetch("/api/hosts/" + encodeURIComponent(hostId) + "/kill", { method: "POST" });
  if (!response.ok) formError(await errorText(response));
}

document.getElementById("task-form").addEventListener("submit", startTask);
document.getElementById("dummy").addEventListener("click", useDummy);
promptEl.addEventListener("input", () => {
  submitKey = "";
});
document.getElementById("cancel").addEventListener("click", cancelSession);
document.getElementById("open-folder").addEventListener("click", () => {
  document.getElementById("folder-input").click();
});
document.getElementById("folder-input").addEventListener("change", (event) => {
  const files = event.target.files;
  if (files && files.length) chooseFolder(files);
  event.target.value = "";
});
document.getElementById("clear-folder").addEventListener("click", () => {
  loadedImport = null;
  setFolderLabel();
});
document.getElementById("live-code").addEventListener("click", toggleLiveCode);
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
  const button = event.target.closest("button[data-session]");
  if (button) selectSession(button.dataset.session);
});
hostsEl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-kill]");
  if (button) killHost(button.dataset.kill);
});

renderChanged();
renderTimeline();
renderHosts();
openGlobalStream();
refreshSessions().then(() => {
  const hash = location.hash.replace(/^#/, "");
  if (hash) selectSession(hash);
});
refreshHosts();
pollSupervisor();
setInterval(() => {
  renderElapsed();
  renderHeartbeatAges();
}, 1000);
setInterval(() => {
  refreshSessions();
  refreshHosts();
  pollSupervisor();
}, 2000);
