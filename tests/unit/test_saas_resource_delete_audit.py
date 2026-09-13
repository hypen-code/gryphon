"""Bounded deletion history, consistent previews, and serialized destructive operations."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from test_saas_resource_delete import _snapshot
from test_saas_store import store as store

from gryphon.errors import ConflictError, SaaSNotFoundError, SaaSQuotaError, SaaSValidationError
from gryphon.models import AuditActor, Channel
from gryphon.saas_audit import audit_actor
from gryphon.saas_audit_archive import archive_resource_audit
from gryphon.saas_store import SaaSStore
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gryphon.saas_database import SQLRow, SQLValue


def test_resource_audit_retention_rejects_oversized_cap_before_open() -> None:
    """Reject unsupported archive retention at configuration time, not during the first mutation."""
    with pytest.raises(SaaSValidationError):
        SaaSStore("sqlite:///:memory:", max_audit_entries=10001)


@pytest.mark.parametrize("limit", [1, 2, 5])
async def test_resource_archive_shares_normal_retention_and_deleted_identity(store: SaaSStore, limit: int) -> None:
    """Ordinary resource mutations evict oldest archived events under the same cap, never an extra allowance."""
    store._max_audit = limit
    tenant = await store.create_tenant("deleted-name")
    await store.create_channel(tenant.id, "channel")
    preview = await store.preview_tenant_deletion(tenant.id)
    actor = AuditActor(id="bootstrap", kind="bootstrap", display_source="snapshot")
    with audit_actor(actor):
        await store.delete_tenant(tenant.id, tenant.name, preview["confirmation_token"], "bootstrap")
    archived = await store.list_deleted_tenant_audit()
    assert len(archived) <= limit
    assert archived[0].resource_name == tenant.name and archived[0].actor == actor
    assert archived == await store.list_audit(tenant.id)
    live = await store.create_tenant("live")
    for _ in range(limit):
        await store.create_channel(live.id, "channel")
    assert await store.list_deleted_tenant_audit() == []
    assert len(await store.list_audit(live.id)) == limit
    async with store._db.transaction():
        rows = await store._db.execute(
            "SELECT COUNT(*) AS total FROM (SELECT id FROM saas_audit UNION ALL "
            "SELECT id FROM saas_audit_archive WHERE category='resource') AS events"
        )
        assert rows[0]["total"] == limit


async def test_resource_archive_reads_merge_scoped_pages_and_exclude_live_tenants(store: SaaSStore) -> None:
    """Global deleted history excludes archives belonging to retained tenants and never includes user events."""
    tenant = await store.create_tenant("retained")
    deleted = await store.create_tenant("deleted")
    user = await UserStore(store._db).create_user("member", "Member", "test-password-only", "tenant_user", deleted.id)
    async with store._db.transaction():
        await archive_resource_audit(store._db, tenant.id)
    await store.create_channel(tenant.id, "live-event")
    events = await store.list_audit(tenant.id)
    assert [item.event for item in events] == ["channel_created", "tenant_created"]
    assert await store.list_audit(tenant.id, limit=1, offset=1) == events[1:]
    assert await store.list_deleted_tenant_audit() == []
    preview = await store.preview_tenant_deletion(deleted.id)
    await store.delete_tenant(deleted.id, deleted.name, preview["confirmation_token"], "bootstrap")
    archived = await store.list_deleted_tenant_audit()
    assert len(archived) == 2 and all(item.tenant_id == deleted.id for item in archived)
    assert await store.list_deleted_tenant_audit(limit=1, offset=1) == archived[1:]
    assert await store.list_audit(tenant.id) == events
    user_events = await UserStore(store._db).list_audit(deleted.id)
    assert {item.event for item in user_events} == {"user_created", "user_deleted"}
    assert all(item.subject_id == user.id for item in user_events)
    for limit, offset in [(0, 0), (101, 0), (1, -1), (1, 10001)]:
        with pytest.raises(SaaSQuotaError):
            await store.list_deleted_tenant_audit(limit, offset)


@pytest.mark.parametrize("kind", ["tenant", "channel"])
async def test_resource_delete_concurrent_requests_commit_once(store: SaaSStore, kind: str) -> None:
    """Concurrent confirmed requests are serialized and only one succeeds or appends a deletion event."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    results: Sequence[object]
    if kind == "tenant":
        preview = await store.preview_tenant_deletion(tenant.id)
        results = await asyncio.gather(
            *(store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap") for _ in range(2)),
            return_exceptions=True,
        )
    else:
        preview = await store.preview_channel_deletion(tenant.id, channel.id)
        results = await asyncio.gather(
            *(store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"]) for _ in range(2)),
            return_exceptions=True,
        )
    assert sum(isinstance(item, tuple | Channel) for item in results) == 1
    assert sum(isinstance(item, SaaSNotFoundError) for item in results) == 1
    assert sum(item.event == f"{kind}_deleted" for item in await store.list_audit(tenant.id)) == 1


async def test_resource_preview_reads_public_rows_only_and_no_documents(store: SaaSStore) -> None:
    """Preview CAS does not read passwords, key digests or potentially large specification documents."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    await UserStore(store._db).create_user("member", "Member", "test-password-only", "tenant_user", tenant.id)
    execute = store._db.execute
    statements = []

    async def record(sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Capture query shapes, never the private values persisted in the disposable store."""
        statements.append(sql)
        return await execute(sql, params)

    with patch.object(store._db, "execute", record):
        workspace = await store.preview_tenant_deletion(tenant.id)
        endpoint = await store.preview_channel_deletion(tenant.id, channel.id)
    assert not any("password_hash" in sql or "key_digest" in sql or "SELECT *" in sql for sql in statements)
    assert not any("payload" in sql for sql in statements if "FROM saas_specs" in sql)
    assert set(workspace) == {
        "kind",
        "id",
        "name",
        "label",
        "confirmation_token",
        "impact",
        "user_ids",
        "channel_ids",
        "spec_ids",
    }
    assert set(endpoint) == {"kind", "id", "name", "label", "confirmation_token", "impact"}


@pytest.mark.parametrize("kind", ["tenant", "channel"])
async def test_resource_delete_cancellation_after_removal_restores_history(store: SaaSStore, kind: str) -> None:
    """Cancellation before commit restores physical rows and pruned audit data, not a partial workspace."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    preview = (
        await store.preview_tenant_deletion(tenant.id)
        if kind == "tenant"
        else await store.preview_channel_deletion(tenant.id, channel.id)
    )
    before, execute = await _snapshot(store), store._db.execute
    store._max_audit = 1

    async def cancel(sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Cancel only once actual physical deletion has run inside the transaction."""
        result = await execute(sql, params)
        if sql.startswith("DELETE FROM saas_tenants" if kind == "tenant" else "DELETE FROM saas_channels"):
            raise asyncio.CancelledError
        return result

    with patch.object(store._db, "execute", cancel), pytest.raises(asyncio.CancelledError):
        if kind == "tenant":
            await store.delete_tenant(tenant.id, "same", preview["confirmation_token"], "bootstrap")
        else:
            await store.delete_channel(tenant.id, channel.id, "same", preview["confirmation_token"])
    assert await _snapshot(store) == before


@pytest.mark.parametrize("kind", ["tenant", "channel", "workspace_channel", "user"])
async def test_resource_preview_invalid_public_identity_fails_closed(store: SaaSStore, kind: str) -> None:
    """Corrupt payload identity or assigned platform-account rows never authorize workspace deletion."""
    tenant = await store.create_tenant("same")
    channel = await store.create_channel(tenant.id, "same")
    execute = store._db.execute

    async def corrupt(sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Inject invalid public projections without disabling any database constraint."""
        rows = await execute(sql, params)
        if kind == "user" and "FROM saas_users" in sql:
            return [{"role": "platform_admin", "tenant_id": tenant.id}]
        table = "saas_tenants" if kind == "tenant" else "saas_channels"
        if kind != "user" and "SELECT" in sql and table in sql and rows:
            payload = json.loads(str(rows[0]["payload"]))
            payload["id"] = "foreign"
            rows[0]["payload"] = json.dumps(payload)
        return rows

    before = await _snapshot(store)
    with patch.object(store._db, "execute", corrupt), pytest.raises(ConflictError):
        if kind == "channel":
            await store.preview_channel_deletion(tenant.id, channel.id)
        else:
            await store.preview_tenant_deletion(tenant.id)
    assert await _snapshot(store) == before
