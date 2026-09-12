"""Explicit browser-administrator selection of literal POST reads from canonical saved bytes.

Callers must authorize platform administration and recheck it after awaiting selection.
Descriptions and extension hints are untrusted display text, never permission evidence.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from gryphon.compiler.catalog import module_name
from gryphon.errors import InputValidationError
from gryphon.models import ReadOnlyPostOperation
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.saas_upload import parse_uploaded_document
from gryphon.utils.logging import get_logger

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import SaaSSpec

logger = get_logger(__name__)
_MAX_SELECTION = 1000


def _candidates(
    previous: SaaSSpec,
    config: GryphonConfig,
    limit: int,
) -> list[tuple[dict[str, Any], ReadOnlyPostOperation]]:
    """Enumerate supported literal POSTs; never derive routing authority from submitted names."""
    name = module_name(previous.name)
    parsed = parse_uploaded_document(previous.document, config, limit, name, False)
    result = []
    for endpoint in parsed.endpoints:
        if endpoint.method != "POST":
            continue
        try:
            permit = ReadOnlyPostOperation(
                server_name=name,
                method="POST",
                base_url=endpoint.base_url or parsed.base_url,
                path=endpoint.path,
            )
        except ValidationError:
            logger.warning("post_read_candidate_excluded", reason="non_literal_route")
            continue
        inherited = permit in config.allowed_read_only_post_operations
        result.append(
            (
                {
                    "function_name": f"{name}.{endpoint.operation_id}",
                    "summary": endpoint.summary,
                    "method": "POST",
                    "base_url": permit.base_url,
                    "path": permit.path,
                    "approved": inherited or permit in previous.approved_post_reads,
                    "operator_approved": inherited,
                },
                permit,
            )
        )
    return sorted(result, key=lambda entry: entry[0]["function_name"])


async def list_post_read_candidates(
    previous: SaaSSpec,
    config: GryphonConfig,
    limit: int,
) -> list[dict[str, Any]]:
    """Return reviewable saved POST rows without HTTP; caller owns admission and authorization."""
    candidates = await finish_cleanup(asyncio.to_thread(_candidates, previous, config, limit))
    return [row for row, _ in candidates]


async def select_post_reads(
    previous: SaaSSpec,
    functions: Any,
    config: GryphonConfig,
    limit: int,
) -> list[ReadOnlyPostOperation]:
    """Replace scoped grants using canonical names only; inherited operator grants remain immutable."""
    if (
        not isinstance(functions, list)
        or len(functions) > _MAX_SELECTION
        or any(not isinstance(name, str) for name in functions)
    ):
        raise InputValidationError("POST read selection must be a bounded list of function names")
    if len(set(functions)) != len(functions):
        raise InputValidationError("Duplicate POST read selection")
    candidates = await finish_cleanup(asyncio.to_thread(_candidates, previous, config, limit))
    allowed = {row["function_name"]: (row, permit) for row, permit in candidates}
    if any(name not in allowed for name in functions):
        raise InputValidationError("Unknown or stale POST read selection")
    return [allowed[name][1] for name in sorted(functions) if not allowed[name][0]["operator_approved"]]
