"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { runInNewContext } = require("node:vm");
const script = readFileSync(resolve(__dirname, "../../src/gryphon/static/specifications.js"), "utf8");

function element(tag = "div", text = "", className = "") {
  return { tag, textContent: text, className, value: "", checked: false, files: [], children: [], events: {}, dataset: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, handler) { this.events[name] = handler; },
    setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
    classList: { toggle() {}, remove() {} }, querySelectorAll() { return []; },
    querySelector() { return element(); }, showModal() { this.opened = true; }, focus() {},
    reset() {}, close() { this.opened = false; this.events.close?.(); },
  };
}
function setup() {
  const elements = new Map(); const handlers = {}; const calls = []; const messages = [];
  const $ = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const state = { tenant: "tenant-a", epoch: 1, csrf: "test-session", settings: { max_spec_bytes: 128 }, specs: [], channels: [], me: { role: "platform_admin" } };
  const sandbox = { $, state, URL, TextEncoder, count: String, segment: encodeURIComponent, node: element, isAdmin: () => state.me?.role === "platform_admin",
    tenantPath: (tail) => `/api/tenants/${state.tenant}${tail}`, bind: (id, fn) => { handlers[id] = fn; },
    action: (label, fn) => ({ textContent: label, click: fn }), emptyRow: () => {}, viewSource: () => {},
    run: async (fn) => { if (!state.busy) await fn(); },
    showDialog: (id) => { $(id).opened = true; }, notify: (text) => messages.push(text), refresh: async () => {},
    api: async (path, method, payload) => { calls.push({ path, method, payload }); return sandbox.result; },
    result: { id: "new", diagnostics: { total_operations: 5, available_operations: 2, filtered_operations: 2, unsupported_operations: 1 } },
  };
  runInNewContext(`${script}\nspecifications.wire(); globalThis.render = specifications.render; globalThis.specifications = specifications;`, sandbox);
  const open = () => { handlers["new-spec"](); $("spec-name").value = "Example"; };
  const file = (content = "{}", name = "source.yaml") => ({ name, size: content.length, text: async () => content });
  const edit = (spec) => { state.specs = [spec]; sandbox.render(); $("spec-rows").children[0].children[2].children[1].click(); };
  return { $, state, sandbox, handlers, calls, messages, open, file, edit };
}
function text(node) { return [node.textContent, ...(node.children || []).map(text)].join(" "); }

test("one logical row retains ordered history and exact pinned channel information", () => {
  const ui = setup(); ui.state.specs = [{ id: "new", name: "Example", parent_id: "old" }, { id: "old", name: "Example" }];
  ui.state.channels = [{ name: "Pinned channel", spec_ids: ["old"] }];
  ui.sandbox.render(); const rows = ui.$("spec-rows").children;
  assert.equal(rows.length, 1); assert.equal(rows[0].dataset.specificationId, "old"); assert.equal(rows[0].dataset.specId, "new");
  assert.match(text(rows[0]), /Older versions are pinned/);
  rows[0].children[2].children[3].click(); const history = ui.$("spec-history-rows").children;
  assert.equal(history.length, 2); assert.equal(history[1].dataset.specId, "old");
  assert.match(text(history[1]), /Pinned channels: Pinned channel/);
  assert.equal(history[1].children[2].children[0].textContent, "View source / download");
  assert.equal(JSON.stringify(ui.state.channels[0].spec_ids), '["old"]');
});

for (const kind of ["openapi", "ucp"]) test(`${kind} URL imports keep source kind and use the shared tenant API`, async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = kind; ui.$("spec-kind").events.change();
  assert.equal(ui.$("spec-file").disabled, true); assert.equal(ui.$("spec-url").required, true);
  ui.$("spec-url").value = "https://example.com/spec.json"; await ui.handlers["spec-form"]();
  assert.equal(JSON.stringify(ui.calls), JSON.stringify([{ path: "/api/tenants/tenant-a/specs", method: "POST", payload: { name: "Example", url: "https://example.com/spec.json", kind, read_only_filter: true } }]));
  assert.match(ui.messages[0], /2 included in discovery · 2 filtered · 1 unsupported · 5 total/);
});

