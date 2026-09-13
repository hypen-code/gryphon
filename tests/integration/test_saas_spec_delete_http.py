"""Confirmed logical-spec deletion uses real authenticated HTTP and isolated MCP runtimes."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from starlette.requests import Request
from test_saas_http import _data
from test_saas_user_http import POLICY, Members, _publish
from test_saas_user_http import members as members

from gryphon.saas_http import read_object

if TYPE_CHECKING:
    from gryphon.models import SpecDeletionPreview


async def _versions(members: Members) -> tuple[dict[str, str], list[str]]:
    """Retain a three-version lineage with the original channel intentionally pinned."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    versions = [channel["spec"]]
    for version in ("2", "3"):
        document = (await members.first.get(prefix + "/specs/" + versions[-1])).json()["document"]
        document["info"]["version"] = version
        response = await members.first.post(
            prefix + "/specs/" + versions[-1] + "/refresh", json={"content": json.dumps(document)}
        )
        assert response.status_code == 201
        versions.append(response.json()["id"])
    return channel, versions


async def _preview(http: httpx.AsyncClient, prefix: str, spec_id: str) -> dict[str, Any]:
    """Load the exact server-computed impact without implying deletion consent."""
    response = await http.get(prefix + "/specs/" + spec_id + "/deletion")
    assert response.status_code == 200
    assert "key_digest" not in response.text and "document" not in response.json()
    return dict(response.json())


def _confirmation(preview: dict[str, Any]) -> dict[str, str]:
    """Supply an explicit exact-name confirmation and the current impact fingerprint."""
    return {"confirm_name": preview["name"], "confirmation_token": preview["confirmation_token"]}


async def _post_delete_runtime(channel: dict[str, str], cached: dict[str, Any]) -> None:
    """Keep the channel key and receipts while removing capability authority and stale replay."""
    async with Client(channel["url"], auth=channel["token"]) as client:
        listing = await _data(client, "list_servers")
        assert [item["name"] for item in listing["servers"]] == ["inventory"]
        replay = await _data(client, "run_cached_code", {"cache_id": cached["cache_id"], "params": {}})
        assert not replay["success"] and replay["error_type"] == "conflict"
        receipt = await _data(client, "get_run", {"run_id": cached["run_id"]})
        assert receipt["status"] == "succeeded" and receipt["result"]["data"] == {"value": 3}
        with patch("gryphon.security.broker.NetworkClient.request", AsyncMock()) as network:
            missing = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("weather.current", {})', "description": "Deleted"},
            )
        assert not missing["success"] and missing["error_type"] == "not_found"
        network.assert_not_awaited()
        assert (await _data(client, "execute_code", {"code": "result = 42", "description": "Kept channel"}))[
            "data"
        ] == 42


async def test_confirmed_delete_removes_lineage_and_drains_only_affected_runtime(members: Members) -> None:
    """Typed deletion removes all versions, not unrelated same-name roots, channels or receipts."""
    channel, versions = await _versions(members)
    prefix = "/api/tenants/" + members.tenants[0]
    inventory = await _publish(members.first, members.tenants[0], "inventory")
    same_name = await _publish(members.first, members.tenants[0], "weather")
    policy = POLICY | {"name": "weather", "spec_ids": [versions[0], inventory["spec"]], "enabled": True}
    before = await members.first.patch(channel["path"], json=policy)
    assert before.status_code == 200
    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch(
            "gryphon.security.broker.NetworkClient.request",
            AsyncMock(return_value=httpx.Response(200, json={"value": 3})),
        ):
            cached = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("weather.current", {})', "description": "Before deletion"},
            )
        assert cached["success"]
    broker = members.app.state.runtimes._runtimes[channel["channel"]].broker
    preview = await _preview(members.first, prefix, versions[-1])
    assert preview["version_count"] == 3 and set(preview["version_ids"]) == set(versions)
    assert [item["id"] for item in preview["channels"]] == [channel["channel"]]
    response = await members.first.request("DELETE", prefix + "/specs/" + versions[-1], json=_confirmation(preview))
    assert response.status_code == 200 and response.json()["deleted"] is True
    assert set(response.json()["deleted_spec_ids"]) == set(versions)
    assert response.json()["updated_channel_ids"] == [channel["channel"]]
    assert broker is not None and broker._closed
    retained = (await members.first.get(prefix + "/specs")).json()["items"]
    assert {item["id"] for item in retained} == {inventory["spec"], same_name["spec"]}
    bound = (await members.first.get(prefix + "/channels")).json()["items"]
    updated = next(item for item in bound if item["id"] == channel["channel"])
    assert updated["spec_ids"] == [inventory["spec"]] and updated["revision"] == before.json()["revision"] + 1
    assert updated["key_active"] and len(bound) == 3
    for spec_id in versions:
        assert (await members.first.get(prefix + "/specs/" + spec_id)).status_code == 404
    await _post_delete_runtime(channel, cached)
    event = next(
        item for item in (await members.first.get("/api/audit")).json()["items"] if item["event"] == "spec_deleted"
    )
    assert event["actor"]["id"] == members.users[0].id and event["spec_id"] == versions[0]
    assert (
        await members.first.request("DELETE", prefix + "/specs/" + versions[-1], json=_confirmation(preview))
    ).status_code == 404


