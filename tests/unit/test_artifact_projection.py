"""Offline artifact projections reuse bounded admission without broker or recipe grants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from gryphon.errors import CacheError, ExecutionError, InputValidationError, SecurityViolationError
from gryphon.models import ExecutionScope
from gryphon.models.artifacts import artifact_shape
from gryphon.runtime.artifacts import ArtifactStore
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.registry import Registry
from gryphon.runtime.sandboxes import RestrictedSandbox
from gryphon.saas_store import TOOLS
from gryphon.saas_traffic import TrafficObservation
from gryphon.server import create_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from gryphon.config import GryphonConfig


@pytest.fixture
async def executor(gryphon_config: GryphonConfig) -> AsyncIterator[CodeExecutor]:
    """Use real isolated SQLite receipts, artifact storage, and fresh Monty VMs."""
    cache = CacheStore(gryphon_config.cache_db_path)
    await cache.initialize()
    runtime = CodeExecutor(gryphon_config, cache, Registry(gryphon_config.compiled_output_dir), AsyncMock())
    await runtime.startup()
    try:
        yield runtime
    finally:
        await runtime.shutdown()
        await cache.close()


async def test_projection_large_json_reduces_without_broker_or_chunks(executor: CodeExecutor) -> None:
    """Project field lists and numeric aggregates from 165 KB without a second API call."""
    data = {"query": "catalog", "products": [{"name": f"p{i}", "price": i, "extra": "x" * 500} for i in range(310)]}
    artifact = await executor.artifacts.put(data, "alice")
    assert 165_000 <= artifact.size_bytes <= 170_000
    code = (
        "rows = inputs['artifact']['products']\n"
        "result = {'query': inputs['artifact']['query'], 'count': len(rows), "
        "'sum': sum([r['price'] for r in rows]), "
        "'names': [r['name'] for r in rows[:inputs['params']['limit']]]}"
    )
    with (
        patch.object(executor._broker, "invoke", new_callable=AsyncMock) as broker,
        patch.object(executor.artifacts, "read", new_callable=AsyncMock) as read,
    ):
        result = await executor.transform_artifact(artifact.id, code, "Summarize stored catalog", {"limit": 2}, "alice")
    assert result.success and result.data == {"query": "catalog", "count": 310, "sum": 47895, "names": ["p0", "p1"]}
    assert result.cache_id is None and result.tool_calls == 0 and result.run_id is not None
    assert (await executor._cache.search(owner="alice")) == []
    receipt = await executor.get_run(result.run_id, "alice")
    assert receipt is not None and receipt.result == result and receipt.status == "succeeded"
    assert await executor.get_run(result.run_id, "bob") is None
    broker.assert_not_awaited()
    read.assert_not_awaited()


@pytest.mark.parametrize(
    "code",
    [
        "result = await call_tool('a.b', {})",
        "alias = call_tool\nresult = await alias('a.b', {})",
        "box = [call_tool]\nresult = await box[0]('a.b', {})",
        "import json\nresult = 1",
        "from os import environ\nresult = 1",
        "result = open('file')",
        "result = __import__('socket')",
    ],
)
async def test_projection_denies_capability_and_imports_before_loading(executor: CodeExecutor, code: str) -> None:
    """Static checks block capability references even when hidden behind aliases."""
    with patch.object(executor.artifacts, "load_json", new_callable=AsyncMock) as load:
        result = await executor.transform_artifact("a" * 64, code, "blocked")
    assert not result.success and result.error_type == "security" and result.run_id is None
    load.assert_not_awaited()


async def test_projection_vm_has_no_external_callback_even_without_ast(executor: CodeExecutor) -> None:
    """VM isolation itself denies hidden call_tool aliases, independently of static defense."""
    sandbox = RestrictedSandbox(executor._config, executor._registry, None)
    scope = ExecutionScope(run_id="offline", owner="local", deadline=time.monotonic() + 2)
    with patch.object(executor._broker, "invoke", new_callable=AsyncMock) as broker, pytest.raises(ExecutionError):
        await sandbox.run("alias = call_tool\nawait alias('a.b', {})", {}, scope)
    broker.assert_not_awaited()
    assert scope.calls == 0
    # Even direct invocation of an accidentally retained callback denies before lookup.
    state: dict[str, Any] = {"calls": 0, "failure": None}
    callback = sandbox._capability(scope, set(), state)
    with patch.object(executor._registry, "get_function") as lookup, pytest.raises(RuntimeError):
        await callback("a.b", {})
    assert isinstance(state["failure"], SecurityViolationError)
    lookup.assert_not_called()


@pytest.mark.parametrize("case", ["code", "inputs", "nan", "id", "description", "docker"])
async def test_projection_validates_all_boundaries_before_load(executor: CodeExecutor, case: str) -> None:
    """No filesystem read occurs before code, JSON inputs, ID and profile validation."""
    code, description, artifact_id = "result = 1", "projection", "a" * 64
    inputs: dict[str, Any] = {}
    if case == "code":
        code = "x" * (executor._config.max_code_size_bytes + 1)
    elif case == "inputs":
        inputs = {"value": "x" * executor._config.max_response_size_bytes}
    elif case == "nan":
        inputs = {"value": float("nan")}
    elif case == "id":
        artifact_id = "../outside"
    elif case == "description":
        description = "x" * 4097
    else:
        executor._config.sandbox_mode = "docker"
    with patch.object(executor.artifacts, "load_json", new_callable=AsyncMock) as load:
        result = await executor.transform_artifact(artifact_id, code, description, inputs)
    assert not result.success and result.run_id is None
    assert result.error_type == ("security" if case == "docker" else "validation")
    load.assert_not_awaited()


async def test_projection_oversized_output_retains_owned_artifact_not_recipe(executor: CodeExecutor) -> None:
    """The normal producer/receipt bounds apply to projection output too."""
    artifact = await executor.artifacts.put({"big": "x" * 170_000}, "alice")
    result = await executor.transform_artifact(artifact.id, "result = inputs['artifact']", "identity", owner="alice")
    assert result.success and result.truncated and result.artifact_id == artifact.id and result.cache_id is None
    too_big = await executor.transform_artifact(
        artifact.id,
        "result = 'x' * inputs['params']['size']",
        "overflow",
        {"size": executor._config.max_response_size_bytes + 1},
        "alice",
    )
    assert not too_big.success and too_big.error_type == "validation"


async def test_projection_shared_slots_gate_loading_and_cancellation(executor: CodeExecutor) -> None:
    """Waiting runs cannot read artifacts; cancellation retains normal owner-scoped receipts."""
    executor._execution_slots = asyncio.Semaphore(0)
    with patch.object(executor.artifacts, "load_json", new_callable=AsyncMock) as load:
        task = asyncio.create_task(executor.transform_artifact("a" * 64, "result = 1", "queued", owner="alice"))
        while not executor._jobs:
            await asyncio.sleep(0.001)
        run_id = next(iter(executor._jobs))
        assert not await executor.cancel(run_id, "bob")
        load.assert_not_awaited()
        assert await executor.cancel(run_id, "alice")
        with pytest.raises(asyncio.CancelledError):
            await task
    record = await executor.get_run(run_id, "alice")
    assert record is not None and record.status == "cancelled"
    assert not executor._jobs and not executor._projections


async def test_projection_admission_capacity_and_shutdown(executor: CodeExecutor) -> None:
    """Projections share local queue bounds and shutdown drains all admitted jobs."""
    executor._config.max_concurrent_executions = 1
    executor._execution_slots = asyncio.Semaphore(0)
    tasks = [asyncio.create_task(executor.transform_artifact("a" * 64, "result = 1", "queued")) for _ in range(2)]
    while len(executor._jobs) < 2:
        await asyncio.sleep(0.001)
    overflow = await executor.transform_artifact("a" * 64, "result = 1", "overflow")
    assert not overflow.success and overflow.error_type == "capacity"
    await executor.shutdown()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not executor._jobs and not executor._projections
    stopped = await executor.transform_artifact("a" * 64, "result = 1", "stopped")
    assert stopped.error_type == "execution"


async def test_projection_execution_timeout_is_durable(executor: CodeExecutor) -> None:
    """Unbounded computation is stopped by the normal VM and executor deadline."""
    executor._config.execution_timeout_seconds = 1
    artifact = await executor.artifacts.put({}, "local")
    result = await executor.transform_artifact(artifact.id, "while True:\n    pass\nresult = 1", "timeout")
    assert not result.success and result.error_type == "timeout" and result.run_id is not None
    record = await executor.get_run(result.run_id)
    assert record is not None and record.status == "failed"


@pytest.mark.parametrize(
    "case", ["foreign", "missing", "traversal", "symlink", "tamper", "size", "corrupt", "index_tamper", "cap"]
)
async def test_projection_load_json_owned_bounded_integrity(tmp_path: Path, case: str) -> None:
    """Private indexed loading rejects unsafe files and even hash-matching invalid JSON."""
    store = ArtifactStore(str(tmp_path))
    artifact = await store.put({"v": 1}, "alice")
    directory = tmp_path / ".gryphon-artifacts-v1" / hashlib.sha256(b"alice").hexdigest()
    path = directory / f"{artifact.id}.json"
    owner, artifact_id, cap = "alice", artifact.id, 1024
    error: type[Exception] = CacheError
    if case == "foreign":
        owner = "bob"
    elif case == "missing":
        artifact_id = "a" * 64
    elif case == "traversal":
        artifact_id, error = "../outside", InputValidationError
    elif case == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "outside")
    elif case == "tamper":
        path.write_text('{"v":2}')
    elif case == "size":
        path.write_text("{}")
    elif case in {"corrupt", "index_tamper"}:
        payload = b"invalid" if case == "corrupt" else b'{"v":2}'
        digest = hashlib.sha256(payload).hexdigest()
        path.write_bytes(payload)
        if case == "corrupt":
            artifact_id = hashlib.sha256(f"{directory.name}:{digest}".encode()).hexdigest()
            path.rename(directory / f"{artifact_id}.json")
        (directory / "index.json").write_text(json.dumps({artifact_id: [digest, len(payload)]}))
    else:
        cap = 1
    with pytest.raises(error):
        await store.load_json(artifact_id, owner, cap)


@pytest.mark.parametrize("cap", [0, -1, True, 16 * 1024 * 1024 + 1])
async def test_projection_load_rejects_invalid_caps(tmp_path: Path, cap: int) -> None:
    """Internal callers cannot widen the immutable 16 MiB storage ceiling."""
    with pytest.raises(InputValidationError):
        await ArtifactStore(str(tmp_path)).load_json("a" * 64, "alice", cap)


@pytest.mark.parametrize(
    "value,kind", [(None, "null"), (True, "boolean"), (1, "number"), (1.5, "number"), ("hidden", "string")]
)
def test_projection_shape_scalar_never_previews_values(value: Any, kind: str) -> None:
    """Scalar metadata identifies JSON type only."""
    assert artifact_shape(value) == {"json_type": kind}


def test_projection_shape_reports_complete_keys_and_explicit_omissions() -> None:
    """Never silently shorten key names or expose artifact values in metadata."""
    assert artifact_shape([1, 2]) == {"json_type": "array", "length": 2}
    assert artifact_shape({"query": "hidden", "products": []}) == {
        "json_type": "object",
        "top_level_keys": ["query", "products"],
        "key_count": 2,
        "keys_truncated": False,
    }
    shape = artifact_shape({str(i): "hidden" for i in range(100)})
    assert shape["key_count"] == 100 and len(shape["top_level_keys"]) == 32 and shape["keys_truncated"]
    shape = artifact_shape({"x" * 5000: "hidden"})
    assert shape["top_level_keys"] == [] and shape["key_count"] == 1 and shape["keys_truncated"]
    assert len(json.dumps(shape).encode()) < 1024 and "hidden" not in json.dumps(shape)
    shape = artifact_shape({str(i) + "é" * 50: None for i in range(10)})
    assert len(json.dumps(shape["top_level_keys"], separators=(",", ":")).encode()) <= 512
    assert shape["keys_truncated"] and shape["key_count"] == 10


async def test_projection_mcp_schema_owner_and_native_result(executor: CodeExecutor) -> None:
    """The eleventh core tool accepts only projection data and forwards verified authority."""
    artifact = await executor.artifacts.put({"query": "stored"}, "verified")
    server = create_server(executor._config, executor._registry, executor._cache, executor)
    with patch(
        "gryphon.runtime.context.get_access_token",
        return_value=AccessToken(token="test", client_id="verified", scopes=[]),
    ):
        async with Client(server) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert len(tools) == 11
            projection = tools["transform_artifact"]
            assert set(projection.input_schema["properties"]) == {"artifact_id", "code", "description", "inputs"}
            assert projection.annotations is not None and not projection.annotations.read_only_hint
            response = await client.call_tool(
                "transform_artifact",
                {
                    "artifact_id": artifact.id,
                    "code": "result = inputs['artifact']['query']",
                    "description": "Read query",
                    "inputs": {"owner": "forged"},
                },
            )
            assert not response.is_error and response.structured_content is not None
            assert response.structured_content["success"] and response.structured_content["data"] == "stored"
            assert response.structured_content.get("cache_id") is None and "next" not in response.structured_content
            rejected = await client.call_tool(
                "transform_artifact",
                {"artifact_id": artifact.id, "code": "result = 1", "description": "Forged", "owner": "verified"},
                raise_on_error=False,
            )
            assert rejected.is_error


async def test_projection_waiting_for_local_slot_times_out_without_load(executor: CodeExecutor) -> None:
    """Local capacity and queue deadlines also guard disk and JSON parsing work."""
    executor._semaphore = asyncio.Semaphore(0)
    executor._config.queue_timeout_seconds = 1
    with patch.object(executor.artifacts, "load_json", new_callable=AsyncMock) as load:
        result = await executor.transform_artifact("a" * 64, "result = 1", "Queue timeout")
    assert result.error_type == "capacity" and result.run_id is not None
    load.assert_not_awaited()


async def test_projection_cancel_during_file_worker_waits_for_worker(executor: CodeExecutor) -> None:
    """Cancelling the public request cannot abandon owned off-loop artifact reads."""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked(*args: Any) -> dict[str, Any]:
        """Hold the artifact worker until cancellation has reached its cleanup wait."""
        entered.set()
        assert release.wait(5)
        finished.set()
        return {}

    with patch.object(executor.artifacts, "_load_json", side_effect=blocked):
        task = asyncio.create_task(executor.transform_artifact("a" * 64, "result = 1", "Cancel worker"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set() and not executor._jobs and not executor._projections


async def test_projection_request_digest_binds_artifact_identity(executor: CodeExecutor) -> None:
    """Different stored inputs cannot accidentally share request identity or cached replay."""
    first = await executor.artifacts.put({"v": 1}, "local")
    second = await executor.artifacts.put({"v": 2}, "local")
    results = [
        await executor.transform_artifact(item.id, "result = inputs['artifact']", "Projection")
        for item in [first, second]
    ]
    records = [await executor.get_run(result.run_id or "") for result in results]
    assert all(record is not None for record in records)
    assert records[0] is not None and records[1] is not None and records[0].request_hash != records[1].request_hash
    assert all(result.cache_id is None for result in results)


def test_projection_traffic_dimensions_are_allowlisted_content_free() -> None:
    """Hosted request telemetry accepts the new fixed core name, never artifact contents."""
    assert "transform_artifact" in TOOLS and len(TOOLS) == 13
    observation = TrafficObservation(100)
    observation.observe({"type": "http.response.start", "status": 200, "headers": []})
    observation.observe(
        {"type": "http.response.body", "body": b'{"result":{"structuredContent":{"success":true}}}', "more_body": False}
    )
    metric = observation.snapshot("transform_artifact", True)
    assert metric.tool == "transform_artifact" and metric.success and metric.payload_bytes > 0
    assert not observation.buffer


async def test_projection_analytics_is_compute_not_api_or_replay(executor: CodeExecutor) -> None:
    """A stored artifact is not a newly accepted API response or an API-reduction baseline."""
    observer = AsyncMock()
    executor._analytics = observer
    artifact = await executor.artifacts.put([1, 2, 3], "local")
    result = await executor.transform_artifact(artifact.id, "result = sum(inputs['artifact'])", "Sum")
    assert result.success and result.data == 6
    observer.assert_awaited_once()
    assert observer.await_args is not None
    metric = observer.await_args.args[0]
    assert metric.origin == "execute" and metric.backend_started and metric.api_calls == 0
    assert metric.api_responses == metric.upstream_bytes == 0 and not metric.comparison_eligible


async def test_projection_vm_memory_remains_bounded(executor: CodeExecutor) -> None:
    """The isolated projection backend retains the operator's Monty memory budget."""
    executor._config.sandbox_memory_bytes = 1_000_000
    artifact = await executor.artifacts.put({}, "local")
    result = await executor.transform_artifact(artifact.id, "result = [0] * 1000000", "Memory bound")
    assert not result.success and result.error_type == "capacity" and result.run_id is not None


@pytest.mark.parametrize("artifact_id", [None, 123, False])
async def test_projection_invalid_handle_type_cannot_select_normal_execution(
    executor: CodeExecutor, artifact_id: Any
) -> None:
    """Invalid programmatic calls must not turn a projection into broker-enabled execution."""
    result = await executor.transform_artifact(artifact_id, "result = await call_tool('a.b', {})", "Invalid handle")
    assert not result.success and result.error_type == "validation" and result.run_id is None
