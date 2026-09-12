"""Hosted URL import authorization and failure atomicity with isolated synthetic network traffic."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from test_saas_spec_refresh import _spec
from test_saas_user_http import POLICY, Members
from test_saas_user_http import members as members

from gryphon.errors import ExecutionError

if TYPE_CHECKING:
    from gryphon.models import Channel, SaaSSpec

_URL = "https://example.com/openapi.json"


async def _import(http: httpx.AsyncClient, tenant: str) -> str:
    """Create remote provenance through authenticated HTTP without contacting the advertised origin."""
    with patch(
        "gryphon.saas_spec_import.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json=_spec("old")))
    ) as network:
        response = await http.post(
            f"/api/tenants/{tenant}/specs", json={"name": "remote", "url": _URL, "kind": "openapi"}
        )
    assert response.status_code == 201
    network.assert_awaited_once()
    spec_id: str = response.json()["id"]
    return spec_id


async def _bound(members: Members) -> str:
    """Bind an own-tenant remote import so failure assertions cover channel revisions too."""
    tenant = members.tenants[0]
    spec_id = await _import(members.first, tenant)
    response = await members.first.post(f"/api/tenants/{tenant}/channels", json=POLICY | {"spec_ids": [spec_id]})
    assert response.status_code == 201
    return spec_id


async def _snapshot(members: Members) -> tuple[list[SaaSSpec], list[Channel]]:
    """Read stored snapshots directly even after a browser session has been revoked."""
    store = members.app.state.store
    return await store.list_specs(members.tenants[0]), await store.list_channels(members.tenants[0])


async def test_member_own_url_import_refresh_allowed(members: Members) -> None:
    """Tenant users may import and refresh their own URL catalogs with exact binding replacement."""
    spec_id = await _bound(members)
    before_specs, before_channels = await _snapshot(members)
    with patch(
        "gryphon.saas_spec_import.NetworkClient.request", AsyncMock(return_value=httpx.Response(200, json=_spec("new")))
    ) as network:
        response = await members.first.post(
            f"/api/tenants/{members.tenants[0]}/specs/{spec_id}/refresh", json={"update_channels": True}
        )
    assert response.status_code == 201 and response.json()["parent_id"] == spec_id
    network.assert_awaited_once()
    assert network.await_args is not None and network.await_args.args == ("GET", _URL)
    after_specs, after_channels = await _snapshot(members)
    assert before_specs[0] in after_specs and len(after_specs) == 2
    assert after_channels[0].spec_ids == [response.json()["id"]]
    assert after_channels[0].revision == before_channels[0].revision + 1


async def test_member_foreign_tenant_and_forged_spec_denied_before_network(members: Members) -> None:
    """Neither foreign tenant paths nor foreign/forged IDs under the own prefix initiate fetching."""
    foreign_id = await _import(members.second, members.tenants[1])
    own, foreign = (f"/api/tenants/{tenant}/specs" for tenant in members.tenants)
    requests = [
        (foreign, {"name": "remote", "url": _URL, "kind": "openapi"}),
        (foreign + f"/{foreign_id}/refresh", {}),
        (own + f"/{foreign_id}/refresh", {}),
        (own + f"/{uuid4()}/refresh", {}),
    ]
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        for path, payload in requests:
            response = await members.first.post(
                path, json=payload, headers={"x-role": "platform_admin", "x-tenant-id": members.tenants[1]}
            )
            assert response.status_code == 404 and response.json() == {"error": "not_found"}
    network.assert_not_called()
    assert await _snapshot(members) == ([], [])


async def test_disabled_tenant_blocks_member_and_admin_fetch(members: Members) -> None:
    """Disabled tenants deny both URL import and refresh before creating a network client."""
    spec_id = await _bound(members)
    before = await _snapshot(members)
    prefix = f"/api/tenants/{members.tenants[0]}"
    assert (await members.admin.patch(prefix, json={"enabled": False})).status_code == 200
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        for browser, status in ((members.first, 401), (members.admin, 409)):
            for suffix, payload in (
                ("/specs", {"name": "remote", "url": _URL, "kind": "openapi"}),
                (f"/specs/{spec_id}/refresh", {"update_channels": True}),
            ):
                assert (await browser.post(prefix + suffix, json=payload)).status_code == status
    network.assert_not_called()
    after = await _snapshot(members)
    assert after[0] == before[0] and after[1][0].spec_ids == before[1][0].spec_ids


async def test_disabled_admin_account_denied_before_fetch(members: Members) -> None:
    """A disabled persisted administrator cannot fetch despite retaining its old browser cookie."""
    spec_id = await _bound(members)
    operator = (await members.admin.get("/api/me")).json()["user"]
    await members.app.state.admin.users.update_user(operator["id"], enabled=False, actor_id=operator["id"])
    prefix = f"/api/tenants/{members.tenants[0]}/specs"
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        for path, payload in (
            (prefix, {"name": "remote", "url": _URL, "kind": "openapi"}),
            (prefix + f"/{spec_id}/refresh", {}),
        ):
            assert (await members.admin.post(path, json=payload)).status_code == 401
    network.assert_not_called()


@pytest.mark.parametrize("refresh", [False, True])
async def test_revoked_browser_during_fetch_does_not_publish(members: Members, refresh: bool) -> None:
    """Post-fetch authorization rejects a session revoked while remote bytes were unavailable."""
    spec_id = await _bound(members)
    before = await _snapshot(members)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled(*args: object, **kwargs: object) -> httpx.Response:
        """Expose an event barrier instead of relying on network timing."""
        entered.set()
        await release.wait()
        return httpx.Response(200, json=_spec("new"))

    prefix = f"/api/tenants/{members.tenants[0]}/specs"
    path = prefix + f"/{spec_id}/refresh" if refresh else prefix
    payload = {"update_channels": True} if refresh else {"name": "another", "url": _URL, "kind": "openapi"}
    with patch("gryphon.saas_spec_import.NetworkClient.request", side_effect=stalled) as network:
        task = asyncio.create_task(members.first.post(path, json=payload))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                assert (await members.first.post("/api/logout")).status_code == 200
                release.set()
                response = await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    network.assert_awaited_once()
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert await _snapshot(members) == before


async def test_refresh_fetch_failure_leaves_version_binding_and_audit_unchanged(members: Members) -> None:
    """A failed remote refresh has no specification, binding, revision or audit mutation."""
    spec_id = await _bound(members)
    before = await _snapshot(members)
    audit = await members.app.state.store.list_audit(members.tenants[0])
    with patch(
        "gryphon.saas_spec_import.NetworkClient.request", AsyncMock(side_effect=ExecutionError("synthetic failure"))
    ) as network:
        response = await members.first.post(
            f"/api/tenants/{members.tenants[0]}/specs/{spec_id}/refresh", json={"update_channels": True}
        )
    network.assert_awaited_once()
    assert response.status_code == 400 and response.json() == {"error": "validation"}
    assert await _snapshot(members) == before
    assert await members.app.state.store.list_audit(members.tenants[0]) == audit


@pytest.mark.parametrize("kind", [None, False, 7, [], {}, ["openapi"]])
async def test_import_invalid_kind_http_rejected_before_network(members: Members, kind: object) -> None:
    """Malformed JSON kinds produce a closed input error, not a server error or fetch."""
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        response = await members.first.post(
            f"/api/tenants/{members.tenants[0]}/specs", json={"name": "remote", "url": _URL, "kind": kind}
        )
    assert response.status_code == 400
    network.assert_not_called()
    assert await _snapshot(members) == ([], [])


async def test_ucp_http_url_rejected_before_network(members: Members) -> None:
    """Hosted UCP ingestion rejects an insecure profile URL before network construction."""
    with patch("gryphon.saas_spec_import.NetworkClient") as network:
        response = await members.first.post(
            f"/api/tenants/{members.tenants[0]}/specs",
            json={"name": "shop", "url": "http://shop.example/profile", "kind": "ucp"},
        )
    assert response.status_code == 400
    network.assert_not_called()
    assert await _snapshot(members) == ([], [])
