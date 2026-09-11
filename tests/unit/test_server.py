"""Gryphon v2 structured tool, ownership, lifecycle, and context-budget tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.tools import FunctionTool

from gryphon.errors import (
    CacheError,
    CapacityError,
    ConflictError,
    ExecutionError,
    ExecutionTimeoutError,
    FunctionNotFoundError,
    InputValidationError,
    LintError,
    SecurityViolationError,
    ServerNotFoundError,
)
from gryphon.models import CacheSummary, EndpointManifest, ExecutionResult, FunctionInfo, RunRecord, ServerInfo
from gryphon.runtime.context import bounded_json, bounded_page, bounded_text, json_bytes, public_result
from gryphon.server import _BASE_INSTRUCTIONS, create_server

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

Bundle = tuple[FastMCP, MagicMock, MagicMock, MagicMock]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def components(gryphon_config: GryphonConfig) -> Bundle:
    """Return model-backed catalog metadata and isolated runtime dependencies."""
    registry, cache, executor = MagicMock(), MagicMock(), MagicMock()
    registry.list_servers.return_value = [ServerInfo(name="weather", description="Weather API", functions=["current"])]
    # Skills disabled by default; content is fetched only by an explicit tool call.
    registry.has_skills.return_value = False
    registry.skills_path.return_value = None
    registry.fingerprint.return_value = "catalog-v2"
    registry.search_functions.return_value = [{"server_name": "weather", "function_name": "current"}]
    registry.get_function.return_value = FunctionInfo(
        server_name="weather",
        function_name="current",
        summary="Current weather",
        source_code='result = await call_tool("weather.current", {"city": ""})',
        method="GET",
        path="/current",
    )
    registry.get_endpoint.return_value = EndpointManifest(
        function_name="current",
        summary="Current weather",
        method="GET",
        path="/current",
        parameters_summary="",
        response_summary="",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        output_schema={"type": "object", "properties": {"temperature": {"type": "number"}}},
    )
    summary = CacheSummary(id="recipe", description="Get weather", servers_used=["weather"], use_count=1, created_at=1)
    cache.search = AsyncMock(return_value=[summary])
    cache.initialize, cache.close = AsyncMock(), AsyncMock()
    executor.startup, executor.shutdown = AsyncMock(), AsyncMock()
    executor.execute = AsyncMock(return_value=ExecutionResult(success=True, data=42, cache_id="recipe"))
    executor.replay = AsyncMock(return_value=ExecutionResult(success=True, data=84, cache_id="recipe"))
    executor.submit = AsyncMock(return_value=RunRecord(id="run", request_hash="private", created_at=1, updated_at=1))
    executor.get_run = AsyncMock(return_value=None)
    executor.cancel = AsyncMock(return_value=True)
    executor.artifacts.read = AsyncMock(return_value={"text": "chunk", "next_offset": 5, "eof": False})
    return create_server(gryphon_config, registry, cache, executor), registry, cache, executor


async def _call(mcp: FastMCP, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke the registered guarded handler, retaining direct failure-path coverage."""
    tool = await mcp.get_tool(name)
    assert isinstance(tool, FunctionTool)
    result = await tool.fn(**arguments)
    assert isinstance(result, dict)
    return result


# ---------------------------------------------------------------------------
# create_server — basic and initialize_server replacement
# ---------------------------------------------------------------------------


async def test_create_server_supplied_dependencies_are_never_initialized(components: Bundle) -> None:
    """The CLI retains complete lifecycle ownership of injected dependencies."""
    mcp, registry, cache, executor = components
    async with Client(mcp):
        assert len(await mcp.list_tools()) == 10
    registry.load.assert_not_called()
    cache.initialize.assert_not_awaited()
    cache.close.assert_not_awaited()
    executor.startup.assert_not_awaited()
    executor.shutdown.assert_not_awaited()


