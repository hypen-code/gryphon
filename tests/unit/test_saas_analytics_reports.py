"""Bound report memory/CPU concurrency and reject misleading or unsafe aggregate totals."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from test_saas_analytics import request, run, timestamp
from test_saas_analytics import store as store

from gryphon.errors import CapacityError, SaaSValidationError
from gryphon.models.traffic import MAX_MEASUREMENT
from gryphon.saas_analytics import AnalyticsStore
from gryphon.saas_analytics_report import build_report, methodology, run_delta

if TYPE_CHECKING:
    from datetime import date

    from gryphon.saas_database import SQLRow
    from gryphon.saas_store import SaaSStore


class BlockedReport:
    """Block only report CPU work with deterministic cross-thread release signaling."""

    def __init__(self) -> None:
        """Capture the test loop for thread-safe startup signaling."""
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.release = threading.Event()

    def __call__(
        self,
        rows: list[SQLRow],
        channels: dict[str, str],
        start: date,
        end: date,
        since: float | None,
    ) -> dict[str, Any]:
        """Wait for explicit release before running the production report calculation."""
        self.loop.call_soon_threadsafe(self.started.set)
        if not self.release.wait(timeout=5):
            raise AssertionError("Report test did not release its worker")
        return build_report(rows, channels, start, end, since)


async def test_analytics_report_admission_precedes_reads_and_cancel_drains_worker(store: SaaSStore) -> None:
    """One queued reader cannot materialize rows or overtake an active cancelled CPU build."""
    tenant = await store.create_tenant("tenant")
    channel = await store.create_channel(tenant.id, "channel")
    analytics, blocker = AnalyticsStore(store._db), BlockedReport()
    reads = AsyncMock(wraps=analytics._report_rows)
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    with patch("gryphon.saas_analytics.build_report", blocker), patch.object(analytics, "_report_rows", reads):
        try:
            first = asyncio.create_task(analytics.get_report(tenant.id))
            tasks.append(first)
            await asyncio.wait_for(blocker.started.wait(), timeout=2)
            second = asyncio.create_task(analytics.get_report(tenant.id))
            tasks.append(second)
            await asyncio.sleep(0)
            assert analytics._report_admitted == 2 and reads.await_count == 1
            with pytest.raises(CapacityError):
                await analytics.get_report(tenant.id)
            assert await asyncio.wait_for(analytics.record_request(tenant.id, channel.id, request()), timeout=1)
            assert await asyncio.wait_for(analytics.record_run(tenant.id, channel.id, run()), timeout=1)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done() and not second.done()
            assert analytics._report_admitted == 2 and reads.await_count == 1
            with pytest.raises(CapacityError):
                await analytics.get_report(tenant.id)
            blocker.release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second)["summary"]["requests"] == 1
            assert reads.await_count == 2
        finally:
            blocker.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert analytics._report_admitted == 0
    assert (await analytics.get_report(tenant.id))["summary"]["requests"] == 1


@pytest.mark.parametrize("timeout", [False, True])
async def test_analytics_report_waiter_cancellation_and_timeout_release_only_waiter(
    store: SaaSStore,
    timeout: bool,
) -> None:
    """A cancelled or expired queued report does not release the active worker's slot."""
    tenant = await store.create_tenant("tenant")
    analytics, blocker = AnalyticsStore(store._db), BlockedReport()
    reads = AsyncMock(wraps=analytics._report_rows)
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    with patch("gryphon.saas_analytics.build_report", blocker), patch.object(analytics, "_report_rows", reads):
        try:
            first = asyncio.create_task(analytics.get_report(tenant.id))
            tasks.append(first)
            await asyncio.wait_for(blocker.started.wait(), timeout=2)
            with patch("gryphon.saas_analytics.REPORT_QUEUE_SECONDS", 0.001 if timeout else 2):
                second = asyncio.create_task(analytics.get_report(tenant.id))
                tasks.append(second)
                await asyncio.sleep(0)
                if not timeout:
                    second.cancel()
                with pytest.raises(CapacityError if timeout else asyncio.CancelledError):
                    await second
            assert analytics._report_admitted == 1 and reads.await_count == 1
            assert not first.done()
            blocker.release.set()
            await first
        finally:
            blocker.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert analytics._report_admitted == 0
    await analytics.get_report(tenant.id)


