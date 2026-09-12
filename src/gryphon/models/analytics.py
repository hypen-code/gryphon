"""Content-free execution measurements, never model billing or CPU estimates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type RunOrigin = Literal["execute", "submit", "replay"]
type RunErrorType = Literal[
    "capacity",
    "timeout",
    "security",
    "validation",
    "conflict",
    "cache",
    "sandbox_unavailable",
    "not_found",
    "execution",
    "internal",
    "cancelled",
]


class RunMetrics(BaseModel):
    """One admitted run's scalar measurements, with no execution or API content.

    Durations are monotonic wall milliseconds, not CPU time. Result bytes measure
    strict canonical final JSON before artifact summarization; upstream bytes
    measure accepted broker JSON before user-code reduction. Neither measures
    model round trips or actual tokens. Null final JSON measures four bytes;
    non-list final values have no item count. Unstarted admissions are explicit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    run_id: str = Field(min_length=1, max_length=64)
    origin: RunOrigin
    status: Literal["succeeded", "failed", "cancelled"]
    error_type: RunErrorType | None = None
    sandbox_mode: Literal["restricted", "docker"]
    duration_ms: float = Field(default=0, ge=0)
    queue_ms: float = Field(default=0, ge=0)
    execution_ms: float = Field(default=0, ge=0)
    source_bytes: int = Field(default=0, ge=0)
    source_lines: int = Field(default=0, ge=0)
    input_bytes: int = Field(default=0, ge=0)
    result_bytes: int = Field(default=0, ge=0)
    result_items: int | None = Field(default=None, ge=0)
    upstream_bytes: int = Field(default=0, ge=0)
    upstream_items: int = Field(default=0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    api_responses: int = Field(default=0, ge=0)
    broker_ms: float = Field(default=0, ge=0)
    backend_started: bool = False
    artifact_created: bool = False
    comparison_eligible: bool = False
