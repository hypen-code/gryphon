"""Unit tests for Gryphon's CLI entry point and composed commands."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

    from gryphon.config import GryphonConfig

import pytest

from gryphon.__main__ import _build_parser, _cmd_clean, _cmd_compile, _cmd_run, _cmd_serve, main

# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------


def test_build_parser_returns_parser() -> None:
    """Build an argparse parser, not a runtime service."""
    import argparse  # noqa: PLC0415

    assert isinstance(_build_parser(), argparse.ArgumentParser)


def test_build_parser_prog_name() -> None:
    """Use the public Gryphon executable name."""
    assert _build_parser().prog == "gryphon"


def test_build_parser_no_subcommand_gives_none() -> None:
    """Leave the command unset when only help is needed."""
    assert _build_parser().parse_args([]).command is None


def test_build_parser_compile_subcommand() -> None:
    """Compile defaults to deterministic, non-dry execution."""
    args = _build_parser().parse_args(["compile"])
    assert (args.command, args.llm_enhance, args.dry_run) == ("compile", False, False)


def test_build_parser_compile_with_flags() -> None:
    """Retain explicitly requested compiler options."""
    args = _build_parser().parse_args(["compile", "--llm-enhance", "--dry-run"])
    assert (args.llm_enhance, args.dry_run) == (True, True)


def test_build_parser_serve_subcommand_defaults() -> None:
    """Do not override secure settings defaults."""
    args = _build_parser().parse_args(["serve"])
    assert (args.transport, args.host, args.port) == ("stdio", None, None)


@pytest.mark.parametrize("command", ["serve", "run"])
def test_build_parser_http_transport_overrides(command: str) -> None:
    """Both serving commands expose the same bind controls."""
    args = _build_parser().parse_args([command, "--transport", "http", "--host", "127.0.0.2", "--port", "9000"])
    assert (args.transport, args.host, args.port) == ("http", "127.0.0.2", 9000)


def test_build_parser_run_subcommand() -> None:
    """Run defaults to stdio and supplies missing host/port attributes."""
    args = _build_parser().parse_args(["run"])
    assert (args.command, args.transport, args.host, args.port) == ("run", "stdio", None, None)


# ---------------------------------------------------------------------------
# parser — clean subcommand
# ---------------------------------------------------------------------------


def test_build_parser_clean_subcommand() -> None:
    """Cleaning never implicitly confirms a filesystem change."""
    args = _build_parser().parse_args(["clean"])
    assert (args.then, args.dry_run, args.llm_enhance, args.yes) == (None, False, False, False)


def test_build_parser_clean_compile() -> None:
    """Preserve the clean-then-compile spelling."""
    assert _build_parser().parse_args(["clean", "compile"]).then == "compile"


def test_build_parser_clean_compile_with_flags() -> None:
    """Retain options when combining clean and compile."""
    args = _build_parser().parse_args(["clean", "compile", "--dry-run", "--llm-enhance", "--yes"])
    assert (args.then, args.dry_run, args.llm_enhance, args.yes) == ("compile", True, True, True)


# ---------------------------------------------------------------------------
# _cmd_clean
# ---------------------------------------------------------------------------


async def test_cmd_clean_archives_directory(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """An empty generated directory is renamed, not recursively deleted."""
    compiled_dir = tmp_path / "compiled"
    compiled_dir.mkdir()
    args = _build_parser().parse_args(["clean", "--yes"])
    args._config = gryphon_config
    assert await _cmd_clean(args) == 0
    assert not compiled_dir.exists() and len(list(tmp_path.glob("compiled.gryphon-archive-*"))) == 1


async def test_cmd_clean_nonexistent_dir_exits_0(gryphon_config: GryphonConfig) -> None:
    """Missing outputs are harmless when explicitly confirmed."""
    args = _build_parser().parse_args(["clean", "--yes"])
    args._config = gryphon_config
    assert await _cmd_clean(args) == 0


async def test_cmd_clean_then_compile_calls_compile(gryphon_config: GryphonConfig) -> None:
    """Cleaning reuses the compile handler and the loaded configuration."""
    args = _build_parser().parse_args(["clean", "compile", "--yes"])
    args._config = gryphon_config
    with patch("gryphon.__main__._cmd_compile", new=AsyncMock(return_value=0)) as compile_command:
        await _cmd_clean(args)
    compile_command.assert_awaited_once_with(args)


# ---------------------------------------------------------------------------
# _cmd_compile
# ---------------------------------------------------------------------------


@pytest.fixture
def compile_result() -> MagicMock:
    """Return a compiler result without any real API or filesystem access."""
    return MagicMock(compiled=["weather"], skipped=[], failed=[], total_endpoints=5, mcp_json="private-client-config")


async def test_cmd_compile_success(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """Compile success is represented by exit code zero."""
    args = _build_parser().parse_args(["compile"])
    args._config = gryphon_config
    with patch("gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=compile_result)):
        assert await _cmd_compile(args) == 0


async def test_cmd_compile_with_failures_returns_1(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """A partial compile failure is not reported as success."""
    compile_result.failed = ["bad_server"]
    args = _build_parser().parse_args(["compile"])
    args._config = gryphon_config
    with patch("gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=compile_result)):
        assert await _cmd_compile(args) == 1


async def test_cmd_compile_llm_enhance_sets_config(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """Set requested enhancement before constructing the compiler."""
    args = _build_parser().parse_args(["compile", "--llm-enhance"])
    args._config = gryphon_config
    with patch("gryphon.compiler.orchestrator.Orchestrator") as orchestrator:
        orchestrator.return_value.compile_all = AsyncMock(return_value=compile_result)
        await _cmd_compile(args)
    # llm_enhance should have been set on config
    assert orchestrator.call_args.args[0].llm_enhance is True


async def test_cmd_compile_skipped_sources_exits_0(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """Dry-run propagates to the compiler and accepts up-to-date sources."""
    compile_result.compiled, compile_result.skipped = [], ["weather"]
    args = _build_parser().parse_args(["compile", "--dry-run"])
    args._config = gryphon_config
    with patch(
        "gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=compile_result)
    ) as run:
        assert await _cmd_compile(args) == 0
    run.assert_awaited_once_with(dry_run=True)


# ---------------------------------------------------------------------------
# _cmd_serve
# ---------------------------------------------------------------------------


async def test_cmd_serve_overrides_host_and_port(gryphon_config: GryphonConfig) -> None:
    """Apply optional host and port overrides without relying on MagicMock attributes."""
    args = _build_parser().parse_args(["serve", "--host", "127.0.0.2", "--port", "1234"])
    args._config = gryphon_config
    gryphon_config.compile_on_startup = False
    with patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)):
        await _cmd_serve(args)
    # Host and port should have been overridden on config
    assert (gryphon_config.host, gryphon_config.port) == ("127.0.0.2", 1234)


@pytest.mark.parametrize("enabled", [False, True])
async def test_cmd_serve_respects_compile_on_startup(gryphon_config: GryphonConfig, enabled: bool) -> None:
    """Explicit serve follows its configuration rather than always or never compiling."""
    args = _build_parser().parse_args(["serve"])
    args._config = gryphon_config
    gryphon_config.compile_on_startup = enabled
    with (
        patch("gryphon.__main__._cmd_compile", new=AsyncMock(return_value=0)) as compile_command,
        patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)),
    ):
        await _cmd_serve(args)
    assert compile_command.await_count == int(enabled)


# ---------------------------------------------------------------------------
# _cmd_run
# ---------------------------------------------------------------------------


async def test_cmd_run_compile_failure_returns_1(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """Do not serve stale/partially compiled output when compilation fails."""
    compile_result.failed = ["bad_server"]
    args = _build_parser().parse_args(["run"])
    args._config = gryphon_config
    with (
        patch("gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=compile_result)),
        patch("gryphon.__main__._cmd_serve", new=AsyncMock()) as serve,
    ):
        assert await _cmd_run(args) == 1
    serve.assert_not_awaited()


async def test_cmd_run_compiles_exactly_once(gryphon_config: GryphonConfig, compile_result: MagicMock) -> None:
    """Regression: composed run/serve must not compile twice or lose host defaults."""
    args = _build_parser().parse_args(["run"])
    with (
        patch("gryphon.__main__.load_config", return_value=gryphon_config) as load,
        patch(
            "gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=compile_result)
        ) as run,
        patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)) as serve,
    ):
        assert await _cmd_run(args) == 0
    run.assert_awaited_once_with(dry_run=False)
    serve.assert_awaited_once_with(args, gryphon_config)
    load.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_no_args_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help succeeds even when user configuration is broken."""
    monkeypatch.setattr("sys.argv", ["gryphon"])
    with patch("gryphon.__main__.load_config", side_effect=ValueError), pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_main_compile_command_runs(monkeypatch: pytest.MonkeyPatch, gryphon_config: GryphonConfig) -> None:
    """Load configuration exactly once before dispatching a CLI command."""
    monkeypatch.setattr("sys.argv", ["gryphon", "compile"])
    with (
        patch("gryphon.__main__.load_dotenv"),
        patch("gryphon.__main__.load_config", return_value=gryphon_config) as load,
        patch("gryphon.__main__.setup_logging"),
        patch("gryphon.__main__._cmd_compile", new=AsyncMock(return_value=0)),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    assert exc_info.value.code == 0
    load.assert_called_once_with(None)


def test_main_config_load_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration errors no longer continue with insecure fallback settings."""
    monkeypatch.setattr("sys.argv", ["gryphon", "doctor"])
    with (
        patch("gryphon.__main__.load_dotenv"),
        patch("gryphon.__main__.load_config", side_effect=ValueError("bad config")),
        patch("gryphon.__main__.setup_logging") as setup,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()
    # Falls back to INFO level for safe failure diagnostics only
    setup.assert_called_once_with("INFO")
    assert exc_info.value.code == 1
