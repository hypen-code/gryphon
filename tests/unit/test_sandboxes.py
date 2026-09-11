"""Fail-closed offline sandbox tests with bounded mocked Docker streams."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gryphon.config import GryphonConfig
from gryphon.errors import (
    CapacityError,
    DockerUnavailableError,
    ExecutionError,
    ExecutionTimeoutError,
    InputValidationError,
)
from gryphon.models import ExecutionScope
from gryphon.runtime.docker_sandbox import DockerSandbox, _parse_memory_bytes
from gryphon.runtime.sandboxes import RestrictedSandbox


def _config(**overrides: Any) -> GryphonConfig:
    """Validate isolated settings without reading an operator's environment file."""
    options: dict[str, Any] = {"_env_file": None, **overrides}
    return GryphonConfig(**options)


def _docker() -> MagicMock:
    """Build a Docker client with explicit runtimes and a finite attached stream."""
    client = MagicMock()
    client.close = AsyncMock()
    client.system.info = AsyncMock(return_value={"Runtimes": {"runsc": {}}})
    client.images.inspect = AsyncMock(return_value={"Id": "offline-image"})
    container = AsyncMock()
    container.id = "owned-only"
    container.wait.return_value = {"StatusCode": 0}
    stream = AsyncMock()
    stream.__aenter__.return_value = stream
    stream.read_out.side_effect = [SimpleNamespace(stream=1, data=b'{"success":true,"data":42}'), None]
    container.attach = MagicMock(return_value=stream)
    client.containers.create = AsyncMock(return_value=container)
    return client


def _scope() -> ExecutionScope:
    """Create unexpired authority for a mocked run."""
    return ExecutionScope(run_id="run", deadline=time.monotonic() + 30)


async def test_docker_startup_missing_runtime_does_not_downgrade() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    client.system.info.return_value = {"Runtimes": {"runc": {}}}
    with patch("aiodocker.Docker", return_value=client), pytest.raises(DockerUnavailableError):
        await backend.startup()
    client.containers.create.assert_not_awaited()
    client.close.assert_awaited_once()


async def test_docker_startup_unavailable_daemon_never_starts_services() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    client.system.info.side_effect = ConnectionError("private socket diagnostic")
    with patch("aiodocker.Docker", return_value=client), pytest.raises(DockerUnavailableError) as error:
        await backend.startup()
    assert "private socket diagnostic" not in str(error.value)


async def test_docker_execution_enforces_offline_resource_and_mount_policy() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        result = await backend.run("42", {}, _scope())
        await backend.close()
    assert result.data == 42
    config = client.containers.create.call_args.kwargs["config"]
    assert config["User"] == "1000:1000" and config["NetworkDisabled"]
    assert "Env" not in config and "Binds" not in config["HostConfig"]
    assert config["HostConfig"] | {} == {
        "Memory": 268435456,
        "MemorySwap": 268435456,
        "CpuPeriod": 100000,
        "CpuQuota": 50000,
        "SecurityOpt": ["no-new-privileges:true"],
        "CapDrop": ["ALL"],
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": "size=64m,mode=1777,noexec,nosuid,nodev"},
        "NetworkMode": "none",
        "PidsLimit": 64,
        "Runtime": "runsc",
        "LogConfig": {"Type": "none"},
        "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}, {"Name": "core", "Soft": 0, "Hard": 0}],
    }
    client.containers.list.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"success":"true","data":1}',
        b'{"success":true}',
        b'{"success":true,"data":NaN}',
        b'{"success":true,"data":Infinity}',
    ],
)
def test_docker_invalid_json_output_never_falls_back_to_success(raw: bytes) -> None:
    backend = DockerSandbox(_config())
    with pytest.raises((ExecutionError, InputValidationError)):
        backend._parse_output(raw)


async def test_docker_stderr_overflow_deletes_owned_container() -> None:
    backend, client = DockerSandbox(_config(max_response_size_bytes=1024)), _docker()
    container = client.containers.create.return_value
    stream = container.attach.return_value
    stream.read_out.side_effect = [SimpleNamespace(stream=2, data=b"x" * 100_000), None]
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        with pytest.raises(CapacityError):
            await backend.run("42", {}, _scope())
        await backend.close()
    container.delete.assert_awaited_once_with(force=True)


async def test_docker_cancel_force_deletes_only_owned_container() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    container = client.containers.create.return_value
    stream = container.attach.return_value
    entered = asyncio.Event()

    async def read() -> None:
        """Hold the attached stream until the caller is cancelled."""
        entered.set()
        await asyncio.Event().wait()

    stream.read_out.side_effect = read
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        job = asyncio.create_task(backend.run("42", {}, _scope()))
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        await backend.close()
    container.delete.assert_awaited_once_with(force=True)
    client.containers.list.assert_not_called()


async def test_docker_start_failure_still_removes_created_container() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    container = client.containers.create.return_value
    container.start.side_effect = RuntimeError("start failed")
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        with pytest.raises(RuntimeError):
            await backend.run("42", {}, _scope())
        await backend.close()
    container.delete.assert_awaited_once_with(force=True)


