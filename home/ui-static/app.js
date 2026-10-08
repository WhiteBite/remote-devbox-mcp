const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const BEAT_MS = 1000;
const POLL_EVERY = 4;
const LOG_SOURCES = ["host", "ingress", "watchdog", "runner"];

const el = {};
const state = {
  status: null,
  manifest: null,
  manifestStatus: 0,
  manifestView: "structured",
  exposure: null,
  exposureStatus: 0,
  profileLoaded: false,
  lastProfile: undefined,
  logSource: "",
  logFilter: "",
  handoffText: "",
  revealed: false,
  ingressUrl: "",
  beat: 0,
  inFlight: false,
  unreachable: false,
  actionBusy: false,
  submittedId: "",
  editorName: "",
  editorPath: "",
  editorDirty: false,
};

let sessionToken = null;
let pollerCount = 0;
let eventsSource = null;

function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function icon(name, cls) {
  const s = document.createElementNS(SVGNS, "svg");
  if (cls) s.setAttribute("class", cls);
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVGNS, "use");
  u.setAttribute("href", "#i-" + name);
  s.appendChild(u);
  return s;
}

function qs(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? "?" + s : "";
}

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    const ct = res.headers.get("content-type") || "";
    let data = null;
    if (ct.includes("application/json")) {
      try { data = await res.json(); } catch { data = null; }
    } else {
      data = await res.text();
    }
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: null, error: err };
  }
}

async function ensureSessionToken() {
  if (sessionToken) return sessionToken;
  const res = await api("/api/session");
  if (res.ok && res.data && typeof res.data.header_token === "string") {
    sessionToken = res.data.header_token;
  }
  return sessionToken;
}

function setDot(node, level) {
  if (!node) return;
  node.className = "dot " + (level || "idle");
}

function parseCompose(text) {
  const rows = [];
  if (typeof text === "string") {
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      const sp = line.indexOf(" ");
      const name = sp === -1 ? line : line.slice(0, sp);
      const status = sp === -1 ? "" : line.slice(sp + 1);
      rows.push({ name, status, healthy: /health|running|up\b/i.test(status) });
    }
  }
  return { total: rows.length, healthy: rows.filter((r) => r.healthy).length, rows };
}

function computeHealth(s) {
  if (!s) return { level: "idle", word: "unknown", summary: "awaiting first poll", cells: [] };
  const p = s.ports || {};
  const wd = !!s.watchdog;
  const comp = parseCompose(s.compose_ps);
  const hs = s.host_services || { recorded: 0, alive: 0 };
  const ip = s.ingress_pids || { recorded: 0, alive: 0 };
  const runnerNA = p.runner === null || p.runner === undefined;

  let level = "ok";
  const issues = [];
  if (!p.bridge) { level = "err"; issues.push("bridge down"); }
  if (!p.ingress) { level = "err"; issues.push("ingress down"); }
  if (level !== "err") {
    if (!wd) { level = "warn"; issues.push("watchdog off"); }
    else if (comp.total && comp.healthy < comp.total) { level = "warn"; issues.push((comp.total - comp.healthy) + " svc unhealthy"); }
    else if (hs.recorded && hs.alive < hs.recorded) { level = "warn"; issues.push("host svc " + hs.alive + "/" + hs.recorded); }
  }

  const ok = ["bridge", "ingress", "cockpit"].filter((k) => p[k]).length;
  const summary = issues.length
    ? issues.join(" · ")
    : ok + "/3 core up · " + comp.healthy + "/" + comp.total + " services · watchdog " + (wd ? "on" : "off");

  const cells = [
    { label: "bridge", level: p.bridge ? "ok" : "err" },
    { label: "ingress", level: p.ingress ? "ok" : "err" },
    { label: "cockpit", level: p.cockpit ? "ok" : "err" },
    { label: "runner", level: runnerNA ? "idle" : (p.runner ? "ok" : "warn") },
    { label: "watchdog", level: wd ? "ok" : "warn" },
    { label: "services " + comp.healthy + "/" + comp.total, level: comp.total === 0 ? "idle" : (comp.healthy === comp.total ? "ok" : "warn") },
    { label: "host svc " + hs.alive + "/" + hs.recorded, level: hs.recorded === 0 ? "idle" : (hs.alive === hs.recorded ? "ok" : "warn") },
    { label: "ingress pids " + ip.alive + "/" + ip.recorded, level: ip.recorded === 0 ? "idle" : (ip.alive === ip.recorded ? "ok" : "warn") },
  ];
  return { level, word: level === "ok" ? "healthy" : level === "warn" ? "degraded" : "down", summary, cells };
}

