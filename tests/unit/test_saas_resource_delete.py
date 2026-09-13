"""Physical channel and workspace deletion against disposable control databases."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from test_saas_store import store as store

from gryphon.errors import ConflictError, SaaSNotFoundError, SaaSQuotaError, SaaSStoreError, SaaSValidationError
from gryphon.models import AuditActor, SpecImport
from gryphon.saas_audit import audit_actor
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gryphon.models import Channel
    from gryphon.saas_database import SQLRow, SQLValue
    from gryphon.saas_store import SaaSStore

TABLES = (
    "tenants",
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
    "audit_archive",
)


async def _snapshot(store: SaaSStore) -> list[object]:
    """Snapshot temporary SQL state to detect any mutation after rejection or rollback."""
    async with store._db.transaction():
        return [await store._db.execute(f"SELECT * FROM saas_{table} ORDER BY 1,2") for table in TABLES]


async def _traffic(store: SaaSStore, channel: Channel) -> None:
    """Create each channel FK dependency without network calls or local runtime files."""
    await store.record_usage(channel.tenant_id, channel.id, "get_run", "success", 1)
    async with store._db.transaction():
        await store._db.execute(
            "INSERT INTO saas_analytics_daily VALUES (?,?,?,?)", (channel.tenant_id, channel.id, "2026-01-01", "{}")
        )
        await store._db.execute(
            "INSERT INTO saas_analytics_receipts VALUES (?,?,?,'run',?,1,?)",
            (channel.id, channel.tenant_id, channel.id, "a" * 64, "2026-01-01"),
        )
        await store._db.execute("INSERT INTO saas_analytics_metadata VALUES (?,?,1)", (channel.tenant_id, channel.id))


async def test_channel_delete_exact_scope_frees_key_and_channel_quota(store: SaaSStore) -> None:
    """Delete one channel and dependencies, retaining specs, members, sibling channels and other tenants."""
    tenant = await store.create_tenant("same")
    foreign = await store.create_tenant("same")
    spec = await store.create_spec(tenant.id, "api", {})
    channels = [
        await store.create_channel(owner, "same", spec_ids=ids)
        for owner, ids in [(tenant.id, [spec.id]), (tenant.id, [spec.id]), (foreign.id, [])]
    ]
    target, sibling, other = channels
    user = await UserStore(store._db).create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    key = await store.rotate_key(tenant.id, target.id)
    target = await store.get_channel(tenant.id, target.id)
    preview = await store.preview_channel_deletion(tenant.id, target.id)
    for channel in channels:
        await _traffic(store, channel)
    assert await store.preview_channel_deletion(tenant.id, target.id) == preview
    assert preview["impact"] == {"channels": 1, "users": 0, "specs": 0}
    store._max_channels = 2
    with pytest.raises(SaaSQuotaError):
        await store.create_channel(tenant.id, "full")
    actor = AuditActor(id="bootstrap", kind="bootstrap", display_source="snapshot")
    with audit_actor(actor):
        assert await store.delete_channel(tenant.id, target.id, "same", preview["confirmation_token"]) == target
    assert await store.lookup_key(key) is None
    assert await store.lookup_key_digest(hashlib.sha256(key.encode()).hexdigest()) is None
    assert await store.get_channel(tenant.id, sibling.id) == sibling
    assert await store.get_channel(foreign.id, other.id) == other
    assert await store.list_specs(tenant.id) == [spec]
    assert await UserStore(store._db).get_user(user.id) == user
    assert (await store.create_channel(tenant.id, "free")).name == "free"
    events = await store.list_audit(tenant.id)
    deleted = next(item for item in events if item.event == "channel_deleted")
    assert deleted.channel_id == target.id and deleted.resource_name == "same" and deleted.actor == actor
    async with store._db.transaction():
        assert await store._db.execute("PRAGMA foreign_key_check") == []
        for table in TABLES[4:9]:
            rows = await store._db.execute(f"SELECT channel_id FROM saas_{table}")
            expected = {sibling.id} if table == "bindings" else {sibling.id, other.id}
            assert {row["channel_id"] for row in rows} == expected


@pytest.mark.parametrize("kind", ["tenant", "channel"])
@pytest.mark.parametrize("name", [None, True, 1, [], {}, "", " same ", "SAME", "same\n", "x" * 129])
async def test_resource_delete_malformed_name_is_noop(store: SaaSStore, kind: str, name: object) -> None:
    """Exact stored names are mandatory without trimming, coercion or case folding."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    preview = (
        await store.preview_tenant_deletion(tenant.id)
        if kind == "tenant"
        else await store.preview_channel_deletion(tenant.id, channel.id)
    )
    before = await _snapshot(store)
    with pytest.raises(SaaSValidationError):
        if kind == "tenant":
            await store.delete_tenant(tenant.id, name, preview["confirmation_token"], "bootstrap")
        else:
            await store.delete_channel(tenant.id, channel.id, name, preview["confirmation_token"])
    assert await _snapshot(store) == before


