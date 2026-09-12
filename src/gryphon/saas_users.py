"""Revisioned hosted accounts using shared SQL storage and private password hashes."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from gryphon.errors import ConflictError, SaaSDisabledError, SaaSNotFoundError, SaaSQuotaError, SaaSValidationError
from gryphon.models import UserAccount, UserAudit, UserRole
from gryphon.models.users import UserAuditEvent, normalize_username
from gryphon.saas_audit import account_actor
from gryphon.saas_passwords import password_bytes

if TYPE_CHECKING:
    from gryphon.saas_database import SaaSDatabase, SQLRow, SQLValue

MAX_USERS = 1000
MAX_TENANT_USERS = 100
MAX_LIST_LIMIT = 100
MAX_AUDIT_ENTRIES = 10000
PUBLIC_COLUMNS = "id,username,name,role,tenant_id,enabled,revision,created_at"


def _account(row: SQLRow) -> UserAccount:
    """Project only explicitly public columns, never credential material."""
    return UserAccount.model_validate({key: row[key] for key in PUBLIC_COLUMNS.split(",")})


def _actor(actor_id: str) -> None:
    """Accept the bootstrap recovery identity or a canonical server account UUID."""
    if actor_id == "bootstrap":
        return
    try:
        if str(UUID(actor_id)) == actor_id:
            return
    except ValueError:
        pass
    raise SaaSValidationError("Invalid audit actor")


def _page(limit: int, offset: int) -> None:
    """Bound public list pagination before querying retained resources."""
    if not 1 <= limit <= MAX_LIST_LIMIT or not 0 <= offset <= MAX_AUDIT_ENTRIES:
        raise SaaSValidationError("Invalid account page")


class UserStore:
    """Borrow an initialized control database; never close or own its lifecycle."""

    def __init__(
        self, database: SaaSDatabase, *, max_users: int = MAX_USERS, max_users_per_tenant: int = MAX_TENANT_USERS
    ) -> None:
        """Share hashing admission and transactional serialization with sibling stores."""
        if not 1 <= max_users <= MAX_USERS or not 1 <= max_users_per_tenant <= MAX_TENANT_USERS:
            raise SaaSValidationError("Invalid account quotas")
        self._db = database
        self._passwords = database.password_hasher
        self._max_users, self._max_tenant_users = max_users, max_users_per_tenant

    async def _row(self, value: str, column: Literal["id", "username"] = "id") -> SQLRow | None:
        """Read a private credential snapshot with the tenant's current enabled state."""
        rows = await self._db.execute(
            "SELECT u.*,t.enabled AS tenant_enabled FROM saas_users u "
            f"LEFT JOIN saas_tenants t ON t.id=u.tenant_id WHERE u.{column}=?",
            (value,),
        )
        return rows[0] if rows else None

    @staticmethod
    def _active(row: SQLRow | None) -> bool:
        """Require an enabled account and enabled membership when tenant-bound."""
        return (
            row is not None and row["enabled"] == 1 and (row["role"] == "platform_admin" or row["tenant_enabled"] == 1)
        )

    async def _require(self, user_id: str) -> SQLRow:
        """Resolve a retained account or raise a static missing-resource failure."""
        row = await self._row(user_id)
        if row is None:
            raise SaaSNotFoundError("Account does not exist")
        return row

    async def _quota(self, item: UserAccount) -> None:
        """Serialize retained-account quotas and unique username checks before insertion."""
        if await self._row(item.username, "username") is not None:
            raise ConflictError("Username is already registered")
        rows = await self._db.execute("SELECT COUNT(*) AS total FROM saas_users")
        if int(str(rows[0]["total"])) >= self._max_users:
            raise SaaSQuotaError("Account quota exceeded")
        if item.tenant_id is not None:
            tenants = await self._db.execute("SELECT enabled FROM saas_tenants WHERE id=?", (item.tenant_id,))
            if not tenants:
                raise SaaSNotFoundError("Tenant does not exist")
            if tenants[0]["enabled"] != 1:
                raise SaaSDisabledError("Tenant is disabled")
            rows = await self._db.execute(
                "SELECT COUNT(*) AS total FROM saas_users WHERE tenant_id=?", (item.tenant_id,)
            )
            if int(str(rows[0]["total"])) >= self._max_tenant_users:
                raise SaaSQuotaError("Tenant account quota exceeded")

    async def _audit(self, item: UserAccount, actor_id: str, event: UserAuditEvent) -> None:
        """Append bounded static metadata inside the account mutation transaction."""
        await self._db.execute(
            "INSERT INTO saas_user_audit VALUES (?,?,?,?,?,?)",
            (str(uuid4()), actor_id, item.id, item.tenant_id, event, time.time()),
        )
        await self._db.execute(
            "DELETE FROM saas_user_audit WHERE id IN (SELECT id FROM saas_user_audit "
            "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?)",
            (MAX_AUDIT_ENTRIES, MAX_AUDIT_ENTRIES),
        )

    async def create_user(
        self,
        username: str,
        name: str,
        password: str,
        role: UserRole,
        tenant_id: str | None = None,
        actor_id: str = "bootstrap",
    ) -> UserAccount:
        """Create a server UUID account with immutable role/membership and a salted hash."""
        _actor(actor_id)
        try:
            item = UserAccount(
                id=str(uuid4()), username=username, name=name, role=role, tenant_id=tenant_id, created_at=time.time()
            )
        except ValidationError:
            raise SaaSValidationError("Invalid account") from None
        password_bytes(password)
        async with self._db.transaction():
            await self._quota(item)
        hashed = await self._passwords.hash_password(password)
        async with self._db.transaction():
            await self._quota(item)
            await self._db.execute(
                "INSERT INTO saas_users (" + PUBLIC_COLUMNS + ",password_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item.id,
                    item.username,
                    item.name,
                    item.role,
                    item.tenant_id,
                    1,
                    item.revision,
                    item.created_at,
                    hashed,
                ),
            )
            await self._audit(item, actor_id, "user_created")
        return item

    async def get_user(self, user_id: str) -> UserAccount | None:
        """Return public metadata including disabled accounts, or None when absent."""
        async with self._db.transaction():
            row = await self._row(user_id)
            return _account(row) if row is not None else None

    async def list_users(self, limit: int = 100, offset: int = 0, tenant_id: str | None = None) -> list[UserAccount]:
        """List a stable bounded page, optionally restricted to one tenant's accounts."""
        _page(limit, offset)
        clause = "" if tenant_id is None else " WHERE tenant_id=?"
        params: tuple[SQLValue, ...] = () if tenant_id is None else (tenant_id,)
        async with self._db.transaction():
            rows = await self._db.execute(
                f"SELECT {PUBLIC_COLUMNS} FROM saas_users{clause} ORDER BY created_at,id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [_account(row) for row in rows]

    async def authenticate(self, username: str, password: str) -> UserAccount | None:
        """Do equal hash work for rejected identities and recheck revisions before authorizing."""
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = ""
        async with self._db.transaction():
            row = await self._row(normalized, "username")
        stored = str(row["password_hash"]) if row is not None and self._active(row) else None
        verified = await self._passwords.verify_password(password, stored)
        if not verified or row is None:
            return None
        async with self._db.transaction():
            current = await self._row(str(row["id"]))
            if not self._active(current) or current is None or current["revision"] != row["revision"]:
                return None
            return _account(current)

    async def update_user(
        self, user_id: str, *, name: str | None = None, enabled: bool | None = None, actor_id: str
    ) -> UserAccount:
        """Update only display name/status and advance revision; membership cannot transfer."""
        _actor(actor_id)
        async with self._db.transaction():
            item = _account(await self._require(user_id))
            try:
                updated = UserAccount.model_validate(
                    item.model_dump()
                    | {
                        "name": item.name if name is None else name,
                        "enabled": item.enabled if enabled is None else enabled,
                        "revision": item.revision + 1,
                    }
                )
            except ValidationError:
                raise SaaSValidationError("Invalid account update") from None
            await self._db.execute(
                "UPDATE saas_users SET name=?,enabled=?,revision=? WHERE id=?",
                (updated.name, int(updated.enabled), updated.revision, user_id),
            )
            event: UserAuditEvent = (
                "user_updated" if enabled is None else "user_enabled" if enabled else "user_disabled"
            )
            await self._audit(updated, actor_id, event)
            return updated

    async def _save_password(self, row: SQLRow, hashed: str, actor_id: str, event: UserAuditEvent) -> UserAccount:
        """Replace private credentials and advance public revision in the caller's transaction."""
        item = _account(row)
        updated = item.model_copy(update={"revision": item.revision + 1})
        await self._db.execute(
            "UPDATE saas_users SET password_hash=?,revision=? WHERE id=?", (hashed, updated.revision, updated.id)
        )
        await self._audit(updated, actor_id, event)
        return updated

    async def reset_password(self, user_id: str, password: str, actor_id: str) -> UserAccount:
        """Administratively replace a retained account password and invalidate its sessions."""
        _actor(actor_id)
        password_bytes(password)
        async with self._db.transaction():
            await self._require(user_id)
        hashed = await self._passwords.hash_password(password)
        async with self._db.transaction():
            return await self._save_password(await self._require(user_id), hashed, actor_id, "password_reset")

    async def change_password(
        self, user_id: str, current: str, new: str, expected_revision: int, actor_id: str
    ) -> UserAccount:
        """Require current credentials and an unchanged active revision before self-service replacement."""
        _actor(actor_id)
        password_bytes(new)
        async with self._db.transaction():
            row = await self._row(user_id)
        stored = str(row["password_hash"]) if row is not None and self._active(row) else None
        verified = await self._passwords.verify_password(current, stored)
        if not verified or row is None:
            raise SaaSValidationError("Current credentials are invalid")
        if row["revision"] != expected_revision:
            raise ConflictError("Account revision changed")
        hashed = await self._passwords.hash_password(new)
        async with self._db.transaction():
            latest = await self._row(user_id)
            if (
                latest is None
                or not self._active(latest)
                or latest["revision"] != expected_revision
                or latest["password_hash"] != row["password_hash"]
            ):
                raise ConflictError("Account revision changed")
            return await self._save_password(latest, hashed, actor_id, "password_changed")

    async def list_audit(self, tenant_id: str | None = None, limit: int = 100, offset: int = 0) -> list[UserAudit]:
        """List static administration events globally or within one tenant, newest first."""
        _page(limit, offset)
        clause = "" if tenant_id is None else " WHERE tenant_id=?"
        params: tuple[SQLValue, ...] = () if tenant_id is None else (tenant_id,)
        async with self._db.transaction():
            rows = await self._db.execute(
                f"SELECT * FROM saas_user_audit{clause} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            actors = {
                actor_id: await account_actor(self._db, actor_id) for actor_id in {str(row["actor_id"]) for row in rows}
            }
            return [UserAudit.model_validate(row | {"actor": actors[str(row["actor_id"])]}) for row in rows]
