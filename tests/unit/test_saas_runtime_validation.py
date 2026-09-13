"""Authoritative hosted runtime validation, cancellation rollback and bounded deletion churn."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from test_saas_user_api import api as api

from gryphon.errors import ConflictError
from gryphon.saas_catalog import compile_catalog
from gryphon.saas_runtime import _Runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx
    from starlette.applications import Starlette

    from gryphon.config import GryphonConfig
    from gryphon.models import Channel, SaaSSpec
    from gryphon.runtime.registry import Registry
    from gryphon.saas_api import AdminAPI
    from gryphon.saas_runtime import ChannelRuntimeManager


async def _channel(admin: AdminAPI) -> Channel:
    """Create an enabled, empty-catalog channel with no key or external network dependency."""
    tenant = await admin.store.create_tenant("Workspace")
    return await admin.store.create_channel(tenant.id, "Channel")


async def _delete(admin: AdminAPI, channel: Channel) -> None:
    """Commit deletion directly, deliberately omitting runtime invalidation to exercise live validation."""
    preview = await admin.store.preview_channel_deletion(channel.tenant_id, channel.id)
    await admin.store.delete_channel(channel.tenant_id, channel.id, channel.name, preview["confirmation_token"])


async def _acquire(manager: ChannelRuntimeManager, channel: Channel) -> None:
    """Acquire and release one real runtime while leaving cached dependencies owned by the manager."""
    async with manager.acquire(channel, []):
        pass


async def test_factory_injects_authoritative_validator_and_never_fetches_key_hashes(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """The production factory binds the exact store; validation selects only scoped public metadata."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    assert admin.runtimes._validator == admin.store.is_current_channel
    with patch.object(admin.store._db, "execute", wraps=admin.store._db.execute) as execute:
        assert await admin.store.is_current_channel(channel)
    queries = [call for call in execute.call_args_list if call.args[0].startswith("SELECT")]
    assert len(queries) == 1
    assert "key_digest" not in queries[0].args[0]
    assert queries[0].args[1] == (channel.id, channel.tenant_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Forged"),
        ("revision", 99),
        ("tenant_id", str(uuid4())),
        ("id", str(uuid4())),
        ("include_function_summaries", True),
    ],
)
async def test_store_current_channel_rejects_nonidentical_or_foreign_snapshot(
    api: tuple[httpx.AsyncClient, Starlette],
    field: str,
    value: object,
) -> None:
    """Identity and complete immutable configuration must match, not only a caller-provided revision."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    assert not await admin.store.is_current_channel(channel.model_copy(update={field: value}))


@pytest.mark.parametrize("change", ["delete", "revision", "tenant_disabled", "channel_disabled", "key"])
async def test_cached_runtime_rechecks_live_channel_authority_before_reuse(
    api: tuple[httpx.AsyncClient, Starlette],
    change: str,
) -> None:
    """A previously valid cached revision cannot bypass database deletion, status or policy/key changes."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    await _acquire(admin.runtimes, channel)
    runtime = admin.runtimes._runtimes[channel.id]
    if change == "delete":
        await _delete(admin, channel)
    elif change == "revision":
        await admin.store.update_channel(channel.tenant_id, channel.id, name="Updated")
    elif change == "tenant_disabled":
        await admin.store.set_tenant_enabled(channel.tenant_id, False)
    elif change == "channel_disabled":
        await admin.store.update_channel(channel.tenant_id, channel.id, enabled=False)
    else:
        await admin.store.rotate_key(channel.tenant_id, channel.id)
    with pytest.raises(ConflictError):
        await _acquire(admin.runtimes, channel)
    assert runtime.references == 0
    assert admin.runtimes._runtimes[channel.id] is runtime


@pytest.mark.parametrize("resource", ["tenant", "channel"])
async def test_deletion_during_startup_revalidates_and_closes_partial_runtime(
    api: tuple[httpx.AsyncClient, Starlette],
    resource: str,
) -> None:
    """Fresh post-startup validation rejects a snapshot deleted after its initial check, with no watermark."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(config: GryphonConfig, item: Channel, specs: Sequence[SaaSSpec]) -> Registry:
        """Pause actual compilation so the real store deletion commits while the manager lock is held."""
        entered.set()
        await release.wait()
        return await compile_catalog(config, item, specs)

    with patch("gryphon.saas_runtime.compile_catalog", paused):
        task = asyncio.create_task(_acquire(admin.runtimes, channel))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            runtime = admin.runtimes._runtimes[channel.id]
            if resource == "channel":
                await _delete(admin, channel)
            else:
                preview = await admin.store.preview_tenant_deletion(channel.tenant_id)
                await admin.store.delete_tenant(
                    channel.tenant_id, preview["name"], preview["confirmation_token"], "bootstrap"
                )
            assert admin.runtimes._revoked == {}
        finally:
            release.set()
            with pytest.raises(ConflictError):
                await task
    assert runtime.broker is not None and runtime.broker._closed
    assert runtime.closing is not None and runtime.closing.done()
    assert not runtime.verifier.active
    assert admin.runtimes._runtimes == {} and admin.runtimes._revoked == {}


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("outcome", ["false", "error", "cancel"])
async def test_validator_rejection_failure_and_cancellation_never_publish_runtime(
    api: tuple[httpx.AsyncClient, Starlette],
    phase: int,
    outcome: str,
) -> None:
    """Failure at either validation boundary denies acquisition; initialized resources are fully closed."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    calls = 0
    seen: list[_Runtime] = []

    async def validate(item: Channel) -> bool:
        """Raise at the requested boundary without altering authoritative control-plane state."""
        nonlocal calls
        calls += 1
        assert admin.runtimes._lock.locked()
        if calls == phase:
            seen.extend(admin.runtimes._runtimes.values())
            if outcome == "error":
                raise RuntimeError("private validation failure")
            if outcome == "cancel":
                raise asyncio.CancelledError
            return False
        return await admin.store.is_current_channel(item)

    expected = {"false": ConflictError, "error": RuntimeError, "cancel": asyncio.CancelledError}[outcome]
    with patch.object(admin.runtimes, "_validator", validate), pytest.raises(expected):
        await _acquire(admin.runtimes, channel)
    assert calls == phase and len(seen) == phase - 1
    assert admin.runtimes._runtimes == {} and admin.runtimes._revoked == {}
    for runtime in seen:
        assert not runtime.verifier.active
        assert runtime.closing is not None and runtime.closing.done()
        assert runtime.broker is not None and runtime.broker._closed


