"""Hosted account validation, isolation, credential lifecycle, and bounded hashing tests."""

from __future__ import annotations

import asyncio
import secrets
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gryphon.errors import (
    CapacityError,
    ConflictError,
    SaaSDisabledError,
    SaaSNotFoundError,
    SaaSQuotaError,
    SaaSStoreError,
    SaaSValidationError,
)
from gryphon.models import UserAccount
from gryphon.saas_passwords import HASH_ADMISSION, HASH_LENGTH, HASH_PATTERN, PasswordHasher, password_bytes
from gryphon.saas_store import SaaSStore
from gryphon.saas_users import UserStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SaaSStore]:
    """Own an isolated database while account stores only borrow it."""
    value = SaaSStore(f"sqlite:///{tmp_path / 'accounts.db'}")
    await value.initialize()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.parametrize("username", ["ab", "a" * 129, "abc def", " name", "Kelvin", "abc\n", "a/b", "你好啊"])
async def test_users_invalid_username_rejected(store: SaaSStore, username: str) -> None:
    with pytest.raises(SaaSValidationError):
        await UserStore(store._db).create_user(username, "Display", secrets.token_urlsafe(24), "platform_admin")


@pytest.mark.parametrize("name", ["", "a" * 129, "line\nbreak", "nul\x00", "hidden\u200b", "del\x7f"])
async def test_users_invalid_name_rejected(store: SaaSStore, name: str) -> None:
    with pytest.raises(SaaSValidationError):
        await UserStore(store._db).create_user("valid", name, secrets.token_urlsafe(24), "platform_admin")


@pytest.mark.parametrize("password", ["", "a" * 11, "a" * 129, "\ud800" * 12])
def test_password_invalid_bounds_rejected(password: str) -> None:
    with pytest.raises(SaaSValidationError):
        password_bytes(password)


async def test_password_hash_format_salt_limits_and_verification() -> None:
    hasher = PasswordHasher()
    password = secrets.token_urlsafe(24)
    first, second = await asyncio.gather(hasher.hash_password(password), hasher.hash_password(password))
    assert first != second and len(first) == HASH_LENGTH and HASH_PATTERN.fullmatch(first)
    assert await hasher.verify_password(password, first)
    assert not await hasher.verify_password(secrets.token_urlsafe(24), first)
    assert len(password_bytes("\U0001f600" * 128)) == 512
    for malformed in (None, "", first.replace("600000", "600001"), first.upper(), first + "0"):
        assert not await hasher.verify_password(password, malformed)
    assert not await hasher.verify_password("short", first)


async def test_users_admin_create_isolation_public_audit_and_unique_names(store: SaaSStore) -> None:
    users = UserStore(store._db)
    tenant = await store.create_tenant("one")
    other = await store.create_tenant("two")
    password = secrets.token_urlsafe(24)
    admin = await users.create_user("Admin@Example", "Administrator", password, "platform_admin")
    member = await users.create_user("member", "Member", password, "tenant_user", tenant.id, admin.id)
    assert admin.username == "admin@example" and admin.tenant_id is None
    assert await users.authenticate("ADMIN@EXAMPLE", password) == admin
    assert await users.get_user(member.id) == member
    assert await users.get_user(str(uuid4())) is None
    assert await users.list_users(tenant_id=tenant.id) == [member]
    assert await users.list_users(tenant_id=other.id) == []
    assert await users.list_users(limit=1, offset=1) == [member]
    with pytest.raises(ConflictError):
        await users.create_user("MEMBER", "Other", password, "platform_admin")
    async with store._db.transaction():
        rows = await store._db.execute("SELECT * FROM saas_users")
        audit_rows = await store._db.execute("SELECT * FROM saas_user_audit")
    assert all(password not in str(row) for row in [*rows, *audit_rows])
    assert "password" not in admin.model_dump_json() and "hash" not in member.model_dump_json()
    audit = await users.list_audit(tenant.id)
    assert len(audit) == 1 and audit[0].actor_id == admin.id and audit[0].subject_id == member.id
    assert await users.list_audit(other.id) == []
    with pytest.raises(ValidationError):
        UserAccount.model_validate(admin.model_dump() | {"password": password})


async def test_users_membership_constraints_and_immutable_updates(store: SaaSStore) -> None:
    users = UserStore(store._db)
    tenant = await store.create_tenant("tenant")
    password = secrets.token_urlsafe(24)
    with pytest.raises(SaaSValidationError):
        await users.create_user("admin", "Admin", password, "platform_admin", tenant.id)
    with pytest.raises(SaaSValidationError):
        await users.create_user("member", "Member", password, "tenant_user")
    with pytest.raises(SaaSNotFoundError):
        await users.create_user("member", "Member", password, "tenant_user", str(uuid4()))
    member = await users.create_user("member", "Member", password, "tenant_user", tenant.id)
    updated = await users.update_user(member.id, name="Revised", actor_id="bootstrap")
    assert updated.role == member.role and updated.tenant_id == tenant.id
    assert updated.revision == member.revision + 1
    for role, tenant_id in (("other", tenant.id), ("platform_admin", tenant.id), ("tenant_user", None)):
        with pytest.raises(SaaSStoreError):
            async with store._db.transaction():
                await store._db.execute(
                    "UPDATE saas_users SET role=?,tenant_id=? WHERE id=?", (role, tenant_id, member.id)
                )
    assert await users.get_user(member.id) == updated


