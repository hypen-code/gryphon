"use strict";
const $ = (id) => document.getElementById(id);
const state = { csrf: "", tenant: "", tenants: [], specs: [], channels: [], settings: null, editing: null, source: null, confirm: null, me: null, users: [], userOffset: 0, userNext: null, editingUser: null, passwordUser: null, epoch: 0, busy: false };
const pages = { analytics: ["Analytics", "Measured execution activity, honest estimates, and transparent methodology."], overview: ["Overview", "A clear view of your channels, capabilities, and activity."], channels: ["Channels", "Give each connection exactly the capabilities it needs."], specs: ["API specifications", "Versioned API catalogs, ready to bind to your channels."], audit: ["Audit trail", "A transparent record of administrative changes in your scope."], users: ["Users", "Manage named accounts and their access to the control plane."] };
const errors = {
  invalid_token: "The administrator token was not accepted.",
  invalid_credentials: "The username or password was not accepted.",
  invalid_password: "The password was not accepted. Check the current password and use 12–128 characters for a new password.",
  csrf: "Your security session is out of date. Refresh the page and sign in again.",
  invalid_csrf: "Your security session is out of date. Refresh the page and sign in again.",
  validation: "Check the document format, unique API binding names, and sandbox/library policy. The submitted configuration was not accepted.",
  invalid_spec: "The specification could not be validated. Check its Swagger/OpenAPI document or UCP discovery structure.",
  ucp_discovery: "UCP discovery failed. Use a public HTTPS website root, /.well-known/ucp profile, or MCP endpoint and check operator network policy.",
  ucp_transport: "The UCP transport could not be used. Check the advertised REST or MCP binding and its endpoint; unsupported transports are not callable.",
  ucp_schema: "The UCP tool or schema contract is unsupported or invalid. Check the advertised tool inputs, required caller metadata, and supported contracts.",
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
const NOTICE_DURATION_MS = 10000;
let noticeTimer = null;
let noticeRevision = 0;
function dismissNotice() {
  clearTimeout(noticeTimer); noticeTimer = null; noticeRevision += 1;
  $("notice").hidden = true; $("notice-text").textContent = ""; $("notice").classList.remove("error");
}
function notify(message, error = false) {
  dismissNotice();
  if (!message) return;
  $("notice-text").textContent = message; $("notice").classList.toggle("error", error); $("notice").hidden = false;
  const revision = noticeRevision;
  noticeTimer = setTimeout(() => { if (revision === noticeRevision) dismissNotice(); }, NOTICE_DURATION_MS);
  const target = document.querySelector("dialog[open] .dialog-message");
  if (target) { target.textContent = message; target.classList.toggle("error", error); }
}
function showDialog(id) {
  const dialog = $(id); const message = dialog.querySelector(".dialog-message");
  if (message) message.textContent = "";
  dialog.showModal();
}
function clearKey() { $("channel-token").value = ""; $("channel-token").type = "password"; $("reveal-key").textContent = "Reveal key"; $("reveal-key").setAttribute("aria-pressed", "false"); }
const isAdmin = () => state.me?.role === "platform_admin";
function clearPasswords(root = document) { root.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ""; }); }
function clearDialog(dialog) { clearPasswords(dialog); if (dialog.id === "key-dialog") clearKey(); }
function signedOut() {
  analytics.reset(); specifications.resetDeletion();
  state.epoch += 1; state.csrf = ""; state.tenant = ""; state.tenants = []; state.channels = []; state.specs = []; state.settings = null; state.source = null; state.confirm = null;
  state.me = null; state.users = []; state.userOffset = 0; state.userNext = null; state.editing = null; state.editingUser = null; state.passwordUser = null;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); clearKey(); clearPasswords();
  document.querySelectorAll("form").forEach((form) => form.reset()); $("source-content").textContent = ""; $("source-metadata").textContent = ""; $("client-config").textContent = ""; $("endpoint").value = "";
  renderIdentity(); renderTenants(); renderSpecs(); renderChannels(); renderUsage([]); renderAudit([]); renderUsers(); notify("");
  $("app").hidden = true; $("login").hidden = false; $("login-username").focus();
}
function validationMessage(path) {
  if (path === "/api/password" || (path.startsWith("/api/users/") && path.endsWith("/password"))) return errors.invalid_password;
  if (path === "/api/users" || path.startsWith("/api/users/")) return "Check the username format, display name, 12–128 character password, and enabled assigned tenant.";
  if (path.includes("/specs")) return "Check the specification document or public source URL, supported tool/schema contracts, and operator network policy. UCP accepts a HTTPS website root, discovery profile, or MCP endpoint.";
  return errors.validation;
}
async function api(path, method = "GET", body, signal) {
  const epoch = state.epoch; const headers = { Accept: "application/json" };
  if (method !== "GET") { headers["Content-Type"] = "application/json"; if (state.csrf) headers["X-CSRF-Token"] = state.csrf; }
  let response;
  try { response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", redirect: "error", signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000), ...(body === undefined ? {} : { body: JSON.stringify(body) }) }); }
  catch { throw new Error("The server could not be reached. Check your connection, then refresh before retrying a change."); }
  if (signal?.aborted || epoch !== state.epoch) throw new Error("Workspace changed. Refresh to see the latest data.");
  const result = await response.json().catch(() => ({}));
  if (signal?.aborted || epoch !== state.epoch) throw new Error("Workspace changed. Refresh to see the latest data.");
  if (response.status === 401) { if (path !== "/api/login") signedOut(); throw new Error(path === "/api/login" ? "The sign-in credentials were not accepted." : "Sign in to continue. Your session may have expired."); }
  if (!response.ok) {
    if (response.status === 403) { clearPasswords(); state.users = []; state.userNext = null; renderUsers(); document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); if (location.hash === "#users") location.hash = "#overview"; }
    const fallback = { 400: "Check your input and try again.", 403: "This action was denied. Refresh your session or check operator policy.", 404: "Resource not found. Refresh the workspace.", 409: "A conflicting change was detected. Refresh and try again.", 413: "This specification exceeds the upload size limit.", 422: "The submitted configuration is not valid.", 429: "Too many requests. Wait a moment and try again.", 503: "The service is temporarily unavailable. Try again shortly." };
    const category = result.error === "validation" ? validationMessage(path) : (Object.hasOwn(errors, result.error) ? errors[result.error] : "");
    throw new Error(`${category || fallback[response.status] || "The request failed. Refresh and try again."} (HTTP ${response.status})`);
  }
  return result;
}
async function run(work, trigger) {
  if (state.busy) return;
  state.busy = true; if (trigger) trigger.disabled = true; $("tenant-select").disabled = true;
  try { await work(); } catch (error) { notify(error.message || "Unable to complete this action.", true); }
  finally { state.busy = false; if (trigger) trigger.disabled = false; $("tenant-select").disabled = !isAdmin() || !state.tenants.length; renderUsers(); }
}
function bind(id, work, event = "click") {
  $(id).addEventListener(event, (e) => { e.preventDefault(); run(() => work(e), event === "submit" ? e.submitter : e.currentTarget); });
}
function page() {
  let key = Object.hasOwn(pages, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  if (key === "users" && !isAdmin()) { key = "overview"; if (state.me) { location.hash = "#overview"; notify("Only platform administrators can manage users.", true); } }
  Object.keys(pages).forEach((name) => { $(`page-${name}`).hidden = name !== key; document.querySelector(`[data-page="${name}"]`).toggleAttribute("aria-current", name === key); });
  document.querySelector(`[data-page="${key}"]`).setAttribute("aria-current", "page");
  $("page-title").textContent = pages[key][0]; $("page-description").textContent = pages[key][1];
  $("tenant-empty").hidden = !!state.tenant || !isAdmin() || ["audit", "users"].includes(key); $("tenant-content").hidden = !state.tenant;
}
function renderTenants() {
  const select = $("tenant-select"); select.replaceChildren();
  if (!state.tenants.length) select.append(new Option("No tenants yet", ""));
  state.tenants.forEach((tenant) => select.append(new Option(`${tenant.name}${tenant.enabled ? "" : " · disabled"}`, tenant.id)));
  select.value = state.tenant; select.disabled = state.busy || !isAdmin() || !state.tenants.length;
  $("assigned-tenant").textContent = state.tenants.find((item) => item.id === state.tenant)?.name || "Assigned tenant unavailable";
  const tenant = state.tenants.find((item) => item.id === state.tenant);
  $("tenant-heading").textContent = tenant?.name || "No tenant selected";
  $("tenant-status").textContent = tenant ? (tenant.enabled ? "Tenant enabled" : "Tenant disabled") : "No tenant selected";
  $("tenant-status").className = `badge ${tenant ? (tenant.enabled ? "" : "negative") : "neutral"}`;
  $("tenant-context").textContent = tenant ? (tenant.enabled ? "Channel access follows each channel's policy." : "All channel access is blocked until this tenant is enabled.") : "Create a tenant to get started.";
  $("toggle-tenant").disabled = !tenant; $("toggle-tenant").textContent = tenant?.enabled ? "Disable tenant" : "Enable tenant"; page();
}
async function refresh() {
  analytics.clear("Refreshing analytics…");
  $("loading").hidden = false; $("main").setAttribute("aria-busy", "true");
  try {
    state.me = (await api("/api/me")).user; renderIdentity();
    const [settings, tenants, audit] = await Promise.all([api("/api/settings"), api("/api/tenants"), api("/api/audit")]);
    state.settings = settings; state.tenants = isAdmin() ? tenants.items : tenants.items.filter((tenant) => tenant.id === state.me.tenant_id);
    if (!state.tenants.some((tenant) => tenant.id === state.tenant)) state.tenant = state.tenants[0]?.id || "";
    renderTenants(); renderAudit(audit.items); await loadTenant(); if (isAdmin()) await loadUsers(); notify("Workspace is up to date.");
  } catch (error) { analytics.clear(); emptyRow("audit-rows", 5, "Audit data may be unavailable. Use Refresh data to retry."); throw error; }
  finally { $("loading").hidden = true; $("main").removeAttribute("aria-busy"); }
}
async function loadTenant() {
  analytics.selectTenant(state.tenant); specifications.resetDeletion(); if ($("spec-delete-dialog").open) $("spec-delete-dialog").close();
  state.specs = []; state.channels = []; renderSpecs(); renderChannels(); renderUsage([]);
  if (!state.tenant) return;
  $("loading").hidden = false;
  try {
    const [specs, channels, usage] = await Promise.all([api(tenantPath("/specs")), api(tenantPath("/channels")), api(tenantPath("/usage"))]);
    state.specs = specs.items; state.channels = channels.items; renderSpecs(); renderChannels(); renderUsage(usage.items); analytics.channels(state.channels); await analytics.load();
  } catch (error) {
    analytics.clear(); emptyRow("spec-rows", 3, "Specifications unavailable. Use Refresh data to retry."); emptyRow("usage-rows", 4, "Usage unavailable. Use Refresh data to retry.");
    $("channel-list").replaceChildren(node("p", "Channels unavailable. Use Refresh data to retry.", "empty-state"));
    ["metric-calls", "metric-success", "metric-latency", "metric-channels"].forEach((id) => { $(id).textContent = "—"; });
    $("usage-chart").replaceChildren(node("p", "Usage unavailable. Refresh to retry.", "empty-cell")); throw error;
  } finally { $("loading").hidden = true; }
}
async function enter(session) {
  state.csrf = session.csrf_token; clearPasswords(); $("login").hidden = true; $("app").hidden = false; $("main").focus(); await refresh();
}
function action(label, work, danger = false) {
  const button = node("button", label, `text-button${danger ? " danger-text" : ""}`); button.type = "button";
  button.addEventListener("click", (event) => { event.preventDefault(); run(work, button); }); return button;
}
function emptyRow(id, columns, text) {
  const row = node("tr"); const cell = node("td", text, "empty-cell"); cell.colSpan = columns; row.append(cell); $(id).replaceChildren(row);
}
function renderSpecs() { specifications.render(); }
function detail(label, value) { const line = node("p", `${label} `); line.append(node("strong", value)); return line; }
function renderChannels() {
  $("channel-list").replaceChildren(); $("nav-channel-count").textContent = String(state.channels.length);
  state.channels.forEach((channel) => {
    const card = node("article", "", "channel-card"); const header = node("div", "", "channel-card-header");
    header.append(node("h2", channel.name), node("span", channel.enabled ? "Enabled" : "Disabled", `badge ${channel.enabled ? "" : "neutral"}`));
    const details = node("div", "", "channel-details");
    const bindings = specifications.bindingChoices(channel.spec_ids).filter((spec) => channel.spec_ids.includes(spec.id)).map((spec) => spec.name);
    details.append(detail("Sandbox", channel.sandbox_mode === "docker" ? "Docker · offline" : "Restricted Python"), detail("Specifications", bindings.join(", ") || "None bound"), detail("Libraries", channel.allowed_imports.join(", ") || "No imports"), detail("Revision", String(channel.revision ?? "—")));
    details.append(detail("Discovery", channel.include_function_summaries ? "Function names and descriptions · bounded continuation" : "Compact server summaries"));
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
function auditActor(item) {
  const actor = item.actor && typeof item.actor === "object" ? item.actor : {};
  const id = actor.id || item.actor_id; const name = actor.name || item.actor_name; const username = actor.username || item.actor_username;
  const bootstrap = actor.kind === "bootstrap" || id === "bootstrap" || id === "bootstrap_admin";
  const cell = node("td", bootstrap ? "Bootstrap administrator" : name || username || (id ? "Account" : "Unknown / legacy actor"));
  if (username && username !== name) cell.append(node("small", username));
  if (id) cell.append(node("small", `Actor ID: ${id}`));
  if (actor.display_source === "current" && (name || username)) cell.append(node("small", "Current account name"));
  return cell;
}
function renderAudit(items) {
  $("audit-rows").replaceChildren();
  items.forEach((item) => {
    const row = node("tr"); const date = new Date(typeof item.created_at === "number" ? item.created_at * 1000 : item.created_at);
    const event = node("td", item.event); const subject = item.subject || {}; if (item.spec_id) event.append(node("small", `Specification: ${item.spec_id}`));
    const subjectId = subject.id || item.subject_id || item.user_id; const subjectName = subject.name || subject.username || item.subject_name || item.subject_username;
    if (subjectId || subjectName) event.append(node("small", `Account subject: ${subjectName || subjectId}${subjectName && subjectId ? ` · ${subjectId}` : ""}`));
    row.append(event, auditActor(item), node("td", state.tenants.find((tenant) => tenant.id === item.tenant_id)?.name || item.tenant_id || "—"), node("td", item.channel_id || "—"), node("td", Number.isNaN(date.getTime()) ? "—" : date.toLocaleString())); $("audit-rows").append(row);
  });
  if (!items.length) emptyRow("audit-rows", 5, "No administrative activity recorded yet.");
}
function choices(id, items, selected) {
  $(id).replaceChildren();
  items.forEach((item) => {
    const label = node("label", "", "check-label"); const input = document.createElement("input"); input.type = "checkbox"; input.value = item.id; input.checked = selected.includes(item.id);
    const text = node("span", item.name); if (item.name !== item.id) text.append(node("small", item.id)); label.append(input, text);
    if (item.latest_id) label.append(action("Use latest", () => { input.value = item.latest_id; input.checked = true; text.textContent = item.latest_name; text.append(node("small", item.latest_id)); label.lastChild.remove(); }));
    $(id).append(label);
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
  $("channel-function-summaries").checked = channel?.include_function_summaries ?? false;
  choices("spec-options", specifications.bindingChoices(channel?.spec_ids || []), channel?.spec_ids || []);
  choices("library-options", state.settings.allowed_imports.map((name) => ({ id: name, name })), channel?.allowed_imports || []);
  $("sandbox-mode").querySelector('[value="docker"]').disabled = !state.settings.docker_enabled;
  $("sandbox-mode").value = channel?.sandbox_mode || "restricted"; sandboxPolicy(); showDialog("channel-dialog");
}
const selected = (id) => [...$(id).querySelectorAll("input:checked")].map((input) => input.value);
async function saveChannel() {
  const payload = { name: $("channel-name").value.trim(), spec_ids: selected("spec-options"), sandbox_mode: $("sandbox-mode").value, allowed_imports: $("sandbox-mode").value === "docker" ? selected("library-options") : [], include_function_summaries: $("channel-function-summaries").checked };
  if (!payload.name) throw new Error("Enter a channel name.");
  if (payload.sandbox_mode === "docker" && !state.settings.docker_enabled) throw new Error("Docker is disabled by the operator. Select restricted execution.");
  if (state.editing) payload.enabled = $("channel-enabled").checked;
  await api(state.editing ? channelPath(state.editing) : tenantPath("/channels"), state.editing ? "PATCH" : "POST", payload);
  $("channel-dialog").close(); await refresh(); notify("Channel saved. Rotate its key when you are ready to connect a client.");
}
async function viewSource(spec) {
  const snapshot = { tenant: state.tenant, epoch: state.epoch, csrf: state.csrf }; specifications.current(snapshot); const source = await api(tenantPath(`/specs/${segment(spec.id)}`)); specifications.current(snapshot);
  state.source = { name: source.name, text: JSON.stringify(source.document, null, 2) };
  $("source-title").textContent = `${source.name} · ${spec.id}`; $("source-metadata").textContent = specifications.sourceMetadata(source).join(" · "); $("source-content").textContent = state.source.text; showDialog("source-dialog");
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
  if (!isAdmin()) throw new Error("Only platform administrators can change tenant status.");
  const tenant = state.tenants.find((item) => item.id === state.tenant);
  if (!tenant) return;
  confirmAction(tenant.enabled ? "Disable tenant" : "Enable tenant", `${tenant.name}: ${tenant.enabled ? "all of this tenant's channel access will be blocked." : "enabled channels will be allowed to accept authenticated connections again."}`, async () => {
    await api(tenantPath(), "PATCH", { enabled: !tenant.enabled }); $("confirm-dialog").close(); await refresh(); notify("Tenant status updated.");
  });
}
function renderIdentity() {
  $("identity-name").textContent = state.me?.name || state.me?.username || ""; $("identity-role").textContent = state.me ? (isAdmin() ? "Platform administrator" : "Tenant user") : "";
  ["nav-users", "new-tenant", "empty-new-tenant", "toggle-tenant", "tenant-select"].forEach((id) => { $(id).hidden = !isAdmin(); });
  $("assigned-tenant").hidden = isAdmin(); $("change-password").hidden = !state.me?.id; $("audit-scope").textContent = isAdmin() ? "All tenants" : "Assigned tenant";
  if (!isAdmin()) { state.users = []; state.userNext = null; renderUsers(); } page();
}
async function loadUsers(offset = 0) {
  if (!isAdmin()) throw new Error("Only platform administrators can manage users.");
  state.users = []; state.userNext = null; renderUsers();
  const result = await api(`/api/users?offset=${offset}`); state.users = result.items; state.userOffset = offset; state.userNext = result.next_offset; renderUsers();
}
function renderUsers() {
  $("user-rows").replaceChildren();
  state.users.forEach((user) => {
    const row = node("tr"); const actions = node("td", "", "user-actions");
    const toggle = action(user.enabled ? "Disable" : "Enable", () => toggleUser(user), user.enabled); toggle.disabled = user.id === state.me?.id;
    actions.append(action("Edit name", () => editUser(user)), toggle, action("Reset password", () => passwordDialog(user)));
    row.append(node("td", user.username), node("td", user.name), node("td", user.role === "platform_admin" ? "Platform administrator" : "Tenant user"), node("td", state.tenants.find((tenant) => tenant.id === user.tenant_id)?.name || user.tenant_id || "All tenants"), node("td", user.enabled ? "Enabled" : "Disabled"), actions); $("user-rows").append(row);
  });
  if (!state.users.length) emptyRow("user-rows", 6, "No users to display. Create an account or refresh to retry.");
  $("users-previous").disabled = !isAdmin() || state.userOffset === 0; $("users-next").disabled = !isAdmin() || state.userNext === null;
  $("users-page").textContent = state.users.length ? `Users ${state.userOffset + 1}–${state.userOffset + state.users.length}` : "No users loaded";
}
function userRole() {
  const tenant = $("user-role").value === "tenant_user"; $("user-tenant-field").hidden = !tenant; $("user-tenant").disabled = !tenant; $("user-tenant").required = tenant;
}
function editUser(user = null) {
  if (!isAdmin()) throw new Error("Only platform administrators can manage users.");
  state.editingUser = user; $("user-form").reset(); $("user-title").textContent = user ? "Edit user name" : "Create user"; $("user-name").value = user?.name || "";
  $("user-create-fields").hidden = !!user; $("user-create-fields").disabled = !!user;
  $("user-tenant").replaceChildren(new Option("Select a tenant", "")); state.tenants.forEach((tenant) => $("user-tenant").append(new Option(tenant.name, tenant.id))); userRole(); showDialog("user-dialog");
}
function passwordValue(passwordId, confirmId) {
  const password = $(passwordId).value;
  if ([...password].length < 12 || [...password].length > 128) throw new Error("Use a password of 12–128 characters; a generated passphrase is recommended.");
  if (password !== $(confirmId).value) throw new Error("The password confirmation does not match.");
  return password;
}
async function saveUser() {
  if (!isAdmin()) throw new Error("Only platform administrators can manage users.");
  const payload = { name: $("user-name").value.trim() }; if (!payload.name) throw new Error("Enter a name.");
  if (!state.editingUser) {
    Object.assign(payload, { username: $("user-username").value.trim(), password: passwordValue("user-password", "user-confirm"), role: $("user-role").value, tenant_id: $("user-role").value === "tenant_user" ? $("user-tenant").value : null });
    if (payload.role === "tenant_user" && !payload.tenant_id) throw new Error("Select the user's assigned tenant.");
  }
  const user = state.editingUser; clearPasswords($("user-dialog"));
  await api(user ? `/api/users/${segment(user.id)}` : "/api/users", user ? "PATCH" : "POST", payload);
  $("user-dialog").close();
  if (user && user.id === state.me?.id) { signedOut(); notify("Profile updated. Sign in again to continue."); }
  else { await refresh(); notify("User saved."); }
}
function toggleUser(user) {
  if (!isAdmin() || user.id === state.me?.id) throw new Error("You cannot change this account's status.");
  confirmAction(user.enabled ? "Disable user" : "Enable user", `${user.username}: ${user.enabled ? "sessions will be revoked. Separately issued shared channel keys remain valid; rotate exposed channel keys too." : "the account will be allowed to sign in again."}`, async () => {
    await api(`/api/users/${segment(user.id)}`, "PATCH", { enabled: !user.enabled }); $("confirm-dialog").close(); await refresh(); notify("User status updated.");
  });
}
function passwordDialog(user = null) {
  if (user ? !isAdmin() : !state.me?.id) throw new Error("A named account is required for this action.");
  state.passwordUser = user; $("password-form").reset(); $("password-title").textContent = user ? "Reset password" : "Change password"; $("password-subject").textContent = user ? user.username : state.me.username;
  $("current-password-field").hidden = !!user; $("current-password").disabled = !!user; $("current-password").required = !user; showDialog("password-dialog");
}
async function savePassword() {
  const user = state.passwordUser; const password = passwordValue("new-password", "confirm-password"); const current = $("current-password").value; clearPasswords($("password-dialog"));
  await api(user ? `/api/users/${segment(user.id)}/password` : "/api/password", "POST", user ? { password } : { current_password: current, new_password: password });
  $("password-dialog").close(); if (!user || user.id === state.me?.id) { signedOut(); notify("Password changed. Sign in with your new password."); } else { await refresh(); notify("Password reset. The user's existing sessions were revoked."); }
}
function wireAccounts() {
  bind("account-login-form", async () => { const username = $("login-username").value.trim(); const password = $("login-password").value; clearPasswords(); await enter(await api("/api/login", "POST", { username, password })); }, "submit");
  bind("new-user", () => editUser()); bind("user-form", saveUser, "submit"); $("user-role").addEventListener("change", userRole);
  bind("change-password", () => passwordDialog()); bind("password-form", savePassword, "submit");
  bind("users-previous", () => loadUsers(Math.max(0, state.userOffset - 100))); bind("users-next", () => { if (state.userNext !== null) return loadUsers(state.userNext); });
  ["user-dialog", "password-dialog"].forEach((id) => $(id).addEventListener("close", () => { clearPasswords($(id)); $(id).querySelector("form").reset(); state.editingUser = null; state.passwordUser = null; }));
  window.addEventListener("pagehide", () => clearPasswords());
}
function wireForms() {
  bind("login-form", async () => { const token = $("login-token").value; clearPasswords(); await enter(await api("/api/login", "POST", { token })); }, "submit");
  bind("tenant-form", async () => {
    const name = $("tenant-name").value.trim(); if (!name) throw new Error("Enter a tenant name.");
    const tenant = await api("/api/tenants", "POST", { name }); state.tenant = tenant.id; $("tenant-dialog").close(); await refresh(); notify("Tenant created. Upload a specification or create your first channel.");
  }, "submit");
  bind("channel-form", saveChannel, "submit");
  bind("confirm-form", async () => { if (state.confirm) await state.confirm(); }, "submit");
  $("sandbox-mode").addEventListener("change", sandboxPolicy);
}
function wireActions() {
  $("dismiss-notice").addEventListener("click", dismissNotice);
  const newTenant = () => { if (!isAdmin()) throw new Error("Only platform administrators can create tenants."); $("tenant-form").reset(); showDialog("tenant-dialog"); };
  bind("new-tenant", newTenant); bind("empty-new-tenant", newTenant); bind("toggle-tenant", toggleTenant);
  bind("new-channel", () => editChannel());
  bind("refresh", refresh); bind("logout", async () => { analytics.reset(); try { await api("/api/logout", "POST", {}); } finally { signedOut(); } notify("Signed out."); });
  $("tenant-select").addEventListener("change", () => run(async () => { if (!isAdmin()) { renderTenants(); return; } state.epoch += 1; state.tenant = $("tenant-select").value; clearKey(); renderTenants(); await loadTenant(); notify("Tenant workspace loaded."); }));
  bind("download-source", downloadSource); bind("copy-endpoint", () => copy($("endpoint").value)); bind("copy-config", () => copy($("client-config").textContent)); bind("copy-key", () => copy($("channel-token").value));
  bind("reveal-key", () => { const reveal = $("channel-token").type === "password"; $("channel-token").type = reveal ? "text" : "password"; $("reveal-key").textContent = reveal ? "Hide key" : "Reveal key"; $("reveal-key").setAttribute("aria-pressed", String(reveal)); });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => { if (!state.busy) { const dialog = button.closest("dialog"); clearDialog(dialog); dialog.close(); } }));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); else clearDialog(dialog); }));
  $("key-dialog").addEventListener("close", clearKey); $("confirm-dialog").addEventListener("close", () => { state.confirm = null; });
  $("source-dialog").addEventListener("close", () => { state.source = null; $("source-content").textContent = ""; $("source-metadata").textContent = ""; });
  window.addEventListener("hashchange", page); window.addEventListener("pagehide", clearKey);
}
wireForms(); wireActions(); wireAccounts(); specifications.wire(); analytics.wire(); page();
run(async () => { try { await enter(await api("/api/session")); } catch (error) { if (state.csrf) throw error; notify(error.message, true); } });
