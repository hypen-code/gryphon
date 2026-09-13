"use strict";
const lifecycle = (() => {
  const paths = {
    edit: "m16 3 5 5-12 12-6 1 1-6zM14 5l5 5",
    suspend: "M8 5v14M16 5v14", resume: "m8 4 12 8-12 8z",
    password: "M14 7a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM10 13l-7 7M3 17l3 3",
    delete: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7",
    connect: "m9 15 6-6M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0",
    rotate: "M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6",
    revoke: "M5 5l14 14M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18",
  };
  let deletion = null; let revision = 0;
  const snapshot = () => ({ tenant: state.tenant, epoch: state.epoch, csrf: state.csrf, actor: state.me?.id, role: state.me?.role });
  function current(scope, workspace = true) {
    if (!scope || !state.csrf || scope.csrf !== state.csrf || scope.epoch !== state.epoch || scope.actor !== state.me?.id || scope.role !== state.me?.role || (workspace && scope.tenant !== state.tenant)) throw new Error("Workspace changed. Reopen the action for a fresh preview.");
  }
  function decorate(button, kind, label) {
    button.replaceChildren(); button.classList.add("lifecycle-action"); button.dataset.action = kind;
    button.setAttribute("aria-label", label); button.title = label; button.dataset.tooltip = label;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    Object.entries({ viewBox: "0 0 24 24", width: "18", height: "18", fill: "none", stroke: "currentColor", "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" }).forEach(([key, value]) => svg.setAttribute(key, value));
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", paths[kind]); svg.append(path); button.append(svg); return button;
  }
  function icon(kind, label, work, danger = false) { return decorate(action(label, work, danger), kind, label); }
  function reset(close = true) {
    revision += 1; deletion = null; $("lifecycle-delete-name").value = ""; $("lifecycle-delete-submit").disabled = true;
    $("lifecycle-delete-impact").replaceChildren(); $("lifecycle-delete-warning").textContent = "";
    if (close && $("lifecycle-delete-dialog").open) $("lifecycle-delete-dialog").close();
  }
  function active(scope) {
    current(scope);
    if (scope !== deletion || scope.revision !== revision || !$("lifecycle-delete-dialog").open) throw new Error("Deletion confirmation expired. Reopen for a fresh preview.");
    authorize(scope.kind, scope.id);
  }
  function authorize(kind, id) {
    if (!["tenant", "channel", "user"].includes(kind) || !state.me || !state.csrf) throw new Error("Sign in to continue.");
    if (kind !== "channel" && !isAdmin()) throw new Error("Only platform administrators can delete tenants or users.");
    if (kind === "user" && id === state.me.id) throw new Error("You cannot delete your current account. Use another administrator.");
    if (kind === "channel" && !isAdmin() && (state.me.role !== "tenant_user" || state.me.tenant_id !== state.tenant)) throw new Error("This channel is outside your assigned workspace.");
    if (kind === "tenant" && id !== state.tenant) throw new Error("Select the tenant before deleting it.");
  }
  function deletionPath(kind, id) {
    if (kind === "user") return `/api/users/${segment(id)}`;
    if (kind === "tenant") return `/api/tenants/${segment(id)}`;
    return `/api/tenants/${segment(state.tenant)}/channels/${segment(id)}`;
  }
  function validPreview(preview, scope) {
    if (!preview || preview.kind !== scope.kind || preview.id !== scope.id || typeof preview.name !== "string" || !preview.name || typeof preview.confirmation_token !== "string" || !preview.confirmation_token || !["users", "channels", "specs"].every((key) => Number.isSafeInteger(preview.impact?.[key]) && preview.impact[key] >= 0)) throw new Error("Deletion preview unavailable. Close and reopen to try again.");
  }
  function impact(preview) {
    const target = $("lifecycle-delete-impact"); target.replaceChildren(node("p", `Exact ${preview.kind === "user" ? "username" : "name"}:`), node("strong", preview.name, "spec-exact-name"));
    target.append(node("p", `Removes ${count(preview.impact.users)} users · ${count(preview.impact.channels)} channels · ${count(preview.impact.specs)} specification versions.`));
    const descriptions = {
      tenant: "The entire workspace and all assigned tenant users, channels, keys, bindings, specification versions, and scoped usage/analytics are removed. All workspace access ends. Suspend is the reversible alternative.",
      channel: "This channel, its keys, bindings, and scoped usage/analytics are removed. Its MCP endpoint stops accepting access. Specifications and users are kept.",
      user: "This account is deleted and its browser sessions/cookies are revoked. Separately issued shared channel keys remain valid. For offboarding, rotate exposed channel keys too. Shared channels and specifications are kept. Another enabled administrator is required before deleting the last enabled administrator.",
    };
    $("lifecycle-delete-warning").textContent = `Irreversible deletion. ${descriptions[preview.kind]} Audit history remains under normal bounded retention. Local execution receipts/artifact files are NOT purged; removed endpoints no longer provide access to them.`;
  }
  function sync() {
    let enabled = false;
    try { active(deletion); enabled = !!deletion.preview && $("lifecycle-delete-name").value === deletion.preview.name; }
    catch { if (deletion) reset(); }
    $("lifecycle-delete-submit").disabled = state.busy || !enabled;
  }
  async function open(kind, resource) {
    reset(); authorize(kind, resource.id);
    const scope = { ...snapshot(), kind, id: resource.id, path: deletionPath(kind, resource.id), revision, preview: null };
    deletion = scope; $("lifecycle-delete-title").textContent = `Delete ${kind}`; $("lifecycle-delete-submit").textContent = `Delete ${kind}`;
    $("lifecycle-delete-label").textContent = kind === "user" ? "Exact username" : `Exact ${kind} name`;
    $("lifecycle-delete-impact").textContent = "Loading fresh deletion preview…"; showDialog("lifecycle-delete-dialog");
    try {
      active(scope); const preview = await api(`${scope.path}/deletion`); active(scope); validPreview(preview, scope);
      scope.preview = preview; $("lifecycle-delete-name").value = ""; impact(preview); $("lifecycle-delete-name").focus(); sync();
    } catch (error) {
      if (deletion === scope) { reset(false); throw new Error(`${error.message}${kind === "user" ? " If this is the last enabled administrator, create or enable another administrator first." : ""}`); }
    }
  }
  async function remove() {
    const scope = deletion;
    try {
      active(scope); const confirm_name = $("lifecycle-delete-name").value;
      if (!scope.preview || confirm_name !== scope.preview.name) throw new Error("Type the exact name shown in the fresh preview, including case and spaces.");
      const confirmation_token = scope.preview.confirmation_token;
      const result = await api(scope.path, "DELETE", { confirm_name, confirmation_token }); active(scope);
      if (result.deleted !== true || result[`${scope.kind}_id`] !== scope.id) throw new Error("Deletion result was not confirmed. Refresh the workspace before continuing.");
    } catch (error) {
      if (deletion !== scope) return;
      reset(false); throw new Error(`${error.message} Close and reopen for a fresh preview and retype the name.${scope?.kind === "user" ? " If this is the last enabled administrator, create or enable another administrator first." : ""}`);
    }
    reset(); await refresh(); current(scope, scope.kind !== "tenant"); notify(`${scope.kind[0].toUpperCase()}${scope.kind.slice(1)} deleted. Audit history follows normal retention.`);
  }
  function tenantControls(tenant) {
    const toggle = $("toggle-tenant"); const remove = $("delete-tenant");
    decorate(toggle, tenant?.enabled ? "suspend" : "resume", tenant?.enabled ? "Suspend tenant" : "Resume tenant"); decorate(remove, "delete", "Delete tenant");
    toggle.disabled = remove.disabled = state.busy || !tenant || !isAdmin(); toggle.hidden = remove.hidden = !isAdmin();
  }
  function toggleTenant() {
    if (!isAdmin()) throw new Error("Only platform administrators can change tenant status.");
    const tenant = state.tenants.find((item) => item.id === state.tenant); if (!tenant) return;
    const scope = snapshot();
    confirmAction(tenant.enabled ? "Suspend tenant" : "Resume tenant", `${tenant.name}: ${tenant.enabled ? "reversibly block all channel access and revoke tenant-user browser sessions. Data and channel keys are retained; resuming does not restore revoked browser sessions." : "enabled channels can accept existing valid channel keys again. Tenant users must sign in again."}`, async () => {
      current(scope); if (!isAdmin()) throw new Error("Administrator access required.");
      await api(`/api/tenants/${segment(tenant.id)}`, "PATCH", { enabled: !tenant.enabled }); current(scope);
      $("confirm-dialog").close(); await refresh(); current(scope); notify(tenant.enabled ? "Tenant suspended." : "Tenant resumed.");
    });
  }
  function toggleUser(user) {
    if (!isAdmin() || user.id === state.me?.id) throw new Error("You cannot change this account's status.");
    const scope = snapshot();
    confirmAction(user.enabled ? "Suspend user" : "Enable user", `${user.username}: ${user.enabled ? "sessions will be revoked. Separately issued shared channel keys remain valid; rotate exposed channel keys too." : "the account will be allowed to sign in again."}`, async () => {
      current(scope); await api(`/api/users/${segment(user.id)}`, "PATCH", { enabled: !user.enabled }); current(scope);
      $("confirm-dialog").close(); await refresh(); current(scope); notify("User status updated.");
    });
  }
  function userActions(user) {
    const actions = node("td", "", "user-actions"); const self = user.id === state.me?.id;
    const toggle = icon(user.enabled ? "suspend" : "resume", self ? "Cannot change your current account status" : user.enabled ? "Suspend user" : "Enable user", () => toggleUser(user), user.enabled);
    const remove = icon("delete", self ? "Cannot delete your current account; use another administrator" : "Delete user", () => open("user", user), true);
    toggle.disabled = remove.disabled = self || state.busy; if (self) { toggle.setAttribute("aria-disabled", "true"); remove.setAttribute("aria-disabled", "true"); }
    actions.append(icon("edit", "Edit name", () => editUser(user)), toggle, icon("password", "Reset password", () => passwordDialog(user)), remove); return actions;
  }
  function renderUsers() {
    $("user-rows").replaceChildren();
    if (isAdmin()) state.users.forEach((user) => {
      const row = node("tr"); row.dataset.userId = user.id;
      row.append(node("td", user.username), node("td", user.name), node("td", user.role === "platform_admin" ? "Platform administrator" : "Tenant user"), node("td", state.tenants.find((tenant) => tenant.id === user.tenant_id)?.name || user.tenant_id || "All tenants"), node("td", user.enabled ? "Enabled" : "Suspended"), userActions(user)); $("user-rows").append(row);
    });
    if (!state.users.length || !isAdmin()) emptyRow("user-rows", 6, "No users to display. Create an account or refresh to retry.");
    $("users-previous").disabled = !isAdmin() || state.userOffset === 0; $("users-next").disabled = !isAdmin() || state.userNext === null;
    $("users-page").textContent = state.users.length ? `Users ${state.userOffset + 1}–${state.userOffset + state.users.length}` : "No users loaded";
  }
  function wire() {
    bind("delete-tenant", () => open("tenant", { id: state.tenant }));
    $("lifecycle-delete-name").addEventListener("input", sync);
    $("lifecycle-delete-form").addEventListener("submit", (event) => { event.preventDefault(); run(remove).finally(sync); });
    $("lifecycle-delete-dialog").addEventListener("close", () => { if (!$("lifecycle-delete-dialog").open) reset(false); });
    $("lifecycle-delete-dialog").addEventListener("cancel", () => reset());
    $("lifecycle-delete-dialog").querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => reset()));
    window.addEventListener("pagehide", () => reset());
  }
  return { icon, open, reset, sync, snapshot, current, tenantControls, toggleTenant, renderUsers, wire };
})();
