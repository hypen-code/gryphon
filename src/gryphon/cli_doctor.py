"""Read-only diagnostics with an explicit allowlist of non-secret configuration."""

from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gryphon import __version__
from gryphon.config import _ENV_FILE

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

_PROTOCOL_VERSION = "2026-07-28"
_BUDGET_FIELDS = (
    "context_budget_bytes",
    "discovery_limit",
    "execution_timeout_seconds",
    "queue_timeout_seconds",
    "max_concurrent_executions",
    "max_tool_calls",
    "sandbox_memory_bytes",
    "max_code_size_bytes",
    "max_output_size_bytes",
    "max_response_size_bytes",
    "max_spec_size_bytes",
    "http_timeout_seconds",
    "cache_ttl_seconds",
    "cache_max_entries",
    "run_ttl_seconds",
    "run_max_entries",
    "artifact_max_entries",
)
_PATH_FIELDS = ("swagger_config_file", "compiled_output_dir", "cache_db_path", "run_db_path", "artifact_dir")
_CORE_TOOLS = (
    "list_servers",
    "search_functions",
    "get_functions",
    "execute_code",
    "run_cached_code",
    "submit_code",
    "get_run",
    "cancel_run",
    "list_recipes",
    "read_artifact",
    "transform_artifact",
)


def _installed_version(package: str) -> str | None:
    """Read package metadata without importing or initializing any runtime service."""
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _path_status(raw: str) -> dict[str, Any]:
    """Report path existence without opening configuration, database, or artifact files."""
    path = Path(raw).absolute()
    return {"path": str(path), "exists": path.exists(), "symlink": path.is_symlink()}


def _configuration(config: GryphonConfig, env_file: str | None) -> dict[str, Any]:
    """Select configuration diagnostics without serializing arbitrary settings."""
    paths = {name: _path_status(getattr(config, name)) for name in _PATH_FIELDS}
    paths["env_file"] = _path_status(env_file if env_file is not None else str(_ENV_FILE))
    return {
        "paths": paths,
        "compile_on_startup": config.compile_on_startup,
        "http_auth_configured": config.http_auth_token is not None,
        "ucp_agent_profile_configured": config.ucp_agent_profile is not None,
        "http_auth_required": True,
        "port": config.port,
        "allow_writes": config.allow_writes,
        "allow_private_networks": config.allow_private_networks,
    }


def _execution(config: GryphonConfig) -> dict[str, Any]:
    """Describe execution prerequisites without probing or starting the daemon."""
    import shutil  # noqa: PLC0415

    return {
        "profile": config.sandbox_mode,
        "restricted_engine": "Monty",
        "docker_required": config.sandbox_mode == "docker",
        "docker_cli_present": shutil.which("docker") is not None,
        "docker_daemon": "not_probed",
        "docker_network": "none",
        "daemon_autostart": False,
    }


def doctor_report(config: GryphonConfig, env_file: str | None = None) -> str:
    """Build a JSON diagnostics report without network, compilation, or daemon access.

    Args:
        config: Validated settings. Only explicitly allowlisted fields are read.
        env_file: Selected environment file, reported as a path rather than content.

    Returns:
        JSON containing versions, declared capabilities, configuration paths, and budgets.
    """
    packages = {name: _installed_version(name) for name in ("fastmcp", "mcp", "pydantic-monty", "aiodocker")}
    report = {
        "name": "Gryphon",
        "version": __version__,
        "python": sys.version.split()[0],
        "mcp_protocol_version": _PROTOCOL_VERSION,
        "packages": packages,
        "capabilities": {
            "framework": "FastMCP 4",
            "tools": list(_CORE_TOOLS),
            "prompts": ["reusable_code_guide"],
            "native_json": True,
            "structured_inputs": True,
            "durable_run_receipts": True,
            "mcp_tasks": False,
            "generated_host_code_execution": False,
            "additional_tools_enabled": config.enable_additional_tools,
        },
        "configuration": _configuration(config, env_file),
        "budgets": {name: getattr(config, name) for name in _BUDGET_FIELDS},
        "execution": _execution(config),
        "read_only": True,
    }
    return json.dumps(report, indent=2, sort_keys=True)
