"""V2 compiler catalog security and regeneration regression coverage."""

from __future__ import annotations

import ast
import json
from typing import TYPE_CHECKING, Any

import pytest

from gryphon.compiler.catalog import load_manifest
from gryphon.compiler.codegen import CodeGenerator
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.compiler.top_level_codegen import TopLevelFunctionGenerator
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError
from gryphon.models import ParamSchema, ResponseField, ServerSpec, SwaggerSource

if TYPE_CHECKING:
    from pathlib import Path


def _compiler(tmp_path: Path) -> Orchestrator:
    """Construct a fully isolated compiler for direct source regeneration tests."""
    return Orchestrator(GryphonConfig.model_construct(compiled_output_dir=str(tmp_path / "compiled")))


@pytest.mark.parametrize(("field", "value"), [("base_url", "https://other.example/v2"), ("is_read_only", False)])
async def test_source_policy_changes_regenerate(
    tmp_path: Path,
    weather_swagger_source: SwaggerSource,
    field: str,
    value: Any,
) -> None:
    """Unchanged swagger bytes do not mask changes to destination or source policy."""
    compiler = _compiler(tmp_path)
    await compiler._compile_source(weather_swagger_source, False)
    updated = weather_swagger_source.model_copy(update={field: value})
    count, _ = await compiler._compile_source(updated, False)
    assert count > 0
    manifest = load_manifest(tmp_path / "compiled" / "weather" / "manifest.json")
    assert getattr(manifest, field) == value


@pytest.mark.parametrize("artifact", ["functions.py", "manifest.json"])
async def test_tampered_artifacts_regenerate(
    tmp_path: Path,
    weather_swagger_source: SwaggerSource,
    artifact: str,
) -> None:
    """Source integrity and full manifest compatibility are checked before skips."""
    compiler = _compiler(tmp_path)
    await compiler._compile_source(weather_swagger_source, False)
    path = tmp_path / "compiled" / "weather" / artifact
    path.write_text("{}")
    assert (await compiler._compile_source(weather_swagger_source, False))[0] > 0


async def test_removed_top_level_selection_tombstones_stale_tools(
    tmp_path: Path,
    weather_swagger_source: SwaggerSource,
) -> None:
    """Removing promotion configuration cannot leave an active SDK wrapper registry."""
    compiler = _compiler(tmp_path)
    weather_swagger_source.top_level_functions = ["get_current_weather"]
    await compiler._compile_source(weather_swagger_source, False)
    weather_swagger_source.top_level_functions = []
    await compiler._compile_source(weather_swagger_source, False)
    code = (tmp_path / "compiled" / "weather" / "top_level_functions.py").read_text()
    assert "_TOP_LEVEL_TOOLS: list[dict[str, Any]] = []" in code
    assert "async def" not in code


@pytest.mark.parametrize(
    "target", ["functions.py", "manifest.json", "skills.md", "top_level_functions.py", "__init__.py"]
)
async def test_artifact_symlinks_never_overwritten(
    tmp_path: Path,
    weather_swagger_source: SwaggerSource,
    target: str,
) -> None:
    """All artifact writes and refresh paths reject final-component symlinks."""
    compiler = _compiler(tmp_path)
    directory = tmp_path / "compiled" / "weather"
    directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (directory / target).symlink_to(outside)
    if target == "skills.md":
        weather_swagger_source.skills_url = str(outside)
    with pytest.raises(CompileError, match="Symlink"):
        await compiler._compile_source(weather_swagger_source, False)
    assert outside.read_text() == "private"


async def test_output_directory_symlink_rejected(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """A symlinked server directory is not treated as a harmless normalized name."""
    root = tmp_path / "compiled"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "weather").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CompileError, match="Symlink"):
        await _compiler(tmp_path)._compile_source(weather_swagger_source, False)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("payload", ['"""\nraise RuntimeError("injected")\n#', "x\\u000a\nimport os\n", "quote'\r\n"])
def test_untrusted_documentation_cannot_inject_source(sample_server_spec: ServerSpec, payload: str) -> None:
    """Descriptions, defaults and field names remain literals in both SDK templates."""
    spec = sample_server_spec.model_copy(deep=True)
    spec.description = payload
    endpoint = spec.endpoints[0]
    endpoint.summary = payload
    endpoint.description = payload
    endpoint.parameters[1].default = payload
    endpoint.parameters[1].enum = [payload]
    endpoint.response_schema = [ResponseField(name=payload, field_type="string")]
    sdk = CodeGenerator().generate(spec)
    wrappers = TopLevelFunctionGenerator().generate(spec, "weather", [endpoint.operation_id])
    for code in [sdk, wrappers or ""]:
        tree = ast.parse(code)
        assert not any(isinstance(node, ast.Raise) for node in ast.walk(tree))
    assert "verify=False" not in sdk
    assert "verify=True" in sdk


def test_wire_name_literals_and_aliases_are_distinct(sample_server_spec: ServerSpec) -> None:
    """Query key spelling is retained even when SDK spelling changes."""
    endpoint = sample_server_spec.endpoints[0]
    endpoint.parameters = [ParamSchema(name="userName", location="query", param_type="string", required=True)]
    code = CodeGenerator().generate(sample_server_spec)
    assert "user_name: str" in code
    assert "'userName': user_name" in code


@pytest.mark.parametrize("name", ['bad"name', "bad\nname", "bad;name"])
def test_malformed_operation_names_rejected(name: str) -> None:
    """Operation labels are normalized only after structural validation."""
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused"))
    with pytest.raises(CompileError, match="Malformed"):
        parser._sanitize_identifier(name)


def test_colliding_operation_names_rejected() -> None:
    """Distinct source IDs cannot overwrite the same normalized Python function."""
    parser = SwaggerParser(SwaggerSource(name="test", swagger_url="unused", base_url="https://example.com"))
    parser._raw_doc = {"paths": {"/x": {"get": {"operationId": "getUser"}}, "/y": {"get": {"operationId": "get_user"}}}}
    with pytest.raises(CompileError, match="collide"):
        parser._parse_paths()


def test_sdk_generation_has_no_auth_values(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Auth hints never copy literal credentials, even when SDK configuration is printed."""
    auth = SwaggerSource.model_validate(
        {
            "name": "weather",
            "swagger_url": "unused",
            "auth": {"type": "basic", "username": "${USER_NAME}", "password": "${PASSWORD}"},
        }
    ).auth
    hints = Orchestrator._auth_env_hints("weather", auth)
    assert hints == {"USER_NAME": "${USER_NAME}", "PASSWORD": "${PASSWORD}"}
    directory = tmp_path / "weather"
    directory.mkdir()
    compiler = _compiler(tmp_path)
    compiler._write_manifest(directory, sample_server_spec)
    raw = json.loads((directory / "manifest.json").read_text())
    assert "auth" not in raw