test("file imports preserve the bounded content contract", async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "file"; ui.$("spec-kind").events.change();
  assert.equal(ui.$("spec-url").disabled, true); assert.equal(ui.$("spec-file").required, true);
  ui.$("spec-file").files = [ui.file()]; await ui.handlers["spec-form"]();
  assert.equal(JSON.stringify(ui.calls[0].payload), JSON.stringify({ name: "Example", content: "{}", read_only_filter: true }));
});

for (const url of ["javascript:alert(1)", "https://user:password@example.com/spec", "https://example.com/spec#fragment", "https://example.com/spec?token=not-a-secret", "not a url"]) test(`unsafe or incomplete URL is rejected: ${url}`, async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "openapi"; ui.$("spec-url").value = url;
  await assert.rejects(ui.handlers["spec-form"]()); assert.equal(ui.calls.length, 0);
});

for (const source_type of ["file", "openapi_url", "ucp_url"]) test(`${source_type} refresh preserves identity and confirms updating bindings`, async () => {
  const ui = setup(); ui.edit({ id: "old/id", name: "Example", source_type, source_url: "https://example.com/spec" });
  assert.equal(ui.$("spec-update-channels").checked, true);
  assert.equal(ui.$("spec-replacement").required, source_type === "file");
  if (source_type === "file") ui.$("spec-replacement").files = [ui.file()];
  await ui.handlers["spec-refresh-form"]();
  assert.equal(ui.calls[0].path, "/api/tenants/tenant-a/specs/old%2Fid/refresh");
  assert.equal(JSON.stringify(ui.calls[0].payload), JSON.stringify(source_type === "file" ? { update_channels: true, read_only_filter: true, content: "{}" } : { update_channels: true, read_only_filter: true }));
  assert.match(ui.messages[0], /Channels bound to the previous version were updated/);
});

test("refresh permits snapshot-only opt-out and reports unchanged responses", async () => {
  const ui = setup(); const spec = { id: "old", name: "Example", source_type: "ucp_url" };
  ui.edit(spec); ui.$("spec-update-channels").checked = false; await ui.handlers["spec-refresh-form"]();
  assert.equal(ui.calls[0].payload.update_channels, false); assert.match(ui.messages[0], /bindings were left unchanged/);
  ui.edit(spec); ui.sandbox.result = { ...ui.sandbox.result, id: "old" }; await ui.handlers["spec-refresh-form"]();
  assert.match(ui.messages[1], /unchanged; existing bindings retained/);
});

test("URL, names and warnings render only as inert text and zero availability is visible", () => {
  const ui = setup(); const attack = '<img src=x onerror="alert(1)">';
  ui.edit({ id: "id", name: attack, source_type: "ucp_url", source_url: attack, warnings: [attack], diagnostics: { total_operations: 2, available_operations: 0, filtered_operations: 1, unsupported_operations: 1 } });
  assert.match(text(ui.$("spec-rows")), /0 included in discovery · 1 filtered · 1 unsupported/);
  assert.equal(ui.$("spec-refresh-source").textContent, `${attack} · UCP URL: ${attack}`);
  assert.equal(ui.$("spec-rows").children[0].children[1].children[0].className, "spec-diagnostics negative");
  assert.ok(text(ui.$("spec-rows")).includes(attack));
});

for (const change of ["tenant", "epoch", "csrf"]) test(`stale ${change} blocks refresh before sending`, async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example", source_type: "openapi_url" });
  ui.state[change] = change === "epoch" ? 2 : change === "csrf" ? "" : "tenant-b";
  await assert.rejects(ui.handlers["spec-refresh-form"](), /Workspace changed/); assert.equal(ui.calls.length, 0);
});

