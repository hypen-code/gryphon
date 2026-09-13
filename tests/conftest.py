"""Shared pytest fixtures for Gryphon test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gryphon.config import GryphonConfig
from gryphon.models import EndpointSpec, ParamSchema, ResponseField, ServerSpec, SwaggerSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def gryphon_config(tmp_path: Path) -> GryphonConfig:
    """Return a test GryphonConfig pointing at temp directories."""
    options: dict[str, Any] = {"_env_file": None}
    return GryphonConfig(
        **options,
        compiled_output_dir=str(tmp_path / "compiled"),
        cache_db_path=str(tmp_path / "data" / "cache.db"),
        run_db_path=str(tmp_path / "data" / "runs.db"),
        artifact_dir=str(tmp_path / "data" / "artifacts"),
        compile_on_startup=False,
        ucp_agent_profile=None,
        cache_enabled=True,
        cache_ttl_seconds=3600,
        execution_timeout_seconds=10,
        log_level="DEBUG",
        debug=True,
    )


@pytest.fixture
def weather_swagger_source() -> SwaggerSource:
    """Weather API swagger source configuration."""
    return SwaggerSource(
        name="weather",
        swagger_url=str(FIXTURES_DIR / "weather_api.yaml"),
        base_url="https://api.weather.example.com/v1",
        auth_header="",
        is_read_only=True,
    )


@pytest.fixture
def hotel_swagger_source() -> SwaggerSource:
    """Hotel booking API swagger source (read-write)."""
    return SwaggerSource(
        name="hotel",
        swagger_url=str(FIXTURES_DIR / "hotel_api.yaml"),
        base_url="https://api.hotel.example.com/v2",
        auth_header="Authorization: Bearer test-token",
        is_read_only=False,
    )


@pytest.fixture
def petstore_swagger_source() -> SwaggerSource:
    """Standard petstore swagger source."""
    return SwaggerSource(
        name="petstore",
        swagger_url=str(FIXTURES_DIR / "petstore.yaml"),
        base_url="https://petstore.example.com/v1",
        auth_header="",
        is_read_only=False,
    )


@pytest.fixture
def sample_endpoint() -> EndpointSpec:
    """A simple GET endpoint spec for unit tests."""
    return EndpointSpec(
        path="/weather/current",
        method="GET",
        operation_id="get_current_weather",
        summary="Get current weather for a location",
        parameters=[
            ParamSchema(
                name="city",
                location="query",
                param_type="string",
                required=True,
                description="City name",
            ),
            ParamSchema(
                name="units",
                location="query",
                param_type="string",
                required=False,
                description="Temperature units",
                default="metric",
                enum=["metric", "imperial", "kelvin"],
            ),
        ],
        response_schema=[
            ResponseField(name="temperature", field_type="number"),
            ResponseField(name="humidity", field_type="integer"),
            ResponseField(name="condition", field_type="string"),
        ],
        tags=["weather"],
    )


@pytest.fixture
def sample_server_spec(sample_endpoint: EndpointSpec) -> ServerSpec:
    """A minimal ServerSpec for codegen unit tests."""
    return ServerSpec(
        name="weather",
        description="Weather forecast and conditions API",
        base_url="https://api.weather.example.com/v1",
        is_read_only=True,
        endpoints=[sample_endpoint],
        swagger_hash="abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
    )