function renderVitals(s) {
  const health = computeHealth(s);
  setDot(el.vitalHealthDot, health.level);
  el.vitalHealthText.textContent = health.word;
  setDot(el.portCockpit, s && s.ports && s.ports.cockpit ? "ok" : "err");
  setDot(el.portIngress, s && s.ports && s.ports.ingress ? "ok" : "err");
  setDot(el.portBridge, s && s.ports && s.ports.bridge ? "ok" : "err");
  const runner = s && s.ports ? s.ports.runner : null;
  setDot(el.portRunner, runner === null || runner === undefined ? "idle" : (runner ? "ok" : "warn"));

  const tile = el.healthBadge.closest(".tile");
  if (tile) tile.style.setProperty("--health-color", "var(--" + (health.level === "ok" ? "ok" : health.level === "warn" ? "warn" : health.level === "err" ? "err" : "idle") + ")");
  el.healthBadge.className = "health-badge " + health.level;
  const bd = el.healthBadge.querySelector(".dot");
  if (bd) bd.className = "dot " + health.level;
  el.healthBadgeText.textContent = health.word;
  el.healthSummary.textContent = health.summary;

  el.healthBreakdown.replaceChildren();
  for (const c of health.cells) {
    const cell = h("span", "dot-cell");
    cell.appendChild(h("span", "dot " + c.level));
    cell.appendChild(document.createTextNode(c.label));
    el.healthBreakdown.appendChild(cell);
  }
}

function renderHero(s) {
  if (!s) return;
  const prof = s.profile || {};
  el.profileName.textContent = prof.active || "no profile";
  el.profileSource.textContent = "source: " + (prof.source_dir || "unresolved");
  el.profileSource.title = prof.source_dir || "";
  if (prof.active) {
    if (!el.useName.value) el.useName.value = prof.active;
    if (!el.editorNameInput.value) el.editorNameInput.value = prof.active;
  }

  const env = s.env || {};
  const man = s.manifest || {};
  const mode = (man.mode || env.MODE || "").toString();
  const known = ["readonly", "standard", "full"].includes(mode);
  el.modeBadge.className = "mode-badge " + (known ? mode : "unknown");
  el.modeBadge.textContent = mode || "unknown";
  const modeTile = el.modeBadge.closest(".tile");
  if (modeTile) modeTile.style.setProperty("--mode-color", "var(--" + (mode === "full" ? "ok" : mode === "readonly" ? "warn" : mode === "standard" ? "info" : "idle") + ")");

  const tc = (env.TOOLCHAIN || "").trim();
  el.toolchainChips.replaceChildren();
  if (tc) {
    for (const part of tc.split(/\s+/)) el.toolchainChips.appendChild(h("span", "chip accent", part));
  } else {
    el.toolchainChips.appendChild(h("span", "chip", "none"));
  }

  const url = (man.ingress_url || env.PUBLIC_URL || "").toString();
  state.ingressUrl = url;
  el.ingressUrl.textContent = url || "no tunnel";
  el.ingressUrl.style.color = url ? "" : "var(--fg-mute)";
  el.ingressCopy.disabled = !url;

  const wd = !!s.watchdog;
  el.watchdogState.className = "pill-state " + (wd ? "ok" : "idle");
  el.watchdogState.replaceChildren();
  el.watchdogState.appendChild(h("span", "dot " + (wd ? "ok" : "idle")));
  el.watchdogState.appendChild(document.createTextNode(wd ? "running" : "stopped"));
}

function renderClock() {
  el.clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
}

function renderBanner() {
  if (state.unreachable) {
    el.bannerText.textContent = "Cockpit API unreachable or session expired. Reopen the cockpit from devbox.py (loopback only).";
    el.banner.classList.add("show");
  } else {
    el.banner.classList.remove("show");
    el.bannerText.textContent = "";
  }
}

function highlightSegments(line, needle) {
  const out = [];
  const parts = line.split("[REDACTED]");
  parts.forEach((part, i) => {
    if (i > 0) out.push({ t: "[REDACTED]", cls: "redacted" });
    if (!needle) { if (part) out.push({ t: part }); return; }
    const low = part.toLowerCase();
    const n = needle.toLowerCase();
    let from = 0;
    let idx;
    while ((idx = low.indexOf(n, from)) !== -1) {
      if (idx > from) out.push({ t: part.slice(from, idx) });
      out.push({ t: part.slice(idx, idx + n.length), mark: true });
      from = idx + n.length;
    }
    if (from < part.length) out.push({ t: part.slice(from) });
  });
  return out;
}

