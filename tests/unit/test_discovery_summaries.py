"""Opt-in summary discovery preserves compact defaults, strict storage, and reachable pages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from pydantic import ValidationError
from test_saas_store import store as store

from gryphon.errors import InputValidationError
from gryphon.models import Channel, FunctionInfo, ServerInfo
from gryphon.runtime.context import json_bytes
from gryphon.runtime.discovery import list_servers_page
from gryphon.server import create_server

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.saas_store import SaaSStore


def _registry(counts: list[int], description: str = "Description") -> MagicMock:
    """Expose model-backed metadata with no schema or source access required by discovery."""
    registry = MagicMock()
    registry.fingerprint.return_value = "f" * 64
    registry.list_servers.return_value = [
        ServerInfo(name=f"server_{index}", description=description, functions=[f"fn_{n:04d}" for n in range(count)])
        for index, count in reversed(list(enumerate(counts)))
    ]
    registry.get_function.side_effect = lambda server, name: FunctionInfo(
        server_name=server, function_name=name, summary="Stored summary", description=description, source_code="unused"
    )
    return registry


def test_summary_setting_defaults_and_legacy_json(gryphon_config: GryphonConfig) -> None:
    """Old persisted channels and omitted configuration preserve compact discovery."""
    assert gryphon_config.include_function_summaries is False
    channel = Channel.model_validate_json('{"id":"c","tenant_id":"t","name":"n","created_at":1}')
    assert channel.include_function_summaries is False
    assert json.loads(channel.model_dump_json())["include_function_summaries"] is False


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_summary_channel_json_requires_real_boolean(value: Any) -> None:
    """Persistence does not coerce truthy strings, integers, containers, or null."""
    payload = {"id": "c", "tenant_id": "t", "name": "n", "created_at": 1, "include_function_summaries": value}
    with pytest.raises(ValidationError):
        Channel.model_validate_json(json.dumps(payload))


async def test_summary_store_roundtrip_toggle_and_omitted_update(store: SaaSStore) -> None:
    """Each channel retains independent defaults and every toggle advances its revision."""
    tenant = await store.create_tenant("Tenant")
    compact = await store.create_channel(tenant.id, "Compact")
    detailed = await store.create_channel(tenant.id, "Detailed", include_function_summaries=True)
    for enabled in [False, True]:
        previous = detailed
        detailed = await store.update_channel(tenant.id, detailed.id, include_function_summaries=enabled)
        assert detailed.revision == previous.revision + 1
        assert (await store.get_channel(tenant.id, detailed.id)).include_function_summaries is enabled
    renamed = await store.update_channel(tenant.id, detailed.id, name="Renamed")
    assert renamed.include_function_summaries is True
    assert await store.get_channel(tenant.id, compact.id) == compact
    await store.close()
    await store.initialize()
    assert (await store.get_channel(tenant.id, detailed.id)).include_function_summaries is True


@pytest.mark.parametrize("value", ["true", "false", 1, 0, [], {}])
async def test_summary_store_invalid_boolean_rolls_back(store: SaaSStore, value: Any) -> None:
    """Store model validation is independent of API validation and changes no revision on failure."""
    tenant = await store.create_tenant("Tenant")
    channel = await store.create_channel(tenant.id, "Compact")
    with pytest.raises(ValidationError):
        await store.create_channel(tenant.id, "Invalid", include_function_summaries=value)
    with pytest.raises(ValidationError):
        await store.update_channel(tenant.id, channel.id, include_function_summaries=value)
    assert await store.list_channels(tenant.id) == [channel]


def test_summary_all_functions_fit_not_capped_at_ten(gryphon_config: GryphonConfig) -> None:
    """Requested and configured server limits never become implicit per-server function limits."""
    gryphon_config.include_function_summaries = True
    gryphon_config.discovery_limit = 1
    registry = _registry([32, 2])
    page = list_servers_page(registry, gryphon_config, 0, 10, 0)
    assert page["servers"][0]["functions"] == [{"name": f"fn_{n:04d}", "description": "Description"} for n in range(32)]
    assert page["next_cursor"] == 1 and page["next_function_cursor"] == 0
    assert page["truncated"] and page["registry_fingerprint"] == "f" * 64
    assert registry.get_endpoint.call_count == registry.get_function_source.call_count == 0


def test_summary_description_falls_back_to_stored_summary(gryphon_config: GryphonConfig) -> None:
    """An absent real description uses the function's endpoint summary without exposing source."""
    gryphon_config.include_function_summaries = True
    page = list_servers_page(_registry([1], ""), gryphon_config, 0, 10, 0)
    assert page["servers"][0]["functions"] == [{"name": "fn_0000", "description": "Stored summary"}]
    assert not page["truncated"] and page["next_cursor"] is page["next_function_cursor"] is None


