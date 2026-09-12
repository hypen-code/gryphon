"""Administrator-only tenant, catalog, channel and usage endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from gryphon.compiler.catalog import module_name
from gryphon.errors import InputValidationError, SaaSDisabledError
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_access import AccessControl, account, platform_admin
from gryphon.saas_analytics import AnalyticsStore
from gryphon.saas_auth import COOKIE_NAME, AdminSessions, RateLimiter
from gryphon.saas_http import admin_endpoint, failure, fields, read_object
from gryphon.saas_spec_import import SpecImporter
from gryphon.saas_user_api import UserAPI
from gryphon.saas_users import UserStore
from gryphon.security.ast_guard import available_imports

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.config import GryphonConfig
    from gryphon.models import SaaSSpec, SpecImport
    from gryphon.saas_config import SaaSConfig
    from gryphon.saas_runtime import ChannelRuntimeManager
    from gryphon.saas_store import SaaSStore

_USAGE_PAGE_SIZE = 100
_MAX_USAGE_ROWS = 2400


def _string(value: Any) -> str:
    """Require a nonempty bounded display name or identifier without control characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise InputValidationError("Invalid text")
    return value.strip()


def _strings(value: Any) -> list[str]:
    """Require a small distinct list rather than coercing arbitrary JSON values."""
    if not isinstance(value, list) or len(value) > 100:
        raise InputValidationError("Invalid list")
    result = [_string(item) for item in value]
    if len(set(result)) != len(result):
        raise InputValidationError("Duplicate list entries")
    return result