@pytest.mark.parametrize("owned", ["all", "registry", "cache", "executor"])
async def test_create_server_initializes_only_owned_dependencies(
    gryphon_config: GryphonConfig,
    components: Bundle,
    owned: str,
) -> None:
    """Optional setup occurs once inside FastMCP lifespan and closes owned resources."""
    _, registry, cache, executor = components
    flags = [owned in {"all", name} for name in ("registry", "cache", "executor")]
    with (
        patch("gryphon.runtime.context.Registry", return_value=registry),
        patch("gryphon.runtime.context.CacheStore", return_value=cache),
        patch("gryphon.runtime.context.CodeExecutor", return_value=executor),
    ):
        mcp = create_server(
            gryphon_config, *[None if flag else value for flag, value in zip(flags, components[1:], strict=True)]
        )
        registry.load.assert_not_called()
        async with Client(mcp):
            pass
    assert registry.load.call_count == int(flags[0])
    assert cache.initialize.await_count == cache.close.await_count == int(flags[1])
    assert executor.startup.await_count == executor.shutdown.await_count == int(flags[2])


# ---------------------------------------------------------------------------
# list_servers tool and search_functions
# ---------------------------------------------------------------------------


async def test_list_servers_returns_compact_metadata_without_function_dump(components: Bundle) -> None:
    """Discovery spends context on server metadata, not every endpoint name."""
    result = await _call(components[0], "list_servers")
    assert result == {
        "servers": [{"name": "weather", "description": "Weather API", "function_count": 1}],
        "registry_fingerprint": "catalog-v2",
        "truncated": False,
        "next_cursor": None,
        "total": 1,
    }


async def test_server_pages_obey_configured_cap(components: Bundle, gryphon_config: GryphonConfig) -> None:
    """A configured one-item cap preserves deterministic ordering and advances cursors by one."""
    mcp, registry, _, _ = components
    gryphon_config.discovery_limit = 1
    registry.list_servers.return_value = [ServerInfo(name=name, description="") for name in ["z", "a", "m"]]
    pages = [await _call(mcp, "list_servers", cursor=cursor, limit=10) for cursor in range(3)]
    assert [row["name"] for page in pages for row in page["servers"]] == ["a", "m", "z"]
    assert [len(page["servers"]) for page in pages] == [1, 1, 1]
    assert [page["next_cursor"] for page in pages] == [1, 2, None]
    assert pages[0]["truncated"] and not pages[-1]["truncated"]


@pytest.mark.parametrize("arguments", [{"cursor": -1}, {"limit": 0}, {"limit": 101}, {"cursor": True}])
async def test_list_servers_invalid_bounds_return_validation(components: Bundle, arguments: dict[str, Any]) -> None:
    """Invalid pagination returns a structured failure without querying the catalog."""
    assert (await _call(components[0], "list_servers", **arguments))["error_type"] == "validation"


async def test_search_functions_uses_registry_search(components: Bundle) -> None:
    """Search uses registry ranking and lookahead rather than dumping all functions."""
    result = await _call(components[0], "search_functions", query="weather", limit=2)
    components[1].search_functions.assert_called_once_with("weather", limit=3)
    assert result["functions"][0]["function_name"] == "current"


# ---------------------------------------------------------------------------
# get_functions tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 5])
async def test_get_functions_returns_real_schema_not_host_source(components: Bundle, count: int) -> None:
    """Inspection includes actual input schemas and the broker invocation contract."""
    result = await _call(
        components[0], "get_functions", functions=[{"server_name": "weather", "function_name": "current"}] * count
    )
    assert len(result["functions"]) == count
    assert result["functions"][0]["input_schema"] == components[1].get_endpoint.return_value.input_schema
    assert result["functions"][0]["output_schema"] == components[1].get_endpoint.return_value.output_schema
    assert result["functions"][0]["usage_example"] == components[1].get_function.return_value.source_code
    assert "source_code" not in json.dumps(result) and "import_statement" not in json.dumps(result)


@pytest.mark.parametrize(
    "functions",
    [
        [],
        [{}] * 6,
        [{}],
        [{"server_name": "weather"}],
        [{"server_name": "weather", "function_name": "current", "owner": "forged"}],
    ],
)
async def test_get_functions_invalid_batch_returns_validation(
    components: Bundle, functions: list[dict[str, str]]
) -> None:
    """Batch bounds and exact selector keys are enforced before registry inspection."""
    assert (await _call(components[0], "get_functions", functions=functions))["error_type"] == "validation"


