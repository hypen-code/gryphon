"""V2 enhancer requests fail explicitly and never contact a paid model service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gryphon.compiler.llm_enhancer import enhance_with_llm
from gryphon.compiler.orchestrator import Orchestrator
from gryphon.config import GryphonConfig
from gryphon.errors import CompileError

# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["", "configured-key"])
async def test_enhance_explicitly_unsupported_with_any_key(key: str) -> None:
    """Presence of credentials does not enable unsafe source rewriting."""
    config = GryphonConfig.model_construct(llm_enhance=True, llm_api_key=key)
    with pytest.raises(CompileError, match="unsupported in v2"):
        await enhance_with_llm("result = 1", "weather", config)


# ---------------------------------------------------------------------------
# litellm not installed
# ---------------------------------------------------------------------------


async def test_enhance_rejection_does_not_require_optional_dependency() -> None:
    """The diagnostic is stable even when the former provider library is absent."""
    with patch.dict("sys.modules", {"litellm": None}), pytest.raises(CompileError, match="unsupported"):
        await enhance_with_llm("result = 1", "weather", GryphonConfig.model_construct())


# ---------------------------------------------------------------------------
# No executable-source enhancement
# ---------------------------------------------------------------------------


async def test_enhance_never_sends_source_or_credentials() -> None:
    """No provider call is made, even with an installed model client and API key."""
    provider = MagicMock()
    with patch.dict("sys.modules", {"litellm": provider}), pytest.raises(CompileError):
        await enhance_with_llm("result = 1", "weather", GryphonConfig.model_construct(llm_api_key="configured"))
    provider.completion.assert_not_called()


# ---------------------------------------------------------------------------
# No silent fallback on LLM failure
# ---------------------------------------------------------------------------


async def test_compile_requested_enhancer_fails_explicitly(gryphon_config: GryphonConfig) -> None:
    """The CLI-facing orchestrator cannot silently ignore the enhancement flag."""
    gryphon_config.llm_enhance = True
    # No fallback to original code when enhancement was explicitly requested.
    with pytest.raises(CompileError, match="unsupported in v2"):
        await Orchestrator(gryphon_config).compile_all(dry_run=True)
