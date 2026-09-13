"""Deletion HTTP authority and cancellation ownership against disposable control state."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request
from test_saas_user_api import PASSWORD
from test_saas_user_api import api as api

from gryphon.saas_audit import current_actor
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_runtime import _Runtime

if TYPE_CHECKING:
    import httpx
    from starlette.applications import Starlette
    from starlette.types import Message

    from gryphon.models import Channel, Tenant, UserAccount
    from gryphon.saas_api import AdminAPI


class Retirement:
    """Expose deterministic revocation and closure barriers without creating runtime files."""

    def __init__(self, count: int, failure: BaseException | None = None) -> None:
        """Track all invalidations, requiring cutoffs covering the deleted revision."""
        self.count, self.failure = count, failure
        self.started: list[str] = []
        self.finished: list[str] = []
        self.ready, self.release = asyncio.Event(), asyncio.Event()

    async def invalidate(self, channel_id: str, before_revision: int | None = None) -> None:
        """Start every invalidation before waiting; optionally fail the first immediately."""
        assert before_revision == 2
        self.started.append(channel_id)
        if len(self.started) == self.count:
            self.ready.set()
        if self.failure is not None and len(self.started) == 1:
            raise self.failure
        await self.release.wait()
        self.finished.append(channel_id)


async def _target(admin: AdminAPI, kind: str) -> tuple[str, dict[str, object]]:
    """Create one disposable target and exact current consent through real store previews."""
    tenant = await admin.store.create_tenant("Exact name")
    path = f"/api/tenants/{tenant.id}"
    if kind == "channel":
        channel = await admin.store.create_channel(tenant.id, "Exact name")
        path += f"/channels/{channel.id}"
        preview = await admin.store.preview_channel_deletion(tenant.id, channel.id)
    else:
        preview = await admin.store.preview_tenant_deletion(tenant.id)
    return path, {"confirm_name": preview["name"], "confirmation_token": preview["confirmation_token"]}


def _request(path: str, cookie: str, body: dict[str, object]) -> Request:
    """Build an authenticated request without inventing verified request-state authority."""
    parts = path.split("/")
    params = {"tenant_id": parts[3]}
    if len(parts) > 4:
        params["channel_id"] = parts[5]

    async def receive() -> Message:
        """Provide closed JSON once to the ordinary body parser."""
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": path,
            "path_params": params,
            "headers": [(b"cookie", f"{COOKIE_NAME}={cookie}".encode()), (b"content-type", b"application/json")],
            "query_string": b"channel_id=forged&kind=channel&actor_id=forged",
        },
        receive,
    )


async def _members(admin: AdminAPI, tenant_id: str) -> tuple[list[UserAccount], list[str]]:
    """Issue multiple sessions at old/current revisions for every real workspace member."""
    users, cookies = [], []
    for index in range(2):
        user = await admin.users.create_user(f"member-{index}", "Member", PASSWORD, "tenant_user", tenant_id)
        user = await admin.users.update_user(user.id, name="Updated", actor_id="bootstrap")
        users.append(user)
        for revision in (user.revision - 1, user.revision, user.revision):
            session = admin.sessions.login_user(user.id, revision)
            assert session is not None
            cookies.append(session[0])
    return users, cookies


class Publication:
    """Pause after real commit, before API-side session revocation and runtime retirement."""

    def __init__(self, admin: AdminAPI) -> None:
        """Retain the original bound store operation and independent publication barriers."""
        self.delete = admin.store.delete_tenant
        self.committed, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(
        self, tid: str, name: object, token: object, actor: str
    ) -> tuple[Tenant, list[Channel], list[UserAccount]]:
        """Preserve real transaction semantics and inherited verified audit metadata on cancellation."""
        result = await self.delete(tid, name, token, actor)
        self.committed.set()
        await self.release.wait()
        assert current_actor().id == "bootstrap"
        return result


async def test_tenant_delete_cancel_after_commit_finishes_cleanup_and_revokes_all_sessions(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Repeated request cancellation cannot strand committed deletion or current-revision cookies."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, _ = await _target(admin, "tenant")
    tenant_id = path.split("/")[-1]
    channels = [await admin.store.create_channel(tenant_id, str(index)) for index in range(3)]
    users, cookies = await _members(admin, tenant_id)
    preview = await admin.store.preview_tenant_deletion(tenant_id)
    body = {"confirm_name": preview["name"], "confirmation_token": preview["confirmation_token"]}
    retire, publication = Retirement(3), Publication(admin)
    committed, publish = publication.committed, publication.release
    with (
        patch.object(admin.store, "delete_tenant", publication),
        patch.object(admin.runtimes, "invalidate", retire.invalidate),
        patch.object(admin.sessions, "revoke_user", wraps=admin.sessions.revoke_user) as revoke,
    ):
        task = asyncio.create_task(client.request("DELETE", path, json=body))
        try:
            await asyncio.wait_for(committed.wait(), 3)
            assert await admin.store.list_tenants() == []
            assert not retire.started and all(admin.sessions.verify(cookie) is not None for cookie in cookies)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            publish.set()
            await asyncio.wait_for(retire.ready.wait(), 3)
            assert set(retire.started) == {channel.id for channel in channels} and not task.done()
            assert all(admin.sessions.verify(cookie) is None for cookie in cookies)
            assert all(admin.sessions.identity(cookie) is None for cookie in cookies)
            assert all([await admin.users.get_user(user.id) is None for user in users])
            assert revoke.call_count == len(users)
            for user in users:
                revoke.assert_any_call(user.id, before_revision=user.revision + 1)
        finally:
            publish.set()
            retire.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert set(retire.finished) == {channel.id for channel in channels}
    assert admin.sessions.verify(client.cookies[COOKIE_NAME]) is not None
    assert current_actor().kind == "unknown"


@pytest.mark.parametrize("kind", ["tenant", "channel"])
async def test_resource_delete_waits_for_retirement_before_success(
    api: tuple[httpx.AsyncClient, Starlette],
    kind: str,
) -> None:
    """No successful HTTP deletion response is returned while owned runtime closure is pending."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, body = await _target(admin, kind)
    if kind == "tenant":
        await admin.store.create_channel(path.split("/")[-1], "Child")
        preview = await admin.store.preview_tenant_deletion(path.split("/")[-1])
        body["confirmation_token"] = preview["confirmation_token"]
    retire = Retirement(1)
    with patch.object(admin.runtimes, "invalidate", retire.invalidate):
        task = asyncio.create_task(client.request("DELETE", path, json=body))
        try:
            await asyncio.wait_for(retire.ready.wait(), 3)
            assert not task.done()
        finally:
            retire.release.set()
            response = await task
    assert response.status_code == 200
    assert response.json() == {"deleted": True, f"{kind}_id": path.split("/")[-1]}
    assert retire.finished == retire.started


