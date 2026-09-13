"""Additive archive schema, combined retention, snapshots, and batch transaction boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
import pytest
from test_saas_users import store as store

from gryphon.errors import SaaSStoreError, SaaSValidationError
from gryphon.models.audit import AuditActor
from gryphon.models.users import UserAudit
from gryphon.saas_audit import audit_actor
from gryphon.saas_audit_archive import append_user_deletion, archive_resource_audit, archive_user_audit, prune_audit
from gryphon.saas_database import SaaSDatabase
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.saas_store import SaaSStore

PASSWORD = "disposable-archive-password"


async def test_archive_schema_additive_preserves_original_checks_and_foreign_keys(store: SaaSStore) -> None:
    """Opening a pre-archive temporary database adds only the archive table and its index."""
    tenant = await store.create_tenant("Preserved tenant")
    users = UserStore(store._db)
    user = await users.create_user("member", "Member", PASSWORD, "tenant_user", tenant.id)
    query = (
        "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'saas_audit_archive%' ORDER BY name"
    )
    async with store._db.transaction():
        before = await store._db.execute(query)
        await store._db.execute("DROP TABLE saas_audit_archive")
    await store.close()
    await store.initialize()
    async with store._db.transaction():
        assert await store._db.execute(query) == before
        assert await store._db.execute("PRAGMA foreign_key_list(saas_audit_archive)") == []
        assert await store._db.execute("PRAGMA foreign_keys") == [{"foreign_keys": 1}]
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute("DELETE FROM saas_users WHERE id=?", (user.id,))
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute("UPDATE saas_user_audit SET event='user_deleted' WHERE subject_id=?", (user.id,))
    assert await users.get_user(user.id) == user and await store.get_tenant(tenant.id) == tenant


async def test_archive_user_batch_preserves_public_only_history_and_foreign_scope(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten thousand subject events move in one batch rather than one PostgreSQL round trip per row."""
    tenant = await store.create_tenant("Deleted subjects")
    other = await store.create_tenant("Retained subjects")
    users = UserStore(store._db)
    first = await users.create_user("first", "Renamable", PASSWORD, "tenant_user", tenant.id)
    second = await users.create_user("second", "Other", PASSWORD, "tenant_user", other.id)
    async with store._db.transaction():
        await store._db.executemany(
            "INSERT INTO saas_user_audit VALUES (?,?,?,?,?,?)",
            [(str(uuid4()), "bootstrap", first.id, tenant.id, "user_updated", float(index)) for index in range(9998)],
        )
    batched = AsyncMock(wraps=store._db.executemany)
    monkeypatch.setattr(store._db, "executemany", batched)
    async with store._db.transaction():
        await archive_user_audit(store._db, [first.id])
        await store._db.execute("DELETE FROM saas_users WHERE id=?", (first.id,))
        rows = await store._db.execute("SELECT payload FROM saas_audit_archive WHERE category='user'")
        retained = await store._db.execute("SELECT subject_id FROM saas_user_audit")
    assert batched.await_count == 1 and len(rows) == 9999 and retained == [{"subject_id": second.id}]
    events = [UserAudit.model_validate_json(str(row["payload"])) for row in rows]
    assert all(event.subject_name is None and event.subject_username == first.username for event in events)
    assert all(event.actor.name is None for event in events)
    assert all("password_hash" not in str(row) and PASSWORD not in str(row) for row in rows)
    assert {event.subject_id for event in await users.list_audit(tenant.id)} == {first.id}
    assert [event.subject_id for event in await users.list_audit(other.id)] == [second.id]
    assert len(await users.list_audit(tenant.id, limit=2, offset=2)) == 2


async def test_archive_user_retention_counts_live_and_archived_together(store: SaaSStore) -> None:
    """Future ordinary account mutations prune archived history as well as live rows."""
    tenant = await store.create_tenant("Tenant")
    users = UserStore(store._db)
    first = await users.create_user("first", "First", PASSWORD, "tenant_user", tenant.id)
    second = await users.create_user("second", "Second", PASSWORD, "tenant_user", tenant.id)
    async with store._db.transaction():
        await archive_user_audit(store._db, [first.id])
        await append_user_deletion(store._db, first, "bootstrap")
        await store._db.execute("DELETE FROM saas_users WHERE id=?", (first.id,))
        await prune_audit(store._db, "user", 2)
    events = await users.list_audit()
    assert len(events) == 2 and events[0].event == "user_deleted" and events[1].subject_id == second.id
    async with store._db.transaction():
        await store._db.executemany(
            "INSERT INTO saas_user_audit VALUES (?,?,?,?,?,?)",
            [(str(uuid4()), "bootstrap", second.id, tenant.id, "user_updated", 0.0) for _ in range(9999)],
        )
    await users.update_user(second.id, name="Updated", actor_id="bootstrap")
    async with store._db.transaction():
        rows = await store._db.execute(
            "SELECT (SELECT COUNT(*) FROM saas_user_audit) + "
            "(SELECT COUNT(*) FROM saas_audit_archive WHERE category='user') AS total"
        )
    assert rows == [{"total": 10000}]
    assert [event.event for event in await users.list_audit(limit=2)] == ["user_updated", "user_deleted"]


