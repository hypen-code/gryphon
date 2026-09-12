"""Typed internal record helpers shared by transactional control-plane operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from gryphon.errors import SaaSNotFoundError, SaaSQuotaError

if TYPE_CHECKING:
    from gryphon.saas_database import SaaSDatabase, SQLValue

Table = Literal["tenants", "specs", "channels"]


class SaaSRecords:
    """Provide scoped SQL helpers under transactions owned by the concrete store."""

    _db: SaaSDatabase
    _max_list: int
    _max_audit: int

    async def _get[T: BaseModel](self, table: Table, model: type[T], id: str, tenant_id: str | None = None) -> T:
        """Load one resource using mandatory tenant scope for child resources."""
        suffix, params = ("", (id,)) if table == "tenants" else (" AND tenant_id=?", (id, tenant_id))
        rows = await self._db.execute(f"SELECT payload FROM saas_{table} WHERE id=?{suffix}", params)
        if not rows:
            raise SaaSNotFoundError("Control-plane resource not found")
        return model.model_validate_json(str(rows[0]["payload"]))

    async def _quota(self, table: Table, maximum: int, tenant_id: str | None = None) -> None:
        """Count retained resources while holding the cross-process transaction lock."""
        suffix, params = ("", ()) if tenant_id is None else (" WHERE tenant_id=?", (tenant_id,))
        rows = await self._db.execute(f"SELECT COUNT(*) AS count FROM saas_{table}{suffix}", params)
        if int(str(rows[0]["count"])) >= maximum:
            raise SaaSQuotaError("Control-plane resource quota exceeded")

    def _page(self, limit: int, offset: int) -> None:
        """Reject unbounded or negative administrative listing requests."""
        if not 1 <= limit <= self._max_list or not 0 <= offset <= self._max_audit:
            raise SaaSQuotaError("Control-plane listing quota exceeded")

    async def _list[T: BaseModel](
        self,
        table: Table,
        model: type[T],
        tenant_id: str | None,
        limit: int,
        offset: int,
    ) -> list[T]:
        """Read a deterministic bounded page without widening tenant scope."""
        self._page(limit, offset)
        suffix = "" if tenant_id is None else " WHERE tenant_id=?"
        params: tuple[SQLValue, ...] = () if tenant_id is None else (tenant_id,)
        rows = await self._db.execute(
            f"SELECT payload FROM saas_{table}{suffix} ORDER BY id LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        return [model.model_validate_json(str(row["payload"])) for row in rows]
