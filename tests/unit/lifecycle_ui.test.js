"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { runInNewContext } = require("node:vm");
const root = resolve(__dirname, "../../src/gryphon/static");
function element(tag = "div", text = "", className = "") {
  return { tag, textContent: text, className, children: [], events: {}, dataset: {}, value: "", open: false,
    append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; this.textContent = ""; },
    addEventListener(name, handler) { this.events[name] = handler; }, setAttribute(name, value) { this[name] = value; },
    classList: { add() {}, remove() {}, toggle() {} }, querySelectorAll() { return []; }, querySelector() { return element(); },
    showModal() { this.open = true; }, close() { this.open = false; this.events.close?.(); }, focus() {}, reset() {},
  };
}
function setup(kind = "channel") {
  const elements = new Map(); const calls = []; const messages = []; let refreshes = 0;
  const $ = (id) => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const sandbox = { document: { getElementById: $, createElement: element, createElementNS: (_, tag) => element(tag), querySelectorAll: () => [], querySelector: () => element() }, window: { addEventListener() {} }, Intl, setTimeout, clearTimeout };
  const admin = readFileSync(resolve(root, "admin.js"), "utf8").split("wireForms(); wireActions();")[0];
  runInNewContext(`${readFileSync(resolve(root, "lifecycle.js"), "utf8")}\n${admin}\nglobalThis.state = state; globalThis.lifecycle = lifecycle; lifecycle.wire();`, sandbox);
  Object.assign(sandbox.state, { tenant: "tenant/id", epoch: 1, csrf: "test-csrf", me: { id: "admin", role: "platform_admin" }, tenants: [{ id: "tenant/id", name: "Tenant", enabled: true }] });
  const id = kind === "tenant" ? "tenant/id" : `${kind}/id`;
  const preview = { kind, id, name: kind === "user" ? "exact.username" : " Exact <Name> ", label: "Cached display label", confirmation_token: "fresh-token", impact: { users: 2, channels: 3, specs: 4 } };
  sandbox.notify = (message) => messages.push(message); sandbox.refresh = async () => { refreshes++; };
  sandbox.api = async (path, method = "GET", payload) => { calls.push({ path, method, payload }); return method === "DELETE" ? { deleted: true, [`${kind}_id`]: id } : preview; };
  const open = () => sandbox.run(() => sandbox.lifecycle.open(kind, { id, name: "Not authoritative" }));
  const type = (value) => { $("lifecycle-delete-name").value = value; $("lifecycle-delete-name").events.input(); };
  const submit = async () => { $("lifecycle-delete-form").events.submit({ preventDefault() {} }); while (sandbox.state.busy) await new Promise(setImmediate); };
  return { $, sandbox, state: sandbox.state, calls, messages, preview, open, type, submit, refreshes: () => refreshes };
}
function text(item) { return [item.textContent, ...item.children.map(text)].join(" "); }
for (const kind of ["tenant", "channel", "user"]) test(`${kind} deletion uses fresh server identity and exact closed payload`, async () => {
  const ui = setup(kind); await ui.open();
  const base = kind === "tenant" ? "/api/tenants/tenant%2Fid" : kind === "user" ? "/api/users/user%2Fid" : "/api/tenants/tenant%2Fid/channels/channel%2Fid";
  assert.equal(ui.calls[0].path, `${base}/deletion`); assert.equal(ui.$("lifecycle-delete-name").value, "");
  assert.match(text(ui.$("lifecycle-delete-impact")), /2 users · 3 channels · 4 specification versions/);
  assert.ok(text(ui.$("lifecycle-delete-impact")).includes(ui.preview.name)); assert.ok(!text(ui.$("lifecycle-delete-impact")).includes("Not authoritative"));
  for (const wrong of ["", "wrong", ui.preview.name.toUpperCase(), `${ui.preview.name} `]) { ui.type(wrong); assert.equal(ui.$("lifecycle-delete-submit").disabled, true); }
  ui.type(ui.preview.name); assert.equal(ui.$("lifecycle-delete-submit").disabled, false); await ui.submit();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.calls[1])), { path: base, method: "DELETE", payload: { confirm_name: ui.preview.name, confirmation_token: "fresh-token" } });
  assert.equal(ui.$("lifecycle-delete-name").value, ""); assert.equal(ui.$("lifecycle-delete-dialog").open, false); assert.equal(ui.refreshes(), 1);
});
for (const kind of ["tenant", "channel", "user"]) test(`${kind} wrong-name submission cannot send DELETE`, async () => {
  const ui = setup(kind); await ui.open(); ui.type(ui.preview.name.trim() + "x"); await ui.submit();
  assert.equal(ui.calls.length, 1); assert.equal(ui.$("lifecycle-delete-submit").disabled, true); assert.equal(ui.$("lifecycle-delete-name").value, "");
});
for (const change of ["tenant", "epoch", "csrf", "actor", "role"]) for (const phase of ["before", "preview", "delete"]) test(`${change} drift ${phase} clears confirmation and suppresses stale updates`, async () => {
  const ui = setup(); const drift = () => { if (change === "actor") ui.state.me.id = "other"; else if (change === "role") ui.state.me.role = "tenant_user"; else ui.state[change] = change === "epoch" ? 2 : "changed"; };
  if (phase !== "preview") { await ui.open(); ui.type(ui.preview.name); }
  if (phase === "before") drift();
  else ui.sandbox.api = async (path, method) => { ui.calls.push({ path, method }); drift(); return phase === "preview" ? ui.preview : { deleted: true, channel_id: ui.preview.id }; };
  if (phase === "preview") await ui.open(); else await ui.submit();
  assert.equal(ui.$("lifecycle-delete-name").value, ""); assert.equal(ui.$("lifecycle-delete-submit").disabled, true); assert.equal(ui.refreshes(), 0);
  const sent = ui.calls.length; await ui.submit(); assert.equal(ui.calls.length, sent);
});
for (const status of [400, 409, 404, 403]) test(`HTTP ${status} clears token and never retries; reopening starts blank`, async () => {
  const ui = setup(); await ui.open(); ui.type(ui.preview.name); const original = ui.sandbox.api;
  ui.sandbox.api = async (...args) => { await original(...args); throw new Error(`Conflict (HTTP ${status})`); }; await ui.submit();
  assert.equal(ui.$("lifecycle-delete-name").value, ""); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
  await ui.submit(); assert.equal(ui.calls.length, 2); ui.sandbox.api = original; await ui.open(); assert.equal(ui.calls.length, 3);
  assert.equal(ui.$("lifecycle-delete-name").value, ""); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
});
for (const event of ["close", "cancel"]) test(`${event} while preview pending prevents stale dialog reopening`, async () => {
  const ui = setup(); let finish; ui.sandbox.api = () => new Promise((resolve) => { finish = resolve; });
  const pending = ui.open(); if (event === "close") ui.$("lifecycle-delete-dialog").close(); else ui.$("lifecycle-delete-dialog").events.cancel();
  finish(ui.preview); await pending; assert.equal(ui.$("lifecycle-delete-dialog").open, false); assert.equal(ui.$("lifecycle-delete-name").value, "");
});
test("busy delete stays disabled and duplicate submit never repeats a mutation", async () => {
  const ui = setup(); await ui.open(); ui.type(ui.preview.name); let finish; let deletes = 0;
  ui.sandbox.api = () => { deletes++; return new Promise((resolve) => { finish = resolve; }); };
  const pending = ui.submit(); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
  ui.$("lifecycle-delete-form").events.submit({ preventDefault() {} }); ui.type(ui.preview.name); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
  finish({ deleted: true, channel_id: ui.preview.id }); await pending; assert.equal(deletes, 1); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
});
for (const kind of ["tenant", "user"]) test(`tenant user cannot preview ${kind} deletion`, async () => {
  const ui = setup(kind); ui.state.me = { id: "member", role: "tenant_user", tenant_id: ui.state.tenant }; await ui.open(); assert.equal(ui.calls.length, 0);
});
test("own tenant member may delete channel but foreign membership cannot", async () => {
  const ui = setup(); ui.state.me = { id: "member", role: "tenant_user", tenant_id: ui.state.tenant }; await ui.open(); assert.equal(ui.calls.length, 1);
  ui.state.me.tenant_id = "foreign"; await ui.open(); assert.equal(ui.calls.length, 1);
});
test("self-delete blocked, user icons compact and inert, deleted subject never becomes audit actor", async () => {
  const ui = setup("user"); ui.state.me.id = ui.preview.id; await ui.open(); assert.equal(ui.calls.length, 0);
  ui.state.users = [{ id: ui.preview.id, username: "admin", name: "<img src=x>", role: "platform_admin", enabled: true }]; ui.sandbox.lifecycle.renderUsers();
  const buttons = ui.$("user-rows").children[0].children[5].children; assert.equal(buttons.length, 4);
  assert.equal(buttons[3].disabled, true); assert.match(buttons[3].title, /current account/);
  for (const button of buttons) { assert.equal(button["aria-label"], button.title); assert.equal(button.dataset.tooltip, button.title); assert.equal(button.children[0].tag, "svg"); assert.equal(button.children[0]["aria-hidden"], "true"); }
  ui.sandbox.renderAudit([{ event: "user.deleted", subject: { id: "deleted-id", username: "deleted-user" }, actor: { id: "actor-id", name: "Acting admin", display_source: "snapshot" } }]);
  const cells = ui.$("audit-rows").children[0].children; assert.match(text(cells[0]), /deleted-user/); assert.match(text(cells[1]), /Acting admin/); assert.doesNotMatch(text(cells[1]), /deleted-user/);
});
for (const enabled of [true, false]) test(`tenant ${enabled ? "suspend" : "resume"} PATCH preserves scoped id and explicit status`, async () => {
  const ui = setup("tenant"); ui.state.tenants[0].enabled = enabled; ui.sandbox.lifecycle.toggleTenant(); await ui.state.confirm();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.calls[0])), { path: "/api/tenants/tenant%2Fid", method: "PATCH", payload: { enabled: !enabled } });
});
test("tenant status confirmation rejects workspace change before mutation", async () => {
  const ui = setup("tenant"); ui.sandbox.lifecycle.toggleTenant(); ui.state.tenant = "other"; await assert.rejects(ui.state.confirm(), /Workspace changed/); assert.equal(ui.calls.length, 0);
});
for (const field of ["kind", "id", "name", "confirmation_token", "impact"]) test(`malformed preview ${field} fails closed`, async () => {
  const ui = setup(); ui.preview[field] = null; await ui.open(); ui.type("anything"); await ui.submit();
  assert.equal(ui.calls.length, 1); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
});
test("protected administrator preview explains creating another enabled administrator", async () => {
  const ui = setup("user"); ui.sandbox.api = async () => { throw new Error("Conflict (HTTP 409)"); }; await ui.open();
  assert.match(ui.messages.at(-1), /create or enable another administrator first/); assert.equal(ui.$("lifecycle-delete-submit").disabled, true);
});
test("closing a pending delete prevents stale completion from refreshing or reopening dialogs", async () => {
  const ui = setup(); await ui.open(); ui.type(ui.preview.name); let finish;
  ui.sandbox.api = () => new Promise((resolve) => { finish = resolve; }); const pending = ui.submit();
  ui.$("lifecycle-delete-dialog").close(); finish({ deleted: true, channel_id: ui.preview.id }); await pending;
  assert.equal(ui.refreshes(), 0); assert.equal(ui.$("lifecycle-delete-dialog").open, false); assert.equal(ui.$("lifecycle-delete-name").value, "");
});
test("reopening replaces a previous preview token and always starts with blank text", async () => {
  const ui = setup(); await ui.open(); ui.type(ui.preview.name); ui.preview.confirmation_token = "new-token";
  await ui.open(); assert.equal(ui.$("lifecycle-delete-name").value, ""); ui.type(ui.preview.name); await ui.submit();
  assert.equal(ui.calls[2].payload.confirmation_token, "new-token");
});
test("workspace reload and signout reset hooks clear pending deletion", async () => {
  const ui = setup(); await ui.open(); ui.type(ui.preview.name); ui.sandbox.lifecycle.reset();
  assert.equal(ui.$("lifecycle-delete-dialog").open, false); await ui.submit(); assert.equal(ui.calls.length, 1);
  const admin = readFileSync(resolve(root, "admin.js"), "utf8");
  assert.match(admin, /analytics\.reset\(\); specifications\.resetDeletion\(\); lifecycle\.reset\(\)/);
  assert.match(admin, /analytics\.selectTenant\(state\.tenant\); lifecycle\.reset\(\)/);
  assert.match(admin, /lifecycle\.reset\(\); state\.csrf = session\.csrf_token/);
});
test("scope-specific impact explains irreversible cleanup, retention, and offboarding", async () => {
  for (const kind of ["tenant", "channel", "user"]) {
    const ui = setup(kind); await ui.open(); const warning = ui.$("lifecycle-delete-warning").textContent;
    assert.match(warning, /Irreversible/); assert.match(warning, /normal bounded retention/); assert.match(warning, /NOT purged/);
    assert.match(warning, kind === "tenant" ? /entire workspace.*all assigned tenant users/ : kind === "channel" ? /Specifications and users are kept/ : /rotate exposed channel keys/);
  }
});
