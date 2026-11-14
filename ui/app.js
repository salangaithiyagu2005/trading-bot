const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const reasonOptions = new Set();
let tradeMode = "paper";
let pollTimer;
let currentSettings;
let currentSession;
let structuredRules;

function notice(message, error = false) {
  const node = $("#notice");
  node.textContent = message || "";
  node.classList.toggle("error", error);
  if (message) setTimeout(() => { if (node.textContent === message) node.textContent = ""; }, 6000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function setText(selector, value) { $(selector).textContent = value == null || value === "" ? "—" : String(value); }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function element(name, text, className) {
  const node = document.createElement(name);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function showPanel(name) {
  $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${name}`));
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.panel === name));
  if (name === "tape") loadEvents();
  if (name === "trade") loadTrades();
  if (name === "rules") loadRules();
  if (name === "settings") loadSettings();
}

async function loadSession() {
  try {
    const data = await api("/api/session");
    currentSession = data;
    if (!currentSettings) currentSettings = { scheduled: { risk: { trading_mode: data.scheduled_mode } } };
    else currentSettings.scheduled.risk.trading_mode = data.scheduled_mode;
    const banner = $("#mode-banner");
    const live = data.active_mode === "live";
    banner.textContent = live ? "LIVE CONFIGURATION BLOCKED · NO ORDERS" : "PAPER MODE (Simulated)";
    banner.className = `mode-banner ${live ? "live" : "paper"}`;
    setText("#state", data.state);
    setText("#block-reason", data.block_reason ? `Blocked: ${data.block_reason}` : "");
    setText("#bias", data.bias);
    setText("#bias-score", data.score == null ? "" : `Score ${data.score}${data.veto ? ` · ${data.veto}` : ""}`);
    setText("#token-state", data.token_status?.status);
    setText("#token-expiry", data.token_status?.expires_at ? `Expires ${data.token_status.expires_at}` : "");
    setText("#active-mode", data.active_mode);
    setText("#next-mode", `Next session: ${data.scheduled_mode}`);
    setText("#risk-cap", `₹${data.max_risk_inr_per_trade}`);
    setText("#risk-preview", data.risk_inr == null ? "—" : `₹${data.risk_inr}`);
    renderHealth(data);
    renderRows($("#bias-lines"), data.bias_lines || [], ["name", "points", "detail"]);
    $("#arm-button").disabled = !["IDLE", "RESEARCH", "WATCHING"].includes(data.state)
      || data.health?.token_verify_status !== "ok";
    $("#mode-button").textContent = data.scheduled_mode === "live" ? "Switch to Paper (Next Session)" : "Enable Live (Next Session)";
    $("#mode-button").disabled = !data.server_live_allowed && data.scheduled_mode !== "live";
    $("#mode-button").title = data.server_live_allowed
      ? ""
      : data.scheduled_mode === "live"
        ? "Live execution is unavailable; switch back to paper mode."
        : "Live order lifecycle is not implemented.";
    renderChips($("#sectors"), data.winning_sectors || []);
    renderRows($("#shortlist"), data.shortlist || [], ["tradingsymbol", "sector", "chg_pct", "rel_strength"]);
    renderTable($("#top20"), data.top20 || [], ["tradingsymbol", "sector", "chg_pct", "rel_strength", "anti_chase_ok"]);
    setText("#last-updated", `Updated ${new Date().toLocaleTimeString()}`);
  } catch (error) { notice(error.message, true); }
}

function renderChips(node, items) {
  clear(node);
  items.forEach((item) => node.append(element("span", typeof item === "string" ? item : item.tradingsymbol, "chip")));
}

function renderHealth(session) {
  const health = session.health || {};
  const tokenState = session.token_status?.status || "MISSING";
  const verified = health.token_verify_status === "ok";
  const tokenTone = tokenState === "PRESENT" && verified ? "green"
    : tokenState === "PRESENT" ? "amber" : "red";
  let tickerText;
  let tickerTone;
  if (!health.calendar_valid) {
    tickerText = "Market status unavailable · Calendar invalid";
    tickerTone = "red";
  } else if (!health.market_hours) {
    tickerText = health.ws_connected ? "Ticker connected · Market closed" : "Idle · Market closed";
    tickerTone = "amber";
  } else if (health.ticker_live) {
    tickerText = `Ticker live · ${health.last_tick_age_sec}s · ${health.ws_subscribed_count} tokens · ${health.tick_count} ticks · Spot ${health.nifty_spot_last ?? "—"} / Fut ${health.nifty_fut_last ?? "—"}`;
    tickerTone = "green";
  } else {
    tickerText = health.ws_connected ? "Ticker stale" : "Ticker disconnected";
    tickerTone = "red";
  }
  const pills = [
    { tone: health.engine_alive ? "green" : "red", text: health.engine_alive ? `Engine alive · ${health.heartbeat_age_sec}s` : "Engine offline / stale" },
    { tone: tokenTone, text: tokenState === "PRESENT"
      ? `Token valid · ${verified ? "Verified OK" : "Unverified"}${health.available_margin_inr ? ` · ₹${health.available_margin_inr}` : ""}`
      : `${tokenState} token${health.token_verify_error ? ` · ${health.token_verify_error}` : ""}` },
    { tone: tickerTone, text: tickerText },
    { tone: health.warmup_status === "ok" ? "green" : health.warmup_status === "failed" ? "red" : "amber",
      text: `Warmup ${health.warmup_status || "pending"} · 1m ${health.last_1m_bar_ts || "—"} · OR ${health.or_complete ? "complete" : "pending"}` },
  ];
  const strip = $("#health-strip");
  clear(strip);
  pills.forEach((pill) => strip.append(element("div", pill.text, `health-pill ${pill.tone}`)));
}

function renderRows(node, rows, columns) {
  clear(node);
  if (!rows.length) { node.append(element("span", "No shortlist yet.", "muted")); return; }
  rows.forEach((row) => {
    node.append(element("div", columns.map((key) => `${key}: ${row[key] ?? "—"}`).join(" · "), "list-row"));
  });
}

function renderTable(node, rows, columns, onClick) {
  clear(node);
  if (!rows.length) { node.append(element("p", "No data available.", "muted")); return; }
  const table = element("table");
  const thead = element("thead");
  const header = element("tr");
  columns.forEach((column) => header.append(element("th", column.replaceAll("_", " "))));
  thead.append(header);
  const tbody = element("tbody");
  rows.forEach((row) => {
    const tr = element("tr");
    columns.forEach((column) => tr.append(element("td", row[column] ?? "—")));
    if (onClick) tr.addEventListener("click", () => onClick(row));
    tbody.append(tr);
  });
  table.append(thead, tbody);
  node.append(table);
}

async function loadEvents() {
  try {
    const reason = $("#reason-filter").value;
    const events = await api(`/api/events${reason ? `?reason=${encodeURIComponent(reason)}` : ""}`);
    const node = $("#events");
    clear(node);
    events.forEach((item) => {
      reasonOptions.add(item.reason);
      const event = element("article", undefined, "event");
      event.append(element("time", item.ts), element("span", item.reason, "reason"), element("span", item.reason_text));
      node.append(event);
    });
    const select = $("#reason-filter");
    const selected = select.value;
    clear(select);
    select.append(new Option("All reasons", ""));
    [...reasonOptions].sort().forEach((reason) => select.append(new Option(reason, reason)));
    select.value = selected;
  } catch (error) { notice(error.message, true); }
}

async function loadTrades() {
  try {
    const data = await api(`/api/trades?mode=${tradeMode}&limit=50`);
    const summary = $("#trade-summary");
    clear(summary);
    Object.entries(data.summary || {}).forEach(([key, value]) => {
      const card = element("article", undefined, "card");
      card.append(element("span", key.replaceAll("_", " ")), element("strong", String(value)));
      summary.append(card);
    });
    renderTable($("#trades-table"), data.trades || [],
      ["day", "mode", "underlying", "symbol", "entry_price", "exit_price", "net_pnl"],
      (row) => loadTrade(row.day));
  } catch (error) { notice(error.message, true); }
}

async function loadTrade(day) {
  try {
    const trade = await api(`/api/trade?day=${encodeURIComponent(day)}`);
    $("#trade-detail").textContent = trade ? JSON.stringify(trade, null, 2) : "No filled trade.";
  } catch (error) { notice(error.message, true); }
}

async function loadRules() {
  try {
    const result = await api("/api/rules");
    $("#rules-yaml").value = result.scheduled || "";
    $("#locked-rules").textContent = result.locked ? JSON.stringify(result.locked, null, 2) : "No session rules latched.";
  } catch (error) { notice(error.message, true); }
}

async function loadSettings() {
  try {
    currentSettings = await api("/api/settings");
    const data = currentSettings;
    setText("#token-summary", `${data.token_status.status} · user ${data.token_status.user_id || "—"} · created ${data.token_status.created_at || "—"}`);
    $("#token-summary").textContent += ` · ${data.health?.token_verify_status || "unverified"}${data.health?.token_verified_at ? ` at ${data.health.token_verified_at}` : ""}${data.health?.available_margin_inr ? ` · margin ₹${data.health.available_margin_inr}` : ""}${data.health?.token_verify_error ? ` · ${data.health.token_verify_error}` : ""}`;
    $("#credential-status").textContent = JSON.stringify(data.credentials, null, 2);
    $("#redirect-url").value = data.app_settings.kite_redirect_url || "";
    $("#poll-interval").value = data.app_settings.ui_poll_interval_ms || "2000";
    const file = data.data_files[$("#datafile-name").value];
    $("#datafile-content").value = file?.content || "";
    setText("#datafile-count", `${file?.count || 0} non-comment entries`);
    structuredRules = JSON.parse(JSON.stringify(data.scheduled));
    renderStructuredRules(structuredRules);
  } catch (error) { notice(error.message, true); }
}

function renderStructuredRules(documentData) {
  const form = $("#structured-form");
  clear(form);
  ["session", "calendar", "risk", "universe", "screener", "bias", "sets", "costs"].forEach((section) => {
    const fieldset = element("fieldset");
    fieldset.append(element("legend", section));
    Object.entries(documentData[section] || {}).forEach(([key, value]) => {
      const label = element("label", undefined, "config-field");
      label.append(document.createTextNode(key.replaceAll("_", " ")));
      let input;
      if (typeof value === "boolean") {
        input = element("select");
        input.append(new Option("true", "true"), new Option("false", "false"));
        input.value = String(value);
        input.dataset.kind = "boolean";
      } else {
        input = element("input");
        if (typeof value === "number") {
          input.type = "number";
          input.step = Number.isInteger(value) ? "1" : "any";
          input.value = String(value);
          input.dataset.kind = "number";
        } else if (Array.isArray(value)) {
          input.type = "text";
          input.value = value.join(", ");
          input.dataset.kind = "array";
        } else {
          input.type = "text";
          input.value = String(value);
          input.dataset.kind = "string";
        }
      }
      input.dataset.path = `${section}.${key}`;
      label.append(input);
      fieldset.append(label);
    });
    form.append(fieldset);
  });
}

function collectStructuredRules() {
  const documentData = JSON.parse(JSON.stringify(structuredRules));
  $$("#structured-form [data-path]").forEach((input) => {
    const [section, key] = input.dataset.path.split(".");
    if (input.dataset.kind === "boolean") documentData[section][key] = input.value === "true";
    else if (input.dataset.kind === "number") documentData[section][key] = Number(input.value);
    else if (input.dataset.kind === "array") {
      documentData[section][key] = input.value.split(",").map((part) => part.trim()).filter(Boolean);
    } else documentData[section][key] = input.value;
  });
  return documentData;
}

function configurePolling() {
  if (pollTimer) clearInterval(pollTimer);
  const interval = Number(currentSettings?.app_settings?.ui_poll_interval_ms || 2000);
  pollTimer = setInterval(loadSession, Number.isFinite(interval) ? interval : 2000);
}

function bindActions() {
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => showPanel(tab.dataset.panel)));
  $("#reason-filter").addEventListener("change", loadEvents);
  $$("[data-mode]").forEach((button) => button.addEventListener("click", () => {
    $$("[data-mode]").forEach((item) => item.classList.toggle("active", item === button));
    tradeMode = button.dataset.mode;
    loadTrades();
  }));
  $("#arm-button").addEventListener("click", async () => {
    if (!confirm("Arm the strategy for this session?")) return;
    try { await api("/api/arm", { method: "POST" }); notice("Arm request queued."); loadSession(); }
    catch (error) { notice(error.message, true); }
  });
  $("#kill-button").addEventListener("click", async () => {
    if (!confirm("Kill the strategy and flatten managed positions?")) return;
    if (!confirm("Confirm kill switch. This cannot be undone for today.")) return;
    try { await api("/api/kill", { method: "POST" }); notice("Kill request queued."); loadSession(); }
    catch (error) { notice(error.message, true); }
  });
  $("#mode-button").addEventListener("click", async () => {
    const next = currentSettings?.scheduled?.risk?.trading_mode === "live" ? "paper" : "live";
    if (next === "live" && (currentSession?.paper_trade_count || 0) < 20) {
      if (!confirm("Fewer than 20 paper trades are recorded. Live trading is not recommended. Continue?")) return;
    }
    if (next === "live" && !confirm("Enable live orders for the next session?")) return;
    if (!confirm(`Confirm ${next} mode for the next session.`)) return;
    try {
      await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: next, confirm: true }) });
      notice(`Scheduled ${next} mode. It will not change this session.`);
      await loadSession();
    } catch (error) { notice(error.message, true); }
  });
  $("#save-rules").addEventListener("click", () => saveRules($("#rules-yaml").value, $("#rules-note").value));
  $("#save-structured-rules").addEventListener("click", () => {
    try { saveRules(JSON.stringify(collectStructuredRules()), $("#structured-note").value); }
    catch (error) { notice(`Invalid rules: ${error.message}`, true); }
  });
  $("#datafile-name").addEventListener("change", () => {
    const file = currentSettings?.data_files?.[$("#datafile-name").value];
    $("#datafile-content").value = file?.content || "";
    setText("#datafile-count", `${file?.count || 0} non-comment entries`);
  });
  $("#save-datafile").addEventListener("click", async () => {
    try {
      await api("/api/config/datafile", { method: "POST", body: JSON.stringify({
        name: $("#datafile-name").value, content: $("#datafile-content").value,
      }) });
      notice("Data file saved for the next session.");
      await loadSettings();
    } catch (error) { notice(error.message, true); }
  });
  $("#save-runtime").addEventListener("click", async () => {
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({
        kite_redirect_url: $("#redirect-url").value,
        ui_poll_interval_ms: Number($("#poll-interval").value),
      }) });
      notice("Runtime settings saved.");
      await loadSettings();
      configurePolling();
    } catch (error) { notice(error.message, true); }
  });
  $("#save-credentials").addEventListener("click", async () => {
    const body = {};
    $$("[data-credential]").forEach((input) => { if (input.value) body[input.dataset.credential] = input.value; });
    try {
      await api("/api/config/credentials", { method: "POST", body: JSON.stringify(body) });
      $$("[data-credential]").forEach((input) => { input.value = ""; });
      notice("Credentials saved; secret values are not returned.");
      await loadSettings();
    } catch (error) { notice(error.message, true); }
  });
  $("#save-token").addEventListener("click", async () => {
    try {
      await api("/api/token", { method: "POST", body: JSON.stringify({
        mode: "access_token", token: $("#access-token").value, user_id: $("#token-user").value,
      }) });
      $("#access-token").value = "";
      notice("Token saved locally; no Kite API call was made.");
      await loadSettings();
    } catch (error) { notice(error.message, true); }
  });
  $("#exchange-token").addEventListener("click", async () => {
    try {
      await api("/api/token", { method: "POST", body: JSON.stringify({
        mode: "request_token", token: $("#request-token").value,
      }) });
      $("#request-token").value = "";
      notice("Request token exchanged.");
      await loadSettings();
    } catch (error) { notice(error.message, true); }
  });
  $("#clear-token").addEventListener("click", async () => {
    if (!confirm("Clear the local Kite access token?")) return;
    try { await api("/api/token", { method: "POST", body: JSON.stringify({ mode: "clear" }) }); notice("Token cleared."); await loadSettings(); }
    catch (error) { notice(error.message, true); }
  });
  $("#verify-token").addEventListener("click", async () => {
    try { await api("/api/token/verify", { method: "POST" }); notice("Token verification queued."); }
    catch (error) { notice(error.message, true); }
  });
  $("#draft-review").addEventListener("click", async () => {
    try {
      const result = await api("/api/review/draft", { method: "POST", body: JSON.stringify({ day: $("#review-day").value }) });
      $("#review-note").value = result.draft;
      $$("#review-tags input").forEach((input) => { input.checked = result.tags.includes(input.value); });
      notice("AI draft ready. Review it before saving.");
    } catch (error) { notice(error.message, true); }
  });
  $("#save-review").addEventListener("click", async () => {
    try {
      const tags = $$("#review-tags input:checked").map((input) => input.value);
      await api("/api/review", { method: "POST", body: JSON.stringify({
        day: $("#review-day").value, tags, note: $("#review-note").value, source: "operator",
      }) });
      notice("Review saved.");
    } catch (error) { notice(error.message, true); }
  });
}

async function saveRules(documentText, note) {
  try {
    await api("/api/rules", { method: "POST", body: JSON.stringify({ yaml: documentText, note }) });
    notice("Rules saved for next session.");
    await loadRules();
    await loadSettings();
  } catch (error) { notice(error.message, true); }
}

async function initialize() {
  const tags = [
    "bias_wrong", "late_confirm", "chop_after_entry", "spread_too_wide", "sl_too_tight",
    "target_too_far", "structure_exit_saved_me", "structure_exit_too_early",
    "expiry_noise", "news_spike", "no_signal_correct_skip", "would_have_worked_if_chased",
  ];
  tags.forEach((tag) => {
    const label = element("label");
    const input = element("input");
    input.type = "checkbox"; input.value = tag;
    label.append(input, document.createTextNode(tag.replaceAll("_", " ")));
    $("#review-tags").append(label);
  });
  bindActions();
  await loadSettings();
  await loadSession();
  $("#review-day").value = currentSession?.day || new Date().toISOString().slice(0, 10);
  configurePolling();
}

document.addEventListener("DOMContentLoaded", initialize);