test("workspace changes while reading a file cannot submit into the new tenant", async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "file";
  ui.$("spec-file").files = [{ name: "spec.json", size: 2, text: async () => { ui.state.epoch += 1; return "{}"; } }];
  await assert.rejects(ui.handlers["spec-form"](), /Workspace changed/); assert.equal(ui.calls.length, 0);
});

for (const file of [null, { name: "spec.txt", size: 2 }, { name: "spec.json", size: 129 }, { name: "spec.yaml", size: 1, text: async () => "é".repeat(65) }]) test("missing, unsupported or oversized replacement files are rejected", async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example", source_type: "file" }); ui.$("spec-replacement").files = file ? [file] : [];
  await assert.rejects(ui.handlers["spec-refresh-form"]()); assert.equal(ui.calls.length, 0);
});

test("failed refresh retains the form and makes no success claim", async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example", source_type: "ucp_url" });
  ui.sandbox.api = async () => { throw new Error("Denied by policy"); };
  await assert.rejects(ui.handlers["spec-refresh-form"](), /Denied by policy/); assert.equal(ui.messages.length, 0);
  assert.equal(ui.$("spec-refresh-dialog").opened, true);
});

test("closing forms clears source text and stale refresh selection", async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example", source_type: "ucp_url" }); ui.$("spec-refresh-dialog").close();
  assert.equal(ui.$("spec-refresh-source").textContent, "");
  await assert.rejects(ui.handlers["spec-refresh-form"](), /Workspace changed/); assert.equal(ui.calls.length, 0);
});

for (const kind of ["file", "openapi", "ucp"]) test(`${kind} import can include POST with no separate approval payload`, async () => {
  const ui = setup(); ui.open(); assert.equal(ui.$("spec-read-only-filter").checked, true);
  ui.$("spec-kind").value = kind; ui.$("spec-read-only-filter").checked = false;
  ui.$("spec-url").value = "https://example.com/spec"; ui.$("spec-file").files = [ui.file()];
  await ui.handlers["spec-form"](); assert.equal(ui.calls[0].payload.read_only_filter, false);
  ui.open(); assert.equal(ui.$("spec-read-only-filter").checked, true);
});

function rowFilter(ui) { return ui.$("spec-rows").children[0].children[2].children[2].children[0]; }
for (const source_type of ["file", "openapi_url", "ucp_url"]) test(`${source_type} row filter confirms saved-document successor without upload or refetch`, async () => {
  const ui = setup(); ui.edit({ id: "saved/id", name: "Example", source_type, read_only_filter: true });
  ui.$("spec-refresh-dialog").close(); const filter = rowFilter(ui);
  assert.equal(filter.checked, true); filter.checked = false; await filter.events.change();
  assert.equal(filter.checked, true); assert.equal(ui.calls.length, 0);
  assert.equal(ui.$("spec-refresh-read-only-filter").checked, false);
  assert.equal(ui.$("spec-replacement").disabled, true); assert.equal(ui.$("spec-replacement").required, false);
  assert.match(ui.$("spec-refresh-help").textContent, /No URL refetch or replacement upload/);
  ui.$("spec-update-channels").checked = false; await ui.handlers["spec-refresh-form"]();
  assert.equal(ui.calls[0].path, "/api/tenants/tenant-a/specs/saved%2Fid/filter");
  assert.equal(JSON.stringify(ui.calls[0].payload), JSON.stringify({ update_channels: false, read_only_filter: false }));
});

test("stored false filter is displayed and refresh preserves it by default", async () => {
  const ui = setup(); ui.edit({ id: "saved", name: "Example", source_type: "openapi_url", read_only_filter: false });
  assert.equal(rowFilter(ui).checked, false); assert.equal(ui.$("spec-refresh-read-only-filter").checked, false);
  await ui.handlers["spec-refresh-form"](); assert.equal(ui.calls[0].payload.read_only_filter, false);
});

