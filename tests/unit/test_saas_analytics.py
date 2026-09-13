"""Deterministic bounded analytics, tenant isolation, and honest byte comparisons."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gryphon.errors import SaaSNotFoundError, SaaSQuotaError, SaaSStoreError, SaaSValidationError
from gryphon.models.analytics import RunMetrics
from gryphon.models.traffic import MAX_MEASUREMENT, RequestMetrics
from gryphon.saas_analytics import AnalyticsStore
from gryphon.saas_analytics_schema import MAX_BUCKET_BYTES, decode_bucket, empty_bucket, encode_bucket
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SaaSStore]:
    """Own an isolated control connection for each analytics test."""
    instance = SaaSStore(f"sqlite:///{tmp_path / 'analytics.db'}")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


def run(**changes: Any) -> RunMetrics:
    """Build representative paired measurements without execution contents."""
    values = dict(
        run_id=uuid4().hex,
        origin="execute",
        status="succeeded",
        sandbox_mode="restricted",
        duration_ms=80.0,
        queue_ms=10.0,
        execution_ms=60.0,
        broker_ms=20.0,
        source_bytes=50,
        source_lines=2,
        input_bytes=2,
        result_bytes=20,
        upstream_bytes=100,
        upstream_items=4,
        result_items=1,
        api_calls=2,
        api_responses=2,
        backend_started=True,
        comparison_eligible=True,
    )
    return RunMetrics.model_validate(values | changes)


def request(**changes: Any) -> RequestMetrics:
    """Use server-generated UUIDs, fixed tool names, and scalar byte measurements."""
    return RequestMetrics.model_validate(
        dict(
            request_id=uuid4().hex,
            tool="get_run",
            success=True,
            request_bytes=5,
            response_bytes=9,
            payload_bytes=5,
            duration_ms=10.0,
        )
        | changes
    )


def timestamp(value: str) -> float:
    """Convert explicitly zoned test times without depending on the machine timezone."""
    return datetime.fromisoformat(value).timestamp()


async def test_analytics_empty_history_never_backfills_lifetime_usage(store: SaaSStore) -> None:
    """Older lifetime calls are not fabricated into dated measurements."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    await store.record_usage(tenant.id, channel.id, "get_run", "success", 100)
    analytics = AnalyticsStore(store._db, clock=lambda: timestamp("2026-01-03T12:00:00Z"))
    report = await analytics.get_report(tenant.id)
    assert report["summary"]["requests"] == 0
    assert report["summary"]["payload_reduction_percent"] is None
    assert report["summary"]["run_latency_p95_upper_bound_ms"] is None
    assert report["window"]["recording_since"] is None
    assert report["window"]["start"] == "2025-12-28"
    assert report["window"]["end"] == "2026-01-04"
    assert len(report["daily"]) == 7
    assert report["channels"][0]["name"] == "channel"
    assert report["methodology"]["actual_model_tokens"] is None
    assert report["methodology"]["dedup_receipt_cap_per_tenant"] == 100000


async def test_analytics_paired_weighted_reduction_uses_full_artifact_bytes(store: SaaSStore) -> None:
    """Weight bytes across runs, include complete artifacts, and round estimates per run."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    await analytics.record_run(tenant.id, channel.id, run(upstream_bytes=101, result_bytes=5))
    await analytics.record_run(tenant.id, channel.id, run(upstream_bytes=9, result_bytes=45, artifact_created=True))
    await analytics.record_run(tenant.id, channel.id, run(status="failed", result_bytes=999, error_type="execution"))
    summary = (await analytics.get_report(tenant.id))["summary"]
    assert summary["payload_reduction_percent"] == pytest.approx(100 * 60 / 110)
    assert summary["comparison_result_bytes"] == 50
    assert summary["result_bytes"] == 1049
    assert summary["comparison_upstream_estimated_tokens"] == 29
    assert summary["comparison_result_estimated_tokens"] == 14
    assert summary["estimated_token_reduction"] == 15
    assert summary["comparable_runs"] == 2
    assert summary["artifacts_created"] == 1
    assert summary["run_latency_p50_upper_bound_ms"] == 100
    assert summary["run_errors"] == {"execution": 1}


@pytest.mark.parametrize(("upstream", "result", "expected"), [(5, 20, -300.0), (0, 4, None)])
async def test_analytics_expansion_and_zero_baseline_are_honest(
    store: SaaSStore,
    upstream: int,
    result: int,
    expected: float | None,
) -> None:
    """Negative reduction is preserved and division by zero is explicitly unavailable."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    await analytics.record_run(tenant.id, channel.id, run(upstream_bytes=upstream, result_bytes=result))
    summary = (await analytics.get_report(tenant.id))["summary"]
    assert summary["payload_reduction_percent"] == expected
    json.dumps(summary, allow_nan=False)


