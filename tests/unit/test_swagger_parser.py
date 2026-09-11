"""Fixture-backed parser tests and supported routing/document regressions."""

from __future__ import annotations

import ast
from typing import Any

import pytest

from gryphon.compiler.codegen import CodeGenerator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import CompileError
from gryphon.models import ServerSpec, SwaggerSource


def _parser() -> SwaggerParser:
    """Construct a parser for structural tests without inline YAML fixtures."""
    return SwaggerParser(SwaggerSource(name="test", swagger_url="unused", base_url="https://example.com"))


async def test_parse_weather_api_returns_server_spec(weather_swagger_source: SwaggerSource) -> None:
    """Fixture parsing retains deterministic identity and source policy."""
    spec = await SwaggerParser(weather_swagger_source).parse()
    assert isinstance(spec, ServerSpec)
    assert spec.name == "weather"
    assert len(spec.swagger_hash) == 64  # SHA256 hex digest


async def test_weather_api_has_expected_endpoints(weather_swagger_source: SwaggerSource) -> None:
    """Historical fixture operation IDs remain available."""
    spec = await SwaggerParser(weather_swagger_source).parse()
    assert {ep.operation_id for ep in spec.endpoints} >= {"get_current_weather", "get_weather_forecast"}


async def test_weather_api_parameters_parsed(weather_swagger_source: SwaggerSource) -> None:
    """Requiredness, enum, original names and native schemas survive normalization."""
    spec = await SwaggerParser(weather_swagger_source).parse()
    endpoint = next(ep for ep in spec.endpoints if ep.operation_id == "get_current_weather")
    params = {param.name: param for param in endpoint.parameters}
    assert params["city"].required and params["city"].param_type == "string"
    assert not params["units"].required
    assert params["units"].enum == ["metric", "imperial", "kelvin"]
    assert params["city"].json_schema["type"] == "string"


async def test_readonly_server_excludes_mutating_methods(hotel_swagger_source: SwaggerSource) -> None:
    """Mutating routes are removed before codegen or manifest generation."""
    hotel_swagger_source.is_read_only = True
    spec = await SwaggerParser(hotel_swagger_source).parse()
    assert {ep.method for ep in spec.endpoints} <= {"GET", "HEAD", "OPTIONS"}
    # GET should still be present
    assert any(ep.method == "GET" for ep in spec.endpoints)


async def test_petstore_resolves_dollar_refs(petstore_swagger_source: SwaggerSource) -> None:
    """Local response references populate real field metadata."""
    spec = await SwaggerParser(petstore_swagger_source).parse()
    # The response schema should be populated (from resolved $ref)
    assert next(ep for ep in spec.endpoints if ep.operation_id == "list_pets").response_schema


async def test_swagger_hash_is_consistent(weather_swagger_source: SwaggerSource) -> None:
    """Repeated parse does not introduce nondeterministic document identity."""
    first = await SwaggerParser(weather_swagger_source).parse()
    second = await SwaggerParser(weather_swagger_source).parse()
    assert first.swagger_hash == second.swagger_hash


async def test_response_fields_populated(weather_swagger_source: SwaggerSource) -> None:
    """Response metadata is derived from actual schemas, not generated text."""
    spec = await SwaggerParser(weather_swagger_source).parse()
    endpoint = next(ep for ep in spec.endpoints if ep.operation_id == "get_current_weather")
    assert {field.name for field in endpoint.response_schema} >= {"temperature", "humidity", "condition"}
    assert endpoint.response_json_schema["properties"]["temperature"]["type"] == "number"


async def test_declared_path_param_preserves_wire_name(petstore_swagger_source: SwaggerSource) -> None:
    """SDK spelling never replaces original broker input keys."""
    spec = await SwaggerParser(petstore_swagger_source).parse()
    endpoint = next(ep for ep in spec.endpoints if ep.operation_id == "get_pet_by_id")
    param = next(param for param in endpoint.parameters if param.location == "path")
    assert (param.name, param.param_type, param.required) == ("petId", "integer", True)