@pytest.mark.parametrize("kind", ["tenant", "channel"])
@pytest.mark.parametrize("token", [None, True, 42, [], {}, "", "x" * 64, "a" * 63, "a" * 65, "A" * 64, "a" * 64 + "\n"])
async def test_resource_delete_malformed_token_is_noop(store: SaaSStore, kind: str, token: object) -> None:
    """Tokens must be lowercase SHA256 strings, and malformed input never changes retained data."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    before = await _snapshot(store)
    with pytest.raises(SaaSValidationError):
        if kind == "tenant":
            await store.delete_tenant(tenant.id, "same", token, "bootstrap")
        else:
            await store.delete_channel(tenant.id, channel.id, "same", token)
    assert await _snapshot(store) == before


@pytest.mark.parametrize("change", ["user", "password", "spec", "channel", "config", "bindings", "key", "tenant"])
async def test_tenant_delete_stale_workspace_is_noop(store: SaaSStore, change: str) -> None:
    """All account revisions, immutable versions, channels/configuration and relational bindings enter CAS."""
    tenant = await store.create_tenant("same")
    users = UserStore(store._db)
    user = await users.create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "same")
    preview = await store.preview_tenant_deletion(tenant.id)
    if change == "user":
        await users.create_user("new-member", "New", "test-password-only", "tenant_user", tenant.id)
    elif change == "password":
        await users.reset_password(user.id, "changed-test-password", "bootstrap")
    elif change == "spec":
        await store.refresh_spec(tenant.id, spec.id, SpecImport(document={"v": 2}))
    elif change == "channel":
        await store.create_channel(tenant.id, "same")
    elif change == "config":
        channel.include_function_summaries = True
        async with store._db.transaction():
            await store._db.execute(
                "UPDATE saas_channels SET payload=? WHERE id=?", (channel.model_dump_json(), channel.id)
            )
    elif change == "bindings":
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (tenant.id, channel.id, spec.id))
    elif change == "key":
        await store.rotate_key(tenant.id, channel.id)
    else:
        await store.disable_tenant(tenant.id)
    before = await _snapshot(store)
    with pytest.raises(ConflictError):
        await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap")
    assert await _snapshot(store) == before


@pytest.mark.parametrize("change", ["config", "revision", "key", "bindings"])
async def test_channel_delete_stale_config_is_noop(store: SaaSStore, change: str) -> None:
    """Full public channel configuration and relational bindings are checked even without revision changes."""
    tenant = await store.create_tenant("same")
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "same")
    preview = await store.preview_channel_deletion(tenant.id, channel.id)
    if change == "config":
        channel.allowed_imports = ["math"]
        async with store._db.transaction():
            await store._db.execute(
                "UPDATE saas_channels SET payload=? WHERE id=?", (channel.model_dump_json(), channel.id)
            )
    elif change == "revision":
        await store.update_channel(tenant.id, channel.id)
    elif change == "key":
        await store.rotate_key(tenant.id, channel.id)
    else:
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (tenant.id, channel.id, spec.id))
    before = await _snapshot(store)
    with pytest.raises(ConflictError):
        await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    assert await _snapshot(store) == before


async def test_tenant_delete_entire_workspace_preserves_foreign_and_platform_accounts(store: SaaSStore) -> None:
    """Delete all versions/same-name roots and disabled members; never delete the platform actor or foreign rows."""
    tenant = await store.create_tenant("same")
    foreign = await store.create_tenant("same")
    users = UserStore(store._db)
    admin = await users.create_user("admin", "Admin", "test-password-only", "platform_admin")
    user = await users.create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    user = await users.update_user(user.id, enabled=False, actor_id=admin.id)
    outsider = await users.create_user("outsider", "Outside", "test-password-only", "tenant_user", foreign.id)
    root = await store.create_spec(tenant.id, "api", {})
    latest, _ = await store.refresh_spec(tenant.id, root.id, SpecImport(document={"v": 2}))
    unrelated = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "same", spec_ids=[root.id, latest.id, unrelated.id])
    other = await store.create_channel(foreign.id, "same")
    key = await store.rotate_key(tenant.id, channel.id)
    await _traffic(store, channel)
    await _traffic(store, other)
    store._max_list = 1
    preview = await store.preview_tenant_deletion(tenant.id)
    assert preview["impact"] == {"users": 1, "channels": 1, "specs": 3}
    assert preview["spec_ids"] == sorted([root.id, latest.id, unrelated.id])
    assert preview["user_ids"] == [user.id] and preview["channel_ids"] == [channel.id]
    await store.record_usage(tenant.id, channel.id, "get_run", "success", 1)
    async with store._db.transaction():
        await store._audit(tenant.id, "channel_updated", channel.id)
    assert await store.preview_tenant_deletion(tenant.id) == preview
    deleted, channels, members = await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], admin.id)
    assert deleted == tenant and [item.id for item in channels] == [channel.id] and members == [user]
    assert await store.lookup_key(key) is None and await users.get_user(user.id) is None
    assert await users.get_user(admin.id) == admin and await users.get_user(outsider.id) == outsider
    assert await store.get_channel(foreign.id, other.id) == other
    store._max_list = 100
    assert await store.list_tenants() == [foreign]
    assert await store.list_specs(tenant.id) == [] and await store.list_channels(tenant.id) == []
    events = await store.list_audit(tenant.id)
    assert events[0].event == "tenant_deleted" and events[0].resource_name == "same"
    assert await store.list_deleted_tenant_audit() == events
    assert all(item.tenant_id == tenant.id for item in events)
    async with store._db.transaction():
        assert await store._db.execute("PRAGMA foreign_key_check") == []
        assert await store._db.execute("SELECT id FROM saas_audit WHERE tenant_id=?", (tenant.id,)) == []
        for table in TABLES[5:9]:
            rows = await store._db.execute(f"SELECT channel_id FROM saas_{table}")
            assert {row["channel_id"] for row in rows} == {other.id}
    recreated = await users.create_user("member", "New", "test-password-only", "tenant_user", foreign.id)
    assert recreated.id != user.id


@pytest.mark.parametrize("kind", ["tenant", "channel"])
async def test_resource_delete_failure_restores_rows_and_pruned_audits(store: SaaSStore, kind: str) -> None:
    """Failure after real mutation, archive copying and pruning restores every dependent row and audit."""
    tenant = await store.create_tenant("same")
    await UserStore(store._db).create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    spec = await store.create_spec(tenant.id, "api", {})
    channel = await store.create_channel(tenant.id, "same", spec_ids=[spec.id])
    await store.rotate_key(tenant.id, channel.id)
    await _traffic(store, channel)
    preview = (
        await store.preview_tenant_deletion(tenant.id)
        if kind == "tenant"
        else await store.preview_channel_deletion(tenant.id, channel.id)
    )
    store._max_audit = 1
    before, execute = await _snapshot(store), store._db.execute

    async def fail(sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Fail only after actual final DELETE, so rollback must restore pruned and archived rows."""
        rows = await execute(sql, params)
        if sql.startswith("DELETE FROM saas_tenants" if kind == "tenant" else "DELETE FROM saas_channels"):
            raise SaaSStoreError("Synthetic deletion failure")
        return rows

    with patch.object(store._db, "execute", fail), pytest.raises(SaaSStoreError):
        if kind == "tenant":
            await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap")
        else:
            await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    assert await _snapshot(store) == before


