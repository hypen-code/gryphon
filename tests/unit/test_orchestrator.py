"""V2 compilation, invalidation, credential-free configuration and containment tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gryphon.compiler.catalog import load_manifest
from gryphon.compiler.orchestrator import CompileResult, Orchestrator, _to_module_name
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError
from gryphon.models import ServerSpec, SwaggerSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compiler(tmp_path: Path, source: SwaggerSource | None = None) -> Orchestrator:
    """Create a compiler with fully temporary configuration/output and no environment file."""
    config_file = tmp_path / "swaggers.yaml"
    if source:
        config_file.write_text(yaml.safe_dump({"servers": [source.model_dump()]}))
    return Orchestrator(
        GryphonConfig.model_construct(
            compiled_output_dir=str(tmp_path / "compiled"),
            swagger_config_file=str(config_file),
            cache_db_path=str(tmp_path / "cache.db"),
        )
    )


# ---------------------------------------------------------------------------
# _to_module_name()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("weather", "weather"),
        ("Open-Meteo Weather API", "open_meteo_weather_api"),
        ("MyAPI", "myapi"),
        ("foo--bar", "foo_bar"),
        ("1api", "m_1api"),
    ],
)
def test_module_name_normalization(name: str, expected: str) -> None:
    """Valid historical display names retain their canonical module identity."""
    assert _to_module_name(name) == expected


@pytest.mark.parametrize("name", ["../escape", "/absolute", "bad\\name", "bad\nname", "...", "class"])
def test_malformed_module_names_rejected(name: str) -> None:
    """Malformed names are not sanitized into potentially colliding paths."""
    with pytest.raises(CompileError):
        _to_module_name(name)


# ---------------------------------------------------------------------------
# CompileResult
# ---------------------------------------------------------------------------


def test_compile_result_defaults() -> None:
    """An empty result has no successes, failures, skipped sources or artifacts."""
    result = CompileResult()
    assert not result.compiled and not result.skipped and not result.failed and result.total_endpoints == 0


# ---------------------------------------------------------------------------
# load_swagger_sources()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", [None, "", "servers: []"])
def test_empty_configuration_returns_no_sources(tmp_path: Path, content: str | None) -> None:
    """Missing and empty source configuration remain supported."""
    compiler = _compiler(tmp_path)
    if content is not None:
        (tmp_path / "swaggers.yaml").write_text(content)
    assert compiler.load_swagger_sources() == []


def test_invalid_entries_and_collisions_fail_closed(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """Partially valid credential-bearing config is never logged or silently accepted."""
    compiler = _compiler(tmp_path)
    valid = weather_swagger_source.model_dump()
    for entries in [
        [valid, {"bad_key": "no_name"}],
        [valid, {**valid, "name": "Weather"}],
    ]:  # missing fields or colliding names
        (tmp_path / "swaggers.yaml").write_text(yaml.safe_dump({"servers": entries}))
        # Invalid entries are rejected before any output is written.
        with pytest.raises(CompileError):
            compiler.load_swagger_sources()


# ---------------------------------------------------------------------------
# _is_up_to_date()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", [None, "{not json}", '{"swagger_hash":"same"}'])
def test_incomplete_manifest_never_skips(tmp_path: Path, content: str | None) -> None:
    """Matching raw source hashes alone cannot mark legacy manifests compatible."""
    compiler = _compiler(tmp_path)
    directory = tmp_path / "compiled" / "weather"
    directory.mkdir(parents=True)
    path = directory / "manifest.json"
    if content:
        path.write_text(content)
    assert not compiler._is_up_to_date(path, "same")


# ---------------------------------------------------------------------------
# _write_functions() / _write_manifest()
# ---------------------------------------------------------------------------


def test_write_complete_manifest_and_literal_safe_init(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Compiler-produced metadata is sufficient for registry use without SDK reads."""
    compiler = _compiler(tmp_path)
    directory = tmp_path / "weather"
    directory.mkdir()
    compiler._write_functions(directory, sample_server_spec, "result = 1\n")
    compiler._write_manifest(directory, sample_server_spec)
    manifest = load_manifest(directory / "manifest.json")
    assert manifest.format_version == 2
    assert manifest.endpoints[0].parameters == sample_server_spec.endpoints[0].parameters
    assert manifest.endpoints[0].base_url == sample_server_spec.base_url
    assert (directory / "functions.py").read_text() == "result = 1\n"


# ---------------------------------------------------------------------------
# _lint_all_generated_code()
# ---------------------------------------------------------------------------


def test_lint_no_generated_files_is_noop(tmp_path: Path) -> None:
    """An empty compile directory does not require a linter installation."""
    # Should not raise even with no functions.py files
    _compiler(tmp_path)._lint_all_generated_code()


