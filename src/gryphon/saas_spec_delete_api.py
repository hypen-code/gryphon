"""Authenticated deletion previews and exact-name consent for whole specifications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_http import fields, read_object

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.saas_access import AccessControl
    from gryphon.saas_runtime import ChannelRuntimeManager
    from gryphon.saas_store import SaaSStore


class SpecDeletionAPI:
    """Keep consent validation, tenant authority and runtime retirement outside presentation code."""

    def __init__(self, store: SaaSStore, access: AccessControl, runtimes: ChannelRuntimeManager) -> None:
        """Borrow the existing authenticated control-plane dependencies."""
        self.store, self.access, self.runtimes = store, access, runtimes

    async def preview(self, request: Request) -> Response:
        """Return current version and channel impact without mutating any stored resource."""
        preview = await self.store.preview_spec_deletion(
            request.path_params["tenant_id"], request.path_params["spec_id"]
        )
        denied = await self.access.authorize(request)
        return denied if denied is not None else JSONResponse(preview.model_dump(mode="json"))

    async def delete(self, request: Request) -> Response:
        """Require closed consent fields and freshly verified authority before starting deletion."""
        data = await read_object(request, 4096)
        fields(data, {"confirm_name", "confirmation_token"})
        denied = await self.access.authorize(request)
        if denied is not None:
            return denied
        result = await finish_cleanup(
            self._publish(request.path_params["tenant_id"], request.path_params["spec_id"], data)
        )
        return JSONResponse(result)

    async def _publish(self, tenant_id: str, spec_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Commit validated consent and await every affected runtime's retirement before returning."""
        preview, channels = await self.store.delete_spec(
            tenant_id, spec_id, data["confirm_name"], data["confirmation_token"]
        )
        for channel in channels:
            await self.runtimes.invalidate(channel.id, before_revision=channel.revision)
        return {
            "deleted": True,
            "specification_id": preview.specification_id,
            "deleted_spec_ids": preview.version_ids,
            "updated_channel_ids": [channel.id for channel in channels],
        }