function renderLogs(res) {
  el.logLive.className = "dot " + (res.ok ? "ok" : res.status === 0 ? "err" : "warn");
  const river = el.logRiver;
  river.replaceChildren();

  if (!res.ok) {
    const e = h("div", "empty");
    e.appendChild(icon("alert"));
    e.appendChild(h("span", null, res.status === 0 ? "log stream unreachable" : "logs unavailable (" + res.status + ")"));
    river.appendChild(e);
    el.logMeta.textContent = "error";
    return;
  }

  const logs = (res.data && res.data.logs) || [];
  let lineCount = 0;
  const needle = state.logFilter;

  if (!logs.length) {
    const e = h("div", "empty");
    e.appendChild(icon("inbox"));
    e.appendChild(h("span", null, needle ? "no lines match “" + needle + "”" : "no log lines for this source"));
    river.appendChild(e);
    el.logMeta.textContent = "0 lines";
    return;
  }

  for (const group of logs) {
    const lines = Array.isArray(group.lines) ? group.lines : [];
    lineCount += lines.length;
    const g = h("div", "log-group");
    const head = h("div", "log-group-head");
    head.appendChild(h("span", "log-src " + (LOG_SOURCES.includes(group.source) ? group.source : "host"), group.source || "?"));
    head.appendChild(h("span", "log-file", group.file || ""));
    head.appendChild(h("span", "log-count", lines.length + " lines"));
    g.appendChild(head);

    const pre = h("div", "log-lines");
    for (const line of lines) {
      const ln = h("span", "log-line");
      for (const seg of highlightSegments(String(line), needle)) {
        if (seg.cls) ln.appendChild(h("span", seg.cls, seg.t));
        else if (seg.mark) ln.appendChild(h("mark", null, seg.t));
        else ln.appendChild(document.createTextNode(seg.t));
      }
      pre.appendChild(ln);
    }
    g.appendChild(pre);
    river.appendChild(g);
  }

  const scope = state.logSource ? state.logSource : "all";
  el.logMeta.textContent = logs.length + " files · " + lineCount + " lines · " + scope;
}

function renderHandoffState() {
  el.handoffPre.textContent = state.handoffText || (state.revealed ? "(empty)" : "loading hand-off…");
  el.handoffPre.classList.toggle("revealed", state.revealed);
  el.handoffBadge.className = "badge " + (state.revealed ? "revealed" : "masked");
  el.handoffBadge.textContent = state.revealed ? "revealed" : "masked";
  el.revealLabel.textContent = state.revealed ? "Hide" : "Reveal";
  const use = el.btnReveal.querySelector("use");
  if (use) use.setAttribute("href", state.revealed ? "#i-eye-off" : "#i-eye");
}

function setHandoffError(msg) {
  el.handoffPre.textContent = msg;
  el.handoffPre.classList.remove("revealed");
}

async function loadHandoffMasked() {
  state.revealed = false;
  const res = await api("/api/handoff");
  if (res.ok && res.data) state.handoffText = typeof res.data.handoff === "string" ? res.data.handoff : "";
  else state.handoffText = res.status === 0 ? "hand-off unreachable" : "hand-off unavailable (" + res.status + ")";
  renderHandoffState();
}

async function toggleReveal() {
  if (!state.revealed) {
    const token = await ensureSessionToken();
    if (!token) { setHandoffError("no session token — reload the cockpit"); return; }
    const res = await api("/api/handoff?reveal=1", { headers: { "X-RDM-Token": token } });
    if (res.ok && res.data) {
      state.revealed = true;
      state.handoffText = typeof res.data.handoff === "string" ? res.data.handoff : "";
      renderHandoffState();
    } else {
      setHandoffError(res.status === 403 ? "reveal forbidden (bad header token)" : "reveal failed (" + res.status + ")");
    }
  } else {
    await loadHandoffMasked();
  }
}

function metricBox(label, value) {
  const text = value == null || value === "" ? "—" : String(value);
  const m = h("div", "metric");
  m.title = text;
  m.appendChild(h("span", "label", label));
  m.appendChild(h("div", "metric-val", text));
  return m;
}

function authTag(auth) {
  const a = (auth || "none").toString();
  const cls = ["ingress", "bearer", "self"].includes(a) ? a : "other";
  return h("span", "auth-tag " + cls, a);
}