@pytest.mark.parametrize("name", ["", "Weather", "weather ", " weather", None, 1, ["weather"]])
async def test_spec_delete_requires_exact_typed_name_without_effects(members: Members, name: object) -> None:
    """The API validates consent too; browser-only button state cannot authorize deletion."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])
    body: dict[str, object] = _confirmation(preview) | {"confirm_name": name}
    response = await members.first.request("DELETE", prefix + "/specs/" + channel["spec"], json=body)
    assert response.status_code == 400
    assert (await members.first.get(prefix + "/specs/" + channel["spec"])).status_code == 200
    assert await _preview(members.first, prefix, channel["spec"]) == preview


@pytest.mark.parametrize("change", ["refresh", "binding", "channel"])
async def test_spec_delete_rejects_stale_impact_and_requires_new_consent(members: Members, change: str) -> None:
    """A new revision or affected channel cannot be silently swept into an old confirmation."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])
    if change == "refresh":
        response = await members.first.post(
            prefix + "/specs/" + channel["spec"] + "/filter", json={"read_only_filter": False}
        )
        assert response.status_code == 201
    elif change == "binding":
        assert (
            await members.first.post(prefix + "/channels", json=POLICY | {"spec_ids": [channel["spec"]]})
        ).status_code == 201
    else:
        assert (
            await members.first.patch(
                channel["path"], json=POLICY | {"spec_ids": [channel["spec"]], "enabled": True, "name": "Renamed"}
            )
        ).status_code == 200
    before = (await members.first.get(prefix + "/specs")).json()
    response = await members.first.request("DELETE", prefix + "/specs/" + channel["spec"], json=_confirmation(preview))
    assert response.status_code == 409
    assert (await members.first.get(prefix + "/specs")).json() == before
    assert (await _preview(members.first, prefix, channel["spec"]))["confirmation_token"] != preview[
        "confirmation_token"
    ]


async def test_spec_deletion_auth_csrf_and_foreign_scope_precede_store_access(members: Members) -> None:
    """Forged role headers, foreign handles and channel credentials do not bypass browser authority."""
    channel = await _publish(members.second, members.tenants[1], "weather")
    foreign = "/api/tenants/" + members.tenants[1] + "/specs/" + channel["spec"]
    own = "/api/tenants/" + members.tenants[0] + "/specs/" + channel["spec"]
    with patch.object(members.app.state.store, "preview_spec_deletion", AsyncMock()) as preview:
        assert (await members.first.get(foreign + "/deletion")).status_code == 404
        assert (
            await members.first.request("DELETE", foreign, json={}, headers={"x-role": "platform_admin"})
        ).status_code == 404
        assert (
            await members.second.request("DELETE", foreign, json={}, headers={"x-csrf-token": "wrong"})
        ).status_code == 403
        async with httpx.AsyncClient(base_url=members.first.base_url) as anonymous:
            assert (
                await anonymous.get(foreign + "/deletion", headers={"authorization": "Bearer " + channel["token"]})
            ).status_code == 401
        preview.assert_not_awaited()
    assert (await members.first.get(own + "/deletion")).status_code == 404
    assert (
        await members.first.request("DELETE", own, json={"confirm_name": "weather", "confirmation_token": "0" * 64})
    ).status_code == 404


