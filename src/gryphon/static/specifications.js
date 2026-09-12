"use strict";
const specifications = (() => {
  let draft = null;
  let updating = null;
  const remote = (spec) => ["openapi_url", "ucp_url"].includes(spec.source_type);
  const context = () => ({ tenant: state.tenant, epoch: state.epoch });
  const sourceLabel = (spec) => spec.source_type === "ucp_url" ? "UCP URL" : remote(spec) ? "Swagger / OpenAPI URL" : "Uploaded file";
  function current(snapshot) {
    if (!snapshot || snapshot.tenant !== state.tenant || snapshot.epoch !== state.epoch || !state.csrf) throw new Error("Workspace changed. Reopen the specification form before retrying.");
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
  function versionCell(spec) {
    const version = node("td", spec.id);
    sourceMetadata(spec).forEach((value) => version.append(node("small", value, "source-location")));
    version.append(node("span", diagnostics(spec), `spec-diagnostics${spec.diagnostics?.available_operations === 0 ? " negative" : ""}`));
    (spec.warnings || []).filter((warning) => typeof warning === "string").slice(0, 10).forEach((warning) => version.append(node("small", warning)));
    return version;
  }
  function history(group) {
    $("spec-history-title").textContent = `${group.latest.name} · version history`;
    $("spec-history-rows").replaceChildren();
    group.versions.forEach((spec) => {
      const row = node("tr"); row.dataset.specId = spec.id;
      const identity = versionCell(spec); identity.append(node("small", spec.id === group.latest.id ? "Latest version" : "Retained snapshot"));
      identity.append(node("small", `Read-only filter: ${spec.read_only_filter ?? true ? "on" : "off"}`));
      if (spec.parent_id) identity.append(node("small", `Previous version: ${spec.parent_id}`));
      const actions = node("td"); actions.append(action("View source / download", () => viewSource(spec)));
      row.append(identity, node("td", bindingLabel(spec, spec.id === group.latest.id)), actions); $("spec-history-rows").append(row);
    });
    showDialog("spec-history-dialog");
  }
  function render() {
    $("spec-rows").replaceChildren(); const logical = groups();
    logical.forEach((group) => {
      const spec = group.latest; const row = node("tr"); row.dataset.specificationId = group.id; row.dataset.specId = spec.id;
      const name = node("td"); name.append(node("strong", spec.name), node("small", sourceLabel(spec)));
      if (remote(spec)) name.append(node("small", spec.source_url || "Source URL unavailable", "source-location"));
      const version = versionCell(spec); version.append(node("small", `Latest version · ${group.versions.length} retained snapshot${group.versions.length === 1 ? "" : "s"}`));
      version.append(node("small", bindingLabel(spec, true)));
      const pinned = group.versions.slice(1).some((old) => state.channels.some((channel) => channel.spec_ids.includes(old.id)));
      if (pinned) version.append(node("small", "Older versions are pinned to channels. See History for bindings."));
      const actions = node("td", "", "spec-actions");
      actions.append(action("View source / download", () => viewSource(spec)), action(remote(spec) ? "Refresh from URL" : "Update / replace file", () => openRefresh(spec)), filterControl(spec, false));
      actions.append(action(`History (${group.versions.length})`, () => history(group)));
      row.append(name, version, actions); $("spec-rows").append(row);
    });
    if (!logical.length) emptyRow("spec-rows", 3, "No specifications yet. Upload a JSON or YAML file, or import a Swagger/OpenAPI or UCP URL.");
  }
  function filterControl(spec, superseded) {
    const label = node("label", "", "check-label"); const input = node("input");
    input.type = "checkbox"; input.checked = spec.read_only_filter ?? true; input.disabled = superseded;
    input.setAttribute("aria-label", `Read-only filter for ${spec.name} · ${spec.id}`); input.setAttribute("aria-describedby", "spec-filter-policy");
    input.title = superseded ? "Change the latest snapshot instead; this stored filter is retained." : "Confirm a new snapshot from this saved document; no fetch or upload.";
    input.addEventListener("change", () => {
      const requested = input.checked; input.checked = spec.read_only_filter ?? true;
      if (!superseded) run(() => openRefresh(spec, requested), input);
    });
    label.append(input, node("span", "Read-only filter")); return label;
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
  function wire() {
    $("spec-history-dialog").addEventListener("close", () => { $("spec-history-rows").replaceChildren(); $("spec-history-title").textContent = "Version history"; });
    bind("new-spec", openImport); bind("spec-form", importSpec, "submit"); bind("spec-refresh-form", refreshSpec, "submit");
    $("spec-kind").addEventListener("change", sourceKind);
    $("spec-file").addEventListener("change", () => { if (!$("spec-name").value) $("spec-name").value = $("spec-file").files[0]?.name.replace(/\.(json|ya?ml)$/i, "").replace(/[^a-z0-9_ -]/gi, "-").slice(0, 120) || ""; });
    $("spec-dialog").addEventListener("close", () => { draft = null; $("spec-form").reset(); sourceKind(); });
    $("spec-refresh-dialog").addEventListener("close", () => { updating = null; $("spec-refresh-form").reset(); $("spec-refresh-source").textContent = ""; });
  }
  return { render, wire, groups, bindingChoices, sourceMetadata };
})();