@pytest.mark.parametrize("missing", [False, True])
def test_lint_unavailable_or_warning_is_diagnostic(tmp_path: Path, missing: bool) -> None:
    """SDK lint does not import source, execute it, or expose its content in warnings."""
    compiler = _compiler(tmp_path)
    directory = tmp_path / "compiled" / "weather"
    directory.mkdir(parents=True)
    (directory / "functions.py").write_text("result = 1\n")
    with patch(
        "subprocess.run", side_effect=FileNotFoundError() if missing else None, return_value=MagicMock(returncode=1)
    ):
        compiler._lint_all_generated_code()  # must not raise
        compiler._lint_all_generated_code()  # logs warning, does not raise


# ---------------------------------------------------------------------------
# compile_all() — with real fixture YAMLs
# ---------------------------------------------------------------------------


async def test_compile_no_sources_returns_empty(tmp_path: Path) -> None:
    """No sources is not a compile failure."""
    assert not (await _compiler(tmp_path).compile_all()).failed


async def test_compile_dry_run_has_no_output_side_effects(
    tmp_path: Path, weather_swagger_source: SwaggerSource
) -> None:
    """Dry-run parses and validates everything without creating even the output root."""
    result = await _compiler(tmp_path, weather_swagger_source).compile_all(dry_run=True)
    assert result.total_endpoints > 0
    # No files written in dry-run mode
    assert not (tmp_path / "compiled").exists()


