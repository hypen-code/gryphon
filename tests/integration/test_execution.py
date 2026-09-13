"""Real Monty, SQLite receipts, recipe replay, and artifact integration tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gryphon.config import GryphonConfig
from gryphon.errors import CapacityError, ExecutionError
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.registry import Registry
from gryphon.runtime.runs import RunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from gryphon.models import ExecutionScope, RunRecord


@pytest.fixture
async def execution(tmp_path: Path) -> AsyncIterator[CodeExecutor]:
    """Use private on-disk stores and real Monty, with no remote API calls."""
    options: dict[str, Any] = {
        "_env_file": None,
        "compiled_output_dir": str(tmp_path / "compiled"),
        "cache_db_path": str(tmp_path / "cache.db"),
        "run_db_path": str(tmp_path / "runs.db"),
        "artifact_dir": str(tmp_path / "artifacts"),
        "execution_timeout_seconds": 1,
    }
    config = GryphonConfig(**options)
    cache = CacheStore(config.cache_db_path)
    registry = Registry(config.compiled_output_dir)
    registry.load()
    await cache.initialize()
    executor = CodeExecutor(config, cache, registry, broker=AsyncMock())
    await executor.startup()
    try:
        yield executor
    finally:
        await executor.shutdown()
        await cache.close()


async def test_real_vm_results_are_durable_and_owner_scoped(execution: CodeExecutor) -> None:
    result = await execution.execute("result = sum(inputs['values'])", "sum", {"values": [1, 2, 3]}, owner="alice")
    assert result.run_id is not None
    receipt = await execution.get_run(result.run_id, "alice")
    assert receipt is not None and receipt.result is not None
    assert receipt.status == "succeeded" and receipt.result.data == 6
    assert await execution.get_run(result.run_id, "bob") is None


async def test_recipe_replay_uses_explicit_new_inputs_not_source_rewriting(execution: CodeExecutor) -> None:
    source = "result = {'value': inputs['city'], 'literal': 'London'}"
    first = await execution.execute(source, "city", {"city": "London"})
    assert first.cache_id is not None
    second = await execution.replay(first.cache_id, {"city": "Paris"})
    assert second.success and second.data == {"value": "Paris", "literal": "London"}


async def test_recipe_replay_enforces_persisted_schema(execution: CodeExecutor) -> None:
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    first = await execution.execute("result = inputs['n'] + 1", "increment", {"n": 1}, schema)
    assert first.cache_id is not None
    invalid = await execution.replay(first.cache_id, {"n": "not-an-integer"})
    assert not invalid.success and invalid.error_type == "validation"


async def test_recipe_replay_runs_ast_guard_again(execution: CodeExecutor) -> None:
    first = await execution.execute("result = 42", "recipe")
    assert first.cache_id is not None
    with patch.object(execution._ast_guard, "validate", wraps=execution._ast_guard.validate) as guard:
        second = await execution.replay(first.cache_id)
    assert second.success and guard.call_count >= 1


async def test_artifact_overflow_preserves_full_json_and_bounds_inline_output(execution: CodeExecutor) -> None:
    execution._config.max_output_size_bytes = 1024
    original = {"rows": ["datum" * 20 for _ in range(30)]}
    result = await execution.execute("result = inputs", "large", original, owner="alice")
    assert result.success and result.truncated and len(result.model_dump_json().encode()) <= 1024
    assert result.artifact_id is not None
    page = await execution.artifacts.read(result.artifact_id, "alice", 0, 8192)
    assert json.loads(page["text"]) == original


async def test_real_vm_memory_limit_is_enforced(execution: CodeExecutor) -> None:
    execution._config.sandbox_memory_bytes = 1_000_000
    result = await execution.execute("result = 'x' * 10000000", "memory limit")
    assert not result.success and result.error_type == "capacity"


async def test_real_vm_cpu_limit_does_not_block_event_loop(execution: CodeExecutor) -> None:
    ticking = asyncio.Event()

    async def ticker() -> None:
        """Verify the native VM does not execute on the host event-loop thread."""
        await asyncio.sleep(0.05)
        ticking.set()

    timer = asyncio.create_task(ticker())
    result = await execution.execute("while True:\n    pass\nresult = 1", "CPU limit")
    await timer
    assert ticking.is_set() and not result.success and result.error_type == "timeout"


async def test_real_vm_print_producer_limit_is_enforced(execution: CodeExecutor) -> None:
    execution._config.max_output_size_bytes = 1024
    result = await execution.execute("print('x' * 2000)\nresult = 1", "stdout bound")
    assert not result.success and result.error_type == "capacity"


@pytest.mark.parametrize("source", ["result = {1, 2}", "result = (1, 2)", "result = float('nan')"])
async def test_real_vm_non_json_results_fail_without_caching(execution: CodeExecutor, source: str) -> None:
    result = await execution.execute(source, "non JSON")
    assert not result.success and result.cache_id is None and result.error_type == "validation"


async def test_broker_budget_all_routes_are_counted(execution: CodeExecutor) -> None:
    execution._config.max_tool_calls = 2
    cast("AsyncMock", execution._broker.invoke).return_value = {"ok": True}
    source = 'for i in range(3):\n    await call_tool("weather.get", {})\nresult = 1'
    with patch.object(execution._registry, "get_function"):
        result = await execution.execute(source, "budget")
    assert not result.success and result.error_type == "capacity" and result.tool_calls == 2
    assert cast("AsyncMock", execution._broker.invoke).await_count == 2


async def test_broker_failure_caught_by_code_cannot_be_reclassified_success(execution: CodeExecutor) -> None:
    cast("AsyncMock", execution._broker.invoke).side_effect = CapacityError("private diagnostic")
    source = 'try:\n    await call_tool("weather.get", {})\nexcept Exception:\n    pass\nresult = 1'
    with patch.object(execution._registry, "get_function"):
        result = await execution.execute(source, "fail closed")
    assert (
        not result.success and result.error_type == "capacity" and "private diagnostic" not in result.model_dump_json()
    )


async def test_cancellation_revokes_and_awaits_inflight_broker(execution: CodeExecutor) -> None:
    """Repeated caller cancellation cannot release an active broker grant or VM."""
    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    authority: list[ExecutionScope] = []

    async def wait(server: str, function: str, arguments: dict[str, Any], scope: ExecutionScope) -> None:
        """Observe revocation before cancellation enters broker cleanup."""
        authority.append(scope)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert scope.cancelled
            cleaning.set()
            await release.wait()
            finished.set()

    cast("AsyncMock", execution._broker.invoke).side_effect = wait
    with patch.object(execution._registry, "get_function"):
        record = await execution.submit('result = await call_tool("weather.get", {})', "cancel", owner="alice")
        await asyncio.wait_for(entered.wait(), 2)
        cancel = asyncio.create_task(execution.cancel(record.id, "alice"))
        await asyncio.wait_for(cleaning.wait(), 2)
        cancel.cancel()
        await asyncio.sleep(0)
        cancel.cancel()
        await asyncio.sleep(0)
        assert not cancel.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancel
    receipt = await execution.get_run(record.id, "alice")
    assert receipt is not None
    assert authority[0].cancelled and finished.is_set() and receipt.status == "cancelled"


async def test_idempotency_persists_across_clean_executor_restart(execution: CodeExecutor) -> None:
    first = await execution.execute("result = inputs", "stable", {"n": 1}, idempotency_key="stable")
    await execution.shutdown()
    fresh = CodeExecutor(execution._config, execution._cache, execution._registry, broker=AsyncMock())
    await fresh.startup()
    try:
        second = await fresh.execute("result = inputs", "stable", {"n": 1}, idempotency_key="stable")
        assert second.run_id == first.run_id and second.data == first.data
    finally:
        await fresh.shutdown()


async def test_second_executor_cannot_interrupt_an_active_ledger(
    execution: CodeExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def wait(*args: Any) -> None:
        """Keep the first run active while another process attempts recovery."""
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(execution._sandbox, "run", AsyncMock(side_effect=wait))
    record = await execution.submit("result = 42", "lease owner")
    await entered.wait()
    second = CodeExecutor(execution._config, execution._cache, execution._registry, broker=AsyncMock())
    try:
        await second.startup()
        # A concurrent executor serves without owning recovery, and stale-scoped recovery
        # never interrupts the first process's live run.
        assert second._owns_recovery is False
        receipt = await execution.get_run(record.id)
        assert receipt is not None and receipt.status == "running"
    finally:
        await second.shutdown()
        await execution.cancel(record.id)


async def test_startup_is_idempotent_but_stopped_executor_cannot_restart(execution: CodeExecutor) -> None:
    with patch.object(execution._runs, "recover_interrupted", wraps=execution._runs.recover_interrupted) as recover:
        await execution.startup()
        recover.assert_not_awaited()
    await execution.shutdown()
    with pytest.raises(ExecutionError, match="cannot be restarted"):
        await execution.startup()


async def test_cancel_before_first_job_step_persists_cancelled_receipt(execution: CodeExecutor) -> None:
    record = await execution.submit("result = 42", "cancel before scheduling")
    job = execution._jobs[record.id]
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)
    assert await execution.cancel(record.id)
    receipt = await execution.get_run(record.id)
    assert receipt is not None and receipt.status == "cancelled"


async def test_queued_catalog_change_fails_without_executing(execution: CodeExecutor) -> None:
    record = await execution.submit("result = 42", "catalog snapshot")
    job = execution._jobs[record.id]
    with patch.object(execution._registry, "fingerprint", return_value="changed"):
        result = await job
    assert not result.success and result.error_type == "conflict"


async def test_submission_snapshots_nested_input_data(execution: CodeExecutor) -> None:
    values = {"nested": [1, 2]}
    record = await execution.submit("result = inputs", "immutable input", values)
    values["nested"][0] = 99
    result = await execution._jobs[record.id]
    assert result.data == {"nested": [1, 2]}


async def test_foreign_owner_cannot_replay_recipe(execution: CodeExecutor) -> None:
    first = await execution.execute("result = 42", "owned recipe", owner="alice")
    assert first.cache_id is not None
    result = await execution.replay(first.cache_id, owner="bob")
    assert not result.success and result.error_type == "cache"


async def test_broker_owned_counter_is_not_double_incremented(execution: CodeExecutor) -> None:
    execution._config.max_tool_calls = 2

    async def counted(server: str, function: str, arguments: dict[str, Any], scope: ExecutionScope) -> int:
        """Model the production broker's own atomic call counter."""
        scope.calls += 1
        return scope.calls

    cast("AsyncMock", execution._broker.invoke).side_effect = counted
    source = 'await call_tool("weather.get", {})\nresult = await call_tool("weather.get", {})'
    with patch.object(execution._registry, "get_function"):
        result = await execution.execute(source, "single counter")
    assert result.success and result.data == 2 and result.tool_calls == 2