async def test_users_disable_reenable_never_revives_old_revision(store: SaaSStore) -> None:
    users = UserStore(store._db)
    tenant = await store.create_tenant("tenant")
    password = secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", password, "tenant_user", tenant.id)
    await store.disable_tenant(tenant.id)
    assert await users.authenticate(member.username, password) is None
    with pytest.raises(SaaSDisabledError):
        await users.create_user("other", "Other", password, "tenant_user", tenant.id)
    await store.set_tenant_enabled(tenant.id, True)
    enabled = await users.authenticate(member.username, password)
    assert enabled is not None and enabled.revision == member.revision + 2
    disabled = await users.update_user(member.id, enabled=False, actor_id="bootstrap")
    assert disabled.revision == enabled.revision + 1
    assert await users.authenticate(member.username, password) is None
    enabled = await users.update_user(member.id, enabled=True, actor_id="bootstrap")
    assert enabled.revision == disabled.revision + 1
    assert await users.authenticate(member.username, password) == enabled


async def test_users_reset_and_self_change_revision_credentials(store: SaaSStore) -> None:
    users = UserStore(store._db)
    first, second, third = (secrets.token_urlsafe(24) for _ in range(3))
    admin = await users.create_user("admin", "Admin", first, "platform_admin")
    reset = await users.reset_password(admin.id, second, "bootstrap")
    assert reset.revision == admin.revision + 1
    assert await users.authenticate(admin.username, first) is None
    with pytest.raises(SaaSValidationError):
        await users.change_password(admin.id, first, third, reset.revision, admin.id)
    with pytest.raises(ConflictError):
        await users.change_password(admin.id, second, third, admin.revision, admin.id)
    changed = await users.change_password(admin.id, second, third, reset.revision, admin.id)
    assert changed.revision == reset.revision + 1
    assert await users.authenticate(admin.username, second) is None
    assert await users.authenticate(admin.username, third) == changed
    assert {event.event for event in await users.list_audit()} == {"user_created", "password_reset", "password_changed"}


