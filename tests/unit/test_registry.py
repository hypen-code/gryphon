"""Regression tests for complete, immutable v2 registry metadata and safe inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gryphon.compiler.catalog import input_schema
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError, FunctionNotFoundError, ServerNotFoundError
from gryphon.runtime.registry import Registry

if TYPE_CHECKING:
    from gryphon.models import ServerSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path, spec: ServerSpec) -> Registry:
    """Write real compiler-produced v2 metadata rather than summary-only fake manifests."""
    directory = tmp_path / spec.name
    directory.mkdir(exist_ok=True)
    compiler = Orchestrator(GryphonConfig.model_construct(compiled_output_dir=str(tmp_path)))
    compiler._write_manifest(directory, spec)
    registry = Registry(str(tmp_path))
    registry.load()
    return registry


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [True, False])
def test_load_absent_or_empty_directory_is_empty(tmp_path: Path, missing: bool) -> None:
    """An uncompiled installation can start with an empty catalog."""
    registry = Registry(str(tmp_path / "missing" if missing else tmp_path))
    registry.load()
    assert registry.list_servers() == []


def test_load_multiple_manifests(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Distinct registered names produce stable server ordering."""
    _registry(tmp_path, sample_server_spec)
    other = sample_server_spec.model_copy(update={"name": "hotel"})
    registry = _registry(tmp_path, other)
    assert [server.name for server in registry.list_servers()] == ["hotel", "weather"]