async def test_archive_resource_move_retention_and_caller_rollback(store: SaaSStore) -> None:
    """Resource payloads move unchanged, category caps stay separate, and rollback restores both tables."""
    tenant = await store.create_tenant("Archived tenant")
    other = await store.create_tenant("Live tenant")
    async with store._db.transaction():
        before = await store._db.execute("SELECT * FROM saas_audit ORDER BY id")
    with pytest.raises(RuntimeError, match="rollback"):
        async with store._db.transaction():
            await archive_resource_audit(store._db, tenant.id)
            await prune_audit(store._db, "resource", 1)
            raise RuntimeError("rollback")
    async with store._db.transaction():
        assert await store._db.execute("SELECT * FROM saas_audit ORDER BY id") == before
        await archive_resource_audit(store._db, tenant.id)
        archived = await store._db.execute("SELECT id,tenant_id,created_at,payload FROM saas_audit_archive")
        assert archived == [row for row in before if row["tenant_id"] == tenant.id]
        await store._db.execute("DELETE FROM saas_tenants WHERE id=?", (tenant.id,))
        await prune_audit(store._db, "resource", 1)
        assert await store._db.execute("SELECT * FROM saas_audit_archive") == []
        assert await store._db.execute("SELECT tenant_id FROM saas_audit") == [{"tenant_id": other.id}]


async def test_archive_deletion_snapshots_verified_actor_not_unrelated_context(store: SaaSStore) -> None:
    """Only deletion-time attribution snapshots names; mismatched request context cannot forge the actor."""
    users = UserStore(store._db)
    admin = await users.create_user("admin", "Original admin", PASSWORD, "platform_admin")
    subject = await users.create_user("subject", "Subject", PASSWORD, "platform_admin")
    snapshot = AuditActor(id=admin.id, username=admin.username, name=admin.name, kind="user", display_source="snapshot")
    async with store._db.transaction():
        with audit_actor(snapshot):
            await append_user_deletion(store._db, subject, admin.id)
        with audit_actor(AuditActor(id=str(uuid4()), name="Unrelated", kind="user", display_source="snapshot")):
            await append_user_deletion(store._db, subject, admin.id)
        with audit_actor(AuditActor(id=admin.id)):
            await append_user_deletion(store._db, subject, admin.id)
    await users.update_user(admin.id, name="Current admin", actor_id="bootstrap")
    events = [event for event in await users.list_audit() if event.event == "user_deleted"]
    assert len(events) == 3 and all(event.actor == snapshot for event in events)


async def test_archive_helpers_empty_and_invalid_retention_and_closed_batch(tmp_path: Path, store: SaaSStore) -> None:
    """Empty subject sets are safe and batch writes retain transport lifecycle and transactional errors."""
    async with store._db.transaction():
        await archive_user_audit(store._db, [])
        await store._db.executemany("INSERT INTO saas_audit_archive VALUES (?,?,?,?,?)", [])
        for limit in (0, 10001):
            with pytest.raises(SaaSValidationError):
                await prune_audit(store._db, "user", limit)
    closed = SaaSDatabase(f"sqlite:///{tmp_path / 'unopened.db'}")
    with pytest.raises(SaaSStoreError):
        await closed.executemany("SELECT ?", [(1,)])
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.executemany(
                "INSERT INTO saas_audit_archive VALUES (?,?,?,?,?)", [("id", "invalid", None, 0.0, "{}")]
            )
    assert await store._db.execute("SELECT * FROM saas_audit_archive") == []


async def test_archive_postgres_batch_parameters_and_transaction_rollback() -> None:
    """The PostgreSQL batch adapter binds placeholders and leaves errors to the existing transaction."""
    cursor = MagicMock()
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    cursor.execute, cursor.executemany = AsyncMock(), AsyncMock()
    cursor.description = None
    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.close = AsyncMock()
    with patch("gryphon.saas_database.psycopg.AsyncConnection.connect", AsyncMock(return_value=connection)):
        db = SaaSDatabase("postgresql://localhost/disposable-mock")
        await db.initialize()
        try:
            async with db.transaction():
                await db.executemany("INSERT INTO example VALUES (?)", [("first",), ("second",)])
            cursor.executemany.assert_awaited_once_with("INSERT INTO example VALUES (%s)", [("first",), ("second",)])
            cursor.executemany.side_effect = psycopg.IntegrityError("Synthetic failure")
            with pytest.raises(SaaSStoreError):
                async with db.transaction():
                    await db.executemany("INSERT INTO example VALUES (?)", [("duplicate",)])
            assert cursor.execute.await_args.args[0] == "ROLLBACK"
        finally:
            await db.close()


async def test_archive_legacy_actor_names_remain_current_not_fabricated_snapshots(store: SaaSStore) -> None:
    """Archiving an older event preserves current-name semantics until its actor is removed."""
    users = UserStore(store._db)
    actor = await users.create_user("actor", "Original actor", PASSWORD, "platform_admin")
    subject = await users.create_user("subject", "Subject", PASSWORD, "platform_admin", actor_id=actor.id)
    async with store._db.transaction():
        await archive_user_audit(store._db, [subject.id])
    await users.update_user(actor.id, name="Current actor", actor_id="bootstrap")
    event = next(event for event in await users.list_audit() if event.subject_id == subject.id)
    assert event.actor.name == "Current actor" and event.actor.display_source == "current"
    preview = await users.preview_user_deletion(actor.id, "bootstrap")
    await users.delete_user(actor.id, actor.username, str(preview["confirmation_token"]), "bootstrap")
    event = next(event for event in await users.list_audit() if event.subject_id == subject.id)
    assert event.actor.id == actor.id and event.actor.name is None and event.actor.kind == "unknown"
