"""Static contract checks for tenant-scoped, content-free analytics presentation."""

from __future__ import annotations

import re
from pathlib import Path

from gryphon.saas_analytics_report import methodology, report_metrics
from gryphon.saas_analytics_schema import empty_bucket

_ROOT = Path(__file__).resolve().parents[2] / "src" / "gryphon"
_SCRIPT = _ROOT / "static" / "analytics.js"
_ADMIN = _ROOT / "static" / "admin.js"
_TEMPLATE = _ROOT / "templates" / "admin.html"


def test_analytics_ui_report_fields_match_server_schema() -> None:
    """Presentation reads real numeric report fields, not guessed lifetime usage data."""
    script = _SCRIPT.read_text()
    fields = set(re.findall(r"summary\.([a-z0-9_]+)", script))
    assert fields <= report_metrics(empty_bucket()).keys()
    assert "result.summary" in script and "result.daily" in script and "result.channels" in script
    assert "result.window" in script and "result.methodology" in script


def test_analytics_ui_safe_scoped_get_and_race_guards() -> None:
    """Reports use the shared cookie helper and abort locally without stale UI writes."""
    script = _SCRIPT.read_text()
    admin = _ADMIN.read_text()
    assert 'api(`/api/tenants/${segment(scope)}/analytics?${query}`, "GET", undefined, request.signal)' in script
    assert 'new URLSearchParams({ days: $("analytics-days").value })' in script
    assert 'query.set("channel_id", $("analytics-channel").value)' in script
    assert "fetch(" not in script
    assert "new AbortController()" in script and "controller?.abort()" in script
    assert "current !== selection" in script and "epoch !== state.epoch" in script
    assert "scope !== state.tenant" in script and "tenant !== state.tenant" in script
    assert "signal?.aborted || epoch !== state.epoch" in admin
    assert "AbortSignal.any([signal, AbortSignal.timeout(30000)])" in admin
    assert 'credentials: "same-origin"' in admin and 'cache: "no-store"' in admin


def test_analytics_ui_clears_old_reports_on_every_scope_transition() -> None:
    """Refresh, logout, tenant selection, and failed loads remove data and disable export."""
    script = _SCRIPT.read_text()
    admin = _ADMIN.read_text()
    assert '$("analytics-report").hidden = true' in script
    assert '$("analytics-download").disabled = true' in script
    assert "report = null" in script and "containers.forEach((id) => $(id).replaceChildren())" in script
    assert "function signedOut() {\n  analytics.reset();" in admin
    assert 'async function refresh() {\n  analytics.clear("Refreshing analytics…")' in admin
    assert "async function loadTenant() {\n  analytics.selectTenant(state.tenant)" in admin
    assert 'bind("logout", async () => { analytics.reset();' in admin
    assert 'window.addEventListener("pagehide", reset)' in script
    assert "select.disabled = state.busy || !isAdmin() || !state.tenants.length" in admin
    assert '"change", load' in script
    assert "current === selection && epoch === state.epoch && !request.signal.aborted" in script


def test_analytics_ui_ratios_use_actual_execution_and_paired_denominators() -> None:
    """Polling never substitutes for terminal runs and missing comparison data is not savings."""
    script = _SCRIPT.read_text()
    assert "rate(summary.replay_backend_starts, summary.backend_starts)" in script
    assert "rate(summary.successful_runs, summary.terminal_runs)" in script
    assert "percent(summary.payload_reduction_percent)" in script
    assert "const paired = summary.comparable_runs > 0" in script
    assert "paired ? summary.comparison_upstream_bytes : null" in script
    assert "paired ? summary.comparison_result_bytes : null" in script
    assert "total > 0" in script and "— N/A" in script
    assert "value.toFixed(1)" in script
    assert "Math.max(0," not in script and '"100%"' not in script
    for field in ("successful_runs", "failed_runs", "cancelled_runs", "incomplete_requests"):
        assert field in script


