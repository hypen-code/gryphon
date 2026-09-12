"""Thin MCP adapter for owner-scoped offline artifact projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gryphon.runtime.context import public_result, trusted_owner

if TYPE_CHECKING:
    from gryphon.runtime.executor import CodeExecutor


class ArtifactTools:
    """Extend the core adapters without adding authority beyond the executor."""

    executor: CodeExecutor

    async def transform_artifact(
        self, artifact_id: str, code: str, description: str, inputs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Reduce stored JSON offline, without refetching or reading chunks.

        Args:
            artifact_id: Owned handle returned by execution or a run receipt.
            code: Restricted Python assigning result; no imports or call_tool capability.
            description: Bounded generic projection description, without input values.
            inputs: Optional JSON parameters exposed as inputs['params']; parsed stored
                JSON is inputs['artifact']. Neither grants filesystem or network access.

        Returns:
            The execute_code result/run/artifact shape, but no replayable cache_id.
            Requires the restricted profile; Docker configurations reject projections.
        """
        result = await self.executor.transform_artifact(artifact_id, code, description, inputs, owner=trusted_owner())
        return public_result(result)
