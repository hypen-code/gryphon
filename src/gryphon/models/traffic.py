"""Bounded, content-free observations of completed hosted MCP requests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gryphon.models.analytics import RunErrorType

type ToolName = Literal[
    "list_servers",
    "search_functions",
    "get_functions",
    "execute_code",
    "run_cached_code",
    "submit_code",
    "get_run",
    "cancel_run",
    "list_recipes",
    "read_artifact",
    "list_skills",
    "get_server_skills",
]
type RequestErrorType = RunErrorType | Literal["protocol", "transport", "unknown", "cache_miss", "stale"]
MAX_MEASUREMENT = 2**53 - 1
MAX_DURATION_MS = 86400000


class RequestMetrics(BaseModel):
    """Measure one server-identified request without storing its contents.

    Wire counters measure observed UTF-8 request/response bytes. Payload bytes
    count one canonical structured result, not its duplicate text representation.
    Incomplete transport observations remain explicit and are never imputed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    request_id: str = Field(pattern=r"^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$")
    tool: ToolName
    success: bool
    error_type: RequestErrorType | None = None
    request_bytes: int = Field(default=0, ge=0, le=MAX_MEASUREMENT)
    response_bytes: int = Field(default=0, ge=0, le=MAX_MEASUREMENT)
    payload_bytes: int = Field(default=0, ge=0, le=MAX_MEASUREMENT)
    duration_ms: float = Field(default=0, ge=0, le=MAX_DURATION_MS)
    observation_complete: bool = True