class AdminAPI:
    """Keep HTTP handlers separate from storage and channel execution authority."""

    def __init__(
        self,
        config: SaaSConfig,
        base: GryphonConfig,
        store: SaaSStore,
        runtimes: ChannelRuntimeManager,
        *,
        analytics: AnalyticsStore | None = None,
    ) -> None:
        """Compose explicit dependencies without starting services or reading environment state."""
        self.config, self.base, self.store, self.runtimes = config, base, store, runtimes
        self.sessions = AdminSessions(config)
        self.logins = RateLimiter(config.login_attempts_per_minute)
        self.users = UserStore(store._db)
        self.access = AccessControl(self.sessions, self.users, store)
        self.user_api = UserAPI(self.users, self.access, self.sessions, config)
        self.analytics = analytics if analytics is not None else AnalyticsStore(store._db)
        self.importer = SpecImporter(base, config.max_spec_bytes)

    def routes(self) -> list[Route]:
        """Register the closed administrator API; only login is unauthenticated."""
        prefix = "/api/tenants/{tenant_id}"
        definitions = [
            ("/api/session", self.session, ["GET"]),
            ("/api/logout", self.logout, ["POST"]),
            ("/api/settings", self.settings, ["GET"]),
            ("/api/tenants", self.tenants, ["GET", "POST"]),
            (prefix, self.tenant, ["PATCH"]),
            (prefix + "/specs", self.specs, ["GET", "POST"]),
            (prefix + "/specs/{spec_id}", self.spec, ["GET"]),
            (prefix + "/specs/{spec_id}/refresh", self.refresh_spec, ["POST"]),
            (prefix + "/channels", self.channels, ["GET", "POST"]),
            (prefix + "/channels/{channel_id}", self.channel, ["PATCH"]),
            (prefix + "/channels/{channel_id}/rotate", self.rotate, ["POST"]),
            (prefix + "/channels/{channel_id}/revoke", self.revoke, ["POST"]),
            (prefix + "/usage", self.usage, ["GET"]),
            (prefix + "/analytics", self.analytics_report, ["GET"]),
            ("/api/audit", self.audit, ["GET"]),
        ]
        return [Route("/api/login", self.login, methods=["POST"]), *self.user_api.routes()] + [
            Route(path, admin_endpoint(self.sessions, handler, self.access.authorize), methods=methods)
            for path, handler, methods in definitions
        ]

    async def login(self, request: Request) -> Response:
        """Exchange a bootstrap admin token for a fresh HttpOnly session cookie."""
        if not self.logins.accept():
            return failure("rate_limit", 429)
        data = await read_object(request, 4096)
        session = await self._login(data)
        if session is None:
            return failure("unauthorized", 401)
        self.sessions.logout(request.cookies.get(COOKIE_NAME, ""))
        cookie, csrf = session
        response = JSONResponse({"csrf_token": csrf})
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=self.config.session_ttl_seconds,
            httponly=True,
            secure=self.config.secure_cookies,
            samesite="strict",
            path="/api",
        )
        return response

    async def _login(self, data: dict[str, Any]) -> tuple[str, str] | None:
        """Keep bootstrap recovery separate from username/password authentication."""
        if set(data) == {"token"}:
            token = data["token"]
            return self.sessions.login(token) if isinstance(token, str) and len(token) <= 256 else None
        fields(data, {"username", "password"})
        username, password = data["username"], data["password"]
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or len(password) > 128:
            return None
        user = await self.users.authenticate(username, password)
        return (
            self.sessions.login_user(user.id, user.revision, platform=user.role == "platform_admin")
            if user is not None
            else None
        )

    async def session(self, request: Request) -> Response:
        """Recover CSRF state after reload without exposing the HttpOnly session cookie."""
        return JSONResponse({"csrf_token": self.sessions.verify(request.cookies.get(COOKIE_NAME, ""))})

    async def logout(self, request: Request) -> Response:
        """Revoke the current session and expire its browser cookie."""
        self.sessions.logout(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            COOKIE_NAME, path="/api", secure=self.config.secure_cookies, httponly=True, samesite="strict"
        )
        return response

    async def settings(self, request: Request) -> Response:
        """Expose only non-secret UI capability metadata, never operator settings."""
        return JSONResponse(
            {
                "allowed_imports": available_imports(),
                "docker_enabled": self.config.docker_enabled,
                "max_spec_bytes": self.importer.limit,
            }
        )

    async def tenants(self, request: Request) -> Response:
        """List bounded tenant metadata or create a server-owned tenant identity."""
        if request.method == "GET":
            items = await self.access.tenants(request)
            return JSONResponse({"items": [item.model_dump() for item in items]})
        if not platform_admin(request):
            return failure("forbidden", 403)
        data = await read_object(request, 4096)
        fields(data, {"name"})
        return JSONResponse((await self.store.create_tenant(_string(data["name"]))).model_dump(), status_code=201)

    async def tenant(self, request: Request) -> Response:
        """Disable tenant authentication and revoke all active channel runtimes."""
        if not platform_admin(request):
            return failure("forbidden", 403)
        data = await read_object(request, 4096)
        fields(data, {"enabled"})
        if type(data["enabled"]) is not bool:
            raise InputValidationError("Invalid enabled state")
        tenant_id = request.path_params["tenant_id"]
        item = await self.store.set_tenant_enabled(tenant_id, data["enabled"])
        for user in await self.users.list_users(tenant_id=tenant_id):
            self.sessions.revoke_user(user.id, before_revision=user.revision)
        for channel in await self.store.list_channels(tenant_id):
            await self.runtimes.invalidate(channel.id, before_revision=channel.revision)
        return JSONResponse(item.model_dump())

    async def specs(self, request: Request) -> Response:
        """Manage immutable uploaded catalog versions without accepting filesystem paths."""
        tenant_id = request.path_params["tenant_id"]
        if request.method == "GET":
            items = await self.store.list_specs(tenant_id)
            return JSONResponse({"items": [item.model_dump(exclude={"document"}) for item in items]})
        data = await read_object(request, self.config.max_spec_bytes * 2)
        fields(data, {"name", "url", "kind"} if "url" in data else {"name", "content"})
        name = _string(data["name"])
        if not (await self.store.get_tenant(tenant_id)).enabled:
            raise SaaSDisabledError("Tenant is disabled")
        imported = await self.importer.load(
            module_name(name), content=data.get("content"), url=data.get("url"), kind=data.get("kind", "openapi")
        )
        denied = await self.access.authorize(request)
        if denied is not None:
            return denied
        item = await self.store.create_spec(tenant_id, name, imported.document, imported=imported)
        return JSONResponse(item.model_dump(exclude={"document"}), status_code=201)

    async def refresh_spec(self, request: Request) -> Response:
        """Fetch the saved source again or accept replacement bytes, retaining old immutable snapshots."""
        tenant_id, spec_id = request.path_params["tenant_id"], request.path_params["spec_id"]
        previous = await self.store.get_spec(tenant_id, spec_id)
        if not (await self.store.get_tenant(tenant_id)).enabled:
            raise SaaSDisabledError("Tenant is disabled")
        data = await read_object(request, self.config.max_spec_bytes * 2)
        fields(data, {"content"} if previous.source_type == "file" else set(), {"update_channels"})
        update_channels = data.get("update_channels", False)
        if type(update_channels) is not bool:
            raise InputValidationError("Invalid binding update choice")
        imported = await self.importer.load(
            module_name(previous.name),
            content=data.get("content"),
            url=previous.source_url,
            kind="ucp" if previous.source_type == "ucp_url" else "openapi",
        )
        denied = await self.access.authorize(request)
        if denied is not None:
            return denied
        item = await finish_cleanup(self._publish_spec(tenant_id, spec_id, imported, update_channels))
        return JSONResponse(item.model_dump(exclude={"document"}), status_code=200 if item.id == spec_id else 201)

    async def _publish_spec(
        self, tenant_id: str, spec_id: str, imported: SpecImport, update_channels: bool
    ) -> SaaSSpec:
        """Complete revision publication and revoke every replaced runtime before returning."""
        item, channels = await self.store.refresh_spec(tenant_id, spec_id, imported, update_channels=update_channels)
        for channel in channels:
            await self.runtimes.invalidate(channel.id, before_revision=channel.revision)
        return item

    async def spec(self, request: Request) -> Response:
        """Read an immutable specification using both tenant and spec identity."""
        item = await self.store.get_spec(request.path_params["tenant_id"], request.path_params["spec_id"])
        return JSONResponse(item.model_dump())

    async def _channel_data(self, request: Request, *, update: bool = False) -> dict[str, Any]:
        """Validate narrowed import policy and same-tenant bindings before a transaction."""
        data = await read_object(request, 32768)
        required = {"name", "spec_ids", "sandbox_mode", "allowed_imports"}
        fields(data, required | ({"enabled"} if update else set()))
        data["name"], data["spec_ids"] = _string(data["name"]), _strings(data["spec_ids"])
        data["allowed_imports"] = _strings(data["allowed_imports"])
        mode = data["sandbox_mode"]
        if not isinstance(mode, str) or mode not in {"restricted", "docker"}:
            raise InputValidationError("Unsupported sandbox profile")
        if mode == "docker" and not self.config.docker_enabled:
            raise InputValidationError("Unsupported sandbox profile")
        if (mode == "restricted" and data["allowed_imports"]) or not set(data["allowed_imports"]) <= set(
            available_imports()
        ):
            raise InputValidationError("Unsupported import profile")
        if update and type(data["enabled"]) is not bool:
            raise InputValidationError("Invalid enabled state")
        names = set()
        for spec_id in data["spec_ids"]:
            item = await self.store.get_spec(request.path_params["tenant_id"], spec_id)
            name = module_name(item.name)
            if name in names:
                raise InputValidationError("Specification names collide")
            names.add(name)
        return data

    async def channels(self, request: Request) -> Response:
        """Create keyless isolated channels or list tenant-scoped configuration."""
        tenant_id = request.path_params["tenant_id"]
        if request.method == "GET":
            return JSONResponse({"items": [item.model_dump() for item in await self.store.list_channels(tenant_id)]})
        data = await self._channel_data(request)
        return JSONResponse((await self.store.create_channel(tenant_id, **data)).model_dump(), status_code=201)

    async def channel(self, request: Request) -> Response:
        """Publish revised channel policy and close the old authority before returning."""
        data = await self._channel_data(request, update=True)
        tenant_id, channel_id = request.path_params["tenant_id"], request.path_params["channel_id"]
        item = await self.store.update_channel(tenant_id, channel_id, **data)
        await self.runtimes.invalidate(channel_id, before_revision=item.revision)
        return JSONResponse(item.model_dump())

    async def rotate(self, request: Request) -> Response:
        """Reveal a new independent channel key exactly once and revoke prior work."""
        tenant_id, channel_id = request.path_params["tenant_id"], request.path_params["channel_id"]
        token = await self.store.rotate_key(tenant_id, channel_id)
        current = await self.store.get_channel(tenant_id, channel_id)
        await self.runtimes.invalidate(channel_id, before_revision=current.revision)
        return JSONResponse({"token": token, "endpoint": "/mcp/" + channel_id})

    async def revoke(self, request: Request) -> Response:
        """Remove the channel key and revoke its current execution capability."""
        tenant_id, channel_id = request.path_params["tenant_id"], request.path_params["channel_id"]
        await self.store.revoke_key(tenant_id, channel_id)
        current = await self.store.get_channel(tenant_id, channel_id)
        await self.runtimes.invalidate(channel_id, before_revision=current.revision)
        return JSONResponse({"revoked": True})

    async def usage(self, request: Request) -> Response:
        """Return bounded aggregate counts and duration, never code, inputs, or results."""
        items = []
        for offset in range(0, _MAX_USAGE_ROWS, _USAGE_PAGE_SIZE):
            page = await self.store.list_usage(request.path_params["tenant_id"], offset=offset)
            items.extend(page)
            if len(page) < _USAGE_PAGE_SIZE:
                break
        return JSONResponse(
            {"items": [{**item.model_dump(exclude={"latency_ms"}), "total_ms": item.latency_ms} for item in items]}
        )

    async def analytics_report(self, request: Request) -> Response:
        """Report scoped recorded measurements; never infer actual model-token usage or billing."""
        query = request.query_params
        if set(query) - {"days", "channel_id"} or any(len(query.getlist(key)) != 1 for key in query):
            raise InputValidationError("Invalid analytics filters")
        raw_days = query.get("days", "7")
        if not raw_days.isascii() or not raw_days.isdigit() or len(raw_days) > 2:
            raise InputValidationError("Invalid analytics window")
        report = await self.analytics.get_report(
            request.path_params["tenant_id"],
            days=int(raw_days),
            channel_id=query.get("channel_id"),
        )
        return JSONResponse(report)

    async def audit(self, request: Request) -> Response:
        """Merge a bounded view of recent static administrator events across tenants."""
        events: list[dict[str, Any]] = []
        for tenant in await self.access.tenants(request):
            events.extend(item.model_dump() for item in await self.store.list_audit(tenant.id, limit=10))
        user = account(request)
        tenant_id = user.tenant_id if user is not None else None
        events.extend(item.model_dump() for item in await self.users.list_audit(tenant_id=tenant_id))
        events.sort(key=lambda item: item["created_at"], reverse=True)
        return JSONResponse({"items": events[:100]})
