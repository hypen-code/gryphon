"""Physical user deletion, exact consent, account races, and retained attribution."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from test_saas_user_api import _client, _create, _login
from test_saas_user_api import api as api
from test_saas_users import store as store

from gryphon.errors import ConflictError, SaaSNotFoundError, SaaSQuotaError, SaaSStoreError, SaaSValidationError
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from starlette.applications import Starlette

    from gryphon.models.users import UserAccount
    from gryphon.saas_database import SQLRow, SQLValue
    from gryphon.saas_store import SaaSStore

PASSWORD = "disposable-deletion-password"


async def _member(store: SaaSStore, users: UserStore) -> UserAccount:
    """Create a removable tenant account without weakening platform administrator guards."""
    tenant = await store.create_tenant("Deletion tenant")
    return await users.create_user("member", "Original display", PASSWORD, "tenant_user", tenant.id)


async def _delete(users: UserStore, user: UserAccount, actor: str = "bootstrap") -> UserAccount:
    """Use the same explicit consent sequence as a platform browser."""
    preview = await users.preview_user_deletion(user.id, actor)
    return await users.delete_user(user.id, user.username, str(preview["confirmation_token"]), actor)


async def test_user_delete_frees_username_and_quota_without_revoking_shared_key(store: SaaSStore) -> None:
    """A replacement receives a new UUID while old subject attribution and channel keys survive."""
    users = UserStore(store._db, max_users=1, max_users_per_tenant=1)
    user = await _member(store, users)
    assert user.tenant_id is not None
    channel = await store.create_channel(user.tenant_id, "Shared channel", spec_ids=[])
    key = await store.rotate_key(user.tenant_id, channel.id)
    with pytest.raises(SaaSQuotaError):
        await users.create_user("another", "Other", PASSWORD, "tenant_user", user.tenant_id)
    preview = await users.preview_user_deletion(user.id, "bootstrap")
    assert preview == {
        "kind": "user",
        "id": user.id,
        "name": user.username,
        "label": user.name,
        "confirmation_token": preview["confirmation_token"],
        "impact": {"users": 1, "channels": 0, "specs": 0},
    }
    assert await _delete(users, user) == user
    assert await users.get_user(user.id) is None
    assert await users.authenticate(user.username, PASSWORD) is None
    replacement = await users.create_user(user.username, "Replacement", PASSWORD, "tenant_user", user.tenant_id)
    assert replacement.id != user.id and await store.lookup_key(key) is not None
    events = [event for event in await users.list_audit() if event.subject_id == user.id]
    assert [event.event for event in events] == ["user_deleted", "user_created"]
    assert events[0].subject_name == user.name and events[1].subject_name is None
    assert all(event.subject_username == user.username for event in events)
    assert events[0].actor.kind == "bootstrap" and events[0].actor.display_source == "snapshot"
    async with store._db.transaction():
        assert await store._db.execute("PRAGMA foreign_key_check") == []
        assert await store._db.execute("SELECT * FROM saas_users WHERE id=?", (user.id,)) == []
    await store.close()
    await store.initialize()
    assert [event for event in await users.list_audit() if event.subject_id == user.id] == events


@pytest.mark.parametrize("name", ["MEMBER", " member", "member ", "Original display", "", "a" * 129])
async def test_user_delete_requires_exact_username(store: SaaSStore, name: str) -> None:
    """Consent does not normalize case, trim whitespace, or accept the display name."""
    users = UserStore(store._db)
    user = await _member(store, users)
    preview = await users.preview_user_deletion(user.id, "bootstrap")
    with pytest.raises(SaaSValidationError):
        await users.delete_user(user.id, name, str(preview["confirmation_token"]), "bootstrap")
    assert await users.get_user(user.id) == user and len(await users.list_audit()) == 1


@pytest.mark.parametrize("change", ["name", "enabled", "password", "actor"])
async def test_user_delete_stale_public_revision_or_actor_conflicts(store: SaaSStore, change: str) -> None:
    """Every public revision and the initiating administrator bind the confirmation token."""
    users = UserStore(store._db)
    user = await _member(store, users)
    admin = await users.create_user("administrator", "Admin", PASSWORD, "platform_admin")
    preview = await users.preview_user_deletion(user.id, "bootstrap")
    if change == "name":
        await users.update_user(user.id, name="Updated", actor_id="bootstrap")
    elif change == "enabled":
        await users.update_user(user.id, enabled=False, actor_id="bootstrap")
    elif change == "password":
        await users.reset_password(user.id, PASSWORD, "bootstrap")
    with pytest.raises(ConflictError):
        await users.delete_user(
            user.id, user.username, str(preview["confirmation_token"]), admin.id if change == "actor" else "bootstrap"
        )
    assert await users.get_user(user.id) is not None


async def test_user_delete_self_and_last_enabled_platform_guards_are_atomic(store: SaaSStore) -> None:
    """Concurrent bootstrap deletes cannot remove both remaining enabled platform accounts."""
    users = UserStore(store._db)
    first = await users.create_user("admin-one", "One", PASSWORD, "platform_admin")
    with pytest.raises(ConflictError):
        await users.preview_user_deletion(first.id, "bootstrap")
    second = await users.create_user("admin-two", "Two", PASSWORD, "platform_admin")
    with pytest.raises(ConflictError):
        await users.preview_user_deletion(first.id, first.id)
    previews = [await users.preview_user_deletion(user.id, "bootstrap") for user in (first, second)]
    results = await asyncio.gather(
        *(
            users.delete_user(user.id, user.username, str(preview["confirmation_token"]), "bootstrap")
            for user, preview in zip((first, second), previews, strict=True)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ConflictError) for value in results) == 1
    assert len(await users.list_users()) == 1
    remaining = (await users.list_users())[0]
    await users.update_user(remaining.id, enabled=False, actor_id="bootstrap")
    assert (await _delete(users, remaining)).id == remaining.id


async def test_user_delete_disabled_tenant_is_allowed_and_missing_is_not_found(store: SaaSStore) -> None:
    """Platform cleanup does not depend on enabled tenant membership."""
    users = UserStore(store._db)
    user = await _member(store, users)
    assert user.tenant_id is not None
    await store.disable_tenant(user.tenant_id)
    await _delete(users, user)
    with pytest.raises(SaaSNotFoundError):
        await users.preview_user_deletion(user.id, "bootstrap")
    with pytest.raises(SaaSNotFoundError):
        await users.delete_user(user.id, user.username, "0" * 64, "bootstrap")


@pytest.mark.parametrize("operation", ["authenticate", "reset", "change"])
async def test_user_delete_inflight_credentials_cannot_resurrect(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Hash work started before deletion cannot authenticate or recreate the removed UUID."""
    users = UserStore(store._db)
    user = await _member(store, users)
    method = "verify_password" if operation == "authenticate" else "hash_password"
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(users._passwords, method)

    async def blocked(*args: str | None) -> object:
        """Pause trusted hash completion until the account has been physically removed."""
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(users._passwords, method, blocked)
    pending = (
        users.authenticate(user.username, PASSWORD)
        if operation == "authenticate"
        else users.reset_password(user.id, PASSWORD, "bootstrap")
        if operation == "reset"
        else users.change_password(user.id, PASSWORD, PASSWORD, user.revision, user.id)
    )
    task = asyncio.create_task(pending)
    await asyncio.wait_for(entered.wait(), 5)
    await _delete(users, user)
    release.set()
    if operation == "authenticate":
        assert await task is None
    else:
        with pytest.raises(SaaSNotFoundError if operation == "reset" else ConflictError):
            await task
    assert await users.get_user(user.id) is None
    assert [event.event for event in await users.list_audit()] == ["user_deleted", "user_created"]


