"""Unit tests for the owner-scoped recipe cache store."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from gryphon.errors import CacheError, InputValidationError
from gryphon.runtime.cache import CacheStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[CacheStore]:
    """Yield an isolated pooled cache and always release its connection."""
    store = CacheStore(str(tmp_path / "test_cache.db"), ttl_seconds=3600, max_entries=10)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def test_store_and_retrieve(cache: CacheStore) -> None:
    """Stored entry preserves its exact program and schema."""
    code = "result = inputs['count']"
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    entry_id = await cache.store(code, "compute count", ["weather"], "hash123", input_schema=schema)
    entry = await cache.get(entry_id)
    assert entry is not None
    assert (entry.code, entry.input_schema, entry.servers_used) == (code, schema, ["weather"])


async def test_get_nonexistent_returns_none(cache: CacheStore) -> None:
    """Getting a nonexistent identifier returns None."""
    assert await cache.get("nonexistent_id_12345") is None


async def test_search_by_description(cache: CacheStore) -> None:
    """Search returns entries matching description substring."""
    await cache.store("result = 1", "get weather data", ["weather"], "hash1")
    await cache.store("result = 2", "list hotels", ["hotel"], "hash2")
    await cache.store("result = 3", "book hotel room", ["hotel"], "hash3")
    assert {entry.description for entry in await cache.search("hotel")} == {"list hotels", "book hotel room"}


async def test_search_no_filter_returns_all(cache: CacheStore) -> None:
    """Search without filter returns all valid entries."""
    await cache.store("result = 1", "entry one", [], "hash1")
    await cache.store("result = 2", "entry two", [], "hash2")
    assert len(await cache.search()) == 2


async def test_use_count_increments_on_duplicate(cache: CacheStore) -> None:
    """Storing identical recipe identity increments use_count."""
    code = "result = 99"
    entry_id = await cache.store(code, "test code", [], "hash1")
    duplicate = await cache.store(code, "test code again", [], "hash1")
    # Same code -> same ID -> use_count should be 2
    import hashlib  # noqa: PLC0415

    source_hash = hashlib.sha256(code.encode()).hexdigest()
    expected = hashlib.sha256(json.dumps([source_hash, "{}", "hash1", "local"]).encode()).hexdigest()
    entry = await cache.get(entry_id)
    assert entry is not None
    assert (duplicate, entry_id, entry.use_count) == (expected, expected, 2)


async def test_expired_entry_not_returned(tmp_path: Path) -> None:
    """Entries at the TTL boundary are removed and not returned."""
    cache = CacheStore(
        db_path=str(tmp_path / "ttl_cache.db"),
        ttl_seconds=1,  # 1 second TTL
        max_entries=10,
    )
    await cache.initialize()
    try:
        with patch("gryphon.runtime.cache.time.time", return_value=100):
            entry_id = await cache.store("result = 'expired'", "short lived", [], "hash1")
        # Wait for TTL to expire
        with patch("gryphon.runtime.cache.time.time", return_value=101):
            assert await cache.get(entry_id) is None
    finally:
        await cache.close()


async def test_cleanup_expired_removes_old_entries(cache: CacheStore) -> None:
    """cleanup_expired removes entries past their TTL."""
    await cache.store("result = 1", "old entry", [], "hash1")
    with patch("gryphon.runtime.cache.time.time", return_value=time.time() + 3601):
        assert await cache.cleanup_expired() == 1


async def test_lru_eviction_on_max_entries(cache: CacheStore) -> None:
    """Cache retains the most recently accessed entries at its exact bound."""
    first = await cache.store("result = 0", "first", [], "hash")
    second = await cache.store("result = 1", "second", [], "hash")
    for i in range(2, 10):
        await cache.store(f"result = {i}", str(i), [], "hash")
    await cache.get(first)
    await cache.store("result = 10", "new", [], "hash")
    assert await cache.get(second) is None
    assert len(await cache.search()) == 10


async def test_invalidate_by_swagger_hash(cache: CacheStore) -> None:
    """Trusted catalog invalidation removes the stale hash across owners."""
    await cache.store("result = 1", "old", [], "old", owner="alice")
    await cache.store("result = 1", "old", [], "old", owner="bob")
    await cache.store("result = 2", "new", [], "new")
    assert await cache.invalidate_by_swagger_hash("old") == 2
    assert len(await cache.search()) == 1


# ---------------------------------------------------------------------------
# Error handling — aiosqlite.Error branches
# ---------------------------------------------------------------------------


async def test_initialize_raises_cache_error_on_db_failure(tmp_path: Path) -> None:
    """Initialization suppresses storage details from returned errors."""
    store = CacheStore(str(tmp_path / "bad.db"))
    with (
        patch("aiosqlite.connect", side_effect=aiosqlite.Error("private detail")),
        pytest.raises(CacheError, match="^Failed to initialize database$"),
    ):
        await store.initialize()


@pytest.mark.parametrize("operation", ["store", "get", "search", "invalidate_by_swagger_hash", "cleanup_expired"])
async def test_cache_operation_db_failure_is_sanitized(cache: CacheStore, operation: str) -> None:
    """Every pooled operation translates database failures without leaking details."""
    assert cache._db is not None
    arguments: dict[str, tuple[object, ...]] = {
        "store": ("result = 1", "desc", [], "hash"),
        "get": ("id",),
        "search": (),
        "invalidate_by_swagger_hash": ("old",),
        "cleanup_expired": (),
    }
    with (
        patch.object(cache._db, "execute", new=AsyncMock(side_effect=aiosqlite.Error("private detail"))),
        pytest.raises(CacheError) as caught,
    ):
        await getattr(cache, operation)(*arguments[operation])
    assert "private detail" not in str(caught.value)


async def test_owner_namespace_isolation(cache: CacheStore) -> None:
    """Owners cannot discover or read each other's programs."""
    alice = await cache.store("result = 1", "shared description", [], "hash", owner="alice")
    bob = await cache.store("result = 1", "shared description", [], "hash", owner="bob")
    assert alice != bob
    assert await cache.get(alice, owner="bob") is None
    assert [entry.id for entry in await cache.search(owner="bob")] == [bob]
    assert await cache.search("shared", owner="local") == []


