"""Real HTTP confirmed user deletion and browser authorization boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from test_saas_user_http import PASSWORD, Members, _login, _publish, _user
from test_saas_user_http import members as members

from gryphon.saas_auth import COOKIE_NAME

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.models.users import UserAccount


async def _consent(admin: httpx.AsyncClient, user: UserAccount) -> dict[str, str]:
    """Obtain the current server fingerprint and copy only closed consent fields."""
    response = await admin.get(f"/api/users/{user.id}/deletion")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == user.username and data["label"] == user.name and data["kind"] == "user"
    assert data["impact"] == {"users": 1, "channels": 0, "specs": 0}
    assert PASSWORD not in response.text and "password_hash" not in response.text
    return {"confirm_name": user.username, "confirmation_token": data["confirmation_token"]}


async def test_user_delete_http_revokes_cookies_but_preserves_shared_channel_key(members: Members) -> None:
    """Deletion returns only the removed UUID and rejects old sessions even after username reuse."""
    user = members.users[0]
    channel = await _publish(members.first, members.tenants[0], "shared")
    cookie = members.first.cookies.get(COOKIE_NAME)
    consent = await _consent(members.admin, user)
    response = await members.admin.request("DELETE", f"/api/users/{user.id}", json=consent)
    assert response.status_code == 200 and response.json() == {"deleted": True, "user_id": user.id}
    assert cookie is not None and members.app.state.admin.sessions.identity(cookie) is None
    assert (await members.first.get("/api/me")).status_code == 401
    assert (
        await members.first.post("/api/login", json={"username": user.username, "password": PASSWORD})
    ).status_code == 401
    assert await members.app.state.store.lookup_key(channel["token"]) is not None
    replacement = await _user(members.admin, user.username, user.tenant_id)
    assert replacement.id != user.id
    assert (await members.first.get("/api/me", headers={"cookie": f"{COOKIE_NAME}={cookie}"})).status_code == 401
    await _login(members.first, replacement.username)
    assert (await members.first.get("/api/me")).json()["user"]["id"] == replacement.id
    events = (await members.admin.get("/api/audit")).json()["items"]
    event = next(event for event in events if event["event"] == "user_deleted")
    assert event["subject_id"] == user.id and event["subject_username"] == user.username
    assert event["subject_name"] == user.name and event["actor"]["display_source"] == "snapshot"
    assert not any(
        event.get("subject_id") == user.id for event in (await members.second.get("/api/audit")).json()["items"]
    )


async def test_user_delete_http_rejects_anonymous_tenant_roles_foreign_targets_and_csrf(members: Members) -> None:
    """Neither claims nor own-account targets bypass platform guards or session-bound CSRF."""
    async with httpx.AsyncClient(base_url=members.admin.base_url) as anonymous:
        for user in members.users:
            path = f"/api/users/{user.id}"
            for browser, status in ((anonymous, 401), (members.first, 403), (members.second, 403)):
                assert (
                    await browser.get(path + "/deletion", headers={"x-role": "platform_admin"})
                ).status_code == status
                assert (await browser.request("DELETE", path, json={})).status_code == status
    consent = await _consent(members.admin, members.users[0])
    assert (
        await members.admin.request(
            "DELETE", f"/api/users/{members.users[0].id}", json=consent, headers={"x-csrf-token": "forged"}
        )
    ).status_code == 403
    assert await members.app.state.admin.users.get_user(members.users[0].id) == members.users[0]


@pytest.mark.parametrize(
    "change",
    [
        {"confirm_name": None},
        {"confirm_name": []},
        {"confirm_name": "FIRST-MEMBER"},
        {"confirm_name": "first-member "},
        {"confirmation_token": 1},
        {"confirmation_token": "0" * 63},
        {"actor_id": "bootstrap"},
    ],
)
async def test_user_delete_http_requires_closed_exact_bounded_consent(
    members: Members, change: dict[str, object]
) -> None:
    """Malformed consent is a safe validation failure with no account mutation."""
    user = members.users[0]
    consent = await _consent(members.admin, user)
    response = await members.admin.request("DELETE", f"/api/users/{user.id}", json=consent | change)
    assert response.status_code == 400 and response.json() == {"error": "validation"}
    assert await members.app.state.admin.users.get_user(user.id) == user


async def test_user_delete_http_stale_confirmation_missing_and_protected_accounts(members: Members) -> None:
    """Stale consent requires a fresh preview; self and last-enabled-admin previews fail safely."""
    user = members.users[0]
    consent = await _consent(members.admin, user)
    await members.admin.patch(f"/api/users/{user.id}", json={"name": "Updated display"})
    response = await members.admin.request("DELETE", f"/api/users/{user.id}", json=consent)
    assert response.status_code == 409 and response.json() == {"error": "conflict"}
    assert (await members.admin.get("/api/users/missing/deletion")).status_code == 404
    assert (await members.admin.request("DELETE", "/api/users/missing", json=consent)).status_code == 404
    admin = (await members.admin.get("/api/me")).json()["user"]
    assert (await members.admin.get(f"/api/users/{admin['id']}/deletion")).status_code == 409
    response = await members.admin.request(
        "DELETE",
        f"/api/users/{admin['id']}",
        json={
            "confirm_name": admin["username"],
            "confirmation_token": "0" * 64,
        },
    )
    assert response.status_code == 409
    async with httpx.AsyncClient(base_url=members.admin.base_url) as bootstrap:
        login = await bootstrap.post(
            "/api/login", json={"token": members.app.state.admin.config.admin_token.get_secret_value()}
        )
        bootstrap.headers["x-csrf-token"] = login.json()["csrf_token"]
        assert (await bootstrap.get(f"/api/users/{admin['id']}/deletion")).status_code == 409


async def test_user_delete_http_rechecks_platform_after_body_read(members: Members) -> None:
    """A concurrently disabled administrator cannot complete deletion using earlier authorization."""
    from gryphon.saas_http import read_object

    user = members.users[0]
    consent = await _consent(members.admin, user)
    admin = (await members.admin.get("/api/me")).json()["user"]

    async def changed(request: Request, limit: int) -> dict[str, object]:
        """Revoke the acting account after its guarded request begins reading consent."""
        body = await read_object(request, limit)
        await members.app.state.admin.users.update_user(admin["id"], enabled=False, actor_id="bootstrap")
        return dict(body)

    with patch("gryphon.saas_user_api.read_object", changed):
        response = await members.admin.request("DELETE", f"/api/users/{user.id}", json=consent)
    assert response.status_code == 401 and await members.app.state.admin.users.get_user(user.id) == user


async def test_user_delete_http_rechecks_platform_before_preview_output(members: Members) -> None:
    """Account details are not serialized after preview work invalidates the acting cookie."""
    users = members.app.state.admin.users
    original = users.preview_user_deletion
    admin = (await members.admin.get("/api/me")).json()["user"]

    async def changed(user_id: str, actor_id: str) -> dict[str, object]:
        """Advance administrator revision during preview work without changing the target."""
        preview = await original(user_id, actor_id)
        await users.update_user(admin["id"], name="Changed admin", actor_id="bootstrap")
        return dict(preview)

    with patch.object(users, "preview_user_deletion", changed):
        response = await members.admin.get(f"/api/users/{members.users[0].id}/deletion")
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