function endpointsTable(endpoints) {
  const wrap = h("div", "table-scroll");
  const table = h("table", "data-table");
  const thead = h("thead");
  const hr = h("tr");
  for (const c of ["name", "port", "auth", "path", "token"]) hr.appendChild(h("th", null, c));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tb = h("tbody");
  for (const ep of endpoints) {
    const tr = h("tr");
    tr.appendChild(h("td", "mono-cell", ep.name || ""));
    tr.appendChild(h("td", "mono-cell", ep.port != null ? String(ep.port) : ""));
    const tdAuth = h("td");
    tdAuth.appendChild(authTag(ep.auth));
    tr.appendChild(tdAuth);
    tr.appendChild(h("td", "mono-cell", ep.path || ""));
    tr.appendChild(h("td", "mono-cell", ep.token || ""));
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  wrap.appendChild(table);
  return wrap;
}

function chipList(items, cls) {
  const box = h("div", "chips");
  if (!items || !items.length) { box.appendChild(h("span", "chip", "none")); return box; }
  for (const it of items) box.appendChild(h("span", "chip " + (cls || ""), String(it)));
  return box;
}

function renderManifest() {
  const body = el.manifestBody;
  body.replaceChildren();
  if (state.manifestStatus === 404 || !state.manifest) {
    const e = h("div", "empty");
    e.appendChild(icon("inbox"));
    e.appendChild(h("span", null, state.manifestStatus === 404 ? "no manifest (run devbox.py use <profile>)" : "manifest unavailable"));
    body.appendChild(e);
    el.manifestMeta.textContent = "n/a";
    return;
  }
  const m = state.manifest;
  const eps = Array.isArray(m.endpoints) ? m.endpoints : [];
  el.manifestMeta.textContent = eps.length + " endpoints";

  if (state.manifestView === "raw") {
    body.appendChild(h("pre", "json-block", JSON.stringify(m, null, 2)));
    return;
  }

  const grid = h("div", "metric-grid");
  grid.appendChild(metricBox("profile", m.profile));
  grid.appendChild(metricBox("mode", m.mode));
  grid.appendChild(metricBox("ingress", m.ingress_url));
  grid.appendChild(metricBox("project", m.project));
  grid.appendChild(metricBox("preview origin", m.preview_origin));
  grid.appendChild(metricBox("allowed ports", (m.allowed_ports || []).join(", ")));
  body.appendChild(grid);

  body.appendChild(h("div", "subhead", "endpoints"));
  body.appendChild(endpointsTable(eps));

  const ranges = (m.port_ranges || []).map((r) => Array.isArray(r) ? r[0] + "-" + r[1] : String(r));
  body.appendChild(h("div", "subhead", "port ranges"));
  body.appendChild(chipList(ranges, "accent"));
  body.appendChild(h("div", "subhead", "denied ports"));
  body.appendChild(chipList(m.port_deny || []));

  const cmds = (m.runner_commands || []).concat(m.scripts || []);
  if (cmds.length) {
    body.appendChild(h("div", "subhead", "runner commands & scripts"));
    const list = h("div", "endpoint-list");
    for (const c of cmds) {
      const row = h("div", "endpoint-row");
      row.appendChild(h("span", "endpoint-name", c.name || ""));
      row.appendChild(h("span", "endpoint-path", c.description || ""));
      row.appendChild(h("span", "auth-tag other", c.tool || ""));
      list.appendChild(row);
    }
    body.appendChild(list);
  }

  if (Array.isArray(m.bridge_tools) && m.bridge_tools.length) {
    body.appendChild(h("div", "subhead", "bridge tools"));
    body.appendChild(chipList(m.bridge_tools));
  }
}

function renderExposure() {
  const body = el.exposureBody;
  body.replaceChildren();
  if (state.exposureStatus === 404 || !state.exposure) {
    const e = h("div", "empty");
    e.appendChild(icon("inbox"));
    e.appendChild(h("span", null, state.exposureStatus === 404 ? "no active profile" : "exposure unavailable"));
    body.appendChild(e);
    el.exposureMeta.textContent = "n/a";
    return;
  }
  const x = state.exposure;
  const ports = x.allowed_ports || [];
  const eps = x.endpoints || [];
  el.exposureMeta.textContent = ports.length + " ports · " + eps.length + " endpoints";

  const p = x.profile || {};
  const grid = h("div", "metric-grid");
  grid.appendChild(metricBox("profile", p.name));
  grid.appendChild(metricBox("project dir", p.project_dir));
  body.appendChild(grid);

  body.appendChild(h("div", "subhead", "allowed ports"));
  const pg = h("div", "port-grid");
  if (!ports.length) pg.appendChild(h("span", "chip", "none"));
  for (const port of ports) {
    const cell = h("span", "port-cell");
    cell.appendChild(h("span", "hash", "#"));
    cell.appendChild(document.createTextNode(String(port)));
    pg.appendChild(cell);
  }
  body.appendChild(pg);

  body.appendChild(h("div", "subhead", "exposed endpoints"));
  const list = h("div", "endpoint-list");
  if (!eps.length) list.appendChild(h("span", "chip", "none"));
  for (const ep of eps) {
    const row = h("div", "endpoint-row");
    row.appendChild(h("span", "endpoint-name", ep.name || ""));
    row.appendChild(h("span", "endpoint-path", ep.path || ""));
    row.appendChild(authTag(ep.auth));
    list.appendChild(row);
  }
  body.appendChild(list);
}

function renderDiff(res) {
  const beat = el.diffBeat;
  const text = el.diffText;
  const d = res.ok ? res.data || {} : {};
  if (!res.ok || !d.git) {
    beat.className = "heartbeat na";
    text.replaceChildren(document.createTextNode("n/a (not a git repo)"));
    return;
  }
  beat.className = "heartbeat live";
  const lines = Array.isArray(d.porcelain) ? d.porcelain : [];
  if (!lines.length) {
    text.replaceChildren(document.createTextNode("clean"));
    return;
  }
  let untracked = 0;
  for (const line of lines) if (String(line).startsWith("??")) untracked += 1;
  const changed = lines.length - untracked;
  text.replaceChildren();
  if (changed) text.appendChild(h("span", "add", changed + " mod"));
  if (changed && untracked) text.appendChild(document.createTextNode(" · "));
  if (untracked) text.appendChild(h("span", "unt", untracked + " new"));
}

function setActionBusy(busy) {
  state.actionBusy = busy;
  for (const b of document.querySelectorAll("[data-action]")) b.disabled = busy;
  if (el.btnEditorSave) el.btnEditorSave.disabled = busy;
}

function setActionStatus(kind, text) {
  el.actionStatus.className = "action-status " + kind;
  const dotKind = kind === "ok" ? "ok" : kind === "err" ? "err" : kind === "run" ? "warn" : kind === "busy" ? "warn" : "idle";
  el.actionStatusDot.className = "dot " + dotKind;
  el.actionStatusText.textContent = text;
  el.actionMeta.textContent = kind === "run" ? "running" : kind === "busy" ? "busy" : kind === "ok" ? "done" : kind === "err" ? "error" : "single-flight";
}

function handleActionEvent(ev) {
  if (!ev || ev.kind !== "action") return;
  const tool = ev.tool || "action";
  const id = ev.job_id || "";
  if (ev.status === "started") {
    setActionBusy(true);
    setActionStatus("run", "running " + tool + "…");
  } else if (ev.status === "finished") {
    setActionBusy(false);
    const exit = ev.exit;
    const okExit = exit === 0 || exit == null;
    setActionStatus(okExit ? "ok" : "err", tool + " finished · exit " + (exit == null ? "?" : exit));
    if (id && id === state.submittedId) {
      state.submittedId = "";
      tick();
    }
  }
}

function connectEvents() {
  if (eventsSource || typeof EventSource === "undefined") return;
  try {
    eventsSource = new EventSource("/api/events");
  } catch {
    eventsSource = null;
    return;
  }
  eventsSource.onmessage = (e) => {
    let ev = null;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleActionEvent(ev);
  };
}

function actionBody(verb) {
  if (verb === "use") {
    const name = el.useName.value.trim();
    if (!name) { setActionStatus("err", "use: enter a profile name"); return null; }
    return { name };
  }
  if (verb === "allow") {
    const raw = el.allowPort.value.trim();
    const port = Number(raw);
    if (!raw || !Number.isInteger(port) || port < 1 || port > 65535) {
      setActionStatus("err", "allow: enter a valid port (1-65535)"); return null;
    }
    return { port, ui: el.allowUi.checked };
  }
  if (verb === "start") {
    const name = el.startName.value.trim();
    const body = { preview: el.startPreview.checked };
    if (name) body.name = name;
    return body;
  }
  return {};
}

async function runAction(verb) {
  if (state.actionBusy) { setActionStatus("busy", "busy — another action is already running"); return; }
  const body = actionBody(verb);
  if (body === null) return;
  const token = await ensureSessionToken();
  if (!token) { setActionStatus("err", "no session token — reload the cockpit"); return; }
  setActionBusy(true);
  setActionStatus("run", "submitting " + verb + "…");
  const res = await api("/api/action/" + encodeURIComponent(verb), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-RDM-Token": token },
    body: JSON.stringify(body),
  });
  if (res.status === 202 && res.data && res.data.action_id) {
    state.submittedId = res.data.action_id;
    setActionStatus("run", "accepted " + verb + " · " + res.data.action_id + " — running…");
  } else if (res.status === 409) {
    state.submittedId = "";
    setActionBusy(true);
    setActionStatus("busy", "busy — another action is already running (409)");
  } else {
    setActionBusy(false);
    const msg = res.data && res.data.error ? res.data.error : "rejected (" + res.status + ")";
    setActionStatus("err", verb + " · " + msg);
  }
}

