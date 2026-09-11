"""Explicit v2 rejection of unverified executable-source enhancement."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gryphon.errors import CompileError
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig

logger = get_logger(__name__)


async def enhance_with_llm(code: str, server_name: str, config: GryphonConfig) -> str:
    """Reject executable-source enhancement until a metadata-only contract is supported.

    Args:
        code: Deterministic SDK source, never sent to an external service.
        server_name: Source identifier, never interpreted as executable code.
        config: Application settings; no credentials are read or serialized here.

    Raises:
        CompileError: Always, because v2 does not support source enhancement.
    """
    # LLM providers previously received generated source and an explicit API key.
    # V2 never sends auth configuration or executable source to an enhancement service.
    logger.warning("llm_enhancement_unsupported")
    raise CompileError("LLM enhancement is unsupported in v2; deterministic manifests are required")
