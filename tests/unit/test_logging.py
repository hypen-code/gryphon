"""Logging boundary tests for protocol integrity and third-party payload privacy."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest

from gryphon.utils.logging import _StderrHandler, get_logger, setup_logging


@pytest.mark.parametrize("name", ["aiosqlite", "httpx", "httpcore", "httpx2", "httpcore2"])
def test_debug_mode_does_not_enable_raw_wire_diagnostics(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    """SQL parameters and HTTP details stay disabled even with application DEBUG."""
    setup_logging("DEBUG")
    logging.getLogger(name).debug("synthetic-sensitive-wire-payload")
    captured = capsys.readouterr()
    assert "synthetic-sensitive-wire-payload" not in captured.err + captured.out


def test_application_events_use_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """Application logging never corrupts an MCP stdio response stream."""
    setup_logging("INFO")
    get_logger("gryphon.logging_test").info("execution_finished", success=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "execution_finished" in captured.err


def test_handler_uses_current_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler does not retain an earlier closed pytest or embedded-host stream."""
    previous, current = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stderr", previous)
    handler = _StderrHandler()
    previous.close()
    monkeypatch.setattr("sys.stderr", current)
    handler.emit(logging.LogRecord("gryphon", logging.INFO, "", 0, "current-stream", (), None))
    assert current.getvalue() == "current-stream\n"


@pytest.mark.parametrize("missing", [True, False])
def test_handler_tolerates_unavailable_stderr(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    """Process shutdown cannot redirect diagnostics onto stdout."""
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr("sys.stderr", None if missing else stream)
    _StderrHandler().emit(logging.LogRecord("gryphon", logging.INFO, "", 0, "event", (), None))


def test_handler_tolerates_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logging I/O failures do not break an otherwise successful execution."""

    class BrokenStream:
        """A host-owned stderr that fails while being written."""

        closed = False

        def write(self, value: Any) -> None:
            """Simulate a closed pipe without disclosing any event payload."""
            raise OSError("closed")

    monkeypatch.setattr("sys.stderr", BrokenStream())
    _StderrHandler().emit(logging.LogRecord("gryphon", logging.INFO, "", 0, "event", (), None))