function setEditorMsg(kind, text) {
  el.editorMsg.className = "editor-msg " + kind;
  el.editorMsg.textContent = text;
}

async function loadProfile() {
  const name = el.editorNameInput.value.trim();
  if (!name) { setEditorMsg("err", "enter a profile name to load"); return; }
  setEditorMsg("idle", "loading " + name + "…");
  const res = await api("/api/profile/" + encodeURIComponent(name));
  if (!res.ok) {
    const msg = res.status === 404 ? "profile not found" : res.data && res.data.error ? res.data.error : "load failed (" + res.status + ")";
    setEditorMsg("err", msg);
    return;
  }
  const d = res.data || {};
  state.editorName = d.name || name;
  state.editorPath = d.path || "";
  state.editorDirty = false;
  el.editorSource.textContent = "source: " + (d.source_dir || "—");
  el.editorSource.title = d.source_dir || "";
  el.editorArea.value = JSON.stringify(d.raw != null ? d.raw : {}, null, 2);
  el.editorMeta.textContent = state.editorName;
  setEditorMsg("ok", "loaded " + (d.path || name));
}

async function saveProfile() {
  const name = el.editorNameInput.value.trim() || state.editorName;
  if (!name) { setEditorMsg("err", "enter a profile name to save"); return; }
  let parsed;
  try {
    parsed = JSON.parse(el.editorArea.value);
  } catch (err) {
    setEditorMsg("err", "invalid JSON: " + err.message);
    return;
  }
  const token = await ensureSessionToken();
  if (!token) { setEditorMsg("err", "no session token — reload the cockpit"); return; }
  setEditorMsg("idle", "saving " + name + "…");
  const res = await api("/api/profile/" + encodeURIComponent(name), {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-RDM-Token": token },
    body: JSON.stringify(parsed),
  });
  if (res.ok) {
    state.editorDirty = false;
    state.editorName = name;
    el.editorMeta.textContent = name;
    setEditorMsg("ok", "saved " + (res.data && res.data.path ? res.data.path : name));
    state.profileLoaded = false;
    tick();
  } else {
    const msg = res.data && res.data.error ? res.data.error : "save failed (" + res.status + ")";
    setEditorMsg("err", msg);
  }
}