async def test_analytics_run_classification_distinguishes_replay_admission_and_compute(store: SaaSStore) -> None:
    """Count actual starts, not requests or admission attempts, as backend executions."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    await analytics.record_run(tenant.id, channel.id, run(origin="replay"))
    await analytics.record_run(
        tenant.id,
        channel.id,
        run(
            origin="replay",
            status="cancelled",
            backend_started=False,
            api_calls=0,
            api_responses=0,
            comparison_eligible=False,
        ),
    )
    await analytics.record_run(
        tenant.id, channel.id, run(origin="submit", api_calls=0, api_responses=0, result_items=None)
    )
    summary = (await analytics.get_report(tenant.id))["summary"]
    assert summary["backend_starts"] == 2
    assert summary["terminal_runs"] == 3
    assert summary["completed_runs"] == 2
    assert summary["cancelled_runs"] == 1
    assert summary["replay_backend_starts"] == summary["replay_successes"] == 1
    assert summary["reused_source_bytes"] == 50
    assert summary["pure_compute_runs"] == summary["multi_call_runs"] == 1
    assert summary["comparable_runs"] == 1
    assert summary["queue_ms"] == 30
    assert summary["execution_ms"] == 180
    assert summary["broker_ms"] == 60
    assert summary["run_duration_ms"] == 240


async def test_analytics_requests_include_polling_artifacts_and_incomplete_observations(store: SaaSStore) -> None:
    """Request traffic and structured payloads remain distinct from terminal run counts."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    await analytics.record_request(tenant.id, channel.id, request())
    await analytics.record_request(tenant.id, channel.id, request(tool="read_artifact"))
    await analytics.record_request(
        tenant.id,
        channel.id,
        request(tool="run_cached_code", success=False, error_type="cache_miss", observation_complete=False),
    )
    summary = (await analytics.get_report(tenant.id))["summary"]
    assert summary["requests"] == 3
    assert summary["terminal_runs"] == 0
    assert summary["request_wire_bytes"] == 15
    assert summary["response_wire_bytes"] == 27
    assert summary["structured_payload_bytes"] == 15
    assert summary["payload_estimated_tokens"] == 6
    assert summary["incomplete_requests"] == 1
    assert summary["request_errors"] == {"cache_miss": 1}
    assert summary["request_successes_by_tool"] == {"get_run": 1, "read_artifact": 1}
    assert summary["request_failures_by_tool"] == {"run_cached_code": 1}


async def test_analytics_dedup_is_transactional_scoped_and_survives_reopen(store: SaaSStore) -> None:
    """Concurrent repeated observers and reconnects share durable bounded deduplication."""
    tenant = await store.create_tenant("tenant")
    first = await store.create_channel(tenant.id, "first")
    second = await store.create_channel(tenant.id, "second")
    analytics = AnalyticsStore(store._db)
    event = run()
    results = await asyncio.gather(*(analytics.record_run(tenant.id, first.id, event) for _ in range(4)))
    assert results.count(True) == 1
    assert await analytics.record_run(tenant.id, second.id, event)
    assert await analytics.record_request(tenant.id, first.id, request(request_id=event.run_id))
    await store.close()
    await store.initialize()
    assert not await analytics.record_run(tenant.id, first.id, event)
    report = await analytics.get_report(tenant.id)
    assert report["summary"]["terminal_runs"] == 2
    assert report["summary"]["requests"] == 1
    async with store._db.transaction():
        rows = await store._db.execute("SELECT * FROM saas_analytics_receipts")
    assert event.run_id not in str(rows)
    assert all("event_digest" in row for row in rows)


