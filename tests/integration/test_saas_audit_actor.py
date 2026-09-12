"""Real HTTP verified actor attribution without exposing unrelated identities or credentials."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

from test_saas_user_http import PASSWORD, POLICY, Members, _login, _publish
from test_saas_user_http import members as members

from gryphon.errors import SaaSStoreError
from gryphon.models import AuditActor, AuditEvent, UserAccount
from gryphon.saas_audit import current_actor

if TYPE_CHECKING:
    from gryphon.saas_store import SaaSStore


def _actor(user: UserAccount) -> dict[str, object]:
    """Require the exact safe snapshot contract, not a permissive subset of account fields."""
    return {"id": user.id, "username": user.username, "name": user.name, "kind": "user", "display_source": "snapshot"}


async def test_audit_resource_events_snapshot_verified_actor_after_rename(members: Members) -> None:
    """All resource changes retain request-time names and durable actor IDs after an account rename."""
    resource = await _publish(members.first, members.tenants[0], "weather")
    assert (await members.first.patch(resource["path"], json=POLICY | {"enabled": True})).status_code == 200
    assert (await members.first.post(resource["path"] + "/revoke")).status_code == 200
    renamed = await members.admin.patch("/api/users/" + members.users[0].id, json={"name": "New display name"})
    assert renamed.status_code == 200
    await _login(members.first, members.users[0].username)
    response = await members.first.get("/api/audit")
    events = response.json()["items"]
    resource_events = [item for item in events if "channel_id" in item and item["event"] != "tenant_created"]
    assert {item["event"] for item in resource_events} == {
        "spec_created",
        "channel_created",
        "channel_updated",
        "key_rotated",
        "key_revoked",
    }
    assert all(item["actor"] == _actor(members.users[0]) for item in resource_events)
    admin = UserAccount.model_validate((await members.admin.get("/api/me")).json()["user"])
    tenant_event = next(item for item in events if item["event"] == "tenant_created")
    assert tenant_event["actor"] == _actor(admin)
    assert all(item["tenant_id"] == members.tenants[0] for item in events)
    assert members.users[1].id not in response.text and members.users[1].name not in response.text
    assert PASSWORD not in response.text and resource["token"] not in response.text
    assert "password_hash" not in response.text and "pbkdf2" not in response.text
    assert current_actor() == AuditActor()


async def test_audit_concurrent_requests_do_not_mix_actors_or_accept_headers(members: Members) -> None:
    """Overlapping mutations keep each server-verified actor despite competing claimed identities."""
    store: SaaSStore = members.app.state.store
    channels = [await store.create_channel(tenant, "Compute") for tenant in members.tenants]
    barrier = asyncio.Barrier(2)
    original = store.rotate_key

    async def overlapping(tenant_id: str, channel_id: str) -> str:
        """Ensure both independently authenticated handlers are live before either can persist."""
        await barrier.wait()
        return await original(tenant_id, channel_id)

    with patch.object(store, "rotate_key", overlapping):
        async with asyncio.timeout(5):
            responses = await asyncio.gather(
                *(
                    browser.post(
                        f"/api/tenants/{channel.tenant_id}/channels/{channel.id}/rotate",
                        headers={"x-actor-id": "bootstrap", "x-user-id": members.users[1 - index].id},
                    )
                    for index, (browser, channel) in enumerate(
                        zip((members.first, members.second), channels, strict=True)
                    )
                )
            )
    assert all(response.status_code == 200 for response in responses)
    for browser, user in zip((members.first, members.second), members.users, strict=True):
        events = (await browser.get("/api/audit")).json()["items"]
        rotated = [item for item in events if item["event"] == "key_rotated"]
        assert len(rotated) == 1 and rotated[0]["actor"] == _actor(user)
    assert current_actor() == AuditActor()


async def test_audit_filter_cleanup_task_retains_request_actor(members: Members) -> None:
    """Immutable version and binding events produced in finish_cleanup carry the initiating user."""
    resource = await _publish(members.first, members.tenants[0], "weather")
    store: SaaSStore = members.app.state.store
    before = {item.id for item in await store.list_audit(members.tenants[0])}
    response = await members.first.post(
        f"/api/tenants/{members.tenants[0]}/specs/{resource['spec']}/filter",
        json={"read_only_filter": False, "update_channels": True},
    )
    assert response.status_code == 201
    events = (await members.first.get("/api/audit")).json()["items"]
    changed = [item for item in events if "channel_id" in item and item["id"] not in before]
    assert {item["event"] for item in changed} == {"spec_created", "channel_updated"}
    assert all(item["actor"] == _actor(members.users[0]) for item in changed)


async def test_audit_rejected_forged_actor_body_has_no_mutation(members: Members) -> None:
    """Body attribution is never accepted, and failed authorization creates no successful event."""
    store: SaaSStore = members.app.state.store
    before = await store.list_audit(members.tenants[0])
    for extra in ({"actor": {"kind": "bootstrap"}}, {"actor_id": members.users[1].id}):
        response = await members.first.post(f"/api/tenants/{members.tenants[0]}/channels", json=POLICY | extra)
        assert response.status_code == 400
    response = await members.second.post(f"/api/tenants/{members.tenants[0]}/channels", json=POLICY)
    assert response.status_code == 404
    response = await members.first.post(
        f"/api/tenants/{members.tenants[0]}/channels", json=POLICY, headers={"x-csrf-token": "forged"}
    )
    assert response.status_code == 403
    assert await store.list_audit(members.tenants[0]) == before
    assert await store.list_channels(members.tenants[0]) == []


async def test_audit_http_failure_rolls_back_and_next_actor_is_clean(members: Members) -> None:
    """Failure after a real attributed insert rolls back and cannot taint the next browser request."""
    store: SaaSStore = members.app.state.store
    before = await store.list_audit(members.tenants[0])
    original = store._audit

    async def fail(tenant_id: str, event: AuditEvent, channel_id: str | None = None) -> None:
        """Insert first, then fail within the same mutation transaction."""
        await original(tenant_id, event, channel_id)
        raise SaaSStoreError("Synthetic failure")

    with patch.object(store, "_audit", fail):
        response = await members.first.post(f"/api/tenants/{members.tenants[0]}/channels", json=POLICY)
    assert response.status_code == 503
    assert await store.list_audit(members.tenants[0]) == before
    assert await store.list_channels(members.tenants[0]) == []
    response = await members.second.post(f"/api/tenants/{members.tenants[1]}/channels", json=POLICY)
    assert response.status_code == 201
    events = (await members.second.get("/api/audit")).json()["items"]
    created = next(item for item in events if item["event"] == "channel_created")
    assert created["actor"] == _actor(members.users[1])
    assert current_actor() == AuditActor()
