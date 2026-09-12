"""Opt-in real PostgreSQL workflow using only a newly created disposable test container."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING
from uuid import uuid4

import aiodocker
import pytest
from aiodocker.exceptions import DockerError
from pydantic import SecretStr

from gryphon.errors import SaaSNotFoundError, SaaSQuotaError, SaaSStoreError
from gryphon.models import Tenant
from gryphon.saas_store import SaaSStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.models import Channel

POSTGRES_IMAGE = "postgres:17.6"
STARTUP_ATTEMPTS = 120
STARTUP_DELAY = 0.5
IMAGE_PULL_TIMEOUT = 300


async def _ensure_postgres_image(docker: aiodocker.Docker) -> None:
    """Reuse the exact pinned image or bound the initial public-registry pull."""
    try:
        await docker.images.inspect(POSTGRES_IMAGE)
    except DockerError as exc:
        if exc.status != 404:
            raise
        async with asyncio.timeout(IMAGE_PULL_TIMEOUT):
            await docker.images.pull(POSTGRES_IMAGE)


async def _wait_for_postgres(url: SecretStr) -> None:
    """Wait for schema initialization without emitting credentials or backend exception details."""
    for _ in range(STARTUP_ATTEMPTS):
        store = SaaSStore(url.get_secret_value())
        try:
            await store.initialize()
            return
        except SaaSStoreError:
            await asyncio.sleep(STARTUP_DELAY)
        finally:
            await store.close()
    pytest.fail("Disposable PostgreSQL did not become ready", pytrace=False)


@pytest.fixture
async def postgres_url() -> AsyncIterator[SecretStr]:
    """Create a unique loopback-only PostgreSQL container on tmpfs and always remove it."""
    if os.environ.get("GRYPHON_TEST_POSTGRES") != "1":
        pytest.skip("Set GRYPHON_TEST_POSTGRES=1 to run disposable PostgreSQL tests")
    async with aiodocker.Docker() as docker:
        await _ensure_postgres_image(docker)
        password = secrets.token_urlsafe(48)
        container = await docker.containers.create(
            name=f"gryphon-postgres-test-{uuid4().hex}",
            config={
                "Image": POSTGRES_IMAGE,
                "Env": [f"POSTGRES_PASSWORD={password}"],
                "ExposedPorts": {"5432/tcp": {}},
                "HostConfig": {
                    "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "0"}]},
                    "Tmpfs": {"/var/lib/postgresql/data": "rw,size=536870912"},
                },
            },
        )
        try:
            await container.start()
            info = await container.show()
            binding = info["NetworkSettings"]["Ports"]["5432/tcp"][0]
            assert binding["HostIp"] == "127.0.0.1"
            assert all(mount["Type"] != "volume" for mount in info["Mounts"])
            url = SecretStr(f"postgresql://postgres:{password}@127.0.0.1:{binding['HostPort']}/postgres")
            await _wait_for_postgres(url)
            yield url
        finally:
            await container.delete(force=True, v=True)


async def _check_isolation(store: SaaSStore, tenant: Tenant, other: Tenant) -> Channel:
    """Verify real composite foreign keys, tenant-scoped queries, and canonical specification storage."""
    spec = await store.create_spec(tenant.id, "api", {"paths": {}, "openapi": "3.1.0"})
    foreign = await store.create_spec(other.id, "foreign", {})
    with pytest.raises(SaaSNotFoundError):
        await store.create_channel(tenant.id, "invalid", spec_ids=[foreign.id])
    assert await store.list_channels(tenant.id) == []
    channel = await store.create_channel(tenant.id, "one", spec_ids=[spec.id])
    assert await store.get_spec(tenant.id, spec.id) == spec
    assert await store.list_specs(tenant.id) == [spec]
    assert await store.list_channels(tenant.id) == [channel]
    with pytest.raises(SaaSNotFoundError):
        await store.get_channel(other.id, channel.id)
    with pytest.raises(SaaSNotFoundError):
        await store.get_spec(other.id, spec.id)
    with pytest.raises(SaaSNotFoundError):
        await store.update_channel(tenant.id, channel.id, spec_ids=[foreign.id])
    with pytest.raises(SaaSStoreError):
        async with store._db.transaction():
            await store._db.execute("INSERT INTO saas_bindings VALUES (?,?,?)", (tenant.id, channel.id, foreign.id))
    assert await store.get_channel(tenant.id, channel.id) == channel
    return channel


async def _check_lifecycle(store: SaaSStore, tenant: Tenant, channel: Channel) -> None:
    """Exercise digest-only keys, revisioned configuration, disable/reenable, and revocation."""
    first = await store.rotate_key(tenant.id, channel.id)
    assert await store.lookup_key(first) is not None
    async with store._db.transaction():
        rows = await store._db.execute("SELECT key_digest,payload FROM saas_channels WHERE id=?", (channel.id,))
    assert rows[0]["key_digest"] == hashlib.sha256(first.encode()).hexdigest()
    assert first not in str(rows)
    second = await store.rotate_key(tenant.id, channel.id)
    assert await store.lookup_key(first) is None
    assert await store.lookup_key(second) is not None
    updated = await store.update_channel(tenant.id, channel.id, sandbox_mode="docker", allowed_imports=["math"])
    assert updated.revision > channel.revision and updated.sandbox_mode == "docker"
    await store.disable_tenant(tenant.id)
    assert await store.lookup_key(second) is None
    await store.set_tenant_enabled(tenant.id, True)
    assert (await store.get_channel(tenant.id, channel.id)).revision == updated.revision + 2
    assert await store.lookup_key(second) is not None
    await store.update_channel(tenant.id, channel.id, enabled=False)
    assert await store.lookup_key(second) is None
    await store.update_channel(tenant.id, channel.id, enabled=True)
    await store.revoke_key(tenant.id, channel.id)
    assert await store.lookup_key(second) is None


async def _check_quotas_and_telemetry(store: SaaSStore, tenant: Tenant, channel: Channel, other: Tenant) -> None:
    """Verify quotas and real PostgreSQL upserts preserve tenant-isolated safe aggregate telemetry."""
    with pytest.raises(SaaSQuotaError):
        await store.create_tenant("overflow")
    with pytest.raises(SaaSQuotaError):
        await store.create_channel(tenant.id, "overflow")
    with pytest.raises(SaaSQuotaError):
        await store.create_spec(tenant.id, "overflow", {})
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 1.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "success", 2.5)
    await store.record_usage(tenant.id, channel.id, "execute_code", "error", 3)
    usage = await store.list_usage(tenant.id, channel.id)
    assert len(usage) == 2 and usage[1].calls == 2 and usage[1].latency_ms == 4
    assert await store.list_usage(other.id, channel.id) == []
    audit = await store.list_audit(tenant.id)
    assert all(event.tenant_id == tenant.id for event in audit)
    assert {"tenant_enabled", "key_rotated", "key_revoked"}.issubset({event.event for event in audit})


async def test_postgres_control_plane_workflow_and_exclusive_host_lease(postgres_url: SecretStr) -> None:
    """Validate PostgreSQL behavior end to end and refuse simultaneous hosted workers."""
    first = SaaSStore(postgres_url.get_secret_value(), max_tenants=2, max_channels_per_tenant=1, max_specs_per_tenant=1)
    second = SaaSStore(postgres_url.get_secret_value(), max_tenants=2)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(first.close)
        cleanup.push_async_callback(second.close)
        await first.initialize()
        await second.initialize()
        await first.acquire_host_lease()
        await first.acquire_host_lease()
        with pytest.raises(SaaSStoreError, match="already owned"):
            await second.acquire_host_lease()
        await second.initialize()
        async with first._db.transaction():
            rows = await first._db.execute("SELECT current_setting('server_version_num') AS version")
        assert rows[0]["version"] == "170006"
        tenant = await first.create_tenant("alpha")
        results = await asyncio.gather(
            first.create_tenant("beta"), second.create_tenant("beta-race"), return_exceptions=True
        )
        assert sum(isinstance(result, SaaSQuotaError) for result in results) == 1
        other = next(result for result in results if isinstance(result, Tenant))
        channel = await _check_isolation(first, tenant, other)
        await _check_lifecycle(first, tenant, channel)
        await _check_quotas_and_telemetry(first, tenant, channel, other)
        await first.close()
        await second.acquire_host_lease()
        assert await second.get_tenant(tenant.id) == tenant
        assert (await second.get_channel(tenant.id, channel.id)).id == channel.id
