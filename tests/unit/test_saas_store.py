"""Isolated control-plane persistence, authorization, quotas, and transaction tests."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from gryphon.errors import SaaSDisabledError, SaaSNotFoundError, SaaSQuotaError, SaaSStoreError, SaaSValidationError
from gryphon.saas_database import SaaSDatabase
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SaaSStore]:
    """Yield an initialized private SQLite control plane and always close it."""
    instance = SaaSStore(f"sqlite:///{tmp_path / 'saas.db'}")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


async def test_tenant_create_list_disable_retains_metadata(store: SaaSStore) -> None:
    """Administrative metadata survives disabling without widening authentication."""
    tenant = await store.create_tenant("alpha")
    assert await store.get_tenant(tenant.id) == tenant
    assert await store.list_tenants() == [tenant]
    disabled = await store.disable_tenant(tenant.id)
    assert not disabled.enabled
    assert await store.list_tenants(limit=1) == [disabled]
    assert await store.list_tenants(limit=1, offset=1) == []


async def test_specs_canonical_json_roundtrip_and_tenant_isolation(store: SaaSStore) -> None:
    """Canonical hashing is stable and foreign tenant reads remain unavailable."""
    tenant = await store.create_tenant("alpha")
    other = await store.create_tenant("beta")
    first = await store.create_spec(tenant.id, "api", {"z": 1, "a": {"b": 2}})
    second = await store.create_spec(tenant.id, "api-v2", {"a": {"b": 2}, "z": 1})
    assert first.sha256 == second.sha256
    assert await store.get_spec(tenant.id, first.id) == first
    assert len(await store.list_specs(tenant.id)) == 2
    assert await store.list_specs(other.id) == []
    with pytest.raises(SaaSNotFoundError):
        await store.get_spec(other.id, first.id)


async def test_channel_configuration_revision_and_bound_specs(store: SaaSStore) -> None:
    """Configuration changes update the revision and preserve server-owned identity."""
    tenant = await store.create_tenant("alpha")
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "one", spec_ids=[spec.id])
    assert not channel.key_active
    assert await store.get_channel(tenant.id, channel.id) == channel
    updated = await store.update_channel(
        tenant.id, channel.id, name="two", sandbox_mode="docker", allowed_imports=["math"], spec_ids=[]
    )
    assert updated.revision == 2
    assert updated.id == channel.id
    assert updated.spec_ids == []
    assert updated.allowed_imports == ["math"]
    assert updated.sandbox_mode == "docker"
    assert await store.list_channels(tenant.id) == [updated]


async def test_channel_cross_tenant_binding_rolls_back_create_and_update(store: SaaSStore) -> None:
    """Invalid ownership never leaves a partial channel or changes existing configuration."""
    tenant = await store.create_tenant("alpha")
    other = await store.create_tenant("beta")
    spec = await store.create_spec(other.id, "foreign", {})
    with pytest.raises(SaaSNotFoundError):
        await store.create_channel(tenant.id, "invalid", spec_ids=[spec.id])
    assert await store.list_channels(tenant.id) == []
    channel = await store.create_channel(tenant.id, "valid")
    with pytest.raises(SaaSNotFoundError):
        await store.update_channel(tenant.id, channel.id, spec_ids=[spec.id])
    assert await store.get_channel(tenant.id, channel.id) == channel
    with pytest.raises(SaaSNotFoundError):
        await store.get_channel(other.id, channel.id)
    assert await store.list_channels(other.id) == []


async def test_channel_invalid_bindings_imports_and_mode_refused(store: SaaSStore) -> None:
    """Store validation rejects malformed bounded configuration independently of the API."""
    tenant = await store.create_tenant("alpha")
    spec = await store.create_spec(tenant.id, "api", {})
    with pytest.raises(SaaSValidationError):
        await store.create_channel(tenant.id, "duplicate", spec_ids=[spec.id, spec.id])
    with pytest.raises(SaaSValidationError):
        await store.create_channel(tenant.id, "invalid", allowed_imports=["../../escape"])
    with pytest.raises(ValidationError):
        await store.create_channel(tenant.id, "oversized", allowed_imports=["math"] * 101)
    assert await store.list_channels(tenant.id) == []


async def test_keys_rotation_revocation_and_digest_only_storage(store: SaaSStore) -> None:
    """Rotation invalidates immediately and SQL contains only the SHA256 key digest."""
    tenant = await store.create_tenant("alpha")
    channel = await store.create_channel(tenant.id, "one")
    first = await store.rotate_key(tenant.id, channel.id)
    resolved = await store.lookup_key(first)
    assert resolved is not None and resolved[0] == tenant and resolved[1].id == channel.id
    digest = hashlib.sha256(first.encode()).hexdigest()
    assert await store.lookup_key_digest(digest) == resolved
    async with store._db.transaction():
        rows = await store._db.execute("SELECT key_digest,payload FROM saas_channels")
    assert rows[0]["key_digest"] == digest
    assert first not in str(rows)
    second = await store.rotate_key(tenant.id, channel.id)
    assert (await store.get_channel(tenant.id, channel.id)).revision == 3
    assert await store.lookup_key(first) is None
    assert await store.lookup_key(second) is not None
    await store.revoke_key(tenant.id, channel.id)
    assert await store.lookup_key(second) is None
    assert not (await store.get_channel(tenant.id, channel.id)).key_active
    assert await store.lookup_key("short") is None
    assert await store.lookup_key("x" * 257) is None


async def test_disabled_channel_and_tenant_refuse_authentication(store: SaaSStore) -> None:
    """Every lookup rechecks both enabled flags and disabled tenants reject writes."""
    tenant = await store.create_tenant("alpha")
    channel = await store.create_channel(tenant.id, "one")
    token = await store.rotate_key(tenant.id, channel.id)
    await store.update_channel(tenant.id, channel.id, enabled=False)
    assert await store.lookup_key(token) is None
    await store.update_channel(tenant.id, channel.id, enabled=True)
    assert await store.lookup_key(token) is not None
    await store.disable_tenant(tenant.id)
    assert await store.lookup_key(token) is None
    with pytest.raises(SaaSDisabledError):
        await store.create_spec(tenant.id, "blocked", {})
    with pytest.raises(SaaSDisabledError):
        await store.create_channel(tenant.id, "blocked")
    with pytest.raises(SaaSDisabledError):
        await store.update_channel(tenant.id, channel.id, name="blocked")
    with pytest.raises(SaaSDisabledError):
        await store.rotate_key(tenant.id, channel.id)
    await store.revoke_key(tenant.id, channel.id)


async def test_resource_quotas_are_transactional_and_count_disabled_records(tmp_path: Path) -> None:
    """Concurrent creation cannot bypass quotas, including through another connection."""
    url = f"sqlite:///{tmp_path / 'quota.db'}"
    first = SaaSStore(url, max_tenants=1, max_channels_per_tenant=1, max_specs_per_tenant=1)
    second = SaaSStore(url, max_tenants=1, max_channels_per_tenant=1, max_specs_per_tenant=1)
    await first.initialize()
    await second.initialize()
    try:
        results = await asyncio.gather(first.create_tenant("a"), second.create_tenant("b"), return_exceptions=True)
        assert sum(isinstance(result, SaaSQuotaError) for result in results) == 1
        tenant = (await first.list_tenants())[0]
        await first.create_channel(tenant.id, "first")
        with pytest.raises(SaaSQuotaError):
            await second.create_channel(tenant.id, "overflow")
        await first.create_spec(tenant.id, "first", {})
        with pytest.raises(SaaSQuotaError):
            await second.create_spec(tenant.id, "overflow", {})
        await first.disable_tenant(tenant.id)
        with pytest.raises(SaaSQuotaError):
            await first.create_tenant("overflow")
    finally:
        await first.close()
        await second.close()


async def test_store_spec_bytes_and_listing_limits_reject_unbounded_inputs(store: SaaSStore) -> None:
    """Invalid JSON and all bounded-list dimensions are rejected before storage grows."""
    tenant = await store.create_tenant("alpha")
    with pytest.raises(SaaSQuotaError):
        await store.create_spec(tenant.id, "large", {"payload": "x" * 2097152})
    with pytest.raises(SaaSValidationError):
        await store.create_spec(tenant.id, "nan", {"value": float("nan")})
    with pytest.raises(SaaSValidationError):
        await store.create_spec(tenant.id, "object", {"value": object()})
    for limit, offset in [(0, 0), (101, 0), (1, -1), (1, 10001)]:
        with pytest.raises(SaaSQuotaError):
            await store.list_tenants(limit=limit, offset=offset)
    with pytest.raises(SaaSValidationError):
        SaaSStore("sqlite:///:memory:", max_tenants=0)


async def test_usage_aggregation_allowlisted_dimensions_and_tenant_isolation(store: SaaSStore) -> None:
    """Usage records aggregate only calls and latency under fixed tool/status dimensions."""
    tenant = await store.create_tenant("alpha")
    other = await store.create_tenant("beta")
    channel = await store.create_channel(tenant.id, "one")
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 1.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 2.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "error", 3)
    usage = await store.list_usage(tenant.id, channel.id)
    assert len(usage) == 2
    assert usage[1].calls == 2 and usage[1].latency_ms == 4
    assert await store.list_usage(tenant.id) == usage
    assert await store.list_usage(other.id, channel.id) == []
    with pytest.raises(SaaSNotFoundError):
        await store.record_usage(other.id, channel.id, "execute_code", "success", 1)
    for tool, latency in [("secret-text", 1), ("get_run", -1), ("get_run", float("inf"))]:
        with pytest.raises(SaaSValidationError):
            await store.record_usage(tenant.id, channel.id, tool, "success", latency)


async def test_audit_static_events_are_bounded_and_do_not_include_names(tmp_path: Path) -> None:
    """Bounded audit history contains identifiers and static events, not user metadata."""
    store = SaaSStore(f"sqlite:///{tmp_path / 'audit.db'}", max_audit_entries=2)
    await store.initialize()
    try:
        tenant = await store.create_tenant("private-tenant-name")
        channel = await store.create_channel(tenant.id, "private-channel-name")
        await store.rotate_key(tenant.id, channel.id)
        await store.revoke_key(tenant.id, channel.id)
        events = await store.list_audit(tenant.id)
        assert [event.event for event in events] == ["key_revoked", "key_rotated"]
        assert "private" not in str(events)
    finally:
        await store.close()


async def test_database_foreign_keys_and_cancellation_rollback(store: SaaSStore) -> None:
    """Foreign keys reject orphan rows and cancellation does not poison the connection."""
    with pytest.raises(SaaSStoreError, match="database operation failed"):
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_specs VALUES (?,?,?)", ("absent", "id", "{}"))
    with pytest.raises(asyncio.CancelledError):
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_tenants VALUES (?,?,?)", ("temporary", 1, "{}"))
            raise asyncio.CancelledError
    assert await store.list_tenants() == []
    assert (await store.create_tenant("healthy")).enabled


async def test_database_initialization_cleanup_and_closed_access(tmp_path: Path) -> None:
    """Invalid URLs and failed opens surface static errors and close can be repeated."""
    invalid = SaaSStore("unsupported://database")
    with pytest.raises(SaaSValidationError):
        await invalid.initialize()
    with pytest.raises(SaaSStoreError, match="not initialized"):
        await invalid.list_tenants()
    missing = SaaSStore(f"sqlite:///{tmp_path / 'absent' / 'store.db'}")
    with pytest.raises(SaaSStoreError, match="initialization failed"):
        await missing.initialize()
    await missing.close()
    await invalid.close()
    with pytest.raises(SaaSStoreError, match="not initialized"):
        await invalid.acquire_host_lease()
    memory = SaaSStore("sqlite:///:memory:")
    await memory.initialize()
    await memory.acquire_host_lease()
    await memory.close()


async def test_database_reopen_preserves_control_plane_state(tmp_path: Path) -> None:
    """Closing and reopening retains resources and hashed authentication authority."""
    store = SaaSStore(f"sqlite:///{tmp_path / 'persistent.db'}")
    await store.initialize()
    tenant = await store.create_tenant("alpha")
    channel = await store.create_channel(tenant.id, "one")
    token = await store.rotate_key(tenant.id, channel.id)
    await store.initialize()
    await store.close()
    await store.initialize()
    try:
        assert await store.lookup_key(token) is not None
    finally:
        await store.close()


async def test_database_postgres_parameters_transaction_and_cleanup() -> None:
    """The PostgreSQL adapter binds parameters and holds its transaction advisory lock."""
    cursor = MagicMock()
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    cursor.execute = AsyncMock()
    cursor.description = None
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.close = AsyncMock()
    with patch("gryphon.saas_database.psycopg.AsyncConnection.connect", new=AsyncMock(return_value=connection)):
        database = SaaSDatabase("postgresql://localhost/test")
        await database.initialize()
        cursor.description = ("column",)
        cursor.fetchall = AsyncMock(return_value=[{"value": "row"}])
        async with database.transaction():
            assert await database.execute("SELECT ? AS value", ("parameter",)) == [{"value": "row"}]
        assert any(call.args == ("SELECT %s AS value", ("parameter",)) for call in cursor.execute.call_args_list)
        assert any("pg_advisory_xact_lock(%s)" in call.args[0] for call in cursor.execute.call_args_list)
        await database.close()
        connection.close.assert_awaited_once()


async def test_audit_failure_rolls_back_key_rotation_and_resource_creation(store: SaaSStore) -> None:
    """Administrative mutations and their audit event always commit or fail together."""
    tenant = await store.create_tenant("alpha")
    channel = await store.create_channel(tenant.id, "one")
    token = await store.rotate_key(tenant.id, channel.id)
    previous = await store.get_channel(tenant.id, channel.id)
    with patch.object(store, "_audit", new=AsyncMock(side_effect=SaaSStoreError("Injected audit failure"))):
        with pytest.raises(SaaSStoreError):
            await store.rotate_key(tenant.id, channel.id)
        with pytest.raises(SaaSStoreError):
            await store.create_tenant("rolled-back")
    assert await store.get_channel(tenant.id, channel.id) == previous
    assert await store.lookup_key(token) is not None
    assert await store.list_tenants() == [tenant]


async def test_database_composite_foreign_key_refuses_cross_tenant_binding(store: SaaSStore) -> None:
    """SQL foreign keys independently enforce the same tenant scope as store validation."""
    tenant = await store.create_tenant("alpha")
    other = await store.create_tenant("beta")
    spec = await store.create_spec(other.id, "foreign", {})
    channel = await store.create_channel(tenant.id, "one")
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (tenant.id, channel.id, spec.id))
    assert (await store.get_channel(tenant.id, channel.id)).spec_ids == []


async def test_tenant_names_are_bound_parameters_not_sql(store: SaaSStore) -> None:
    """SQL-shaped tenant names remain inert metadata and cannot alter the schema."""
    name = "'); DROP TABLE saas_tenants; --"
    tenant = await store.create_tenant(name)
    assert (await store.get_tenant(tenant.id)).name == name
    assert (await store.create_tenant("second")).enabled


async def test_tenant_reenable_advances_all_owned_channels_atomically(store: SaaSStore) -> None:
    """Tenant toggles supersede runtime tombstones, including for individually disabled channels."""
    tenant = await store.create_tenant("alpha")
    other = await store.create_tenant("beta")
    first = await store.create_channel(tenant.id, "one")
    second = await store.create_channel(tenant.id, "two")
    foreign = await store.create_channel(other.id, "foreign")
    second = await store.update_channel(tenant.id, second.id, enabled=False)
    token = await store.rotate_key(tenant.id, first.id)
    first = await store.get_channel(tenant.id, first.id)
    await store.disable_tenant(tenant.id)
    assert await store.lookup_key(token) is None
    assert (await store.set_tenant_enabled(tenant.id, True)).enabled
    assert (await store.get_channel(tenant.id, first.id)).revision == first.revision + 2
    assert (await store.get_channel(tenant.id, second.id)).revision == second.revision + 2
    assert not (await store.get_channel(tenant.id, second.id)).enabled
    assert await store.get_channel(other.id, foreign.id) == foreign
    assert await store.lookup_key(token) is not None
    with (
        patch.object(store, "_audit", new=AsyncMock(side_effect=SaaSStoreError("Injected audit failure"))),
        pytest.raises(SaaSStoreError),
    ):
        await store.set_tenant_enabled(tenant.id, False)
    assert (await store.get_tenant(tenant.id)).enabled
    assert (await store.get_channel(tenant.id, first.id)).revision == first.revision + 2
    assert (await store.list_audit(tenant.id))[0].event == "tenant_enabled"


async def test_host_lease_duplicate_sqlite_owner_refused_then_released(tmp_path: Path) -> None:
    """SQLite hosting uses the actual database path and releases ownership on close."""
    path = tmp_path / "host.db"
    first = SaaSStore(f"sqlite:///{path}")
    second = SaaSStore(f"sqlite:///{tmp_path / 'host-link.db'}")
    try:
        await first.initialize()
        (tmp_path / "host-link.db").symlink_to(path)
        await second.initialize()
        await first.acquire_host_lease()
        await first.acquire_host_lease()
        with pytest.raises(SaaSStoreError, match="already owned"):
            await second.acquire_host_lease()
        await first.close()
        await second.initialize()
        await second.acquire_host_lease()
        await second.close()
        await first.initialize()
        acquire = first._db._acquire_host_lease

        async def interrupted() -> None:
            """Cancel immediately after actual acquisition to exercise registered rollback."""
            await acquire()
            raise asyncio.CancelledError

        with patch.object(first._db, "_acquire_host_lease", new=interrupted), pytest.raises(asyncio.CancelledError):
            await first.acquire_host_lease()
        await second.initialize()
        await second.acquire_host_lease()
    finally:
        await first.close()
        await second.close()
