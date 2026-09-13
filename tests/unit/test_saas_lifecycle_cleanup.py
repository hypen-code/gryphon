"""Cold deletion tombstones and revision-safe suspend/resume completion ownership."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from test_saas_resource_delete_api import _request
from test_saas_user_api import PASSWORD
from test_saas_user_api import api as api

from gryphon.errors import ConflictError
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_runtime import _Runtime

if TYPE_CHECKING:
    import httpx
    from starlette.applications import Starlette

    from gryphon.models import Channel
    from gryphon.saas_api import AdminAPI


class RevisionRetirement:
    """Assert exact status-change cutoffs and expose independently blocked cleanup survivors."""

    def __init__(self, channels: list[Channel], failure: bool) -> None:
        """Remember expected next committed revisions without granting unconditional revocation."""
        self.expected = {channel.id: channel.revision + 1 for channel in channels}
        self.started: dict[str, int] = {}
        self.finished: list[str] = []
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.failure = failure

    async def invalidate(self, channel_id: str, before_revision: int | None = None) -> None:
        """Fail one child immediately, but hold every survivor until the test releases cleanup."""
        assert before_revision == self.expected[channel_id]
        assert before_revision is not None
        self.started[channel_id] = before_revision
        if self.started == self.expected:
            self.ready.set()
        if self.failure and len(self.started) == 1:
            raise RuntimeError("private cleanup failure")
        await self.release.wait()
        self.finished.append(channel_id)


@pytest.mark.parametrize("kind", ["tenant", "channel"])
@pytest.mark.parametrize("stale_revision", [1, 3])
async def test_resource_delete_blocks_delayed_cold_snapshot_without_startup_or_capacity(
    api: tuple[httpx.AsyncClient, Starlette],
    kind: str,
    stale_revision: int,
) -> None:
    """A request holding any pre-deletion snapshot cannot initialize a cold runtime after commit."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant = await admin.store.create_tenant("Workspace")
    first = await admin.store.create_channel(tenant.id, "Channel")
    await admin.store.update_channel(tenant.id, first.id, name="Updated")
    current = await admin.store.update_channel(tenant.id, first.id, name="Current")
    snapshot = first if stale_revision == 1 else current
    path = f"/api/tenants/{tenant.id}" + (f"/channels/{current.id}" if kind == "channel" else "")
    preview = (await client.get(path + "/deletion")).json()
    release = asyncio.Event()

    async def delayed_acquire() -> None:
        """Hold the verified old snapshot until deletion retirement has completely returned."""
        await release.wait()
        async with admin.runtimes.acquire(snapshot, []):
            pytest.fail("A deleted channel snapshot acquired runtime capacity")

    with patch.object(_Runtime, "start", AsyncMock()) as startup:
        task = asyncio.create_task(delayed_acquire())
        try:
            response = await client.request(
                "DELETE",
                path,
                json={
                    "confirm_name": preview["name"],
                    "confirmation_token": preview["confirmation_token"],
                },
            )
            assert response.status_code == 200
            assert admin.runtimes._revoked == {}
            assert admin.runtimes._runtimes == {}
        finally:
            release.set()
            with pytest.raises(ConflictError):
                await task
        startup.assert_not_awaited()
    assert admin.runtimes._runtimes == {}


async def _workspace(admin: AdminAPI, enabled: bool) -> tuple[str, list[Channel], list[str]]:
    """Create revisioned members and channels, then prepare the opposite initial tenant status."""
    tenant = await admin.store.create_tenant("Workspace")
    for index in range(3):
        await admin.store.create_channel(tenant.id, str(index))
    user = await admin.users.create_user("member", "Member", PASSWORD, "tenant_user", tenant.id)
    if enabled:
        await admin.store.set_tenant_enabled(tenant.id, False)
    current = await admin.users.get_user(user.id)
    assert current is not None
    cookies = []
    for _ in range(3):
        session = admin.sessions.login_user(current.id, current.revision)
        assert session is not None
        cookies.append(session[0])
    return tenant.id, await admin.store.list_channels(tenant.id), cookies


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["success", "failure", "cancel"])
async def test_tenant_status_starts_all_retirements_and_finishes_survivors(
    api: tuple[httpx.AsyncClient, Starlette],
    enabled: bool,
    mode: str,
) -> None:
    """Suspend/resume owns cleanup through cancellation/failure and never uses unconditional cutoffs."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant_id, channels, cookies = await _workspace(admin, enabled)
    retire = RevisionRetirement(channels, mode == "failure")
    with patch.object(admin.runtimes, "invalidate", retire.invalidate):
        task = asyncio.create_task(client.patch(f"/api/tenants/{tenant_id}", json={"enabled": enabled}))
        try:
            await asyncio.wait_for(retire.ready.wait(), 3)
            assert retire.started == retire.expected and not task.done()
            assert (await admin.store.get_tenant(tenant_id)).enabled is enabled
            assert all(admin.sessions.verify(cookie) is None for cookie in cookies)
            if mode == "cancel":
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            retire.release.set()
            if mode == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await task
                assert response.status_code == (500 if mode == "failure" else 200)
                if mode == "failure":
                    assert response.json() == {"error": "internal"}
        survivors = set(retire.started) - ({next(iter(retire.started))} if mode == "failure" else set())
        assert set(retire.finished) == survivors


@pytest.mark.parametrize("cancelled", [False, True])
async def test_suspended_cleanup_preserves_concurrently_resumed_runtime_revision(
    api: tuple[httpx.AsyncClient, Starlette],
    cancelled: bool,
) -> None:
    """A delayed suspend cleanup never retires a newer resumed runtime, even after request cancellation."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant = await admin.store.create_tenant("Workspace")
    channel = await admin.store.create_channel(tenant.id, "Channel")
    ready, release = asyncio.Event(), asyncio.Event()
    original = admin.store.list_channels

    async def paused(tenant_id: str) -> list[Channel]:
        """Freeze committed suspension snapshots before a separate resume advances the revision."""
        channels = await original(tenant_id)
        ready.set()
        await release.wait()
        return channels

    with patch.object(admin.store, "list_channels", paused):
        task = asyncio.create_task(client.patch(f"/api/tenants/{tenant.id}", json={"enabled": False}))
        try:
            await asyncio.wait_for(ready.wait(), 3)
            await admin.store.set_tenant_enabled(tenant.id, True)
            resumed = await admin.store.get_channel(tenant.id, channel.id)
            runtime = _Runtime(resumed)
            admin.runtimes._runtimes[channel.id] = runtime
            if cancelled:
                task.cancel()
                await asyncio.sleep(0)
        finally:
            release.set()
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert (await task).status_code == 200
    assert resumed.revision == channel.revision + 2
    assert runtime.verifier.active and runtime.closing is None
    assert admin.runtimes._runtimes[channel.id] is runtime
    assert admin.runtimes._revoked == {}


async def test_tenant_status_rechecks_platform_session_after_body(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Verified initial request state cannot substitute for fresh authorization after parsing input."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant = await admin.store.create_tenant("Workspace")
    cookie = client.cookies[COOKIE_NAME]
    request = _request(f"/api/tenants/{tenant.id}", cookie, {"enabled": False})
    request.scope["method"] = "PATCH"
    assert await admin.access.authorize(request) is None
    admin.sessions.logout(cookie)
    with patch.object(admin.store, "set_tenant_enabled", AsyncMock()) as update:
        response = await admin.tenant(request)
        update.assert_not_awaited()
    assert response.status_code == 401
    assert (await admin.store.get_tenant(tenant.id)).enabled
