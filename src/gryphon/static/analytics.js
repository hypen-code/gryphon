"use strict";
const analytics = (() => {
  let tenant = "", selection = 0, controller = null, report = null;
  const containers = ["analytics-kpis", "analytics-bytes", "analytics-tokens", "analytics-reuse", "analytics-workload", "analytics-daily-chart", "analytics-daily-rows", "analytics-channel-rows", "analytics-traffic", "analytics-methodology"];
  function clear(message = "Analytics unavailable. Use Refresh data to retry.") {
    selection += 1; controller?.abort(); controller = null; report = null;
    $("analytics-report").hidden = true; $("analytics-download").disabled = true;
    $("analytics-status").textContent = message; $("analytics-status").hidden = false;
    $("page-analytics").removeAttribute("aria-busy");
    containers.forEach((id) => $(id).replaceChildren());
    $("analytics-window").textContent = ""; $("analytics-recording").textContent = "";
  }
  function reset() {
    clear("Select a tenant to view recorded analytics."); tenant = "";
    $("analytics-days").value = "7"; $("analytics-channel").replaceChildren(new Option("All channels", ""));
  }
  function selectTenant(id) {
    if (tenant !== id) reset(); else clear("Loading analytics…");
    tenant = id;
  }
  function channels(items) {
    const selected = $("analytics-channel").value;
    $("analytics-channel").replaceChildren(new Option("All channels", ""));
    items.forEach((channel) => $("analytics-channel").append(new Option(channel.name, channel.id)));
    $("analytics-channel").value = items.some((channel) => channel.id === selected) ? selected : "";
  }
  async function load() {
    clear(tenant ? "Loading analytics…" : "Select a tenant to view recorded analytics.");
    if (!tenant || tenant !== state.tenant || !state.me) return;
    const current = selection, epoch = state.epoch, scope = tenant;
    const request = new AbortController(); controller = request;
    const query = new URLSearchParams({ days: $("analytics-days").value });
    if ($("analytics-channel").value) query.set("channel_id", $("analytics-channel").value);
    $("page-analytics").setAttribute("aria-busy", "true");
    try {
      const result = await api(`/api/tenants/${segment(scope)}/analytics?${query}`, "GET", undefined, request.signal);
      if (request.signal.aborted || current !== selection || epoch !== state.epoch || scope !== state.tenant) return;
      render(result); report = result; $("analytics-download").disabled = false;
    } catch {
      if (current === selection && epoch === state.epoch && !request.signal.aborted) clear();
    } finally {
      if (current === selection) { controller = null; $("page-analytics").removeAttribute("aria-busy"); }
    }
  }
  const numeric = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value) => numeric(value) ? count(value) : "— N/A";
  const bytes = (value) => numeric(value) ? `${count(value)} B` : "— N/A";
  const percent = (value) => numeric(value) ? `${value.toFixed(1)}%` : "— N/A";
  const rate = (part, total) => numeric(part) && numeric(total) && total > 0 ? percent(part / total * 100) : "— N/A";
  const milliseconds = (value) => numeric(value) ? `${count(Math.round(value))} ms` : "— N/A";
  const dimension = (values, key) => values && typeof values === "object" ? (values[key] ?? 0) : null;
  const breakdown = (values) => values && typeof values === "object" ? Object.entries(values).map(([key, value]) => `${key}: ${number(value)}`).join(" · ") || "None recorded" : "— N/A";
  function metric(label, value, explanation) {
    const card = node("article", "", "metric");
    card.append(node("p", label), node("strong", value), node("small", explanation)); $("analytics-kpis").append(card);
  }
  function facts(id, rows) {
    rows.forEach(([label, value]) => { const row = node("div"); row.append(node("dt", label), node("dd", value)); $(id).append(row); });
  }
  function bars(id, rows, format) {
    const max = Math.max(1, ...rows.map(([, value]) => numeric(value) ? value : 0));
    rows.forEach(([label, value]) => {
      const row = node("div", "", "chart-item"), heading = node("div", "", "chart-label");
      heading.append(node("span", label), node("strong", format(value))); row.append(heading);
      if (numeric(value)) { const meter = node("meter"); meter.min = 0; meter.max = max; meter.value = value; meter.setAttribute("aria-label", `${label}: ${format(value)}`); row.append(meter); }
      $(id).append(row);
    });
  }
  function render(result) {
    if (!result.window || !result.methodology || !Array.isArray(result.daily) || !Array.isArray(result.channels) || !numeric(result.summary?.requests) || !numeric(result.summary?.terminal_runs)) throw new Error("Invalid analytics report");
    const summary = result.summary, window = result.window;
    metric("Recorded MCP calls", number(summary.requests), "Observed requests, including metadata, polls, and artifact reads");
    metric("Full API payload reduction", percent(summary.payload_reduction_percent), `${number(summary.comparable_runs)} successful paired API runs · signed byte reduction`);
    metric("Actual code-reuse rate", rate(summary.replay_backend_starts, summary.backend_starts), "Replay backend starts / all backend starts; not replay attempts");
    metric("Run success rate", rate(summary.successful_runs, summary.terminal_runs), "Succeeded / all terminal runs, including failed and cancelled");
    const paired = summary.comparable_runs > 0;
    bars("analytics-bytes", [["Accepted API JSON", paired ? summary.comparison_upstream_bytes : null], ["Full final JSON", paired ? summary.comparison_result_bytes : null]], bytes);
    bars("analytics-tokens", [["API payload · ESTIMATED", paired ? summary.comparison_upstream_estimated_tokens : null], ["Final output · ESTIMATED", paired ? summary.comparison_result_estimated_tokens : null]], number);
    renderReuse(summary); renderWorkload(summary); renderTraffic(summary); renderDaily(result.daily); renderChannels(result.channels); renderMethodology(result.methodology);
    $("analytics-window").textContent = `${window.start} to ${window.end} (end exclusive) · ${window.days} days · UTC · ${$("analytics-channel").value ? "Selected channel" : "All channels"}`;
    $("analytics-recording").textContent = `Recording since: ${window.recording_since || "Not recorded yet"}. Retention: ${window.retention_days} days. Observed data only; no historic backfill.`;
    const empty = summary.requests === 0 && summary.terminal_runs === 0;
    $("analytics-status").textContent = empty ? "No observations recorded for this window and channel selection. Ratios with no denominator are N/A, not savings." : "";
    $("analytics-status").hidden = !empty; $("analytics-report").hidden = false;
  }
  function renderReuse(summary) {
    const reused = summary.reused_source_bytes;
    facts("analytics-reuse", [
      ["Fresh backend executions", number(summary.backend_starts - summary.replay_backend_starts)],
      ["Replay backend executions", number(summary.replay_backend_starts)],
      ["Successful replays", number(summary.replay_successes)],
      ["Replay requests (not executions)", number(dimension(summary.requests_by_tool, "run_cached_code"))],
      ["Failed replay requests", number(dimension(summary.request_failures_by_tool, "run_cached_code"))],
      ["Cache errors / policy or idempotency conflicts · all requests", `${number(dimension(summary.request_errors, "cache"))} / ${number(dimension(summary.request_errors, "conflict"))}`],
      ["Reused source bytes", bytes(reused)],
      ["Reused source token equivalent · ESTIMATED", numeric(reused) ? number(Math.ceil(reused / 4)) : "— N/A"],
    ]);
    $("analytics-reuse").append(node("p", "Reused-source estimate is ceil(total UTF-8 source bytes / 4), not actual generation avoided. Replay executes code again; it is not a cached API response.", "hint"));
  }
  function renderWorkload(summary) {
    facts("analytics-workload", [
      ["Terminal runs / async terminal runs", `${number(summary.terminal_runs)} / ${number(dimension(summary.run_origins, "submit"))}`],
      ["Succeeded / failed / cancelled", `${number(summary.successful_runs)} / ${number(summary.failed_runs)} / ${number(summary.cancelled_runs)}`],
      ["Run errors by category", breakdown(summary.run_errors)],
      ["Pure compute runs", number(summary.pure_compute_runs)],
      ["Multiple API-response runs (fan-out)", number(summary.multi_call_runs)],
      ["Broker attempts / accepted responses", `${number(summary.api_calls)} / ${number(summary.api_responses)}`],
      ["Artifacts created", number(summary.artifacts_created)],
      ["All-run upstream / final JSON", `${bytes(summary.upstream_bytes)} / ${bytes(summary.result_bytes)}`],
      ["Backend execution wall time · total", milliseconds(summary.execution_ms)],
      ["Queue wait · total", milliseconds(summary.queue_ms)],
      ["API broker wait · total", milliseconds(summary.broker_ms)],
      ["Run wall time · total", milliseconds(summary.run_duration_ms)],
      ["Backend wall time · average", summary.backend_starts ? milliseconds(summary.execution_ms / summary.backend_starts) : "— N/A"],
      ["Queue wait · average", summary.terminal_runs ? milliseconds(summary.queue_ms / summary.terminal_runs) : "— N/A"],
      ["Run p50 / p95 · histogram upper bounds", `${milliseconds(summary.run_latency_p50_upper_bound_ms)} / ${milliseconds(summary.run_latency_p95_upper_bound_ms)}`],
      ["Program source bytes / static lines observed", `${bytes(summary.source_bytes)} / ${number(summary.source_lines)}`],
      ["Validated run input JSON bytes", bytes(summary.input_bytes)],
      ["Accepted upstream list items / final list items", `${number(summary.upstream_items)} / ${number(summary.result_items)}`],
    ]);
    $("analytics-workload").append(node("p", "Items count only top-level JSON arrays, not semantic records. Static source lines are not executed instruction counts. Broker attempts can be rejected before HTTP dispatch; durations are overlapping wall time, not CPU or time saved.", "hint"));
  }
  function renderTraffic(summary) {
    const tools = summary.requests_by_tool;
    const metadata = ["list_servers", "search_functions", "get_functions", "list_recipes", "list_skills", "get_server_skills"].reduce((total, tool) => total + dimension(tools, tool), 0);
    facts("analytics-traffic", [
      ["Metadata / discovery requests", number(metadata)],
      ["Execute / submit / replay requests", `${number(dimension(tools, "execute_code"))} / ${number(dimension(tools, "submit_code"))} / ${number(dimension(tools, "run_cached_code"))}`],
      ["get_run polls / cancel requests", `${number(dimension(tools, "get_run"))} / ${number(dimension(tools, "cancel_run"))}`],
      ["Artifact read requests", number(dimension(tools, "read_artifact"))],
      ["Successful / failed requests", `${number(summary.request_successes)} / ${number(summary.request_failures)}`],
      ["Request errors by category", breakdown(summary.request_errors)],
      ["Incomplete traffic observations", number(summary.incomplete_requests)],
      ["Request / response wire bytes", `${bytes(summary.request_wire_bytes)} / ${bytes(summary.response_wire_bytes)}`],
      ["Structured response payload bytes", bytes(summary.structured_payload_bytes)],
      ["Structured payload tokens · ESTIMATED", number(summary.payload_estimated_tokens)],
      ["Request / response wire equivalents · ESTIMATED tokens", `${number(summary.request_estimated_tokens)} / ${number(summary.response_estimated_tokens)}`],
      ["Response-production latency · average", summary.requests ? milliseconds(summary.request_duration_ms / summary.requests) : "— N/A"],
      ["Response-production p50 / p95 · histogram upper bounds", `${milliseconds(summary.request_latency_p50_upper_bound_ms)} / ${milliseconds(summary.request_latency_p95_upper_bound_ms)}`],
    ]);
  }
  function renderDaily(items) {
    items.forEach((item) => {
      const row = node("tr");
      [item.date, number(item.requests), number(item.terminal_runs), number(item.successful_runs), number(item.failed_runs), number(item.cancelled_runs), percent(item.payload_reduction_percent)].forEach((value) => row.append(node("td", value)));
      $("analytics-daily-rows").append(row);
    });
    dailyChart(items);
  }
  function svgNode(tag, attrs) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value))); return element;
  }
  function dailyChart(items) {
    if (!items.length) return;
    const width = 720, height = 110, step = width / items.length;
    const max = Math.max(1, ...items.map((item) => item.requests));
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Daily UTC MCP call counts. Exact values and all run outcomes are in the following table." });
    items.forEach((item, index) => {
      const barHeight = item.requests / max * 80;
      const rect = svgNode("rect", { x: index * step, y: 85 - barHeight, width: Math.max(1, step - 2), height: barHeight });
      const title = svgNode("title", {}); title.textContent = `${item.date}: ${number(item.requests)} MCP calls`; rect.append(title); svg.append(rect);
    });
    [[items[0].date, 0, "start"], [items[items.length - 1].date, width, "end"]].forEach(([date, x, anchor]) => {
      const text = svgNode("text", { x, y: 105, "text-anchor": anchor }); text.textContent = date; svg.append(text);
    });
    $("analytics-daily-chart").append(svg);
  }
  function renderChannels(items) {
    items.forEach((item) => {
      const row = node("tr");
      [item.name, number(item.requests), number(item.terminal_runs), rate(item.successful_runs, item.terminal_runs), rate(item.replay_backend_starts, item.backend_starts), percent(item.payload_reduction_percent)].forEach((value) => row.append(node("td", value)));
      $("analytics-channel-rows").append(row);
    });
    if (!items.length) emptyRow("analytics-channel-rows", 6, "No channels in this reporting scope.");
  }
  function renderMethodology(methodology) {
    ["estimator", "estimator_rounding", "token_estimate", "comparison", "payload_basis", "reduction", "traffic", "traffic_exclusions", "completeness", "durations", "latency", "deduplication", "history", "limitations", "completed_runs"].forEach((key) => {
      if (typeof methodology[key] === "string") $("analytics-methodology").append(node("li", methodology[key]));
    });
  }
  function download() {
    if (!report || !state.me || tenant !== state.tenant) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "gryphon-analytics.json";
    try { document.body.append(link); link.click(); }
    finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }
  function wire() {
    ["analytics-days", "analytics-channel"].forEach((id) => $(id).addEventListener("change", load));
    $("analytics-download").addEventListener("click", download);
    window.addEventListener("pagehide", reset);
  }
  return { clear, reset, selectTenant, channels, load, wire };
})();
