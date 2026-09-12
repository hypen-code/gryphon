"""Compile tenant-owned JSON through the normal compiler without host-file authority."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from gryphon.compiler.catalog import contained_path, module_name
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.compiler.schemas import validate_document_tree
from gryphon.errors import CompileError, InputValidationError, SecurityViolationError
from gryphon.models import SwaggerSource
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.registry import Registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from gryphon.config import GryphonConfig
    from gryphon.models import Channel, SaaSSpec

_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
_MAX_SPECS = 100


def validate_id(value: str) -> str:
    """Accept only canonical lowercase UUIDs or UUID hex strings before building paths."""
    if not _ID.fullmatch(value):
        raise InputValidationError("Invalid hosted identifier")
    return value


def validate_selection(channel: Channel, specs: Sequence[SaaSSpec]) -> None:
    """Bind the exact immutable spec selection to one enabled tenant-owned channel."""
    validate_id(channel.id)
    validate_id(channel.tenant_id)
    if not channel.enabled:
        raise SecurityViolationError("Channel is disabled")
    if channel.revision < 1 or channel.sandbox_mode not in {"restricted", "docker"}:
        raise InputValidationError("Invalid channel revision or sandbox profile")
    if len(specs) > _MAX_SPECS or len({spec.id for spec in specs}) != len(specs):
        raise InputValidationError("Invalid channel specification selection")
    if set(channel.spec_ids) != {spec.id for spec in specs} or len(set(channel.spec_ids)) != len(channel.spec_ids):
        raise InputValidationError("Channel specification selection does not match")
    for spec in specs:
        validate_id(spec.id)
        if spec.tenant_id != channel.tenant_id:
            raise SecurityViolationError("Specification belongs to a different tenant")


def validate_uploaded_document(document: dict[str, Any], max_bytes: int) -> str:
    """Reject external references and interpolation before invoking normal compiler policy."""
    validate_document_tree(document)
    try:
        encoded = json.dumps(document, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise CompileError("Uploaded specification must be bounded JSON") from None
    if len(encoded.encode()) > max_bytes or "${" in encoded:
        raise CompileError("Uploaded specification exceeds limits or contains environment interpolation")
    nodes: list[Any] = [document]
    while nodes:
        node = nodes.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None and (not isinstance(reference, str) or not reference.startswith("#/")):
                raise CompileError("Uploaded specifications cannot reference external documents")
            nodes.extend(node.values())
        elif isinstance(node, list):
            nodes.extend(node)
    return encoded


def channel_config(base: GryphonConfig, channel: Channel, state_dir: Path) -> GryphonConfig:
    """Copy validated operator settings without re-reading environment or sharing stores."""
    root = contained_path(state_dir, "channels", validate_id(channel.id))
    updates: dict[str, Any] = {
        "cache_db_path": str(contained_path(root, "cache.db")),
        "run_db_path": str(contained_path(root, "runs.db")),
        "artifact_dir": str(contained_path(root, "artifacts")),
        "compiled_output_dir": str(root / "catalog"),
        "swagger_config_file": str(root / "sources.json"),
        "swaggers": None,
        "sandbox_mode": channel.sandbox_mode,
        "sandbox_allowed_imports": list(channel.allowed_imports),
        "http_auth_token": None,
        "compile_on_startup": False,
        "enable_additional_tools": False,
        "allow_writes": False,
        "allowed_write_operations": [],
        "llm_api_key": "",
    }
    return base.model_copy(deep=True, update=updates)


def prepare_channel_storage(config: GryphonConfig) -> None:
    """Create a private owned channel directory before any SQLite file becomes visible."""
    root = contained_path(Path(config.cache_db_path).parent)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = root.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SecurityViolationError("Channel state directory must be private and owned")


async def compile_catalog(config: GryphonConfig, channel: Channel, specs: Sequence[SaaSSpec]) -> Registry:
    """Compile in an owned worker and remove all temporary files, including on cancellation."""
    validate_selection(channel, specs)
    snapshot = [spec.model_copy(deep=True) for spec in specs]
    return await finish_cleanup(asyncio.to_thread(_compile_catalog, config, channel.revision, snapshot))


def _compile_catalog(config: GryphonConfig, revision: int, specs: Sequence[SaaSSpec]) -> Registry:
    """Run the existing compiler in a private temporary workspace and load immutable metadata."""
    with TemporaryDirectory(prefix="gryphon-channel-") as directory:
        root = Path(directory)
        sources = _write_sources(root, specs, config.max_spec_size_bytes)
        source_path = root / "sources.json"
        source_path.write_text(json.dumps({"servers": sources}), encoding="utf-8")
        output = root / "compiled"
        isolated = config.model_copy(
            update={"compiled_output_dir": str(output), "swagger_config_file": str(source_path), "swaggers": None}
        )
        result = asyncio.run(Orchestrator(isolated).compile_all())
        if result.failed:
            raise CompileError("Channel specification compilation failed")
        _stable_metadata(output, isolated, revision, specs)
        registry = Registry(str(output))
        registry.load()
        return registry


def _stable_metadata(output: Path, config: GryphonConfig, revision: int, specs: Sequence[SaaSSpec]) -> None:
    """Bind compiler provenance to immutable upload IDs, not ephemeral staging paths or wall time."""
    compiler = Orchestrator(config)
    for spec in specs:
        name = module_name(spec.name)
        path = output / name / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        source = SwaggerSource(name=name, swagger_url=f"uploaded:{spec.id}", is_read_only=True)
        manifest["template_hash"] = compiler._source_hash(source)
        manifest["compiled_at"] = f"channel-revision:{revision}"
        path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_sources(root: Path, specs: Sequence[SaaSSpec], max_bytes: int) -> list[dict[str, Any]]:
    """Construct trusted source configs; uploaded documents never supply paths or authentication."""
    sources: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, spec in enumerate(specs):
        name = module_name(spec.name)
        if name in names:
            raise CompileError("Channel specification names collide")
        names.add(name)
        path = root / f"source-{index}.json"
        path.write_text(validate_uploaded_document(spec.document, max_bytes), encoding="utf-8")
        sources.append({"name": name, "swagger_url": str(path), "is_read_only": True})
    return sources