async def test_user_delete_failure_rolls_back_archive_pruning_and_account(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after pruning restores the entire pre-deletion live audit and account state."""
    users = UserStore(store._db)
    user = await _member(store, users)
    for index in range(3):
        await users.update_user(user.id, name=f"Version {index}", actor_id="bootstrap")
    before = await users.list_audit()
    original = store._db.execute

    async def fail(sql: str, params: Sequence[SQLValue] = ()) -> list[SQLRow]:
        """Inject a failure only after archive writes and pruning have run."""
        if sql.startswith("DELETE FROM saas_users "):
            raise SaaSStoreError("Synthetic failure")
        return await original(sql, params)

    monkeypatch.setattr(store._db, "execute", fail)
    monkeypatch.setattr("gryphon.saas_audit_archive.MAX_AUDIT_ENTRIES", 2)
    with pytest.raises(SaaSStoreError):
        await _delete(users, user)
    assert await users.list_audit() == before
    assert await users.get_user(user.id) is not None
    assert await store._db.execute("SELECT * FROM saas_audit_archive") == []


async def test_user_delete_does_not_fabricate_old_actor_name_on_username_reuse(store: SaaSStore) -> None:
    """Deleted actors remain unknown by UUID even after another account takes their username."""
    users = UserStore(store._db)
    actor = await _member(store, users)
    assert actor.tenant_id is not None
    subject = await users.create_user("subject", "Subject", PASSWORD, "tenant_user", actor.tenant_id, actor.id)
    await _delete(users, actor)
    await users.create_user(actor.username, "Replacement actor", PASSWORD, "tenant_user", actor.tenant_id)
    event = next(event for event in await users.list_audit() if event.subject_id == subject.id)
    assert event.actor.id == actor.id and event.actor.kind == "unknown"
    assert event.actor.name is None and event.actor.username is None


async def test_user_delete_cancellation_finishes_session_revocation(
    api: tuple[httpx.AsyncClient, Starlette], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Client cancellation after commit cannot interrupt owned account-session revocation."""
    admin, app = api
    await _create(admin, "retained-admin")
    user = await _create(admin, "deleted-admin")
    users: UserStore = app.state.admin.users
    original = users.delete_user
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(user_id: str, confirm_name: str, token: str, actor_id: str) -> UserAccount:
        """Pause after durable deletion to exercise cleanup ownership at the sensitive boundary."""
        deleted = await original(user_id, confirm_name, token, actor_id)
        entered.set()
        await release.wait()
        return deleted

    monkeypatch.setattr(users, "delete_user", blocked)
    async with _client(app) as browser:
        await _login(browser, user.username)
        preview = (await admin.get(f"/api/users/{user.id}/deletion")).json()
        task = asyncio.create_task(
            admin.request(
                "DELETE",
                f"/api/users/{user.id}",
                json={
                    "confirm_name": user.username,
                    "confirmation_token": preview["confirmation_token"],
                },
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await browser.get("/api/me")).status_code == 401
        assert await users.get_user(user.id) is None
        assert (await users.list_audit())[0].actor.kind == "bootstrap"
