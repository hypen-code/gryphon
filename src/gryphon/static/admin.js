"use strict";
const $ = (id) => document.getElementById(id);
const state = { csrf: "", tenant: "", tenants: [], specs: [], channels: [], settings: null, editing: null, source: null, confirm: null, epoch: 0, busy: false };
const pages = { overview: ["Overview", "A clear view of your channels, capabilities, and activity."], channels: ["Channels", "Give each connection exactly the capabilities it needs."], specs: ["API specifications", "Versioned API catalogs, ready to bind to your channels."], audit: ["Audit trail", "A transparent record of administrative changes across tenants."] };
const errors = {
  invalid_token: "The administrator token was not accepted.",
  csrf: "Your security session is out of date. Refresh the page and sign in again.",
  invalid_csrf: "Your security session is out of date. Refresh the page and sign in again.",
  validation: "Check the document format, unique API binding names, and sandbox/library policy. The submitted configuration was not accepted.",
  invalid_spec: "The specification could not be validated. Check its OpenAPI or Swagger structure.",
  invalid_request: "The request was not accepted. Check the form values.",
  sandbox_unavailable: "Docker execution is unavailable. Ask the operator to check its configuration.",
  not_found: "This resource no longer exists. Refresh the workspace.",
  conflict: "The resource changed or the name is already in use. Refresh and try again.",
  quota_exceeded: "The workspace has reached its configured resource limit.",
  capacity: "The service has reached a configured capacity limit. Wait before retrying or contact the operator.",
  rate_limit: "Too many requests. Wait a moment before trying again.",
  request_timeout: "The request timed out. Refresh to check whether a change was applied before retrying.",
  tenant_disabled: "This tenant is disabled. Enable it before using its channels."
};
const count = (value) => new Intl.NumberFormat().format(value);
const segment = (value) => encodeURIComponent(value);
const tenantPath = (tail = "") => `/api/tenants/${segment(state.tenant)}${tail}`;
const channelPath = (channel, tail = "") => tenantPath(`/channels/${segment(channel.id)}${tail}`);
const node = (tag, text = "", className = "") => { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; };
const successStatus = (status) => ["success", "succeeded", "ok", "completed"].includes(status);
function notify(message, error = false) {
  $("notice").textContent = message; $("notice").classList.toggle("error", error); $("notice").hidden = !message;
  const target = document.querySelector("dialog[open] .dialog-message");
  if (target) { target.textContent = message; target.classList.toggle("error", error); }
}
function showDialog(id) {
  const dialog = $(id); const message = dialog.querySelector(".dialog-message");
  if (message) message.textContent = "";
  dialog.showModal();
}
function clearKey() { $("channel-token").value = ""; $("channel-token").type = "password"; $("reveal-key").textContent = "Reveal key"; $("reveal-key").setAttribute("aria-pressed", "false"); }
function signedOut() {
  state.epoch += 1; state.csrf = ""; state.tenant = ""; state.tenants = []; state.channels = []; state.specs = []; state.settings = null; state.source = null; state.confirm = null;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); clearKey();
  $("login-token").value = ""; $("source-content").textContent = ""; $("app").hidden = true; $("login").hidden = false; $("login-token").focus();
}
async function api(path, method = "GET", body) {
  const epoch = state.epoch;
  const headers = { Accept: "application/json" };
  if (method !== "GET") { headers["Content-Type"] = "application/json"; if (state.csrf) headers["X-CSRF-Token"] = state.csrf; }
  let response;
  try { response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(30000), ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); }
  catch { throw new Error("The server could not be reached. Check your connection, then refresh before retrying a change."); }
  if (epoch !== state.epoch) throw new Error("Workspace changed. Refresh to see the latest data.");
  const result = await response.json().catch(() => ({}));
  if (epoch !== state.epoch) throw new Error("Workspace changed. Refresh to see the latest data.");
  if (response.status === 401) { if (path !== "/api/login") signedOut(); throw new Error(path === "/api/login" ? "The administrator token was not accepted." : "Sign in to continue. Your session may have expired."); }
  if (!response.ok) {
    const fallback = { 400: "Check your input and try again.", 403: "This action was denied. Refresh your session or check operator policy.", 404: "Resource not found. Refresh the workspace.", 409: "A conflicting change was detected. Refresh and try again.", 413: "This specification exceeds the upload size limit.", 422: "The submitted configuration is not valid.", 429: "Too many requests. Wait a moment and try again.", 503: "The service is temporarily unavailable. Try again shortly." };
    const category = Object.hasOwn(errors, result.error) ? errors[result.error] : "";
    throw new Error(`${category || fallback[response.status] || "The request failed. Refresh and try again."} (HTTP ${response.status})`);
  }
  return result;
}
async function run(work, trigger) {
  if (state.busy) return;
  state.busy = true; if (trigger) trigger.disabled = true; $("tenant-select").disabled = true;
  try { await work(); } catch (error) { notify(error.message || "Unable to complete this action.", true); }
  finally { state.busy = false; if (trigger) trigger.disabled = false; $("tenant-select").disabled = !state.tenants.length; }
}
function bind(id, work, event = "click") {
  $(id).addEventListener(event, (e) => { e.preventDefault(); run(() => work(e), event === "submit" ? e.submitter : e.currentTarget); });
}
function page() {
  const key = Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  Object.keys(pages).forEach((name) => { $(`page-${name}`).hidden = name !== key; document.querySelector(`[data-page="${name}"]`).toggleAttribute("aria-current", name === key); });
  document.querySelector(`[data-page="${key}"]`).setAttribute("aria-current", "page");
  $("page-title").textContent = pages[key][0]; $("page-description").textContent = pages[key][1];
  $("tenant-empty").hidden = !!state.tenant || key === "audit"; $("tenant-content").hidden = !state.tenant;
}
function renderTenants() {
  const select = $("tenant-select"); select.replaceChildren();
  if (!state.tenants.length) select.append(new Option("No tenants yet", ""));
  state.tenants.forEach((tenant) => select.append(new Option(`${tenant.name}${tenant.enabled ? "" : " · disabled"}`, tenant.id)));
  select.value = state.tenant;
  const tenant = state.tenants.find((item) => item.id === state.tenant);
  $("tenant-heading").textContent = tenant?.name || "No tenant selected";
  $("tenant-status").textContent = tenant ? (tenant.enabled ? "Tenant enabled" : "Tenant disabled") : "No tenant selected";
  $("tenant-status").className = `badge ${tenant ? (tenant.enabled ? "" : "negative") : "neutral"}`;
  $("tenant-context").textContent = tenant ? (tenant.enabled ? "Channel access follows each channel's policy." : "All channel access is blocked until this tenant is enabled.") : "Create a tenant to get started.";
  $("toggle-tenant").disabled = !tenant; $("toggle-tenant").textContent = tenant?.enabled ? "Disable tenant" : "Enable tenant"; page();
}
async function refresh() {
  $("loading").hidden = false; $("main").setAttribute("aria-busy", "true");
  try {
    const [settings, tenants, audit] = await Promise.all([api("/api/settings"), api("/api/tenants"), api("/api/audit")]);
    state.settings = settings; state.tenants = tenants.items;
    if (!state.tenants.some((tenant) => tenant.id === state.tenant)) state.tenant = state.tenants[0]?.id || "";
    renderTenants(); renderAudit(audit.items); await loadTenant(); notify("Workspace is up to date.");
  } catch (error) { emptyRow("audit-rows", 4, "Audit data may be unavailable. Use Refresh data to retry."); throw error; }
  finally { $("loading").hidden = true; $("main").removeAttribute("aria-busy"); }
}
async function loadTenant() {
  state.specs = []; state.channels = []; renderSpecs(); renderChannels(); renderUsage([]);
  if (!state.tenant) return;
  $("loading").hidden = false;
  try {
    const [specs, channels, usage] = await Promise.all([api(tenantPath("/specs")), api(tenantPath("/channels")), api(tenantPath("/usage"))]);
    state.specs = specs.items; state.channels = channels.items; renderSpecs(); renderChannels(); renderUsage(usage.items);
  } catch (error) {
    emptyRow("spec-rows", 3, "Specifications unavailable. Use Refresh data to retry."); emptyRow("usage-rows", 4, "Usage unavailable. Use Refresh data to retry.");
    $("channel-list").replaceChildren(node("p", "Channels unavailable. Use Refresh data to retry.", "empty-state"));
    ["metric-calls", "metric-success", "metric-latency", "metric-channels"].forEach((id) => { $(id).textContent = "—"; });
    $("usage-chart").replaceChildren(node("p", "Usage unavailable. Refresh to retry.", "empty-cell")); throw error;
  } finally { $("loading").hidden = true; }
}
async function enter(session) {
  state.csrf = session.csrf_token; $("login").hidden = true; $("app").hidden = false; $("main").focus(); await refresh();
}
function action(label, work, danger = false) {
  const button = node("button", label, `text-button${danger ? " danger-text" : ""}`); button.type = "button";
  button.addEventListener("click", () => run(work, button)); return button;
}
function emptyRow(id, columns, text) {
  const row = node("tr"); const cell = node("td", text, "empty-cell"); cell.colSpan = columns; row.append(cell); $(id).replaceChildren(row);
}
function renderSpecs() {
  $("spec-rows").replaceChildren();
  state.specs.forEach((spec) => {
    const row = node("tr"); const name = node("td"); name.append(node("strong", spec.name));
    const actions = node("td"); actions.append(action("View source / download", () => viewSource(spec)));
    row.append(name, node("td", spec.id), actions); $("spec-rows").append(row);
  });
  if (!state.specs.length) emptyRow("spec-rows", 3, "No specifications yet. Upload a JSON or YAML document to get started.");
}
function detail(label, value) { const line = node("p", `${label} `); line.append(node("strong", value)); return line; }
function renderChannels() {
  $("channel-list").replaceChildren(); $("nav-channel-count").textContent = String(state.channels.length);
  state.channels.forEach((channel) => {
    const card = node("article", "", "channel-card"); const header = node("div", "", "channel-card-header");
    header.append(node("h2", channel.name), node("span", channel.enabled ? "Enabled" : "Disabled", `badge ${channel.enabled ? "" : "neutral"}`));
    const details = node("div", "", "channel-details");
    const bindings = channel.spec_ids.map((id) => state.specs.find((spec) => spec.id === id)?.name || id);
    details.append(detail("Sandbox", channel.sandbox_mode === "docker" ? "Docker · offline" : "Restricted Python"), detail("Specifications", bindings.join(", ") || "None bound"), detail("Libraries", channel.allowed_imports.join(", ") || "No imports"), detail("Revision", String(channel.revision ?? "—")));
    const actions = node("div", "", "channel-actions");
    actions.append(action("Edit channel", () => editChannel(channel)), action("Connect", () => connect(channel)), action("Rotate key", () => confirmKey(channel, true)), action("Revoke key", () => confirmKey(channel, false), true));
    card.append(header, node("p", channel.id, "channel-id"), details, actions); $("channel-list").append(card);
  });
  if (!state.channels.length) $("channel-list").append(node("p", "No channels yet. Create a channel to bind specifications and define its execution policy.", "empty-state"));
}
function renderUsage(items) {
  const calls = items.reduce((sum, item) => sum + item.calls, 0);
  const successes = items.filter((item) => successStatus(item.status)).reduce((sum, item) => sum + item.calls, 0);
  const latency = items.reduce((sum, item) => sum + item.total_ms, 0);
  $("metric-calls").textContent = count(calls); $("metric-success").textContent = calls ? `${(successes / calls * 100).toFixed(1)}%` : "—";
  $("metric-success-detail").textContent = `${count(successes)} successful calls`; $("metric-latency").textContent = calls ? `${(latency / calls).toFixed(0)} ms` : "—";
  $("metric-channels").textContent = count(state.channels.filter((channel) => channel.enabled).length);
  $("metric-channel-detail").textContent = `Of ${count(state.channels.length)} configured · tenant policy applies`;
  $("usage-rows").replaceChildren();
  items.forEach((item) => {
    const row = node("tr"); const identity = node("td"); identity.append(node("strong", channelName(item.channel_id)), node("small", item.tool));
    const status = node("td"); status.append(node("span", item.status, `badge ${successStatus(item.status) ? "" : "neutral"}`));
    row.append(identity, status, node("td", count(item.calls)), node("td", item.calls ? `${(item.total_ms / item.calls).toFixed(1)} ms` : "—")); $("usage-rows").append(row);
  });
  if (!items.length) emptyRow("usage-rows", 4, "No tool calls recorded yet. Usage will appear after clients use a channel.");
  renderChart(items);
}
function channelName(id) { return state.channels.find((channel) => channel.id === id)?.name || id || "—"; }
function renderChart(items) {
  const totals = new Map(); items.forEach((item) => totals.set(item.channel_id, (totals.get(item.channel_id) || 0) + item.calls));
  const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1]); const max = Math.max(1, ...sorted.map((entry) => entry[1]));
  $("usage-chart").replaceChildren();
  sorted.forEach(([id, calls]) => {
    const item = node("div", "", "chart-item"); const label = node("div", "", "chart-label"); label.append(node("span", channelName(id)), node("strong", count(calls)));
    const meter = node("meter"); meter.min = 0; meter.max = max; meter.value = calls; meter.setAttribute("aria-label", `${channelName(id)}: ${count(calls)} calls`);
    item.append(label, meter); $("usage-chart").append(item);
  });
  if (!sorted.length) $("usage-chart").append(node("p", "No traffic yet. Your channel activity will appear here.", "empty-cell"));
}
function renderAudit(items) {
  $("audit-rows").replaceChildren();
  items.forEach((item) => {
    const row = node("tr"); const date = new Date(typeof item.created_at === "number" ? item.created_at * 1000 : item.created_at);
    row.append(node("td", item.event), node("td", state.tenants.find((tenant) => tenant.id === item.tenant_id)?.name || item.tenant_id || "—"), node("td", item.channel_id || "—"), node("td", Number.isNaN(date.getTime()) ? "—" : date.toLocaleString())); $("audit-rows").append(row);
  });
  if (!items.length) emptyRow("audit-rows", 4, "No administrative activity recorded yet.");
}
function choices(id, items, selected) {
  $(id).replaceChildren();
  items.forEach((item) => {
    const label = node("label", "", "check-label"); const input = document.createElement("input"); input.type = "checkbox"; input.value = item.id; input.checked = selected.includes(item.id);
    const text = node("span", item.name); if (item.name !== item.id) text.append(node("small", item.id)); label.append(input, text); $(id).append(label);
  });
  if (!items.length) $(id).append(node("p", id === "spec-options" ? "No specifications uploaded. You can bind them later." : "No libraries approved by the operator.", "hint"));
}
function sandboxPolicy() {
  const docker = $("sandbox-mode").value === "docker";
  $("library-fieldset").disabled = !docker;
  if (!docker) document.querySelectorAll("#library-options input").forEach((input) => { input.checked = false; });
  $("sandbox-help").textContent = docker ? "Offline CPython: no network, API access, broker, or credentials. Only approved preinstalled libraries are available." : `Restricted Python: no imports, filesystem, or direct network. Bound APIs are accessed only through the broker.${state.settings.docker_enabled ? "" : " Docker is disabled by the operator."}`;
}
function editChannel(channel = null) {
  if (!state.settings || !state.tenant) throw new Error("Select a tenant and refresh its settings first.");
  state.editing = channel; $("channel-form").reset(); $("channel-dialog-title").textContent = channel ? "Edit channel" : "Create channel";
  $("channel-name").value = channel?.name || ""; $("channel-enabled").checked = channel?.enabled ?? true; $("channel-enabled-label").hidden = !channel;
  choices("spec-options", state.specs, channel?.spec_ids || []);
  choices("library-options", state.settings.allowed_imports.map((name) => ({ id: name, name })), channel?.allowed_imports || []);
  $("sandbox-mode").querySelector('[value="docker"]').disabled = !state.settings.docker_enabled;
  $("sandbox-mode").value = channel?.sandbox_mode || "restricted"; sandboxPolicy(); showDialog("channel-dialog");
}
const selected = (id) => [...$(id).querySelectorAll("input:checked")].map((input) => input.value);
async function saveChannel() {
  const payload = { name: $("channel-name").value.trim(), spec_ids: selected("spec-options"), sandbox_mode: $("sandbox-mode").value, allowed_imports: $("sandbox-mode").value === "docker" ? selected("library-options") : [] };
  if (!payload.name) throw new Error("Enter a channel name.");
  if (payload.sandbox_mode === "docker" && !state.settings.docker_enabled) throw new Error("Docker is disabled by the operator. Select restricted execution.");
  if (state.editing) payload.enabled = $("channel-enabled").checked;
  await api(state.editing ? channelPath(state.editing) : tenantPath("/channels"), state.editing ? "PATCH" : "POST", payload);
  $("channel-dialog").close(); await refresh(); notify("Channel saved. Rotate its key when you are ready to connect a client.");
}
async function uploadSpec() {
  const file = $("spec-file").files[0]; const name = $("spec-name").value.trim();
  if (!name) throw new Error("Enter a specification name.");
  if (!file || !/\.(json|ya?ml)$/i.test(file.name)) throw new Error("Choose a .json, .yaml, or .yml file.");
  if (file.size > state.settings.max_spec_bytes) throw new Error(`File is too large. The limit is ${count(state.settings.max_spec_bytes)} bytes.`);
  const content = await file.text();
  if (new TextEncoder().encode(content).length > state.settings.max_spec_bytes) throw new Error("The decoded document exceeds the upload size limit.");
  await api(tenantPath("/specs"), "POST", { name, content }); $("spec-dialog").close(); $("spec-form").reset(); await refresh(); notify("Specification uploaded as a new immutable version.");
}
async function viewSource(spec) {
  const source = await api(tenantPath(`/specs/${segment(spec.id)}`));
  state.source = { name: source.name, text: JSON.stringify(source.document, null, 2) };
  $("source-title").textContent = source.name; $("source-content").textContent = state.source.text; showDialog("source-dialog");
}
function downloadSource() {
  if (!state.source) return;
  const url = URL.createObjectURL(new Blob([state.source.text], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `${state.source.name.replace(/[^a-z0-9_-]/gi, "_").slice(0, 100) || "specification"}.json`;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); notify("JSON download started.");
}
function connect(channel) {
  const endpoint = `${location.origin}/mcp/${segment(channel.id)}`; $("endpoint").value = endpoint;
  const authorization = "Bearer <YOUR_CHANNEL_TOKEN>";
  $("client-config").textContent = JSON.stringify({ mcpServers: { gryphon: { url: endpoint, headers: { Authorization: authorization } } } }, null, 2);
  showDialog("connect-dialog");
}
async function copy(text) {
  if (!text) throw new Error("There is nothing to copy.");
  try { await navigator.clipboard.writeText(text); notify("Copied to clipboard. Store credentials securely."); }
  catch { throw new Error("Clipboard access is unavailable. Select the field and copy it manually."); }
}
function confirmAction(title, description, work) {
  state.confirm = work; $("confirm-title").textContent = title; $("confirm-description").textContent = description; $("confirm-submit").textContent = title; showDialog("confirm-dialog");
}
function confirmKey(channel, rotate) {
  confirmAction(rotate ? "Rotate channel key" : "Revoke channel key", `${channel.name}: ${rotate ? "the previous key will immediately stop working. A new key will be shown only once." : "the current key will immediately stop working. Rotate a new key later to restore authenticated access."}`, async () => {
    const result = await api(channelPath(channel, rotate ? "/rotate" : "/revoke"), "POST", {}); $("confirm-dialog").close();
    if (rotate) { clearKey(); $("channel-token").value = result.token; showDialog("key-dialog"); notify("Key rotated. Copy the new key before closing this dialog."); }
    else notify("Channel key revoked. Existing credentials can no longer authenticate.");
  });
}
function toggleTenant() {
  const tenant = state.tenants.find((item) => item.id === state.tenant);
  if (!tenant) return;
  confirmAction(tenant.enabled ? "Disable tenant" : "Enable tenant", `${tenant.name}: ${tenant.enabled ? "all of this tenant's channel access will be blocked." : "enabled channels will be allowed to accept authenticated connections again."}`, async () => {
    await api(tenantPath(), "PATCH", { enabled: !tenant.enabled }); $("confirm-dialog").close(); await refresh(); notify("Tenant status updated.");
  });
}
function wireForms() {
  bind("login-form", async () => { const token = $("login-token").value; $("login-token").value = ""; await enter(await api("/api/login", "POST", { token })); }, "submit");
  bind("tenant-form", async () => {
    const name = $("tenant-name").value.trim(); if (!name) throw new Error("Enter a tenant name.");
    const tenant = await api("/api/tenants", "POST", { name }); state.tenant = tenant.id; $("tenant-dialog").close(); await refresh(); notify("Tenant created. Upload a specification or create your first channel.");
  }, "submit");
  bind("spec-form", uploadSpec, "submit"); bind("channel-form", saveChannel, "submit");
  bind("confirm-form", async () => { if (state.confirm) await state.confirm(); }, "submit");
  $("spec-file").addEventListener("change", () => { if (!$("spec-name").value) $("spec-name").value = $("spec-file").files[0]?.name.replace(/\.(json|ya?ml)$/i, "").replace(/[^a-z0-9_ -]/gi, "-").slice(0, 120) || ""; });
  $("sandbox-mode").addEventListener("change", sandboxPolicy);
}
function wireActions() {
  const newTenant = () => { $("tenant-form").reset(); showDialog("tenant-dialog"); };
  bind("new-tenant", newTenant); bind("empty-new-tenant", newTenant); bind("toggle-tenant", toggleTenant);
  bind("new-channel", () => editChannel());
  bind("new-spec", () => { if (!state.settings) throw new Error("Refresh settings before uploading."); $("spec-form").reset(); $("file-limit").textContent = `Maximum file size: ${count(state.settings.max_spec_bytes)} bytes. No remote URLs are fetched by this form.`; showDialog("spec-dialog"); });
  bind("refresh", refresh); bind("logout", async () => { await api("/api/logout", "POST", {}); signedOut(); notify("Signed out."); });
  $("tenant-select").addEventListener("change", () => run(async () => { state.epoch += 1; state.tenant = $("tenant-select").value; clearKey(); renderTenants(); await loadTenant(); notify("Tenant workspace loaded."); }));
  bind("download-source", downloadSource); bind("copy-endpoint", () => copy($("endpoint").value)); bind("copy-config", () => copy($("client-config").textContent)); bind("copy-key", () => copy($("channel-token").value));
  bind("reveal-key", () => { const reveal = $("channel-token").type === "password"; $("channel-token").type = reveal ? "text" : "password"; $("reveal-key").textContent = reveal ? "Hide key" : "Reveal key"; $("reveal-key").setAttribute("aria-pressed", String(reveal)); });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => { if (!state.busy) button.closest("dialog").close(); }));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); }));
  $("key-dialog").addEventListener("close", clearKey); $("confirm-dialog").addEventListener("close", () => { state.confirm = null; });
  $("source-dialog").addEventListener("close", () => { state.source = null; $("source-content").textContent = ""; });
  window.addEventListener("hashchange", page); window.addEventListener("pagehide", clearKey);
}
wireForms(); wireActions(); page();
run(async () => { try { await enter(await api("/api/session")); } catch (error) { if (state.csrf) throw error; notify(error.message, true); } });
