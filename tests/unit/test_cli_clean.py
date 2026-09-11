"""Temporary-filesystem tests for conservative, reversible Gryphon cleanup."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from gryphon.__main__ import _build_parser, _cmd_clean
from gryphon.cli_clean import archive_outputs
from gryphon.models import ServerManifest
from gryphon.runtime.cache import CacheStore

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


def _compiled_module(root: Path) -> Path:
    """Create a recognized generated module entirely within a temporary directory."""
    module = root / "weather"
    module.mkdir(parents=True)
    manifest = ServerManifest(
        server_name="weather",
        description="Weather",
        swagger_hash="test-hash",
        compiled_at="2026-07-28T00:00:00Z",
        base_url="https://weather.example.com",
        is_read_only=True,
    )
    (module / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (module / "functions.py").write_text('"""Generated fixture."""\n', encoding="utf-8")
    return module


async def test_clean_requires_explicit_confirmation(gryphon_config: GryphonConfig) -> None:
    """No archive helper or filesystem access is invoked without --yes."""
    args = _build_parser().parse_args(["clean"])
    args._config = gryphon_config
    with patch("gryphon.cli_clean.archive_outputs") as archive:
        assert await _cmd_clean(args) == 1
    archive.assert_not_called()


async def test_clean_archives_only_compiled_output_and_cache(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """Keep cache bytes and generated modules recoverable, retaining other stores."""
    module = _compiled_module(tmp_path / "compiled")
    cache = CacheStore(gryphon_config.cache_db_path)
    await cache.initialize()
    await cache.close()
    cache_bytes = Path(gryphon_config.cache_db_path).read_bytes()
    runs = tmp_path / "runs.db"
    runs.write_bytes(b"run-records")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "retained.json").write_text("{}", encoding="utf-8")
    gryphon_config.run_db_path, gryphon_config.artifact_dir = str(runs), str(artifacts)
    archive_outputs(gryphon_config)
    compiled_archive = next(tmp_path.glob("compiled.gryphon-archive-*"))
    cache_archive = next((tmp_path / "data").glob("cache.db.gryphon-archive-*"))
    assert (compiled_archive / module.name / "functions.py").exists() and cache_archive.read_bytes() == cache_bytes
    assert runs.read_bytes() == b"run-records" and (artifacts / "retained.json").exists()


def test_clean_unknown_compiled_content_refuses_all_changes(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """Do not move an arbitrary directory merely because it is configured as compiled output."""
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    user_file = compiled / "valuable.txt"
    user_file.write_text("user-owned", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown content"):
        archive_outputs(gryphon_config)
    assert user_file.read_text(encoding="utf-8") == "user-owned" and not list(tmp_path.glob("*.gryphon-archive-*"))


def test_clean_unknown_module_file_refuses_archive(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """Manifest presence does not authorize moving extra user files."""
    module = _compiled_module(tmp_path / "compiled")
    (module / "notes.txt").write_text("do not move", encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown content"):
        archive_outputs(gryphon_config)
    assert module.exists()


@pytest.mark.parametrize("protected", ["root", "home", "repo", "cwd"])
def test_clean_protected_paths_are_refused(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected: str,
) -> None:
    """Reject dangerous roots without performing actual clean operations on them."""
    monkeypatch.chdir(tmp_path)
    choices = {
        "root": "/",
        "home": str(Path.home()),
        "repo": str(Path(__file__).resolve().parents[2]),
        "cwd": str(tmp_path),
    }
    gryphon_config.compiled_output_dir = choices[protected]
    with pytest.raises(ValueError, match="protected"):
        archive_outputs(gryphon_config)


@pytest.mark.parametrize("target", ["compiled", "cache", "ancestor", "module_file"])
def test_clean_symbolic_links_are_refused(gryphon_config: GryphonConfig, tmp_path: Path, target: str) -> None:
    """Refuse direct, nested, and ancestor symlinks before moving any output."""
    real = tmp_path / "real"
    real.mkdir()
    if target == "compiled":
        (tmp_path / "compiled").symlink_to(real, target_is_directory=True)
    elif target == "cache":
        cache = Path(gryphon_config.cache_db_path)
        cache.parent.mkdir()
        cache.symlink_to(real / "missing.db")
    elif target == "ancestor":
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        gryphon_config.compiled_output_dir = str(link / "compiled")
    else:
        module = _compiled_module(tmp_path / "compiled")
        (module / "skills.md").symlink_to(real / "missing.md")
    with pytest.raises(ValueError):
        archive_outputs(gryphon_config)
    assert real.exists()


def test_clean_unrecognized_database_refuses_before_compiled_move(
    gryphon_config: GryphonConfig, tmp_path: Path
) -> None:
    """Validate both targets before renaming either one."""
    _compiled_module(tmp_path / "compiled")
    cache = Path(gryphon_config.cache_db_path)
    cache.parent.mkdir()
    connection = sqlite3.connect(cache)
    connection.execute("CREATE TABLE user_data (value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="unrecognized database"):
        archive_outputs(gryphon_config)
    assert (tmp_path / "compiled").exists() and cache.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_clean_database_sidecars_require_stopped_server(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    suffix: str,
) -> None:
    """Never separate SQLite journal state from its database."""
    cache = Path(gryphon_config.cache_db_path)
    cache.parent.mkdir()
    cache.with_name(cache.name + suffix).write_bytes(b"state")
    with pytest.raises(ValueError, match="Stop the server"):
        archive_outputs(gryphon_config)
    assert cache.with_name(cache.name + suffix).exists()


@pytest.mark.parametrize("retained", ["run_db_path", "artifact_dir", "swagger_config_file"])
def test_clean_paths_overlapping_retained_data_are_refused(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    retained: str,
) -> None:
    """Even valid generated directories cannot include configured runs, artifacts, or config."""
    module = _compiled_module(tmp_path / "compiled")
    setattr(gryphon_config, retained, str(module / "retained"))
    with pytest.raises(ValueError, match="overlapping"):
        archive_outputs(gryphon_config)
    assert module.exists()


def test_clean_dry_run_does_not_archive(gryphon_config: GryphonConfig, tmp_path: Path) -> None:
    """Dry-run validates recognition without altering filesystem contents."""
    module = _compiled_module(tmp_path / "compiled")
    archive_outputs(gryphon_config, dry_run=True)
    assert module.exists() and not list(tmp_path.glob("*.gryphon-archive-*"))


async def test_clean_second_rename_failure_rolls_back_first(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cache rename restores the already-renamed compiled directory."""
    module = _compiled_module(tmp_path / "compiled")
    cache = CacheStore(gryphon_config.cache_db_path)
    await cache.initialize()
    await cache.close()
    original = Path.rename

    def fail_cache(source: Path, destination: str | Path) -> Path:
        """Simulate an isolated cache filesystem failure."""
        if str(source) == gryphon_config.cache_db_path:
            raise OSError("fixture failure")
        return original(source, destination)

    monkeypatch.setattr(Path, "rename", fail_cache)
    with pytest.raises(OSError, match="fixture failure"):
        archive_outputs(gryphon_config)
    assert module.exists() and Path(gryphon_config.cache_db_path).exists()
