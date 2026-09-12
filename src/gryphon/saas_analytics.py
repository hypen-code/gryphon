"""Serialized tenant-scoped daily analytics with bounded deduplication receipts."""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from gryphon.errors import CapacityError, SaaSNotFoundError, SaaSQuotaError, SaaSValidationError
from gryphon.models import Channel
from gryphon.models.analytics import RunMetrics
from gryphon.models.traffic import MAX_DURATION_MS, MAX_MEASUREMENT, RequestMetrics
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_analytics_report import build_report, request_delta, run_delta
from gryphon.saas_analytics_schema import (
    MAX_CHANNELS,
    MAX_RECEIPTS,
    MAX_ROWS,
    RETENTION_DAYS,
    Bucket,
    decode_bucket,
    empty_bucket,
    encode_bucket,
    merge_bucket,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from gryphon.saas_database import SaaSDatabase, SQLRow, SQLValue

REPORT_ADMISSION_LIMIT = 2
REPORT_QUEUE_SECONDS = 2.0


class AnalyticsStore:
    """Persist only daily numeric aggregates and bounded opaque event digests.

    The supplied database owns connection lifetime and transaction serialization.
    Observer failures propagate to the caller; analytics never owns execution.
    """

    def __init__(self, database: SaaSDatabase, *, clock: Callable[[], float] = time.time) -> None:
        """Accept a shared control connection and an injectable UTC wall clock."""
        self._db = database
        self._clock = clock
        self._report_lock = asyncio.Lock()
        self._report_admitted = 0

    async def record_run(self, tenant_id: str, channel_id: str, metrics: RunMetrics) -> bool:
        """Record one terminal observation; retained duplicate run IDs return false."""
        try:
            checked = RunMetrics.model_validate(metrics.model_dump(warnings=False), strict=True)
        except ValidationError as exc:
            raise SaaSValidationError("Invalid run analytics observation") from exc
        self._validate_run_bounds(checked)
        return await self._record(tenant_id, channel_id, "run", checked.run_id, run_delta(checked))

    async def record_request(self, tenant_id: str, channel_id: str, metrics: RequestMetrics) -> bool:
        """Record observed request traffic independently of run terminal observations."""
        try:
            checked = RequestMetrics.model_validate(metrics.model_dump(warnings=False), strict=True)
        except ValidationError as exc:
            raise SaaSValidationError("Invalid request analytics observation") from exc
        return await self._record(tenant_id, channel_id, "request", checked.request_id, request_delta(checked))

    @staticmethod
    def _validate_run_bounds(metrics: RunMetrics) -> None:
        """Bound instrumentation counters even if a caller bypassed model construction."""
        for key, value in metrics.model_dump().items():
            if type(value) in (int, float):
                maximum = MAX_DURATION_MS if key.endswith("_ms") else MAX_MEASUREMENT
                if not 0 <= value <= maximum:
                    raise SaaSValidationError("Run analytics measurement exceeds bound")

    async def _require_scope(self, tenant_id: str, channel_id: str | None) -> None:
        """Require tenant existence and exact channel membership, including empty windows."""
        tenants = await self._db.execute("SELECT id FROM saas_tenants WHERE id=?", (tenant_id,))
        if not tenants:
            raise SaaSNotFoundError("Analytics resource not found")
        if channel_id is not None:
            channels = await self._db.execute(
                "SELECT id FROM saas_channels WHERE tenant_id=? AND id=?", (tenant_id, channel_id)
            )
            if not channels:
                raise SaaSNotFoundError("Analytics resource not found")

    async def _prune(self, tenant_id: str, cutoff: str) -> None:
        """Expire analytics globally, but cap receipts only within the recording tenant."""
        await self._db.execute("DELETE FROM saas_analytics_daily WHERE day<?", (cutoff,))
        await self._db.execute("DELETE FROM saas_analytics_receipts WHERE day<?", (cutoff,))
        rows = await self._db.execute(
            "SELECT COUNT(*) AS count FROM saas_analytics_receipts WHERE tenant_id=?", (tenant_id,)
        )
        overflow = int(str(rows[0]["count"])) - MAX_RECEIPTS
        if overflow > 0:
            await self._db.execute(
                "DELETE FROM saas_analytics_receipts WHERE tenant_id=? AND id IN "
                "(SELECT id FROM saas_analytics_receipts WHERE tenant_id=? ORDER BY recorded_at,id LIMIT ?)",
                (tenant_id, tenant_id, overflow),
            )

    async def _initialize_channel(self, tenant_id: str, channel_id: str, now: float) -> None:
        """Preserve the actual first observation and enforce the retained channel bound."""
        existing = await self._db.execute(
            "SELECT channel_id FROM saas_analytics_metadata WHERE tenant_id=? AND channel_id=?", (tenant_id, channel_id)
        )
        if existing:
            return
        rows = await self._db.execute(
            "SELECT COUNT(*) AS count FROM saas_analytics_metadata WHERE tenant_id=?", (tenant_id,)
        )
        if int(str(rows[0]["count"])) >= MAX_CHANNELS:
            raise SaaSQuotaError("Analytics channel quota exceeded")
        await self._db.execute("INSERT INTO saas_analytics_metadata VALUES (?,?,?)", (tenant_id, channel_id, now))

    async def _aggregate(self, tenant_id: str, channel_id: str, day: str, delta: Bucket) -> None:
        """Check aggregate bounds before updating a day, inside the receipt transaction."""
        rows = await self._db.execute(
            "SELECT payload FROM saas_analytics_daily WHERE tenant_id=? AND channel_id=? AND day=?",
            (tenant_id, channel_id, day),
        )
        bucket = decode_bucket(str(rows[0]["payload"])) if rows else empty_bucket()
        if not rows:
            counts = await self._db.execute(
                "SELECT COUNT(*) AS count FROM saas_analytics_daily WHERE tenant_id=?", (tenant_id,)
            )
            if int(str(counts[0]["count"])) >= MAX_ROWS:
                raise SaaSQuotaError("Analytics daily row quota exceeded")
        merge_bucket(bucket, delta)
        await self._db.execute(
            "INSERT INTO saas_analytics_daily VALUES (?,?,?,?) ON CONFLICT(tenant_id,channel_id,day) "
            "DO UPDATE SET payload=excluded.payload",
            (tenant_id, channel_id, day, encode_bucket(bucket)),
        )

    async def _record(
        self,
        tenant_id: str,
        channel_id: str,
        event_type: Literal["run", "request"],
        event_id: str,
        delta: Bucket,
    ) -> bool:
        """Commit receipt, first observation, retention, and daily delta atomically."""
        encode_bucket(delta)
        now = self._clock()
        today = datetime.fromtimestamp(now, UTC).date()
        cutoff = (today - timedelta(days=RETENTION_DAYS - 1)).isoformat()
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        async with self._db.transaction():
            await self._require_scope(tenant_id, channel_id)
            await self._prune(tenant_id, cutoff)
            existing = await self._db.execute(
                "SELECT id FROM saas_analytics_receipts WHERE tenant_id=? AND channel_id=? "
                "AND event_type=? AND event_digest=?",
                (tenant_id, channel_id, event_type, digest),
            )
            if existing:
                return False
            await self._initialize_channel(tenant_id, channel_id, now)
            await self._db.execute(
                "INSERT INTO saas_analytics_receipts VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), tenant_id, channel_id, event_type, digest, now, today.isoformat()),
            )
            await self._aggregate(tenant_id, channel_id, today.isoformat(), delta)
            await self._prune(tenant_id, cutoff)
        return True

    async def _report_rows(
        self,
        tenant_id: str,
        channel_id: str | None,
        start: str,
        end: str,
    ) -> tuple[list[SQLRow], dict[str, str], float | None]:
        """Read a consistent bounded snapshot, keeping all identity filtering in SQL."""
        suffix = "" if channel_id is None else " AND channel_id=?"
        params: tuple[SQLValue, ...] = (tenant_id,) if channel_id is None else (tenant_id, channel_id)
        async with self._db.transaction():
            await self._require_scope(tenant_id, channel_id)
            rows = await self._db.execute(
                "SELECT channel_id,day,payload FROM saas_analytics_daily WHERE tenant_id=?"
                f"{suffix} AND day>=? AND day<? ORDER BY day,channel_id LIMIT ?",
                (*params, start, end, MAX_ROWS + 1),
            )
            channel_suffix = "" if channel_id is None else " AND id=?"
            channels = await self._db.execute(
                f"SELECT payload FROM saas_channels WHERE tenant_id=?{channel_suffix} ORDER BY id LIMIT ?",
                (*params, MAX_CHANNELS + 1),
            )
            metadata = await self._db.execute(
                "SELECT MIN(initialized_since) AS initialized_since FROM saas_analytics_metadata WHERE tenant_id=?"
                f"{suffix}",
                params,
            )
        if len(rows) > MAX_ROWS or len(channels) > MAX_CHANNELS:
            raise SaaSQuotaError("Analytics report bound exceeded")
        names = {}
        for row in channels:
            channel = Channel.model_validate_json(str(row["payload"]))
            names[channel.id] = channel.name
        since = metadata[0]["initialized_since"]
        return rows, names, float(since) if since is not None else None

    @asynccontextmanager
    async def _report_permit(self) -> AsyncIterator[None]:
        """Bound reports to one active build and one waiter without locking observations."""
        if self._report_admitted >= REPORT_ADMISSION_LIMIT:
            raise CapacityError("Analytics report capacity exceeded")
        self._report_admitted += 1
        try:
            try:
                async with asyncio.timeout(REPORT_QUEUE_SECONDS):
                    await self._report_lock.acquire()
            except TimeoutError:
                raise CapacityError("Analytics report queue wait exceeded") from None
            try:
                yield
            finally:
                self._report_lock.release()
        finally:
            self._report_admitted -= 1

    async def get_report(self, tenant_id: str, *, days: int = 7, channel_id: str | None = None) -> dict[str, Any]:
        """Read one bounded snapshot; cancellation drains its CPU build before releasing admission."""
        if type(days) is not int or not 1 <= days <= RETENTION_DAYS:
            raise SaaSValidationError("Analytics days must be between 1 and 90")
        async with self._report_permit():
            end = datetime.fromtimestamp(self._clock(), UTC).date() + timedelta(days=1)
            start = end - timedelta(days=days)
            rows, channels, since = await self._report_rows(tenant_id, channel_id, start.isoformat(), end.isoformat())
            task = asyncio.create_task(asyncio.to_thread(build_report, rows, channels, start, end, since))
            report = await finish_cleanup(task)
            report["window"].update(tenant_id=tenant_id, channel_id=channel_id)
            return report
