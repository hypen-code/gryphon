"""Structured stderr logging with sensitive third-party wire diagnostics disabled."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog


class _StderrHandler(logging.Handler):
    """Resolve stderr at emission time instead of retaining a closed capture stream."""

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted event without redirecting logs into protocol stdout."""
        stream = sys.stderr
        if stream is None or stream.closed:
            return
        try:
            stream.write(self.format(record) + "\n")
            stream.flush()
        except (OSError, ValueError):
            return


def setup_logging(level: str = "INFO") -> None:
    """Configure structured stderr events without exposing database or HTTP payloads.

    Args:
        level: Log level string; unrecognized names use INFO.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Configure standard library logging
    logging.basicConfig(format="%(message)s", level=log_level)
    for name in ("aiosqlite", "httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
    ]
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer() if level.upper() == "DEBUG" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        foreign_pre_chain=shared_processors,
    )
    handler = _StderrHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger for an application module.

    Args:
        name: Logger name, normally the module's __name__.

    Returns:
        A bound structured logger.
    """
    return cast("structlog.BoundLogger", structlog.get_logger(name))
