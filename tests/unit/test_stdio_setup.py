"""Package-friendly environment-only stdio startup regressions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic import ValidationError
from pydantic_settings import SettingsError

from gryphon.__main__ import _build_parser, _cmd_run, _config_for, main
from gryphon.cli_setup import prepare_stdio_state
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError
from gryphon.utils.logging import setup_logging

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exclude operator configuration and redirect user state into temporary storage."""
    for name in os.environ:
        if name.startswith("GRYPHON_") or name == "XDG_STATE_HOME":
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(GryphonConfig.model_config, "env_file", str(tmp_path / ".env"))
    monkeypatch.setattr("gryphon.__main__._ENV_FILE", tmp_path / ".env")


def _stdio_config(env_file: str | None = None) -> GryphonConfig:
    """Load the same configuration boundary as the environment-only CLI."""
    return _config_for(argparse.Namespace(command="stdio", env_file=env_file))


def test_stdio_parser_accepts_explicit_env_file(tmp_path: Path) -> None:
    """Both standard positions support an explicitly chosen env file."""
    for args in (["stdio", "--env-file", str(tmp_path)], ["--env-file", str(tmp_path), "stdio"]):
        parsed = _build_parser().parse_args(args)
        assert parsed.command == "stdio" and parsed.env_file == str(tmp_path)


def test_stdio_defaults_ignore_cwd_dotenv_and_yaml(tmp_path: Path) -> None:
    """The computation-only launch neither imports ambient settings nor catalogs."""
    (tmp_path / ".env").write_text("GRYPHON_SANDBOX_MODE=docker\n", encoding="utf-8")
    config = _stdio_config()
    assert config.sandbox_mode == "restricted"
    assert config.swaggers == []
    assert Orchestrator(config).load_swagger_sources() == []
    assert Path(config.compiled_output_dir).is_relative_to(tmp_path / ".local/state/gryphon")
    assert not Path(config.compiled_output_dir).exists()


def test_stdio_explicit_dotenv_and_environment_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit env-file settings work, with launch environment retaining precedence."""
    env_file = tmp_path / "chosen.env"
    env_file.write_text("GRYPHON_SANDBOX_MODE=docker\nGRYPHON_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("GRYPHON_LOG_LEVEL", "WARNING")
    config = _stdio_config(str(env_file))
    assert config.sandbox_mode == "docker" and config.log_level == "WARNING"


def test_stdio_state_root_and_configuration_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source removal and source-policy changes cannot preserve a previous catalog."""
    monkeypatch.setenv("GRYPHON_STATE_DIR", str(tmp_path / "state"))
    empty = _stdio_config()
    source = {"name": "weather", "swagger_url": str(_FIXTURES / "weather_api.yaml"), "is_read_only": True}
    monkeypatch.setenv("GRYPHON_SWAGGERS", json.dumps([source]))
    first = _stdio_config()
    monkeypatch.setenv("GRYPHON_SWAGGERS", json.dumps([source], indent=2, sort_keys=True))
    assert _stdio_config().compiled_output_dir == first.compiled_output_dir
    source["is_read_only"] = False
    monkeypatch.setenv("GRYPHON_SWAGGERS", json.dumps([source]))
    changed = _stdio_config()
    for field in ("compiled_output_dir", "cache_db_path", "run_db_path", "artifact_dir"):
        paths = {getattr(config, field) for config in (empty, first, changed)}
        assert len(paths) == 3
        assert all(Path(path).is_relative_to(tmp_path / "state") for path in paths)


def test_stdio_xdg_state_home_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute XDG state home supplies the default Gryphon root."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert Path(_stdio_config().compiled_output_dir).is_relative_to(tmp_path / "xdg-state/gryphon")


