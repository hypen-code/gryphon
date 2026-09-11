"""SQLite-backed owner-scoped recipe cache with TTL and LRU eviction."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from gryphon.errors import CacheError, InputValidationError
from gryphon.models import CacheEntry, CacheSummary
from gryphon.utils.hashing import hash_code, hash_content
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS code_cache (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    code TEXT NOT NULL,
    servers_used TEXT NOT NULL,
    swagger_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    use_count INTEGER DEFAULT 1,
    ttl_seconds INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT 'local',
    input_schema TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cache_last_used ON code_cache(last_used_at);
CREATE INDEX IF NOT EXISTS idx_cache_description ON code_cache(description);
"""


class _SQLiteStore:
    """One pooled connection with serialized, durable transactions."""

    def __init__(self, db_path: str) -> None:
        """Keep connection and lock state without opening the database."""
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _open(self, schema: str) -> None:
        """Open a WAL/FULL database, avoiding filesystem work on the event loop."""
        async with self._lock:
            if self._db is not None:
                return
            db: aiosqlite.Connection | None = None
            try:
                await asyncio.to_thread(Path(self._db_path).parent.mkdir, parents=True, exist_ok=True)
                db = await aiosqlite.connect(self._db_path, isolation_level=None)
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=FULL")
                await db.execute("PRAGMA busy_timeout=5000")
                await db.executescript(schema)
                self._db = db
            except (OSError, aiosqlite.Error):
                raise CacheError("Failed to initialize database") from None
            finally:
                if db is not None and self._db is None:
                    await db.close()

    async def close(self) -> None:
        """Close the pooled connection after any active transaction completes."""
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    @asynccontextmanager
    async def _transaction(self, operation: str) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize transactions and always queue rollback after pending commands."""
        async with self._lock:
            db = self._db
            if db is None:
                raise CacheError(f"Failed to {operation}: store is not initialized")
            try:
                await db.execute("BEGIN IMMEDIATE")
                yield db
                await db.commit()
            except aiosqlite.Error:
                raise CacheError(f"Failed to {operation}") from None
            finally:
                try:
                    await db.rollback()
                except aiosqlite.Error:
                    raise CacheError(f"Failed to {operation}") from None


class CacheStore(_SQLiteStore):
    """Async SQLite cache of programs and schemas, never execution input values."""

    def __init__(self, db_path: str, ttl_seconds: int = 3600, max_entries: int = 500) -> None:
        """Configure recipe retention.

        Args:
            db_path: Filesystem path for the SQLite database file.
            ttl_seconds: Default cache entry lifetime in seconds.
            max_entries: Maximum number of entries before LRU eviction.
        """
        super().__init__(db_path)
        if ttl_seconds < 1 or max_entries < 1:
            raise InputValidationError("Cache retention limits must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries

    async def initialize(self) -> None:
        """Create tables and invalidate legacy recipes lacking safe identity.

        Raises:
            CacheError: If database initialization fails.
        """
        await self._open(_CREATE_TABLE_SQL)
        async with self._transaction("initialize cache") as db:
            async with db.execute("PRAGMA table_info(code_cache)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            for name, default in (("owner", "'local'"), ("input_schema", "'{}'")):
                if name not in columns:
                    await db.execute(f"ALTER TABLE code_cache ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")
                    await db.execute("DELETE FROM code_cache")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_owner ON code_cache(owner)")
            await self._prune(db)
        logger.info("cache_initialized")

    async def store(
        self,
        code: str,
        description: str,
        servers_used: list[str],
        swagger_hash: str,
        *,
        owner: str = "local",
        input_schema: dict[str, Any] | None = None,
    ) -> str:
        """Store a recipe identified by program, schema, catalog, and owner.

        Args:
            code: Program source; no execution inputs are appended.
            description: Searchable recipe description.
            servers_used: API server names used by the program.
            swagger_hash: Combined catalog identity.
            owner: Server-derived ownership namespace.
            input_schema: Declarative parameter schema, not default input values.

        Returns:
            SHA256 recipe identifier.
        """
        entry_id, schema, servers = self._metadata(code, input_schema, swagger_hash, owner, servers_used)
        now = time.time()
        async with self._transaction("store cache entry") as db:
            await db.execute("DELETE FROM code_cache WHERE (? - created_at) >= ttl_seconds", (now,))
            # Upsert — increment use_count if already exists
            await db.execute(
                """INSERT INTO code_cache
                   (id, description, code, servers_used, swagger_hash, created_at,
                    last_used_at, use_count, ttl_seconds, owner, input_schema)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                   last_used_at = excluded.last_used_at, use_count = use_count + 1""",
                (entry_id, description, code, servers, swagger_hash, now, now, self._ttl_seconds, owner, schema),
            )
            await self._prune(db)
        logger.debug("cache_stored", id=entry_id[:12])
        return entry_id

    async def get(self, entry_id: str, *, owner: str = "local") -> CacheEntry | None:
        """Retrieve a recipe only in the caller's namespace and refresh its LRU.

        Args:
            entry_id: Cache recipe identifier.
            owner: Server-derived ownership namespace.

        Returns:
            The valid recipe, or None for missing, expired, or foreign IDs.
        """
        now = time.time()
        async with self._transaction("retrieve cache entry") as db:
            async with db.execute("SELECT * FROM code_cache WHERE id = ? AND owner = ?", (entry_id, owner)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                logger.debug("cache_miss", id=entry_id[:12])
                return None
            entry = self._row_to_entry(row)
            # Check TTL
            if now - entry.created_at >= entry.ttl_seconds:
                await self._delete(db, entry_id)
                logger.debug("cache_expired", id=entry_id[:12])
                return None
            # Update last_used_at
            await db.execute(
                "UPDATE code_cache SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (now, entry_id),
            )
        logger.debug("cache_hit", id=entry_id[:12])
        return entry

    async def search(self, query: str | None = None, limit: int = 50, *, owner: str = "local") -> list[CacheSummary]:
        """Search nonexpired recipes in one ownership namespace.

        Args:
            query: Optional case-insensitive description substring.
            limit: Maximum results, bounded by the cache capacity.
            owner: Server-derived ownership namespace.

        Returns:
            Recipe summaries ordered by popularity and recency.
        """
        now = time.time()
        limit = max(0, min(limit, self._max_entries))
        async with self._transaction("search cache") as db:
            if query:
                where = "description LIKE ? AND (? - created_at) < ttl_seconds AND owner = ?"
                params = (f"%{query}%", now, owner, limit)
            else:
                where = "(? - created_at) < ttl_seconds AND owner = ?"
                params = (now, owner, limit)  # type: ignore[assignment]
            async with db.execute(
                f"SELECT * FROM code_cache WHERE {where} ORDER BY use_count DESC, last_used_at DESC LIMIT ?",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            CacheSummary(
                id=row["id"],
                description=row["description"],
                servers_used=json.loads(row["servers_used"]),
                use_count=row["use_count"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def invalidate_by_swagger_hash(self, swagger_hash: str) -> int:
        """Remove stale catalog recipes across namespaces for trusted maintenance.

        Args:
            swagger_hash: Old catalog hash to invalidate.

        Returns:
            Number of removed recipes.
        """
        async with (
            self._transaction("invalidate cache entries") as db,
            db.execute("DELETE FROM code_cache WHERE swagger_hash = ?", (swagger_hash,)) as cursor,
        ):
            count = cursor.rowcount
        if count:
            logger.info("cache_invalidated", count=count)
        return count

    async def cleanup_expired(self) -> int:
        """Remove all expired recipes.

        Returns:
            Number of removed recipes.
        """
        async with (
            self._transaction("clean expired entries") as db,
            db.execute("DELETE FROM code_cache WHERE (? - created_at) >= ttl_seconds", (time.time(),)) as cursor,
        ):
            count = cursor.rowcount
        logger.debug("cache_expired_cleaned", count=count)
        return count

    @staticmethod
    def _metadata(
        code: str, input_schema: dict[str, Any] | None, swagger_hash: str, owner: str, servers_used: list[str]
    ) -> tuple[str, str, str]:
        """Serialize recipe metadata with exact source and canonical schema identity."""
        if input_schema is not None and not isinstance(input_schema, dict):
            raise InputValidationError("Invalid recipe metadata")
        try:
            schema = json.dumps(input_schema or {}, sort_keys=True, separators=(",", ":"), allow_nan=False)
            entry_id = hash_content(json.dumps([hash_code(code), schema, swagger_hash, owner]))
            return entry_id, schema, json.dumps(servers_used)
        except (TypeError, ValueError, RecursionError):
            raise InputValidationError("Invalid recipe metadata") from None

    async def _prune(self, db: aiosqlite.Connection) -> None:
        """Bound the table by removing expired and least recently used recipes."""
        await db.execute("DELETE FROM code_cache WHERE (? - created_at) >= ttl_seconds", (time.time(),))
        async with db.execute(
            """DELETE FROM code_cache WHERE id IN (
               SELECT id FROM code_cache ORDER BY last_used_at DESC, rowid DESC LIMIT -1 OFFSET ?)""",
            (self._max_entries,),
        ) as cursor:
            if cursor.rowcount:
                logger.info("cache_evicted_lru", count=cursor.rowcount)

    async def _delete(self, db: aiosqlite.Connection, entry_id: str) -> None:
        """Delete a single cache entry using the caller's active transaction."""
        await db.execute("DELETE FROM code_cache WHERE id = ?", (entry_id,))

    def _row_to_entry(self, row: aiosqlite.Row) -> CacheEntry:
        """Convert a stored recipe into the shared boundary model."""
        return CacheEntry(
            id=row["id"],
            description=row["description"],
            code=row["code"],
            servers_used=json.loads(row["servers_used"]),
            swagger_hash=row["swagger_hash"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=row["use_count"],
            ttl_seconds=row["ttl_seconds"],
            owner=row["owner"],
            input_schema=json.loads(row["input_schema"]),
        )
