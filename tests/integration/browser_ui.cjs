"use strict";
const assert = require("node:assert/strict");
const { readFile, stat } = require("node:fs/promises");
const { join } = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const puppeteer = require("puppeteer");
const lifecycleFlow = require("./browser_lifecycle.cjs");

async function idle(page) { await page.waitForFunction(() => !state.busy); }
async function submit(page, form, suffix, status = 201, method = "POST") {
  const received = page.waitForResponse((response) => response.request().method() === method && new URL(response.url()).pathname.endsWith(suffix));
  await page.click(`${form} button[type=submit]`); const response = await received;
  assert.equal(response.status(), status, `Unexpected status for ${suffix}`); await idle(page);
  return response.json();
}
async function rowButton(page, id, action) {
  return page.$(`#spec-rows tr[data-spec-id="${id}"] [data-spec-action="${action}"]`);
}
async function historySource(page, latest, previous) {
  await (await rowButton(page, latest, "history")).click(); await page.waitForSelector("#spec-history-dialog[open]");
  await dialogSizes(page, "#spec-history-dialog");
  const source = await page.$(`#spec-history-rows tr[data-spec-id="${previous}"] [data-spec-action="source"]`);
  assert.ok(source); await source.click(); await page.waitForSelector("#source-dialog[open]");
}
async function pinnedEdit(page, channelId, older, latest) {
  await page.click('[data-page="channels"]');
  const edit = await page.evaluateHandle((id) => [...document.querySelectorAll(".channel-card")].find((card) => card.querySelector(".channel-id").textContent === id).querySelector('[data-action="edit"]'), channelId);
  await edit.asElement().click();
  assert.equal(await page.$eval(`#spec-options input[value="${older}"]`, (input) => input.checked), true);
  assert.match(await page.$eval("#spec-options", (options) => options.textContent), /pinned older version/);
  assert.equal(await page.$(`#spec-options input[value="${latest}"]`), null);
  await page.click("#channel-function-summaries");
  const saved = await submit(page, "#channel-form", `/channels/${channelId}`, 200, "PATCH"); assert.deepEqual(saved.spec_ids, [older]);
  const reopen = await page.evaluateHandle((id) => [...document.querySelectorAll(".channel-card")].find((card) => card.querySelector(".channel-id").textContent === id).querySelector('[data-action="edit"]'), channelId);
  await reopen.asElement().click(); await page.click("#spec-options button"); await idle(page);
  assert.equal(await page.$eval(`#spec-options input[value="${latest}"]`, (input) => input.checked), true);
  assert.equal(await page.$(`#spec-options input[value="${older}"]`), null);
  await close(page, "#channel-dialog"); await binding(page, channelId, older);
  await page.click('[data-page="specs"]');
}
async function close(page, id) { await page.click(`${id} [data-close]`); await page.waitForSelector(`${id}[open]`, { hidden: true }); }
async function dialogSizes(page, id) {
  for (const width of [390, 1400]) {
    await page.setViewport({ width, height: 1000 });
    assert.equal(await page.$eval(id, (dialog) => { const box = dialog.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth; }), true);
  }
}
async function icons(page, width) {
  await page.setViewport({ width, height: 1000 });
  const metrics = await page.$$eval("nav [data-page]", (links) => links.map((link) => {
    const svg = link.querySelector("svg"); const box = svg.getBoundingClientRect();
    return { page: link.dataset.page, width: box.width, height: box.height, fill: svg.getAttribute("fill"), hidden: svg.getAttribute("aria-hidden"), display: getComputedStyle(svg).display };
  }));
  assert.equal(metrics.length, 6);
  for (const metric of metrics) { assert.equal(metric.width, 20); assert.equal(metric.height, 20); assert.equal(metric.fill, "none"); assert.equal(metric.hidden, "true"); assert.notEqual(metric.display, "none"); }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  return `${width}px: six visible 20x20 outlined icons, no document overflow`;
}
async function modes(page) {
  await page.click("#new-spec");
  assert.equal(await page.$eval("#spec-read-only-filter", (input) => input.checked), true);
  for (const kind of ["file", "openapi", "ucp", "file"]) {
    await page.select("#spec-kind", kind);
    const controls = await page.evaluate(() => ({ file: document.querySelector("#spec-file").disabled, url: document.querySelector("#spec-url").disabled, required: document.querySelector("#spec-file").required }));
    assert.deepEqual(controls, { file: kind !== "file", url: kind === "file", required: kind === "file" });
    assert.equal(await page.evaluate(() => document.querySelector("#spec-dialog").getBoundingClientRect().width <= innerWidth), true);
  }
  await close(page, "#spec-dialog");
}
async function channel(page, spec, name) {
  await page.click('[data-page="channels"]'); await page.click("#new-channel"); await page.type("#channel-name", name);
  await page.click(`#spec-options input[value="${spec}"]`);
  assert.equal(await page.$eval("#channel-function-summaries", (input) => input.checked), false);
  if (name === "File channel") await page.click("#channel-function-summaries");
  const created = await submit(page, "#channel-form", "/channels");
  assert.equal(created.include_function_summaries, name === "File channel");
  if (created.include_function_summaries) await editSummaries(page, created);
  await page.click('[data-page="specs"]'); return created;
}
async function editSummaries(page, channel) {
  const edit = await page.evaluateHandle((id) => [...document.querySelectorAll(".channel-card")].find((card) => card.querySelector(".channel-id").textContent === id).querySelector('[data-action="edit"]'), channel.id);
  await edit.asElement().click();
  assert.equal(await page.$eval("#channel-function-summaries", (input) => input.checked), true);
  await page.click("#channel-function-summaries");
  const updated = await submit(page, "#channel-form", `/channels/${channel.id}`, 200, "PATCH");
  assert.equal(updated.include_function_summaries, false);
  assert.match(await page.$eval("#channel-list", (list) => list.textContent), /Compact server summaries/);
}
async function refreshFile(page, root, spec, version, update) {
  await (await rowButton(page, spec, "refresh")).click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await page.$eval("#spec-update-channels", (input) => input.checked), true);
  assert.equal(await page.$eval("#spec-replacement", (input) => input.required && !input.disabled), true);
  if (!update) await page.click("#spec-update-channels");
  await (await page.$("#spec-replacement")).uploadFile(join(root, `spec-${version}.json`));
  return submit(page, "#spec-refresh-form", "/refresh");
}
async function binding(page, channel, spec) {
  const ids = await page.evaluate((id) => state.channels.find((channel) => channel.id === id).spec_ids, channel);
  assert.deepEqual(ids, [spec]);
}
async function fileFlow(page, root) {
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserFile");
  await (await page.$("#spec-file")).uploadFile(join(root, "spec-1.json"));
  const original = await submit(page, "#spec-form", "/specs");
  assert.deepEqual(original.diagnostics, { total_operations: 2, available_operations: 1, filtered_operations: 1, unsupported_operations: 0 });
  assert.match(await page.$eval("#spec-rows", (rows) => rows.textContent), /1 available/);
  const bound = await channel(page, original.id, "File channel");
  const second = await refreshFile(page, root, original.id, 2, true); await binding(page, bound.id, second.id);
  const third = await refreshFile(page, root, second.id, 3, false); await binding(page, bound.id, second.id);
  assert.equal(third.parent_id, second.id);
  await page.reload(); await idle(page); await page.waitForSelector(`#spec-rows tr[data-spec-id="${third.id}"]`);
  assert.equal(await page.$$eval("#spec-rows tr", (rows) => rows.length), 1);
  await pinnedEdit(page, bound.id, second.id, third.id);
  await (await rowButton(page, third.id, "history")).click();
  for (const version of [original, second]) {
    await page.click(`#spec-history-rows tr[data-spec-id="${version.id}"] [data-spec-action="details"]`);
    const body = await page.$eval("#spec-details-content", (body) => body.textContent); assert.ok(body.includes(version.id)); assert.ok(body.includes(version.sha256));
    if (version === second) assert.match(body, /Pinned channels: File channel/);
    await close(page, "#spec-details-dialog");
  }
  await close(page, "#spec-history-dialog"); await historySource(page, third.id, original.id);
  assert.equal(await page.$$eval("#spec-history-rows tr", (rows) => rows.length), 3);
  assert.match(await page.$eval("#spec-history-rows", (rows) => rows.textContent), /1 bound channel/);
  await dialogSizes(page, "#source-dialog");
  const source = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.equal(source.paths["/data"].get.operationId, "read_v1");
  const cdp = await page.createCDPSession(); await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: root });
  await page.click("#download-source");
  for (let tries = 0; tries < 50; tries++) { if (await stat(join(root, "BrowserFile.json")).catch(() => null)) break; await delay(50); }
  assert.deepEqual(JSON.parse(await readFile(join(root, "BrowserFile.json"), "utf8")), source);
  await close(page, "#source-dialog"); await close(page, "#spec-history-dialog"); await binding(page, bound.id, second.id);
  const latestBound = await channel(page, third.id, "Filter channel"); const filtered = await filterFlow(page, third, latestBound.id);
  const postBound = await channel(page, filtered.id, "Automatic POST channel"); await automaticPOSTFlow(page, filtered, postBound.id);
  await binding(page, bound.id, second.id);
  return "one lineage row after repeated refresh/filter changes and reload; History downloads and pinned channel edits preserved; POST inclusion persists with no manual controls or requests; retired route returns 404; channel summaries POST/PATCH persisted";
}
async function savedFilter(page, id) { return rowButton(page, id, "filter"); }
async function filterFlow(page, original, channelId) {
  const filter = await savedFilter(page, original.id); assert.equal(await filter.evaluate((input) => input.getAttribute("aria-pressed") === "true"), true);
  await filter.click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await filter.evaluate((input) => input.getAttribute("aria-pressed") === "true"), true);
  assert.equal(await page.$eval("#spec-refresh-read-only-filter", (input) => input.checked), false);
  assert.equal(await page.$eval("#spec-replacement", (input) => input.disabled && !input.required), true);
  assert.match(await page.$eval("#spec-refresh-help", (p) => p.textContent), /No URL refetch or replacement upload/);
  await close(page, "#spec-refresh-dialog"); await binding(page, channelId, original.id);
  await filter.click(); const changed = await submit(page, "#spec-refresh-form", "/filter");
  assert.equal(changed.read_only_filter, false); assert.equal(changed.parent_id, original.id);
  assert.equal(changed.diagnostics.available_operations, original.source_type === "ucp_url" ? original.diagnostics.available_operations : 2);
  await binding(page, channelId, changed.id);
  assert.equal(await page.$(`#spec-rows tr[data-spec-id="${original.id}"]`), null);
  await (await rowButton(page, changed.id, "history")).click();
  assert.match(await page.$eval(`#spec-history-rows tr[data-spec-id="${original.id}"]`, (row) => row.textContent), /Filter on/);
  await close(page, "#spec-history-dialog");
  const next = await savedFilter(page, changed.id); assert.equal(await next.evaluate((input) => input.getAttribute("aria-pressed") === "true"), false);
  await next.click(); await page.click("#spec-refresh-read-only-filter");
  const same = await submit(page, "#spec-refresh-form", "/filter", 200); assert.equal(same.id, changed.id);
  await (await savedFilter(page, changed.id)).click(); await page.click("#spec-update-channels");
  const restored = await submit(page, "#spec-refresh-form", "/filter"); assert.equal(restored.read_only_filter, true);
  await binding(page, channelId, changed.id); return restored;
}
async function retiredPOSTRoute(page, spec) {
  assert.equal(await page.$('[id^="spec-permissions"]'), null);
  assert.doesNotMatch(await page.$eval("#spec-rows", (rows) => rows.textContent), /POST read permissions|POST read status/);
  const statuses = await page.evaluate(async (id) => {
    const path = tenantPath(`/specs/${id}/post-reads`);
    const get = await fetch(path);
    const post = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }, body: JSON.stringify({ functions: [] }) });
    return [get.status, post.status];
  }, spec);
  assert.deepEqual(statuses, [404, 404]);
}
async function automaticPOSTFlow(page, spec, channelId) {
  const requests = []; const watch = (request) => { if (request.url().includes("/post-reads")) requests.push(request.url()); };
  page.on("request", watch);
  await (await savedFilter(page, spec.id)).click();
  assert.match(await page.$eval("#spec-refresh-filter-help", (node) => node.textContent), /Included POST operations execute automatically and may have side effects/);
  const included = await submit(page, "#spec-refresh-form", "/filter");
  assert.equal(included.read_only_filter, false); assert.equal(included.diagnostics.available_operations, 2);
  await binding(page, channelId, included.id);
  await page.reload(); await idle(page); await page.waitForSelector(`#spec-rows tr[data-spec-id="${included.id}"]`);
  assert.equal(await page.$$eval("#spec-rows tr", (rows) => rows.length), 1);
  assert.equal(await (await savedFilter(page, included.id)).evaluate((input) => input.getAttribute("aria-pressed") === "true"), false);
  assert.deepEqual(requests, []); page.off("request", watch);
  await retiredPOSTRoute(page, included.id);
}
async function urlFlow(page) {
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserURL"); await page.select("#spec-kind", "openapi");
  await page.type("#spec-url", "https://example.com/openapi.json"); const original = await submit(page, "#spec-form", "/specs");
  assert.equal(original.source_type, "openapi_url"); const bound = await channel(page, original.id, "URL channel");
  await (await rowButton(page, original.id, "refresh")).click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await page.$eval("#spec-replacement", (input) => input.disabled && !input.required), true);
  const updated = await submit(page, "#spec-refresh-form", "/refresh"); await binding(page, bound.id, updated.id);
  assert.equal(updated.source_url, original.source_url); assert.equal(updated.parent_id, original.id);
  await (await rowButton(page, updated.id, "refresh")).click(); const same = await submit(page, "#spec-refresh-form", "/refresh", 200);
  assert.equal(same.id, updated.id); assert.match(await page.$eval("#notice-text", (notice) => notice.textContent), /unchanged/);
  await filterFlow(page, updated, bound.id);
  return "OpenAPI URL import/refetch uses synthetic pinned HTTP; saved filter changes do not refetch; bindings, stored checkbox state and unchanged HTTP 200 verified";
}
async function ucpMode(page) {
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserUCP"); await page.select("#spec-kind", "ucp");
  await page.type("#spec-url", "https://example.com"); const original = await submit(page, "#spec-form", "/specs");
  assert.equal(original.source_type, "ucp_url"); assert.equal(original.source_url, "https://example.com");
  assert.equal(original.diagnostics.available_operations, 1);
  assert.ok(original.warnings.some((warning) => warning.includes("Non-GET")));
  assert.ok(original.warnings.some((warning) => warning.includes("UCP-Agent")));
  assert.doesNotMatch(await page.$eval("#spec-rows", (rows) => rows.textContent), /UCP-Agent/);
  await (await rowButton(page, original.id, "details")).click(); assert.match(await page.$eval("#spec-details-content", (body) => body.textContent), /UCP-Agent/); await close(page, "#spec-details-dialog");
  const bound = await channel(page, original.id, "UCP channel");
  await (await rowButton(page, original.id, "source")).click(); await page.waitForSelector("#source-dialog[open]");
  const oldDocument = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.deepEqual(Object.keys(oldDocument.paths), ["/checkout-sessions/{id}"]);
  await close(page, "#source-dialog");
  await (await rowButton(page, original.id, "refresh")).click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await page.$eval("#spec-replacement", (input) => input.disabled), true);
  const updated = await submit(page, "#spec-refresh-form", "/refresh"); await binding(page, bound.id, updated.id);
  assert.equal(updated.source_type, "ucp_url"); assert.equal(updated.source_url, original.source_url);
  assert.equal(updated.parent_id, original.id); assert.equal(updated.diagnostics.available_operations, 3);
  assert.equal(await page.evaluate((id) => state.channels.find((channel) => channel.id === id).revision, bound.id), bound.revision + 1);
  await (await rowButton(page, updated.id, "source")).click(); await page.waitForSelector("#source-dialog[open]");
  const compiled = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.deepEqual(Object.values(compiled.paths).map((path) => path.get.operationId).sort(), ["get_cart", "get_checkout", "get_order"]);
  await close(page, "#source-dialog");
  await historySource(page, updated.id, original.id);
  assert.deepEqual(JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent)), oldDocument);
  await close(page, "#source-dialog"); await close(page, "#spec-history-dialog");
  await filterFlow(page, updated, bound.id);
  return "UCP import/refetch: GET checkout/cart/order, warnings and old snapshots retained; disabling saved filter does not expand adapter subset or refetch";
}
async function ucpMCPMode(page) {
  const source = "https://shop.example.com/api/ucp/mcp"; const endpoint = "https://shop.myshopify.com/api/ucp/mcp";
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserMCP"); await page.select("#spec-kind", "ucp");
  await page.type("#spec-url", source); const original = await submit(page, "#spec-form", "/specs");
  assert.equal(original.source_url, source); assert.equal(original.diagnostics.available_operations, 1);
  assert.ok(original.warnings.some((warning) => /unsupported MCP tool schema/i.test(warning)));
  await (await rowButton(page, original.id, "source")).click(); await page.waitForSelector("#source-dialog[open]");
  const compiled = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.equal(compiled["x-gryphon-ucp"].transport, "mcp"); assert.equal(compiled.servers[0].url, endpoint);
  assert.match(await page.$eval("#source-metadata", (p) => p.textContent), /Resolved UCP transport: mcp/);
  assert.ok((await page.$eval("#source-metadata", (p) => p.textContent)).includes(endpoint));
  const operation = Object.values(compiled.paths).find((path) => path.post.operationId === "get_cart").post;
  assert.deepEqual(operation.requestBody.content["application/json"].schema.required, ["meta"]);
  await dialogSizes(page, "#source-dialog"); await close(page, "#source-dialog");
  const bound = await channel(page, original.id, "MCP channel");
  await (await savedFilter(page, original.id)).click(); const included = await submit(page, "#spec-refresh-form", "/filter");
  assert.equal(included.diagnostics.available_operations, 2); assert.equal(included.read_only_filter, false);
  await binding(page, bound.id, included.id);
  await (await rowButton(page, included.id, "refresh")).click(); const same = await submit(page, "#spec-refresh-form", "/refresh", 200);
  assert.equal(same.id, included.id);
  for (const [index, url] of ["https://shop.example.com", "https://shop.example.com/.well-known/ucp"].entries()) {
    await page.click("#new-spec"); await page.type("#spec-name", `BrowserMCPProfile${index}`); await page.select("#spec-kind", "ucp");
    await page.type("#spec-url", url); const imported = await submit(page, "#spec-form", "/specs");
    assert.equal(imported.diagnostics.available_operations, 1);
  }
  return "UCP MCP endpoint/root/profile discovered server-side with canonical delegated binding, required caller meta, explicit unsupported schema warnings and network-free POST inclusion";
}
async function tenantIdentityAndAudit(page, password) {
  const attack = '<img src=x onerror="alert(1)">'; const tenant = await page.evaluate(() => state.tenant);
  await page.click('[data-page="users"]'); await page.click("#new-user");
  await page.type("#user-name", attack); await page.type("#user-username", "browser.tenant");
  await page.type("#user-password", password); await page.type("#user-confirm", password);
  await page.select("#user-role", "tenant_user"); await page.select("#user-tenant", tenant);
  const account = await submit(page, "#user-form", "/api/users");
  await page.click('[data-page="audit"]');
  assert.match(await page.$eval("#audit-rows", (rows) => rows.textContent), /Bootstrap administrator/);
  await page.click("#logout"); await idle(page);
  const userRequests = []; const watch = (request) => { if (new URL(request.url()).pathname.startsWith("/api/users")) userRequests.push(request.url()); };
  page.on("request", watch);
  await page.type("#login-username", "browser.tenant"); await page.type("#login-password", password);
  await submit(page, "#account-login-form", "/api/login", 200);
  await page.click('[data-page="specs"]');
  const spec = await page.$eval("#spec-rows tr", (row) => row.dataset.specId);
  await retiredPOSTRoute(page, spec);
  await page.click('[data-page="channels"]'); await page.click('.channel-card [data-action="edit"]');
  const channelId = await page.evaluate(() => state.editing.id); await page.type("#channel-name", " edited");
  await submit(page, "#channel-form", `/channels/${channelId}`, 200, "PATCH");
  await page.click('[data-page="audit"]');
  const actor = await page.$$eval("#audit-rows tr", (rows, id) => rows.find((row) => row.children[1].textContent.includes(id)).children[1].textContent, account.id);
  assert.ok(actor.includes(attack)); assert.ok(actor.includes("browser.tenant")); assert.ok(actor.includes(`Actor ID: ${account.id}`));
  assert.equal(await page.$("#audit-rows img"), null); assert.deepEqual(userRequests, []); page.off("request", watch);
  await page.click('[data-page="specs"]');
  return "tenant has no manual POST controls and retired route returns 404; no user-list requests; named actor is escaped and distinct from account subject; bootstrap actor is explicit";
}
async function specIcons(page) {
  for (const width of [390, 1400]) {
    await page.setViewport({ width, height: 1000 });
    const controls = await page.$$eval('#spec-rows [data-spec-action]', (buttons) => buttons.map((button) => ({ label: button.getAttribute("aria-label"), title: button.title, tooltip: button.dataset.tooltip, svg: button.querySelector("svg").getAttribute("aria-hidden"), width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height })));
    for (const button of controls) { assert.equal(button.label, button.title); assert.equal(button.title, button.tooltip); assert.equal(button.svg, "true"); assert.ok(button.width >= (width === 390 ? 44 : 32)); assert.ok(button.height >= (width === 390 ? 44 : 32)); }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
  await page.focus('#spec-rows [data-spec-action="details"]'); await page.keyboard.press("Enter"); await page.waitForSelector("#spec-details-dialog[open]");
  await dialogSizes(page, "#spec-details-dialog"); await page.keyboard.press("Escape"); await page.waitForSelector("#spec-details-dialog[open]", { hidden: true });
  await page.waitForFunction(() => document.activeElement.dataset.specAction === "details");
  assert.equal(await page.evaluate(() => getComputedStyle(document.activeElement, "::after").opacity), "1");
}
async function deletionFlow(page, root) {
  const before = await page.evaluate(() => state.specs.map((spec) => spec.id)); const deletes = []; const watch = (request) => { if (request.method() === "DELETE") deletes.push(request.url()); }; page.on("request", watch);
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserDelete"); await (await page.$("#spec-file")).uploadFile(join(root, "spec-1.json"));
  const original = await submit(page, "#spec-form", "/specs"); const first = await channel(page, original.id, "Delete older binding");
  const latest = await refreshFile(page, root, original.id, 2, false); const second = await channel(page, latest.id, "Delete latest binding");
  await (await rowButton(page, latest.id, "download")).click(); await idle(page);
  for (let tries = 0; tries < 50; tries++) { if (await stat(join(root, "BrowserDelete.json")).catch(() => null)) break; await delay(50); }
  assert.equal(JSON.parse(await readFile(join(root, "BrowserDelete.json"), "utf8")).paths["/data"].get.operationId, "read_v2");
  await (await rowButton(page, latest.id, "delete")).click(); await page.waitForSelector("#spec-delete-dialog[open]"); await idle(page);
  await page.type("#spec-delete-name", "BrowserDelete"); await page.keyboard.press("Escape");
  assert.equal(await page.$eval("#spec-delete-name", (input) => input.value), "");
  await (await rowButton(page, latest.id, "delete")).click(); await idle(page); await dialogSizes(page, "#spec-delete-dialog");
  assert.match(await page.$eval("#spec-delete-impact", (body) => body.textContent), /2 saved versions.*2 channels/s);
  for (const name of ["", "browserdelete", " BrowserDelete", "BrowserDelete "]) {
    await page.$eval("#spec-delete-name", (input, value) => { input.value = value; input.dispatchEvent(new Event("input")); }, name);
    assert.equal(await page.$eval("#spec-delete-submit", (button) => button.disabled), true);
    await page.$eval("#spec-delete-form", (form) => form.dispatchEvent(new Event("submit", { cancelable: true }))); await idle(page);
  }
  assert.equal(deletes.length, 0); await page.$eval("#spec-delete-name", (input) => { input.value = "BrowserDelete"; input.dispatchEvent(new Event("input")); });
  await page.evaluate(async (id) => { const { spec_ids, sandbox_mode, allowed_imports, enabled } = state.channels.find((channel) => channel.id === id); await api(tenantPath(`/channels/${id}`), "PATCH", { name: "Changed binding channel", spec_ids, sandbox_mode, allowed_imports, enabled }); }, first.id);
  await submit(page, "#spec-delete-form", `/specs/${latest.id}`, 409, "DELETE");
  assert.equal(await page.$eval("#spec-delete-submit", (button) => button.disabled), true);
  assert.match(await page.$eval("#spec-delete-dialog .dialog-message", (body) => body.textContent), /fresh preview/);
  await page.$eval("#spec-delete-form", (form) => form.dispatchEvent(new Event("submit", { cancelable: true }))); await idle(page);
  assert.equal(deletes.length, 1); await close(page, "#spec-delete-dialog"); await (await rowButton(page, latest.id, "delete")).click(); await idle(page);
  await page.type("#spec-delete-name", "BrowserDelete"); const result = await submit(page, "#spec-delete-form", `/specs/${latest.id}`, 200, "DELETE");
  assert.equal(deletes.length, 2); page.off("request", watch); assert.equal(result.deleted, true); assert.deepEqual(result.deleted_spec_ids.sort(), [original.id, latest.id].sort());
  assert.equal(await page.$eval("#spec-delete-name", (input) => input.value), "");
  const retained = await page.evaluate((ids) => ({ specs: state.specs.map((spec) => spec.id), channels: state.channels.filter((channel) => ids.includes(channel.id)).map((channel) => channel.spec_ids) }), [first.id, second.id]);
  assert.deepEqual(retained.specs.sort(), before.sort()); assert.deepEqual(retained.channels, [[], []]);
  assert.equal(await page.$(`#spec-rows tr[data-spec-id="${latest.id}"]`), null);
  assert.deepEqual(await page.evaluate(async (ids) => Promise.all(ids.map(async (id) => (await fetch(tenantPath(`/specs/${id}`))).status)), [original.id, latest.id]), [404, 404]);
  await page.click('[data-page="audit"]'); assert.match(await page.$$eval("#audit-rows tr", (rows) => rows.find((row) => row.children[0].textContent.startsWith("spec_deleted")).children[1].textContent), /browser.tenant/); await page.click('[data-page="specs"]');
  return "throwaway lineage deleted by exact typed name; Escape clears; stale preview returns 409 without retry; older/latest bindings detach while channels and other APIs remain";
}
async function notifications(page) {
  await page.evaluate(() => { notify("Dismiss while busy"); state.busy = true; });
  await page.click("#dismiss-notice");
  assert.equal(await page.$eval("#notice", (notice) => notice.hidden), true);
  await page.evaluate(() => { state.busy = false; });
  await page.click("#new-spec"); await page.evaluate(() => notify("Keep this inline validation error", true));
  await page.waitForFunction(() => document.querySelector("#notice").hidden, { timeout: 12000 });
  assert.equal(await page.$eval("#notice-text", (notice) => notice.textContent), "");
  assert.equal(await page.$eval("#spec-dialog .dialog-message", (message) => message.textContent), "Keep this inline validation error");
  await close(page, "#spec-dialog");
  return "SVG notification dismiss works while busy; real ten-second expiry clears toast while inline dialog error survives";
}
async function main(input) {
  const browser = await puppeteer.launch({ headless: true }); const report = []; const faults = []; const external = [];
  try {
    const page = await browser.newPage(); page.setDefaultTimeout(15000); page.on("pageerror", (error) => faults.push(error.message));
    await page.setRequestInterception(true);
    page.on("request", (request) => { if (new URL(request.url()).origin !== input.origin && !request.url().startsWith("data:") && !request.url().startsWith("blob:")) { external.push(new URL(request.url()).hostname); request.abort(); } else request.continue(); });
    await page.goto(input.origin); await idle(page); await page.click(".bootstrap-login summary"); await page.type("#login-token", input.token);
    await submit(page, "#login-form", "/api/login", 200); await page.waitForSelector("#app:not([hidden])");
    await page.click("#new-tenant"); await page.type("#tenant-name", "Browser fixture"); await submit(page, "#tenant-form", "/api/tenants");
    report.push(await icons(page, 1400)); report.push(await icons(page, 390));
    await page.click('[data-page="specs"]'); await modes(page); report.push("all source modes and dialog width verified on 390px mobile");
    await page.setViewport({ width: 1400, height: 1000 }); await modes(page);
    report.push(await fileFlow(page, input.root)); report.push(await urlFlow(page)); report.push(await ucpMode(page)); report.push(await ucpMCPMode(page)); await specIcons(page); report.push(await tenantIdentityAndAudit(page, input.password)); report.push(await deletionFlow(page, input.root)); report.push(await lifecycleFlow(page, input, { idle, submit, close, dialogSizes })); report.push(await notifications(page));
    assert.deepEqual(faults, []); assert.deepEqual(external, []);
    report.push("file/URL refresh and source dialogs fit both 390px and 1400px viewports");
    report.push("zero uncaught browser exceptions; zero external browser requests; real cookies/CSRF requests used");
    console.log(JSON.stringify({ browser: await browser.version(), checks: report }, null, 2));
  } finally { await browser.close(); }
}
(async () => { let input = ""; for await (const chunk of process.stdin) input += chunk; await main(JSON.parse(input)); })().catch((error) => { console.error(error.stack); process.exitCode = 1; });
