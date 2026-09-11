"""Bounded, owner-scoped JSON artifacts using private atomic filesystem storage."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from gryphon.errors import CacheError, InputValidationError
from gryphon.models import ArtifactRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MAX_BYTES = 16 * 1024 * 1024
_MAX_READ_BYTES = 8192
_MAX_NODES = 100_000
_MAX_DEPTH = 64
_MAX_ENTRIES = 10_000
_INDEX_LIMIT = 2 * 1024 * 1024
_NAMESPACE = ".gryphon-artifacts-v1"
_INDEX = "index.json"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_T = TypeVar("_T")


class ArtifactStore:
    """Private JSON artifacts with per-owner retention and byte-based pagination.

    Files are tracked by a private per-owner index; cleanup never enumerates or
    deletes arbitrary user files. Per-owner advisory directory locks serialize
    instances. Directory descriptors and no-follow opens prevent symlink escapes.
    """

    def __init__(self, root: str, max_entries: int = 100) -> None:
        """Configure private artifact storage without performing filesystem I/O.

        Args:
            root: Trusted storage root; symlink components are rejected.
            max_entries: Retained artifacts per owner, between 1 and 10000.
        """
        if not 1 <= max_entries <= _MAX_ENTRIES:
            raise InputValidationError("Invalid artifact retention limit")
        self._root = Path(os.path.abspath(root))
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    async def put(self, data: Any, owner: str) -> ArtifactRecord:
        """Atomically store bounded JSON and enforce this owner's retention quota.

        Args:
            data: JSON-compatible data; serialized size may not exceed 16 MiB.
            owner: Trusted server-derived ownership namespace.

        Returns:
            Owner-scoped identifier, exact UTF-8 byte size, and content digest.

        Raises:
            InputValidationError: Data exceeds JSON or storage limits.
            CacheError: The private filesystem cannot safely store the artifact.
        """
        record = await self._io(self._put, data, self._owner_hash(owner))
        return record.model_copy(update={"owner": owner})

    async def read(self, artifact_id: str, owner: str, offset: int = 0, limit: int = 8192) -> dict[str, Any]:
        """Read a bounded UTF-8 page using absolute byte offsets.

        Args:
            artifact_id: Lowercase SHA256 owner-scoped identifier.
            owner: Trusted server-derived ownership namespace.
            offset: UTF-8 character-aligned byte offset, including end of file.
            limit: Text byte budget, from 1 to 8192; never split a code point.

        Returns:
            Text, offset, next_offset (also at EOF), eof, and total size_bytes.
            The text's encoded byte length never exceeds limit.

        Raises:
            InputValidationError: Identifier or byte bounds are invalid.
            CacheError: Artifact is missing, foreign, corrupt, or unsafe.
        """
        if not _HEX.fullmatch(artifact_id):
            raise InputValidationError("Invalid artifact identifier")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _MAX_READ_BYTES:
            raise InputValidationError("Invalid artifact read bounds")
        return await self._io(self._read, artifact_id, self._owner_hash(owner), offset, limit)

    async def _io(self, function: Callable[..., _T], *args: Any) -> _T:
        """Offload all I/O and retain the lock until a cancelled worker finishes."""
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise
            except (OSError, ValueError, TypeError, RecursionError):
                raise CacheError("Artifact storage operation failed") from None

    @staticmethod
    def _owner_hash(owner: str) -> str:
        """Derive filesystem-safe namespace names without storing raw owners."""
        if not owner or len(owner) > 1024:
            raise InputValidationError("Invalid artifact owner")
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _put(self, data: Any, owner_hash: str) -> ArtifactRecord:
        """Reserve a bounded ownership claim before durably writing its data."""
        payload = self._serialize(data)
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = hashlib.sha256(f"{owner_hash}:{digest}".encode()).hexdigest()
        with self._directory(owner_hash, create=True) as directory:
            index = self._load_index(directory)
            name = f"{artifact_id}.json"
            if artifact_id not in index and self._exists(directory, name):
                raise CacheError("Artifact storage conflict")
            index.pop(artifact_id, None)
            for old_id in list(index)[: max(0, len(index) + 1 - self._max_entries)]:
                self._delete_owned(directory, old_id, index[old_id])
                del index[old_id]
            index[artifact_id] = [digest, len(payload)]
            self._atomic_write(directory, _INDEX, json.dumps(index, separators=(",", ":")).encode())
            self._atomic_write(directory, name, payload)
        return ArtifactRecord(id=artifact_id, owner=owner_hash, size_bytes=len(payload), sha256=digest)

    def _read(self, artifact_id: str, owner_hash: str, offset: int, limit: int) -> dict[str, Any]:
        """Verify ownership, size, integrity, and character-aligned pagination."""
        with self._directory(owner_hash, create=False) as directory:
            index = self._load_index(directory)
            if artifact_id not in index:
                raise CacheError("Artifact not found")
            digest, size = index[artifact_id]
            with self._file(directory, f"{artifact_id}.json") as file:
                self._verify(file, str(digest), int(size))
                if offset > size:
                    raise InputValidationError("Artifact offset exceeds size")
                os.lseek(file, offset, os.SEEK_SET)
                page = os.read(file, min(limit, size - offset))
            try:
                text = page.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason != "unexpected end of data" or exc.start == 0:
                    raise InputValidationError("Artifact bounds must accommodate complete UTF-8 characters") from None
                text = page[: exc.start].decode("utf-8")
            next_offset = offset + len(text.encode("utf-8"))
            return {
                "artifact_id": artifact_id,
                "text": text,
                "offset": offset,
                "next_offset": next_offset,
                "eof": next_offset == size,
                "size_bytes": size,
            }

    @contextmanager
    def _directory(self, owner_hash: str, *, create: bool) -> Iterator[int]:
        """Walk each path component using pinned no-follow directory descriptors."""
        directory = os.open(self._root.anchor, _DIRECTORY_FLAGS)
        parts = [*self._root.parts[1:], _NAMESPACE, owner_hash]
        try:
            for position, part in enumerate(parts):
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                        os.fsync(directory)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
                os.close(directory)
                directory = child
                if position >= len(parts) - 3:
                    os.fchmod(directory, 0o700)
            fcntl.flock(directory, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(directory)

    @staticmethod
    @contextmanager
    def _file(directory: int, name: str) -> Iterator[int]:
        """Open only regular, singly linked private files without following links."""
        file = os.open(name, _FILE_FLAGS, dir_fd=directory)
        try:
            metadata = os.fstat(file)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CacheError("Unsafe artifact file")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise CacheError("Unsafe artifact permissions")
            yield file
        finally:
            os.close(file)

    def _load_index(self, directory: int) -> dict[str, list[Any]]:
        """Read a bounded, validated index; absent indices never confer ownership."""
        try:
            with self._file(directory, _INDEX) as file:
                if os.fstat(file).st_size > _INDEX_LIMIT:
                    raise CacheError("Invalid artifact index")
                index = json.loads(os.read(file, _INDEX_LIMIT + 1))
        except FileNotFoundError:
            return {}
        if not isinstance(index, dict) or len(index) > _MAX_ENTRIES + 1:
            raise CacheError("Invalid artifact index")
        for key, value in index.items():
            if (
                not _HEX.fullmatch(key)
                or not isinstance(value, list)
                or len(value) != 2
                or not isinstance(value[0], str)
                or not _HEX.fullmatch(value[0])
                or type(value[1]) is not int
                or not 0 <= value[1] <= _MAX_BYTES
            ):
                raise CacheError("Invalid artifact index")
        return index

    def _atomic_write(self, directory: int, name: str, payload: bytes) -> None:
        """Fsync a bounded private temporary file, rename it, then fsync its directory."""
        temporary = f".pending-{uuid.uuid4().hex}"
        if self._exists(directory, name):
            with self._file(directory, name):
                pass
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(file, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if self._exists(directory, temporary):
                os.unlink(temporary, dir_fd=directory)

    @staticmethod
    def _exists(directory: int, name: str) -> bool:
        """Test a directory entry itself, including broken symlinks."""
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _delete_owned(self, directory: int, artifact_id: str, metadata: list[Any]) -> None:
        """Delete only an indexed regular artifact whose content still matches."""
        name = f"{artifact_id}.json"
        try:
            with self._file(directory, name) as file:
                self._verify(file, str(metadata[0]), int(metadata[1]))
        except FileNotFoundError:
            return
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)

    @staticmethod
    def _verify(file: int, digest: str, size: int) -> None:
        """Hash a bounded file incrementally rather than allocating its entire data."""
        if os.fstat(file).st_size != size or size > _MAX_BYTES:
            raise CacheError("Artifact integrity check failed")
        actual = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = os.read(file, min(remaining, 65536))
            if not chunk:
                raise CacheError("Artifact integrity check failed")
            remaining -= len(chunk)
            actual.update(chunk)
        if actual.hexdigest() != digest:
            raise CacheError("Artifact integrity check failed")

    @staticmethod
    def _serialize(data: Any) -> bytes:
        """Bound input structure and serialized UTF-8 before writing anything."""
        stack = [(data, 0)]
        nodes = characters = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if depth > _MAX_DEPTH or nodes > _MAX_NODES:
                raise InputValidationError("Artifact JSON structure exceeds limits")
            kind = type(value)
            if kind is str:
                characters += len(value)
            elif kind in (list, dict):
                if len(value) + nodes + len(stack) > _MAX_NODES:
                    raise InputValidationError("Artifact JSON structure exceeds limits")
                if kind is dict:
                    if any(type(key) is not str for key in value):
                        raise InputValidationError("Artifact JSON keys must be strings")
                    characters += sum(len(key) for key in value)
                    stack.extend((child, depth + 1) for child in value.values())
                else:
                    stack.extend((child, depth + 1) for child in value)
            elif kind not in (int, float, bool, type(None)):
                raise InputValidationError("Artifact data must be JSON compatible")
            if characters > _MAX_BYTES:
                raise InputValidationError("Artifact exceeds storage size limit")
        output = bytearray()
        try:
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(data):
                encoded = chunk.encode("utf-8")
                if len(output) + len(encoded) > _MAX_BYTES:
                    raise InputValidationError("Artifact exceeds storage size limit")
                output.extend(encoded)
        except (TypeError, ValueError, RecursionError):
            raise InputValidationError("Artifact data must be JSON compatible") from None
        return bytes(output)
