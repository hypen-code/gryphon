"use strict";
const specifications = (() => {
  let draft = null;
  let updating = null;
  let deleting = null;
  const remote = (spec) => ["openapi_url", "ucp_url"].includes(spec.source_type);
  const context = () => ({ tenant: state.tenant, epoch: state.epoch, csrf: state.csrf });
  const sourceLabel = (spec) => spec.source_type === "ucp_url" ? "UCP URL" : remote(spec) ? "Swagger / OpenAPI URL" : "Uploaded file";
  function current(snapshot) {
    if (!snapshot || snapshot.tenant !== state.tenant || snapshot.epoch !== state.epoch || snapshot.csrf !== state.csrf || !state.csrf) {
      resetDeletion(); throw new Error("Workspace changed. Reopen the specification form before retrying.");
    }
  }
  function ready() {
    if (!state.settings || !state.tenant) throw new Error("Select a tenant and refresh its settings first.");
  }
  function diagnostics(spec) {
    const d = spec.diagnostics;
    if (!d) return "Operation counts unavailable for this snapshot. Refresh to compile diagnostics.";
    return `${count(d.available_operations)} included in discovery · ${count(d.filtered_operations)} filtered · ${count(d.unsupported_operations)} unsupported · ${count(d.total_operations)} total operations`;
  }
  const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  function groups() {
    const specs = state.specs.filter((spec) => !spec.tenant_id || spec.tenant_id === state.tenant);
    const byId = new Map(specs.map((spec) => [spec.id, spec])); const lineages = new Map();
    specs.forEach((spec) => {
      let root = spec; const seen = new Set([root.id]); let depth = 0;
      while (root.parent_id && byId.has(root.parent_id) && !seen.has(root.parent_id)) {
        root = byId.get(root.parent_id); seen.add(root.id); depth += 1;
      }
      const id = spec.specification_id || root.parent_id || root.id;
      const key = `${spec.tenant_id || state.tenant}:${id}`;
      if (!lineages.has(key)) lineages.set(key, { id, versions: [] });
      lineages.get(key).versions.push({ spec, depth });
    });
    return [...lineages.values()].map((group) => {
      group.versions.sort((a, b) => b.depth - a.depth || compare(String(b.spec.created_at || ""), String(a.spec.created_at || "")) || compare(b.spec.id, a.spec.id));
      return { id: group.id, versions: group.versions.map((entry) => entry.spec), latest: group.versions[0].spec };
    }).sort((a, b) => compare(a.latest.name, b.latest.name) || compare(a.id, b.id));
  }
  function bindingChoices(selected) {
    const items = groups().map((group) => {
      const pinned = group.versions.find((spec) => selected.includes(spec.id)); const spec = pinned || group.latest;
      const older = spec.id !== group.latest.id;
      return { id: spec.id, name: `${spec.name}${older ? " · pinned older version" : " · latest version"}`, ...(older ? { latest_id: group.latest.id, latest_name: `${group.latest.name} · latest version` } : {}) };
    });
    selected.filter((id) => !items.some((item) => item.id === id)).forEach((id) => items.push({ id, name: `${id} · pinned unavailable version` }));
    return items;
  }
  function bindingLabel(spec, latest) {
    const channels = state.channels.filter((channel) => channel.spec_ids.includes(spec.id));
    return channels.length ? `${latest ? "Bound" : "Pinned"} channels: ${channels.map((channel) => channel.name).join(", ")}` : "No channels bound to this version";
  }
  function sourceMetadata(spec) {
    const saved = spec.document?.["x-gryphon-ucp"];
    if (!saved && spec.source_type !== "ucp_url") return [];
    const transport = spec.ucp_transport || spec.source_transport || saved?.transport;
    const endpoint = spec.resolved_url || spec.resolved_endpoint || saved?.resolved_url || saved?.endpoint || spec.document?.servers?.[0]?.url;
    return [typeof transport === "string" && `Resolved UCP transport: ${transport}`, typeof endpoint === "string" && `Resolved endpoint: ${endpoint}`].filter(Boolean);
  }
  const paths = {
    details: "M12 8h.01M11 12h1v5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    source: "m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16",
    download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
    refresh: "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2",
    filter: "M3 4h18l-7 8v7l-4 2v-9Z",
    history: "M3 4v5h5M3 9a9 9 0 1 1 0 6M12 7v5l3 2",
    delete: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"
  };
  function icon(kind, label, work) {
    const button = action(label, async () => {
      const before = new Set(document.querySelectorAll("dialog[open]")); await work();
      const opened = [...document.querySelectorAll("dialog[open]")].find((dialog) => !before.has(dialog));
      opened?.addEventListener("close", () => { if (button.isConnected) button.focus(); }, { once: true });
    }, kind === "delete"); button.textContent = "";
    button.className += " spec-action"; button.dataset.specAction = kind; button.dataset.tooltip = label;
    button.title = label; button.setAttribute("aria-label", label);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    Object.entries({ viewBox: "0 0 24 24", width: "20", height: "20", fill: "none", stroke: "currentColor", "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" }).forEach(([key, value]) => svg.setAttribute(key, value));
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", paths[kind]); svg.append(path); button.append(svg); return button;
  }
  function versionCell(spec, versions) {
    const version = node("td", "", "spec-summary");
    version.append(node("span", versions ? `${versions} version${versions === 1 ? "" : "s"}` : spec.id.slice(0, 8), "badge neutral"));
    version.append(node("span", spec.diagnostics ? `${count(spec.diagnostics.available_operations)} available` : "Counts unavailable", `badge${spec.diagnostics?.available_operations === 0 ? " negative" : ""}`));
    version.append(node("span", `${count((spec.warnings || []).length)} warnings`, "badge neutral")); return version;
  }
  function details(spec, group) {
    const body = $("spec-details-content"); body.replaceChildren();
    $("spec-details-title").textContent = `${spec.name} · version details`;
    const latest = spec.id === group.latest.id;
    const fields = [[latest ? "Latest version UUID" : "Version UUID", spec.id], ["Latest version UUID", group.latest.id], ["SHA-256", spec.sha256 || "Unavailable"], ["Source kind", sourceLabel(spec)], ["Original URL", spec.source_url || "Uploaded file"], ["Resolved profile URL", spec.resolved_profile_url || "Unavailable"], ["Read-only filter", (spec.read_only_filter ?? true) ? "on" : "off"], ["Previous version", spec.parent_id || "None (root)"], ["Retained versions", String(group.versions.length)], ["Created", spec.created_at || "Unavailable"]];
    fields.filter(([label], index) => !latest || index !== 1).forEach(([label, value]) => { const line = node("p"); line.append(node("strong", `${label}: `), node("span", value)); body.append(line); });
    sourceMetadata(spec).forEach((value) => body.append(node("p", value)));
    if (spec.source_type !== "ucp_url") body.append(node("p", `Transport: ${spec.source_transport || "Unavailable"}`), node("p", `Resolved endpoint: ${spec.resolved_endpoint || "Unavailable"}`));
    body.append(node("p", diagnostics(spec)), node("p", bindingLabel(spec, latest)), node("h3", `Warnings (${count((spec.warnings || []).length)})`));
    const warnings = node("ul"); (spec.warnings || []).filter((warning) => typeof warning === "string").slice(0, 100).forEach((warning) => warnings.append(node("li", warning)));
    body.append(warnings); showDialog("spec-details-dialog");
  }
  async function sourceAction(spec, download = false) {
    const snapshot = context(); current(snapshot); await viewSource(spec); current(snapshot);
    if (download) { downloadSource(); $("source-dialog").close(); }
  }
  function versionActions(spec, group) {
    const actions = node("td", "", "spec-actions");
    actions.append(icon("details", "Version details", () => details(spec, group)), icon("source", "View source", () => sourceAction(spec)), icon("download", "Download JSON", () => sourceAction(spec, true))); return actions;
  }
  function history(group) {
    $("spec-history-title").textContent = `${group.latest.name} · version history`; $("spec-history-rows").replaceChildren();
    group.versions.forEach((spec) => {
      const row = node("tr"); row.dataset.specId = spec.id; const identity = versionCell(spec);
      identity.append(node("small", spec.id === group.latest.id ? "Latest version" : "Retained snapshot"));
      identity.append(node("small", `Filter ${(spec.read_only_filter ?? true) ? "on" : "off"}`));
      const bound = state.channels.filter((channel) => channel.spec_ids.includes(spec.id)).length;
      row.append(identity, node("td", `${bound} bound channel${bound === 1 ? "" : "s"}`), versionActions(spec, group)); $("spec-history-rows").append(row);
    }); showDialog("spec-history-dialog");
  }
  function render() {
    $("spec-rows").replaceChildren(); const logical = groups();
    logical.forEach((group) => {
      const spec = group.latest; const row = node("tr"); row.dataset.specificationId = group.id; row.dataset.specId = spec.id;
      const name = node("td", "", "spec-identity"); name.append(node("strong", spec.name), node("small", sourceLabel(spec)));
      if (remote(spec)) { const url = node("small", spec.source_url || "Source URL unavailable", "source-location"); url.title = url.textContent; name.append(url); }
      const actions = versionActions(spec, group);
      actions.append(icon("refresh", remote(spec) ? "Refresh from URL" : "Update / replace file", () => openRefresh(spec)), filterControl(spec));
      actions.append(icon("history", `History (${group.versions.length})`, () => history(group)), icon("delete", "Delete specification and all versions", () => openDeletion(spec)));
      row.append(name, versionCell(spec, group.versions.length), actions); $("spec-rows").append(row);
    });
    if (!logical.length) emptyRow("spec-rows", 3, "No specifications yet. Upload a JSON or YAML file, or import a Swagger/OpenAPI or UCP URL.");
  }
  function filterControl(spec) {
    const enabled = spec.read_only_filter ?? true;
    const button = icon("filter", `Read-only filter: ${enabled ? "on" : "off"}. Confirm change`, () => openRefresh(spec, !enabled));
    button.setAttribute("aria-pressed", String(enabled)); button.setAttribute("aria-describedby", "spec-filter-policy"); return button;
  }
  function sourceKind() {
    const file = $("spec-kind").value === "file";
    $("spec-file-fields").hidden = !file; $("spec-file").disabled = !file; $("spec-file").required = file;
    $("spec-url-fields").hidden = file; $("spec-url").disabled = file; $("spec-url").required = !file;
  }
  function openImport() {
    ready(); draft = context(); $("spec-form").reset(); $("spec-read-only-filter").checked = true; sourceKind();
    $("file-limit").textContent = `Maximum file size: ${count(state.settings.max_spec_bytes)} bytes.`;
    showDialog("spec-dialog");
  }
  async function readFile(id, snapshot) {
    current(snapshot);
    const file = $(id).files[0];
    if (!file || !/\.(json|ya?ml)$/i.test(file.name)) throw new Error("Choose a .json, .yaml, or .yml file.");
    if (file.size > state.settings.max_spec_bytes) throw new Error(`File is too large. The limit is ${count(state.settings.max_spec_bytes)} bytes.`);
    const content = await file.text(); current(snapshot);
    if (new TextEncoder().encode(content).length > state.settings.max_spec_bytes) throw new Error("The decoded document exceeds the upload size limit.");
    return content;
  }
  function sourceURL() {
    const value = $("spec-url").value.trim(); let url;
    try { url = new URL(value); } catch { throw new Error("Enter a complete public HTTP(S) specification URL."); }
    if (value.length > 2048 || !["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("Use an HTTP(S) URL of at most 2,048 characters without credentials, query strings, or fragments. Operator network policy still applies.");
    if ($("spec-kind").value === "ucp" && url.protocol !== "https:") throw new Error("UCP sources require a public HTTPS website root, discovery profile, or MCP endpoint.");
    return value;
  }
  async function importSpec() {
    ready(); const snapshot = draft; current(snapshot);
    const name = $("spec-name").value.trim(); const kind = $("spec-kind").value; const read_only_filter = $("spec-read-only-filter").checked;
    if (!name) throw new Error("Enter a specification name.");
    let result;
    if (kind === "file") {
      const content = await readFile("spec-file", snapshot); current(snapshot);
      result = await api(tenantPath("/specs"), "POST", { name, content, read_only_filter });
    } else {
      if (!["openapi", "ucp"].includes(kind)) throw new Error("Choose a supported specification source.");
      const url = sourceURL(); result = await api(tenantPath("/specs"), "POST", { name, url, kind, read_only_filter });
    }
    current(snapshot); $("spec-dialog").close(); $("spec-form").reset(); await refresh();
    notify(`Specification imported as an immutable snapshot. ${diagnostics(result)}`);
  }
  function openRefresh(spec, requestedFilter) {
    ready(); const filterOnly = typeof requestedFilter === "boolean";
    updating = { ...context(), spec, filterOnly }; $("spec-refresh-form").reset();
    const fromURL = remote(spec); const noFile = fromURL || filterOnly;
    $("spec-refresh-title").textContent = filterOnly ? "Confirm discovery filter change" : "Update / refresh specification";
    $("spec-refresh-submit").textContent = filterOnly ? "Confirm filter change" : "Confirm update / refresh";
    $("spec-refresh-source").textContent = `${spec.name} · ${sourceLabel(spec)}${fromURL ? `: ${spec.source_url || "Source URL unavailable"}` : ""}`;
    $("spec-refresh-help").textContent = filterOnly ? "Create a new immutable version from the saved document only. No URL refetch or replacement upload. The old version and its filter remain unchanged. Uncheck the read-only filter to include POST operations. Included POST operations execute automatically and may have side effects." : fromURL ? "Refetch the saved URL using its original source kind and name. A changed document or filter creates a new immutable version." : "Choose a replacement file. The API name stays the same; a changed document or filter creates a new immutable version.";
    $("spec-replacement-fields").hidden = noFile; $("spec-replacement").disabled = noFile; $("spec-replacement").required = !noFile;
    $("spec-refresh-read-only-filter").checked = filterOnly ? requestedFilter : (spec.read_only_filter ?? true);
    $("spec-replacement-limit").textContent = `Maximum file size: ${count(state.settings.max_spec_bytes)} bytes.`;
    $("spec-update-channels").checked = true; showDialog("spec-refresh-dialog");
  }
  async function refreshSpec() {
    ready(); const snapshot = updating; current(snapshot); const spec = snapshot.spec;
    const payload = { update_channels: $("spec-update-channels").checked, read_only_filter: $("spec-refresh-read-only-filter").checked };
    if (!snapshot.filterOnly && !remote(spec)) payload.content = await readFile("spec-replacement", snapshot);
    current(snapshot);
    const result = await api(tenantPath(`/specs/${segment(spec.id)}/${snapshot.filterOnly ? "filter" : "refresh"}`), "POST", payload);
    current(snapshot); $("spec-refresh-dialog").close(); $("spec-refresh-form").reset(); await refresh();
    const outcome = result.id === spec.id ? "Specification unchanged; existing bindings retained." : payload.update_channels ? "New snapshot saved. Channels bound to the previous version were updated." : "New snapshot saved. Channel bindings were left unchanged.";
    notify(`${outcome} ${diagnostics(result)}`);
  }
  function resetDeletion() {
    deleting = null; $("spec-delete-form").reset(); $("spec-delete-name").value = "";
    $("spec-delete-impact").replaceChildren(); $("spec-delete-submit").disabled = true;
  }
  function syncDeletion() {
    const valid = deleting?.preview && !deleting.busy && !state.busy && $("spec-delete-name").value === deleting.preview.name;
    $("spec-delete-submit").disabled = !valid;
  }
  async function openDeletion(spec) {
    ready(); resetDeletion(); const snapshot = { ...context(), spec, preview: null, busy: false }; deleting = snapshot;
    current(snapshot);
    const preview = await api(tenantPath(`/specs/${segment(spec.id)}/deletion`)); current(snapshot);
    if (deleting !== snapshot) return;
    if (!preview || typeof preview.name !== "string" || !preview.name || typeof preview.confirmation_token !== "string" || !preview.confirmation_token || !Array.isArray(preview.channels)) throw new Error("Deletion preview unavailable. Reopen to retry.");
    snapshot.preview = preview; const impact = $("spec-delete-impact");
    impact.append(node("p", `Delete ${preview.name}: all ${count(preview.version_count)} saved versions and their history permanently.`));
    impact.append(node("p", `Detach this specification from ${count(preview.channels.length)} channels. Channels, keys, and other specifications remain.`));
    const channels = node("ul"); preview.channels.forEach((channel) => channels.append(node("li", `${channel.name} · revision ${channel.revision}`))); impact.append(channels);
    const prompt = node("p", "Type the exact specification name: "); prompt.append(node("code", preview.name, "spec-exact-name")); impact.append(prompt);
    $("spec-delete-name").value = ""; syncDeletion(); showDialog("spec-delete-dialog");
  }
  async function deleteSpec() {
    const snapshot = deleting; current(snapshot);
    if (!snapshot.preview || snapshot.busy) throw new Error("Reopen Delete to obtain a fresh preview and confirm again.");
    const confirm_name = $("spec-delete-name").value;
    if (!confirm_name || confirm_name !== snapshot.preview.name) throw new Error("Type the exact specification name, including case and spaces.");
    const confirmation_token = snapshot.preview.confirmation_token; snapshot.busy = true; syncDeletion();
    try {
      current(snapshot);
      await api(tenantPath(`/specs/${segment(snapshot.spec.id)}`), "DELETE", { confirm_name, confirmation_token });
    } catch (error) {
      current(snapshot);
      if (deleting === snapshot) { snapshot.preview = null; snapshot.busy = false; syncDeletion(); }
      throw new Error(`${error.message} Reopen Delete for a fresh preview and explicitly confirm again; deletion was not retried.`);
    }
    current(snapshot); if (deleting !== snapshot) return;
    resetDeletion(); $("spec-delete-dialog").close(); await refresh(); current(snapshot);
    notify("Specification and all saved versions deleted. Channels and keys retained; affected bindings detached.");
  }
  function wire() {
    $("spec-delete-form").addEventListener("submit", (event) => { event.preventDefault(); return run(deleteSpec).finally(syncDeletion); });
    $("spec-delete-name").addEventListener("input", syncDeletion);
    $("spec-delete-dialog").addEventListener("close", resetDeletion);
    $("spec-delete-dialog").addEventListener("cancel", () => { if (!state.busy) resetDeletion(); });
    $("spec-details-dialog").addEventListener("close", () => { $("spec-details-content").replaceChildren(); $("spec-details-title").textContent = "Version details"; });
    $("spec-history-dialog").addEventListener("close", () => { $("spec-history-rows").replaceChildren(); $("spec-history-title").textContent = "Version history"; });
    bind("new-spec", openImport); bind("spec-form", importSpec, "submit"); bind("spec-refresh-form", refreshSpec, "submit");
    $("spec-kind").addEventListener("change", sourceKind);
    $("spec-file").addEventListener("change", () => { if (!$("spec-name").value) $("spec-name").value = $("spec-file").files[0]?.name.replace(/\.(json|ya?ml)$/i, "").replace(/[^a-z0-9_ -]/gi, "-").slice(0, 120) || ""; });
    $("spec-dialog").addEventListener("close", () => { draft = null; $("spec-form").reset(); sourceKind(); });
    $("spec-refresh-dialog").addEventListener("close", () => { updating = null; $("spec-refresh-form").reset(); $("spec-refresh-source").textContent = ""; });
  }
  return { render, wire, groups, bindingChoices, sourceMetadata, current, resetDeletion };
})();
