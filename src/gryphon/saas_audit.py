"""Task-local public audit metadata; never an input to authorization decisions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from gryphon.models.audit import AuditActor
from gryphon.models.users import UserAccount

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.requests import Request

    from gryphon.saas_database import SaaSDatabase

_UNKNOWN_ACTOR = AuditActor()
_ACTOR: ContextVar[AuditActor] = ContextVar("hosted_audit_actor", default=_UNKNOWN_ACTOR)


def current_actor() -> AuditActor:
    """Return immutable metadata inherited by owned cleanup tasks, not execution authority."""
    return _ACTOR.get()


def request_actor(request: Request) -> AuditActor:
    """Snapshot only identity already verified by the server's authorization callback."""
    if getattr(request.state, "authenticated", False) is not True:
        return AuditActor()
    user = getattr(request.state, "account", False)
    if user is None:
        return AuditActor(id="bootstrap", kind="bootstrap", display_source="snapshot")
    if isinstance(user, UserAccount):
        return AuditActor(id=user.id, username=user.username, name=user.name, kind="user", display_source="snapshot")
    return AuditActor()


@contextmanager
def audit_actor(actor: AuditActor) -> Iterator[None]:
    """Scope audit metadata to one request and restore it on success, failure or cancellation."""
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)


async def account_actor(database: SaaSDatabase, actor_id: str) -> AuditActor:
    """Resolve only public actor fields for an already authorized account-audit event.

    Account audit rows retain IDs, not historical names. The current projection is
    explicitly labeled and never inferred from the event's subject or tenant.
    """
    if actor_id == "bootstrap":
        return AuditActor(id=actor_id, kind="bootstrap")
    rows = await database.execute("SELECT id,username,name FROM saas_users WHERE id=?", (actor_id,))
    if not rows:
        return AuditActor(id=actor_id)
    row = rows[0]
    return AuditActor(
        id=str(row["id"]), username=str(row["username"]), name=str(row["name"]), kind="user", display_source="current"
    )
