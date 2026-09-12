"""Opt-in analytics SQL portability checks against newly created PostgreSQL only."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from test_saas_postgres import postgres_url as postgres_url

from gryphon.errors import SaaSNotFoundError, SaaSStoreError
from gryphon.models.analytics import RunMetrics
from gryphon.models.traffic import RequestMetrics
from gryphon.saas_analytics import AnalyticsStore
from gryphon.saas_analytics_schema import empty_bucket, encode_bucket
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from pydantic import SecretStr


async def test_postgres_analytics_atomic_dedup_retention_and_foreign_scope(postgres_url: SecretStr) -> None:
    """Real cross-connection transactions deduplicate, cap receipts, and enforce scoped foreign keys."""
    first = SaaSStore(postgres_url.get_secret_value())
    second = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(first.close)
        cleanup.push_async_callback(second.close)
        await first.initialize()
        await second.initialize()
        tenant = await first.create_tenant("analytics")
        other = await first.create_tenant("other")
        channel = await first.create_channel(tenant.id, "channel")
        now = 1767225600.0
        stores = [AnalyticsStore(store._db, clock=lambda: now) for store in (first, second)]
        event = RunMetrics(
            run_id=uuid4().hex,
            origin="replay",
            status="succeeded",
            sandbox_mode="restricted",
            backend_started=True,
            api_calls=1,
            api_responses=1,
            upstream_bytes=9,
            result_bytes=5,
            comparison_eligible=True,
        )
        results = await asyncio.gather(*(store.record_run(tenant.id, channel.id, event) for store in stores))
        assert results.count(True) == 1
        with pytest.raises(SaaSNotFoundError):
            await stores[0].get_report(other.id, channel_id=channel.id)
        with pytest.raises(SaaSStoreError):
            async with first._db.transaction():
                await first._db.execute(
                    "INSERT INTO saas_analytics_daily VALUES (?,?,?,?)",
                    (other.id, channel.id, "2026-01-01", encode_bucket(empty_bucket())),
                )
        with patch("gryphon.saas_analytics.MAX_RECEIPTS", 1):
            now += 1
            await stores[0].record_request(
                tenant.id, channel.id, RequestMetrics(request_id=uuid4().hex, tool="get_run", success=True)
            )
        report = await stores[1].get_report(tenant.id)
        assert report["summary"]["comparison_upstream_estimated_tokens"] == 3
        assert report["summary"]["comparison_result_estimated_tokens"] == 2
        async with first._db.transaction():
            sql = "SELECT id FROM saas_analytics_receipts WHERE tenant_id=?"
            assert len(await first._db.execute(sql, (tenant.id,))) == 1
        now += 90 * 86400
        assert await stores[1].record_run(tenant.id, channel.id, event)
        assert (await stores[0].get_report(tenant.id, days=90))["summary"]["terminal_runs"] == 1


async def test_postgres_analytics_additive_upgrade_retains_existing_control_data(postgres_url: SecretStr) -> None:
    """Installing absent analytics tables does not rewrite legacy usage or channel authority."""
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        tenant = await store.create_tenant("legacy")
        channel = await store.create_channel(tenant.id, "legacy")
        await store.record_usage(tenant.id, channel.id, "execute_code", "success", 1)
        async with store._db.transaction():
            await store._db.execute("DROP TABLE saas_analytics_receipts")
            await store._db.execute("DROP TABLE saas_analytics_daily")
            await store._db.execute("DROP TABLE saas_analytics_metadata")
        await store.close()
        await store.initialize()
        report = await AnalyticsStore(store._db).get_report(tenant.id)
        assert report["window"]["recording_since"] is None
        assert report["methodology"]["dedup_receipt_cap_per_tenant"] == 100000
        assert report["summary"]["terminal_runs"] == 0
        assert (await store.list_usage(tenant.id))[0].calls == 1
        assert await store.get_channel(tenant.id, channel.id) == channel


async def test_postgres_analytics_receipt_cap_preserves_quiet_tenant_dedup(postgres_url: SecretStr) -> None:
    """PostgreSQL cap eviction targets only the noisy tenant, never another tenant's recent receipts."""
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        quiet = await store.create_tenant("quiet")
        noisy = await store.create_tenant("noisy")
        quiet_channel = await store.create_channel(quiet.id, "quiet")
        noisy_channel = await store.create_channel(noisy.id, "noisy")
        now = 1767225600.0
        analytics = AnalyticsStore(store._db, clock=lambda: now)
        event = RequestMetrics(request_id=uuid4().hex, tool="get_run", success=True)
        with patch("gryphon.saas_analytics.MAX_RECEIPTS", 2):
            assert await analytics.record_request(quiet.id, quiet_channel.id, event)
            before = await analytics.get_report(quiet.id)
            for _ in range(6):
                now += 1
                observation = event.model_copy(update={"request_id": uuid4().hex})
                assert await analytics.record_request(noisy.id, noisy_channel.id, observation)
            assert not await analytics.record_request(quiet.id, quiet_channel.id, event)
            assert await analytics.get_report(quiet.id) == before
        assert (await analytics.get_report(noisy.id))["summary"]["requests"] == 6
        async with store._db.transaction():
            rows = await store._db.execute(
                "SELECT tenant_id,COUNT(*) AS count FROM saas_analytics_receipts GROUP BY tenant_id"
            )
        assert {str(row["tenant_id"]): int(str(row["count"])) for row in rows} == {quiet.id: 1, noisy.id: 2}