async def test_compile_unchanged_source_skips(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """Only complete identical v2 output can skip recompilation."""
    compiler = _compiler(tmp_path, weather_swagger_source)
    assert (await compiler.compile_all()).compiled == ["weather"]
    # Second run — manifest exists with same hash
    assert (await compiler.compile_all()).skipped == ["weather"]


async def test_compile_sanitized_server_directory(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """Human-readable names map to one canonical catalog identity."""
    weather_swagger_source.name = "My Weather API"
    result = await _compiler(tmp_path, weather_swagger_source).compile_all()
    assert result.compiled == ["My Weather API"]
    # Directory must be the sanitized module name, not the raw server name
    assert (tmp_path / "compiled" / "my_weather_api" / "manifest.json").is_file()


async def test_compile_fetch_failure_recorded(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """A missing configured document records a failed source without leaking config."""
    weather_swagger_source.swagger_url = str(tmp_path / "missing")
    assert (await _compiler(tmp_path, weather_swagger_source).compile_all()).failed == ["weather"]


# ---------------------------------------------------------------------------
# _fetch_skills_content()
# ---------------------------------------------------------------------------


async def test_skills_local_and_remote_bounded_fetch(tmp_path: Path) -> None:
    """Skills use the same safe document transport and a bounded local read."""
    path = tmp_path / "skills.md"
    path.write_text("Instructions")
    assert await Orchestrator._fetch_skills_content(str(path), "weather") == "Instructions"
    assert await Orchestrator._fetch_skills_content(str(path), "weather", 1) is None
    client = MagicMock()
    client.request = AsyncMock(return_value=MagicMock(content=b"Instructions"))
    client.close = AsyncMock()
    with patch("gryphon.compiler.documents.NetworkClient", return_value=client):
        assert await Orchestrator._fetch_skills_content("https://example.com/skills", "weather") == "Instructions"
    client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# _write_skills()
# ---------------------------------------------------------------------------


def test_write_skills_preserves_on_fetch_failure(tmp_path: Path) -> None:
    """Transient optional documentation failures retain existing useful documentation."""
    Orchestrator._write_skills(tmp_path, "Instructions", "weather")
    Orchestrator._write_skills(tmp_path, None, "weather")
    assert (tmp_path / "skills.md").read_text() == "Instructions"


# ---------------------------------------------------------------------------
# _find_latest_server_dir — contained manifests only
# ---------------------------------------------------------------------------


def test_latest_server_directory_absent_and_present(tmp_path: Path) -> None:
    """Availability follows contained manifest files rather than arbitrary directories."""
    compiler = _compiler(tmp_path)
    # compiled dir does not exist
    assert compiler._find_latest_server_dir() is None
    directory = tmp_path / "compiled" / "weather"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{}")
    assert compiler._find_latest_server_dir() == directory


# ---------------------------------------------------------------------------
# _resolve_gryphon_command — CLI discovery only
# ---------------------------------------------------------------------------


def test_resolve_cli_fallback_and_local_venv(tmp_path: Path) -> None:
    """Client hints discover the command path without running project code."""
    assert Orchestrator._resolve_gryphon_command(tmp_path).startswith(str(Path(sys.executable).parent))
    binary = tmp_path / ".venv" / "bin" / "gryphon"
    binary.parent.mkdir(parents=True)
    binary.write_text("unused")
    # compiled dir is nested under tmp_path
    assert Orchestrator._resolve_gryphon_command(tmp_path / "compiled") == str(binary)


# ---------------------------------------------------------------------------
# _auth_env_hints — placeholders only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "static", "value": "private-static-value"},
        {"type": "jwt", "token": "private-jwt-value"},
        {"type": "basic", "username": "private-user", "password": "private-password"},
        {
            "type": "oauth2",
            "token_url": "https://auth.example/token",
            "client_id": "id",
            "client_secret": "private-secret",
        },
        {
            "type": "keycloak",
            "base_url": "https://auth.example",
            "realm": "realm",
            "client_id": "id",
            "client_secret": "private-secret",
        },
        {
            "type": "session",
            "login_url": "https://auth.example/login",
            "username": "private-user",
            "password": "private-password",
        },
    ],
)
def test_auth_hints_never_serialize_values(auth: dict[str, Any]) -> None:
    """Every auth mode emits only a fixed placeholder, including literal basic credentials."""
    source = SwaggerSource.model_validate({"name": "svc", "swagger_url": "unused", "auth": auth})
    assert Orchestrator._auth_env_hints("svc", source.auth) == {"GRYPHON_SVC_AUTH": "${GRYPHON_SVC_AUTH}"}


# ---------------------------------------------------------------------------
# _generate_mcp_json — credential-free client hints
# ---------------------------------------------------------------------------


async def test_mcp_json_headers_and_auth_are_placeholders(
    tmp_path: Path, weather_swagger_source: SwaggerSource
) -> None:
    """Printed client configuration does not leak configured static/header values."""
    source = SwaggerSource.model_validate(
        {
            **weather_swagger_source.model_dump(),
            "auth": {"type": "static", "value": "private-credential"},
            "extra_headers": {"X-Key": "private-header"},
        }
    )
    result = await _compiler(tmp_path, source).compile_all()
    assert result.mcp_json is not None
    assert "private-" not in result.mcp_json
    env = json.loads(result.mcp_json)["mcpServers"]["gryphon"]["env"]
    assert env["GRYPHON_ENABLE_ADDITIONAL_TOOLS"] == "false"
    assert env["GRYPHON_WEATHER_EXTRA_HEADERS"] == "${GRYPHON_WEATHER_EXTRA_HEADERS}"


# ---------------------------------------------------------------------------
# base_url propagation from spec without source mutation
# ---------------------------------------------------------------------------


async def test_compile_does_not_mutate_source_config(tmp_path: Path, weather_swagger_source: SwaggerSource) -> None:
    """Resolved compile destinations are stored in manifests, never backfilled into source policy."""
    # no base_url — falls back to spec
    weather_swagger_source.base_url = ""
    compiler = _compiler(tmp_path, weather_swagger_source)
    _, spec = await compiler._compile_source(weather_swagger_source, dry_run=True)
    assert spec.base_url  # non-empty
    assert weather_swagger_source.base_url == ""


@pytest.mark.parametrize("env_file", [None, "operator settings.env"])
async def test_mcp_json_paths_and_env_file_are_independent_of_client_cwd(
    tmp_path: Path, weather_swagger_source: SwaggerSource, monkeypatch: pytest.MonkeyPatch, env_file: str | None
) -> None:
    """A launch entry pins stores and the selected dotenv path without copying credential values."""
    monkeypatch.chdir(tmp_path)
    source = SwaggerSource.model_validate(
        {
            **weather_swagger_source.model_dump(),
            "auth": {"type": "static", "value": "private-auth"},
            "extra_headers": {"X-Key": "private-header"},
        }
    )
    config = _compiler(tmp_path, source)._config
    compiler = Orchestrator(config, env_file=env_file)
    result = await compiler.compile_all()
    assert result.mcp_json is not None and "private-" not in result.mcp_json
    entry = json.loads(result.mcp_json)["mcpServers"]["gryphon"]
    assert entry["env"]["GRYPHON_COMPILE_ON_STARTUP"] == "false"
    for field in ("compiled_output_dir", "swagger_config_file", "cache_db_path", "run_db_path", "artifact_dir"):
        assert entry["env"][f"GRYPHON_{field.upper()}"] == str(Path(getattr(config, field)).resolve())
    assert entry["args"] == (["serve", "--env-file", str(tmp_path / env_file)] if env_file else ["serve"])
    if env_file:
        assert not (tmp_path / env_file).exists()
        assert not any(key.startswith("GRYPHON_WEATHER_") for key in entry["env"])
    assert (await compiler.compile_all()).mcp_json == result.mcp_json
