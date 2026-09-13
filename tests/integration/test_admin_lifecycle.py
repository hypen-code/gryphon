"""Confirmed tenant, channel and user deletion through real authenticated HTTP."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _data
from test_saas_user_http import PASSWORD, POLICY, Members, _publish, _user
from test_saas_user_http import members as members


async def preview(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    """Request current server-side impact without granting any deletion authority."""
    response = await client.get(path + "/deletion")
    assert response.status_code == 200
    assert "password_hash" not in response.text and "key_digest" not in response.text
    return dict(response.json())


def consent(item: dict[str, Any]) -> dict[str, str]:
    """Provide only the exact typed identifier and opaque current-impact token."""
    return {"confirm_name": item["name"], "confirmation_token": item["confirmation_token"]}


async def test_channel_delete_requires_name_revokes_key_and_preserves_specs(members: Members) -> None:
    """Deleting a channel removes its authority, not the API definitions or local execution files."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    prefix = "/api/tenants/" + members.tenants[0]
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(
            client, "execute_code", {"code": 'result = "x" * 70000', "description": "Retained local data"}
        )
        assert result["success"] and result["artifact_id"]
    runtime = members.app.state.runtimes._runtimes[channel["channel"]]
    broker = runtime.broker
    run_file = Path(members.app.state.runtimes._state_dir) / "channels" / channel["channel"] / "runs.db"
    impact = await preview(members.first, channel["path"])
    invalid = consent(impact) | {"confirm_name": "Weather"}
    assert (await members.first.request("DELETE", channel["path"], json=invalid)).status_code == 400
    response = await members.first.request("DELETE", channel["path"], json=consent(impact))
    assert response.status_code == 200 and response.json() == {"deleted": True, "channel_id": channel["channel"]}
    assert broker is not None and broker._closed and run_file.is_file()
    assert (await members.first.get(prefix + "/specs/" + channel["spec"])).status_code == 200
    assert (await members.first.get(prefix + "/channels")).json()["items"] == []
    assert (
        await members.first.post(channel["url"], headers={"authorization": "Bearer " + channel["token"]}, json={})
    ).status_code == 401
    event = next(
        item for item in (await members.first.get("/api/audit")).json()["items"] if item["event"] == "channel_deleted"
    )
    assert event["actor"]["id"] == members.users[0].id and event["channel_id"] == channel["channel"]
    assert (await members.first.request("DELETE", channel["path"], json=consent(impact))).status_code == 404


async def test_tenant_suspend_resume_then_delete_entire_workspace(members: Members) -> None:
    """Suspension preserves resources; confirmed deletion cascades only the selected workspace."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    foreign = await _publish(members.second, members.tenants[1], "inventory")
    path = "/api/tenants/" + members.tenants[0]
    suspended = await members.admin.patch(path, json={"enabled": False})
    assert suspended.status_code == 200 and not suspended.json()["enabled"]
    assert (await members.first.get("/api/me")).status_code == 401
    assert (
        await members.first.post(channel["url"], headers={"authorization": "Bearer " + channel["token"]}, json={})
    ).status_code == 401
    assert (await members.admin.get(path + "/specs/" + channel["spec"])).status_code == 200
    assert (await members.admin.patch(path, json={"enabled": True})).status_code == 200
    async with Client(channel["url"], auth=channel["token"]) as client:
        assert (await _data(client, "execute_code", {"code": "result = 7", "description": "Resumed"}))["data"] == 7
    assert (await members.first.get("/api/me")).status_code == 401
    impact = await preview(members.admin, path)
    assert impact["impact"] == {"users": 1, "channels": 1, "specs": 1}
    response = await members.admin.request("DELETE", path, json=consent(impact))
    assert response.status_code == 200 and response.json() == {"deleted": True, "tenant_id": members.tenants[0]}
    assert [item["id"] for item in (await members.admin.get("/api/tenants")).json()["items"]] == [members.tenants[1]]
    assert await members.app.state.admin.users.get_user(members.users[0].id) is None
    assert (await members.admin.get(path + "/specs/" + channel["spec"])).status_code == 404
    assert (await members.admin.get(path + "/deletion")).status_code == 404
    assert (
        await members.admin.post(channel["url"], headers={"authorization": "Bearer " + channel["token"]}, json={})
    ).status_code == 401
    async with Client(foreign["url"], auth=foreign["token"]) as client:
        assert (await _data(client, "execute_code", {"code": "result = 9", "description": "Unrelated"}))["data"] == 9
    events = (await members.admin.get("/api/audit")).json()["items"]
    deletion = next(item for item in events if item["event"] == "tenant_deleted")
    assert deletion["tenant_id"] == members.tenants[0] and deletion["actor"]["username"] == "persisted-admin"
    assert any(item["event"] == "user_deleted" and item["subject_id"] == members.users[0].id for item in events)
    assert any(item["event"] == "tenant_created" and item["tenant_id"] == members.tenants[0] for item in events)


async def test_user_delete_revokes_sessions_but_not_shared_channel_keys(members: Members) -> None:
    """Deleted account identifiers cannot authenticate again or acquire a recreated username's identity."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    user = members.users[0]
    path = "/api/users/" + user.id
    impact = await preview(members.admin, path)
    assert impact["name"] == user.username
    deleted = await members.admin.request("DELETE", path, json=consent(impact))
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True, "user_id": user.id}
    assert (await members.first.get("/api/me")).status_code == 401
    assert (
        await members.first.post("/api/login", json={"username": user.username, "password": PASSWORD})
    ).status_code == 401
    async with Client(channel["url"], auth=channel["token"]) as client:
        assert (
            await _data(client, "execute_code", {"code": "result = 7", "description": "Shared key still independent"})
        )["data"] == 7
    new = await _user(members.admin, user.username, members.tenants[0])
    assert new.id != user.id
    events = (await members.admin.get("/api/audit")).json()["items"]
    deletion = next(item for item in events if item["event"] == "user_deleted")
    assert deletion["subject_id"] == user.id and deletion["actor"]["username"] == "persisted-admin"
    assert any(item["event"] == "user_created" and item["subject_id"] == user.id for item in events)
    assert PASSWORD not in str(events) and "password_hash" not in str(events)


