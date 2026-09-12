"""Narrowing-only channel import authority on the mandatory execution AST path."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gryphon.errors import SecurityViolationError
from gryphon.models import ExecutionResult
from gryphon.runtime.execution_validation import effective_imports
from gryphon.runtime.executor import CodeExecutor
from gryphon.security.ast_guard import ASTGuard, available_imports, configured_imports

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


@pytest.fixture
async def executor(gryphon_config: GryphonConfig) -> AsyncIterator[CodeExecutor]:
    """Run the real admission/guard/ledger path with only Docker and broker mocked."""
    gryphon_config.sandbox_mode = "docker"
    cache, broker, registry, backend = AsyncMock(), AsyncMock(), MagicMock(), AsyncMock()
    cache.store.return_value = "recipe"
    registry.fingerprint.return_value = "catalog"
    registry.list_servers.return_value = []
    backend.run.return_value = ExecutionResult(success=True, data=42)
    with patch("gryphon.runtime.executor.DockerSandbox", return_value=backend):
        instance = CodeExecutor(gryphon_config, cache, registry, broker)
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.mark.parametrize("module", available_imports())
async def test_imports_default_profile_preserves_existing_modules(executor: CodeExecutor, module: str) -> None:
    """All advertised names retain the established stdio Docker AST grants."""
    result = await executor.execute(f"import {module}\nresult = 42", "default")
    assert result.success
    cast("AsyncMock", executor._sandbox.run).assert_awaited_once()


@pytest.mark.parametrize("code", ["import math", "from math import sqrt", "import numpy", "import pandas"])
async def test_imports_subset_positive_uses_mandatory_ast(executor: CodeExecutor, code: str) -> None:
    """A configured subset is checked at admission and again before execution."""
    executor._config.sandbox_allowed_imports = ["math", "numpy", "pandas"]
    with patch.object(executor._ast_guard, "validate", wraps=executor._ast_guard.validate) as guard:
        result = await executor.execute(code + "\nresult = 42", "subset")
    assert result.success and guard.call_count == 2
    expected = frozenset({"math", "numpy", "pandas"})
    assert all(call.kwargs["allowed_modules"] == expected for call in guard.call_args_list)


@pytest.mark.parametrize("code", ["import json", "from json import loads", "import collections.abc", "import numpy"])
async def test_imports_unselected_module_rejected_before_backend(executor: CodeExecutor, code: str) -> None:
    """Defaults and numeric grants cannot leak back into an explicit subset."""
    executor._config.sandbox_allowed_imports = ["math"]
    result = await executor.execute(code + "\nresult = 42", "unselected")
    assert result.error_type == "security"
    cast("AsyncMock", executor._sandbox.run).assert_not_awaited()


@pytest.mark.parametrize("code", ["import math", "from math import sqrt", "from __future__ import annotations"])
async def test_imports_empty_list_denies_every_import(executor: CodeExecutor, code: str) -> None:
    """An empty list is not treated as the unspecified/default profile."""
    executor._config.sandbox_allowed_imports = []
    result = await executor.execute(code + "\nresult = 42", "none")
    assert result.error_type == "security"
    cast("AsyncMock", executor._sandbox.run).assert_not_awaited()


async def test_imports_empty_list_still_allows_plain_computation(executor: CodeExecutor) -> None:
    """Disabling imports does not disable the offline compute backend."""
    executor._config.sandbox_allowed_imports = []
    assert (await executor.execute("result = 42", "no imports")).success


@pytest.mark.parametrize("module", ["os", "sys", "requests", "socket", "importlib", "math.random", "unknown", "*"])
async def test_imports_forbidden_modules_cannot_be_granted(executor: CodeExecutor, module: str) -> None:
    """Malformed authority fails closed even when the source contains no imports."""
    executor._config.sandbox_allowed_imports = ["math", module]
    result = await executor.execute("result = 42", "invalid policy")
    assert result.error_type == "security"
    cast("AsyncMock", executor._sandbox.run).assert_not_awaited()


@pytest.mark.parametrize("modules", [None, [], ["math"], ["numpy", "pandas"]])
async def test_imports_restricted_profile_never_grants_imports(
    executor: CodeExecutor, modules: list[str] | None
) -> None:
    """Restricted AST validation itself denies imports, not only source preparation."""
    executor._config.sandbox_mode = "restricted"
    executor._config.sandbox_allowed_imports = modules
    with pytest.raises(SecurityViolationError):
        executor._guard("import math\nresult = 42")
    result = await executor.execute("import math\nresult = 42", "restricted")
    assert result.error_type == "security"
    assert effective_imports(executor._config) == frozenset()
    cast("AsyncMock", executor._sandbox.run).assert_not_awaited()


def test_imports_available_names_are_detached_sorted_supported() -> None:
    """UI inventory omits compiler directives but preserves exact dotted grants."""
    modules = available_imports()
    assert modules == sorted(set(modules))
    assert "__future__" not in modules and "collections.abc" in modules
    assert {"numpy", "pandas", "math"} <= set(modules)
    assert configured_imports(modules) == frozenset(modules)
    modules.clear()
    assert available_imports()
    assert "__future__" in configured_imports(None)


async def test_imports_future_directive_retains_explicit_compatibility(executor: CodeExecutor) -> None:
    """The supported compiler directive can still be selected explicitly."""
    executor._config.sandbox_allowed_imports = ["__future__"]
    result = await executor.execute("from __future__ import annotations\nresult = 42", "future")
    assert result.success


@pytest.mark.parametrize("value", ["math", {"math"}, [1], [[]], ["Math"], [" math"]])
def test_imports_malformed_configuration_fails_closed(value: object) -> None:
    """Revalidation rejects untyped or mutated configuration rather than coercing."""
    with pytest.raises(SecurityViolationError):
        configured_imports(cast("list[str]", value))


@pytest.mark.parametrize("value", [frozenset({"os"}), frozenset({"numpy"}), ["math"]])
def test_imports_ast_override_cannot_widen_grants(value: object) -> None:
    """The AST override itself only narrows existing standard/numeric authority."""
    with pytest.raises(SecurityViolationError):
        ASTGuard().validate("result = 42", allowed_modules=cast("frozenset[str]", value))


def test_imports_unknown_backend_fails_closed(gryphon_config: GryphonConfig) -> None:
    """An invalid mutated sandbox mode cannot fall back to broader authority."""
    config = gryphon_config.model_copy(update={"sandbox_mode": "unknown"})
    with pytest.raises(SecurityViolationError):
        effective_imports(config)


async def test_imports_digest_canonicalizes_order_and_duplicates(executor: CodeExecutor) -> None:
    """Equivalent selected sets retain recipe and idempotency identity."""
    executor._config.sandbox_allowed_imports = ["numpy", "math", "math"]
    identity = executor._fingerprint()
    executor._config.sandbox_allowed_imports = ["math", "numpy"]
    assert identity == executor._fingerprint()


async def test_imports_digest_default_matches_explicit_full_profile(executor: CodeExecutor) -> None:
    """Policy identity binds effective authority, not its configuration spelling."""
    identity = executor._fingerprint()
    executor._config.sandbox_allowed_imports = sorted(configured_imports(None))
    assert identity == executor._fingerprint()


@pytest.mark.parametrize("modules", [[], ["math"], ["numpy", "pandas"]])
async def test_imports_digest_drift_invalidates_replay(executor: CodeExecutor, modules: list[str]) -> None:
    """A changed import policy rejects old recipes before backend invocation."""
    identity = executor._fingerprint()
    cast("AsyncMock", executor._cache.get).return_value = MagicMock(swagger_hash=identity)
    executor._config.sandbox_allowed_imports = modules
    assert identity != executor._fingerprint()
    result = await executor.replay("recipe")
    assert result.error_type == "conflict"
    cast("AsyncMock", executor._sandbox.run).assert_not_awaited()


@pytest.mark.parametrize("module, success", [("math", True), ("json", False)])
async def test_imports_replay_rechecks_exact_current_ast_profile(
    executor: CodeExecutor, module: str, success: bool
) -> None:
    """A current recipe still runs the complete narrowing guard pipeline."""
    executor._config.sandbox_allowed_imports = ["math"]
    cast("AsyncMock", executor._cache.get).return_value = MagicMock(
        swagger_hash=executor._fingerprint(),
        code=f"import {module}\nresult = 42",
        description="replay",
        input_schema={},
    )
    with patch.object(executor._ast_guard, "validate", wraps=executor._ast_guard.validate) as guard:
        result = await executor.replay("recipe")
    assert result.success is success
    assert guard.call_count == (2 if success else 1)
    assert cast("AsyncMock", executor._sandbox.run).await_count == int(success)