async def test_analytics_utc_window_and_channel_filters(store: SaaSStore) -> None:
    """Bucket recording time in UTC rather than local offsets or caller event timestamps."""
    tenant = await store.create_tenant("tenant")
    first = await store.create_channel(tenant.id, "first")
    second = await store.create_channel(tenant.id, "second")
    now = timestamp("2026-01-01T23:59:59Z")
    analytics = AnalyticsStore(store._db, clock=lambda: now)
    await analytics.record_run(tenant.id, first.id, run())
    now = timestamp("2026-01-02T02:00:00+02:00")
    await analytics.record_run(tenant.id, second.id, run())
    report = await analytics.get_report(tenant.id, days=1)
    assert report["summary"]["terminal_runs"] == 1
    assert report["daily"][0]["date"] == "2026-01-02"
    scoped = await analytics.get_report(tenant.id, days=2, channel_id=first.id)
    assert scoped["summary"]["terminal_runs"] == 1
    assert len(scoped["channels"]) == 1
    assert scoped["window"]["recording_since"] == "2026-01-01T23:59:59+00:00"


async def test_analytics_foreign_scope_and_database_foreign_keys_fail_closed(store: SaaSStore) -> None:
    """Empty windows and direct SQL still cannot associate a foreign channel with a tenant."""
    tenant = await store.create_tenant("tenant")
    other = await store.create_tenant("other")
    channel = await store.create_channel(tenant.id, "private channel")
    analytics = AnalyticsStore(store._db)
    with pytest.raises(SaaSNotFoundError):
        await analytics.get_report(other.id, channel_id=channel.id)
    with pytest.raises(SaaSNotFoundError):
        await analytics.record_run(other.id, channel.id, run())
    with pytest.raises(SaaSNotFoundError):
        await analytics.get_report("absent")
    assert (await analytics.get_report(other.id))["channels"] == []
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute(
                "INSERT INTO saas_analytics_daily VALUES (?,?,?,?)",
                (other.id, channel.id, "2026-01-01", encode_bucket(empty_bucket())),
            )


async def test_analytics_retention_prunes_only_owned_rows_and_preserves_first_recording(store: SaaSStore) -> None:
    """Day 90 removes day zero receipts/aggregates but not other persistent control data."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    await store.record_usage(tenant.id, channel.id, "get_run", "success", 1)
    now = timestamp("2026-01-01T00:00:00Z")
    analytics = AnalyticsStore(store._db, clock=lambda: now)
    event = run()
    await analytics.record_run(tenant.id, channel.id, event)
    now += 89 * 86400
    assert not await analytics.record_run(tenant.id, channel.id, event)
    now += 86400
    assert await analytics.record_run(tenant.id, channel.id, event)
    report = await analytics.get_report(tenant.id, days=90)
    assert report["summary"]["terminal_runs"] == 1
    assert report["window"]["recording_since"] == "2026-01-01T00:00:00+00:00"
    async with store._db.transaction():
        assert len(await store._db.execute("SELECT * FROM saas_analytics_daily")) == 1
        assert len(await store._db.execute("SELECT * FROM saas_analytics_receipts")) == 1
    assert (await store.list_usage(tenant.id))[0].calls == 1
    assert await store.get_channel(tenant.id, channel.id) == channel


async def test_analytics_receipt_cap_evicts_only_receipts_not_aggregates(store: SaaSStore) -> None:
    """Per-tenant cap pressure ends dedup retention without removing measured daily totals."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    now = timestamp("2026-01-01T00:00:00Z")
    analytics = AnalyticsStore(store._db, clock=lambda: now)
    first = run()
    with patch("gryphon.saas_analytics.MAX_RECEIPTS", 2):
        await analytics.record_run(tenant.id, channel.id, first)
        for _ in range(2):
            now += 1
            await analytics.record_run(tenant.id, channel.id, run())
        now += 1
        assert await analytics.record_run(tenant.id, channel.id, first)
    assert (await analytics.get_report(tenant.id))["summary"]["terminal_runs"] == 4
    async with store._db.transaction():
        sql = "SELECT id FROM saas_analytics_receipts WHERE tenant_id=?"
        assert len(await store._db.execute(sql, (tenant.id,))) == 2


