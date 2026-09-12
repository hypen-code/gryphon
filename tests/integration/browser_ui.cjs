"use strict";
const assert = require("node:assert/strict");
const { readFile, stat } = require("node:fs/promises");
const { join } = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const puppeteer = require("puppeteer");

async function idle(page) { await page.waitForFunction(() => !state.busy); }
async function submit(page, form, suffix, status = 201) {
  const received = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith(suffix));
  await page.click(`${form} button[type=submit]`); const response = await received;
  assert.equal(response.status(), status, `Unexpected status for ${suffix}`); await idle(page);
  return response.json();
}
async function rowButton(page, id, index) {
  const handle = await page.evaluateHandle((id, index) => [...document.querySelectorAll("#spec-rows tr")].find((row) => row.children[1].firstChild.textContent === id).querySelectorAll("button")[index], id, index);
  return handle.asElement();
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
  const created = await submit(page, "#channel-form", "/channels");
  await page.click('[data-page="specs"]'); return created;
}
async function refreshFile(page, root, spec, version, update) {
  await (await rowButton(page, spec, 1)).click(); await dialogSizes(page, "#spec-refresh-dialog");
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
  assert.match(await page.$eval("#spec-rows", (rows) => rows.textContent), /1 available · 1 filtered/);
  const bound = await channel(page, original.id, "File channel");
  const second = await refreshFile(page, root, original.id, 2, true); await binding(page, bound.id, second.id);
  const third = await refreshFile(page, root, second.id, 3, false); await binding(page, bound.id, second.id);
  assert.equal(third.parent_id, second.id);
  const old = await rowButton(page, original.id, 1); assert.equal(await old.evaluate((button) => button.disabled), true);
  assert.equal(await old.evaluate((button) => button.textContent), "Superseded snapshot");
  await (await rowButton(page, original.id, 0)).click(); await page.waitForSelector("#source-dialog[open]");
  await dialogSizes(page, "#source-dialog");
  const source = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.equal(source.paths["/data"].get.operationId, "read_v1");
  const cdp = await page.createCDPSession(); await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: root });
  await page.click("#download-source");
  for (let tries = 0; tries < 50; tries++) { if (await stat(join(root, "BrowserFile.json")).catch(() => null)) break; await delay(50); }
  assert.deepEqual(JSON.parse(await readFile(join(root, "BrowserFile.json"), "utf8")), source);
  await close(page, "#source-dialog"); return "file upload; refresh true updates binding, false preserves binding; superseded update disabled; old compiled JSON view/download retained";
}
async function urlFlow(page) {
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserURL"); await page.select("#spec-kind", "openapi");
  await page.type("#spec-url", "https://example.com/openapi.json"); const original = await submit(page, "#spec-form", "/specs");
  assert.equal(original.source_type, "openapi_url"); const bound = await channel(page, original.id, "URL channel");
  await (await rowButton(page, original.id, 1)).click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await page.$eval("#spec-replacement", (input) => input.disabled && !input.required), true);
  const updated = await submit(page, "#spec-refresh-form", "/refresh"); await binding(page, bound.id, updated.id);
  assert.equal(updated.source_url, original.source_url); assert.equal(updated.parent_id, original.id);
  await (await rowButton(page, updated.id, 1)).click(); const same = await submit(page, "#spec-refresh-form", "/refresh", 200);
  assert.equal(same.id, updated.id); assert.match(await page.$eval("#notice", (notice) => notice.textContent), /unchanged/);
  return "OpenAPI URL import/refetch uses synthetic pinned HTTP; bound channel advances; unchanged refresh reports HTTP 200";
}
async function ucpMode(page) {
  await page.click("#new-spec"); await page.type("#spec-name", "BrowserUCP"); await page.select("#spec-kind", "ucp");
  await page.type("#spec-url", "https://example.com"); const original = await submit(page, "#spec-form", "/specs");
  assert.equal(original.source_type, "ucp_url"); assert.equal(original.source_url, "https://example.com/.well-known/ucp");
  assert.equal(original.diagnostics.available_operations, 1);
  assert.ok(original.warnings.some((warning) => warning.includes("Non-GET")));
  assert.ok(original.warnings.some((warning) => warning.includes("UCP-Agent")));
  assert.match(await page.$eval("#spec-rows", (rows) => rows.textContent), /UCP-Agent/);
  const bound = await channel(page, original.id, "UCP channel");
  await (await rowButton(page, original.id, 0)).click(); await page.waitForSelector("#source-dialog[open]");
  const oldDocument = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.deepEqual(Object.keys(oldDocument.paths), ["/checkout-sessions/{id}"]);
  await close(page, "#source-dialog");
  await (await rowButton(page, original.id, 1)).click(); await dialogSizes(page, "#spec-refresh-dialog");
  assert.equal(await page.$eval("#spec-replacement", (input) => input.disabled), true);
  const updated = await submit(page, "#spec-refresh-form", "/refresh"); await binding(page, bound.id, updated.id);
  assert.equal(updated.source_type, "ucp_url"); assert.equal(updated.source_url, original.source_url);
  assert.equal(updated.parent_id, original.id); assert.equal(updated.diagnostics.available_operations, 3);
  assert.equal(await page.evaluate((id) => state.channels.find((channel) => channel.id === id).revision, bound.id), bound.revision + 1);
  await (await rowButton(page, updated.id, 0)).click(); await page.waitForSelector("#source-dialog[open]");
  const compiled = JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent));
  assert.deepEqual(Object.values(compiled.paths).map((path) => path.get.operationId).sort(), ["get_cart", "get_checkout", "get_order"]);
  await close(page, "#source-dialog");
  await (await rowButton(page, original.id, 0)).click(); await page.waitForSelector("#source-dialog[open]");
  assert.deepEqual(JSON.parse(await page.$eval("#source-content", (pre) => pre.textContent)), oldDocument);
  await close(page, "#source-dialog");
  return "UCP 2026-08-25 import/refetch succeeded: checkout GET grows to checkout/cart/order, warnings shown, bound revision advances, old compiled snapshot retained";
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
    report.push(await fileFlow(page, input.root)); report.push(await urlFlow(page)); report.push(await ucpMode(page));
    assert.deepEqual(faults, []); assert.deepEqual(external, []);
    report.push("file/URL refresh and source dialogs fit both 390px and 1400px viewports");
    report.push("zero uncaught browser exceptions; zero external browser requests; real cookies/CSRF requests used");
    console.log(JSON.stringify({ browser: await browser.version(), checks: report }, null, 2));
  } finally { await browser.close(); }
}
(async () => { let input = ""; for await (const chunk of process.stdin) input += chunk; await main(JSON.parse(input)); })().catch((error) => { console.error(error.stack); process.exitCode = 1; });
