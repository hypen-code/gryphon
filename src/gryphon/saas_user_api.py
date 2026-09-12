"""Platform-admin user management and authenticated self-service password changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from gryphon.errors import InputValidationError
from gryphon.saas_access import account, actor_id
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_http import admin_endpoint, failure, fields, read_object

if TYPE_CHECKING:
    from starlette.requests import Request

    from gryphon.saas_access import AccessControl
    from gryphon.saas_auth import AdminSessions
    from gryphon.saas_config import SaaSConfig
    from gryphon.saas_users import UserStore

_PAGE_SIZE = 100


def _password(value: object) -> str:
    """Validate type without including a submitted credential in exceptions."""
    if not isinstance(value, str):
        raise InputValidationError("Invalid password")
    return value


class UserAPI:
    """Keep account administration separate from tenant catalog and execution operations."""

    def __init__(self, users: UserStore, access: AccessControl, sessions: AdminSessions, config: SaaSConfig) -> None:
        """Inject shared session and persistence dependencies without additional ownership."""
        self.users, self.access, self.sessions, self.config = users, access, sessions, config

    def routes(self) -> list[Route]:
        """Require platform authority for every account-management endpoint, including listings."""
        return [
            Route("/api/me", admin_endpoint(self.sessions, self.me, self.access.authorize), methods=["GET"]),
            Route(
                "/api/password", admin_endpoint(self.sessions, self.password, self.access.authorize), methods=["POST"]
            ),
            Route(
                "/api/users",
                admin_endpoint(self.sessions, self.collection, self.access.platform),
                methods=["GET", "POST"],
            ),
            Route(
                "/api/users/{user_id}",
                admin_endpoint(self.sessions, self.update, self.access.platform),
                methods=["PATCH"],
            ),
            Route(
                "/api/users/{user_id}/password",
                admin_endpoint(self.sessions, self.reset, self.access.platform),
                methods=["POST"],
            ),
        ]

    async def me(self, request: Request) -> Response:
        """Describe verified role and membership; never return password material or session credentials."""
        user = account(request)
        return JSONResponse(
            {
                "user": user.model_dump()
                if user is not None
                else {
                    "id": None,
                    "username": "bootstrap",
                    "name": "Bootstrap administrator",
                    "role": "platform_admin",
                    "tenant_id": None,
                }
            }
        )

    async def collection(self, request: Request) -> Response:
        """List a bounded public account page or create an immutable-role account."""
        if request.method == "POST":
            return await self.create(request)
        try:
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            raise InputValidationError("Invalid page") from None
        if offset < 0 or offset > 1000 or offset % _PAGE_SIZE:
            raise InputValidationError("Invalid page")
        items = await self.users.list_users(offset=offset)
        more = bool(await self.users.list_users(limit=1, offset=offset + len(items))) if items else False
        return JSONResponse(
            {
                "items": [user.model_dump() for user in items],
                "next_offset": offset + len(items) if more else None,
            }
        )

    async def create(self, request: Request) -> Response:
        """Create accounts only from closed administrator input; never allow a client-selected audit actor."""
        data = await read_object(request, 4096)
        fields(data, {"username", "name", "password", "role", "tenant_id"})
        if not isinstance(data["username"], str) or not isinstance(data["name"], str):
            raise InputValidationError("Invalid account fields")
        if data["role"] not in ("platform_admin", "tenant_user"):
            raise InputValidationError("Invalid account role")
        if data["tenant_id"] is not None and not isinstance(data["tenant_id"], str):
            raise InputValidationError("Invalid tenant membership")
        user = await self.users.create_user(
            data["username"],
            data["name"],
            _password(data["password"]),
            data["role"],
            tenant_id=data["tenant_id"],
            actor_id=actor_id(request),
        )
        return JSONResponse(user.model_dump(), status_code=201)

    async def update(self, request: Request) -> Response:
        """Change only display name or enabled status; role and membership cannot be reassigned."""
        data = await read_object(request, 4096)
        fields(data, set(), {"name", "enabled"})
        if not data or ("name" in data and not isinstance(data["name"], str)):
            raise InputValidationError("Invalid account update")
        if "enabled" in data and type(data["enabled"]) is not bool:
            raise InputValidationError("Invalid account status")
        user_id = request.path_params["user_id"]
        if data.get("enabled") is False and user_id == actor_id(request):
            return failure("self_disable", 409)
        user = await self.users.update_user(user_id, **data, actor_id=actor_id(request))
        self.sessions.revoke_user(user.id, before_revision=user.revision)
        return JSONResponse(user.model_dump())

    async def reset(self, request: Request) -> Response:
        """Reset credentials and advance session revision without returning a password or hash."""
        data = await read_object(request, 4096)
        fields(data, {"password"})
        user = await self.users.reset_password(
            request.path_params["user_id"],
            _password(data["password"]),
            actor_id(request),
        )
        self.sessions.revoke_user(user.id, before_revision=user.revision)
        return JSONResponse(user.model_dump())

    async def password(self, request: Request) -> Response:
        """Require current credentials for self-service changes and expire the caller's session."""
        user = account(request)
        if user is None:
            return failure("forbidden", 403)
        data = await read_object(request, 4096)
        fields(data, {"current_password", "new_password"})
        updated = await self.users.change_password(
            user.id,
            _password(data["current_password"]),
            _password(data["new_password"]),
            user.revision,
            user.id,
        )
        self.sessions.revoke_user(user.id, before_revision=updated.revision)
        self.sessions.logout(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"changed": True})
        response.delete_cookie(
            COOKIE_NAME,
            path="/api",
            secure=self.config.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response
