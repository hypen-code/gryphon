"""Scoped browser review and publication of explicit POST-read execution approvals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response

from gryphon.errors import SaaSDisabledError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_access import platform_admin
from gryphon.saas_http import failure, fields, read_object

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.saas_api import AdminAPI


class PostReadAPI:
    """Require verified platform authority for grants; permit tenant-scoped status inspection."""

    def __init__(self, admin: AdminAPI) -> None:
        """Borrow existing lifecycle, session, import-admission and publication dependencies."""
        self.admin = admin

    async def manage(self, request: Request) -> Response:
        """Review canonical routes or replace explicit grants without trusting body-supplied authority."""
        if request.method == "POST" and not platform_admin(request):
            return failure("forbidden", 403)
        tenant_id, spec_id = request.path_params["tenant_id"], request.path_params["spec_id"]
        previous = await self.admin.store.get_spec(tenant_id, spec_id)
        if not (await self.admin.store.get_tenant(tenant_id)).enabled:
            raise SaaSDisabledError("Tenant is disabled")
        if request.method == "GET":
            items = await self.admin.importer.post_read_candidates(previous)
            denied = await self.admin.access.authorize(request)
            return denied if denied is not None else JSONResponse({"items": items})
        data = await read_object(request, 65536)
        fields(data, {"functions"})
        selected = await self.admin.importer.select_post_reads(previous, data["functions"])
        snapshot = previous.model_copy(deep=True, update={"approved_post_reads": selected})
        imported = await self.admin.importer.refilter(snapshot, snapshot.read_only_filter)
        denied = await self.admin.access.platform(request)
        if denied is not None:
            return denied
        item = await finish_cleanup(self.admin._publish_spec(tenant_id, spec_id, imported, True))
        return JSONResponse(item.model_dump(exclude={"document"}), status_code=200 if item.id == spec_id else 201)