async def test_users_unknown_disabled_and_invalid_receive_dummy_work(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = UserStore(store._db)
    password = secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", password, "platform_admin")
    await users.update_user(member.id, enabled=False, actor_id="bootstrap")
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(users._passwords, "verify_password", verify)
    for username in ("unknown", member.username, "not/valid"):
        assert await users.authenticate(username, password) is None
    assert verify.await_count == 3
    assert all(call.args == (password, None) for call in verify.await_args_list)


async def test_users_authentication_rechecks_concurrent_revision(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = UserStore(store._db)
    password = secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", password, "platform_admin")
    original = users._passwords.verify_password

    async def mutate(password: str, stored: str | None) -> bool:
        verified = await original(password, stored)
        await users.update_user(member.id, enabled=False, actor_id="bootstrap")
        return verified

    monkeypatch.setattr(users._passwords, "verify_password", mutate)
    assert await users.authenticate(member.username, password) is None


async def test_users_change_rechecks_concurrent_password_reset(
    store: SaaSStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = UserStore(store._db)
    first, second = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", first, "platform_admin")
    original = users._passwords.hash_password

    async def mutate(password: str) -> str:
        hashed = await original(password)
        await users.update_user(member.id, enabled=False, actor_id="bootstrap")
        return hashed

    monkeypatch.setattr(users._passwords, "hash_password", mutate)
    with pytest.raises(ConflictError):
        await users.change_password(member.id, first, second, member.revision, member.id)
    assert await users.authenticate(member.username, second) is None


async def test_users_concurrent_duplicate_and_quotas_rollback(store: SaaSStore) -> None:
    users = UserStore(store._db, max_users=2, max_users_per_tenant=1)
    tenant = await store.create_tenant("tenant")
    password = secrets.token_urlsafe(24)
    results = await asyncio.gather(
        *(users.create_user("member", "Member", password, "tenant_user", tenant.id) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    member = next(result for result in results if isinstance(result, UserAccount))
    await users.update_user(member.id, enabled=False, actor_id="bootstrap")
    with pytest.raises(SaaSQuotaError):
        await users.create_user("another", "Another", password, "tenant_user", tenant.id)
    results = await asyncio.gather(
        *(users.create_user(name, "Admin", password, "platform_admin") for name in ("admin1", "admin2")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SaaSQuotaError) for result in results) == 1
    assert len(await users.list_users()) == 2
    assert len(await users.list_audit()) == 3


async def test_users_legacy_migration_reopen_preserves_resources(store: SaaSStore) -> None:
    tenant = await store.create_tenant("legacy")
    spec = await store.create_spec(tenant.id, "catalog", {})
    channel = await store.create_channel(tenant.id, "channel", spec_ids=[spec.id])
    key = await store.rotate_key(tenant.id, channel.id)
    async with store._db.transaction():
        await store._db.execute("DROP TABLE saas_user_audit")
        await store._db.execute("DROP TABLE saas_users")
    await store.close()
    await store.initialize()
    users = UserStore(store._db)
    password = secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", password, "tenant_user", tenant.id)
    await store.close()
    await store.initialize()
    assert await users.authenticate(member.username, password) == member
    assert await store.get_tenant(tenant.id) == tenant
    assert await store.get_spec(tenant.id, spec.id) == spec
    assert await store.lookup_key(key) is not None


async def test_password_workers_bounded_and_cancellation_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    hasher = PasswordHasher()
    release = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def derive(algorithm: str, password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
        loop.call_soon_threadsafe(started.set)
        assert algorithm == "sha256" and iterations == 600000 and length == 32 and len(salt) == 32
        if not release.wait(timeout=10):
            raise RuntimeError("Test worker timed out")
        return bytes(length)

    monkeypatch.setattr("gryphon.saas_passwords.hashlib.pbkdf2_hmac", derive)
    tasks = [asyncio.create_task(hasher.hash_password(secrets.token_urlsafe(24))) for _ in range(HASH_ADMISSION)]
    try:
        await started.wait()
        assert hasher._admitted == HASH_ADMISSION
        with pytest.raises(CapacityError):
            await hasher.hash_password(secrets.token_urlsafe(24))
        tasks[0].cancel()
        tasks[-1].cancel()
        await asyncio.sleep(0)
        assert not tasks[0].done()
        tasks[0].cancel()
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError) and isinstance(results[-1], asyncio.CancelledError)
    assert hasher._admitted == 0 and hasher._slots._value == 2


async def test_users_invalid_pages_actor_update_and_missing_account(store: SaaSStore) -> None:
    users = UserStore(store._db)
    for limit, offset in ((0, 0), (101, 0), (1, -1), (1, 10001)):
        with pytest.raises(SaaSValidationError):
            await users.list_users(limit, offset)
        with pytest.raises(SaaSValidationError):
            await users.list_audit(limit=limit, offset=offset)
    with pytest.raises(SaaSValidationError):
        UserStore(store._db, max_users=1001)
    password = secrets.token_urlsafe(24)
    with pytest.raises(SaaSValidationError):
        await users.create_user("member", "Member", password, "platform_admin", actor_id="not-an-id")
    with pytest.raises(SaaSNotFoundError):
        await users.reset_password(str(uuid4()), password, "bootstrap")
    member = await users.create_user("member", "Member", password, "platform_admin")
    with pytest.raises(SaaSValidationError):
        await users.update_user(member.id, name="\x00", actor_id="bootstrap")
    assert await users.get_user(member.id) == member


async def test_users_audit_retention_and_atomic_rollback(store: SaaSStore, monkeypatch: pytest.MonkeyPatch) -> None:
    users = UserStore(store._db)
    password = secrets.token_urlsafe(24)
    member = await users.create_user("member", "Member", password, "platform_admin")
    monkeypatch.setattr("gryphon.saas_users.MAX_AUDIT_ENTRIES", 2)
    await users.update_user(member.id, enabled=False, actor_id="bootstrap")
    updated = await users.update_user(member.id, enabled=True, actor_id="bootstrap")
    assert [event.event for event in await users.list_audit()] == ["user_enabled", "user_disabled"]
    monkeypatch.setattr(users, "_audit", AsyncMock(side_effect=SaaSStoreError("Audit unavailable")))
    with pytest.raises(SaaSStoreError):
        await users.reset_password(member.id, secrets.token_urlsafe(24), "bootstrap")
    assert await users.authenticate(member.username, password) == updated


def test_users_model_rejects_invalid_ids_role_and_password_byte_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {"id": str(uuid4()), "username": "member", "name": "Member", "role": "platform_admin", "created_at": 0}
    for changes in ({"id": uuid4().hex}, {"role": "tenant_admin"}, {"tenant_id": "not-a-uuid"}):
        with pytest.raises(ValidationError):
            UserAccount.model_validate(values | changes)
    monkeypatch.setattr("gryphon.saas_passwords.PASSWORD_MAX_BYTES", 16)
    with pytest.raises(SaaSValidationError):
        password_bytes("\U0001f600" * 12)