def test_analytics_ui_methodology_and_model_billing_limits_explicit() -> None:
    """Byte estimates cannot be confused with measured model tokens or generation avoided."""
    template = _TEMPLATE.read_text()
    script = _SCRIPT.read_text()
    for text in ("OBSERVED BYTES", "ESTIMATED", "Not observable", "not a model tokenizer", "billing"):
        assert text in template
    assert "including artifact data" in methodology()["payload_basis"]
    for key in (
        "estimator",
        "estimator_rounding",
        "token_estimate",
        "comparison",
        "payload_basis",
        "traffic_exclusions",
        "completeness",
        "history",
        "limitations",
    ):
        assert f'"{key}"' in script and key in methodology()
    assert "answer correctness" in methodology()["limitations"]
    assert "not actual generation avoided" in script
    assert "window.recording_since" in script and "window.retention_days" in script
    assert "no historic backfill" in script
    assert "not CPU time or time saved" in template


def test_analytics_ui_export_is_local_reproducible_and_releases_blob() -> None:
    """Download retains the entire server report; no publishing, credentials, or HTML sinks."""
    script = _SCRIPT.read_text()
    assert 'new Blob([JSON.stringify(report, null, 2)], { type: "application/json" })' in script
    assert 'link.download = "gryphon-analytics.json"' in script
    assert "finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }" in script
    assert "if (!report || !state.me || tenant !== state.tenant) return" in script
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "localStorage",
        "sessionStorage",
        "sendBeacon",
        "Authorization",
        "eval(",
    ):
        assert forbidden not in script
    assert "textContent" in script and 'node("td", value)' in script


def test_analytics_ui_both_roles_and_narrow_filters() -> None:
    """Analytics stays available inside selected tenant content without changing Users gates."""
    template = _TEMPLATE.read_text()
    admin = _ADMIN.read_text()
    assert '<a href="#analytics" data-page="analytics">Analytics</a>' in template
    assert (
        template.index('id="tenant-content"')
        < template.index('id="page-analytics"')
        < template.index('id="page-audit"')
    )
    days = template.split('id="analytics-days"')[1].split("</select>")[0]
    assert re.findall(r'value="([0-9]+)"', days) == ["7", "30", "90"]
    assert '<option value="">All channels</option>' in template
    assert 'if (key === "users" && !isAdmin())' in admin
    assert 'analytics: ["Analytics"' in admin
    assert "isAdmin" not in _SCRIPT.read_text()


def test_analytics_ui_latency_and_workload_labels_preserve_measurement_limits() -> None:
    """Attempts, broad errors and histogram estimates cannot imply stronger measurements."""
    script = _SCRIPT.read_text()
    for label in (
        "Broker attempts / accepted responses",
        "Failed replay requests",
        "Cache errors / policy or idempotency conflicts · all requests",
        "Backend wall time · average",
        "Queue wait · average",
        "Response-production latency · average",
        "Run p50 / p95 · histogram upper bounds",
        "Response-production p50 / p95 · histogram upper bounds",
        "Program source bytes / static lines observed",
        "Accepted upstream list items / final list items",
        "Structured payload tokens · ESTIMATED",
        "Request / response wire equivalents · ESTIMATED tokens",
    ):
        assert label in script
    assert "Static source lines are not executed instruction counts" in script
    assert "Items count only top-level JSON arrays" in script
    assert "Broker attempts can be rejected before HTTP dispatch" in script
    assert "Cache misses / policy or catalog drift" not in script


def test_analytics_ui_all_static_container_references_resolve() -> None:
    """Array-based cleanup targets and dynamically populated panels all exist in shipped HTML."""
    identifiers = set(re.findall(r'id="([a-z-]+)"', _TEMPLATE.read_text()))
    references = set(re.findall(r'"(analytics-[a-z-]+)"', _SCRIPT.read_text()))
    assert references <= identifiers
    assert len(references) >= 15
