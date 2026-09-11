"""Optional, fail-closed offline Docker compute profile for Gryphon."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiodocker

from gryphon.errors import CapacityError, DockerUnavailableError, ExecutionError, ExecutionTimeoutError
from gryphon.models import ExecutionResult
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer
    from aiodocker.stream import Stream

    from gryphon.config import GryphonConfig
    from gryphon.models import ExecutionScope

logger = get_logger(__name__)
_CLEANUP_SECONDS = 10
_PROTOCOL_OVERHEAD = 4096
_MEMORY_SUFFIXES = {"k": 1024, "m": 1048576, "g": 1073741824}


def _parse_memory_bytes(value: str) -> int:
    """Parse an operator memory limit, rejecting nonpositive values."""
    value = value.strip().lower()
    try:
        result = int(value[:-1]) * _MEMORY_SUFFIXES[value[-1]] if value[-1] in _MEMORY_SUFFIXES else int(value)
    except (ValueError, IndexError) as exc:
        raise DockerUnavailableError("Invalid Docker memory limit") from exc
    if result <= 0:
        raise DockerUnavailableError("Invalid Docker memory limit")
    return result


class DockerSandbox:
    """Create an isolated, networkless container per execution; never share state."""

    def __init__(self, config: GryphonConfig) -> None:
        """Remember policy without opening sockets or starting system services."""
        self._config = config
        self._docker: aiodocker.Docker | None = None
        self._owned: dict[str, DockerContainer] = {}
        self._session = uuid.uuid4().hex

    async def startup(self) -> None:
        """Require a reachable daemon, existing image, and explicitly selected runtime.

        Raises:
            DockerUnavailableError: On any unavailable requirement; no downgrade.
        """
        if self._docker is not None:
            return
        client: aiodocker.Docker | None = None
        try:
            client = aiodocker.Docker(url=self._config.docker_host or None)
            async with asyncio.timeout(_CLEANUP_SECONDS):
                info = await client.system.info()
                runtime = self._config.docker_runtime
                if not runtime or runtime not in info.get("Runtimes", {}):
                    raise DockerUnavailableError("Configured Docker runtime is unavailable; refusing to downgrade")
                await client.images.inspect(self._config.docker_image)
                self._base_host_config()
        except BaseException as exc:
            if client is not None:
                await finish_cleanup(client.close())
            if not isinstance(exc, Exception):
                raise
            logger.warning("docker_unavailable", error_type=type(exc).__name__)
            raise DockerUnavailableError("Docker image, daemon, or configured runtime is unavailable") from None
        self._docker = client

    async def close(self) -> None:
        """Delete only owned containers and close the client despite caller cancellation."""
        await finish_cleanup(self._close())

    async def _close(self) -> None:
        """Attempt every owned deletion even when another container cannot be removed."""
        try:
            results = await asyncio.gather(
                *(self._delete(container) for container in list(self._owned.values())), return_exceptions=True
            )
            if any(isinstance(result, BaseException) for result in results):
                raise ExecutionError("Owned Docker container cleanup failed")
        finally:
            client, self._docker = self._docker, None
            if client is not None:
                await client.close()

    def _base_host_config(self) -> dict[str, Any]:
        """Build enforced resource limits with no binds, credentials, or networking."""
        memory = _parse_memory_bytes(self._config.container_memory_limit)
        return {
            "Memory": memory,
            "MemorySwap": memory,
            "CpuPeriod": 100_000,
            "CpuQuota": 50_000,
            "SecurityOpt": ["no-new-privileges:true"],
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "size=64m,mode=1777,noexec,nosuid,nodev"},
            "NetworkMode": "none",
            "PidsLimit": 64,
            "Runtime": self._config.docker_runtime,
            "LogConfig": {"Type": "none"},
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}, {"Name": "core", "Soft": 0, "Hard": 0}],
        }

    async def run(self, code: str, inputs: dict[str, Any], scope: ExecutionScope) -> ExecutionResult:
        """Stream one request and bounded stdout/stderr; always remove the container.

        Args:
            code: Guarded, prepared offline Python.
            inputs: Detached JSON input object, delivered over stdin.
            scope: Server authority; Docker has no broker capability.

        Returns:
            A strictly parsed JSON execution result without raw-output fallback.
        """
        if self._docker is None:
            raise DockerUnavailableError("Docker sandbox startup has not completed")
        if scope.cancelled or not math.isfinite(scope.deadline) or time.monotonic() >= scope.deadline:
            raise ExecutionTimeoutError("Execution scope expired")
        config: dict[str, Any] = {
            "Image": self._config.docker_image,
            "User": "1000:1000",
            "WorkingDir": "/workspace",
            "Entrypoint": ["python", "-I", "-B", "/workspace/entrypoint.py"],
            "Cmd": [],
            "OpenStdin": True,
            "StdinOnce": True,
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "NetworkDisabled": True,
            "Labels": {"gryphon.session": self._session, "gryphon.run": scope.run_id},
            "HostConfig": self._base_host_config(),
        }
        name = f"gryphon-{self._session[:12]}-{uuid.uuid4().hex[:12]}"
        creating = asyncio.create_task(self._docker.containers.create(config=config, name=name))
        container: DockerContainer | None = None
        try:
            async with asyncio.timeout_at(scope.deadline):
                container = await asyncio.shield(creating)
                self._owned[container.id] = container
                logger.info("docker_container_created", run_id=scope.run_id)
                return await self._communicate(container, code, inputs)
        except TimeoutError:
            logger.warning("docker_execution_timeout", run_id=scope.run_id)
            raise ExecutionTimeoutError("Offline execution timed out") from None
        except aiodocker.exceptions.DockerError:
            raise ExecutionError("Docker execution failed") from None
        finally:
            scope.cancelled = True
            await finish_cleanup(self._cleanup_creation(creating, container))

    async def _cleanup_creation(
        self,
        creating: asyncio.Task[DockerContainer],
        container: DockerContainer | None,
    ) -> None:
        """Await late creation and remove it without losing ownership on cancellation."""
        if container is None:
            with suppress(Exception):
                container = await creating
                self._owned[container.id] = container
        if container is not None:
            await self._delete(container)

    async def _communicate(self, container: DockerContainer, code: str, inputs: dict[str, Any]) -> ExecutionResult:
        """Use a single JSON line rather than environment variables or host mounts."""
        payload = {
            "code": code,
            "inputs": inputs,
            "timeout": self._config.execution_timeout_seconds,
            "max_output": self._config.max_output_size_bytes,
            "max_response": self._config.max_response_size_bytes,
        }
        request_limit = self._config.max_response_size_bytes + self._config.max_code_size_bytes * 6 + _PROTOCOL_OVERHEAD
        request = json_bytes(payload, request_limit) + b"\n"
        async with container.attach(stdin=True, stdout=True, stderr=True) as stream:
            await container.start()
            await stream.write_in(request)
            raw = await self._read_output(stream)
        status = await container.wait()
        if status.get("StatusCode") == 124:
            raise ExecutionTimeoutError("Offline execution timed out")
        if status.get("StatusCode") == 137:
            raise CapacityError("Offline sandbox was killed by a resource limit")
        if status.get("StatusCode") != 0:
            raise ExecutionError("Offline sandbox terminated without a valid result")
        return self._parse_output(raw)

    async def _read_output(self, stream: Stream) -> bytes:
        """Bound combined stream bytes while omitting stderr content entirely."""
        chunks: list[bytes] = []
        total = stderr_bytes = 0
        maximum = self._config.max_response_size_bytes + self._config.max_output_size_bytes + _PROTOCOL_OVERHEAD
        while (message := await stream.read_out()) is not None:
            total += len(message.data)
            if message.stream == 2:
                stderr_bytes += len(message.data)
            if total > maximum or stderr_bytes > self._config.max_output_size_bytes:
                raise CapacityError("Docker stdout/stderr exceeds output limit")
            if message.stream == 1:
                chunks.append(message.data)
        return b"".join(chunks)

    def _parse_output(self, raw: bytes) -> ExecutionResult:
        """Reject invalid JSON, coercible success values, and untrusted error text."""
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or type(payload.get("success")) is not bool:
                raise ValueError("Invalid envelope")
            json_bytes(payload, self._config.max_response_size_bytes)
            if not payload["success"]:
                kind = payload.get("error_type")
                if kind == "timeout":
                    raise ExecutionTimeoutError("Offline execution timed out")
                if kind == "capacity":
                    raise CapacityError("Offline execution exceeded a resource limit")
                raise ExecutionError("Offline Python execution failed")
            if "data" not in payload:
                raise ValueError("Missing result")
            printed = payload.get("printed_bytes", 0)
            if type(printed) is not int or printed < 0 or printed > self._config.max_output_size_bytes:
                raise ValueError("Invalid printed output counter")
            prints = f"Sandbox printed {printed} bytes (content omitted)." if printed else None
            return ExecutionResult(success=True, data=payload["data"], prints=prints)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ExecutionError("Sandbox returned invalid JSON output") from None

    async def _delete(self, container: DockerContainer) -> None:
        """Force-kill and remove one owned container; retain failed cleanup for close."""
        try:
            async with asyncio.timeout(_CLEANUP_SECONDS):
                await container.delete(force=True)
            self._owned.pop(container.id, None)
            logger.info("docker_container_removed")
        except Exception as exc:
            logger.error("docker_container_cleanup_failed", error_type=type(exc).__name__)
            raise ExecutionError("Owned Docker container cleanup failed") from None
