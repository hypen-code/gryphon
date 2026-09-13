"""Server-verified browser identities and tenant authorization independent of UI visibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gryphon.models import UserAccount
from gryphon.saas_auth import COOKIE_NAME
from gryphon.saas_http import failure

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from gryphon.models import Tenant
    from gryphon.saas_auth import AdminSessions
    from gryphon.saas_store import SaaSStore
    from gryphon.saas_users import UserStore


def account(request: Request) -> UserAccount | None:
    """Return a verified account, or the explicitly authenticated bootstrap operator."""
    if not getattr(request.state, "authenticated", False):
        raise RuntimeError("Browser identity has not been authenticated")
    user = request.state.account
    if user is not None and not isinstance(user, UserAccount):
        raise RuntimeError("Invalid browser identity")
    return user


def platform_admin(request: Request) -> bool:
    """Do not infer platform authority from URLs, JSON fields, or unverified cookies."""
    user = account(request)
    return user is None or user.role == "platform_admin"


def actor_id(request: Request) -> str:
    """Use only server-issued identity when recording administrative changes."""
    user = account(request)
    return user.id if user is not None else "bootstrap"


class AccessControl:
    """Revalidate account revision and enabled membership on every authenticated API request."""

    def __init__(self, sessions: AdminSessions, users: UserStore, store: SaaSStore) -> None:
        """Borrow explicitly owned stores and session state."""
        self.sessions, self.users, self.store = sessions, users, store

    async def authorize(self, request: Request) -> Response | None:
        """Authenticate browser identity and reject foreign tenant paths before resource access."""
        cookie = request.cookies.get(COOKIE_NAME, "")
        if self.sessions.verify(cookie) is None:
            return failure("unauthorized", 401)
        identity = self.sessions.identity(cookie)
        user = None
        if identity is not None:
            user = await self.users.get_user(identity[0])
            if user is None or not user.enabled or user.revision != identity[1]:
                self.sessions.logout(cookie)
                return failure("unauthorized", 401)
            if user.tenant_id is not None and not (await self.store.get_tenant(user.tenant_id)).enabled:
                self.sessions.logout(cookie)
                return failure("unauthorized", 401)
        request.state.account, request.state.authenticated = user, True
        tenant_id = request.path_params.get("tenant_id")
        if user is not None and user.role == "tenant_user" and tenant_id is not None and tenant_id != user.tenant_id:
            return failure("not_found", 404)
        return None

    async def platform(self, request: Request) -> Response | None:
        """Restrict platform-wide user and tenant administration to verified platform administrators."""
        denied = await self.authorize(request)
        if denied is not None:
            return denied
        return None if platform_admin(request) else failure("forbidden", 403)

    async def tenants(self, request: Request) -> list[Tenant]:
        """Filter tenant discovery before serialization, including for global audit views."""
        user = account(request)
        if user is not None and user.tenant_id is not None:
            return [await self.store.get_tenant(user.tenant_id)]
        return await self.store.list_tenants()
