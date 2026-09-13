"""Content-free measurement deltas and truthful bounded daily analytics reports."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from gryphon.saas_analytics_schema import (
    LATENCY_BOUNDS,
    MAX_RECEIPTS,
    RETENTION_DAYS,
    Bucket,
    decode_bucket,
    empty_bucket,
    increment_dimension,
    merge_bucket,
    observe_latency,
    validate_bucket,
)

if TYPE_CHECKING:
    from gryphon.models.analytics import RunMetrics
    from gryphon.models.traffic import RequestMetrics
    from gryphon.saas_database import SQLRow


def estimated_tokens(size: int) -> int:
    """Return a byte-based heuristic, never an actual tokenizer or billing measurement."""
    return (size + 3) // 4


def request_delta(metrics: RequestMetrics) -> Bucket:
    """Count one observed MCP request independently of admitted or terminal runs."""
    bucket = empty_bucket()
    bucket.update(
        requests=1,
        request_successes=int(metrics.success),
        request_failures=int(not metrics.success),
        incomplete_requests=int(not metrics.observation_complete),
        request_wire_bytes=metrics.request_bytes,
        response_wire_bytes=metrics.response_bytes,
        structured_payload_bytes=metrics.payload_bytes,
        request_duration_ms=metrics.duration_ms,
        request_estimated_tokens=estimated_tokens(metrics.request_bytes),
        response_estimated_tokens=estimated_tokens(metrics.response_bytes),
        payload_estimated_tokens=estimated_tokens(metrics.payload_bytes),
    )
    increment_dimension(bucket, "requests_by_tool", metrics.tool)
    increment_dimension(
        bucket, "request_successes_by_tool" if metrics.success else "request_failures_by_tool", metrics.tool
    )
    if metrics.error_type is not None:
        increment_dimension(bucket, "request_errors", metrics.error_type)
    observe_latency(bucket, "request_latency_bins", metrics.duration_ms)
    return bucket


def run_delta(metrics: RunMetrics) -> Bucket:
    """Count actual execution, full final JSON, and explicitly eligible paired samples."""
    bucket = empty_bucket()
    succeeded = metrics.status == "succeeded"
    replay_started = metrics.origin == "replay" and metrics.backend_started
    bucket.update(
        terminal_runs=1,
        completed_runs=int(metrics.status != "cancelled"),
        successful_runs=int(succeeded),
        failed_runs=int(metrics.status == "failed"),
        cancelled_runs=int(metrics.status == "cancelled"),
        backend_starts=int(metrics.backend_started),
        replay_backend_starts=int(replay_started),
        replay_successes=int(replay_started and succeeded),
        reused_source_bytes=metrics.source_bytes if replay_started else 0,
        result_item_observations=int(metrics.result_items is not None),
        result_items=metrics.result_items or 0,
        pure_compute_runs=int(metrics.backend_started and metrics.api_calls == 0),
        multi_call_runs=int(metrics.api_responses > 1),
        artifacts_created=int(metrics.artifact_created),
        run_duration_ms=metrics.duration_ms,
    )
    _run_details(bucket, metrics)
    observe_latency(bucket, "run_latency_bins", metrics.duration_ms)
    return bucket


def _run_details(bucket: Bucket, metrics: RunMetrics) -> None:
    """Add scalar totals and paired-only comparison populations to a run delta."""
    for key in (
        "source_bytes",
        "source_lines",
        "input_bytes",
        "result_bytes",
        "upstream_bytes",
        "upstream_items",
        "api_calls",
        "api_responses",
        "queue_ms",
        "execution_ms",
        "broker_ms",
    ):
        value = getattr(metrics, key)
        bucket[key] = 0 if value is None else value
    for key, dimension in (
        ("run_statuses", metrics.status),
        ("run_origins", metrics.origin),
        ("sandbox_modes", metrics.sandbox_mode),
    ):
        increment_dimension(bucket, key, dimension)
    if metrics.error_type is not None:
        increment_dimension(bucket, "run_errors", metrics.error_type)
    if (
        metrics.status == "succeeded"
        and metrics.comparison_eligible
        and metrics.api_responses > 0
        and metrics.result_bytes is not None
    ):
        bucket.update(
            comparable_runs=1,
            comparison_upstream_bytes=metrics.upstream_bytes,
            comparison_result_bytes=metrics.result_bytes,
            comparison_upstream_estimated_tokens=estimated_tokens(metrics.upstream_bytes),
            comparison_result_estimated_tokens=estimated_tokens(metrics.result_bytes),
        )


def _percentile(bins: dict[str, int], percentile: float) -> int | None:
    """Return a histogram upper bound; empty populations have no percentile."""
    total, cumulative = sum(bins.values()), 0
    if not total:
        return None
    for bound in LATENCY_BOUNDS:
        cumulative += bins.get(str(bound), 0)
        if cumulative >= total * percentile:
            return bound
    return None


def report_metrics(bucket: Bucket) -> dict[str, Any]:
    """Derive weighted signed reductions only after summing paired byte populations."""
    validate_bucket(bucket)
    result: dict[str, Any] = dict(bucket)
    upstream = cast("int", bucket["comparison_upstream_bytes"])
    final = cast("int", bucket["comparison_result_bytes"])
    result["payload_reduction_bytes"] = upstream - final
    result["payload_reduction_percent"] = 100 * (upstream - final) / upstream if upstream else None
    result["estimated_token_reduction"] = cast("int", bucket["comparison_upstream_estimated_tokens"]) - cast(
        "int", bucket["comparison_result_estimated_tokens"]
    )
    for prefix in ("request", "run"):
        bins = cast("dict[str, int]", bucket[f"{prefix}_latency_bins"])
        result[f"{prefix}_latency_p50_upper_bound_ms"] = _percentile(bins, 0.50)
        result[f"{prefix}_latency_p95_upper_bound_ms"] = _percentile(bins, 0.95)
    return result


def methodology() -> dict[str, Any]:
    """Expose measurement limitations together with every report and download."""
    return {
        "actual_model_tokens": None,
        "actual_model_cost": None,
        "estimator": "ceil(utf8_bytes / 4)",
        "estimator_rounding": "Per observed request field and per comparable run, then summed.",
        "comparison": "Paired successful eligible runs with accepted API responses and known full final JSON bytes.",
        "payload_basis": "Accepted upstream canonical JSON versus full final canonical JSON, including artifact data.",
        "reduction": "Signed weighted byte reduction; negative means expansion. Zero upstream yields null percent.",
        "traffic": (
            "Allowlisted tools/call only, including get_run/read_artifact; wire and payload bytes differ. "
            "Response bytes measure SDK-produced bodies, not proven client reception or model consumption."
        ),
        "traffic_exclusions": "Excludes initialize, tools/list, HTTP headers, and actual model context.",
        "completeness": "Best-effort excludes dropped/crash observations; not a complete billing or audit ledger.",
        "token_estimate": "Byte heuristic only; no actual tokenizer, model-token equivalence, or billing measurement.",
        "durations": (
            "Monotonic elapsed milliseconds, not CPU time; concurrent run durations may overlap. "
            "Request timing measures response production, excluding its own observation persistence. "
            "Final response handoff waits for bounded persistence attempts, which may fail safely."
        ),
        "latency": "Fixed upper-inclusive histogram bins; p50/p95 values are upper bounds, not exact percentiles.",
        "latency_bin_upper_bounds_ms": list(LATENCY_BOUNDS),
        "dedup_retention_days": RETENTION_DAYS,
        "dedup_receipt_cap_per_tenant": MAX_RECEIPTS,
        "deduplication": "Per-tenant capped event digests deduplicate only while retained; not exactly-once effects.",
        "dedup_storage_bound": "Per-tenant receipt cap multiplied by the configured hosted tenant quota.",
        "history": "Observed data only; no backfill from lifetime usage and no inference from requests minus runs.",
        "limitations": (
            "No measured LLM cost savings, reasoning tokens, CPU savings, or avoided tool round trips. "
            "Execution success and payload reduction do not measure answer correctness or equivalent task quality."
        ),
        "completed_runs": "Succeeded plus failed terminal runs; cancellations are counted separately.",
    }


def build_report(
    rows: list[SQLRow],
    channels: dict[str, str],
    start: date,
    end: date,
    recording_since: float | None,
) -> dict[str, Any]:
    """Aggregate at most 9,000 persisted rows into at most 90 dates and 100 channels."""
    summary = empty_bucket()
    daily = {(start + timedelta(days=offset)).isoformat(): empty_bucket() for offset in range((end - start).days)}
    per_channel = {channel_id: empty_bucket() for channel_id in channels}
    for row in rows:
        bucket = decode_bucket(str(row["payload"]))
        merge_bucket(summary, bucket)
        merge_bucket(daily[str(row["day"])], bucket)
        merge_bucket(per_channel[str(row["channel_id"])], bucket)
    return {
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days,
            "timezone": "UTC",
            "end_exclusive": True,
            "retention_days": RETENTION_DAYS,
            "recording_since": datetime.fromtimestamp(recording_since, UTC).isoformat()
            if recording_since is not None
            else None,
        },
        "summary": report_metrics(summary),
        "daily": [{"date": day, **report_metrics(bucket)} for day, bucket in daily.items()],
        "channels": [
            {"channel_id": channel_id, "name": channels[channel_id], **report_metrics(bucket)}
            for channel_id, bucket in per_channel.items()
        ],
        "methodology": methodology(),
    }