async function refreshProfileData(s) {
  const active = (s && s.profile && s.profile.active) || null;
  if (state.profileLoaded && state.lastProfile === active) return;
  state.lastProfile = active;
  const [m, e] = await Promise.all([api("/api/manifest"), api("/api/exposure")]);
  state.manifestStatus = m.status;
  state.manifest = m.ok ? m.data : null;
  state.exposureStatus = e.status;
  state.exposure = e.ok ? e.data : null;
  state.profileLoaded = true;
  renderManifest();
  renderExposure();
}

async function refreshLogs() {
  const res = await api("/api/logs" + qs({ source: state.logSource, filter: state.logFilter }));
  renderLogs(res);
}

async function tick() {
  if (state.inFlight) return;
  state.inFlight = true;
  try {
    const [statusRes, logsRes, diffRes] = await Promise.all([
      api("/api/status"),
      api("/api/logs" + qs({ source: state.logSource, filter: state.logFilter })),
      api("/api/diff"),
    ]);
    state.unreachable = statusRes.status === 0 || statusRes.status === 401;
    renderBanner();
    if (statusRes.ok && statusRes.data) {
      state.status = statusRes.data;
      renderVitals(statusRes.data);
      renderHero(statusRes.data);
      await refreshProfileData(statusRes.data);
    } else {
      renderVitals(null);
    }
    renderLogs(logsRes);
    renderDiff(diffRes);
  } finally {
    state.inFlight = false;
  }
}

function heartbeat() {
  state.beat += 1;
  renderClock();
  if (state.beat % POLL_EVERY === 0) tick();
}

function startPoller() {
  pollerCount += 1;
  if (pollerCount > 1) throw new Error("invariant: exactly one status poll loop");
  setInterval(heartbeat, BEAT_MS);
}

function jump(id) {
  const node = $(id);
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  node.classList.remove("flash-target");
  void node.offsetWidth;
  node.classList.add("flash-target");
  node.addEventListener("animationend", () => node.classList.remove("flash-target"), { once: true });
}

function setSource(src) {
  state.logSource = src;
  for (const b of document.querySelectorAll(".seg-btn")) {
    b.setAttribute("aria-pressed", String(b.dataset.source === src));
  }
  refreshLogs();
}

function feedback(btn) {
  if (!btn) return;
  btn.classList.remove("flash");
  void btn.offsetWidth;
  btn.classList.add("flash");
  btn.addEventListener("animationend", () => btn.classList.remove("flash"), { once: true });
}

async function copyText(text, btn) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    feedback(btn);
  } catch {
    feedback(btn);
  }
}