def test_load_clears_previous_state(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Reload atomically replaces previous server and search state."""
    registry = _registry(tmp_path, sample_server_spec)
    # Replace manifest and reload
    sample_server_spec.endpoints = []
    Orchestrator(GryphonConfig.model_construct())._write_manifest(tmp_path / "weather", sample_server_spec)
    registry.load()
    assert registry.search_functions("") == []


@pytest.mark.parametrize("content", ["not json", "{}", '{"format_version":1}', '{"format_version":2}'])
def test_load_incompatible_manifest_requires_compile_again(tmp_path: Path, content: str) -> None:
    """Corruption and legacy catalogs must never be silently exposed as v2 tools."""
    directory = tmp_path / "weather"
    directory.mkdir()
    (directory / "manifest.json").write_text(content)
    with pytest.raises(CompileError, match="compile again"):
        Registry(str(tmp_path)).load()


# ---------------------------------------------------------------------------
# list_servers()
# ---------------------------------------------------------------------------


def test_list_servers_returns_real_names_and_summaries(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Discovery describes the compiled endpoint without source imports."""
    server = _registry(tmp_path, sample_server_spec).list_servers()[0]
    assert server.function_summaries == {"get_current_weather": sample_server_spec.endpoints[0].summary}


# ---------------------------------------------------------------------------
# get_function()
# ---------------------------------------------------------------------------


def test_get_function_returns_exact_metadata(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Requiredness, enum, defaults, descriptions and response types survive compilation."""
    endpoint = sample_server_spec.endpoints[0]
    endpoint.parameters[0].location = "header"
    info = _registry(tmp_path, sample_server_spec).get_function("weather", endpoint.operation_id)
    assert info.parameters == endpoint.parameters
    assert info.response_fields == endpoint.response_schema


def test_get_function_never_reads_generated_source(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Inspect uses call_tool examples and has no source-file or credential read side effects."""
    registry = _registry(tmp_path, sample_server_spec)
    with patch.object(Path, "read_text", side_effect=AssertionError("source read")):
        info = registry.get_function("weather", "get_current_weather")
    assert info.source_code == "result = await call_tool(\"weather.get_current_weather\", {'city': ''})"


@pytest.mark.parametrize("method", ["get_function", "get_manifest", "get_swagger_hash", "has_skills", "skills_path"])
def test_unknown_server_rejected_before_path_access(tmp_path: Path, method: str) -> None:
    """Path traversal strings never become filesystem paths for registry lookup."""
    registry = Registry(str(tmp_path))
    args = ["../outside", "fn"] if method == "get_function" else ["../outside"]
    with pytest.raises(ServerNotFoundError):
        getattr(registry, method)(*args)


def test_unknown_function_rejected(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """An SDK source accessor still requires a declared endpoint."""
    registry = _registry(tmp_path, sample_server_spec)
    with pytest.raises(FunctionNotFoundError):
        registry.get_function_source("weather", "../../secret")


# ---------------------------------------------------------------------------
# get_function_source() and _get_function_source()
# ---------------------------------------------------------------------------


def test_sdk_source_missing_is_explicit(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Missing SDK source is not an obstacle to broker use."""
    registry = _registry(tmp_path, sample_server_spec)
    # No functions.py exists; only the explicit SDK accessor reports it.
    assert "unavailable" in registry.get_function_source("weather", "get_current_weather")


def test_sdk_source_accessor_extracts_and_caches_only_target(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Source opt-in does not fall back to module-level credential configuration."""
    registry = _registry(tmp_path, sample_server_spec)
    # Write a functions.py
    path = tmp_path / "weather" / "functions.py"
    path.write_text("UNRELATED = 'not for inspection'\ndef get_current_weather():\n    return {}\n")
    # First call populates cache
    first = registry.get_function_source("weather", "get_current_weather")
    # Overwrite the file — cached result should still be returned
    path.write_text("def other():\n    return 1\n")
    assert registry.get_function_source("weather", "get_current_weather") == first
    assert "UNRELATED" not in first


# ---------------------------------------------------------------------------
# get_swagger_hash()
# ---------------------------------------------------------------------------


def test_get_swagger_hash_matches_input(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """The raw document hash remains available to SDK callers."""
    assert _registry(tmp_path, sample_server_spec).get_swagger_hash("weather") == sample_server_spec.swagger_hash


# ---------------------------------------------------------------------------
# _parse_parameters_summary
# ---------------------------------------------------------------------------


def test_inspection_ignores_malformed_legacy_summary(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Display summaries cannot alter authoritative v2 metadata."""
    registry = _registry(tmp_path, sample_server_spec)
    path = tmp_path / "weather" / "manifest.json"
    raw = json.loads(path.read_text())
    raw["endpoints"][0]["parameters_summary"] = "foo, bar(int"
    path.write_text(json.dumps(raw))
    registry.load()
    # Should not raise; real parameters remain authoritative.
    assert (
        registry.get_function("weather", "get_current_weather").parameters == sample_server_spec.endpoints[0].parameters
    )


# ---------------------------------------------------------------------------
# _parse_response_summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("summary", ["", "response data", "made-up,fields"])
def test_inspection_ignores_lossy_response_summary(
    tmp_path: Path, sample_server_spec: ServerSpec, summary: str
) -> None:
    """Real response types never come from comma-separated display strings."""
    registry = _registry(tmp_path, sample_server_spec)
    path = tmp_path / "weather" / "manifest.json"
    raw = json.loads(path.read_text())
    raw["endpoints"][0]["response_summary"] = summary
    path.write_text(json.dumps(raw))
    registry.load()
    assert (
        registry.get_function("weather", "get_current_weather").response_fields
        == sample_server_spec.endpoints[0].response_schema
    )


# ---------------------------------------------------------------------------
# _extract_function_snippet — fail-closed syntax handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["def broken(: pass", "TOKEN = 'private'\ndef other():\n    return 1\n"])
def test_sdk_extraction_never_returns_full_source(tmp_path: Path, source: str) -> None:
    """Unparseable or missing functions produce diagnostics rather than leaking the module."""
    # No full-source fallback on SyntaxError
    snippet = Registry(str(tmp_path))._extract_function_snippet(source, "get_weather")
    # Function not found in file never exposes the full source.
    assert source not in snippet
    assert "compile again" in snippet


# ---------------------------------------------------------------------------
# has_skills() / skills_path()
# ---------------------------------------------------------------------------


def test_skills_paths_contained_and_registered(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Optional skills can be loaded only for an existing catalog server."""
    registry = _registry(tmp_path, sample_server_spec)
    assert registry.skills_path("weather") is None
    path = tmp_path / "weather" / "skills.md"
    path.write_text("Instructions")
    assert registry.has_skills("weather")
    assert registry.skills_path("weather") == path


def test_skills_symlink_rejected(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Registered names do not permit skills symlinks escaping the compiled root."""
    registry = _registry(tmp_path, sample_server_spec)
    target = tmp_path / "private"
    target.write_text("private")
    (tmp_path / "weather" / "skills.md").symlink_to(target)
    with pytest.raises(CompileError, match="Symlink"):
        registry.skills_path("weather")


def test_manifest_copy_cannot_change_broker_authority(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Public metadata objects cannot mutate the loaded manifest snapshot."""
    registry = _registry(tmp_path, sample_server_spec)
    fingerprint = registry.fingerprint()
    registry.get_manifest("weather").endpoints.clear()
    registry.get_endpoint("weather", "get_current_weather").parameters[0].required = False
    assert registry.get_endpoint("weather", "get_current_weather").parameters[0].required
    assert registry.fingerprint() == fingerprint


def test_fingerprint_covers_full_manifest(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Changing requiredness without changing swagger_hash changes catalog identity."""
    registry = _registry(tmp_path, sample_server_spec)
    first = registry.fingerprint()
    registry.load()
    assert registry.fingerprint() == first
    path = tmp_path / "weather" / "manifest.json"
    manifest = registry.get_manifest("weather")
    manifest.endpoints[0].parameters[0].required = False
    manifest.endpoints[0].input_schema = input_schema(manifest.endpoints[0])
    path.write_text(manifest.model_dump_json())
    registry.load()
    assert registry.fingerprint() != first


def test_search_functions_ranked_deterministic_and_bounded(tmp_path: Path, sample_server_spec: ServerSpec) -> None:
    """Lexical matching uses weighted names and stable ties, with bounded result counts."""
    registry = _registry(tmp_path, sample_server_spec)
    expected: list[dict[str, Any]] = registry.search_functions("current weather")
    assert expected[0]["tool_id"] == "weather.get_current_weather"
    assert registry.search_functions("CURRENT weather", limit=1) == expected
    assert registry.search_functions("unmatched") == []
    assert registry.search_functions("", limit=0) == []
