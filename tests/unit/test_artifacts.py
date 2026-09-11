"""Isolated artifact ownership, quota, atomicity, and filesystem safety tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gryphon.errors import CacheError, InputValidationError
from gryphon.runtime.artifacts import ArtifactStore
from gryphon.utils.hashing import hash_content

if TYPE_CHECKING:
    from pathlib import Path


def _owner_path(root: Path, owner: str = "alice") -> Path:
    """Locate the store's private namespace for filesystem adversarial tests."""
    return root / ".gryphon-artifacts-v1" / hash_content(owner)


async def test_artifact_put_metadata_matches_serialized_content(tmp_path: Path) -> None:
    """Metadata reports caller ownership and exact serialized size and digest."""
    data = {"message": "café", "value": 42}
    store = ArtifactStore(str(tmp_path))
    record = await store.put(data, "alice")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    assert (record.owner, record.size_bytes, record.sha256) == (
        "alice",
        len(encoded),
        hashlib.sha256(encoded).hexdigest(),
    )
    assert record.media_type == "application/json" and len(record.id) == 64
    page = await store.read(record.id, "alice")
    assert json.loads(page["text"]) == data and page["eof"]


async def test_artifact_duplicate_content_reuses_identifier(tmp_path: Path) -> None:
    """Identical owner and content use one retained artifact."""
    store = ArtifactStore(str(tmp_path), max_entries=1)
    first, second = await store.put({"value": 1}, "alice"), await store.put({"value": 1}, "alice")
    assert first.id == second.id
    assert len(list(_owner_path(tmp_path).glob("*.json"))) == 2


async def test_artifact_nonprivate_permissions_are_rejected(tmp_path: Path) -> None:
    """An artifact made readable by another account is no longer trusted."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(1, "alice")
    (_owner_path(tmp_path) / f"{record.id}.json").chmod(0o640)
    with pytest.raises(CacheError, match="permissions"):
        await store.read(record.id, "alice")


async def test_artifact_owner_namespaces_are_isolated(tmp_path: Path) -> None:
    """The same content belongs to independent namespaces and distinct IDs."""
    store = ArtifactStore(str(tmp_path))
    alice, bob = await store.put({"value": 1}, "alice"), await store.put({"value": 1}, "bob")
    assert alice.id != bob.id and alice.sha256 == bob.sha256
    with pytest.raises(CacheError):
        await store.read(alice.id, "bob")


async def test_artifact_retention_is_per_owner_and_survives_restart(tmp_path: Path) -> None:
    """Quota eviction removes only the oldest entry in the inserting namespace."""
    store = ArtifactStore(str(tmp_path), max_entries=1)
    first = await store.put(1, "alice")
    bob = await store.put(1, "bob")
    store = ArtifactStore(str(tmp_path), max_entries=1)
    last = await store.put(2, "alice")
    with pytest.raises(CacheError):
        await store.read(first.id, "alice")
    assert (await store.read(last.id, "alice"))["text"] == "2"
    assert (await store.read(bob.id, "bob"))["text"] == "1"
    assert not (_owner_path(tmp_path) / f"{first.id}.json").exists()


async def test_artifact_cleanup_does_not_touch_user_files(tmp_path: Path) -> None:
    """Only indexed store-owned artifacts can be removed during retention."""
    store = ArtifactStore(str(tmp_path), max_entries=1)
    await store.put(1, "alice")
    user_file = tmp_path / "notes.json"
    user_file.write_text("user data")
    unindexed = _owner_path(tmp_path) / f"{'a' * 64}.json"
    unindexed.write_text("other data")
    await store.put(2, "alice")
    assert user_file.read_text() == "user data" and unindexed.read_text() == "other data"


async def test_artifact_utf8_pages_obey_exact_byte_budget(tmp_path: Path) -> None:
    """Consecutive byte offsets reconstruct Unicode without splitting characters."""
    store = ArtifactStore(str(tmp_path))
    data = "aé中𐍈z"
    record = await store.put(data, "alice")
    offset = 0
    parts: list[str] = []
    while True:
        page = await store.read(record.id, "alice", offset=offset, limit=4)
        assert len(page["text"].encode()) <= 4
        assert page["next_offset"] == offset + len(page["text"].encode())
        parts.append(page["text"])
        offset = page["next_offset"]
        if page["eof"]:
            break
        assert page["text"]
    assert json.loads("".join(parts)) == data and offset == record.size_bytes


async def test_artifact_eof_offset_returns_empty_page(tmp_path: Path) -> None:
    """An EOF read is successful and has no spurious continuation."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(42, "alice")
    page = await store.read(record.id, "alice", offset=record.size_bytes)
    assert (page["text"], page["next_offset"], page["eof"]) == ("", record.size_bytes, True)


