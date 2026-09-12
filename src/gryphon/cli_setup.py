"""Environment-only stdio configuration with isolated writable user state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from gryphon.compiler.catalog import contained_path
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.config import GryphonConfig, load_config


def _state_root(config: GryphonConfig) -> Path:
    """Choose an absolute operator root or the XDG user-state fallback."""
    if config.state_dir is not None:
        root = Path(config.state_dir).expanduser()
        if not root.is_absolute():
            raise ValueError("GRYPHON_STATE_DIR must be an absolute path")
        return root
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "state"
    return base / "gryphon"


def _source_namespace(config: GryphonConfig) -> str:
    """Hash canonical source configuration without persisting raw credential-bearing settings."""
    sources = Orchestrator(config).load_swagger_sources()
    for source in sources:
        for field in ("swagger_url", "skills_url"):
            value = getattr(source, field)
            if value and "://" not in value:
                setattr(source, field, str(Path(value).expanduser().absolute()))
    config.swaggers = sources
    canonical = [source.model_dump(mode="json") for source in sorted(sources, key=lambda source: source.name)]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_stdio_config(env_file: str | None = None) -> GryphonConfig:
    """Load explicit settings and derive source-scoped paths without reading ambient env files.

    Args:
        env_file: An explicitly selected env file, or None for launch-environment settings only.

    Returns:
        Validated settings with absolute default state paths. Explicit storage paths are retained.
    """
    config = load_config(env_file, discover_env=False)
    if config.swaggers is None and "swagger_config_file" not in config.model_fields_set:
        config.swaggers = []
    root = _state_root(config)
    state = contained_path(root, _source_namespace(config))
    config.state_dir = str(root)
    for field, path in {
        "compiled_output_dir": state / "compiled",
        "cache_db_path": state / "cache.db",
        "run_db_path": state / "runs.db",
        "artifact_dir": state / "artifacts",
    }.items():
        if field not in config.model_fields_set:
            setattr(config, field, str(path))
    return config


def prepare_stdio_state(config: GryphonConfig) -> None:
    """Create private state directories only at startup, rejecting existing symlink components."""
    if config.state_dir is None:
        raise ValueError("Stdio state root has not been selected")
    root = contained_path(Path(config.state_dir))
    state = contained_path(root, _source_namespace(config))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.mkdir(mode=0o700, exist_ok=True)