const COMMANDS = [
  { group: "Jump", label: "Go to Health", hint: "hero", icon: "activity", run: () => jump("section-health") },
  { group: "Jump", label: "Go to Log river", hint: "logs", icon: "terminal", run: () => jump("section-logs") },
  { group: "Jump", label: "Go to Hand-off", hint: "secrets", icon: "lock", run: () => jump("section-handoff") },
  { group: "Jump", label: "Go to Manifest", hint: "endpoints", icon: "table", run: () => jump("section-manifest") },
  { group: "Jump", label: "Go to Exposure map", hint: "ports", icon: "map", run: () => jump("section-exposure") },
  { group: "Jump", label: "Go to Actions", hint: "control", icon: "zap", run: () => jump("section-actions") },
  { group: "Jump", label: "Go to Profile editor", hint: "control", icon: "folder", run: () => jump("section-editor") },
  { group: "Actions", label: "Refresh now", hint: "re-poll", icon: "refresh", run: () => tick() },
  { group: "Actions", label: "Toggle hand-off reveal", hint: "show/hide", icon: "eye", run: () => toggleReveal() },
  { group: "Actions", label: "Copy hand-off block", hint: "clipboard", icon: "copy", run: () => copyText(state.handoffText, el.btnCopyHandoff) },
  { group: "Actions", label: "Copy INGRESS URL", hint: "clipboard", icon: "link", run: () => copyText(state.ingressUrl, el.ingressCopy) },
  { group: "Actions", label: "Focus log filter", hint: "search", icon: "search", run: () => { jump("section-logs"); el.logFilter.focus(); } },
  { group: "Devbox", label: "Run doctor", hint: "diagnostics", icon: "activity", run: () => runAction("doctor") },
  { group: "Devbox", label: "Issue tokens", hint: "rotate", icon: "lock", run: () => runAction("issue-tokens") },
  { group: "Devbox", label: "Ingress start", hint: "tunnel", icon: "globe", run: () => runAction("ingress-start") },
  { group: "Devbox", label: "Ingress stop", hint: "tunnel", icon: "globe", run: () => runAction("ingress-stop") },
  { group: "Devbox", label: "Stop host services", hint: "host", icon: "terminal", run: () => runAction("stop-host") },
  { group: "Devbox", label: "Down (whole stack)", hint: "destructive", icon: "alert", run: () => runAction("down") },
  { group: "Editor", label: "Load profile", hint: "fetch", icon: "refresh", run: () => loadProfile() },
  { group: "Editor", label: "Save profile", hint: "put", icon: "save", run: () => saveProfile() },
  { group: "Log source", label: "Logs: all", icon: "terminal", run: () => setSource("") },
  { group: "Log source", label: "Logs: host", icon: "terminal", run: () => setSource("host") },
  { group: "Log source", label: "Logs: ingress", icon: "terminal", run: () => setSource("ingress") },
  { group: "Log source", label: "Logs: watchdog", icon: "terminal", run: () => setSource("watchdog") },
  { group: "Log source", label: "Logs: runner", icon: "terminal", run: () => setSource("runner") },
];

let paletteItems = [];
let paletteIndex = 0;

function filteredCommands(q) {
  const query = q.trim().toLowerCase();
  if (!query) return COMMANDS;
  return COMMANDS.filter((c) => (c.label + " " + c.group + " " + (c.hint || "")).toLowerCase().includes(query));
}

function renderPalette() {
  const q = el.paletteInput.value;
  paletteItems = filteredCommands(q);
  paletteIndex = 0;
  const list = el.paletteList;
  list.replaceChildren();
  if (!paletteItems.length) {
    list.appendChild(h("div", "palette-empty", "no matching commands"));
    return;
  }
  let lastGroup = null;
  paletteItems.forEach((cmd, i) => {
    if (cmd.group !== lastGroup) {
      list.appendChild(h("div", "palette-section", cmd.group));
      lastGroup = cmd.group;
    }
    const item = h("button", "palette-item");
    item.type = "button";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(i === paletteIndex));
    item.dataset.index = String(i);
    const ic = h("span", "pi-icon");
    ic.appendChild(icon(cmd.icon));
    item.appendChild(ic);
    item.appendChild(h("span", "pi-label", cmd.label));
    if (cmd.hint) item.appendChild(h("span", "pi-hint", cmd.hint));
    item.addEventListener("click", () => runPalette(i));
    item.addEventListener("mousemove", () => selectPalette(i));
    list.appendChild(item);
  });
}

function selectPalette(i) {
  if (i < 0 || i >= paletteItems.length) return;
  paletteIndex = i;
  const nodes = el.paletteList.querySelectorAll(".palette-item");
  nodes.forEach((n) => n.setAttribute("aria-selected", String(Number(n.dataset.index) === i)));
  const active = el.paletteList.querySelector('.palette-item[data-index="' + i + '"]');
  if (active) active.scrollIntoView({ block: "nearest" });
}

function movePalette(delta) {
  if (!paletteItems.length) return;
  selectPalette((paletteIndex + delta + paletteItems.length) % paletteItems.length);
}

