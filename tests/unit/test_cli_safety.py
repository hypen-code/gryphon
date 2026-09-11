"""Regression coverage for safe CLI boundaries, diagnostics, and stdio output."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from gryphon.__main__ import _build_parser, _cmd_compile, _cmd_doctor, _cmd_run, _cmd_serve, main
from gryphon.cli_doctor import doctor_report
from gryphon.config import GryphonConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("command", ["compile", "serve", "run", "clean", "doctor"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_env_file_on_either_side_of_subcommand(command: str, position: str) -> None:
    """Do not let a subparser default erase a global environment file."""
    flag = ["--env-file", "custom.env"]
    argv = [*flag, command] if position == "before" else [command, *flag]
    assert _build_parser().parse_args(argv).env_file == "custom.env"


def test_env_file_last_explicit_value_wins() -> None:
    """An explicit subcommand value may override the preceding global value."""
    args = _build_parser().parse_args(["--env-file", "first.env", "doctor", "--env-file", "second.env"])
    assert args.env_file == "second.env"


def test_version_is_gryphon_v2_without_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Version output does not load .env files or initialize any application services."""
    monkeypatch.setattr("sys.argv", ["gryphon", "--version"])
    with patch("gryphon.__main__.load_config") as load, pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0 and capsys.readouterr().out == "Gryphon 2.0.0\n"
    load.assert_not_called()


@pytest.mark.parametrize("command", ["run", "serve"])
async def test_http_without_auth_fails_before_compile_or_startup(gryphon_config: GryphonConfig, command: str) -> None:
    """A missing token must not trigger compilation, storage, or a listener."""
    args = _build_parser().parse_args([command, "--transport", "http"])
    args._config = gryphon_config
    gryphon_config.http_auth_token = None
    with (
        patch("gryphon.__main__._cmd_compile", new=AsyncMock()) as compile_command,
        patch("gryphon.__main__._serve_started", new=AsyncMock()) as serve,
    ):
        assert await (_cmd_run(args) if command == "run" else _cmd_serve(args)) == 1
    compile_command.assert_not_awaited()
    serve.assert_not_awaited()


async def test_http_rechecks_short_token_after_config_mutation(gryphon_config: GryphonConfig) -> None:
    """Do not rely solely on construction-time Pydantic validation for HTTP auth."""
    gryphon_config.http_auth_token = SecretStr("too-short")
    args = _build_parser().parse_args(["serve", "--transport", "http"])
    args._config = gryphon_config
    with patch("gryphon.__main__._serve_started", new=AsyncMock()) as serve:
        assert await _cmd_serve(args) == 1
    serve.assert_not_awaited()


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
async def test_http_invalid_port_override_fails_closed(gryphon_config: GryphonConfig, port: str) -> None:
    """Reject CLI values that would bypass the settings port bounds."""
    args = _build_parser().parse_args(["serve", "--port", port])
    args._config = gryphon_config
    with patch("gryphon.__main__._serve_started", new=AsyncMock()) as serve:
        assert await _cmd_serve(args) == 1
    serve.assert_not_awaited()


def test_main_validation_error_omits_input_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never log Pydantic input_value fields or raw exception text."""
    secret = "sentinel-sensitive-value"
    monkeypatch.setitem(GryphonConfig.model_config, "env_file", None)
    with pytest.raises(ValidationError) as error:
        GryphonConfig(http_auth_token=SecretStr(secret))
    monkeypatch.setattr("sys.argv", ["gryphon", "doctor"])
    with (
        patch("gryphon.__main__.load_dotenv"),
        patch("gryphon.__main__.load_config", side_effect=error.value),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    output = capsys.readouterr()
    assert exc.value.code == 1 and output.out == ""
    assert secret not in output.err and "input_value" not in output.err and "command_failed" in output.err


@pytest.mark.parametrize("position", ["before", "after"])
def test_main_custom_env_file_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    position: str,
) -> None:
    """Both argument orders actually configure the command, not just argparse."""
    env_file = tmp_path / "custom.env"
    env_file.write_text("GRYPHON_EXECUTION_TIMEOUT_SECONDS=17\n", encoding="utf-8")
    monkeypatch.delenv("GRYPHON_EXECUTION_TIMEOUT_SECONDS", raising=False)
    flags = ["--env-file", str(env_file)]
    monkeypatch.setattr(
        "sys.argv", ["gryphon", *flags, "doctor"] if position == "before" else ["gryphon", "doctor", *flags]
    )
    with patch("gryphon.__main__.load_dotenv"), pytest.raises(SystemExit) as exc:
        main()
    report = json.loads(capsys.readouterr().out)
    assert exc.value.code == 0 and report["budgets"]["execution_timeout_seconds"] == 17


@pytest.mark.parametrize("skipped", [False, True])
@pytest.mark.parametrize("command", [["compile"], ["clean", "compile", "--yes"]])
def test_compile_prints_client_json_to_stdout_without_logging_it(
    gryphon_config: GryphonConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    skipped: bool,
    command: list[str],
) -> None:
    """Fresh and up-to-date standalone compilations expose copyable JSON separately from logs."""
    monkeypatch.setattr("sys.argv", ["gryphon", *command])
    payload = {"mcpServers": {"gryphon": {"command": "/test/bin/gryphon", "args": ["serve"], "env": {}}}}
    result = MagicMock(
        compiled=[] if skipped else ["weather"],
        skipped=["weather"] if skipped else [],
        failed=[],
        total_endpoints=0 if skipped else 1,
        mcp_json=json.dumps(payload, indent=2),
    )
    with (
        patch("gryphon.__main__.load_dotenv"),
        patch("gryphon.__main__.load_config", return_value=gryphon_config),
        patch("gryphon.cli_clean.archive_outputs"),
        patch("gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=result)),
        pytest.raises(SystemExit) as exc,
    ):
        main()
    output = capsys.readouterr()
    assert exc.value.code == 0 and json.loads(output.out) == payload
    assert "compile_summary" in output.err and "mcpServers" not in output.err


@pytest.mark.parametrize("command", ["serve", "run"])
async def test_startup_compilation_never_prints_client_json(
    gryphon_config: GryphonConfig, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Automatic compilation preserves stdout exclusively for the MCP transport."""
    gryphon_config.compile_on_startup = True
    args = _build_parser().parse_args([command])
    args._config = gryphon_config
    result = MagicMock(compiled=["weather"], skipped=[], failed=[], total_endpoints=1, mcp_json="client-json")
    with (
        patch(
            "gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=result)
        ) as compile_all,
        patch("gryphon.__main__._serve_started", new=AsyncMock(return_value=0)),
    ):
        assert await (_cmd_run(args) if command == "run" else _cmd_serve(args)) == 0
    compile_all.assert_awaited_once()
    output = capsys.readouterr()
    assert output.out == "" and "client-json" not in output.err