async def test_post_validation_repeated_cancellation_finishes_rollback_and_releases_capacity(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Cancellation while validating and again during closure cannot strand a closed startup entry."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    validating, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def validate(item: Channel) -> bool:
        """Block only after real startup has allocated its owned dependencies."""
        nonlocal calls
        calls += 1
        if calls == 2:
            validating.set()
            await asyncio.Event().wait()
        return await admin.store.is_current_channel(item)

    async def blocked_close() -> None:
        """Expose a second cancellation point while the manager owns dependency rollback."""
        closing.set()
        await release.wait()

    with patch.object(admin.runtimes, "_validator", validate):
        task = asyncio.create_task(_acquire(admin.runtimes, channel))
        try:
            await asyncio.wait_for(validating.wait(), 3)
            runtime = admin.runtimes._runtimes[channel.id]
            runtime.stack.push_async_callback(blocked_close)
            task.cancel()
            await asyncio.wait_for(closing.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not runtime.verifier.active and runtime.broker is not None and runtime.broker._closed
    assert admin.runtimes._runtimes == {} and admin.runtimes._revoked == {}
    await _acquire(admin.runtimes, channel)


async def test_twenty_cold_channel_deletions_do_not_retain_historical_watermarks(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Live SQL validation replaces an unbounded map of deleted identities while still denying stale work."""
    client, app = api
    admin = cast("AdminAPI", app.state.admin)
    tenant = await admin.store.create_tenant("Workspace")
    snapshots = []
    with patch.object(_Runtime, "start", AsyncMock()) as startup:
        for index in range(20):
            channel = await admin.store.create_channel(tenant.id, str(index))
            snapshots.append(channel)
            path = f"/api/tenants/{tenant.id}/channels/{channel.id}"
            preview = (await client.get(path + "/deletion")).json()
            response = await client.request(
                "DELETE",
                path,
                json={
                    "confirm_name": preview["name"],
                    "confirmation_token": preview["confirmation_token"],
                },
            )
            assert response.status_code == 200
            assert admin.runtimes._revoked == {} and admin.runtimes._runtimes == {}
        for snapshot in snapshots:
            with pytest.raises(ConflictError):
                await _acquire(admin.runtimes, snapshot)
        startup.assert_not_awaited()
    assert await admin.store.list_channels(tenant.id) == []


async def test_newer_resumed_cached_runtime_survives_completed_old_cutoff(
    api: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Retiring an earlier status revision clears its watermark without disabling newer verified authority."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    await admin.store.set_tenant_enabled(channel.tenant_id, False)
    suspended = await admin.store.get_channel(channel.tenant_id, channel.id)
    await admin.store.set_tenant_enabled(channel.tenant_id, True)
    resumed = await admin.store.get_channel(channel.tenant_id, channel.id)
    await _acquire(admin.runtimes, resumed)
    runtime = admin.runtimes._runtimes[channel.id]
    await admin.runtimes.invalidate(channel.id, before_revision=suspended.revision)
    assert admin.runtimes._revoked == {} and runtime.verifier.active
    await _acquire(admin.runtimes, resumed)
    with pytest.raises(ConflictError):
        await _acquire(admin.runtimes, channel)
    assert admin.runtimes._runtimes[channel.id] is runtime


@pytest.mark.parametrize(
    "resource,change",
    [
        ("tenant", {"id": str(uuid4())}),
        ("tenant", {"enabled": False}),
        ("channel", {"tenant_id": str(uuid4())}),
        ("channel", {"enabled": False}),
    ],
)
async def test_authoritative_validation_rejects_payload_identity_or_status_disagreement(
    api: tuple[httpx.AsyncClient, Starlette],
    resource: str,
    change: dict[str, object],
) -> None:
    """Enabled SQL columns alone cannot override inconsistent scoped payload identity or status."""
    _, app = api
    admin = cast("AdminAPI", app.state.admin)
    channel = await _channel(admin)
    item = await admin.store.get_tenant(channel.tenant_id) if resource == "tenant" else channel
    modified = item.model_copy(update=change)
    async with admin.store._db.transaction():
        await admin.store._db.execute(
            f"UPDATE saas_{resource}s SET payload=? WHERE id=?", (modified.model_dump_json(), item.id)
        )
    assert not await admin.store.is_current_channel(channel)
