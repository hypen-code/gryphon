"""Unit tests for the TopLevelFunctionGenerator and _normalize_function_name."""

from __future__ import annotations

import ast

from gryphon.compiler.top_level_codegen import TopLevelFunctionGenerator, _normalize_function_name
from gryphon.models import EndpointSpec, ParamSchema, ResponseField, ServerSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(*endpoints: EndpointSpec) -> ServerSpec:
    return ServerSpec(
        name="weather",
        description="Weather API",
        base_url="https://api.example.com/v1",
        is_read_only=True,
        endpoints=list(endpoints),
        swagger_hash="abc123" * 10,
    )


def _make_endpoint(
    operation_id: str = "get_current_weather",
    *,
    required_params: list[str] | None = None,
    optional_params: list[str] | None = None,
    with_body: bool = False,
    response_fields: list[str] | None = None,
) -> EndpointSpec:
    params: list[ParamSchema] = []
    for name in required_params or []:
        params.append(ParamSchema(name=name, location="query", param_type="string", required=True))
    for name in optional_params or []:
        params.append(ParamSchema(name=name, location="query", param_type="string", required=False))
    schema = [ResponseField(name=f, field_type="string") for f in (response_fields or [])]
    return EndpointSpec(
        path="/weather",
        method="GET",
        operation_id=operation_id,
        summary=f"Operation {operation_id}",
        parameters=params,
        request_body_schema={"type": "object"} if with_body else None,
        response_schema=schema,
    )


# ---------------------------------------------------------------------------
# _normalize_function_name
# ---------------------------------------------------------------------------


def test_normalize_camel_case_to_snake() -> None:
    assert _normalize_function_name("getForecast") == "get_forecast"


def test_normalize_pascal_case_to_snake() -> None:
    assert _normalize_function_name("GetAirQuality") == "get_air_quality"


def test_normalize_already_snake_case_unchanged() -> None:
    assert _normalize_function_name("get_forecast") == "get_forecast"


def test_normalize_acronym_sequence() -> None:
    assert _normalize_function_name("getHTTPStatus") == "get_http_status"


def test_normalize_single_word() -> None:
    assert _normalize_function_name("forecast") == "forecast"


# ---------------------------------------------------------------------------
# generate() — empty / no-match cases
# ---------------------------------------------------------------------------


def test_generate_returns_none_for_empty_name_list() -> None:
    gen = TopLevelFunctionGenerator()
    spec = _make_spec(_make_endpoint())
    assert gen.generate(spec, "weather", []) is None


def test_generate_returns_none_when_no_names_match() -> None:
    gen = TopLevelFunctionGenerator()
    spec = _make_spec(_make_endpoint("get_current_weather"))
    result = gen.generate(spec, "weather", ["nonexistent_function"])
    assert result is None


def test_generate_still_generates_when_some_names_unresolved(
    sample_server_spec: ServerSpec,
) -> None:
    gen = TopLevelFunctionGenerator()
    # Mix of valid and invalid names — should return code for the valid one
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather", "ghost_function"])
    assert code is not None
    assert "get_current_weather" in code


# ---------------------------------------------------------------------------
# generate() — output correctness
# ---------------------------------------------------------------------------


def test_generate_produces_valid_python(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    ast.parse(code)  # must not raise SyntaxError


def test_generate_contains_async_def(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "async def get_current_weather" in code


def test_generate_imports_from_server_module(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "from weather.functions import" in code


def test_generate_uses_asyncio_to_thread(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "asyncio.to_thread" in code


def test_generate_contains_top_level_tools_registry(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "_TOP_LEVEL_TOOLS" in code
    assert '"get_current_weather"' in code


def test_generate_accepts_camel_case_name(sample_server_spec: ServerSpec) -> None:
    """camelCase operationId in YAML should resolve to the compiled snake_case function."""
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["getCurrentWeather"])
    assert code is not None
    assert "get_current_weather" in code


def test_generate_includes_required_params_in_signature() -> None:
    gen = TopLevelFunctionGenerator()
    ep = _make_endpoint(required_params=["city", "country"])
    spec = _make_spec(ep)
    code = gen.generate(spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "city" in code
    assert "country" in code


def test_generate_includes_optional_params_with_none_default() -> None:
    gen = TopLevelFunctionGenerator()
    ep = _make_endpoint(optional_params=["units"])
    spec = _make_spec(ep)
    code = gen.generate(spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "units" in code


def test_generate_includes_json_body_param_when_present() -> None:
    gen = TopLevelFunctionGenerator()
    ep_post = EndpointSpec(
        path="/weather",
        method="POST",
        operation_id="create_forecast",
        summary="Create forecast",
        parameters=[],
        request_body_schema={"type": "object"},
        response_schema=[],
    )
    spec = _make_spec(ep_post)
    code = gen.generate(spec, "weather", ["create_forecast"])
    assert code is not None
    assert "json_body" in code


def test_generate_response_fields_in_docstring() -> None:
    gen = TopLevelFunctionGenerator()
    ep = _make_endpoint(response_fields=["temperature", "humidity"])
    spec = _make_spec(ep)
    code = gen.generate(spec, "weather", ["get_current_weather"])
    assert code is not None
    assert "temperature" in code


def test_generate_no_params_function_calls_sync_directly() -> None:
    """Functions with no parameters should still wrap with asyncio.to_thread correctly."""
    gen = TopLevelFunctionGenerator()
    ep = EndpointSpec(
        path="/ping",
        method="GET",
        operation_id="ping",
        summary="Ping",
        parameters=[],
        response_schema=[],
    )
    spec = _make_spec(ep)
    code = gen.generate(spec, "weather", ["ping"])
    assert code is not None
    assert "asyncio.to_thread(_ping_sync)" in code


def test_generate_adds_server_name_to_tool_registry(sample_server_spec: ServerSpec) -> None:
    gen = TopLevelFunctionGenerator()
    code = gen.generate(sample_server_spec, "weather", ["get_current_weather"])
    assert code is not None
    assert '"weather"' in code


def test_generate_long_signature_uses_multiline_format() -> None:
    """Functions with a long signature should use the multi-line def format."""
    gen = TopLevelFunctionGenerator()
    many_params = [f"param_{i}" for i in range(10)]
    ep = _make_endpoint(required_params=many_params)
    spec = _make_spec(ep)
    code = gen.generate(spec, "weather", ["get_current_weather"])
    assert code is not None
    ast.parse(code)


def test_generate_module_name_used_in_import() -> None:
    """The module_name argument (not spec.name) must appear in the import path."""
    gen = TopLevelFunctionGenerator()
    ep = _make_endpoint()
    spec = ServerSpec(
        name="Weather API",  # human-readable, may differ from module name
        description="desc",
        base_url="https://example.com",
        is_read_only=True,
        endpoints=[ep],
        swagger_hash="x" * 64,
    )
    code = gen.generate(spec, "weather_module_name", ["get_current_weather"])
    assert code is not None
    assert "from weather_module_name.functions import" in code