@pytest.mark.parametrize("case", ["dry_run", "partial_failure", "no_sources"])
async def test_compile_without_complete_output_does_not_print_client_json(
    gryphon_config: GryphonConfig, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """Dry runs, failed compilation and absent catalogs never advertise a ready client entry."""
    args = _build_parser().parse_args(["compile", *(["--dry-run"] if case == "dry_run" else [])])
    args._config = gryphon_config
    result = MagicMock(
        compiled=[],
        skipped=[],
        failed=["weather"] if case == "partial_failure" else [],
        total_endpoints=0,
        mcp_json=None if case == "no_sources" else "client-json",
    )
    with patch("gryphon.compiler.orchestrator.Orchestrator.compile_all", new=AsyncMock(return_value=result)):
        assert await _cmd_compile(args) == int(case == "partial_failure")
    output = capsys.readouterr()
    assert output.out == "" and "client-json" not in output.err


def test_doctor_reports_v2_capabilities_and_budgets(gryphon_config: GryphonConfig) -> None:
    """Diagnostics identify the protocol/framework and bounded restricted default."""
    report = json.loads(doctor_report(gryphon_config))
    assert (report["version"], report["mcp_protocol_version"], report["capabilities"]["framework"]) == (
        "2.0.0",
        "2026-07-28",
        "FastMCP 4",
    )
    assert report["execution"]["profile"] == "restricted" and report["execution"]["docker_required"] is False
    assert report["budgets"]["max_tool_calls"] == gryphon_config.max_tool_calls


async def test_doctor_is_read_only_and_never_starts_services(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """Doctor does not compile, open stores, probe Docker, or create files."""
    args = _build_parser().parse_args(["doctor"])
    args._config = gryphon_config
    before = set(tmp_path.rglob("*"))
    with (
        patch("gryphon.__main__._cmd_compile", new=AsyncMock(side_effect=AssertionError)),
        patch("gryphon.__main__._serve_started", new=AsyncMock(side_effect=AssertionError)),
        patch("subprocess.run", side_effect=AssertionError),
        patch("sqlite3.connect", side_effect=AssertionError),
        patch("socket.create_connection", side_effect=AssertionError),
    ):
        assert await _cmd_doctor(args) == 0
    assert set(tmp_path.rglob("*")) == before


def test_doctor_omits_credentials_and_secret_configuration(gryphon_config: GryphonConfig) -> None:
    """Report booleans for auth rather than model-dumping secret-bearing settings."""
    secret = "sensitive-value-not-for-diagnostics" * 2
    gryphon_config.http_auth_token = SecretStr(secret)
    gryphon_config.llm_api_key = secret
    gryphon_config.docker_host = secret
    gryphon_config.allowed_domains = [secret]
    report = doctor_report(gryphon_config)
    assert secret not in report and "llm_api_key" not in report and "docker_host" not in report
    assert json.loads(report)["configuration"]["http_auth_configured"] is True


def test_doctor_missing_optional_package_is_reported(gryphon_config: GryphonConfig) -> None:
    """Missing dependency metadata is a report value, not a service startup attempt."""
    with patch("gryphon.cli_doctor._installed_version", return_value=None):
        report = json.loads(doctor_report(gryphon_config))
    assert report["packages"]["aiodocker"] is None and report["execution"]["docker_daemon"] == "not_probed"
