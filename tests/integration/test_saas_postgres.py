"""Opt-in real PostgreSQL workflow using only a newly created disposable test container."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING
from uuid import uuid4

import aiodocker
import pytest
from aiodocker.exceptions import DockerError
from pydantic import SecretStr

from gryphon.errors import SaaSNotFoundError, SaaSQuotaError, SaaSStoreError
from gryphon.models import AuditActor, Tenant
from gryphon.saas_analytics_schema import empty_bucket, encode_bucket
from gryphon.saas_audit import audit_actor
from gryphon.saas_audit_archive import prune_audit
from gryphon.saas_store import SaaSStore
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.models import Channel, UserAccount
    from gryphon.saas_database import SQLRow

POSTGRES_IMAGE = "postgres:17.6"
STARTUP_ATTEMPTS = 120
STARTUP_DELAY = 0.5
IMAGE_PULL_TIMEOUT = 300


async def _ensure_postgres_image(docker: aiodocker.Docker) -> None:
    """Reuse the exact pinned image or bound the initial public-registry pull."""
    try:
        await docker.images.inspect(POSTGRES_IMAGE)
    except DockerError as exc:
        if exc.status != 404:
            raise
        async with asyncio.timeout(IMAGE_PULL_TIMEOUT):
            await docker.images.pull(POSTGRES_IMAGE)


async def _wait_for_postgres(url: SecretStr) -> None:
    """Wait for schema initialization without emitting credentials or backend exception details."""
    for _ in range(STARTUP_ATTEMPTS):
        store = SaaSStore(url.get_secret_value())
        try:
            await store.initialize()
            return
        except SaaSStoreError:
            await asyncio.sleep(STARTUP_DELAY)
        finally:
            await store.close()
    pytest.fail("Disposable PostgreSQL did not become ready", pytrace=False)


@pytest.fixture
async def postgres_url() -> AsyncIterator[SecretStr]:
    """Create a unique loopback-only PostgreSQL container on tmpfs and always remove it."""
    if os.environ.get("GRYPHON_TEST_POSTGRES") != "1":
        pytest.skip("Set GRYPHON_TEST_POSTGRES=1 to run disposable PostgreSQL tests")
    async with aiodocker.Docker() as docker:
        await _ensure_postgres_image(docker)
        password = secrets.token_urlsafe(48)
        container = await docker.containers.create(
            name=f"gryphon-postgres-test-{uuid4().hex}",
            config={
                "Image": POSTGRES_IMAGE,
                "Env": [f"POSTGRES_PASSWORD={password}"],
                "ExposedPorts": {"5432/tcp": {}},
                "HostConfig": {
                    "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "0"}]},
                    "Tmpfs": {"/var/lib/postgresql/data": "rw,size=536870912"},
                },
            },
        )
        try:
            await container.start()
            info = await container.show()
            binding = info["NetworkSettings"]["Ports"]["5432/tcp"][0]
            assert binding["HostIp"] == "127.0.0.1"
            assert all(mount["Type"] != "volume" for mount in info["Mounts"])
            url = SecretStr(f"postgresql://postgres:{password}@127.0.0.1:{binding['HostPort']}/postgres")
            await _wait_for_postgres(url)
            yield url
        finally:
            await container.delete(force=True, v=True)


async def _check_isolation(store: SaaSStore, tenant: Tenant, other: Tenant) -> Channel:
    """Verify real composite foreign keys, tenant-scoped queries, and canonical specification storage."""
    spec = await store.create_spec(tenant.id, "api", {"paths": {}, "openapi": "3.1.0"})
    foreign = await store.create_spec(other.id, "foreign", {})
    with pytest.raises(SaaSNotFoundError):
        await store.create_channel(tenant.id, "invalid", spec_ids=[foreign.id])
    assert await store.list_channels(tenant.id) == []
    channel = await store.create_channel(tenant.id, "one", spec_ids=[spec.id])
    assert await store.get_spec(tenant.id, spec.id) == spec
    assert await store.list_specs(tenant.id) == [spec]
    assert await store.list_channels(tenant.id) == [channel]
    with pytest.raises(SaaSNotFoundError):
        await store.get_channel(other.id, channel.id)
    with pytest.raises(SaaSNotFoundError):
        await store.get_spec(other.id, spec.id)
    with pytest.raises(SaaSNotFoundError):
        await store.update_channel(tenant.id, channel.id, spec_ids=[foreign.id])
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (tenant.id, channel.id, foreign.id))
    assert await store.get_channel(tenant.id, channel.id) == channel
    return channel


async def _check_lifecycle(store: SaaSStore, tenant: Tenant, channel: Channel) -> None:
    """Exercise digest-only keys, revisioned configuration, disable/reenable, and revocation."""
    first = await store.rotate_key(tenant.id, channel.id)
    assert await store.lookup_key(first) is not None
    async with store._db.transaction():
        rows = await store._db.execute("SELECT key_digest,payload FROM saas_channels WHERE id=?", (channel.id,))
    assert rows[0]["key_digest"] == hashlib.sha256(first.encode()).hexdigest()
    assert first not in str(rows)
    second = await store.rotate_key(tenant.id, channel.id)
    assert await store.lookup_key(first) is None
    assert await store.lookup_key(second) is not None
    updated = await store.update_channel(tenant.id, channel.id, sandbox_mode="docker", allowed_imports=["math"])
    assert updated.revision > channel.revision and updated.sandbox_mode == "docker"
    await store.disable_tenant(tenant.id)
    assert await store.lookup_key(second) is None
    await store.set_tenant_enabled(tenant.id, True)
    assert (await store.get_channel(tenant.id, channel.id)).revision == updated.revision + 2
    assert await store.lookup_key(second) is not None
    await store.update_channel(tenant.id, channel.id, enabled=False)
    assert await store.lookup_key(second) is None
    await store.update_channel(tenant.id, channel.id, enabled=True)
    await store.revoke_key(tenant.id, channel.id)
    assert await store.lookup_key(second) is None


async def _check_quotas_and_telemetry(store: SaaSStore, tenant: Tenant, channel: Channel, other: Tenant) -> None:
    """Verify quotas and real PostgreSQL upserts preserve tenant-isolated safe aggregate telemetry."""
    with pytest.raises(SaaSQuotaError):
        await store.create_tenant("overflow")
    with pytest.raises(SaaSQuotaError):
        await store.create_channel(tenant.id, "overflow")
    with pytest.raises(SaaSQuotaError):
        await store.create_spec(tenant.id, "overflow", {})
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 1.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 2.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "error", 3)
    usage = await store.list_usage(tenant.id, channel.id)
    assert len(usage) == 2 and usage[1].calls == 2 and usage[1].latency_ms == 4
    assert await store.list_usage(other.id, channel.id) == []
    audit = await store.list_audit(tenant.id)
    assert all(event.tenant_id == tenant.id for event in audit)
    assert {"tenant_enabled", "key_rotated", "key_revoked"}.issubset({event.event for event in audit})


async def test_postgres_control_plane_workflow_and_exclusive_host_lease(postgres_url: SecretStr) -> None:
    """Validate PostgreSQL behavior end to end and refuse simultaneous hosted workers."""
    first = SaaSStore(postgres_url.get_secret_value(), max_tenants=2, max_channels_per_tenant=1, max_specs_per_tenant=1)
    second = SaaSStore(postgres_url.get_secret_value(), max_tenants=2)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(first.close)
        cleanup.push_async_callback(second.close)
        await first.initialize()
        await second.initialize()
        await first.acquire_host_lease()
        await first.acquire_host_lease()
        with pytest.raises(SaaSStoreError, match="already owned"):
            await second.acquire_host_lease()
        await second.initialize()
        async with first._db.transaction():
            rows = await first._db.execute("SELECT current_setting('server_version_num') AS version")
        assert rows[0]["version"] == "170006"
        tenant = await first.create_tenant("alpha")
        results = await asyncio.gather(
            first.create_tenant("beta"), second.create_tenant("beta-race"), return_exceptions=True
        )
        assert sum(isinstance(result, SaaSQuotaError) for result in results) == 1
        other = next(result for result in results if isinstance(result, Tenant))
        channel = await _check_isolation(first, tenant, other)
        await _check_lifecycle(first, tenant, channel)
        await _check_quotas_and_telemetry(first, tenant, channel, other)
        await first.close()
        await second.acquire_host_lease()
        assert await second.get_tenant(tenant.id) == tenant
        assert (await second.get_channel(tenant.id, channel.id)).id == channel.id


async def _check_user_retention(store: SaaSStore, users: UserStore, user: UserAccount) -> None:
    """Exercise real batched writes and ordinary 10,000-entry pruning across both user audit tables."""
    async with store._db.transaction():
        await store._db.executemany(
            "INSERT INTO saas_user_audit VALUES (?,?,?,?,?,?)",
            [(str(uuid4()), "bootstrap", user.id, user.tenant_id, "user_updated", 0.0) for _ in range(10000)],
        )
    await users.update_user(user.id, name="Retained replacement", actor_id="bootstrap")
    async with store._db.transaction():
        counts = await store._db.execute(
            "SELECT COUNT(*) AS total FROM (SELECT id FROM saas_user_audit UNION ALL "
            "SELECT id FROM saas_audit_archive WHERE category='user') AS events"
        )
        assert counts == [{"total": 10000}]
        await prune_audit(store._db, "user", 2)
    expected = (await users.list_audit())[:2]
    assert len(expected) == 2 and expected[0].event == "user_updated"
    await store.close()
    await store.initialize()
    assert await users.list_audit() == expected


async def test_postgres_user_delete_archive_scopes_snapshots_and_username_reuse(postgres_url: SecretStr) -> None:
    """Physically delete under live FKs, merge scoped archived/live events, and reconnect durably."""
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        users, password = UserStore(store._db), secrets.token_urlsafe(24)
        tenant, other = await store.create_tenant("Deleted account"), await store.create_tenant("Other")
        actor = await users.create_user("admin", "Original administrator", password, "platform_admin")
        user = await users.create_user("member", "Deleted display", password, "tenant_user", tenant.id, actor.id)
        outsider = await users.create_user("outsider", "Outside", password, "tenant_user", other.id)
        with pytest.raises(SaaSStoreError):
            async with store._db.transaction():
                await store._db.execute("DELETE FROM saas_users WHERE id=?", (user.id,))
        snapshot = AuditActor(
            id=actor.id, username=actor.username, name=actor.name, kind="user", display_source="snapshot"
        )
        preview = await users.preview_user_deletion(user.id, actor.id)
        with audit_actor(snapshot):
            assert await users.delete_user(user.id, user.username, str(preview["confirmation_token"]), actor.id) == user
        assert await users.get_user(user.id) is None and await users.authenticate(user.username, password) is None
        replacement = await users.create_user(user.username, "Replacement", password, "tenant_user", tenant.id)
        assert replacement.id != user.id and await users.get_user(outsider.id) == outsider
        await users.update_user(actor.id, name="Current administrator", actor_id="bootstrap")
        events = await users.list_audit(tenant.id)
        assert [event.event for event in events] == ["user_created", "user_deleted", "user_created"]
        assert events[1].actor == snapshot and events[1].subject_name == user.name
        assert events[1].subject_username == user.username and events[1].subject_id == user.id
        assert events[2].subject_name is None and events[2].actor.name == "Current administrator"
        assert events[2].actor.display_source == "current"
        assert await users.list_audit(tenant.id, limit=1, offset=1) == events[1:2]
        assert [event.subject_id for event in await users.list_audit(other.id)] == [outsider.id]
        async with store._db.transaction():
            rows = await store._db.execute("SELECT payload FROM saas_audit_archive WHERE category='user'")
            assert len(rows) == 2 and all("password_hash" not in str(row) and password not in str(row) for row in rows)
        await store.close()
        await store.initialize()
        assert await users.list_audit(tenant.id) == events and await users.get_user(user.id) is None
        await _check_user_retention(store, users, replacement)


async def _seed_channel_dependencies(store: SaaSStore, channel: Channel) -> None:
    """Populate each channel foreign-key dependency with synthetic, content-free telemetry."""
    await store.record_usage(channel.tenant_id, channel.id, "get_run", "success", 1)
    async with store._db.transaction():
        await store._db.execute(
            "INSERT INTO saas_analytics_daily VALUES (?,?,?,?)",
            (channel.tenant_id, channel.id, "2026-01-01", encode_bucket(empty_bucket())),
        )
        await store._db.execute(
            "INSERT INTO saas_analytics_receipts VALUES (?,?,?,'run',?,1,?)",
            (str(uuid4()), channel.tenant_id, channel.id, "a" * 64, "2026-01-01"),
        )
        await store._db.execute("INSERT INTO saas_analytics_metadata VALUES (?,?,1)", (channel.tenant_id, channel.id))


async def _workspace_rows(store: SaaSStore, tenant_id: str) -> list[list[SQLRow]]:
    """Snapshot all tenant-owned live tables without weakening PostgreSQL referential checks."""
    tables = (
        "users",
        "specs",
        "channels",
        "bindings",
        "usage",
        "analytics_daily",
        "analytics_receipts",
        "analytics_metadata",
        "audit",
        "user_audit",
    )
    async with store._db.transaction():
        return [
            await store._db.execute(f"SELECT * FROM saas_{table} WHERE tenant_id=? ORDER BY 1,2", (tenant_id,))
            for table in tables
        ]


async def _check_workspace_archive(store: SaaSStore, tenant: Tenant, other: Tenant, user: UserAccount) -> None:
    """Verify category-isolated public history, active FK triggers, bounded retention, and reconnect."""
    users = UserStore(store._db)
    events, account_events = await store.list_audit(tenant.id), await users.list_audit(tenant.id)
    assert events[0].event == "tenant_deleted" and events[0].actor.kind == "bootstrap"
    assert events == await store.list_deleted_tenant_audit()
    assert all(event.tenant_id == tenant.id for event in events)
    assert {event.event for event in account_events} == {"user_created", "user_deleted"}
    assert all(event.subject_id == user.id for event in account_events)
    assert account_events[0].actor.display_source == "snapshot" and account_events[0].subject_name == user.name
    async with store._db.transaction():
        assert await store._db.execute("SELECT id FROM saas_tenants WHERE id=?", (tenant.id,)) == []
        assert (
            await store._db.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid='saas_audit_archive'::regclass AND contype='f'"
            )
            == []
        )
        constraints = await store._db.execute(
            "SELECT convalidated FROM pg_constraint WHERE conrelid='saas_user_audit'::regclass AND contype='f'"
        )
        assert len(constraints) == 2 and all(row["convalidated"] is True for row in constraints)
        assert await store._db.execute("SELECT tgname FROM pg_trigger WHERE tgconstraint != 0 AND tgenabled='D'") == []
        await prune_audit(store._db, "resource", 2)
    store._max_audit = 2
    await store.create_channel(other.id, "After archive pruning")
    assert len(await store.list_deleted_tenant_audit()) == 1
    await store.close()
    await store.initialize()
    assert len(await store.list_deleted_tenant_audit()) == 1
    assert await users.list_audit(tenant.id) == account_events
    assert await users.get_user(user.id) is None and await store.get_tenant(other.id) == other
    with pytest.raises(SaaSNotFoundError):
        await store.get_tenant(tenant.id)


async def test_postgres_channel_and_workspace_physical_deletion(postgres_url: SecretStr) -> None:
    """Delete channel dependencies first, then entire assigned workspace while foreign rows remain intact."""
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        tenant, other = await store.create_tenant("Workspace"), await store.create_tenant("Unrelated")
        spec, foreign_spec = (
            await store.create_spec(tenant.id, "API", {}),
            await store.create_spec(other.id, "Other", {}),
        )
        target = await store.create_channel(tenant.id, "Standalone", spec_ids=[spec.id])
        remaining = await store.create_channel(tenant.id, "Workspace channel", spec_ids=[spec.id])
        foreign = await store.create_channel(other.id, "Foreign channel", spec_ids=[foreign_spec.id])
        users, password = UserStore(store._db), secrets.token_urlsafe(24)
        user = await users.create_user("member", "Assigned member", password, "tenant_user", tenant.id)
        outsider = await users.create_user("outsider", "Foreign member", password, "tenant_user", other.id)
        key, foreign_key = await store.rotate_key(tenant.id, remaining.id), await store.rotate_key(other.id, foreign.id)
        for channel in (target, remaining, foreign):
            await _seed_channel_dependencies(store, channel)
        with pytest.raises(SaaSStoreError):
            async with store._db.transaction():
                await store._db.execute("DELETE FROM saas_channels WHERE id=?", (target.id,))
        preview = await store.preview_channel_deletion(tenant.id, target.id)
        await store.delete_channel(tenant.id, target.id, target.name, preview["confirmation_token"])
        assert await store.list_specs(tenant.id) == [spec]
        assert [channel.id for channel in await store.list_channels(tenant.id)] == [remaining.id]
        async with store._db.transaction():
            for table in ("bindings", "usage", "analytics_daily", "analytics_receipts", "analytics_metadata"):
                assert (
                    await store._db.execute(f"SELECT channel_id FROM saas_{table} WHERE channel_id=?", (target.id,))
                    == []
                )
        before = await _workspace_rows(store, other.id)
        preview = await store.preview_tenant_deletion(tenant.id)
        assert preview["impact"] == {"users": 1, "channels": 1, "specs": 1}
        with audit_actor(AuditActor(id="bootstrap", kind="bootstrap", display_source="snapshot")):
            _, channels, members = await store.delete_tenant(
                tenant.id, tenant.name, preview["confirmation_token"], "bootstrap"
            )
        assert [channel.id for channel in channels] == [remaining.id] and members == [user]
        assert not any(await _workspace_rows(store, tenant.id))
        assert await _workspace_rows(store, other.id) == before
        assert await users.get_user(user.id) is None and await users.get_user(outsider.id) == outsider
        assert await store.lookup_key(key) is None and await store.lookup_key(foreign_key) is not None
        await _check_workspace_archive(store, tenant, other, user)