@pytest.mark.parametrize(
    "exception,kind",
    [
        (ServerNotFoundError, "server_not_found"),
        (FunctionNotFoundError, "function_not_found"),
        (RuntimeError, "internal"),
    ],
)
async def test_get_functions_sanitizes_individual_errors(
    components: Bundle, exception: type[Exception], kind: str
) -> None:
    """Per-function failures never disclose registry exception contents."""
    components[1].get_function.side_effect = exception("private diagnostic")
    result = await _call(
        components[0], "get_functions", functions=[{"server_name": "weather", "function_name": "current"}]
    )
    assert result["functions"][0]["error_type"] == kind and "private diagnostic" not in json.dumps(result)


# ---------------------------------------------------------------------------
# execute_code and run_cached_code — structured inputs replace _apply_params_to_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["execute_code", "submit_code"])
async def test_execution_forwards_structured_inputs_and_trusted_owner(components: Bundle, tool: str) -> None:
    """Owner is derived from verified token identity, not a user-controlled input field."""
    code, inputs, schema = 'result = inputs["city"]', {"city": "Kandy", "owner": "forged"}, {"type": "object"}
    components[3].submit.return_value.owner = "verified"
    with patch(
        "gryphon.runtime.context.get_access_token",
        return_value=AccessToken(token="test", client_id="verified", scopes=[]),
    ):
        result = await _call(
            components[0],
            tool,
            code=code,
            description="Get weather",
            inputs=inputs,
            input_schema=schema,
            idempotency_key="retry",
        )
    target = components[3].execute if tool == "execute_code" else components[3].submit
    target.assert_awaited_once_with(
        code, "Get weather", inputs=inputs, input_schema=schema, owner="verified", idempotency_key="retry"
    )
    assert "owner" not in result and "request_hash" not in result


@pytest.mark.parametrize("params", [None, {}, {"city": "Kandy"}, {"city": "quote'\nresult = 999"}])
async def test_run_cached_code_never_rewrites_source(components: Bundle, params: dict[str, Any] | None) -> None:
    """Replay supplies data directly to the engine, including hostile-looking strings."""
    # The original code stays intact; no prepended assignment or _params dictionary.
    result = await _call(components[0], "run_cached_code", cache_id="recipe", params=params)
    components[3].replay.assert_awaited_once_with("recipe", inputs=params, owner="local")
    components[3].execute.assert_not_awaited()
    assert result["next"]["tool"] == "run_cached_code"


@pytest.mark.parametrize("tool", ["execute_code", "run_cached_code"])
@pytest.mark.parametrize(
    "error,kind",
    [
        (SecurityViolationError, "security"),
        (LintError, "lint"),
        (ExecutionTimeoutError, "timeout"),
        (ExecutionError, "execution"),
        (CacheError, "cache"),
        (InputValidationError, "validation"),
        (ConflictError, "conflict"),
        (CapacityError, "capacity"),
        (RuntimeError, "internal"),
    ],
)
async def test_execution_errors_are_sanitized(components: Bundle, tool: str, error: type[Exception], kind: str) -> None:
    """All execution failures retain categories without raw diagnostics or stderr."""
    target = components[3].execute if tool == "execute_code" else components[3].replay
    target.side_effect = error("private diagnostic")
    args = {"code": "result = 1", "description": "Compute"} if tool == "execute_code" else {"cache_id": "recipe"}
    result = await _call(components[0], tool, **args)
    assert result["error_type"] == kind and "private diagnostic" not in json.dumps(result)


@pytest.mark.parametrize("cached", [True, False])
def test_execution_result_only_hints_when_reusable(cached: bool) -> None:
    """Reuse hints require a successful result with an actual cache handle."""
    result = public_result(ExecutionResult(success=True, cache_id="recipe" if cached else None))
    assert ("next" in result) is cached


