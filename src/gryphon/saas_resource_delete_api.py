"""Verified tenant/channel deletion and completion-owned access retirement."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_access import actor_id
from gryphon.saas_http import fields, read_object

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.models import Channel
    from gryphon.saas_access import AccessControl
    from gryphon.saas_auth import AdminSessions
    from gryphon.saas_runtime import ChannelRuntimeManager
    from gryphon.saas_store import SaaSStore


class ResourceDeletionAPI:
    """Require typed, current impact consent without trusting client role or resource lists."""

    def __init__(
        self, store: SaaSStore, access: AccessControl, sessions: AdminSessions, runtimes: ChannelRuntimeManager
    ) -> None:
        """Borrow existing authorization, session and runtime dependencies."""
        self.store, self.access, self.sessions, self.runtimes = store, access, sessions, runtimes

    async def _authorize(self, request: Request) -> Response | None:
        """Only platform administrators can remove tenants; channels retain their tenant boundary."""
        return await (
            self.access.authorize(request) if "channel_id" in request.path_params else self.access.platform(request)
        )

    async def preview(self, request: Request) -> Response:
        """Read one consistent deletion impact and recheck authority before disclosing it."""
        denied = await self._authorize(request)
        if denied is not None:
            return denied
        tenant_id, channel_id = request.path_params["tenant_id"], request.path_params.get("channel_id")
        preview = (
            await self.store.preview_channel_deletion(tenant_id, channel_id)
            if channel_id is not None
            else await self.store.preview_tenant_deletion(tenant_id)
        )
        denied = await self._authorize(request)
        return denied if denied is not None else JSONResponse(preview)

    async def delete(self, request: Request) -> Response:
        """Validate the closed consent body, then finish committed cleanup despite disconnects."""
        denied = await self._authorize(request)
        if denied is not None:
            return denied
        data = await read_object(request, 4096)
        fields(data, {"confirm_name", "confirmation_token"})
        denied = await self._authorize(request)
        if denied is not None:
            return denied
        result = await finish_cleanup(
            self._publish(
                request.path_params["tenant_id"], request.path_params.get("channel_id"), data, actor_id(request)
            )
        )
        return JSONResponse(result)

    async def _publish(
        self, tenant_id: str, channel_id: str | None, data: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Delete only the confirmed resources and revoke all affected access before success."""
        if channel_id is not None:
            channel = await self.store.delete_channel(
                tenant_id, channel_id, data["confirm_name"], data["confirmation_token"]
            )
            await self._retire([channel])
            return {"deleted": True, "channel_id": channel.id}
        tenant, channels, users = await self.store.delete_tenant(
            tenant_id, data["confirm_name"], data["confirmation_token"], actor
        )
        for user in users:
            self.sessions.revoke_user(user.id, before_revision=user.revision + 1)
        await self._retire(channels)
        return {"deleted": True, "tenant_id": tenant.id}

    async def _retire(self, channels: list[Channel]) -> None:
        """Revoke every committed deleted revision, including cold snapshots, before awaiting closure."""
        outcomes = await asyncio.gather(
            *(self.runtimes.invalidate(channel.id, before_revision=channel.revision + 1) for channel in channels),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