@pytest.mark.parametrize("counts", [[], [0], [0, 0, 0]])
def test_summary_zero_functions_and_empty_catalog(gryphon_config: GryphonConfig, counts: list[int]) -> None:
    """Empty servers remain visible with empty arrays and completed continuations."""
    gryphon_config.include_function_summaries = True
    page = list_servers_page(_registry(counts), gryphon_config, 0, 10, 0)
    assert len(page["servers"]) == len(counts)
    assert all(row["functions"] == [] and row["function_count"] == 0 for row in page["servers"])
    assert not page["truncated"] and page["next_cursor"] is page["next_function_cursor"] is None


@pytest.mark.parametrize("budget", [1024, 16384, 262144])
@pytest.mark.parametrize("description", ["Short", "é漢" * 20000, '\x00\n"\\' * 10000])
def test_summary_byte_bounds_and_every_function_reachable(
    gryphon_config: GryphonConfig, budget: int, description: str
) -> None:
    """Huge UTF-8 and escape-heavy metadata cannot stall cursors or drop whole function arrays."""
    gryphon_config.include_function_summaries = True
    gryphon_config.context_budget_bytes = budget
    gryphon_config.discovery_limit = 2
    registry = _registry([25, 0, 24], description)
    positions: set[tuple[int, int]] = set()
    functions: list[tuple[str, str]] = []
    cursor, function_cursor = 0, 0
    while True:
        assert (cursor, function_cursor) not in positions
        positions.add((cursor, function_cursor))
        page = list_servers_page(registry, gryphon_config, cursor, 10, function_cursor)
        assert page == list_servers_page(registry, gryphon_config, cursor, 10, function_cursor)
        assert len(json_bytes(page)) <= min(budget, 16384)
        assert page["registry_fingerprint"] == "f" * 64 and 1 <= len(page["servers"]) <= 2
        for row in page["servers"]:
            assert "functions" in row and (row["functions"] or row["function_count"] == 0)
            functions.extend((row["name"], fn["name"]) for fn in row["functions"])
        if page["next_cursor"] is None:
            assert page["next_function_cursor"] is None
            break
        assert page["truncated"]
        cursor, function_cursor = page["next_cursor"], page["next_function_cursor"]
    assert functions == [
        (f"server_{index}", f"fn_{n:04d}") for index, count in [(0, 25), (2, 24)] for n in range(count)
    ]


@pytest.mark.parametrize("cursor,function_cursor", [(-1, 0), (True, 0), (0, -1), (0, True), (0, 2), (2, 0), (1, 1)])
def test_summary_invalid_cursors_rejected(gryphon_config: GryphonConfig, cursor: int, function_cursor: int) -> None:
    """Both cursor coordinates must refer to an actual deterministic catalog position."""
    gryphon_config.include_function_summaries = True
    with pytest.raises(InputValidationError):
        list_servers_page(_registry([2]), gryphon_config, cursor, 10, function_cursor)


def test_compact_rejects_nonzero_function_cursor(gryphon_config: GryphonConfig) -> None:
    """A function cursor cannot make a compact channel disclose summaries."""
    with pytest.raises(InputValidationError):
        list_servers_page(_registry([2]), gryphon_config, 0, 10, 1)


@pytest.mark.parametrize("enabled", [False, True])
async def test_summary_mcp_schema_is_server_owned(gryphon_config: GryphonConfig, enabled: bool) -> None:
    """Only pagination is client-controlled and real MCP exposes exactly the same eleven meta-tools."""
    gryphon_config.include_function_summaries = enabled
    registry = _registry([20])
    mcp = create_server(gryphon_config, registry, MagicMock(), MagicMock())
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert len(tools) == 11
        discovery = next(tool for tool in tools if tool.name == "list_servers")
        assert set(discovery.input_schema["properties"]) == {"cursor", "limit", "function_cursor"}
        assert ("with function names and descriptions" in str(discovery.description)) is enabled
        result = await client.call_tool("list_servers")
        assert result.structured_content is not None
        assert ("functions" in result.structured_content["servers"][0]) is enabled
        rejected = await client.call_tool(
            "list_servers", {"include_function_summaries": not enabled}, raise_on_error=False
        )
        assert rejected.is_error


def test_summary_oversized_names_keep_explicit_arrays_and_continuation(gryphon_config: GryphonConfig) -> None:
    """Even pathological identifiers receive marked bounded metadata rather than an erased server array."""
    gryphon_config.include_function_summaries = True
    gryphon_config.context_budget_bytes = 1024
    registry = _registry([1])
    registry.list_servers.return_value = [ServerInfo(name="s" * 2000, description="", functions=["f" * 2000])]
    page = list_servers_page(registry, gryphon_config, 0, 10, 0)
    assert len(json_bytes(page)) <= 1024
    assert page["servers"][0]["name_truncated"] and page["servers"][0]["functions"][0]["name_truncated"]
    assert page["truncated"] and page["next_cursor"] is None
