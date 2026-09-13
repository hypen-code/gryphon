"use strict";
const assert = require("node:assert/strict");
const { join } = require("node:path");
async function controls(page, selector) {
  for (const width of [390, 1400]) {
    await page.setViewport({ width, height: 1000 });
    const buttons = await page.$$eval(selector, (items) => items.map((button) => ({ label: button.getAttribute("aria-label"), title: button.title, tooltip: button.dataset.tooltip, svg: button.querySelector("svg")?.getAttribute("aria-hidden"), width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height })));
    assert.ok(buttons.length);
    for (const button of buttons) { assert.equal(button.label, button.title); assert.equal(button.tooltip, button.title); assert.equal(button.svg, "true"); assert.ok(button.width >= (width === 390 ? 44 : 32)); assert.ok(button.height >= (width === 390 ? 44 : 32)); }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
}
async function createUser(page, name, tenant, password, submit) {
  await page.click('[data-page="users"]'); await page.click("#new-user");
  await page.type("#user-name", `Display ${name}`); await page.type("#user-username", name);
  await page.type("#user-password", password); await page.type("#user-confirm", password); await page.select("#user-tenant", tenant);
  return submit(page, "#user-form", "/api/users");
}
async function openDeletion(page, selector, idle) {
  await page.click(selector); await idle(page); await page.waitForSelector("#lifecycle-delete-dialog[open]");
  assert.equal(await page.$eval("#lifecycle-delete-name", (input) => input.value), "");
  assert.equal(await page.$eval("#lifecycle-delete-submit", (button) => button.disabled), true);
}
async function typeName(page, value) {
  await page.$eval("#lifecycle-delete-name", (input, name) => { input.value = name; input.dispatchEvent(new Event("input")); }, value);
}
async function staleChannel(page, channel, idle, submit, close) {
  const selector = `[data-channel-id="${channel.id}"] [data-action="delete"]`;
  await openDeletion(page, selector, idle);
  await typeName(page, channel.name); await page.keyboard.press("Escape");
  assert.equal(await page.$eval("#lifecycle-delete-name", (input) => input.value), "");
  await openDeletion(page, selector, idle);
  for (const name of [channel.name.toLowerCase(), ` ${channel.name}`, `${channel.name} `]) { await typeName(page, name); assert.equal(await page.$eval("#lifecycle-delete-submit", (button) => button.disabled), true); }
  await page.evaluate(async (channel) => { await api(tenantPath(`/channels/${channel.id}`), "PATCH", { name: channel.name, spec_ids: channel.spec_ids, sandbox_mode: channel.sandbox_mode, allowed_imports: channel.allowed_imports, enabled: channel.enabled, include_function_summaries: !channel.include_function_summaries }); }, channel);
  let attempts = 0; const watch = (request) => { if (request.method() === "DELETE") attempts++; }; page.on("request", watch);
  await typeName(page, channel.name); await submit(page, "#lifecycle-delete-form", `/channels/${channel.id}`, 409, "DELETE");
  await page.$eval("#lifecycle-delete-form", (form) => form.dispatchEvent(new Event("submit", { cancelable: true }))); await idle(page); assert.equal(attempts, 1); page.off("request", watch);
  assert.equal(await page.$eval("#lifecycle-delete-name", (input) => input.value), ""); assert.equal(await page.$eval("#lifecycle-delete-submit", (button) => button.disabled), true);
  await close(page, "#lifecycle-delete-dialog"); await openDeletion(page, selector, idle);
  await typeName(page, channel.name); await submit(page, "#lifecycle-delete-form", `/channels/${channel.id}`, 200, "DELETE");
  assert.equal(await page.$(`[data-channel-id="${channel.id}"]`), null);
}
async function userDeletion(page, user, idle, submit) {
  await page.click('[data-page="users"]'); await controls(page, ".user-actions .lifecycle-action");
  const selector = `[data-user-id="${user.id}"] [data-action="delete"]`;
  await page.focus(selector); await page.keyboard.press("Enter"); await idle(page);
  await page.waitForSelector("#lifecycle-delete-dialog[open]");
  assert.match(await page.$eval("#lifecycle-delete-warning", (body) => body.textContent), /rotate exposed channel keys/);
  await typeName(page, user.name); assert.equal(await page.$eval("#lifecycle-delete-submit", (button) => button.disabled), true);
  await typeName(page, user.username); await submit(page, "#lifecycle-delete-form", `/api/users/${user.id}`, 200, "DELETE");
  assert.equal(await page.$(`[data-user-id="${user.id}"]`), null);
  await page.click('[data-page="audit"]');
  const rows = await page.$$eval("#audit-rows tr", (rows, id) => rows.filter((row) => row.children[0].textContent.includes(id)).map((row) => ({ subject: row.children[0].textContent, actor: row.children[1].textContent })), user.id);
  assert.ok(rows.length); assert.ok(rows.some((row) => /deleted/.test(row.subject)));
  for (const row of rows) { assert.match(row.actor, /Bootstrap administrator/); assert.ok(!row.actor.includes(user.username)); }
}
async function channelAccess(page, channel, token) {
  return page.evaluate(async ({ channel, token }) => {
    const response = await fetch(`/mcp/${channel}`, { method: "POST", headers: { Authorization: `Bearer ${token}`, Accept: "application/json, text/event-stream", "Content-Type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "disposable-browser", version: "1" } } }) });
    await response.text(); return response.status;
  }, { channel, token });
}
async function tenantDeletion(page, tenant, idle, submit, close) {
  await page.click('[data-page="overview"]');
  await openDeletion(page, "#delete-tenant", idle);
  assert.match(await page.$eval("#lifecycle-delete-impact", (body) => body.textContent), /1 users · 1 channels · 1 specification versions/);
  const extra = await page.evaluate(async () => api(tenantPath("/channels"), "POST", { name: "New child", spec_ids: [], sandbox_mode: "restricted", allowed_imports: [] }));
  await typeName(page, tenant.name); await submit(page, "#lifecycle-delete-form", `/api/tenants/${tenant.id}`, 409, "DELETE");
  assert.equal(await page.$eval("#lifecycle-delete-name", (input) => input.value), "");
  await close(page, "#lifecycle-delete-dialog"); await openDeletion(page, "#delete-tenant", idle);
  assert.match(await page.$eval("#lifecycle-delete-impact", (body) => body.textContent), /1 users · 2 channels · 1 specification versions/);
  await typeName(page, tenant.name); await submit(page, "#lifecycle-delete-form", `/api/tenants/${tenant.id}`, 200, "DELETE");
  assert.equal(await page.evaluate((id) => state.tenants.some((tenant) => tenant.id === id), tenant.id), false);
  assert.deepEqual(await page.evaluate(async (id) => (await (await fetch(`/api/tenants/${id}/channels`)).json()).items, tenant.id), []);
  assert.equal(await page.evaluate(async (id) => (await fetch(`/api/tenants/${id}/deletion`)).status, tenant.id), 404);
  assert.equal(await page.evaluate(async (id) => (await fetch(`/mcp/${id}`)).status, extra.id), 401);
}
module.exports = async function lifecycleFlow(page, input, { idle, submit, close, dialogSizes }) {
  const originalTenant = await page.evaluate(() => state.tenant);
  await page.click("#logout"); await idle(page); await page.click(".bootstrap-login summary"); await page.type("#login-token", input.token); await submit(page, "#login-form", "/api/login", 200);
  await page.click("#new-tenant"); await page.type("#tenant-name", "Disposable lifecycle"); const tenant = await submit(page, "#tenant-form", "/api/tenants");
  await controls(page, ".tenant-actions .lifecycle-action");
  for (const enabled of [false, true]) {
    assert.equal(await page.$eval("#toggle-tenant", (button) => button.title), enabled ? "Resume tenant" : "Suspend tenant");
    await page.click("#toggle-tenant"); const changed = await submit(page, "#confirm-form", `/api/tenants/${tenant.id}`, 200, "PATCH"); assert.equal(changed.enabled, enabled);
  }
  await page.click('[data-page="specs"]'); await page.click("#new-spec"); await page.type("#spec-name", "LifecycleSpec"); await (await page.$("#spec-file")).uploadFile(join(input.root, "spec-1.json")); const spec = await submit(page, "#spec-form", "/specs");
  const users = []; for (const name of ["lifecycle.delete", "lifecycle.member"]) users.push(await createUser(page, name, tenant.id, input.password, submit));
  const channels = [];
  for (const name of ["Delete channel", "Retained until workspace deletion"]) {
    await page.click('[data-page="channels"]'); await page.click("#new-channel"); await page.type("#channel-name", name); await page.click(`#spec-options input[value="${spec.id}"]`); channels.push(await submit(page, "#channel-form", "/channels"));
  }
  await controls(page, ".channel-actions .lifecycle-action"); await staleChannel(page, channels[0], idle, submit, close);
  await page.click(`[data-channel-id="${channels[1].id}"] [data-action="rotate"]`);
  const key = await submit(page, "#confirm-form", `/channels/${channels[1].id}/rotate`, 200); await close(page, "#key-dialog");
  assert.equal(await page.evaluate((id) => state.specs.some((spec) => spec.id === id), spec.id), true);
  await userDeletion(page, users[0], idle, submit); assert.equal(await channelAccess(page, channels[1].id, key.token), 200);
  await page.click('[data-page="overview"]'); await openDeletion(page, "#delete-tenant", idle); await dialogSizes(page, "#lifecycle-delete-dialog"); await close(page, "#lifecycle-delete-dialog");
  await tenantDeletion(page, tenant, idle, submit, close); assert.equal(await channelAccess(page, channels[1].id, key.token), 401);
  assert.equal(await page.evaluate((id) => state.users.some((user) => user.id === id), users[1].id), false);
  assert.equal(await page.evaluate((id) => state.tenant === id, originalTenant), true);
  await page.click('[data-page="specs"]');
  return "disposable tenant suspend/resume, compact accessible user/channel icons, exact username/channel/workspace confirmations, stale channel and new-child 409 without retry, user/entire-workspace deletion with retained audit attribution verified";
};