test("busy and cancellation cannot change a saved filter; successors replace main rows", async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example" }); ui.$("spec-refresh-dialog").close();
  ui.state.busy = true; const filter = rowFilter(ui); filter.checked = false; await filter.events.change();
  assert.equal(filter.checked, true); assert.equal(ui.$("spec-refresh-dialog").opened, false);
  ui.state.busy = false; filter.checked = false; await filter.events.change(); ui.$("spec-refresh-dialog").close();
  await assert.rejects(ui.handlers["spec-refresh-form"](), /Workspace changed/);
  ui.state.specs.push({ id: "new", name: "Example", parent_id: "old" }); ui.sandbox.render();
  assert.equal(ui.$("spec-rows").children.length, 1); assert.equal(ui.$("spec-rows").children[0].dataset.specId, "new");
  assert.equal(rowFilter(ui).disabled, false); assert.equal(rowFilter(ui).checked, true); assert.equal(ui.calls.length, 0);
});

for (const change of ["tenant", "epoch", "csrf"]) test(`stale ${change} blocks saved filter changes`, async () => {
  const ui = setup(); ui.edit({ id: "old", name: "Example" }); const filter = rowFilter(ui);
  filter.checked = false; await filter.events.change(); ui.state[change] = change === "epoch" ? 2 : change === "tenant" ? "tenant-b" : "";
  await assert.rejects(ui.handlers["spec-refresh-form"](), /Workspace changed/); assert.equal(ui.calls.length, 0);
});

test("lineages are deterministic, tenant scoped and never grouped by name", () => {
  const ui = setup(); const versions = [
    { id: "third", parent_id: "second", name: "Same", tenant_id: "tenant-a" },
    { id: "second", parent_id: "root", name: "Same", tenant_id: "tenant-a" },
    { id: "root", name: "Same", tenant_id: "tenant-a" }, { id: "unrelated", name: "Same", tenant_id: "tenant-a" },
    { id: "foreign", parent_id: "root", name: "Same", tenant_id: "tenant-b" },
  ];
  ui.state.specs = versions; const first = JSON.stringify(ui.sandbox.specifications.groups());
  ui.state.specs = [...versions].reverse(); assert.equal(JSON.stringify(ui.sandbox.specifications.groups()), first);
  ui.sandbox.render(); assert.equal(ui.$("spec-rows").children.length, 2);
  assert.equal(ui.$("spec-rows").children[0].dataset.specId, "third");
  const fresh = ui.sandbox.specifications.bindingChoices([]); assert.equal(fresh.length, 2); assert.equal(fresh[0].id, "third");
  const pinned = ui.sandbox.specifications.bindingChoices(["second"]); assert.equal(pinned.length, 2);
  assert.equal(pinned[0].id, "second"); assert.equal(pinned[0].latest_id, "third"); assert.match(pinned[0].name, /pinned/);
  assert.match(ui.sandbox.specifications.bindingChoices(["unavailable"])[2].name, /pinned unavailable/);
});

test("repeated saved filter updates target latest IDs and survive workspace reload", async () => {
  const ui = setup(); ui.state.specs = [{ id: "root", name: "Example" }];
  ui.sandbox.refresh = async () => { ui.state.specs.push(ui.sandbox.result); ui.sandbox.render(); };
  for (let index = 1; index <= 3; index++) {
    ui.sandbox.render(); const previous = index === 1 ? "root" : `version-${index - 1}`;
    ui.sandbox.result = { id: `version-${index}`, parent_id: previous, name: "Example", read_only_filter: index % 2 === 0 };
    const filter = rowFilter(ui); filter.checked = !filter.checked; await filter.events.change(); await ui.handlers["spec-refresh-form"]();
    assert.equal(ui.calls.at(-1).path, `/api/tenants/tenant-a/specs/${previous}/filter`);
    assert.equal(ui.$("spec-rows").children.length, 1); assert.equal(ui.$("spec-rows").children[0].dataset.specificationId, "root");
  }
  const reloaded = setup(); reloaded.state.specs = JSON.parse(JSON.stringify(ui.state.specs)); reloaded.sandbox.render();
  assert.equal(reloaded.$("spec-rows").children.length, 1); reloaded.$("spec-rows").children[0].children[2].children[3].click();
  assert.equal(reloaded.$("spec-history-rows").children.length, 4);
});