async def test_schema_and_catalog_drift_change_identity(cache: CacheStore) -> None:
    """Identity includes schema and catalog even when the program is unchanged."""
    first = await cache.store("result = 1", "desc", [], "hash", input_schema={"type": "object"})
    schema = await cache.store("result = 1", "desc", [], "hash", input_schema={"type": "string"})
    catalog = await cache.store("result = 1", "desc", [], "new", input_schema={"type": "object"})
    assert len({first, schema, catalog}) == 3


async def test_schema_key_order_does_not_change_identity(cache: CacheStore) -> None:
    """Equivalent JSON object ordering uses the same identity."""
    first = await cache.store("result = 1", "desc", [], "hash", input_schema={"type": "object", "required": []})
    second = await cache.store("result = 1", "desc", [], "hash", input_schema={"required": [], "type": "object"})
    assert first == second


async def test_multiline_literal_whitespace_does_not_collide(cache: CacheStore) -> None:
    """Blank lines and trailing literal spaces are never stripped from identity."""
    values = ["result = '''a\nb'''", "result = '''a \nb'''", "result = '''a\n\nb'''"]
    ids = [await cache.store(code, "literal", [], "hash") for code in values]
    assert len(set(ids)) == len(values)


async def test_concurrent_duplicate_stores_are_atomic(cache: CacheStore) -> None:
    """The pooled connection serializes duplicate upserts without losing counts."""
    ids = await asyncio.gather(*(cache.store("result = 1", "desc", [], "hash") for _ in range(25)))
    entry = await cache.get(ids[0])
    assert entry is not None and entry.use_count == 25


async def test_cache_pools_connection_until_close(cache: CacheStore) -> None:
    """No operation opens a fresh connection after initialization."""
    with patch("aiosqlite.connect", side_effect=AssertionError("unexpected connection")):
        await cache.initialize()
        entry_id = await cache.store("result = 1", "desc", [], "hash")
        assert await cache.get(entry_id) is not None
        await cache.search()
        await cache.cleanup_expired()
        await cache.invalidate_by_swagger_hash("hash")
    await cache.close()
    await cache.close()
    with pytest.raises(CacheError, match="not initialized"):
        await cache.get(entry_id)


async def test_cache_reopens_persisted_recipe(cache: CacheStore) -> None:
    """Committed recipes survive a pooled connection restart."""
    entry_id = await cache.store("result = 1", "desc", [], "hash")
    await cache.close()
    await cache.initialize()
    assert await cache.get(entry_id) is not None


async def test_legacy_cache_is_migrated_without_unsafe_identity(tmp_path: Path) -> None:
    """Legacy code-only keys are invalidated, not silently mapped to new recipes."""
    path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript("""CREATE TABLE code_cache (
            id TEXT PRIMARY KEY, description TEXT, code TEXT, servers_used TEXT,
            swagger_hash TEXT, created_at REAL, last_used_at REAL, use_count INTEGER,
            ttl_seconds INTEGER);
            INSERT INTO code_cache VALUES ('legacy', 'desc', 'result = 1', '[]', 'hash', 1, 1, 1, 9999999999);
        """)
    store = CacheStore(path)
    await store.initialize()
    try:
        assert await store.get("legacy") is None
        entry_id = await store.store("result = 1", "desc", [], "hash", owner="alice")
        entry = await store.get(entry_id, owner="alice")
        assert entry is not None and entry.owner == "alice"
    finally:
        await store.close()


async def test_search_limit_never_becomes_unbounded(cache: CacheStore) -> None:
    """Negative SQLite limits cannot bypass the public result bound."""
    await cache.store("result = 1", "desc", [], "hash")
    assert await cache.search(limit=-1) == []


async def test_invalid_schema_is_sanitized(cache: CacheStore) -> None:
    """Non-JSON schemas are rejected without including their values."""
    with pytest.raises(InputValidationError, match="^Invalid recipe metadata$"):
        await cache.store("result = 1", "desc", [], "hash", input_schema={"bad": object()})


@pytest.mark.parametrize("ttl,capacity", [(0, 1), (1, 0)])
def test_cache_invalid_retention_rejected(tmp_path: Path, ttl: int, capacity: int) -> None:
    """Retention cannot configure an unbounded or immediately invalid store."""
    with pytest.raises(InputValidationError, match="retention"):
        CacheStore(str(tmp_path / "invalid.db"), ttl_seconds=ttl, max_entries=capacity)


async def test_cache_rollback_failure_is_sanitized(cache: CacheStore) -> None:
    """Rollback failures never expose driver diagnostics or database content."""
    assert cache._db is not None
    with (
        patch.object(cache._db, "rollback", new=AsyncMock(side_effect=aiosqlite.Error("private detail"))),
        pytest.raises(CacheError, match="^Failed to search cache$"),
    ):
        await cache.search()
