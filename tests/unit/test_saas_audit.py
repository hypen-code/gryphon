"""Audit-only context isolation, cancellation, rollback and legacy actor projection."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.requests import Request

from gryphon.errors import SaaSStoreError
from gryphon.models import AuditActor, AuditEvent, UserAccount
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_audit import audit_actor, current_actor, request_actor
from gryphon.saas_store import SaaSStore
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SaaSStore]:
    """Own only a disposable audit database."""
    value = SaaSStore(f"sqlite:///{tmp_path / 'audit.db'}")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


def test_audit_request_actor_requires_verified_public_identity() -> None:
    """Headers and unverified request state cannot supply actor metadata."""
    request = Request({"type": "http", "headers": [(b"x-actor-id", b"bootstrap")]})
    assert request_actor(request) == AuditActor()
    request.state.account = None
    assert request_actor(request) == AuditActor()
    request.state.authenticated = True
    assert request_actor(request).kind == "bootstrap"
    request.state.account = {"id": "bootstrap"}
    assert request_actor(request) == AuditActor()
    user = UserAccount(id=str(uuid4()), username="member", name="Member", role="platform_admin", created_at=0)
    request.state.account = user
    assert request_actor(request) == AuditActor(
        id=user.id, username=user.username, name=user.name, kind="user", display_source="snapshot"
    )


@pytest.mark.parametrize("cancelled", [False, True])
async def test_audit_cleanup_inherits_actor_and_context_resets(cancelled: bool) -> None:
    """Owned child tasks retain immutable metadata while request exits restore their prior context."""
    entered, release = asyncio.Event(), asyncio.Event()
    actor = AuditActor(id=str(uuid4()), name="Actor", kind="user", display_source="snapshot")
    observed: list[AuditActor] = []

    async def cleanup() -> None:
        """Keep a child alive across cancellation without losing the original actor."""
        entered.set()
        await release.wait()
        observed.append(current_actor())

    async def request() -> None:
        """Always reset metadata even when cancellation is propagated after cleanup."""
        try:
            with audit_actor(actor):
                await finish_cleanup(cleanup())
        finally:
            assert current_actor() == AuditActor()

    task = asyncio.create_task(request())
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            if cancelled:
                task.cancel()
            release.set()
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert observed == [actor] and current_actor() == AuditActor()


async def test_audit_legacy_and_snapshots_survive_reopen(store: SaaSStore) -> None:
    """Missing legacy metadata is unknown; new explicit system metadata survives storage reopen."""
    tenant = await store.create_tenant("Tenant")
    async with store._db.transaction():
        rows = await store._db.execute("SELECT id,payload FROM saas_audit")
        payload = json.loads(str(rows[0]["payload"]))
        payload.pop("actor")
        await store._db.execute("UPDATE saas_audit SET payload=? WHERE id=?", (json.dumps(payload), rows[0]["id"]))
    assert (await store.list_audit(tenant.id))[0].actor == AuditActor()
    actor = AuditActor(kind="system", name="Maintenance", display_source="snapshot")
    with audit_actor(actor):
        await store.create_channel(tenant.id, "Compute")
    await store.close()
    await store.initialize()
    events = await store.list_audit(tenant.id)
    assert events[0].actor == actor and events[1].actor == AuditActor()
    assert current_actor() == AuditActor()


async def test_audit_failed_transaction_removes_event_and_mutation(store: SaaSStore) -> None:
    """Failure after audit insertion rolls back both the resource and attributed event."""
    tenant = await store.create_tenant("Tenant")
    before = await store.list_audit(tenant.id)
    original = store._audit

    async def fail(tenant_id: str, event: AuditEvent, channel_id: str | None = None) -> None:
        """Fail after the real insert, exercising rollback rather than skipping audit work."""
        await original(tenant_id, event, channel_id)
        raise SaaSStoreError("Synthetic audit failure")

    with patch.object(store, "_audit", fail), pytest.raises(SaaSStoreError), audit_actor(AuditActor(kind="system")):
        await store.create_channel(tenant.id, "Rolled back")
    assert await store.list_channels(tenant.id) == []
    assert await store.list_audit(tenant.id) == before
    assert current_actor() == AuditActor()


async def test_account_audit_actor_is_not_subject_and_names_are_current(store: SaaSStore) -> None:
    """Retained account IDs resolve only public actor fields, explicitly not historical names."""
    users = UserStore(store._db)
    password = "disposable-audit-password"
    actor = await users.create_user("operator", "Original", password, "platform_admin")
    tenant = await store.create_tenant("Tenant")
    subject = await users.create_user("subject", "Subject", password, "tenant_user", tenant.id, actor.id)
    await users.update_user(actor.id, name="Renamed", actor_id="bootstrap")
    events = await users.list_audit(tenant.id)
    assert len(events) == 1 and events[0].subject_id == subject.id
    assert events[0].actor == AuditActor(
        id=actor.id, username=actor.username, name="Renamed", kind="user", display_source="current"
    )
    assert password not in events[0].model_dump_json() and "password_hash" not in events[0].model_dump_json()
    missing = str(uuid4())
    await users.update_user(subject.id, name="Other", actor_id=missing)
    assert (await users.list_audit(tenant.id))[0].actor == AuditActor(id=missing)
    bootstrap = next(event for event in await users.list_audit() if event.actor_id == "bootstrap")
    assert bootstrap.actor == AuditActor(id="bootstrap", kind="bootstrap")