async def test_analytics_failure_rolls_back_receipt_and_metadata(store: SaaSStore) -> None:
    """An aggregate failure leaves an observation retryable and first-recording unset."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    event = run()
    with (
        patch.object(analytics, "_aggregate", new=AsyncMock(side_effect=SaaSStoreError("failed"))),
        pytest.raises(SaaSStoreError),
    ):
        await analytics.record_run(tenant.id, channel.id, event)
    assert (await analytics.get_report(tenant.id))["window"]["recording_since"] is None
    assert await analytics.record_run(tenant.id, channel.id, event)


async def test_analytics_counter_overflow_rejects_and_rolls_back(store: SaaSStore) -> None:
    """Aggregate addition cannot silently overflow or consume the failed event's receipt."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    await analytics.record_request(tenant.id, channel.id, request(request_bytes=MAX_MEASUREMENT))
    with pytest.raises(SaaSValidationError):
        await analytics.record_request(tenant.id, channel.id, request())
    assert (await analytics.get_report(tenant.id))["summary"]["requests"] == 1
    with pytest.raises(SaaSValidationError):
        await analytics.record_run(tenant.id, channel.id, run(source_bytes=MAX_MEASUREMENT + 1))


@pytest.mark.parametrize(
    "changes",
    [
        {"tool": "private-text"},
        {"error_type": "https://private"},
        {"duration_ms": float("nan")},
        {"response_bytes": -1},
        {"request_bytes": MAX_MEASUREMENT + 1},
        {"payload_bytes": True},
        {"request_id": "client-secret"},
        {"code": "private"},
    ],
)
def test_analytics_request_model_rejects_invalid_dimensions_and_counters(changes: dict[str, Any]) -> None:
    """No arbitrary strings, nonfinite values, booleans-as-counts, or excess fields enter persistence."""
    with pytest.raises(ValidationError):
        request(**changes)


@pytest.mark.parametrize("payload", ["null", "{}", "[]", "{", " " * (MAX_BUCKET_BYTES + 1)])
def test_analytics_persisted_payload_validation_is_strict(payload: str) -> None:
    """Malformed or oversized aggregates never become fabricated report counters."""
    with pytest.raises(SaaSValidationError):
        decode_bucket(payload)


async def test_analytics_invalid_windows_and_configured_bounds_fail(store: SaaSStore) -> None:
    """Even overridden store quotas cannot turn analytics into an unbounded report."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics = AnalyticsStore(store._db)
    for days in (0, 91, -1, True):
        with pytest.raises(SaaSValidationError):
            await analytics.get_report(tenant.id, days=days)
    with patch("gryphon.saas_analytics.MAX_CHANNELS", 0), pytest.raises(SaaSQuotaError):
        await analytics.record_run(tenant.id, channel.id, run())
    with patch("gryphon.saas_analytics.MAX_ROWS", 0), pytest.raises(SaaSQuotaError):
        await analytics.record_run(tenant.id, channel.id, run())
    with patch("gryphon.saas_analytics.MAX_CHANNELS", 0), pytest.raises(SaaSQuotaError):
        await analytics.get_report(tenant.id)


async def test_analytics_bypassed_models_are_revalidated(store: SaaSStore) -> None:
    """Copies cannot smuggle arbitrary error text or nonfinite measurements into storage."""
    analytics = AnalyticsStore(store._db)
    with pytest.raises(SaaSValidationError):
        await analytics.record_run("absent", "absent", run().model_copy(update={"duration_ms": float("nan")}))
    with pytest.raises(SaaSValidationError):
        await analytics.record_request("absent", "absent", request().model_copy(update={"error_type": "private"}))


@pytest.mark.parametrize(
    ("key", "value"), [("requests", True), ("request_errors", {"private": 1}), ("run_duration_ms", float("inf"))]
)
def test_analytics_bucket_refuses_untrusted_numbers_and_dimensions(key: str, value: Any) -> None:
    """Validation also protects persisted JSON against altered categorical and scalar values."""
    bucket = empty_bucket()
    bucket[key] = value
    with pytest.raises(SaaSValidationError):
        encode_bucket(bucket)
    with patch("gryphon.saas_analytics_schema.MAX_BUCKET_BYTES", 1), pytest.raises(SaaSValidationError):
        encode_bucket(empty_bucket())