@pytest.mark.parametrize("resource", ["tenant", "channel", "user"])
async def test_lifecycle_delete_requires_current_consent_and_closed_fields(members: Members, resource: str) -> None:
    """Exact names, fresh impact and closed request bodies are enforced beyond the browser."""
    channel = await _publish(members.first, members.tenants[0], "weather")
    path = {
        "tenant": "/api/tenants/" + members.tenants[0],
        "channel": channel["path"],
        "user": "/api/users/" + members.users[0].id,
    }[resource]
    impact = await preview(members.admin, path)
    for body in [
        {},
        {"confirm_name": impact["name"]},
        consent(impact) | {"confirm_name": impact["name"] + " "},
        consent(impact) | {"actor_id": "bootstrap"},
    ]:
        assert (await members.admin.request("DELETE", path, json=body)).status_code == 400
    if resource == "user":
        assert (await members.admin.patch(path, json={"name": "Updated name"})).status_code == 200
    else:
        assert (
            await members.first.patch(
                channel["path"], json=POLICY | {"name": "changed", "spec_ids": [channel["spec"]], "enabled": True}
            )
        ).status_code == 200
    assert (await members.admin.request("DELETE", path, json=consent(impact))).status_code == 409
    assert (await preview(members.admin, path))["confirmation_token"] != impact["confirmation_token"]


async def test_lifecycle_roles_foreign_scopes_csrf_and_self_delete(members: Members) -> None:
    """Only administrators delete tenants/users; ordinary members remain strictly tenant-scoped."""
    channel = await _publish(members.second, members.tenants[1], "foreign")
    tenant_path = "/api/tenants/" + members.tenants[0]
    user_path = "/api/users/" + members.users[0].id
    for path in (tenant_path, user_path):
        assert (await members.first.get(path + "/deletion")).status_code == 403
        assert (await members.first.request("DELETE", path, json={})).status_code == 403
    assert (await members.first.get(channel["path"] + "/deletion")).status_code == 404
    assert (await members.first.request("DELETE", channel["path"], json={})).status_code == 404
    for path in (tenant_path, user_path, channel["path"]):
        assert (
            await members.admin.request("DELETE", path, json={}, headers={"x-csrf-token": "wrong"})
        ).status_code == 403
        async with httpx.AsyncClient(base_url=members.admin.base_url) as anonymous:
            assert (await anonymous.get(path + "/deletion")).status_code == 401
            assert (await anonymous.request("DELETE", path, json={})).status_code == 401
    current = (await members.admin.get("/api/me")).json()["user"]
    await _user(members.admin, "another-admin")
    assert (await members.admin.get("/api/users/" + current["id"] + "/deletion")).status_code == 409
    assert await members.app.state.admin.users.get_user(current["id"]) is not None


async def test_tenant_new_child_invalidates_delete_preview(members: Members) -> None:
    """A newly assigned user is not silently included in a stale tenant deletion confirmation."""
    path = "/api/tenants/" + members.tenants[0]
    impact = await preview(members.admin, path)
    await _user(members.admin, "later-member", members.tenants[0])
    assert (await members.admin.request("DELETE", path, json=consent(impact))).status_code == 409
    assert (await members.first.get("/api/me")).status_code == 200


async def test_delete_rechecks_platform_session_before_mutation(members: Members) -> None:
    """Correct consent is not reusable after the administrator's session is revoked."""
    path = "/api/tenants/" + members.tenants[0]
    impact = await preview(members.admin, path)
    assert (await members.admin.post("/api/logout")).status_code == 200
    with patch.object(members.app.state.admin.store, "delete_tenant", AsyncMock()) as delete:
        assert (await members.admin.request("DELETE", path, json=consent(impact))).status_code == 401
        delete.assert_not_awaited()
