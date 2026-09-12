"""Small serialized SQL transport for PostgreSQL and isolated SQLite stores."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import aiosqlite
import psycopg
from psycopg.rows import dict_row

from gryphon.errors import CacheError, SaaSStoreError, SaaSValidationError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.recovery_lease import RecoveryLease

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

SQLValue = str | int | float | None
SQLRow = dict[str, SQLValue]
WRITE_LOCK_ID = 47525950484
HOST_LOCK_ID = 47525950485
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS saas_tenants ("
    "id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS saas_specs ("
    "tenant_id TEXT NOT NULL REFERENCES saas_tenants(id), id TEXT NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(tenant_id,id))",
    "CREATE TABLE IF NOT EXISTS saas_channels ("
    "tenant_id TEXT NOT NULL REFERENCES saas_tenants(id), id TEXT NOT NULL UNIQUE, "
    "enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), key_digest TEXT UNIQUE, payload TEXT NOT NULL, "
    "PRIMARY KEY(tenant_id,id))",
    "CREATE TABLE IF NOT EXISTS saas_bindings (tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
    "spec_id TEXT NOT NULL, PRIMARY KEY(tenant_id,channel_id,spec_id), "
    "FOREIGN KEY(tenant_id,channel_id) REFERENCES saas_channels(tenant_id,id), "
    "FOREIGN KEY(tenant_id,spec_id) REFERENCES saas_specs(tenant_id,id))",
    "CREATE TABLE IF NOT EXISTS saas_usage (tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
    "tool TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('success','error')), "
    "calls BIGINT NOT NULL CHECK(calls >= 0), latency_ms DOUBLE PRECISION NOT NULL CHECK(latency_ms >= 0), "
    "PRIMARY KEY(tenant_id,channel_id,tool,status), "
    "FOREIGN KEY(tenant_id,channel_id) REFERENCES saas_channels(tenant_id,id))",
    "CREATE TABLE IF NOT EXISTS saas_audit (id TEXT PRIMARY KEY, "
    "tenant_id TEXT NOT NULL REFERENCES saas_tenants(id), created_at DOUBLE PRECISION NOT NULL, "
    "payload TEXT NOT NULL)",
)


class SaaSDatabase:
    """Own one async connection and serialize complete transactions, including reads."""

    def __init__(self, url: str) -> None:
        """Accept explicit PostgreSQL DSNs or sqlite:/// paths; never read settings."""
        self._url = url
        self._sqlite: aiosqlite.Connection | None = None
        self._postgres: psycopg.AsyncConnection[SQLRow] | None = None
        self._lock = asyncio.Lock()
        self._lease: RecoveryLease | None = None
        self._host_lease = False

    async def initialize(self) -> None:
        """Open storage and atomically install schema, closing on initialization failure."""
        if self._sqlite is not None or self._postgres is not None:
            return
        try:
            if self._url.startswith("sqlite:///"):
                self._sqlite = await aiosqlite.connect(self._url[len("sqlite:///") :], isolation_level=None)
                self._sqlite.row_factory = aiosqlite.Row
                await self._sqlite.execute("PRAGMA foreign_keys = ON")
                await self._sqlite.execute("PRAGMA busy_timeout = 5000")
            elif self._url.startswith(("postgresql://", "postgres://")):
                self._postgres = await psycopg.AsyncConnection[SQLRow].connect(
                    self._url, autocommit=True, row_factory=dict_row
                )
            else:
                raise SaaSValidationError("Unsupported control-plane database URL")
            async with self.transaction():
                for statement in SCHEMA:
                    await self.execute(statement)
        except (aiosqlite.Error, psycopg.Error) as exc:
            await self.close()
            raise SaaSStoreError("Control-plane database initialization failed") from exc
        except BaseException:
            await self.close()
            raise

    async def acquire_host_lease(self) -> None:
        """Fail closed unless this connection exclusively owns the hosted database lifecycle."""
        async with self._lock:
            if self._host_lease:
                return
            try:
                await finish_cleanup(self._acquire_host_lease())
                self._host_lease = True
            except (CacheError, aiosqlite.Error, psycopg.Error):
                await finish_cleanup(self._close())
                raise SaaSStoreError("Hosted database is already owned or unavailable") from None
            except BaseException:
                await finish_cleanup(self._close())
                raise

    async def _acquire_host_lease(self) -> None:
        """Acquire using the real SQLite file or a PostgreSQL session-level advisory lock."""
        if self._sqlite is not None:
            rows = await self.execute("PRAGMA database_list")
            path = str(rows[0]["file"])
            if path:
                self._lease = RecoveryLease(path)
                await asyncio.to_thread(self._lease.acquire)
        elif self._postgres is not None:
            rows = await self.execute("SELECT pg_try_advisory_lock(?) AS acquired", (HOST_LOCK_ID,))
            if not rows or rows[0]["acquired"] is not True:
                raise SaaSStoreError("Hosted database is already owned or unavailable")
        else:
            raise SaaSStoreError("Control-plane database is not initialized")

    async def close(self) -> None:
        """Drain transactions, close connections, and release hosted ownership despite cancellation."""
        async with self._lock:
            await finish_cleanup(self._close())

    async def _close(self) -> None:
        """Release registered partial resources; PostgreSQL disconnect releases session locks."""
        try:
            if self._sqlite is not None:
                await self._sqlite.close()
                self._sqlite = None
            if self._postgres is not None:
                await self._postgres.close()
                self._postgres = None
        finally:
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            self._host_lease = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Commit atomically or roll back even on cancellation; serialize quota checks."""
        async with self._lock:
            try:
                await self.execute("BEGIN IMMEDIATE" if self._sqlite is not None else "BEGIN")
                try:
                    if self._postgres is not None:
                        await self.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK_ID,))
                    yield
                    await self.execute("COMMIT")
                except BaseException:
                    await self.execute("ROLLBACK")
                    raise
            except (aiosqlite.Error, psycopg.Error) as exc:
                raise SaaSStoreError("Control-plane database operation failed") from exc

    async def execute(self, sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Run internal SQL with bound parameters inside a caller-owned transaction."""
        if self._sqlite is not None:
            async with self._sqlite.execute(sql, params) as cursor:
                return [cast("SQLRow", dict(row)) for row in await cursor.fetchall()]
        if self._postgres is not None:
            async with self._postgres.cursor() as cursor:
                await cursor.execute(sql.replace("?", "%s"), params)
                return await cursor.fetchall() if cursor.description else []
        raise SaaSStoreError("Control-plane database is not initialized")