# ---------------------------------------------------------------------------
# Persistent receipts, artifacts, and owner-scoped recipe search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args,target",
    [
        ("get_run", {"run_id": "run"}, "get_run"),
        ("cancel_run", {"run_id": "run"}, "cancel"),
        ("list_recipes", {}, "search"),
        ("read_artifact", {"artifact_id": "artifact"}, "read"),
    ],
)
async def test_persistence_tools_forward_verified_owner(
    components: Bundle, tool: str, args: dict[str, Any], target: str
) -> None:
    """Ownership is passed through all persistence boundaries."""
    with patch(
        "gryphon.runtime.context.get_access_token",
        return_value=AccessToken(token="test", client_id="operator", scopes=[]),
    ):
        await _call(components[0], tool, **args)
    mock = (
        components[2].search
        if target == "search"
        else components[3].artifacts.read
        if target == "read"
        else getattr(components[3], target)
    )
    assert mock.call_args.kwargs["owner"] == "operator"


async def test_get_run_missing_and_cancel_rejected_are_indistinguishable(components: Bundle) -> None:
    """Missing and foreign IDs have the same non-disclosing response shape."""
    components[3].cancel.return_value = False
    assert (await _call(components[0], "get_run", run_id="foreign"))["error_type"] == "not_found"
    assert (await _call(components[0], "cancel_run", run_id="foreign"))["error_type"] == "not_found"


# ---------------------------------------------------------------------------
# _build_instructions replacement — on-demand skills, never inline embedding
# ---------------------------------------------------------------------------


async def test_create_server_skills_discovery_failure_does_not_crash(components: Bundle) -> None:
    """Construction never queries guide paths or reads guides into instructions."""
    mcp, registry, _, _ = components
    registry.list_servers.side_effect = RuntimeError("private diagnostic")
    assert mcp.instructions == _BASE_INSTRUCTIONS and len(mcp.instructions.encode()) < 512
    registry.skills_path.assert_not_called()
    assert not await mcp.list_resources()
    assert (await _call(mcp, "list_servers"))["error_type"] == "internal"


@pytest.mark.parametrize("enabled", [True, False])
async def test_skills_tools_are_optional(gryphon_config: GryphonConfig, components: Bundle, enabled: bool) -> None:
    """The optional flag controls only bounded guide tools, not sandbox catalog dumps."""
    gryphon_config.enable_additional_tools = enabled
    mcp = create_server(gryphon_config, *components[1:])
    names = {tool.name for tool in await mcp.list_tools()}
    assert ("list_skills" in names) is enabled and ("get_server_skills" in names) is enabled
    assert "get_sandbox_capabilities" not in names


async def test_skills_missing_returns_not_found(gryphon_config: GryphonConfig, components: Bundle) -> None:
    """Only registered guide paths are eligible for an on-demand read."""
    gryphon_config.enable_additional_tools = True
    mcp = create_server(gryphon_config, *components[1:])
    assert (await _call(mcp, "get_server_skills", server_name="weather"))["error_type"] == "not_found"
    components[1].get_manifest.assert_called_once_with("weather")


# ---------------------------------------------------------------------------
# reusable_code_guide prompt and bounded JSON context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["é" * 20000, '\x00\n"\\' * 20000, "a" * 50000])
@pytest.mark.parametrize("budget", [1024, 16384])
def test_context_text_measures_exact_json_bytes(text: str, budget: int) -> None:
    """Unicode and escape expansion cannot bypass the strict serialized byte ceiling."""
    payload = {"skills": text, "authorization": "none", "truncated": False}
    result = bounded_text(payload, "skills", budget)
    assert len(json_bytes(result)) <= budget and result["truncated"]
    assert result == bounded_text(payload, "skills", budget) and payload["skills"] == text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_context_rejects_non_json_values(value: Any) -> None:
    """Strict JSON does not silently turn unsupported values into strings."""
    with pytest.raises((TypeError, ValueError)):
        bounded_json({"data": value}, 1024)


def test_context_page_does_not_return_partial_function_schema() -> None:
    """Oversized individual schemas produce an explicit error and advancing cursor."""
    result = bounded_page(
        "functions", [{"function_name": "huge", "input_schema": {"enum": ["x" * 5000]}}], "fingerprint", 1024
    )
    assert result["functions"] == [{"function_name": "huge", "truncated": True, "error_type": "context_limit"}]
    assert result["truncated"] and len(json_bytes(result)) <= 1024