async def test_spec_delete_rejects_extra_authority_fields_and_missing_confirmation(members: Members) -> None:
    """Only the exact consent fields are accepted, not caller-supplied deletion sets or actors."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])
    for body in [
        {},
        {"confirm_name": "weather"},
        _confirmation(preview) | {"actor_id": "bootstrap"},
        _confirmation(preview) | {"version_ids": []},
    ]:
        assert (
            await members.first.request("DELETE", prefix + "/specs/" + channel["spec"], json=body)
        ).status_code == 400
    assert await _preview(members.first, prefix, channel["spec"]) == preview


async def test_spec_delete_session_revocation_after_preview_blocks_deletion(members: Members) -> None:
    """A fetched preview is not an authentication capability and survives no session revocation."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])
    assert (await members.first.post("/api/logout")).status_code == 200
    assert (
        await members.first.request("DELETE", prefix + "/specs/" + channel["spec"], json=_confirmation(preview))
    ).status_code == 401
    assert (await members.admin.get(prefix + "/specs/" + channel["spec"])).status_code == 200


async def test_spec_delete_rechecks_session_after_reading_confirmation(members: Members) -> None:
    """Revocation after route admission but before mutation invalidates otherwise correct consent."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])

    async def revoked(request: Request, limit: int) -> dict[str, Any]:
        """Complete body parsing before revoking the initiating browser's session."""
        data = await read_object(request, limit)
        assert (await members.first.post("/api/logout")).status_code == 200
        return data

    with (
        patch("gryphon.saas_spec_delete_api.read_object", side_effect=revoked),
        patch.object(members.app.state.store, "delete_spec", AsyncMock()) as delete,
    ):
        response = await members.first.request(
            "DELETE", prefix + "/specs/" + channel["spec"], json=_confirmation(preview)
        )
    assert response.status_code == 401
    delete.assert_not_awaited()


async def test_spec_deletion_preview_rechecks_authority_before_response(members: Members) -> None:
    """Metadata gathered before session revocation is not disclosed after authority has expired."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    store = members.app.state.store
    original = store.preview_spec_deletion

    async def revoked(tenant_id: str, spec_id: str) -> SpecDeletionPreview:
        """Invalidate the browser after reading its own resource but before returning it."""
        preview: SpecDeletionPreview = await original(tenant_id, spec_id)
        assert (await members.first.post("/api/logout")).status_code == 200
        return preview

    with patch.object(store, "preview_spec_deletion", side_effect=revoked):
        response = await members.first.get(
            "/api/tenants/" + members.tenants[0] + "/specs/" + channel["spec"] + "/deletion"
        )
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


async def test_confirmed_delete_cancellation_joins_runtime_retirement(members: Members) -> None:
    """Cancelling response delivery cannot detach cleanup after a confirmed deletion commits."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    preview = await _preview(members.first, prefix, channel["spec"])
    async with Client(channel["url"], auth=channel["token"]) as client:
        await _data(client, "list_servers")
    manager = members.app.state.runtimes
    broker = manager._runtimes[channel["channel"]].broker
    original = manager.invalidate
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(channel_id: str, *, before_revision: int | None = None) -> None:
        """Expose deterministic cleanup ownership without touching the operator's runtime."""
        entered.set()
        await release.wait()
        await original(channel_id, before_revision=before_revision)

    request = Request({"type": "http", "path_params": {"tenant_id": members.tenants[0], "spec_id": channel["spec"]}})
    with (
        patch("gryphon.saas_spec_delete_api.read_object", AsyncMock(return_value=_confirmation(preview))),
        patch.object(members.app.state.admin.access, "authorize", AsyncMock(return_value=None)),
        patch.object(manager, "invalidate", side_effect=stalled),
    ):
        task = asyncio.create_task(members.app.state.admin.spec_deletion.delete(request))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert broker is not None and broker._closed
    assert (await members.admin.get(prefix + "/specs/" + channel["spec"])).status_code == 404
