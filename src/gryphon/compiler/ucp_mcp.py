"""Adapt native MCP tools into bounded synthetic OpenAPI plus trusted bindings.

Synthetic POST routes are catalog identifiers, never actual upstream routes. Call
arguments retain Gryphon's wrapper: call_tool("shop.get_cart", {"json_body": {...}}).
Only initialize/tools/list discovery runs during import; no business tool is called.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gryphon.compiler.catalog import module_name
from gryphon.compiler.schemas import check_schema, validate_document_tree
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.ucp_profile import SHOPPING_SERVICE, bounded_json
from gryphon.errors import CompileError, UCPImportError
from gryphon.models import MCPBinding, SwaggerSource
from gryphon.security.mcp_client import tool_fingerprint

if TYPE_CHECKING:
    from gryphon.models import SaaSSpec

READ_CAPABILITIES = {
    "get_checkout": "dev.ucp.shopping.checkout",
    "get_cart": "dev.ucp.shopping.cart",
    "get_order": "dev.ucp.shopping.order",
    "search_catalog": "dev.ucp.shopping.catalog.search",
    "lookup_catalog": "dev.ucp.shopping.catalog.lookup",
    "get_product": "dev.ucp.shopping.catalog.lookup",
}
_WRITE_CAPABILITIES = {
    **{f"{verb}_checkout": "dev.ucp.shopping.checkout" for verb in ("create", "update", "complete", "cancel")},
    **{f"{verb}_cart": "dev.ucp.shopping.cart" for verb in ("create", "update", "cancel")},
}
_ANNOTATIONS = frozenset(
    {"description", "title", "$comment", "examples", "example", "deprecated", "readOnly", "writeOnly"}
)
_MAX_TOOLS = 1000
_MAX_WARNINGS = 95


def native_schema(raw: Any) -> dict[str, Any]:
    """Normalize only nonvalidation annotations; never discard unsupported constraints."""
    if not isinstance(raw, dict):
        raise CompileError("MCP schemas must be objects")
    validate_document_tree(raw)
    result = deepcopy(raw)
    pending = [result]
    while pending:
        node = pending.pop()
        if "$schema" in node and node.pop("$schema") not in (
            "https://json-schema.org/draft/2020-12/schema",
            "https://json-schema.org/draft/2020-12/schema#",
        ):
            raise CompileError("Unsupported MCP schema dialect")
        for annotation in _ANNOTATIONS:
            node.pop(annotation, None)
        properties = node.get("properties", {})
        if not isinstance(properties, dict) or any(not isinstance(child, dict) for child in properties.values()):
            raise CompileError("Unsupported MCP schema properties")
        pending.extend(properties.values())
        pending.extend(node[key] for key in ("items", "additionalProperties") if isinstance(node.get(key), dict))
    check_schema(result)
    return result


def eligible_tool(name: str, advertised: dict[str, list[dict[str, Any]]] | None, version: str) -> bool:
    """Match protocol capabilities, never tool-provided readOnlyHint annotations."""
    if advertised is None:
        return True
    capability = READ_CAPABILITIES.get(name, _WRITE_CAPABILITIES.get(name))
    if capability is None:
        return True
    return any(record.get("version") == version for record in advertised.get(capability, []))


def tools_to_openapi(
    tools: list[dict[str, Any]],
    endpoint: str,
    version: str,
    advertised: dict[str, list[dict[str, Any]]] | None,
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, MCPBinding]]:
    """Preserve every supported discovered tool for immutable network-free refiltering."""
    if len(tools) > _MAX_TOOLS:
        raise UCPImportError("MCP tools/list exceeds the supported tool count")
    bounded_json(tools, max_bytes)
    paths: dict[str, Any] = {}
    bindings: dict[str, MCPBinding] = {}
    warnings = {
        "MCP arguments use the json_body wrapper; caller must supply required UCP agent metadata.",
        "MCP schema format values are annotations, not URI or other format validation.",
        "Profile extension declarations do not compose or relax native tool schemas.",
    }
    unsupported = 0
    seen: set[str] = set()
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or name in seen:
            raise UCPImportError("MCP tools/list contains invalid or duplicate tool names")
        seen.add(name)
        if not eligible_tool(name, advertised, version):
            warnings.add(f"Excluded unadvertised capability tool: {name}")
            unsupported += 1
            continue
        try:
            alias = module_name(name.replace(".", "_"))
            operation = _operation(tool, alias)
        except CompileError:
            warnings.add(f"Excluded unsupported MCP tool schema or identifier: {name}")
            unsupported += 1
            continue
        if alias in bindings:
            raise UCPImportError("MCP tool aliases collide after normalization")
        paths[f"/__mcp__/{alias}"] = {"post": operation}
        bindings[alias] = MCPBinding(endpoint=endpoint, tool_name=name, tool_fingerprint=tool_fingerprint(tool))
    if not paths:
        raise UCPImportError("No supported advertised MCP tools; schemas may exceed the bounded JSON Schema subset")
    document = _document(paths, endpoint, version, advertised, warnings, unsupported, len(tools))
    bounded_json(document, max_bytes)
    return document, bindings


def _operation(tool: dict[str, Any], alias: str) -> dict[str, Any]:
    """Compile native validation semantics before the OpenAPI parser can omit a response schema."""
    request = native_schema(tool.get("inputSchema"))
    if request.get("type") != "object":
        raise CompileError("MCP inputSchema must declare object arguments")
    response = native_schema(tool["outputSchema"]) if "outputSchema" in tool else {}
    operation = {
        "operationId": alias,
        "summary": (str(tool.get("description", "")).strip().split("\n", 1)[0] or f"MCP {tool['name']}")[:200],
        "description": (
            "Pass native arguments inside json_body. Supply your real UCP platform profile when meta.ucp-agent.profile "
            "is required; Gryphon does not generate that identity. " + str(tool.get("description", ""))
        )[:1000],
        "requestBody": {"required": True, "content": {"application/json": {"schema": request}}},
        "responses": {
            "200": {"description": "MCP structured result", "content": {"application/json": {"schema": response}}}
        },
    }
    parser = SwaggerParser(SwaggerSource(name="ucp", swagger_url="native-mcp", is_read_only=False))
    parser._raw_doc = {"paths": {f"/__mcp__/{alias}": {"post": operation}}}
    parsed = parser._parse_paths()[0]
    if parsed.response_json_schema != response or parsed.request_body_schema != request:
        raise CompileError("MCP contract cannot be preserved by the bounded compiler")
    return operation


def _document(
    paths: dict[str, Any],
    endpoint: str,
    version: str,
    advertised: dict[str, list[dict[str, Any]]] | None,
    warnings: set[str],
    unsupported: int,
    total: int,
) -> dict[str, Any]:
    """Bound diagnostic rows independently of the number of excluded tools."""
    rows = sorted(warnings)
    if len(rows) > _MAX_WARNINGS:
        rows = rows[:_MAX_WARNINGS] + ["Additional MCP exclusions omitted; see unsupported operation count."]
    return {
        "openapi": "3.1.0",
        "info": {"title": "UCP Shopping MCP tools", "version": version},
        "servers": [{"url": endpoint}],
        "paths": paths,
        "x-gryphon-ucp": {
            "version": version,
            "service": SHOPPING_SERVICE,
            "transport": "mcp",
            "advertised_capabilities": sorted(advertised or {}),
            "warnings": rows,
            "unsupported_operations": unsupported,
            "total_operations": total,
        },
    }


def selected_document(spec: SaaSSpec) -> dict[str, Any]:
    """Select saved MCP aliases using trusted binding names without consulting network or hints."""
    return filter_document(spec.document, spec.mcp_bindings, spec.read_only_filter)


def filter_document(
    document: dict[str, Any],
    bindings: dict[str, MCPBinding],
    read_only_filter: bool,
) -> dict[str, Any]:
    """Apply MCP read visibility without treating POST transport as an HTTP read method."""
    if not bindings:
        return document
    result = deepcopy(document)
    paths: dict[str, Any] = {}
    for path, item in result.get("paths", {}).items():
        operation = item.get("post", {})
        binding = bindings.get(operation.get("operationId"))
        if binding is None or set(item) != {"post"}:
            raise CompileError("MCP snapshot contains an operation without a trusted binding")
        if not read_only_filter or binding.tool_name in READ_CAPABILITIES:
            paths[path] = item
    result["paths"] = paths
    return result
