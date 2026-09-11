"""Supported schema contract tests, separated to keep parser test files bounded."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from gryphon.compiler.catalog import input_schema
from gryphon.compiler.schemas import _MAX_SCHEMA_DEPTH, validate_document_tree
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError
from gryphon.models import SwaggerSource


def _parser() -> SwaggerParser:
    """Return a parser for direct structural tests without network or YAML duplication."""
    return SwaggerParser(SwaggerSource(name="test", swagger_url="unused", base_url="https://example.com"))


# ---------------------------------------------------------------------------
# _parse_parameters — local references and operation overrides
# ---------------------------------------------------------------------------


def test_parameter_reference_and_operation_override() -> None:
    """Operation metadata overrides path metadata for the same original wire key/location."""
    parser = _parser()
    parser._raw_doc = {"parameters": {"Limit": {"name": "limit", "in": "query", "type": "integer"}}}
    params = parser._parse_parameters(
        [
            {"$ref": "#/parameters/Limit"},
            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 3}},
        ]
    )
    assert len(params) == 1 and params[0].default == "3"


@pytest.mark.parametrize("names", [["petId", "pet_id"], ["x", "x"]])
def test_parameter_alias_and_location_collisions_rejected(names: list[str]) -> None:
    """A flat broker input object cannot represent ambiguous names or SDK aliases."""
    parameters = [
        {"name": names[0], "in": "query", "schema": {"type": "string"}},
        {"name": names[1], "in": "header", "schema": {"type": "string"}},
    ]
    with pytest.raises(CompileError, match="collide|Duplicate"):
        _parser()._parse_operation("/x", "GET", {"parameters": parameters}, [])


# ---------------------------------------------------------------------------
# _parse_request_body — JSON-only required bodies
# ---------------------------------------------------------------------------


def test_request_body_empty_returns_none() -> None:
    """Absent bodies have no synthetic JSON parameter."""
    assert _parser()._parse_request_body({}) is None


@pytest.mark.parametrize(
    "body",
    [
        {"content": {"text/plain": {"schema": {"type": "string"}}}},
        {"content": {"application/json": {"schema": {"oneOf": [{"type": "string"}]}}}},
    ],
)
def test_request_body_unsupported_contract_rejected(body: dict[str, Any]) -> None:
    """Unsupported media types and schema composition fail instead of silently dropping validation."""
    with pytest.raises(CompileError, match="Unsupported"):
        _parser()._parse_request_body(body)


def test_request_body_local_reference_preserves_schema() -> None:
    """Referenced body schemas are normalized before broker input validation."""
    parser = _parser()
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    parser._raw_doc = {"components": {"schemas": {"Body": schema}}}
    body = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}}
    assert parser._parse_request_body(body) == schema


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_required_json_body_in_input_schema(method: str) -> None:
    """Body requiredness is preserved for every supported method, including DELETE."""
    body = {
        "required": True,
        "content": {"application/json": {"schema": {"type": "array", "items": {"type": "integer"}}}},
    }
    endpoint = _parser()._parse_operation("/x", method, {"requestBody": body}, [])
    assert endpoint is not None
    schema = input_schema(endpoint)
    assert schema["required"] == ["json_body"] and schema["additionalProperties"] is False
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"json_body": [1]})
    assert not validator.is_valid({})
    assert not validator.is_valid({"json_body": ["wrong"]})
    assert not validator.is_valid({"json_body": [], "extra": True})


def test_typed_enum_and_default_metadata() -> None:
    """String-backed ParamSchema values become real JSON numbers and booleans."""
    endpoint = _parser()._parse_operation(
        "/x",
        "GET",
        {
            "parameters": [
                {"name": "count", "in": "query", "schema": {"type": "integer", "enum": [1, 2], "default": 1}},
                {"name": "enabled", "in": "query", "schema": {"type": "boolean", "default": False}},
            ]
        },
        [],
    )
    assert endpoint is not None
    properties = input_schema(endpoint)["properties"]
    assert properties["count"]["enum"] == [1, 2]
    assert properties["count"]["default"] == 1
    assert properties["enabled"]["default"] is False


# ---------------------------------------------------------------------------
# _parse_response_schema — successful status selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["200", "201", "202"])
def test_response_success_status_metadata(status: str) -> None:
    """Successful responses retain actual field names and declared types."""
    fields = _parser()._parse_response_schema(
        {status: {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
    )
    assert fields[0].name == "id" and fields[0].field_type == "integer"


def test_no_successful_response_returns_empty() -> None:
    """Error response shapes are not advertised as successful results."""
    assert _parser()._parse_response_schema({"404": {"description": "Not found"}}) == []


# ---------------------------------------------------------------------------
# _schema_to_fields — bounded recursion and response references
# ---------------------------------------------------------------------------


def test_response_depth_limit_and_composition_diagnostics() -> None:
    """Response-only unsupported shapes can be omitted with an explicit diagnostic."""
    parser = _parser()
    assert parser._schema_to_fields({"properties": {"x": {"type": "string"}}}, _MAX_SCHEMA_DEPTH + 1) == []
    assert parser._extract_response_fields({"schema": {"anyOf": []}}) == []


def test_response_array_and_nested_reference() -> None:
    """Array response item refs and nested object fields remain structured."""
    parser = _parser()
    parser._raw_doc = {"definitions": {"Item": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
    fields = parser._extract_response_fields({"schema": {"type": "array", "items": {"$ref": "#/definitions/Item"}}})
    assert fields[0].name == "items" and fields[0].nested
    nested = parser._schema_to_fields({"properties": {"meta": {"$ref": "#/definitions/Item"}}}, 0)
    assert nested[0].nested and nested[0].nested[0].name == "id"


# ---------------------------------------------------------------------------
# _extract_type — nullable type annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": ["string", "null"]}, "string"),
        ({"type": ["null"]}, "null"),
        ({"type": "integer"}, "integer"),
    ],
)
def test_extract_type_preserves_primary_json_type(schema: dict[str, Any], expected: str) -> None:
    """Compact SDK annotations do not erase JSON-null-only schemas."""
    assert _parser()._extract_type(schema) == expected


# ---------------------------------------------------------------------------
# _resolve_ref — local pointers only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["https://external.example/schema", "#/missing"])
def test_unresolved_or_external_reference_returns_none(ref: str) -> None:
    """No schema lookup makes a remote request."""
    assert _parser()._resolve_ref(ref) is None


def test_local_reference_supports_json_pointer_escaping() -> None:
    """RFC6901 escaped slashes and tildes resolve without extra traversal."""
    parser = _parser()
    parser._raw_doc = {"definitions": {"a/b~c": {"type": "string"}}}
    assert parser._resolve_ref("#/definitions/a~1b~0c") == {"type": "string"}


@pytest.mark.parametrize(
    "schema", [{"$ref": "#/defs/a"}, {"type": "object", "properties": {"x": {"$ref": "#/defs/a"}}}]
)
def test_recursive_reference_explicitly_rejected(schema: dict[str, Any]) -> None:
    """Recursive aliases cannot cause unbounded reference parsing."""
    parser = _parser()
    parser._raw_doc = {"defs": {"a": schema}}
    with pytest.raises(CompileError, match="reference"):
        parser._normalize_schema({"$ref": "#/defs/a"})


def test_recursive_yaml_tree_is_bounded() -> None:
    """YAML alias cycles are detected before paths or schemas are walked."""
    tree: dict[str, Any] = {}
    tree["cycle"] = tree
    with pytest.raises(CompileError, match="depth"):
        validate_document_tree(tree)