async def test_retire_real_manager_revokes_all_before_first_runtime_drains(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Deleted-revision cutoffs revoke current authority before the manager's serialized drain lock."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant = await admin.store.create_tenant("Workspace")
    channels = [await admin.store.create_channel(tenant.id, str(index)) for index in range(3)]
    runtimes = [_Runtime(channel) for channel in channels]
    for runtime in runtimes:
        runtime.idle.clear()
        admin.runtimes._runtimes[runtime.channel.id] = runtime
    task = asyncio.create_task(admin.resource_deletion._retire(channels))
    try:
        async with asyncio.timeout(3):
            while any(runtime.verifier.active for runtime in runtimes):
                await asyncio.sleep(0)
        assert not task.done()
        assert all(admin.runtimes._revoked[channel.id] == channel.revision for channel in channels)
    finally:
        for runtime in runtimes:
            runtime.idle.set()
        await task
    assert admin.runtimes._runtimes == {}
    assert all(runtime.closing is not None and runtime.closing.done() for runtime in runtimes)


@pytest.mark.parametrize("failure", [RuntimeError("private cleanup failure"), asyncio.CancelledError()])
async def test_retire_attempts_every_runtime_and_propagates_failure(
    api: tuple[httpx.AsyncClient, Starlette],
    failure: BaseException,
) -> None:
    """return_exceptions gather must start all children and await survivors before raising failures."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    retire = Retirement(3, failure)
    tenant = await admin.store.create_tenant("Workspace")
    channels = [await admin.store.create_channel(tenant.id, name) for name in ("first", "second", "third")]
    ids = [channel.id for channel in channels]
    with patch.object(admin.runtimes, "invalidate", retire.invalidate):
        task = asyncio.create_task(admin.resource_deletion._retire(channels))
        try:
            await asyncio.wait_for(retire.ready.wait(), 3)
            assert retire.started == ids and not task.done()
        finally:
            retire.release.set()
            with pytest.raises(type(failure)):
                await task
    assert retire.finished == ids[1:]


@pytest.mark.parametrize("kind", ["tenant", "channel"])
async def test_resource_delete_cleanup_failure_never_returns_false_success(
    api: tuple[httpx.AsyncClient, Starlette],
    kind: str,
) -> None:
    """A committed deletion with failed retirement returns only a safe error, not HTTP 200."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, body = await _target(admin, kind)
    with patch.object(admin.resource_deletion, "_retire", AsyncMock(side_effect=RuntimeError("private failure"))):
        response = await client.request("DELETE", path, json=body)
    assert response.status_code == 500 and response.json() == {"error": "internal"}
    assert (await client.get(path + "/deletion")).status_code == 404


@pytest.mark.parametrize("kind", ["tenant", "channel"])
@pytest.mark.parametrize("stage", ["before", "before_delete", "body", "preview"])
async def test_resource_delete_rechecks_actual_session_around_async_work(
    api: tuple[httpx.AsyncClient, Starlette],
    kind: str,
    stage: str,
) -> None:
    """Fresh platform/tenant authorization is required before reads and after body/preview awaits."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, body = await _target(admin, kind)
    cookie = client.cookies[COOKIE_NAME]
    request = _request(path, cookie, body)
    preview_name = f"preview_{kind}_deletion"
    original = getattr(admin.store, preview_name)

    async def preview(*args: str) -> dict[str, object]:
        """Invalidate real session state after the consistent impact snapshot has been read."""
        result: dict[str, object] = await original(*args)
        admin.sessions.logout(cookie)
        return result

    async def receive() -> Message:
        """Revoke the session while the handler waits for JSON, rather than spoofing request.state."""
        admin.sessions.logout(cookie)
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    if stage in {"before", "before_delete"}:
        admin.sessions.logout(cookie)
    if stage == "body":
        request = Request(request.scope, receive)
    with patch.object(admin.store, preview_name, side_effect=preview) as read:
        with patch.object(admin.store, f"delete_{kind}", AsyncMock()) as delete:
            response = await (
                admin.resource_deletion.preview(request)
                if stage in {"before", "preview"}
                else admin.resource_deletion.delete(request)
            )
            delete.assert_not_awaited()
        assert read.call_count == (1 if stage == "preview" else 0)
    assert response.status_code == 401


@pytest.mark.parametrize("kind", ["tenant", "channel"])
@pytest.mark.parametrize(
    "change",
    [
        {"confirm_name": True},
        {"confirm_name": 1},
        {"confirm_name": []},
        {"confirm_name": " Exact name "},
        {"confirmation_token": False},
        {"confirmation_token": {}},
        {"confirmation_token": "A" * 64},
        {"actor_id": "bootstrap"},
        {"role": "platform_admin"},
        {"channel_ids": []},
        {"force": True},
    ],
)
async def test_resource_delete_closed_typed_consent_preserves_target(
    api: tuple[httpx.AsyncClient, Starlette],
    kind: str,
    change: dict[str, object],
) -> None:
    """Caller identities/resource lists and coercible consent never grant destructive authority."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, body = await _target(admin, kind)
    with patch.object(admin.runtimes, "invalidate", AsyncMock()) as invalidate:
        response = await client.request("DELETE", path, json=body | change)
        invalidate.assert_not_awaited()
    assert response.status_code == 400 and response.json() == {"error": "validation"}
    assert (await client.get(path + "/deletion")).status_code == 200


async def test_resource_preview_query_cannot_choose_role_or_resource_kind(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """An own-tenant member may preview channels, but query channel IDs cannot bypass platform checks."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, _ = await _target(admin, "channel")
    tenant_id = path.split("/")[3]
    user = await admin.users.create_user("member", "Member", PASSWORD, "tenant_user", tenant_id)
    session = admin.sessions.login_user(user.id, user.revision)
    assert session is not None
    headers = {"cookie": f"{COOKIE_NAME}={session[0]}"}
    assert (await client.get(path + "/deletion?kind=tenant", headers=headers)).json()["kind"] == "channel"
    with patch.object(admin.store, "preview_tenant_deletion", AsyncMock()) as preview:
        response = await client.get(
            f"/api/tenants/{tenant_id}/deletion?kind=channel&channel_id=forged", headers=headers
        )
        preview.assert_not_awaited()
    assert response.status_code == 403


async def test_deleted_tenant_audit_only_platform_and_verified_actor(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Archived tenant events remain platform-only, and forged actor headers never replace authenticated metadata."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    path, body = await _target(admin, "tenant")
    response = await client.request(
        "DELETE", path, json=body, headers={"x-actor-id": "forged", "x-role": "platform_admin"}
    )
    assert response.status_code == 200
    events = (await client.get("/api/audit")).json()["items"]
    deletion = next(event for event in events if event["event"] == "tenant_deleted")
    assert deletion["actor"]["id"] == "bootstrap" and deletion["actor"]["kind"] == "bootstrap"
    tenant = await admin.store.create_tenant("Unrelated")
    user = await admin.users.create_user("member", "Member", PASSWORD, "tenant_user", tenant.id)
    session = admin.sessions.login_user(user.id, user.revision)
    assert session is not None
    with patch.object(admin.store, "list_deleted_tenant_audit", AsyncMock()) as archive:
        response = await client.get("/api/audit", headers={"cookie": f"{COOKIE_NAME}={session[0]}"})
        archive.assert_not_awaited()
    assert response.status_code == 200
    assert all(event.get("tenant_id") != path.split("/")[-1] for event in response.json()["items"])
