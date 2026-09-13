"""Bounded public audit preservation for physical resource and account deletion."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from gryphon.errors import SaaSValidationError
from gryphon.models.users import UserAccount, UserAudit
from gryphon.saas_audit import account_actor, current_actor

if TYPE_CHECKING:
    from gryphon.saas_database import SaaSDatabase, SQLValue

MAX_AUDIT_ENTRIES = 10000
_SUBJECT_BATCH_SIZE = 100


async def prune_audit(db: SaaSDatabase, category: Literal["resource", "user"], limit: int) -> None:
    """Cap combined live/archive retention inside the caller's serialized transaction."""
    if category not in ("resource", "user") or not 1 <= limit <= MAX_AUDIT_ENTRIES:
        raise SaaSValidationError("Invalid audit retention")
    table = "saas_audit" if category == "resource" else "saas_user_audit"
    retained = (
        f"SELECT id FROM (SELECT id,created_at FROM {table} UNION ALL "
        "SELECT id,created_at FROM saas_audit_archive WHERE category=?) AS retained "
        "ORDER BY created_at DESC,id DESC LIMIT ?"
    )
    await db.execute(f"DELETE FROM {table} WHERE id NOT IN ({retained})", (category, limit))
    await db.execute(
        f"DELETE FROM saas_audit_archive WHERE category=? AND id NOT IN ({retained})", (category, category, limit)
    )


async def archive_resource_audit(db: SaaSDatabase, tenant_id: str) -> None:
    """Move existing resource payloads before tenant deletion; never start a transaction."""
    await db.execute(
        "INSERT INTO saas_audit_archive (id,category,tenant_id,created_at,payload) "
        "SELECT id,'resource',tenant_id,created_at,payload FROM saas_audit WHERE tenant_id=?",
        (tenant_id,),
    )
    await db.execute("DELETE FROM saas_audit WHERE tenant_id=?", (tenant_id,))
    await prune_audit(db, "resource", MAX_AUDIT_ENTRIES)


async def archive_user_audit(db: SaaSDatabase, user_ids: list[str]) -> None:
    """Move subject events in bounded batches without inventing historical display names."""
    for start in range(0, len(user_ids), _SUBJECT_BATCH_SIZE):
        subjects = user_ids[start : start + _SUBJECT_BATCH_SIZE]
        placeholders = ",".join("?" for _ in subjects)
        rows = await db.execute(
            "SELECT a.*,u.username AS subject_username FROM saas_user_audit a "
            f"JOIN saas_users u ON u.id=a.subject_id WHERE a.subject_id IN ({placeholders})",
            subjects,
        )
        events = [UserAudit.model_validate(row) for row in rows]
        await db.executemany(
            "INSERT INTO saas_audit_archive (id,category,tenant_id,created_at,payload) VALUES (?,'user',?,?,?)",
            [(event.id, event.tenant_id, event.created_at, event.model_dump_json()) for event in events],
        )
        await db.execute(f"DELETE FROM saas_user_audit WHERE subject_id IN ({placeholders})", subjects)
    await prune_audit(db, "user", MAX_AUDIT_ENTRIES)


async def append_user_deletion(db: SaaSDatabase, user: UserAccount, actor_id: str) -> None:
    """Snapshot deletion-time public attribution in the archive, preserving the legacy event CHECK."""
    actor = current_actor()
    if actor.id != actor_id or actor.display_source != "snapshot" or actor.kind not in ("user", "bootstrap"):
        actor = await account_actor(db, actor_id)
    if actor.kind in ("user", "bootstrap"):
        actor = actor.model_copy(update={"display_source": "snapshot"})
    event = UserAudit(
        id=str(uuid4()),
        actor_id=actor_id,
        subject_id=user.id,
        tenant_id=user.tenant_id,
        subject_name=user.name,
        subject_username=user.username,
        event="user_deleted",
        created_at=time.time(),
        actor=actor,
    )
    await db.execute(
        "INSERT INTO saas_audit_archive (id,category,tenant_id,created_at,payload) VALUES (?,'user',?,?,?)",
        (event.id, event.tenant_id, event.created_at, event.model_dump_json()),
    )
    await prune_audit(db, "user", MAX_AUDIT_ENTRIES)


async def list_user_audit(db: SaaSDatabase, tenant_id: str | None, limit: int, offset: int) -> list[UserAudit]:
    """Page live and archived events together before resolving only their public actors."""
    clause = "" if tenant_id is None else " WHERE tenant_id=?"
    archive_clause = "" if tenant_id is None else " AND tenant_id=?"
    params: tuple[SQLValue, ...] = () if tenant_id is None else (tenant_id, tenant_id)
    rows = await db.execute(
        "SELECT id,actor_id,subject_id,tenant_id,event,created_at,NULL AS payload "
        f"FROM saas_user_audit{clause} UNION ALL "
        "SELECT id,NULL AS actor_id,NULL AS subject_id,tenant_id,NULL AS event,created_at,payload "
        f"FROM saas_audit_archive WHERE category='user'{archive_clause} "
        "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    events = [
        UserAudit.model_validate_json(str(row["payload"]))
        if row["payload"] is not None
        else UserAudit.model_validate({key: value for key, value in row.items() if key != "payload"})
        for row in rows
    ]
    actors = {
        actor_id: await account_actor(db, actor_id)
        for actor_id in {event.actor_id for event in events if event.actor.display_source != "snapshot"}
    }
    return [
        event
        if event.actor.display_source == "snapshot"
        else event.model_copy(update={"actor": actors[event.actor_id]})
        for event in events
    ]