def test_stdio_relative_state_root_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit state root cannot accidentally depend on the client's working directory."""
    monkeypatch.setenv("GRYPHON_STATE_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        _stdio_config()


@pytest.mark.parametrize("value", ["broken-json", "{}", "null", '[{"name":"weather"}]'])
def test_stdio_invalid_env_sources_fail_closed(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed source settings must not silently fall back to a YAML catalog."""
    monkeypatch.setenv("GRYPHON_SWAGGERS", value)
    with pytest.raises((ValidationError, SettingsError, ValueError)):
        _stdio_config()


def test_stdio_colliding_env_source_names_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment sources use the same normalization checks as YAML sources."""
    sources = [{"name": name, "swagger_url": "missing.yaml"} for name in ("Weather API", "weather-api")]
    monkeypatch.setenv("GRYPHON_SWAGGERS", json.dumps(sources))
    with pytest.raises(CompileError, match="collide"):
        Orchestrator(_stdio_config()).load_swagger_sources()


def test_stdio_env_sources_override_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit empty environment list suppresses even an explicitly configured YAML file."""
    yaml_file = tmp_path / "sources.yaml"
    yaml_file.write_text("not: [valid", encoding="utf-8")
    monkeypatch.setenv("GRYPHON_SWAGGER_CONFIG_FILE", str(yaml_file))
    monkeypatch.setenv("GRYPHON_SWAGGERS", "[]")
    assert Orchestrator(_stdio_config()).load_swagger_sources() == []


async def test_stdio_startup_compiles_once_without_client_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The convenient mode compiles even if the legacy startup flag is disabled."""
    monkeypatch.setenv("GRYPHON_COMPILE_ON_STARTUP", "false")
    monkeypatch.setenv(
        "GRYPHON_SWAGGERS", json.dumps([{"name": "weather", "swagger_url": str(_FIXTURES / "weather_api.yaml")}])
    )
    args = argparse.Namespace(command="stdio", env_file=None)
    setup_logging("INFO")
    with patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)) as serve:
        assert await _cmd_run(args) == 0
    serve.assert_awaited_once()
    assert capsys.readouterr().out == ""
    assert (Path(_config_for(args).compiled_output_dir) / "weather/manifest.json").is_file()