async def test_resource_delete_missing_foreign_repeated_and_actor_validation(store: SaaSStore) -> None:
    """Missing/foreign identities are not found; trusted explicit actor arguments still require canonical form."""
    tenant = await store.create_tenant("same")
    foreign = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    preview = await store.preview_channel_deletion(tenant.id, channel.id)
    for owner in (foreign.id, "missing"):
        with pytest.raises(SaaSNotFoundError):
            await store.preview_channel_deletion(owner, channel.id)
        with pytest.raises(SaaSNotFoundError):
            await store.delete_channel(owner, channel.id, "same", preview["confirmation_token"])
    await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    with pytest.raises(SaaSNotFoundError):
        await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    preview = await store.preview_tenant_deletion(tenant.id)
    for actor in ("", "body-actor", "BOOTSTRAP", "00000000000000000000000000000000"):
        with pytest.raises(SaaSValidationError):
            await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], actor)
    store._max_tenants = 2
    await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap")
    for owner in (tenant.id, "missing"):
        with pytest.raises(SaaSNotFoundError):
            await store.preview_tenant_deletion(owner)
        with pytest.raises(SaaSNotFoundError):
            await store.delete_tenant(owner, "same", preview["confirmation_token"], "bootstrap")
    assert (await store.create_tenant("free")).name == "free"


async def test_resource_suspend_reversible_but_disabled_workspace_deletable(store: SaaSStore) -> None:
    """Suspension preserves users and keys until explicit deletion and remains independently reversible."""
    tenant = await store.create_tenant("same")
    users = UserStore(store._db)
    user = await users.create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    channel = await store.create_channel(tenant.id, "same")
    key = await store.rotate_key(tenant.id, channel.id)
    await store.disable_tenant(tenant.id)
    assert await store.lookup_key(key) is None and await users.get_user(user.id) is not None
    assert (await store.get_channel(tenant.id, channel.id)).key_active
    await store.set_tenant_enabled(tenant.id, True)
    assert await store.lookup_key(key) is not None
    await store.update_channel(tenant.id, channel.id, enabled=False)
    await store.disable_tenant(tenant.id)
    preview = await store.preview_channel_deletion(tenant.id, channel.id)
    await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    preview = await store.preview_tenant_deletion(tenant.id)
    await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap")
    assert await users.get_user(user.id) is None and await store.lookup_key(key) is None