async def test_docker_creation_cancellation_waits_for_owned_container_cleanup() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    container = client.containers.create.return_value
    assert isinstance(container, AsyncMock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(**kwargs: object) -> AsyncMock:
        """Model Docker accepting creation before returning its container handle."""
        entered.set()
        await release.wait()
        return container

    client.containers.create.side_effect = create
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        job = asyncio.create_task(backend.run("42", {}, _scope()))
        await entered.wait()
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not backend._owned
        await backend.close()
    container.delete.assert_awaited_once_with(force=True)


async def test_repeated_cancellation_cannot_interrupt_container_delete() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    container = client.containers.create.return_value
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delete(**kwargs: object) -> None:
        """Make cleanup observable after repeated caller cancellation."""
        entered.set()
        await release.wait()
        finished.set()

    container.delete.side_effect = delete
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        job = asyncio.create_task(backend.run("42", {}, _scope()))
        await entered.wait()
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert finished.is_set() and not backend._owned
        container.delete.assert_awaited_once_with(force=True)
        await backend.close()


async def test_docker_startup_cancellation_closes_unpublished_client() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    entered = asyncio.Event()

    async def info() -> None:
        """Hold daemon introspection until startup cancellation arrives."""
        entered.set()
        await asyncio.Event().wait()

    client.system.info.side_effect = info
    with patch("aiodocker.Docker", return_value=client):
        startup = asyncio.create_task(backend.startup())
        await entered.wait()
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
    client.close.assert_awaited_once()


@pytest.mark.parametrize("status, error", [(1, ExecutionError), (124, ExecutionTimeoutError), (137, CapacityError)])
async def test_docker_exit_statuses_fail_closed_and_cleanup(status: int, error: type[Exception]) -> None:
    backend, client = DockerSandbox(_config()), _docker()
    container = client.containers.create.return_value
    container.wait.return_value = {"StatusCode": status}
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        await backend.startup()
        with pytest.raises(error):
            await backend.run("42", {}, _scope())
        await backend.close()
    container.delete.assert_awaited_once_with(force=True)


@pytest.mark.parametrize(
    "kind, error", [("capacity", CapacityError), ("timeout", ExecutionTimeoutError), ("private error", ExecutionError)]
)
def test_docker_error_envelopes_never_reflect_untrusted_error_text(kind: str, error: type[Exception]) -> None:
    backend = DockerSandbox(_config())
    raw = ('{"success":false,"error_type":"' + kind + '","error":"never reflect this"}').encode()
    with pytest.raises(error) as captured:
        backend._parse_output(raw)
    assert "never reflect this" not in str(captured.value)


async def test_docker_cleanup_failure_does_not_skip_other_owned_containers() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    first, second = AsyncMock(), AsyncMock()
    first.id, second.id = "first", "second"
    first.delete.side_effect = RuntimeError("private diagnostic")
    backend._owned = {first.id: first, second.id: second}
    backend._docker = client
    with pytest.raises(ExecutionError):
        await backend.close()
    first.delete.assert_awaited_once_with(force=True)
    second.delete.assert_awaited_once_with(force=True)
    client.close.assert_awaited_once()


async def test_docker_execution_requires_started_and_unrevoked_authority() -> None:
    backend, client = DockerSandbox(_config()), _docker()
    with pytest.raises(DockerUnavailableError):
        await backend.run("42", {}, _scope())
    with patch("aiodocker.Docker", return_value=client):
        await backend.startup()
        scope = _scope()
        scope.cancelled = True
        with pytest.raises(ExecutionTimeoutError):
            await backend.run("42", {}, scope)
        await backend.close()
    client.containers.create.assert_not_awaited()


@pytest.mark.parametrize("value, expected", [("256m", 268435456), ("1g", 1073741824), ("4096", 4096)])
def test_memory_limit_parses_explicit_positive_units(value: str, expected: int) -> None:
    assert _parse_memory_bytes(value) == expected


@pytest.mark.parametrize("value", ["", "0", "-1m", "invalid"])
def test_memory_limit_invalid_values_fail_closed(value: str) -> None:
    with pytest.raises(DockerUnavailableError):
        _parse_memory_bytes(value)


def test_docker_response_limit_counts_the_entire_json_envelope() -> None:
    """Envelope fields consume the same resource budget as user data."""
    backend = DockerSandbox(_config(max_response_size_bytes=1024))
    raw = json.dumps({"success": True, "data": "x" * 1000, "printed_bytes": 0}).encode()
    with pytest.raises(InputValidationError):
        backend._parse_output(raw)


async def test_parallel_callbacks_do_not_double_count_broker_owned_calls() -> None:
    """One returning callback cannot pre-charge another callback's broker grant."""
    broker, registry = AsyncMock(), MagicMock()
    backend = RestrictedSandbox(_config(), registry, broker)
    scope = _scope()
    scope.max_calls = 2
    entered, release = asyncio.Event(), asyncio.Event()

    async def counted(server: str, function: str, arguments: dict[str, object], scope: ExecutionScope) -> int:
        """Increment atomically before awaiting, just as the production broker does."""
        scope.calls += 1
        entered.set()
        await release.wait()
        return 1

    broker.invoke.side_effect = counted
    state = {"calls": 0, "printed": 0, "overflow": False, "failure": None}
    callbacks: set[asyncio.Task[object]] = set()
    invoke = backend._capability(scope, callbacks, state)
    first = asyncio.create_task(invoke("weather.get"))
    await entered.wait()
    second = asyncio.create_task(invoke("weather.get"))
    await asyncio.sleep(0)
    release.set()
    tasks = [first, second]
    assert await asyncio.gather(*tasks) == [1, 1]
    assert scope.calls == 2 and state["calls"] == 2 and not callbacks