@pytest.mark.parametrize("dimension", ["day", "channel"])
async def test_analytics_report_rejects_overflow_across_individually_valid_buckets(
    store: SaaSStore,
    dimension: str,
) -> None:
    """Combining valid daily counters cannot return values beyond the JSON-safe integer bound."""
    tenant = await store.create_tenant("tenant")
    first = await store.create_channel(tenant.id, "first")
    second = await store.create_channel(tenant.id, "second")
    now = timestamp("2026-01-01T00:00:00Z")
    analytics = AnalyticsStore(store._db, clock=lambda: now)
    await analytics.record_request(tenant.id, first.id, request(request_bytes=MAX_MEASUREMENT))
    if dimension == "day":
        now += 86400
    channel_id = first.id if dimension == "day" else second.id
    await analytics.record_request(tenant.id, channel_id, request(request_bytes=1))
    with pytest.raises(SaaSValidationError, match="overflow"):
        await analytics.get_report(tenant.id, days=2)
    assert analytics._report_admitted == 0 and not analytics._report_lock.locked()
    if dimension == "day":
        report = await analytics.get_report(tenant.id, days=1)
        assert report["summary"]["request_wire_bytes"] == 1
    else:
        report = await analytics.get_report(tenant.id, channel_id=first.id)
        assert report["summary"]["request_wire_bytes"] == MAX_MEASUREMENT


@pytest.mark.parametrize(
    ("started", "calls", "expected"),
    [(True, 0, 1), (True, 1, 0), (False, 0, 0)],
)
def test_analytics_pure_compute_excludes_rejected_api_attempts(started: bool, calls: int, expected: int) -> None:
    """A failed or rejected API attempt is not pure compute merely because no response was accepted."""
    delta = run_delta(
        run(
            backend_started=started,
            api_calls=calls,
            api_responses=0,
            status="failed",
            error_type="security" if calls else "execution",
        )
    )
    assert delta["pure_compute_runs"] == expected


def test_analytics_methodology_states_scope_gaps_and_heuristic_limitations() -> None:
    """The report describes partial registered tool observations, not a complete billing audit."""
    details = methodology()
    assert "Allowlisted tools/call" in details["traffic"]
    assert "SDK-produced bodies" in details["traffic"]
    assert "not proven client reception or model consumption" in details["traffic"]
    assert "excluding its own observation persistence" in details["durations"]
    assert "Final response handoff waits for bounded persistence attempts" in details["durations"]
    assert "initialize" in details["traffic_exclusions"] and "tools/list" in details["traffic_exclusions"]
    assert "HTTP headers" in details["traffic_exclusions"] and "model context" in details["traffic_exclusions"]
    assert "dropped/crash" in details["completeness"] and "not a complete billing" in details["completeness"]
    assert "no actual tokenizer" in details["token_estimate"]
    assert details["actual_model_tokens"] is None and details["actual_model_cost"] is None
    assert details["dedup_receipt_cap_per_tenant"] == 100000
    assert "dedup_global_receipt_cap" not in details
    assert "hosted tenant quota" in details["dedup_storage_bound"]


async def test_analytics_noisy_tenant_cannot_evict_quiet_tenant_receipts(store: SaaSStore) -> None:
    """Within retention, only a tenant's own activity can consume its receipt allowance."""
    quiet = await store.create_tenant("quiet")
    noisy = await store.create_tenant("noisy")
    quiet_channel = await store.create_channel(quiet.id, "quiet")
    noisy_channel = await store.create_channel(noisy.id, "noisy")
    now = timestamp("2026-01-01T00:00:00Z")
    analytics = AnalyticsStore(store._db, clock=lambda: now)
    event = run()
    with patch("gryphon.saas_analytics.MAX_RECEIPTS", 2):
        assert await analytics.record_run(quiet.id, quiet_channel.id, event)
        before = await analytics.get_report(quiet.id)
        for _ in range(6):
            now += 1
            assert await analytics.record_run(noisy.id, noisy_channel.id, run(origin="submit"))
        assert not await analytics.record_run(quiet.id, quiet_channel.id, event)
        after = await analytics.get_report(quiet.id)
    assert after == before
    assert after["summary"]["terminal_runs"] == 1
    assert (await analytics.get_report(noisy.id))["summary"]["terminal_runs"] == 6
    async with store._db.transaction():
        rows = await store._db.execute(
            "SELECT tenant_id,COUNT(*) AS count FROM saas_analytics_receipts GROUP BY tenant_id"
        )
    assert {str(row["tenant_id"]): int(str(row["count"])) for row in rows} == {quiet.id: 1, noisy.id: 2}
