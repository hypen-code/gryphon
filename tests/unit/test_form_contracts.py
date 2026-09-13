"""Fail-closed compiler and encoder coverage for bounded OpenAPI form bodies."""

from __future__ import annotations

import ast
from typing import Any, Final

import pytest

from gryphon.compiler.catalog import validate_endpoint
from gryphon.compiler.codegen import CodeGenerator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError, InputValidationError
from gryphon.models import EndpointSpec, ServerSpec, SwaggerSource
from gryphon.security.form_encoding import check_form_schema, encode_form

_FORM: Final = "application/x-www-form-urlencoded"


def _parser() -> SwaggerParser:
    """Construct a parser without loading settings, files or network resources."""
    return SwaggerParser(SwaggerSource(name="synthetic", swagger_url="unused", base_url="https://example.test"))


def _body(schema: dict[str, Any], **media: Any) -> dict[str, Any]:
    """Build a request body with explicitly declared form serialization."""
    return {"content": {_FORM: {"schema": schema, **media}}}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": ["object", "null"]},
        {"type": "object", "additionalProperties": True},
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "object", "properties": {"x": {"type": "object"}}},
        {"type": "object", "properties": {"x": {"type": ["string", "null"]}}},
        {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "object"}}}},
        {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "null"}}}},
        {"type": "object", "properties": {"x": {"type": "array"}}},
    ],
)
def test_form_compiler_rejects_unsupported_field_contract(schema: dict[str, Any]) -> None:
    with pytest.raises(CompileError):
        _parser()._parse_request_body(_body(schema))


@pytest.mark.parametrize(
    "encoding", [{"x": {"explode": False}}, {"x": {"style": "deepObject"}}, {"x": {"headers": {}}}, [], False, None]
)
def test_form_compiler_rejects_explicit_serialization(encoding: Any) -> None:
    with pytest.raises(CompileError):
        _parser()._parse_request_body(_body({"type": "object"}, encoding=encoding))


@pytest.mark.parametrize("media_type", ["multipart/mixed", "text/plain", "application/octet-stream"])
def test_form_compiler_never_substitutes_unsupported_media(media_type: str) -> None:
    with pytest.raises(CompileError, match="media type"):
        _parser()._parse_request_body({"content": {media_type: {"schema": {"type": "object"}}}})


def test_form_compiler_prefers_explicit_json_alternative() -> None:
    body = _body({"type": "object"})
    body["content"]["application/json"] = {"schema": {"type": "array", "items": {"type": "string"}}}
    assert _parser()._request_body_media_type(body) == "application/json"
    assert _parser()._parse_request_body(body) == {"type": "array", "items": {"type": "string"}}


def test_form_compiler_closes_unspecified_properties() -> None:
    assert _parser()._parse_request_body(_body({"type": "object"})) == {"type": "object", "additionalProperties": False}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "additionalProperties": False, "properties": []},
        {"type": "object", "additionalProperties": False, "properties": {"x": False}},
    ],
)
def test_form_validator_rejects_malformed_properties(schema: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        check_form_schema(schema)


@pytest.mark.parametrize("body", [None, [], "x", {1: "x"}, {"x": None}, {"x": {}}, {"x": [[1]]}, {"x": float("inf")}])
def test_form_encoder_rejects_unsupported_values(body: Any) -> None:
    with pytest.raises(InputValidationError):
        encode_form(body)


def test_form_encoder_preserves_scalar_and_array_values() -> None:
    assert encode_form({"text": "a+b&c", "yes": True, "no": False, "integer": 1, "number": 1.2, "array": [1, 2]}) == {
        "text": "a+b&c",
        "yes": "true",
        "no": "false",
        "integer": "1",
        "number": "1.2",
        "array": ["1", "2"],
    }
    assert encode_form({}) == {}
    assert encode_form({"array": []}) == {"array": []}


def test_form_manifest_validation_rejects_open_body_contract() -> None:
    endpoint = EndpointSpec(
        path="/query",
        method="POST",
        operation_id="query",
        summary="Query",
        request_body_media_type=_FORM,
        request_body_schema={"type": "object"},
    )
    with pytest.raises(CompileError, match="form body"):
        validate_endpoint(endpoint)


def test_form_sdk_representation_uses_declared_encoding() -> None:
    endpoint = EndpointSpec(
        path="/query",
        method="POST",
        operation_id="query",
        summary="Query",
        request_body_media_type=_FORM,
        request_body_schema={"type": "object", "additionalProperties": False},
    )
    spec = ServerSpec(
        name="synthetic",
        description="Synthetic",
        base_url="https://example.test",
        is_read_only=False,
        swagger_hash="synthetic",
        endpoints=[endpoint],
    )
    code = CodeGenerator().generate(spec)
    ast.parse(code)
    assert "form_body=True" in code
    assert 'request_headers["Content-Type"] = "application/x-www-form-urlencoded"' in code
    assert "data=_form_data(json_body) if form_body else None" in code
