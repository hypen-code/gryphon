"""Exercise Gryphon discovery, restricted execution, and typed recipe reuse offline."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from fastmcp import Client

from gryphon.config import load_config
from gryphon.server import create_server
from gryphon.utils.logging import setup_logging

# ---------------------------------------------------------------------------
# This demo uses a real in-process MCP client without a model API key.
# Optionally run gryphon compile first to include your configured API catalog.
# ---------------------------------------------------------------------------


def _show(label: str, value: Any) -> None:
    """Write a demo result without interfering with an MCP stdio transport."""
    sys.stdout.write(f"{label}\n{json.dumps(value, indent=2, ensure_ascii=False)}\n")


async def demo() -> None:
    """Run the complete workflow with actual bounded Python and no upstream calls."""
    setup_logging("WARNING")
    config = load_config()
    async with Client(create_server(config)) as client:
        # Step 1: List compact server metadata without loading every function schema.
        servers = await client.call_tool("list_servers", {})
        _show("Gryphon catalog", servers.data)

        # Step 2: Inspect only relevant capabilities, if the weather example was compiled.
        matches = await client.call_tool("search_functions", {"query": "weather", "limit": 3})
        _show("Focused discovery", matches.data)
        execution = await client.call_tool(
            "execute_code",
            {
                "code": "result = sum(inputs['values'])",
                "description": "sum numeric values",
                "inputs": {"values": [2, 3, 5]},
                "input_schema": {
                    "type": "object",
                    "properties": {"values": {"type": "array", "items": {"type": "number"}}},
                    "required": ["values"],
                    "additionalProperties": False,
                },
            },
        )
        _show("Restricted execution", execution.data)

        # Step 3: Reuse unchanged code with validated inputs rather than rewriting source.
        if execution.data.get("success") and execution.data.get("cache_id"):
            reused = await client.call_tool(
                "run_cached_code",
                {
                    "cache_id": execution.data["cache_id"],
                    "params": {"values": [10, 20, 30]},
                },
            )
            _show("Typed recipe reuse", reused.data)
        else:
            raise RuntimeError("Gryphon demo execution failed")


if __name__ == "__main__":
    asyncio.run(demo())
