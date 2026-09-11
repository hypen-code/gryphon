"""Integration test for the full compile pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from gryphon.compiler.orchestrator import Orchestrator

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


async def test_compile_weather_api(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Full compile pipeline produces valid output for weather fixture."""
    from pathlib import Path  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    fixtures_dir = Path(__file__).parent.parent / "fixtures"

    # Create a swaggers.yaml config
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "swaggers.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "servers": [
                    {
                        "name": "weather",
                        "swagger_url": str(fixtures_dir / "weather_api.yaml"),
                        "base_url": "https://api.weather.example.com/v1",
                        "is_read_only": True,
                    }
                ]
            }
        )
    )

    gryphon_config.swagger_config_file = str(config_file)

    orchestrator = Orchestrator(gryphon_config)
    result = await orchestrator.compile_all()

    assert "weather" in result.compiled
    assert result.total_endpoints > 0
    assert not result.failed


async def test_compile_produces_functions_py(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Compiled output directory contains functions.py for each server."""
    import yaml  # noqa: PLC0415

    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "swaggers.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "servers": [
                    {
                        "name": "weather",
                        "swagger_url": str(fixtures_dir / "weather_api.yaml"),
                        "base_url": "https://api.weather.example.com/v1",
                    }
                ]
            }
        )
    )
    gryphon_config.swagger_config_file = str(config_file)

    orchestrator = Orchestrator(gryphon_config)
    await orchestrator.compile_all()

    functions_file = Path(gryphon_config.compiled_output_dir) / "weather" / "functions.py"
    assert functions_file.exists()
    content = functions_file.read_text()
    assert "def get_current_weather" in content


async def test_compile_produces_manifest_json(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Compiled output contains valid manifest.json."""
    import yaml  # noqa: PLC0415

    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "swaggers.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "servers": [
                    {
                        "name": "weather",
                        "swagger_url": str(fixtures_dir / "weather_api.yaml"),
                        "base_url": "https://api.weather.example.com/v1",
                    }
                ]
            }
        )
    )
    gryphon_config.swagger_config_file = str(config_file)

    orchestrator = Orchestrator(gryphon_config)
    await orchestrator.compile_all()

    manifest_file = Path(gryphon_config.compiled_output_dir) / "weather" / "manifest.json"
    assert manifest_file.exists()
    manifest = json.loads(manifest_file.read_text())

    assert manifest["server_name"] == "weather"
    assert "swagger_hash" in manifest
    assert "endpoints" in manifest
    assert len(manifest["endpoints"]) > 0


async def test_compile_skips_unchanged_server(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Second compile run skips server if swagger hash matches."""
    import yaml  # noqa: PLC0415

    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "swaggers.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "servers": [
                    {
                        "name": "weather",
                        "swagger_url": str(fixtures_dir / "weather_api.yaml"),
                        "base_url": "https://api.weather.example.com/v1",
                    }
                ]
            }
        )
    )
    gryphon_config.swagger_config_file = str(config_file)

    orchestrator = Orchestrator(gryphon_config)
    result1 = await orchestrator.compile_all()
    assert "weather" in result1.compiled

    result2 = await orchestrator.compile_all()
    assert "weather" in result2.skipped
    assert "weather" not in result2.compiled


async def test_dry_run_does_not_write_files(tmp_path: Path, gryphon_config: GryphonConfig) -> None:
    """Dry run parses but does not write any output files."""
    import yaml  # noqa: PLC0415

    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "swaggers.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "servers": [
                    {
                        "name": "weather",
                        "swagger_url": str(fixtures_dir / "weather_api.yaml"),
                        "base_url": "https://api.weather.example.com/v1",
                    }
                ]
            }
        )
    )
    gryphon_config.swagger_config_file = str(config_file)

    orchestrator = Orchestrator(gryphon_config)
    result = await orchestrator.compile_all(dry_run=True)

    assert result.total_endpoints > 0
    assert not (Path(gryphon_config.compiled_output_dir) / "weather" / "functions.py").exists()


async def test_compile_real_open_meteo_example_native_contract(gryphon_config: GryphonConfig) -> None:
    """The shipped public weather example compiles bounds and output schemas without network access."""
    from gryphon.models import SwaggerSource
    from gryphon.runtime.registry import Registry
    from gryphon.security.encoding import validate_arguments
    from gryphon.security.schema import check_schema

    example = Path(__file__).parents[2] / "examples" / "weather.yaml"
    source = SwaggerSource(name="weather", swagger_url=str(example), is_read_only=True)
    compiler = Orchestrator(gryphon_config)
    count, _ = await compiler._compile_source(source, False)
    assert count == 1
    registry = Registry(gryphon_config.compiled_output_dir)
    registry.load()
    endpoint = registry.get_endpoint("weather", "get_forecast")
    assert endpoint.input_schema["properties"]["latitude"] == {"type": "number", "minimum": -90, "maximum": 90}
    assert endpoint.input_schema["properties"]["longitude"] == {"type": "number", "minimum": -180, "maximum": 180}
    assert endpoint.output_schema["properties"]["current"]["properties"]["temperature_2m"] == {"type": "number"}
    arguments = {"latitude": 51.5, "longitude": -0.12, "current": "temperature_2m"}
    assert validate_arguments(endpoint, arguments) == arguments
    check_schema(endpoint.output_schema)