async def test_execute_caller_cancellation_revokes_its_run(execution: CodeExecutor) -> None:
    entered = asyncio.Event()

    async def wait(*args: Any) -> None:
        """Hold a mocked capability until cancellation is delivered."""
        entered.set()
        await asyncio.Event().wait()

    cast("AsyncMock", execution._broker.invoke).side_effect = wait
    with patch.object(execution._registry, "get_function"):
        caller = asyncio.create_task(execution.execute('result = await call_tool("weather.get", {})', "caller"))
        await entered.wait()
        run_id = next(iter(execution._jobs))
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    receipt = await execution.get_run(run_id)
    assert receipt is not None and receipt.status == "cancelled" and not execution._jobs


async def test_startup_marks_abandoned_receipts_interrupted_without_reexecution(execution: CodeExecutor) -> None:
    await execution.shutdown()
    runs = RunStore(execution._config.run_db_path)
    await runs.initialize()
    queued, _ = await runs.create("alice", "abandoned")
    await runs.start(queued.id, "alice")
    await runs.close()
    # Age the receipt so stale-scoped recovery treats it as abandoned rather than a live peer.
    connection = sqlite3.connect(execution._config.run_db_path)
    try:
        connection.execute("UPDATE runs SET updated_at = 0 WHERE id = ?", (queued.id,))
        connection.commit()
    finally:
        connection.close()
    fresh = CodeExecutor(execution._config, execution._cache, execution._registry, broker=AsyncMock())
    await fresh.startup()
    try:
        receipt = await fresh.get_run(queued.id, "alice")
        assert receipt is not None and receipt.status == "interrupted" and not fresh._jobs
        cast("AsyncMock", fresh._broker.invoke).assert_not_awaited()
    finally:
        await fresh.shutdown()


