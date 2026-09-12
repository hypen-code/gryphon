"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { runInNewContext } = require("node:vm");
const script = readFileSync(resolve(__dirname, "../../src/gryphon/static/specifications.js"), "utf8");

function element(tag = "div", text = "", className = "") {
  return { tag, textContent: text, className, value: "", checked: false, files: [], children: [], events: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, handler) { this.events[name] = handler; },
    reset() {}, close() { this.events.close?.(); },
  };
}
function setup() {
  const elements = new Map(); const handlers = {}; const calls = []; const messages = [];
  const $ = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const state = { tenant: "tenant-a", epoch: 1, csrf: "test-session", settings: { max_spec_bytes: 128 }, specs: [] };
  const sandbox = { $, state, URL, TextEncoder, count: String, segment: encodeURIComponent, node: element,
    tenantPath: (tail) => `/api/tenants/${state.tenant}${tail}`, bind: (id, fn) => { handlers[id] = fn; },
    action: (label, fn) => ({ textContent: label, click: fn }), emptyRow: () => {}, viewSource: () => {},
    showDialog: (id) => { $(id).opened = true; }, notify: (text) => messages.push(text), refresh: async () => {},
    api: async (path, method, payload) => { calls.push({ path, method, payload }); return sandbox.result; },
    result: { id: "new", diagnostics: { total_operations: 5, available_operations: 2, filtered_operations: 2, unsupported_operations: 1 } },
  };
  runInNewContext(`${script}\nspecifications.wire(); globalThis.render = specifications.render;`, sandbox);
  const open = () => { handlers["new-spec"](); $("spec-name").value = "Example"; };
  const file = (content = "{}", name = "source.yaml") => ({ name, size: content.length, text: async () => content });
  const edit = (spec) => { state.specs = [spec]; sandbox.render(); $("spec-rows").children[0].children[2].children[1].click(); };
  return { $, state, sandbox, handlers, calls, messages, open, file, edit };
}
function text(node) { return [node.textContent, ...(node.children || []).map(text)].join(" "); }

test("superseded snapshots disable updates but retain source viewing", () => {
  const ui = setup(); ui.state.specs = [{ id: "old", name: "Example" }, { id: "new", name: "Example", parent_id: "old" }];
  ui.sandbox.render(); const rows = ui.$("spec-rows").children;
  assert.equal(rows[0].children[2].children[1].disabled, true);
  assert.equal(rows[0].children[2].children[1].textContent, "Superseded snapshot");
  assert.match(text(rows[0]), /Superseded · retained snapshot/);
  assert.equal(rows[0].children[2].children[0].textContent, "View source / download");
  assert.equal(rows[1].children[2].children[1].disabled, false);
});

for (const kind of ["openapi", "ucp"]) test(`${kind} URL imports keep source kind and use the shared tenant API`, async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = kind; ui.$("spec-kind").events.change();
  assert.equal(ui.$("spec-file").disabled, true); assert.equal(ui.$("spec-url").required, true);
  ui.$("spec-url").value = "https://example.com/spec.json"; await ui.handlers["spec-form"]();
  assert.equal(JSON.stringify(ui.calls), JSON.stringify([{ path: "/api/tenants/tenant-a/specs", method: "POST", payload: { name: "Example", url: "https://example.com/spec.json", kind } }]));
  assert.match(ui.messages[0], /2 available · 2 filtered · 1 unsupported · 5 total/);
});

test("file imports preserve the bounded content contract", async () => {
  const ui = setup(); ui.open(); ui.$("spec-kind").value = "file"; ui.$("spec-kind").events.change();
  assert.equal(ui.$("spec-url").disabled, true); assert.equal(ui.$("spec-file").required, true);
  ui.$("spec-file").files = [ui.file()]; await ui.handlers["spec-form"]();
  assert.equal(JSON.stringify(ui.calls[0].payload), JSON.stringify({ name: "Example", content: "{}" }));
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
  assert.equal(JSON.stringify(ui.calls[0].payload), JSON.stringify(source_type === "file" ? { update_channels: true, content: "{}" } : { update_channels: true }));
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
  assert.match(text(ui.$("spec-rows")), /0 available · 1 filtered · 1 unsupported/);
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