for (const role of ["platform_admin", "tenant_user"]) test(`${role} has no manual POST permission controls or requests`, async () => {
  const ui = setup(); ui.state.me.role = role;
  ui.edit({ id: "snapshot/id", name: "Example", approved_post_reads: [{ function_name: "example.legacy" }] });
  assert.equal(ui.$("spec-rows").children[0].children[2].children.length, 4);
  assert.doesNotMatch(text(ui.$("spec-rows")), /POST read|permission|approval|attest/i);
  assert.ok(Object.keys(ui.handlers).every((id) => !id.includes("permissions")));
  const filter = rowFilter(ui); filter.checked = false; await filter.events.change();
  assert.match(ui.$("spec-refresh-help").textContent, /Included POST operations execute automatically and may have side effects/);
  await ui.handlers["spec-refresh-form"]();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.calls)), [{ path: "/api/tenants/tenant-a/specs/snapshot%2Fid/filter", method: "POST", payload: { update_channels: true, read_only_filter: false } }]);
});

for (const url of ["https://coolbudget.lk", "https://coolbudget.lk/.well-known/ucp", "https://coolbudget.lk/api/ucp/mcp"]) test(`UCP source ${url} is sent unchanged for server discovery`, async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "ucp"; ui.$("spec-url").value = url;
  ui.$("spec-read-only-filter").checked = false; await ui.handlers["spec-form"]();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.calls)), [{ path: "/api/tenants/tenant-a/specs", method: "POST", payload: { name: "Example", url, kind: "ucp", read_only_filter: false } }]);
});

for (const url of ["http://example.com", "https://caller@example.com/api/ucp/mcp", "https://example.com/api/ucp/mcp?key=value", "https://example.com/#profile", `https://example.com/${"a".repeat(2048)}`]) test(`unsafe UCP source is rejected: ${url.slice(0, 80)}`, async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "ucp"; ui.$("spec-url").value = url;
  await assert.rejects(ui.handlers["spec-form"]()); assert.deepEqual(ui.calls, []);
});

for (const fields of [
  { ucp_transport: "mcp", resolved_url: "https://shop.myshopify.com/api/ucp/mcp" },
  { source_transport: "mcp", resolved_endpoint: "https://shop.myshopify.com/api/ucp/mcp" },
  { document: { "x-gryphon-ucp": { transport: "mcp", endpoint: "https://shop.myshopify.com/api/ucp/mcp" } } },
]) test("resolved UCP metadata renders in rows and retained history", () => {
  const ui = setup(); ui.edit({ id: "saved", name: "Example", source_type: "ucp_url", ...fields });
  assert.match(text(ui.$("spec-rows")), /Resolved UCP transport: mcp/);
  assert.match(text(ui.$("spec-rows")), /Resolved endpoint: https:\/\/shop.myshopify.com\/api\/ucp\/mcp/);
  ui.$("spec-rows").children[0].children[2].children[3].click();
  assert.match(text(ui.$("spec-history-rows")), /Resolved UCP transport: mcp/);
  assert.deepEqual(ui.calls, []);
});

