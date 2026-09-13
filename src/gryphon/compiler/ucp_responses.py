"""Import supported UCP response metadata, explicitly reporting normal compiler omissions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gryphon.compiler.schemas import SchemaParser
from gryphon.errors import CompileError

if TYPE_CHECKING:
    from gryphon.compiler.ucp_refs import ReferenceLoader

_OMITTED = "Unsupported response validation schema omitted; returned JSON is not schema-validated"


async def response_contracts(
    operation: dict[str, Any], loader: ReferenceLoader, base: str, warnings: set[str]
) -> dict[str, Any]:
    """Keep supported success schemas; never fetch unused error, signature or callback metadata."""
    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        raise CompileError("Invalid UCP response contracts")
    result: dict[str, Any] = {}
    for status, raw in sorted(responses.items(), key=lambda pair: str(pair[0])):
        if not str(status).startswith("2"):
            continue
        response, response_base = await loader.resolve(raw, base)
        retained: dict[str, Any] = {"description": response.get("description", "Successful UCP response")}
        if response.get("content"):
            if not isinstance(response["content"], dict):
                raise CompileError("Invalid UCP response content")
            try:
                schema = SchemaParser._media_schema(response["content"])
            except CompileError:
                warnings.add(_OMITTED)
            else:
                normalized = await _response_schema(schema, loader, response_base)
                if normalized is None:
                    warnings.add(_OMITTED)
                else:
                    retained["content"] = {"application/json": {"schema": normalized}}
        result[str(status)] = retained
    return result


async def _response_schema(raw: dict[str, Any], loader: ReferenceLoader, base: str) -> dict[str, Any] | None:
    """Avoid fetching branches of an already unsupported response, preserving supported constraints."""
    schema, schema_base = await loader.resolve(raw, base)
    parser = SchemaParser()
    parser._raw_doc = {}
    preview = _reference_preview(schema, loader)
    try:
        parser._normalize_schema(preview)
    except CompileError:
        return None
    expanded = await loader.expand(schema, schema_base)
    try:
        return parser._normalize_schema(expanded)
    except CompileError:
        return None


def _reference_preview(node: Any, loader: ReferenceLoader, depth: int = 0) -> Any:
    """Check locally visible validation semantics without resolving any nested remote reference."""
    loader.check_budget(depth)
    if isinstance(node, dict):
        if "$ref" in node:
            return {}
        return {key: _reference_preview(value, loader, depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return [_reference_preview(value, loader, depth + 1) for value in node]
    return node
