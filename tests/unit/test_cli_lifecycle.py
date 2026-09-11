"""CLI lifecycle cleanup and isolated real CLI/demo smoke regressions."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic import SecretStr

from gryphon.__main__ import _build_parser, _cmd_run, _cmd_serve
from gryphon.errors import DockerUnavailableError
from gryphon.models import StaticAuthConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

    from gryphon.config import GryphonConfig
    from gryphon.models import SwaggerSource


@pytest.fixture
def services(gryphon_config: GryphonConfig) -> Iterator[dict[str, Any]]:
    """Replace every dependency with non-I/O mocks before a CLI startup.

    Args:
        gryphon_config: Shared settings pointing at isolated temporary storage.

    Yields:
        Dependency instances and constructor mocks used for lifecycle assertions.
    """
    import_module("gryphon.server")
    gryphon_config.compile_on_startup = False
    cache, broker, executor, mcp = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    registry = MagicMock()
    registry.list_servers.return_value = []
    objects = {"cache": cache, "broker": broker, "executor": executor, "mcp": mcp, "registry": registry}
    with ExitStack() as stack:
        paths = {
            "cache_factory": ("gryphon.runtime.cache.CacheStore", cache),
            "broker_factory": ("gryphon.security.broker.ToolBroker", broker),
            "executor_factory": ("gryphon.runtime.executor.CodeExecutor", executor),
            "server_factory": ("gryphon.server.create_server", mcp),
            "registry_factory": ("gryphon.runtime.registry.Registry", registry),
        }
        for name, (path, value) in paths.items():
            objects[name] = stack.enter_context(patch(path, return_value=value))
        objects["sources"] = stack.enter_context(
            patch("gryphon.compiler.orchestrator.Orchestrator.load_swagger_sources", return_value=[])
        )
        stack.enter_context(patch("gryphon.__main__.get_logger"))
        yield objects


async def test_serve_stdio_disables_banner_and_closes_all(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """Normal stdio serving owns exactly one startup and a full close sequence."""
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    assert await _cmd_serve(args) == 0
    services["mcp"].run_stdio_async.assert_awaited_once_with(show_banner=False)
    services["executor"].startup.assert_awaited_once()
    services["executor"].shutdown.assert_awaited_once()
    services["broker"].close.assert_awaited_once()
    services["cache"].close.assert_awaited_once()


async def test_serve_passes_started_dependencies_by_identity(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """Use the new executor registry/broker signature, not an auth dict as registry."""
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    await _cmd_serve(args)
    services["registry_factory"].assert_called_once_with(gryphon_config.compiled_output_dir)
    services["registry"].load.assert_called_once_with()
    services["executor_factory"].assert_called_once_with(
        gryphon_config,
        services["cache"],
        services["registry"],
        broker=services["broker"],
    )
    services["server_factory"].assert_called_once_with(
        gryphon_config,
        registry=services["registry"],
        cache=services["cache"],
        executor=services["executor"],
    )


async def test_serve_passes_typed_auth_to_broker(
    gryphon_config: GryphonConfig,
    services: dict[str, Any],
    weather_swagger_source: SwaggerSource,
) -> None:
    """Resolve stable module names but do not fetch tokens in the CLI."""
    auth = StaticAuthConfig(value="${FIXTURE_AUTH}")
    weather_swagger_source.name, weather_swagger_source.auth = "Weather API", auth
    services["sources"].return_value = [weather_swagger_source]
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    await _cmd_serve(args)
    services["broker_factory"].assert_called_once_with(
        gryphon_config,
        services["registry"],
        auth_configs={"weather_api": auth},
    )


async def test_serve_http_requires_auth_and_uses_loopback(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """Authenticated HTTP defaults to the configured loopback bind."""
    gryphon_config.http_auth_token = SecretStr("fixture-token-" * 4)
    args = _build_parser().parse_args(["serve", "--transport", "http"])
    args._config = gryphon_config
    assert await _cmd_serve(args) == 0
    services["mcp"].run_http_async.assert_awaited_once_with(host="127.0.0.1", port=8000, show_banner=False)


async def test_run_http_preserves_bind_overrides(gryphon_config: GryphonConfig, services: dict[str, Any]) -> None:
    """Compile-then-serve uses the same bind values without a second compile."""
    gryphon_config.http_auth_token = SecretStr("fixture-token-" * 4)
    gryphon_config.compile_on_startup = True
    args = _build_parser().parse_args(["run", "--transport", "http", "--host", "127.0.0.2", "--port", "9001"])
    args._config = gryphon_config
    with patch("gryphon.__main__._cmd_compile", new=AsyncMock(return_value=0)) as compile_command:
        assert await _cmd_run(args) == 0
    compile_command.assert_awaited_once_with(args)
    services["mcp"].run_http_async.assert_awaited_once_with(host="127.0.0.2", port=9001, show_banner=False)


@pytest.mark.parametrize("step", ["initialize", "cleanup_expired", "registry", "sources", "broker"])
async def test_serve_early_failure_closes_cache(
    gryphon_config: GryphonConfig,
    services: dict[str, Any],
    step: str,
) -> None:
    """Register the cache cleanup before initializing it or loading other dependencies."""
    failing = {
        "initialize": services["cache"].initialize,
        "cleanup_expired": services["cache"].cleanup_expired,
        "registry": services["registry"].load,
        "sources": services["sources"],
        "broker": services["broker_factory"],
    }[step]
    failing.side_effect = RuntimeError("fixture failure")
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    with pytest.raises(RuntimeError, match="fixture failure"):
        await _cmd_serve(args)
    services["cache"].close.assert_awaited_once()
    services["server_factory"].assert_not_called()


async def test_serve_executor_constructor_failure_closes_broker(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """An executor construction failure cannot leak the already constructed broker."""
    services["executor_factory"].side_effect = RuntimeError("fixture failure")
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    with pytest.raises(RuntimeError):
        await _cmd_serve(args)
    services["broker"].close.assert_awaited_once()
    services["cache"].close.assert_awaited_once()


@pytest.mark.parametrize("step", ["startup", "server", "transport", "shutdown"])
async def test_serve_late_failures_close_every_dependency(
    gryphon_config: GryphonConfig,
    services: dict[str, Any],
    step: str,
) -> None:
    """Startup, server creation, transport, and cleanup errors all unwind the full stack."""
    failing = {
        "startup": services["executor"].startup,
        "server": services["server_factory"],
        "transport": services["mcp"].run_stdio_async,
        "shutdown": services["executor"].shutdown,
    }[step]
    failing.side_effect = RuntimeError("fixture failure")
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    with pytest.raises(RuntimeError):
        await _cmd_serve(args)
    services["executor"].shutdown.assert_awaited_once()
    services["broker"].close.assert_awaited_once()
    services["cache"].close.assert_awaited_once()


async def test_serve_cancellation_closes_every_dependency(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """Task cancellation still runs deterministic shutdown callbacks."""
    services["mcp"].run_stdio_async.side_effect = asyncio.CancelledError
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    with pytest.raises(asyncio.CancelledError):
        await _cmd_serve(args)
    services["executor"].shutdown.assert_awaited_once()
    services["broker"].close.assert_awaited_once()
    services["cache"].close.assert_awaited_once()


async def test_optional_docker_unavailable_is_safe_failure(
    gryphon_config: GryphonConfig, services: dict[str, Any]
) -> None:
    """No degraded listener or service auto-start substitutes for requested Docker."""
    gryphon_config.sandbox_mode = "docker"
    services["executor"].startup.side_effect = DockerUnavailableError("sensitive-daemon-details")
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    with patch("subprocess.run", side_effect=AssertionError):
        assert await _cmd_serve(args) == 1
    services["server_factory"].assert_not_called()
    services["executor"].shutdown.assert_awaited_once()
    services["broker"].close.assert_awaited_once()
    services["cache"].close.assert_awaited_once()


_SMOKE_TIMEOUT = 60
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_ARGS = ["-m", "gryphon", "--env-file", "/dev/null"]


@pytest.fixture
def cli_smoke_env(tmp_path: Path) -> dict[str, str]:
    """Use public local input and temporary stores without inheriting operator settings."""
    source = {"name": "weather", "swagger_url": str(_REPO_ROOT / "examples/weather.yaml"), "is_read_only": True}
    config = tmp_path / "swaggers.yaml"
    config.write_text(json.dumps({"servers": [source]}), encoding="utf-8")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return {
        "HOME": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "USER": "gryphon-test",
        "LOGNAME": "gryphon-test",
        "SHELL": "/bin/sh",
        "TERM": "dumb",
        "TMPDIR": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GRYPHON_SWAGGER_CONFIG_FILE": str(config),
        "GRYPHON_COMPILED_OUTPUT_DIR": str(tmp_path / "compiled"),
        "GRYPHON_CACHE_DB_PATH": str(tmp_path / "data/cache.db"),
        "GRYPHON_RUN_DB_PATH": str(tmp_path / "data/runs.db"),
        "GRYPHON_ARTIFACT_DIR": str(tmp_path / "data/artifacts"),
        "GRYPHON_COMPILE_ON_STARTUP": "false",
        "GRYPHON_SANDBOX_MODE": "restricted",
        "GRYPHON_ALLOWED_DOMAINS": '["offline.invalid"]',
    }


def _run_smoke_process(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Capture a real child, killing and reaping its process group on timeout or failure."""
    with subprocess.Popen(
        [sys.executable, *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=_SMOKE_TIMEOUT)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        assert process.returncode == 0, stderr
        return stdout


def _assert_smoke_stores_released(tmp_path: Path) -> None:
    """Require real temporary stores and verify the child released exclusive recovery ownership."""
    assert (tmp_path / "data/cache.db").is_file() and (tmp_path / "data/runs.db").is_file()
    with (tmp_path / "data/runs.db.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def _assert_stdio_offline_reuse(client: Client[StdioTransport]) -> None:
    """Exercise structured FastMCP data and exact-source replay without broker calls."""
    executed = await client.call_tool(
        "execute_code",
        {
            "code": "result = sum(inputs['values'])",
            "description": "offline sum",
            "inputs": {"values": [2, 3, 5]},
            "input_schema": {
                "type": "object",
                "properties": {"values": {"type": "array", "items": {"type": "number"}}},
                "required": ["values"],
                "additionalProperties": False,
            },
        },
    )
    assert not executed.is_error and executed.data == executed.structured_content
    assert executed.data["success"] and executed.data["data"] == 10 and executed.data["tool_calls"] == 0
    assert executed.data["cache_id"]
    reused = await client.call_tool(
        "run_cached_code", {"cache_id": executed.data["cache_id"], "params": {"values": [10, 20, 30]}}
    )
    assert not reused.is_error and reused.data == reused.structured_content
    assert reused.data["success"] and reused.data["data"] == 60 and reused.data["tool_calls"] == 0
    assert reused.data["cache_id"] == executed.data["cache_id"]


async def test_cli_local_weather_compile_stdio_discovery_and_replay(
    tmp_path: Path,
    cli_smoke_env: dict[str, str],
) -> None:
    """The actual CLI validates without writes, compiles, and serves the offline MCP workflow."""
    before = set(tmp_path.rglob("*"))
    assert (
        await asyncio.to_thread(_run_smoke_process, [*_CLI_ARGS, "compile", "--dry-run"], tmp_path, cli_smoke_env) == ""
    )
    assert set(tmp_path.rglob("*")) == before
    stdout = await asyncio.to_thread(_run_smoke_process, [*_CLI_ARGS, "compile"], tmp_path, cli_smoke_env)
    repeated = await asyncio.to_thread(_run_smoke_process, [*_CLI_ARGS, "compile"], tmp_path, cli_smoke_env)
    assert json.loads(stdout) == json.loads(repeated)
    entry = json.loads(stdout)["mcpServers"]["gryphon"]
    manifest = json.loads((tmp_path / "compiled/weather/manifest.json").read_text(encoding="utf-8"))
    assert manifest["server_name"] == "weather" and manifest["endpoints"][0]["function_name"] == "get_forecast"
    client_dir = tmp_path / "client"
    client_dir.mkdir()
    client_env = {key: value for key, value in cli_smoke_env.items() if not key.startswith("GRYPHON_")}
    transport = StdioTransport(
        command=entry["command"],
        args=entry["args"],
        cwd=str(client_dir),
        env={**client_env, **entry["env"]},
        keep_alive=False,
        log_file=tmp_path / "stdio.log",
    )
    async with Client(transport, timeout=_SMOKE_TIMEOUT, init_timeout=_SMOKE_TIMEOUT) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert len(names) == 10 and "get_forecast" not in names
        servers = await client.call_tool("list_servers", {})
        assert not servers.is_error and servers.data == servers.structured_content
        assert servers.data["servers"][0]["name"] == "weather"
        assert servers.data["servers"][0]["function_count"] == 1
        found = await client.call_tool("search_functions", {"query": "weather"})
        assert found.data["registry_fingerprint"] == servers.data["registry_fingerprint"]
        assert found.data["functions"][0]["function_name"] == "get_forecast"
        details = await client.call_tool(
            "get_functions", {"functions": [{"server_name": "weather", "function_name": "get_forecast"}]}
        )
        assert set(details.data["functions"][0]["input_schema"]["required"]) == {"latitude", "longitude"}
        await _assert_stdio_offline_reuse(client)
    _assert_smoke_stores_released(tmp_path)


def test_demo_isolated_config_offline_sum_and_reuse(tmp_path: Path, cli_smoke_env: dict[str, str]) -> None:
    """Run the shipped demo itself and require successful native results for both computations."""
    stdout = _run_smoke_process([str(_REPO_ROOT / "examples/demo.py")], tmp_path, cli_smoke_env)
    labels = ["Gryphon catalog", "Focused discovery", "Restricted execution", "Typed recipe reuse"]
    results = []
    remaining = stdout
    for label in labels:
        assert remaining.startswith(label + "\n")
        value, end = json.JSONDecoder().raw_decode(remaining[len(label) + 1 :])
        results.append(value)
        remaining = remaining[len(label) + 1 + end :].lstrip()
    assert not remaining and results[0]["servers"] == [] and results[1]["functions"] == []
    executed, reused = results[2:]
    assert executed["success"] and executed["data"] == 10 and executed["tool_calls"] == 0
    assert reused["success"] and reused["data"] == 60 and reused["tool_calls"] == 0
    assert executed["cache_id"] and reused["cache_id"] == executed["cache_id"]
    _assert_smoke_stores_released(tmp_path)
