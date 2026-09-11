"""Durable owner-scoped run receipts and atomic bounded idempotency."""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import time
import uuid
from typing import TYPE_CHECKING

from gryphon.errors import CacheError, ConflictError, InputValidationError
from gryphon.models import ExecutionResult, RunRecord
from gryphon.runtime.cache import _SQLiteStore
from gryphon.utils.hashing import hash_content

if TYPE_CHECKING:
    import aiosqlite

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_NAMESPACE_LENGTH = 1024
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_hash TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result TEXT,
    UNIQUE(owner, idempotency_hash)
);
CREATE INDEX IF NOT EXISTS idx_runs_retention ON runs(status, updated_at);
"""


class RunStore(_SQLiteStore):
    """Single-process run ledger sharing SQLite's atomic writer transactions.

    Recovery is explicit: initialize never changes queued or running states.
    Active runs are never evicted; a full active ledger rejects new admission.
    Idempotency is retained only as long as its associated receipt. Executors
    must claim ownership before recovery and retain it until close. Claiming
    requires POSIX flock on Linux/macOS; Windows deployments run in Docker.
    """

    def __init__(self, db_path: str, ttl_seconds: int = 86400, max_entries: int = 1000) -> None:
        """Configure durable receipt retention.

        Args:
            db_path: SQLite database path, optionally shared with the recipe cache.
            ttl_seconds: Terminal receipt lifetime after its last state change.
            max_entries: Maximum receipts, including active runs.
        """
        super().__init__(db_path)
        if ttl_seconds < 1 or max_entries < 1:
            raise InputValidationError("Run retention limits must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._execution_fd: int | None = None
        self._claim_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the durable ledger without recovering or claiming execution.

        Raises:
            CacheError: If the database cannot be initialized.
        """
        await self._open(_CREATE_RUNS_SQL)
        async with self._transaction("initialize run ledger") as db:
            await self._expire(db, time.time())
            await self._trim(db, self._max_entries)

    async def claim_execution(self) -> None:
        """Exclusively claim this database's executor lifecycle without waiting.

        Call after initialize and before recovery. Repeated claims by this store
        are idempotent. The private ``<db_path>.lock`` sibling is never removed:
        closing its owned descriptor releases the process-wide advisory lock.
        Acquisition performs only small metadata operations and nonblocking
        flock, with no cancellation point after opening the descriptor.

        Raises:
            CacheError: The store is uninitialized, already claimed elsewhere,
                or its lock file cannot be safely opened.
        """
        async with self._claim_lock, self._lock:
            if self._db is None:
                raise CacheError("Cannot claim execution before initialization")
            if self._execution_fd is None:
                self._execution_fd = self._open_execution_lock()

    async def close(self) -> None:
        """Close the database before releasing only this store's execution claim.

        Release also occurs if database closure fails. Repeated closes and
        closing a store whose claim failed never affect another owner's lock.

        Raises:
            CacheError: An owned lock descriptor cannot be released.
        """
        async with self._claim_lock:
            try:
                await super().close()
            finally:
                descriptor, self._execution_fd = self._execution_fd, None
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        raise CacheError("Unable to release executor ownership") from None

    def _open_execution_lock(self) -> int:
        """Acquire a private no-follow POSIX lock, closing only failed claims."""
        descriptor: int | None = None
        claimed = False
        try:
            descriptor = os.open(f"{self._db_path}.lock", _LOCK_FLAGS, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid():
                raise CacheError("Unsafe executor ownership file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(descriptor, 0o600)
            claimed = True
            return descriptor
        except OSError:
            raise CacheError("Unable to claim executor ownership") from None
        finally:
            if descriptor is not None and not claimed:
                try:
                    os.close(descriptor)
                except OSError:
                    raise CacheError("Unable to claim executor ownership") from None

    async def create(self, owner: str, request_hash: str, idempotency_key: str | None = None) -> tuple[RunRecord, bool]:
        """Atomically admit a run or return the same owner's matching receipt.

        Args:
            owner: Trusted server-derived ownership namespace.
            request_hash: Digest of the request, never raw code or inputs.
            idempotency_key: Optional opaque caller key; only its digest is stored.

        Returns:
            Receipt and whether it was newly created.

        Raises:
            ConflictError: A retained key identifies a different request.
            CacheError: Storage is unavailable or all capacity is occupied.
        """
        self._validate_identity(owner, request_hash, idempotency_key)
        key_hash = hash_content(idempotency_key) if idempotency_key is not None else None
        now = time.time()
        async with self._transaction("create run") as db:
            await self._expire(db, now)
            if key_hash is not None:
                async with db.execute(
                    "SELECT * FROM runs WHERE owner = ? AND idempotency_hash = ?", (owner, key_hash)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    if row["request_hash"] != request_hash:
                        raise ConflictError("Idempotency key belongs to a different request")
                    return self._record(row), False
            await self._trim(db, self._max_entries - 1)
            record = RunRecord(
                id=uuid.uuid4().hex, owner=owner, request_hash=request_hash, created_at=now, updated_at=now
            )
            await db.execute(
                """INSERT INTO runs (id, owner, request_hash, idempotency_hash,
                   status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                (record.id, owner, request_hash, key_hash, now, now),
            )
            return record, True

    async def start(self, run_id: str, owner: str) -> None:
        """Transition an owned queued run to running exactly once.

        Args:
            run_id: Receipt identifier.
            owner: Trusted server-derived ownership namespace.

        Raises:
            ConflictError: The owned receipt is not available in queued state.
        """
        async with (
            self._transaction("start run") as db,
            db.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ? AND owner = ? AND status = 'queued'",
                (time.time(), run_id, owner),
            ) as cursor,
        ):
            if cursor.rowcount != 1:
                raise ConflictError("Run is not available for this transition")

    async def finish(self, run_id: str, owner: str, result: ExecutionResult, *, status: str | None = None) -> None:
        """Persist an already-redacted result without overwriting a terminal run.

        Args:
            run_id: Receipt identifier.
            owner: Trusted server-derived ownership namespace.
            result: Bounded public execution result, not raw request inputs.
            status: Optional terminal state; defaults to succeeded or failed.

        Raises:
            ConflictError: The owned run is missing or already terminal.
            InputValidationError: State or serialized result exceeds the contract.
        """
        state = ("succeeded" if result.success else "failed") if status is None else status
        if state not in _TERMINAL or result.success != (state == "succeeded"):
            raise InputValidationError("Invalid terminal run status")
        try:
            payload = result.model_copy(update={"run_id": run_id}).model_dump_json()
            if len(payload.encode("utf-8")) > _MAX_RESULT_BYTES:
                raise InputValidationError("Run result exceeds storage limit")
        except (TypeError, ValueError, RecursionError):
            raise InputValidationError("Invalid run result") from None
        async with (
            self._transaction("finish run") as db,
            db.execute(
                """UPDATE runs SET status = ?, result = ?, updated_at = ?
                   WHERE id = ? AND owner = ? AND status IN ('queued', 'running')""",
                (state, payload, time.time(), run_id, owner),
            ) as cursor,
        ):
            if cursor.rowcount != 1:
                raise ConflictError("Run is not available for this transition")

    async def get(self, run_id: str, owner: str) -> RunRecord | None:
        """Fetch an unexpired receipt in the authenticated caller's namespace.

        Args:
            run_id: Receipt identifier.
            owner: Trusted server-derived ownership namespace.

        Returns:
            The receipt, or None for a missing, expired, or foreign run.
        """
        async with self._transaction("retrieve run") as db:
            await self._expire(db, time.time())
            async with db.execute("SELECT * FROM runs WHERE id = ? AND owner = ?", (run_id, owner)) as cursor:
                row = await cursor.fetchone()
            return self._record(row) if row is not None else None

    async def recover_interrupted(self) -> int:
        """Mark all abandoned active receipts interrupted, without automatic replay.

        Call exactly once at executor startup after claim_execution and before
        admitting work; one executor owns this database's recovery lifecycle.

        Returns:
            Number of queued or running receipts marked interrupted.
        """
        count = 0
        async with self._transaction("recover interrupted runs") as db:
            async with db.execute("SELECT id FROM runs WHERE status IN ('queued', 'running')") as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                result = ExecutionResult(
                    success=False,
                    run_id=row["id"],
                    error_type="interrupted",
                    error="Execution interrupted before a durable result was recorded",
                )
                await db.execute(
                    "UPDATE runs SET status = 'interrupted', result = ?, updated_at = ? WHERE id = ?",
                    (result.model_dump_json(), time.time(), row["id"]),
                )
                count += 1
        return count

    async def _expire(self, db: aiosqlite.Connection, now: float) -> None:
        """Expire only terminal receipts; retain in-flight idempotency claims."""
        await db.execute(
            "DELETE FROM runs WHERE status NOT IN ('queued', 'running') AND updated_at <= ?",
            (now - self._ttl_seconds,),
        )

    async def _trim(self, db: aiosqlite.Connection, target: int) -> None:
        """Remove oldest terminal receipts or reject admission when all are active."""
        async with db.execute("SELECT COUNT(*) FROM runs") as cursor:
            row = await cursor.fetchone()
        excess = int(row[0]) - target if row is not None else 0
        if excess > 0:
            async with db.execute(
                """DELETE FROM runs WHERE id IN (SELECT id FROM runs
                   WHERE status NOT IN ('queued', 'running') ORDER BY updated_at, rowid LIMIT ?)""",
                (excess,),
            ) as cursor:
                if cursor.rowcount < excess:
                    raise CacheError("Run ledger capacity is occupied by active runs")

    @staticmethod
    def _validate_identity(owner: str, request_hash: str, key: str | None) -> None:
        """Reject empty or unbounded identity metadata without reflecting it."""
        for value in (owner, request_hash, key):
            if value is not None and (not value or len(value) > _MAX_NAMESPACE_LENGTH):
                raise InputValidationError("Invalid run identity metadata")

    @staticmethod
    def _record(row: aiosqlite.Row) -> RunRecord:
        """Decode a persisted public receipt, sanitizing corrupt storage errors."""
        try:
            return RunRecord.model_validate(
                {
                    "id": row["id"],
                    "owner": row["owner"],
                    "request_hash": row["request_hash"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "result": ExecutionResult.model_validate_json(row["result"]) if row["result"] else None,
                }
            )
        except ValueError:
            raise CacheError("Invalid stored run receipt") from None