test("optional UCP metadata is inert, absent on legacy snapshots and never browser fetched", () => {
  const ui = setup(); const attack = '<img src=x onerror="alert(1)">';
  ui.edit({ id: "saved", name: "Example", source_type: "ucp_url", ucp_transport: attack, resolved_url: attack });
  assert.ok(text(ui.$("spec-rows")).includes(attack));
  assert.equal(ui.sandbox.specifications.sourceMetadata({ source_type: "ucp_url" }).length, 0);
  assert.doesNotMatch(script, /fetch\(|post-reads|permissions|attest/);
});

function adminSetup() {
  const elements = new Map(); const calls = []; const timers = new Map(); let now = 0; let next = 0;
  const $ = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const inline = element(); const sandbox = {
    document: { getElementById: $, createElement: element, querySelector: () => inline, querySelectorAll: () => [] },
    window: { addEventListener() {} }, location: { hash: "#overview" }, analytics: { reset() {} },
    setTimeout: (fn, ms) => { timers.set(++next, { fn, due: now + ms }); return next; }, clearTimeout: (id) => timers.delete(id),
    Intl, AbortSignal, Option: function () { return element(); },
  };
  const admin = readFileSync(resolve(__dirname, "../../src/gryphon/static/admin.js"), "utf8").split("wireForms(); wireActions();")[0];
  runInNewContext(`${admin}\nglobalThis.state = state;`, sandbox);
  sandbox.request = sandbox.api; sandbox.refresh = async () => {}; sandbox.api = async (path, method, payload) => { calls.push({ path, method, payload }); };
  sandbox.audit = sandbox.renderAudit;
  sandbox.specifications = { bindingChoices: () => [] };
  sandbox.renderUsers = sandbox.renderIdentity = sandbox.renderTenants = sandbox.renderSpecs = sandbox.renderChannels = sandbox.renderUsage = sandbox.renderAudit = () => {};
  sandbox.state.tenant = "tenant"; sandbox.state.settings = { docker_enabled: false, allowed_imports: [] };
  sandbox.wireActions();
  const tick = (ms) => { now += ms; for (const [id, timer] of [...timers]) if (timer.due <= now) { timers.delete(id); timer.fn(); } };
  return { $, sandbox, calls, timers, tick, inline };
}

for (const [category, message] of [["ucp_discovery", /UCP discovery failed/], ["ucp_transport", /UCP transport could not be used/], ["ucp_schema", /tool or schema contract is unsupported/], ["validation", /public source URL/]]) test(`${category} is a static relevant import error`, async () => {
  const ui = adminSetup(); const requests = [];
  ui.sandbox.fetch = async (path) => { requests.push(path); return { ok: false, status: 400, json: async () => ({ error: category, message: "unsafe upstream details" }) }; };
  await assert.rejects(ui.sandbox.request("/api/tenants/tenant/specs", "POST", {}), (error) => {
    assert.match(error.message, message); assert.match(error.message, /HTTP 400/);
    assert.doesNotMatch(error.message, /approval|attest|unsafe upstream details/); return true;
  });
  assert.deepEqual(requests, ["/api/tenants/tenant/specs"]);
});

test("inline action buttons prevent label activation from reversing explicit binding selection", () => {
  const ui = adminSetup(); let prevented = false; let selected = false;
  const button = ui.sandbox.action("Use latest", () => { selected = true; });
  button.events.click({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(selected, true);
});

test("audit actor names are inert and separate from account subjects and event scope", () => {
  const ui = adminSetup(); const attack = '<img src=x onerror="alert(1)">';
  ui.sandbox.audit([
    { event: "user.created", actor: { id: "actor-id", name: attack, username: "admin", display_source: "current" }, subject: { id: "subject-id", name: "New user" }, tenant_id: "tenant-id", channel_id: "channel-id" },
    { event: "tenant.created", actor_id: "bootstrap", actor_name: "Bootstrap" },
    { event: "legacy" }, { event: "channel.updated", actor_id: "fallback-id", actor_username: "fallback" },
  ]);
  const rows = ui.$("audit-rows").children; assert.equal(rows[0].children.length, 5);
  assert.equal(rows[0].children[1].textContent, attack); assert.match(text(rows[0].children[1]), /Actor ID: actor-id/);
  assert.match(text(rows[0].children[1]), /Current account name/);
  assert.match(text(rows[0].children[0]), /Account subject: New user · subject-id/);
  assert.equal(rows[0].children[2].textContent, "tenant-id"); assert.equal(rows[0].children[3].textContent, "channel-id");
  assert.match(text(rows[1].children[1]), /Bootstrap administrator.*Actor ID: bootstrap/);
  assert.equal(rows[2].children[1].textContent, "Unknown / legacy actor"); assert.equal(rows[3].children[1].textContent, "fallback");
  assert.equal(ui.calls.length, 0);
});

test("notifications expire at exactly ten seconds and preserve inline form errors", () => {
  const ui = adminSetup(); ui.sandbox.notify("Useful validation error", true);
  assert.equal(ui.$("notice").hidden, false); assert.equal([...ui.timers.values()][0].due, 10000);
  ui.tick(9999); assert.equal(ui.$("notice").hidden, false);
  ui.tick(1); assert.equal(ui.$("notice").hidden, true); assert.equal(ui.$("notice-text").textContent, "");
  assert.equal(ui.inline.textContent, "Useful validation error"); assert.equal(ui.timers.size, 0);
});

test("manual dismiss works while busy, clears its timer and preserves inline errors", () => {
  const ui = adminSetup(); ui.sandbox.notify("Check the form", true); ui.sandbox.state.busy = true;
  ui.$("dismiss-notice").events.click(); assert.equal(ui.$("notice").hidden, true);
  assert.equal(ui.timers.size, 0); assert.equal(ui.inline.textContent, "Check the form");
  ui.tick(10000); assert.equal(ui.$("notice-text").textContent, "");
});

test("replacement resets ten-second lifetime and an old queued timeout cannot dismiss it", () => {
  const ui = adminSetup(); ui.sandbox.notify("First"); const stale = [...ui.timers.values()][0].fn;
  ui.tick(6000); ui.sandbox.notify("Replacement"); assert.equal(ui.timers.size, 1); stale();
  ui.tick(4000); assert.equal(ui.$("notice-text").textContent, "Replacement");
  ui.tick(5999); assert.equal(ui.$("notice").hidden, false); ui.tick(1); assert.equal(ui.$("notice").hidden, true);
});

for (const reset of ["empty", "signedOut"]) test(`${reset} clears notification text and stale timers`, () => {
  const ui = adminSetup(); ui.sandbox.notify("Old message"); const stale = [...ui.timers.values()][0].fn;
  if (reset === "empty") ui.sandbox.notify(""); else ui.sandbox.signedOut();
  assert.equal(ui.$("notice-text").textContent, ""); assert.equal(ui.$("notice").hidden, true); assert.equal(ui.timers.size, 0);
  ui.sandbox.notify("New message"); stale(); assert.equal(ui.$("notice-text").textContent, "New message");
});

for (const editing of [false, true]) for (const checked of [false, true]) test(`channel ${editing ? "PATCH" : "POST"} sends summaries ${checked}`, async () => {
  const ui = adminSetup(); const channel = editing ? { id: "channel", name: "Existing", include_function_summaries: true } : null;
  ui.sandbox.editChannel(channel); assert.equal(ui.$("channel-function-summaries").checked, editing);
  ui.$("channel-name").value = "Example"; ui.$("channel-function-summaries").checked = checked;
  await ui.sandbox.saveChannel(); assert.equal(ui.calls[0].method, editing ? "PATCH" : "POST");
  assert.equal(ui.calls[0].payload.include_function_summaries, checked);
  assert.equal(ui.calls[0].path, `/api/tenants/tenant/channels${editing ? "/channel" : ""}`);
  ui.sandbox.editChannel({ id: "legacy", name: "Legacy" }); assert.equal(ui.$("channel-function-summaries").checked, false);
});