@pytest.mark.parametrize("explicit", [False, True])
def test_stdio_main_only_loads_explicit_dotenv(explicit: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential loading cannot discover an ambient .env during stdio startup."""
    env_file = tmp_path / "explicit.env"
    env_file.write_text("", encoding="utf-8")
    argv = ["gryphon", "stdio", *(["--env-file", str(env_file)] if explicit else [])]
    monkeypatch.setattr(sys, "argv", argv)
    with (
        patch("gryphon.__main__.load_dotenv") as dotenv,
        patch("gryphon.__main__._cmd_run", new=AsyncMock(return_value=0)),
        pytest.raises(SystemExit) as exited,
    ):
        main()
    assert exited.value.code == 0
    if explicit:
        dotenv.assert_called_once_with(str(env_file), override=False)
    else:
        dotenv.assert_not_called()


@pytest.mark.parametrize("with_sources", [False, True])
async def test_stdio_real_client_environment_only_computation_and_discovery(tmp_path: Path, with_sources: bool) -> None:
    """A real MCP process requires no checkout config, compilation command or Docker daemon."""
    sources = (
        [{"name": "weather", "swagger_url": str(_FIXTURES / "weather_api.yaml"), "is_read_only": True}]
        if with_sources
        else []
    )
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "GRYPHON_STATE_DIR": str(tmp_path / "state"),
        "GRYPHON_SWAGGERS": json.dumps(sources),
        "GRYPHON_COMPILE_ON_STARTUP": "false",
    }
    (tmp_path / ".env").write_text("GRYPHON_SANDBOX_MODE=docker\n", encoding="utf-8")
    transport = StdioTransport(
        command=sys.executable, args=["-m", "gryphon", "stdio"], env=environment, cwd=str(tmp_path), keep_alive=False
    )
    async with Client(transport, timeout=60, init_timeout=60) as client:
        assert len(await client.list_tools()) == 11
        servers = await client.call_tool("list_servers", {})
        assert len(servers.data["servers"]) == int(with_sources)
        result = await client.call_tool(
            "execute_code",
            {"code": "result = sum(inputs['values'])", "inputs": {"values": [2, 3, 5]}, "description": "offline sum"},
        )
        assert result.data["success"] and result.data["data"] == 10
        reused = await client.call_tool(
            "run_cached_code", {"cache_id": result.data["cache_id"], "params": {"values": [4, 6]}}
        )
        assert reused.data["success"] and reused.data["data"] == 10
    assert len(list((tmp_path / "state").glob("*/runs.db"))) == 1
    assert not (tmp_path / "compiled").exists() and not (tmp_path / "data").exists()


def test_stdio_state_creation_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only startup creates the user root and source namespace, with private permissions."""
    root = tmp_path / "state"
    monkeypatch.setenv("GRYPHON_STATE_DIR", str(root))
    config = _stdio_config()
    assert not root.exists()
    prepare_stdio_state(config)
    assert root.stat().st_mode & 0o777 == 0o700
    assert Path(config.run_db_path).parent.stat().st_mode & 0o777 == 0o700


def test_stdio_state_symlink_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user root cannot traverse a link into an unrelated writable directory."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("GRYPHON_STATE_DIR", str(link / "state"))
    with pytest.raises(CompileError, match="Symlink"):
        _stdio_config()
    assert list(target.iterdir()) == []


def test_stdio_state_unprepared_config_rejected(gryphon_config: GryphonConfig) -> None:
    """The state creation hook refuses a configuration not prepared for this mode."""
    with pytest.raises(ValueError, match="not been selected"):
        prepare_stdio_state(gryphon_config)


def test_stdio_relative_xdg_home_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The XDG specification rejects relative state homes rather than tying state to CWD."""
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    assert Path(_stdio_config().run_db_path).is_relative_to(tmp_path / ".local/state/gryphon")


def test_stdio_explicit_storage_overrides_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can deliberately select existing stores instead of source-scoped defaults."""
    for field in ("compiled_output_dir", "cache_db_path", "run_db_path", "artifact_dir"):
        monkeypatch.setenv(f"GRYPHON_{field.upper()}", str(tmp_path / field))
    config = _stdio_config()
    for field in ("compiled_output_dir", "cache_db_path", "run_db_path", "artifact_dir"):
        assert getattr(config, field) == str(tmp_path / field)


def test_stdio_explicit_yaml_sources_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The convenient launch can opt into existing operator YAML without discovering it."""
    yaml_file = tmp_path / "sources.yaml"
    yaml_file.write_text(
        json.dumps({"servers": [{"name": "weather", "swagger_url": "weather.yaml"}]}), encoding="utf-8"
    )
    monkeypatch.setenv("GRYPHON_SWAGGER_CONFIG_FILE", str(yaml_file))
    sources = Orchestrator(_stdio_config()).load_swagger_sources()
    assert len(sources) == 1 and sources[0].swagger_url == str(tmp_path / "weather.yaml")


def test_stdio_relative_sources_isolated_by_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Identical relative source text in different directories must not share persisted state."""
    monkeypatch.setenv("GRYPHON_SWAGGERS", '[{"name":"weather","swagger_url":"weather.yaml"}]')
    first = _stdio_config()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert _stdio_config().run_db_path != first.run_db_path


async def test_stdio_compile_failure_prevents_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid documents fail closed rather than serving an old or partial catalog."""
    monkeypatch.setenv("GRYPHON_SWAGGERS", '[{"name":"weather","swagger_url":"missing.yaml"}]')
    with patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)) as serve:
        assert await _cmd_run(argparse.Namespace(command="stdio", env_file=None)) == 1
    serve.assert_not_awaited()


def test_stdio_invalid_configuration_has_safe_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public CLI rejects malformed launch settings without printing their values."""
    monkeypatch.setenv("GRYPHON_SWAGGERS", "malformed-swagger-fixture")
    monkeypatch.setattr(sys, "argv", ["gryphon", "stdio"])
    with pytest.raises(SystemExit) as exited:
        main()
    captured = capsys.readouterr()
    assert exited.value.code == 1 and captured.out == ""
    assert "command_failed" in captured.err and "malformed-swagger-fixture" not in captured.err


def test_stdio_config_programmatic_yaml_sentinel_roundtrips(gryphon_config: GryphonConfig) -> None:
    """Adding JSON environment sources does not break validated settings roundtrips."""
    assert GryphonConfig.model_validate(gryphon_config.model_dump()).swaggers is None
