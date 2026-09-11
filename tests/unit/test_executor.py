"""Gryphon v2 executor contract tests: no Docker, network, or shared storage."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gryphon.config import GryphonConfig
from gryphon.errors import CacheError, CapacityError, ConflictError, InputValidationError, SecurityViolationError
from gryphon.models import ExecutionResult, RunRecord
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.recovery_lease import RecoveryLease

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(tmp_path: Path, **options: Any) -> CodeExecutor:
    """Construct detached mocked services with real owner-scoped receipt semantics."""
    options = {
        "_env_file": None,
        "run_db_path": str(tmp_path / "runs.db"),
        "cache_db_path": str(tmp_path / "cache.db"),
        "artifact_dir": str(tmp_path / "artifacts"),
        "compiled_output_dir": str(tmp_path / "compiled"),
        **options,
    }
    config = GryphonConfig(**options)
    cache, broker, registry = AsyncMock(), AsyncMock(), MagicMock()
    cache.store.return_value = "cache-id"
    registry.fingerprint.return_value = "catalog-v2"
    registry.list_servers.return_value = []
    return CodeExecutor(config, cache, registry, broker, _mock_runs())


def _mock_runs() -> AsyncMock:
    """Preserve detached receipt and idempotency semantics in a mocked ledger."""
    runs = AsyncMock()
    records: dict[str, RunRecord] = {}
    keys: dict[tuple[str, str], str] = {}

    async def create(owner: str, digest: str, key: str | None) -> tuple[RunRecord, bool]:
        """Model immutable namespace-scoped durable idempotency."""
        if key is not None and (owner, key) in keys:
            record = records[keys[owner, key]]
            if record.request_hash != digest:
                raise ConflictError("different request")
            return record.model_copy(deep=True), False
        now = time.time()
        record = RunRecord(id=uuid.uuid4().hex, owner=owner, request_hash=digest, created_at=now, updated_at=now)
        records[record.id] = record
        if key is not None:
            keys[owner, key] = record.id
        return record.model_copy(deep=True), True

    async def get(run_id: str, owner: str) -> RunRecord | None:
        """Return detached, owner-filtered state."""
        record = records.get(run_id)
        return record.model_copy(deep=True) if record is not None and record.owner == owner else None

    async def start(run_id: str, owner: str) -> None:
        """Mark an admitted run running."""
        records[run_id].status = "running"

    async def finish(run_id: str, owner: str, result: ExecutionResult, status: str | None = None) -> None:
        """Capture an immutable public terminal receipt."""
        if records[run_id].status not in ("queued", "running"):
            raise ConflictError("already terminal")
        updated = records[run_id].model_dump()
        updated.update(status=status or ("succeeded" if result.success else "failed"), result=result.model_dump())
        records[run_id] = RunRecord.model_validate(updated)

    runs.create.side_effect, runs.get.side_effect = create, get
    runs.start.side_effect, runs.finish.side_effect = start, finish
    return runs


# ---------------------------------------------------------------------------
# CodeExecutor.__init__ and startup / shutdown
# ---------------------------------------------------------------------------


async def test_startup_restricted_never_opens_docker(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    with patch("aiodocker.Docker") as docker:
        await executor.startup()
        await executor.shutdown()
    docker.assert_not_called()
    cast("AsyncMock", executor._runs.recover_interrupted).assert_awaited_once()


async def test_shutdown_safe_when_startup_not_called(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.shutdown()
    cast("AsyncMock", executor._broker.close).assert_awaited_once()


async def test_execute_before_startup_returns_typed_failure(tmp_path: Path) -> None:
    result = await _make_executor(tmp_path).execute("result = 1", "test")
    assert not result.success and result.error_type == "execution"


# ---------------------------------------------------------------------------
# Validation and source preparation (replaces unsafe lint/import rewrites)
# ---------------------------------------------------------------------------


async def test_oversized_code_rejected_before_ast_or_admission(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path, max_code_size_bytes=8)
    await executor.startup()
    with patch.object(executor._ast_guard, "validate") as guard, pytest.raises(InputValidationError):
        await executor.submit("result = 12345", "large")
    guard.assert_not_called()
    cast("AsyncMock", executor._runs.create).assert_not_awaited()
    await executor.shutdown()


@pytest.mark.parametrize("inputs", [{"x": float("nan")}, {1: "key"}, {"x": object()}, {"x": (1, 2)}])
async def test_non_json_inputs_rejected_before_vm(tmp_path: Path, inputs: Any) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    with pytest.raises(InputValidationError):
        await executor.submit("result = inputs", "invalid", inputs)
    cast("AsyncMock", executor._runs.create).assert_not_awaited()
    await executor.shutdown()


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"pattern": "(a+)+$"},
        {"type": "object", "required": ["city"]},
    ],
)
async def test_invalid_input_schema_fails_before_admission(tmp_path: Path, schema: dict[str, Any]) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    with pytest.raises(InputValidationError):
        await executor.submit("result = 1", "invalid", {}, schema)
    cast("AsyncMock", executor._runs.create).assert_not_awaited()
    await executor.shutdown()


@pytest.mark.parametrize("code", ["import os\nresult = 1", "import json\nresult = 1", "result = open('x')"])
async def test_guard_always_blocks_unsafe_or_unavailable_capabilities(tmp_path: Path, code: str) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    with pytest.raises(SecurityViolationError):
        await executor.submit(code, "guard")
    cast("AsyncMock", executor._runs.create).assert_not_awaited()
    await executor.shutdown()


@pytest.mark.parametrize("code", ["result = inputs['n'] + 2", "inputs['n'] + 2", "return inputs['n'] + 2"])
async def test_real_monty_supports_explicit_result_contracts(tmp_path: Path, code: str) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    result = await executor.execute(code, "arithmetic", {"n": 40})
    assert result.success and result.data == 42
    await executor.shutdown()


async def test_main_is_not_automatically_invoked_twice(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    cast("AsyncMock", executor._broker.invoke).return_value = {"ok": True}
    await executor.startup()
    code = 'async def main():\n    return await call_tool("weather.get", {})\nresult = await main()'
    result = await executor.execute(code, "once")
    assert result.success
    cast("AsyncMock", executor._broker.invoke).assert_awaited_once()
    await executor.shutdown()


async def test_main_definition_without_explicit_call_rejected(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    with pytest.raises(InputValidationError, match="call main explicitly"):
        await executor.submit("def main():\n    return 42", "no implicit call")
    await executor.shutdown()


# ---------------------------------------------------------------------------
# Output parsing — strict JSON, safe diagnostics, success-only caching
# ---------------------------------------------------------------------------


async def test_exception_values_and_tracebacks_never_escape(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path, debug=True)
    await executor.startup()
    result = await executor.execute("result = 1 / 0", "private-description")
    assert not result.success and result.error_type == "execution" and result.traceback is None
    cast("AsyncMock", executor._cache.store).assert_not_awaited()
    await executor.shutdown()


async def test_printed_values_are_not_returned_as_diagnostics(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    result = await executor.execute("print(inputs['private'])\nresult = 42", "private", {"private": "never-echo-this"})
    assert result.success and "never-echo-this" not in result.model_dump_json()
    await executor.shutdown()


async def test_cache_disabled_never_stores_recipe(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path, cache_enabled=False)
    await executor.startup()
    result = await executor.execute("result = 42", "no cache")
    assert result.success and result.cache_id is None
    cast("AsyncMock", executor._cache.store).assert_not_awaited()
    await executor.shutdown()


# ---------------------------------------------------------------------------
# Immutable owner-scoped idempotency, bounded admission, and cancellation
# ---------------------------------------------------------------------------


async def test_same_idempotency_key_returns_one_execution(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    first = await executor.execute("result = inputs", "same", {"n": 1}, idempotency_key="one")
    second = await executor.execute("result = inputs", "same", {"n": 1}, idempotency_key="one")
    assert first.run_id == second.run_id
    cast("AsyncMock", executor._cache.store).assert_awaited_once()
    await executor.shutdown()


async def test_changed_idempotency_inputs_conflict(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    await executor.execute("result = inputs", "same", {"n": 1}, idempotency_key="one")
    result = await executor.execute("result = inputs", "same", {"n": 2}, idempotency_key="one")
    assert result.error_type == "conflict"
    await executor.shutdown()


async def test_capacity_rejects_without_spawning_unbounded_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(tmp_path, max_concurrent_executions=1)
    await executor.startup()
    blocked = asyncio.Event()

    async def wait(*args: Any) -> ExecutionResult:
        """Keep the single execution slot occupied until shutdown."""
        await blocked.wait()
        return ExecutionResult(success=True, data=1)

    monkeypatch.setattr(executor._sandbox, "run", AsyncMock(side_effect=wait))
    await executor.submit("result = 1", "running")
    await executor.submit("result = 1", "queued")
    with pytest.raises(CapacityError):
        await executor.submit("result = 1", "rejected")
    assert len(executor._jobs) == 2
    await executor.shutdown()


async def test_foreign_owner_cannot_cancel_or_read_run(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    result = await executor.execute("result = 1", "owned", owner="alice")
    assert result.run_id is not None
    assert await executor.get_run(result.run_id, "bob") is None
    assert not await executor.cancel(result.run_id, "bob")
    await executor.shutdown()


async def test_replay_stale_fingerprint_fails_closed(tmp_path: Path) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    cast("AsyncMock", executor._cache.get).return_value = MagicMock(swagger_hash="old")
    result = await executor.replay("old")
    assert not result.success and result.error_type == "conflict"
    cast("AsyncMock", executor._runs.create).assert_not_awaited()
    await executor.shutdown()


async def test_shutdown_caller_cancellation_still_closes_all_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close() -> None:
        """Hold backend cleanup open long enough to repeatedly cancel its caller."""
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(executor._sandbox, "close", AsyncMock(side_effect=close))
    shutdown = asyncio.create_task(executor.shutdown())
    await entered.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not finished.is_set() and not shutdown.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert finished.is_set()
    await executor.shutdown()
    cast("AsyncMock", executor._broker.close).assert_awaited_once()
    cast("AsyncMock", executor._runs.close).assert_awaited_once()


async def test_shutdown_backend_failure_still_closes_broker_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _make_executor(tmp_path)
    await executor.startup()
    monkeypatch.setattr(executor._sandbox, "close", AsyncMock(side_effect=RuntimeError("private cleanup error")))
    with pytest.raises(RuntimeError):
        await executor.shutdown()
    cast("AsyncMock", executor._broker.close).assert_awaited_once()
    cast("AsyncMock", executor._runs.close).assert_awaited_once()


async def test_queue_timeout_has_durable_capacity_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _make_executor(tmp_path, max_concurrent_executions=1, queue_timeout_seconds=1)
    await executor.startup()
    entered = asyncio.Event()

    async def wait(*args: Any) -> ExecutionResult:
        """Hold the only active slot until shutdown cancels it."""
        entered.set()
        await asyncio.Event().wait()
        return ExecutionResult(success=True, data=1)

    monkeypatch.setattr(executor._sandbox, "run", AsyncMock(side_effect=wait))
    await executor.submit("result = 1", "running")
    await entered.wait()
    queued = await executor.execute("result = 2", "queued")
    assert queued.run_id is not None
    receipt = await executor.get_run(queued.run_id)
    assert receipt is not None
    assert queued.error_type == "capacity" and receipt.status == "failed"
    await executor.shutdown()


@pytest.mark.parametrize("kind", ["symlink", "directory", "public", "hardlink"])
def test_lease_rejects_unsafe_files_without_modifying_them(tmp_path: Path, kind: str) -> None:
    """Fail closed before storage recovery without altering somebody else's files."""
    lock, target = tmp_path / "runs.db.lock", tmp_path / "target"
    target.write_text("untouched")
    target.chmod(0o600)
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir()
    elif kind == "hardlink":
        lock.hardlink_to(target)
    else:
        lock.write_text("public")
        lock.chmod(0o644)
    lease = RecoveryLease(str(tmp_path / "runs.db"))
    with pytest.raises(CacheError):
        lease.acquire()
    lease.close()
    assert target.read_text() == "untouched" and lock.exists()


def test_lease_reuses_private_inode_and_releases_idempotently(tmp_path: Path) -> None:
    """A second open cannot acquire until the first closes; never unlink locks."""
    first = RecoveryLease(str(tmp_path / "runs.db"))
    second = RecoveryLease(str(tmp_path / "runs.db"))
    first.acquire()
    first.acquire()
    lock = tmp_path / "runs.db.lock"
    inode = lock.stat().st_ino
    assert lock.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CacheError):
        second.acquire()
    first.close()
    first.close()
    second.acquire()
    second.close()
    assert lock.stat().st_ino == inode