@pytest.mark.parametrize("offset,limit", [(-1, 10), (0, 0), (0, 8193), (False, 1), (0, True), (100, 2), (2, 2), (1, 1)])
async def test_artifact_invalid_read_bounds_rejected(tmp_path: Path, offset: int, limit: int) -> None:
    """Negative, excessive, noninteger, and invalid UTF-8 bounds are rejected."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put("é", "alice")
    with pytest.raises(InputValidationError):
        await store.read(record.id, "alice", offset=offset, limit=limit)


@pytest.mark.parametrize("identifier", ["../outside", "A" * 64, "f" * 63, "0" * 64 + "/child", "g" * 64])
async def test_artifact_identifier_requires_lowercase_sha256(tmp_path: Path, identifier: str) -> None:
    """Identifiers cannot introduce paths or noncanonical digest spellings."""
    with pytest.raises(InputValidationError, match="identifier"):
        await ArtifactStore(str(tmp_path)).read(identifier, "alice")


async def test_artifact_private_file_and_directory_permissions(tmp_path: Path) -> None:
    """Artifacts and indices are mode 0600 and store directories are mode 0700."""
    root = tmp_path / "private"
    record = await ArtifactStore(str(root)).put({"value": 1}, "alice")
    for directory in (root, root / ".gryphon-artifacts-v1", _owner_path(root)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("index.json", f"{record.id}.json"):
        assert stat.S_IMODE((_owner_path(root) / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("component", ["root", "ancestor", "namespace", "owner"])
async def test_artifact_symlink_directory_components_are_rejected(tmp_path: Path, component: str) -> None:
    """No root, ancestor, or namespace directory symlink is followed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "store"
    if component in {"root", "ancestor"}:
        root.symlink_to(outside, target_is_directory=True)
        if component == "ancestor":
            root = root / "nested"
    else:
        root.mkdir()
        namespace = root / ".gryphon-artifacts-v1"
        if component == "namespace":
            namespace.symlink_to(outside, target_is_directory=True)
        else:
            namespace.mkdir()
            _owner_path(root).symlink_to(outside, target_is_directory=True)
    with pytest.raises(CacheError):
        await ArtifactStore(str(root)).put(1, "alice")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["artifact", "index"])
async def test_artifact_symlink_files_are_rejected_without_touching_target(tmp_path: Path, name: str) -> None:
    """Symlinked data and metadata cannot be read, replaced, or cleaned up."""
    store = ArtifactStore(str(tmp_path), max_entries=1)
    record = await store.put(1, "alice")
    outside = tmp_path / "outside.json"
    outside.write_text("untouched")
    target = _owner_path(tmp_path) / ("index.json" if name == "index" else f"{record.id}.json")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(CacheError):
        await store.read(record.id, "alice")
    with pytest.raises(CacheError):
        await store.put(1, "alice")
    assert outside.read_text() == "untouched" and target.is_symlink()


async def test_artifact_hard_link_is_rejected(tmp_path: Path) -> None:
    """Multiply linked data files cannot be exposed or removed by the store."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(1, "alice")
    os.link(_owner_path(tmp_path) / f"{record.id}.json", tmp_path / "linked.json")
    with pytest.raises(CacheError, match="Unsafe"):
        await store.read(record.id, "alice")


async def test_artifact_integrity_corruption_is_rejected(tmp_path: Path) -> None:
    """Even same-size tampering is caught before returning any text."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(1, "alice")
    (_owner_path(tmp_path) / f"{record.id}.json").write_text("2")
    with pytest.raises(CacheError, match="integrity"):
        await store.read(record.id, "alice")


async def test_artifact_serialized_size_has_16_mib_hard_cap(tmp_path: Path) -> None:
    """The JSON framing counts toward the absolute serialized size limit."""
    with pytest.raises(InputValidationError, match="size limit"):
        await ArtifactStore(str(tmp_path)).put("x" * (16 * 1024 * 1024), "alice")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("data", [float("nan"), {1: "value"}, {"value": object()}])
