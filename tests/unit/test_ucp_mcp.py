"""Native MCP schema adaptation, immutable refiltering and trusted manifest contracts."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from gryphon.compiler.catalog import load_manifest
from gryphon.compiler.ucp_mcp import READ_CAPABILITIES, filter_document, native_schema, tools_to_openapi
from gryphon.errors import CompileError, UCPImportError
from gryphon.models import Channel, MCPBinding, SaaSSpec, SpecImport
from gryphon.saas_catalog import compile_catalog
from gryphon.saas_spec_import import SpecImporter
from gryphon.saas_upload import inspect_mcp, inspect_upload
from gryphon.security.mcp_client import tool_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.config import GryphonConfig

ENDPOINT = "https://merchant.myshopify.com/api/ucp/mcp"
VERSION = "2026-08-25"
LIMIT = 100000


def tool(name: str = "get_cart", **schema: Any) -> dict[str, Any]:
    """Build a Shopify-shaped native object schema retaining required caller agent metadata."""
    return {
        "name": name,
        "inputSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "meta": {
                    "type": "object",
                    "properties": {
                        "ucp-agent": {
                            "type": "object",
                            "properties": {"profile": {"type": "string", "format": "uri"}},
                            "required": ["profile"],
                            "additionalProperties": True,
                        }
                    },
                    "required": ["ucp-agent"],
                    "additionalProperties": True,
                },
            },
            "required": ["id", "meta"],
            "additionalProperties": True,
            **schema,
        },
    }


def snapshot(tools: list[dict[str, Any]] | None = None) -> SpecImport:
    """Keep every supported tool definition separate from trusted transport control metadata."""
    document, bindings = tools_to_openapi(tools or [tool()], ENDPOINT, VERSION, None, LIMIT)
    return SpecImport(
        document=document,
        mcp_bindings=bindings,
        source_type="ucp_url",
        source_transport="mcp",
        source_url="https://merchant.example/api/ucp/mcp",
        resolved_endpoint=ENDPOINT,
        resolved_profile_url="https://merchant.example/.well-known/ucp",
    )


def saved(imported: SpecImport, **updates: Any) -> SaaSSpec:
    """Create immutable synthetic control metadata without an operator database."""
    return SaaSSpec(
        **(
            imported.model_dump()
            | {"id": "a" * 32, "tenant_id": "b" * 32, "name": "Shop", "sha256": "c" * 64, "created_at": 1.0}
            | updates
        )
    )


def test_native_mcp_preserves_required_agent_and_open_objects() -> None:
    original = tool()
    schema = native_schema(original["inputSchema"])
    assert "$schema" not in schema and schema["additionalProperties"] is True
    assert schema["required"] == ["id", "meta"]
    assert schema["properties"]["meta"]["properties"]["ucp-agent"]["required"] == ["profile"]
    assert "$schema" in original["inputSchema"]
    imported = snapshot([original])
    assert imported.mcp_bindings["get_cart"].tool_fingerprint == tool_fingerprint(original)


@pytest.mark.parametrize(
    "constraint",
    [
        {"pattern": ".*"},
        {"allOf": [{"type": "object"}]},
        {"if": {}, "then": {}, "else": {}},
        {"$ref": "https://other.example/schema"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"unevaluatedProperties": False},
        {"exclusiveMinimum": True},
    ],
)
def test_unsupported_native_constraints_exclude_only_affected_tool(constraint: dict[str, Any]) -> None:
    imported = snapshot([tool(), tool("create_cart", **constraint)])
    assert set(imported.mcp_bindings) == {"get_cart"}
    assert imported.document["x-gryphon-ucp"]["unsupported_operations"] == 1
    assert any("create_cart" in row for row in imported.document["x-gryphon-ucp"]["warnings"])
    with pytest.raises(CompileError):
        native_schema(tool(**constraint)["inputSchema"])


def test_unsupported_output_schema_is_not_silently_discarded() -> None:
    invalid = tool("create_cart") | {"outputSchema": {"oneOf": [{"type": "object"}]}}
    imported = snapshot([tool(), invalid])
    assert set(imported.mcp_bindings) == {"get_cart"}


def test_all_six_known_reads_require_matching_profile_capabilities() -> None:
    advertised = {name: [{"version": VERSION}] for name in READ_CAPABILITIES.values()}
    tools = [tool(name) for name in READ_CAPABILITIES]
    document, bindings = tools_to_openapi(tools, ENDPOINT, VERSION, advertised, LIMIT)
    assert set(bindings) == set(READ_CAPABILITIES)
    del advertised["dev.ucp.shopping.cart"]
    _, narrowed = tools_to_openapi(tools, ENDPOINT, VERSION, advertised, LIMIT)
    assert set(narrowed) == set(bindings) - {"get_cart"}
    assert len(document["paths"]) == 6


def test_native_tool_annotations_cannot_grant_read_visibility() -> None:
    imported = snapshot([tool(), tool("create_cart") | {"annotations": {"readOnlyHint": True}}, tool("custom_read")])
    selected = filter_document(imported.document, imported.mcp_bindings, True)
    assert list(selected["paths"]) == ["/__mcp__/get_cart"]
    assert len(filter_document(imported.document, imported.mcp_bindings, False)["paths"]) == 3
    assert len(imported.document["paths"]) == 3


@pytest.mark.parametrize(
    "tools",
    [
        [tool(), tool()],
        [tool("custom-tool"), tool("custom_tool")],
        [tool("../bad")],
        [tool("create_cart", pattern="bad")],
    ],
)
def test_invalid_or_entirely_unsupported_catalog_fails_safely(tools: list[dict[str, Any]]) -> None:
    with pytest.raises(UCPImportError):
        tools_to_openapi(tools, ENDPOINT, VERSION, None, LIMIT)


async def test_saved_mcp_refilter_is_network_free_and_preserves_unseen_tools(gryphon_config: GryphonConfig) -> None:
    imported = await inspect_mcp(snapshot([tool(), tool("create_cart")]), gryphon_config, LIMIT, "shop", True)
    assert imported.diagnostics is not None and imported.diagnostics.available_operations == 1
    previous = saved(imported)
    with patch("gryphon.compiler.ucp_discovery.discover_tools", new_callable=AsyncMock) as discover:
        changed = await SpecImporter(gryphon_config, LIMIT).refilter(previous, False)
        restored = await SpecImporter(gryphon_config, LIMIT).refilter(saved(changed), True)
    discover.assert_not_awaited()
    assert type(changed) is SpecImport
    assert changed.diagnostics is not None and changed.diagnostics.available_operations == 2
    assert restored.diagnostics == imported.diagnostics
    assert changed.document == imported.document and changed.mcp_bindings == imported.mcp_bindings
    assert changed.source_url == imported.source_url and changed.resolved_endpoint == ENDPOINT


async def test_compiled_mcp_binding_is_authoritative_and_changes_fingerprint(gryphon_config: GryphonConfig) -> None:
    spec = saved(snapshot([tool(), tool("create_cart")]))
    channel = Channel(id="d" * 32, tenant_id=spec.tenant_id, name="Channel", spec_ids=[spec.id], created_at=1.0)
    registry = await compile_catalog(gryphon_config, channel, [spec])
    manifest = registry.get_manifest("shop")
    assert manifest.is_read_only is False
    assert [endpoint.function_name for endpoint in manifest.endpoints] == ["get_cart"]
    endpoint = registry.get_endpoint("shop", "get_cart")
    assert endpoint.mcp_binding == spec.mcp_bindings["get_cart"]
    assert endpoint.input_schema["required"] == ["json_body"]
    assert "json_body" in registry.get_function("shop", "get_cart").source_code
    bindings = dict(spec.mcp_bindings)
    bindings["get_cart"] = bindings["get_cart"].model_copy(update={"tool_fingerprint": "f" * 64})
    changed = await compile_catalog(gryphon_config, channel, [spec.model_copy(update={"mcp_bindings": bindings})])
    assert registry.fingerprint() != changed.fingerprint()


async def test_uploaded_mcp_extensions_never_become_transport_bindings(gryphon_config: GryphonConfig) -> None:
    imported = snapshot()
    document = copy.deepcopy(imported.document)
    document["x-mcp-bindings"] = {key: value.model_dump() for key, value in imported.mcp_bindings.items()}
    result = await inspect_upload(json.dumps(document), gryphon_config, LIMIT, "shop", read_only_filter=False)
    assert result.mcp_bindings == {} and result.source_transport is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "http://merchant.example/mcp"),
        ("endpoint", "https://user:secret@merchant.example/mcp"),
        ("endpoint", "https://merchant.example/mcp?secret=yes"),
        ("endpoint", "https://merchant.example/mcp#fragment"),
        ("tool_name", "bad name"),
        ("tool_fingerprint", "not-a-sha256"),
    ],
)
def test_binding_rejects_unsafe_authority(field: str, value: str) -> None:
    values = {"endpoint": ENDPOINT, "tool_name": "get_cart", "tool_fingerprint": "a" * 64}
    values[field] = value
    with pytest.raises(ValidationError):
        MCPBinding.model_validate(values)


@pytest.mark.parametrize(
    "change",
    [
        {"method": "GET"},
        {"path": "/actual-upstream-route"},
        {"request_body_schema": None},
        {"read_only_post": True},
        {"request_body_media_type": "multipart/form-data"},
        {"base_url": "https://other.example/mcp"},
        {"parameters": []},
    ],
)
async def test_manifest_rejects_inconsistent_mcp_dispatch_metadata(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    change: dict[str, Any],
) -> None:
    spec = saved(snapshot())
    channel = Channel(id="d" * 32, tenant_id=spec.tenant_id, name="Channel", spec_ids=[spec.id], created_at=1.0)
    registry = await compile_catalog(gryphon_config, channel, [spec])
    raw = registry.get_manifest("shop").model_dump(mode="json")
    raw["endpoints"][0].update(change)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(CompileError):
        load_manifest(path)


@pytest.mark.parametrize("schema", [None, [], {"properties": []}, {"properties": {"bad": False}}, {"$schema": []}])
def test_malformed_native_schema_rejected_without_coercion(schema: Any) -> None:
    with pytest.raises(CompileError):
        native_schema(schema)


def test_snapshot_filter_rejects_missing_trusted_binding() -> None:
    imported = snapshot([tool(), tool("create_cart")])
    with pytest.raises(CompileError, match="trusted binding"):
        filter_document(imported.document, {"get_cart": imported.mcp_bindings["get_cart"]}, True)


def test_tool_limits_and_warning_rows_are_bounded() -> None:
    with pytest.raises(UCPImportError, match="tool count"):
        tools_to_openapi([tool()] * 1001, ENDPOINT, VERSION, None, LIMIT)
    tools = [tool()] + [tool(f"invalid_{index}", pattern="unsupported") for index in range(100)]
    advertised = {"dev.ucp.shopping.cart": [{"version": VERSION}]}
    document, bindings = tools_to_openapi(tools, ENDPOINT, VERSION, advertised, LIMIT)
    assert len(bindings) == 1 and len(document["x-gryphon-ucp"]["warnings"]) <= 96
    assert document["x-gryphon-ucp"]["unsupported_operations"] == 100
