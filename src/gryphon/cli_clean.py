"""Conservative, reversible archival of recognized Gryphon compiler/cache output."""

from __future__ import annotations

import sqlite3
from pathlib import Path  # noqa: PLC0415
from typing import TYPE_CHECKING
from uuid import uuid4

from gryphon.models import ServerManifest
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

_GENERATED_FILES = frozenset({"manifest.json", "functions.py", "__init__.py", "top_level_functions.py", "skills.md"})
_CACHE_COLUMNS = frozenset(
    {
        "id",
        "description",
        "code",
        "servers_used",
        "swagger_hash",
        "created_at",
        "last_used_at",
        "use_count",
        "ttl_seconds",
        "owner",
        "input_schema",
    }
)
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def _checked_path(raw: str) -> Path:
    """Reject symlinks, protected directories, and their ancestors before resolving."""
    path = Path(raw).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Clean refuses symbolic links")
    path = path.resolve()
    protected = (Path.home().resolve(), Path.cwd().resolve(), Path(__file__).resolve().parents[2])
    if any(path == item or path in item.parents for item in protected):
        raise ValueError("Clean refuses protected directories")
    return path


def _plain_file(path: Path) -> None:
    """Reject links, directories, devices, and multiply linked files."""
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Clean requires an ordinary, unlinked file")


def _validate_compiled(path: Path) -> None:
    """Recognize only shallow module directories with validated Gryphon manifests."""
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError("Compiled output must be a directory")
    for module in path.iterdir():
        if module.is_symlink() or not module.is_dir() or not module.name.isidentifier():
            raise ValueError("Unknown content in compiled output")
        for item in module.iterdir():
            _plain_file(item)
            if item.name not in _GENERATED_FILES:
                raise ValueError("Unknown content in compiled module")
        manifest_path = module / "manifest.json"
        _plain_file(manifest_path)
        with manifest_path.open("rb") as stream:
            content = stream.read(_MAX_MANIFEST_BYTES + 1)
        if len(content) > _MAX_MANIFEST_BYTES:
            raise ValueError("Compiled manifest exceeds archival validation limit")
        manifest = ServerManifest.model_validate_json(content)
        if manifest.server_name != module.name:
            raise ValueError("Compiled module identity mismatch")


def _validate_cache(path: Path) -> None:
    """Accept only a closed Gryphon cache, never an arbitrary configured database."""
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("Stop the server and close/checkpoint the cache before cleaning")
    if not path.exists():
        return
    _plain_file(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(code_cache)")}
        if tables != {"code_cache"} or not {"id", "code", "description"} <= columns <= _CACHE_COLUMNS:
            raise ValueError("Clean refuses an unrecognized database")
    finally:
        connection.close()


def _archive_plan(config: GryphonConfig) -> list[tuple[Path, Path]]:
    """Validate the entire plan before moving anything; never include unrelated stores."""
    # Archive compiled output directory
    compiled_dir = _checked_path(config.compiled_output_dir)
    # Archive cache database
    cache_db = _checked_path(config.cache_db_path)
    targets = (compiled_dir, cache_db)
    protected = tuple(
        Path(raw).resolve() for raw in (config.run_db_path, config.artifact_dir, config.swagger_config_file)
    )
    for target in targets:
        if any(target == item or target in item.parents or item in target.parents for item in protected):
            raise ValueError("Clean refuses paths overlapping retained data or configuration")
    if compiled_dir == cache_db or compiled_dir in cache_db.parents or cache_db in compiled_dir.parents:
        raise ValueError("Clean requires non-overlapping output paths")
    _validate_compiled(compiled_dir)
    _validate_cache(cache_db)
    suffix = ".gryphon-archive-" + uuid4().hex
    return [(path, path.with_name(path.name + suffix)) for path in targets if path.exists()]


def archive_outputs(config: GryphonConfig, *, dry_run: bool = False) -> None:
    """Archive only recognized output, retaining a same-filesystem restoration path.

    Stop the server first. Unknown content, links, database sidecars, and paths
    overlapping runs/artifacts/configuration are refused before any rename.
    Restore manually while stopped by renaming each archive to its original name.

    Args:
        config: Trusted settings identifying compiled output and the cache database.
        dry_run: Validate and report the plan without moving anything.

    Raises:
        ValueError: If any path or its contents cannot be safely recognized.
        OSError: If validation or an archive rename fails.
        sqlite3.Error: If the configured cache is not a readable SQLite database.
    """
    plan = _archive_plan(config)
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in plan:
            if destination.exists() or destination.is_symlink():
                raise ValueError("Archive destination already exists")
            if not dry_run:
                source.rename(destination)
                moved.append((source, destination))
            get_logger(__name__).info("clean_archive", source=str(source), archive=str(destination), dry_run=dry_run)
    except OSError:
        for source, destination in reversed(moved):
            if not source.exists() and not source.is_symlink():
                destination.rename(source)
        raise
    if not plan:
        get_logger(__name__).info("clean_nothing_to_archive")