function runPalette(i) {
  const cmd = paletteItems[i];
  if (!cmd) return;
  closePalette();
  cmd.run();
}

function openPalette() {
  el.paletteOverlay.classList.add("open");
  el.paletteInput.value = "";
  renderPalette();
  el.paletteInput.focus();
}

function closePalette() {
  el.paletteOverlay.classList.remove("open");
}

function cacheDom() {
  const map = {
    vitalHealthDot: "vital-health-dot", vitalHealthText: "vital-health-text",
    portCockpit: "port-cockpit", portIngress: "port-ingress", portBridge: "port-bridge", portRunner: "port-runner",
    diffBeat: "diff-beat", diffText: "diff-text", clock: "clock",
    banner: "banner", bannerText: "banner-text",
    healthBadge: "health-badge", healthBadgeText: "health-badge-text",
    healthSummary: "health-summary", healthBreakdown: "health-breakdown",
    profileName: "profile-name", profileSource: "profile-source",
    modeBadge: "mode-badge", toolchainChips: "toolchain-chips",
    ingressUrl: "ingress-url", ingressCopy: "ingress-copy", watchdogState: "watchdog-state",
    logRiver: "log-river", logMeta: "log-meta", logFilter: "log-filter", logLive: "log-live",
    handoffPre: "handoff-pre", handoffBadge: "handoff-badge", btnReveal: "btn-reveal",
    revealLabel: "reveal-label", btnCopyHandoff: "btn-copy-handoff",
    manifestBody: "manifest-body", manifestMeta: "manifest-meta", tabStructured: "tab-structured", tabRaw: "tab-raw",
    exposureBody: "exposure-body", exposureMeta: "exposure-meta",
    actionStatus: "action-status", actionStatusDot: "action-status-dot", actionStatusText: "action-status-text", actionMeta: "action-meta",
    useName: "use-name", allowPort: "allow-port", allowUi: "allow-ui", startName: "start-name", startPreview: "start-preview",
    editorNameInput: "editor-name", editorSource: "editor-source", editorArea: "editor-area", editorMsg: "editor-msg", editorMeta: "editor-meta",
    btnEditorLoad: "btn-editor-load", btnEditorSave: "btn-editor-save",
    btnRefresh: "btn-refresh", btnPalette: "btn-palette",
    paletteOverlay: "palette-overlay", paletteInput: "palette-input", paletteList: "palette-list",
  };
  for (const [k, id] of Object.entries(map)) el[k] = $(id);
}

function setManifestView(view) {
  state.manifestView = view;
  el.tabStructured.setAttribute("aria-selected", String(view === "structured"));
  el.tabRaw.setAttribute("aria-selected", String(view === "raw"));
  renderManifest();
}

function wireEvents() {
  el.btnRefresh.addEventListener("click", () => tick());
  el.btnPalette.addEventListener("click", openPalette);

  for (const b of document.querySelectorAll(".seg-btn")) {
    b.addEventListener("click", () => setSource(b.dataset.source || ""));
  }
  el.logFilter.addEventListener("input", () => { state.logFilter = el.logFilter.value; refreshLogs(); });

  el.btnReveal.addEventListener("click", () => toggleReveal());
  el.btnCopyHandoff.addEventListener("click", () => copyText(state.handoffText, el.btnCopyHandoff));
  el.ingressCopy.addEventListener("click", () => copyText(state.ingressUrl, el.ingressCopy));

  el.tabStructured.addEventListener("click", () => setManifestView("structured"));
  el.tabRaw.addEventListener("click", () => setManifestView("raw"));

  for (const b of document.querySelectorAll("[data-action]")) {
    b.addEventListener("click", () => runAction(b.dataset.action));
  }
  el.btnEditorLoad.addEventListener("click", loadProfile);
  el.btnEditorSave.addEventListener("click", saveProfile);
  el.editorArea.addEventListener("input", () => { state.editorDirty = true; });
  el.editorNameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); loadProfile(); } });

  el.paletteInput.addEventListener("input", renderPalette);
  el.paletteInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
    else if (e.key === "Enter") { e.preventDefault(); runPalette(paletteIndex); }
    else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  });
  el.paletteOverlay.addEventListener("mousedown", (e) => { if (e.target === el.paletteOverlay) closePalette(); });

  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable);
    if (e.key === "/" && !typing) { e.preventDefault(); openPalette(); }
  });
}

function boot() {
  if (new URLSearchParams(location.search).has("t")) {
    try { history.replaceState(null, "", "/"); } catch {}
  }
  cacheDom();
  wireEvents();
  renderClock();
  renderVitals(null);
  setActionStatus("idle", "idle — no action running");
  loadHandoffMasked();
  connectEvents();
  tick();
  startPoller();
}

boot();