async def test_artifact_non_json_inputs_are_rejected(tmp_path: Path, data: Any) -> None:
    """Unsupported, nonfinite, and nonstring-key JSON inputs fail safely."""
    with pytest.raises(InputValidationError):
        await ArtifactStore(str(tmp_path)).put(data, "alice")


async def test_artifact_cyclic_input_is_bounded(tmp_path: Path) -> None:
    """A recursive object graph cannot force unbounded serialization work."""
    data: list[Any] = []
    data.append(data)
    with pytest.raises(InputValidationError, match="structure"):
        await ArtifactStore(str(tmp_path)).put(data, "alice")


async def test_artifact_atomic_write_failure_preserves_existing_data(tmp_path: Path) -> None:
    """A failed rename does not partially replace data or leave temporary files."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(1, "alice")
    with (
        patch("gryphon.runtime.artifacts.os.replace", side_effect=OSError("private path")),
        pytest.raises(CacheError, match="^Artifact storage operation failed$"),
    ):
        await store.put(1, "alice")
    assert (await store.read(record.id, "alice"))["text"] == "1"
    assert not list(tmp_path.rglob(".pending-*"))


async def test_artifact_multiple_instances_serialize_per_owner_quota(tmp_path: Path) -> None:
    """Advisory directory locks prevent lost indices across worker instances."""
    first, second = ArtifactStore(str(tmp_path), 1), ArtifactStore(str(tmp_path), 1)
    await asyncio.gather(first.put(1, "alice"), second.put(2, "alice"))
    index = json.loads((_owner_path(tmp_path) / "index.json").read_text())
    assert len(index) == 1 and len(list(_owner_path(tmp_path).glob("*.json"))) == 2


async def test_artifact_io_runs_outside_event_loop(tmp_path: Path) -> None:
    """Serialization and filesystem work execute on the worker thread."""
    loop_thread = threading.get_ident()
    original = ArtifactStore._serialize
    threads: list[int] = []

    def checked(data: Any) -> bytes:
        """Capture worker identity before serializing the test payload."""
        threads.append(threading.get_ident())
        return original(data)

    with patch.object(ArtifactStore, "_serialize", side_effect=checked):
        await ArtifactStore(str(tmp_path)).put(1, "alice")
    assert threads and all(identifier != loop_thread for identifier in threads)


async def test_artifact_cancellation_waits_for_worker_completion(tmp_path: Path) -> None:
    """Cancelling a caller does not abandon a running filesystem transaction."""
    started, release = threading.Event(), threading.Event()
    original = ArtifactStore._serialize

    def blocked(data: Any) -> bytes:
        """Pause a worker until the cancellation test explicitly releases it."""
        started.set()
        if not release.wait(5):
            raise TimeoutError("Worker did not receive test release")
        return original(data)

    store = ArtifactStore(str(tmp_path))
    with patch.object(ArtifactStore, "_serialize", side_effect=blocked):
        task = asyncio.create_task(store.put(1, "alice"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    record = await store.put(1, "alice")
    assert (await store.read(record.id, "alice"))["text"] == "1"


@pytest.mark.parametrize("index", [[], {"invalid": ["a" * 64, 1]}, {"a" * 64: ["bad", 1]}])
async def test_artifact_corrupt_index_is_rejected(tmp_path: Path, index: Any) -> None:
    """Corrupt private metadata is never interpreted as artifact ownership."""
    store = ArtifactStore(str(tmp_path))
    record = await store.put(1, "alice")
    (_owner_path(tmp_path) / "index.json").write_text(json.dumps(index))
    with pytest.raises(CacheError, match="index"):
        await store.read(record.id, "alice")


async def test_artifact_corrupt_eviction_target_cannot_grow_quota(tmp_path: Path) -> None:
    """Failed safe cleanup rejects new writes before allocating another artifact."""
    store = ArtifactStore(str(tmp_path), max_entries=1)
    record = await store.put(1, "alice")
    (_owner_path(tmp_path) / f"{record.id}.json").write_text("2")
    with pytest.raises(CacheError, match="integrity"):
        await store.put(3, "alice")
    assert len(list(_owner_path(tmp_path).glob("*.json"))) == 2