def test_undeclared_path_param_detected_and_encoded() -> None:
    """Missing declarations get required wire parameters and literal-safe SDK encoding."""
    endpoint = _parser()._parse_operation("/services/{serviceName}/agent", "GET", {"operationId": "get_agent"}, [])
    assert endpoint is not None
    spec = ServerSpec(
        name="test",
        description="test",
        base_url="https://example.com",
        is_read_only=True,
        swagger_hash="abc",
        endpoints=[endpoint],
    )
    code = CodeGenerator().generate(spec)
    # Valid Python
    ast.parse(code)
    # Param in function signature
    assert "service_name: str" in code
    # Literal-safe replacement with encoded path parameter in URL
    assert "urllib.parse.quote(str(service_name), safe='')" in code


# ---------------------------------------------------------------------------
# _load_document — supported versions and bounded trees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["[]", "key: [unclosed", '{"openapi":"3.2.0","paths":{}}'])
def test_load_document_invalid_or_unsupported_rejected(content: str) -> None:
    """Malformed documents and unsupported versions fail explicitly."""
    with pytest.raises(CompileError):
        _parser()._load_document(content)


# ---------------------------------------------------------------------------
# _resolve_base_url — explicit destination authority
# ---------------------------------------------------------------------------


def test_resolve_base_url_trusted_source_overrides_document() -> None:
    """Explicit source config wins even over operation-level destinations."""
    parser = _parser()
    parser._raw_doc = {"servers": [{"url": "https://untrusted.example"}]}
    endpoint = parser._parse_operation("/thing", "GET", {"servers": [{"url": "https://untrusted.example"}]}, [])
    assert parser._resolve_base_url() == "https://example.com"
    assert endpoint is not None and endpoint.base_url == ""


@pytest.mark.parametrize("servers", [[], ["invalid"], [{"url": ""}], [{"url": "/relative"}]])
def test_resolve_base_url_invalid_document_destination_rejected(servers: list[Any]) -> None:
    """Missing and malformed destinations require explicit trusted configuration."""
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    parser._raw_doc = {"servers": servers}
    with pytest.raises(CompileError):
        parser._resolve_base_url()


# ---------------------------------------------------------------------------
# _collect_extra_server_url_vars — SDK destination mapping
# ---------------------------------------------------------------------------


def test_collect_extra_urls_when_source_has_no_override() -> None:
    """SDK alternate destinations remain configurable when no trusted override exists."""
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    parser._raw_doc = {
        "servers": [{"url": "https://primary.example"}, {"url": "https://cdn.example"}],
        "paths": {"/x": {"get": {"servers": [{"url": "https://op.example"}]}}},
    }
    assert parser._resolve_base_url() == "https://primary.example"
    assert parser._collect_extra_server_url_vars("https://primary.example") == {
        "https://cdn.example": "_BASE_URL_1",
        "https://op.example": "_BASE_URL_2",
    }


# ---------------------------------------------------------------------------
# _resolve_endpoint_base_url — documented SDK priority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "paths", "expected"),
    [
        ({"servers": [{"url": "https://op.example"}]}, [{"url": "https://path.example"}], "https://op.example"),
        ({}, [{"url": "https://path.example"}], "https://path.example"),
        ({}, [], ""),
    ],
)
def test_endpoint_url_priority(operation: dict[str, Any], paths: list[Any], expected: str) -> None:
    """Without a trusted override, operation servers take priority over path servers."""
    assert SwaggerParser._resolve_endpoint_base_url(operation, paths) == expected


# ---------------------------------------------------------------------------
# _generate_operation_id — deterministic fallback
# ---------------------------------------------------------------------------


def test_generate_operation_id_basic_and_empty() -> None:
    """Fallback names remain stable for ordinary and root paths."""
    assert _parser()._generate_operation_id("GET", "/users/{id}") == "get_users__id_"
    # Path with only slashes becomes empty parts list
    assert _parser()._generate_operation_id("POST", "/").startswith("post")


# ---------------------------------------------------------------------------
# _sanitize_identifier — normalization and malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("getHealth", "get_health"),
        ("searchDashboards", "search_dashboards"),
        ("GetHealth", "get_health"),
        ("ListDashboards", "list_dashboards"),
        ("getHTTPStatus", "get_http_status"),
        ("get_current_weather", "get_current_weather"),
        ("list_pets", "list_pets"),
        ("123abc", "fn_123abc"),
    ],
)
def test_identifier_normalization_preserved(raw: str, safe: str) -> None:
    """Historical camelCase, acronym, snake_case and numeric-prefix names remain usable."""
    assert _parser()._sanitize_identifier(raw) == safe
