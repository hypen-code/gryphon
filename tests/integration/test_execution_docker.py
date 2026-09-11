"""Opt-in benign Docker transport checks; production still defaults to runsc.

Enable only deliberately with GRYPHON_TEST_DOCKER=1 after building the tagged
image. These tests explicitly choose runc, never fall back from runsc, never
invoke external APIs, and remove only containers created by their own backend.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from gryphon.config import GryphonConfig
from gryphon.runtime.cache import CacheStore
from gryphon.runtime.docker_sandbox import DockerSandbox
from gryphon.runtime.executor import CodeExecutor
from gryphon.runtime.registry import Registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    os.environ.get("GRYPHON_TEST_DOCKER") != "1", reason="Explicit live Docker opt-in required"
)


@pytest.fixture
async def docker_execution(tmp_path: Path) -> AsyncIterator[CodeExecutor]:
    """Own a single explicit offline test backend and private temporary stores."""
    options: dict[str, Any] = {
        "_env_file": None,
        "sandbox_mode": "docker",
        "docker_runtime": "runc",
        "execution_timeout_seconds": 5,
        "cache_db_path": str(tmp_path / "cache.db"),
        "run_db_path": str(tmp_path / "runs.db"),
        "artifact_dir": str(tmp_path / "artifacts"),
        "compiled_output_dir": str(tmp_path / "compiled"),
    }
    config = GryphonConfig(**options)
    cache = CacheStore(config.cache_db_path)
    await cache.initialize()
    registry = Registry(config.compiled_output_dir)
    registry.load()
    executor = CodeExecutor(config, cache, registry, broker=AsyncMock())
    try:
        await executor.startup()
        yield executor
    finally:
        await executor.shutdown()
        await cache.close()


async def test_live_offline_stdin_supports_large_explicit_inputs(docker_execution: CodeExecutor) -> None:
    result = await docker_execution.execute(
        "result = inputs['n'] + 2", "offline transport", {"n": 40, "padding": "x" * 200_000}
    )
    assert result.success and result.data == 42
    assert isinstance(docker_execution._sandbox, DockerSandbox)
    assert not docker_execution._sandbox._owned
    cast("AsyncMock", docker_execution._broker.invoke).assert_not_awaited()


async def test_live_offline_explicit_return_executes_once(docker_execution: CodeExecutor) -> None:
    result = await docker_execution.execute("return inputs['n'] * 2", "offline return", {"n": 21})
    assert result.success and result.data == 42
    assert isinstance(docker_execution._sandbox, DockerSandbox)
    assert not docker_execution._sandbox._owned


async def test_live_offline_invalid_json_output_never_succeeds(docker_execution: CodeExecutor) -> None:
    result = await docker_execution.execute("result = {1, 2}", "strict JSON")
    assert not result.success and result.cache_id is None
    assert isinstance(docker_execution._sandbox, DockerSandbox)
    assert not docker_execution._sandbox._owned


async def test_live_offline_numpy_and_pandas_compute_without_broker(docker_execution: CodeExecutor) -> None:
    """Use only preinstalled wheels and explicit data in a networkless owned container."""
    source = (
        "import numpy as np\nimport pandas as pd\n"
        "values = np.array(inputs['values'])\n"
        "frame = pd.DataFrame({'value': values})\n"
        "result = {'sum': int(frame['value'].sum()), 'mean': float(np.mean(values))}"
    )
    result = await docker_execution.execute(source, "offline numeric smoke", {"values": [1, 2, 3]})
    assert result.success and result.data == {"sum": 6, "mean": 2.0}
    assert isinstance(docker_execution._sandbox, DockerSandbox)
    assert not docker_execution._sandbox._owned
    cast("AsyncMock", docker_execution._broker.invoke).assert_not_awaited()


async def test_live_offline_full_response_envelope_limit(docker_execution: CodeExecutor) -> None:
    """A native result close to the byte limit cannot evade JSON protocol overhead."""
    docker_execution._config.max_response_size_bytes = 1024
    result = await docker_execution.execute("result = 'x' * 1000", "bounded envelope")
    assert not result.success and result.cache_id is None
    assert isinstance(docker_execution._sandbox, DockerSandbox)
    assert not docker_execution._sandbox._owned