async def test_recovery_lease_excludes_a_separate_process(execution: CodeExecutor) -> None:
    """Verify flock exclusion independently of Python process-local executor state."""
    script = (
        "import sys\nfrom gryphon.runtime.recovery_lease import RecoveryLease\n"
        "from gryphon.errors import CacheError\n"
        "try:\n    RecoveryLease(sys.argv[1]).acquire()\n"
        "except CacheError:\n    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, execution._config.run_db_path)
    assert await asyncio.wait_for(process.wait(), 10) == 0


async def test_result_envelope_counts_metadata_not_only_data(execution: CodeExecutor) -> None:
    """A data payload below the full limit still fails if its envelope exceeds it."""
    execution._config.max_response_size_bytes = 1024
    result = await execution.execute("result = 'x' * 1000", "full envelope")
    assert not result.success and result.error_type == "validation" and result.cache_id is None
    assert len(result.model_dump_json().encode()) <= 1024


async def test_returned_result_mutation_cannot_change_terminal_receipt(execution: CodeExecutor) -> None:
    """Caller mutations and cancellation never rewrite an immutable final record."""
    first = await execution.execute("result = {'rows': [1]}", "detached", idempotency_key="immutable")
    assert isinstance(first.data, dict) and isinstance(first.data["rows"], list)
    first.data["rows"].append(2)
    assert first.run_id is not None
    assert not await execution.cancel(first.run_id)
    replay = await execution.execute("result = {'rows': [1]}", "detached", idempotency_key="immutable")
    assert replay.data == {"rows": [1]}


async def test_inflight_idempotent_duplicate_awaits_single_execution(execution: CodeExecutor) -> None:
    """Concurrent matching requests await the same job instead of rerunning or failing admission."""
    entered, release, retrieved = (asyncio.Event() for _ in range(3))
    original_get = execution._runs.get

    async def wait(*args: Any) -> int:
        """Hold a capability while another caller submits the matching request."""
        entered.set()
        await release.wait()
        return 42

    async def observe(run_id: str, owner: str) -> RunRecord | None:
        """Signal that the duplicate has looked up the active durable receipt."""
        record = await original_get(run_id, owner)
        retrieved.set()
        return record

    cast("AsyncMock", execution._broker.invoke).side_effect = wait
    source = 'result = await call_tool("weather.get", {})'
    callers = []
    with patch.object(execution._registry, "get_function"):
        try:
            callers.append(asyncio.create_task(execution.execute(source, "same", idempotency_key="same")))
            await asyncio.wait_for(entered.wait(), 2)
            with patch.object(execution._runs, "get", side_effect=observe):
                callers.append(asyncio.create_task(execution.execute(source, "same", idempotency_key="same")))
                await asyncio.wait_for(retrieved.wait(), 2)
                assert not callers[1].done()
                release.set()
                first, second = await asyncio.wait_for(asyncio.gather(*callers), 2)
            assert first.success and second.success and first.data == second.data == 42
            assert first.run_id == second.run_id
            cast("AsyncMock", execution._broker.invoke).assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(*callers, return_exceptions=True)
