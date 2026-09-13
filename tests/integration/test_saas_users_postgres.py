"""Opt-in account persistence tests against disposable PostgreSQL, never operator databases."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import pytest
from test_saas_postgres import postgres_url as postgres_url

from gryphon.errors import ConflictError, SaaSQuotaError, SaaSStoreError
from gryphon.models import UserAccount
from gryphon.saas_store import SaaSStore
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from pydantic import SecretStr


async def test_postgres_users_lifecycle_isolation_and_role_constraints(postgres_url: SecretStr) -> None:
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        users = UserStore(store._db)
        tenant = await store.create_tenant("alpha")
        other = await store.create_tenant("beta")
        first, second = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        admin = await users.create_user("ADMIN", "Admin", first, "platform_admin")
        member = await users.create_user("member", "Member", first, "tenant_user", tenant.id, admin.id)
        assert await users.authenticate("admin", first) == admin
        assert await users.list_users(tenant_id=tenant.id) == [member]
        assert await users.list_users(tenant_id=other.id) == []
        for role, owner in (("other", tenant.id), ("platform_admin", tenant.id), ("tenant_user", None)):
            with pytest.raises(SaaSStoreError):
                async with store._db.transaction():
                    await store._db.execute(
                        "UPDATE saas_users SET role=?,tenant_id=? WHERE id=?", (role, owner, member.id)
                    )
        reset = await users.reset_password(member.id, second, admin.id)
        assert reset.revision == member.revision + 1
        assert await users.authenticate(member.username, first) is None
        await store.disable_tenant(tenant.id)
        assert await users.authenticate(member.username, second) is None
        await store.set_tenant_enabled(tenant.id, True)
        active = await users.authenticate(member.username, second)
        assert active is not None and active.revision == reset.revision + 2
        async with store._db.transaction():
            rows = await store._db.execute("SELECT * FROM saas_users")
            audit = await store._db.execute("SELECT * FROM saas_user_audit")
        assert all(first not in str(row) and second not in str(row) for row in [*rows, *audit])
        assert all(event.tenant_id == tenant.id for event in await users.list_audit(tenant.id))
        await store.close()
        await store.initialize()
        assert await users.authenticate(member.username, second) == active


async def test_postgres_users_cross_connection_duplicate_and_quota_rollback(postgres_url: SecretStr) -> None:
    first = SaaSStore(postgres_url.get_secret_value())
    second = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(first.close)
        cleanup.push_async_callback(second.close)
        await first.initialize()
        await second.initialize()
        stores = [UserStore(item._db, max_users=2, max_users_per_tenant=1) for item in (first, second)]
        tenant = await first.create_tenant("tenant")
        password = secrets.token_urlsafe(24)
        results = await asyncio.gather(
            *(users.create_user("member", "Member", password, "tenant_user", tenant.id) for users in stores),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ConflictError) for result in results) == 1
        member = next(result for result in results if isinstance(result, UserAccount))
        await stores[0].update_user(member.id, enabled=False, actor_id="bootstrap")
        with pytest.raises(SaaSQuotaError):
            await stores[1].create_user("other", "Other", password, "tenant_user", tenant.id)
        results = await asyncio.gather(
            *(
                users.create_user(f"admin{index}", "Admin", password, "platform_admin")
                for index, users in enumerate(stores)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, SaaSQuotaError) for result in results) == 1
        assert len(await stores[0].list_users()) == 2 and len(await stores[1].list_audit()) == 3


async def test_postgres_users_additive_legacy_migration(postgres_url: SecretStr) -> None:
    store = SaaSStore(postgres_url.get_secret_value())
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(store.close)
        await store.initialize()
        tenant = await store.create_tenant("legacy")
        spec = await store.create_spec(tenant.id, "catalog", {})
        channel = await store.create_channel(tenant.id, "channel", spec_ids=[spec.id])
        token = await store.rotate_key(tenant.id, channel.id)
        await store.record_usage(tenant.id, channel.id, "execute_code", "success", 1)
        old_audit = await store.list_audit(tenant.id)
        async with store._db.transaction():
            await store._db.execute("DROP TABLE saas_user_audit")
            await store._db.execute("DROP TABLE saas_users")
        await store.close()
        await store.initialize()
        users = UserStore(store._db)
        password = secrets.token_urlsafe(24)
        admin = await users.create_user("admin", "Admin", password, "platform_admin")
        assert await users.authenticate(admin.username, password) == admin
        assert await store.get_tenant(tenant.id) == tenant
        assert await store.get_spec(tenant.id, spec.id) == spec
        assert await store.lookup_key(token) is not None
        assert (await store.list_usage(tenant.id))[0].calls == 1
        assert await store.list_audit(tenant.id) == old_audit
