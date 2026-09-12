"""Additive analytics tables and fixed, bounded aggregate serialization."""

from __future__ import annotations

import json
import math
from typing import cast, get_args

from gryphon.errors import SaaSValidationError
from gryphon.models.analytics import RunErrorType
from gryphon.models.traffic import MAX_MEASUREMENT, ToolName

RETENTION_DAYS = 90
MAX_RECEIPTS = 100000
MAX_CHANNELS = 100
MAX_ROWS = RETENTION_DAYS * MAX_CHANNELS
MAX_BUCKET_BYTES = 32768
LATENCY_BOUNDS = (
    10,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
    120000,
    300000,
    600000,
    3600000,
    86400000,
)
ANALYTICS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS saas_analytics_daily (tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
    "day TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(tenant_id,channel_id,day), "
    "FOREIGN KEY(tenant_id,channel_id) REFERENCES saas_channels(tenant_id,id))",
    "CREATE INDEX IF NOT EXISTS saas_analytics_daily_day ON saas_analytics_daily(day)",
    "CREATE TABLE IF NOT EXISTS saas_analytics_receipts (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
    "channel_id TEXT NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('run','request')), "
    "event_digest TEXT NOT NULL, recorded_at DOUBLE PRECISION NOT NULL, day TEXT NOT NULL, "
    "UNIQUE(tenant_id,channel_id,event_type,event_digest), "
    "FOREIGN KEY(tenant_id,channel_id) REFERENCES saas_channels(tenant_id,id))",
    "CREATE INDEX IF NOT EXISTS saas_analytics_receipts_time ON saas_analytics_receipts(recorded_at,id)",
    "CREATE INDEX IF NOT EXISTS saas_analytics_receipts_day ON saas_analytics_receipts(day)",
    "CREATE TABLE IF NOT EXISTS saas_analytics_metadata (tenant_id TEXT NOT NULL, channel_id TEXT NOT NULL, "
    "initialized_since DOUBLE PRECISION NOT NULL, PRIMARY KEY(tenant_id,channel_id), "
    "FOREIGN KEY(tenant_id,channel_id) REFERENCES saas_channels(tenant_id,id))",
)
INTEGER_COUNTERS = frozenset(
    [
        "requests",
        "request_successes",
        "request_failures",
        "incomplete_requests",
        "request_wire_bytes",
        "response_wire_bytes",
        "structured_payload_bytes",
        "request_estimated_tokens",
        "response_estimated_tokens",
        "payload_estimated_tokens",
        "terminal_runs",
        "completed_runs",
        "successful_runs",
        "failed_runs",
        "cancelled_runs",
        "backend_starts",
        "replay_backend_starts",
        "replay_successes",
        "reused_source_bytes",
        "source_bytes",
        "source_lines",
        "input_bytes",
        "result_bytes",
        "result_items",
        "result_item_observations",
        "upstream_bytes",
        "upstream_items",
        "api_calls",
        "api_responses",
        "pure_compute_runs",
        "multi_call_runs",
        "artifacts_created",
        "comparable_runs",
        "comparison_upstream_bytes",
        "comparison_result_bytes",
        "comparison_upstream_estimated_tokens",
        "comparison_result_estimated_tokens",
    ]
)
FLOAT_COUNTERS = frozenset(["request_duration_ms", "run_duration_ms", "queue_ms", "execution_ms", "broker_ms"])
ERRORS = frozenset(get_args(RunErrorType.__value__)) | frozenset(
    {"protocol", "transport", "unknown", "cache_miss", "stale"}
)
DIMENSIONS = {
    "requests_by_tool": frozenset(get_args(ToolName.__value__)),
    "request_successes_by_tool": frozenset(get_args(ToolName.__value__)),
    "request_failures_by_tool": frozenset(get_args(ToolName.__value__)),
    "request_errors": ERRORS,
    "run_errors": frozenset(get_args(RunErrorType.__value__)),
    "run_statuses": frozenset({"succeeded", "failed", "cancelled"}),
    "run_origins": frozenset({"execute", "submit", "replay"}),
    "sandbox_modes": frozenset({"restricted", "docker"}),
    "request_latency_bins": frozenset(str(bound) for bound in LATENCY_BOUNDS),
    "run_latency_bins": frozenset(str(bound) for bound in LATENCY_BOUNDS),
}
type Bucket = dict[str, int | float | dict[str, int]]


def empty_bucket() -> Bucket:
    """Return independent zero counters and sparse fixed-dimension maps."""
    return {
        **dict.fromkeys(INTEGER_COUNTERS, 0),
        **dict.fromkeys(FLOAT_COUNTERS, 0.0),
        **{key: {} for key in DIMENSIONS},
    }


def validate_bucket(bucket: Bucket) -> None:
    """Reject unknown dimensions, malformed counters, and aggregate overflow."""
    if bucket.keys() != INTEGER_COUNTERS | FLOAT_COUNTERS | DIMENSIONS.keys():
        raise SaaSValidationError("Invalid analytics aggregate fields")
    for key, value in bucket.items():
        if key in DIMENSIONS:
            if not isinstance(value, dict) or not value.keys() <= DIMENSIONS[key]:
                raise SaaSValidationError("Invalid analytics dimensions")
            for count in value.values():
                _number(count, integer=True)
        else:
            _number(value, integer=key in INTEGER_COUNTERS)


def _number(value: object, *, integer: bool) -> None:
    """Require finite nonnegative JSON-safe values without boolean coercion."""
    if type(value) not in ((int,) if integer else (int, float)):
        raise SaaSValidationError("Invalid analytics counter")
    number = cast("int | float", value)
    if not 0 <= number <= MAX_MEASUREMENT or not math.isfinite(number):
        raise SaaSValidationError("Analytics counter overflow")


def encode_bucket(bucket: Bucket) -> str:
    """Serialize only checked numeric aggregates as bounded strict canonical JSON."""
    validate_bucket(bucket)
    encoded = json.dumps(bucket, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > MAX_BUCKET_BYTES:
        raise SaaSValidationError("Analytics aggregate exceeds byte bound")
    return encoded


def decode_bucket(payload: str) -> Bucket:
    """Reject malformed persisted data rather than repairing or fabricating measurements."""
    if len(payload) > MAX_BUCKET_BYTES:
        raise SaaSValidationError("Analytics aggregate exceeds byte bound")
    try:
        bucket = cast("Bucket", json.loads(payload))
        if not isinstance(bucket, dict) or encode_bucket(bucket) != payload:
            raise SaaSValidationError("Invalid canonical analytics aggregate")
    except (ValueError, TypeError, RecursionError) as exc:
        raise SaaSValidationError("Invalid analytics aggregate") from exc
    return bucket


def merge_bucket(target: Bucket, source: Bucket) -> None:
    """Add already validated buckets; callers check storage bounds before persistence."""
    for key, value in source.items():
        if isinstance(value, dict):
            values = cast("dict[str, int]", target[key])
            for dimension, count in value.items():
                values[dimension] = values.get(dimension, 0) + count
        else:
            target[key] = cast("int | float", target[key]) + value


def increment_dimension(bucket: Bucket, key: str, dimension: str) -> None:
    """Increment one internal allowlisted categorical observation."""
    values = cast("dict[str, int]", bucket[key])
    values[dimension] = values.get(dimension, 0) + 1


def observe_latency(bucket: Bucket, key: str, duration: float) -> None:
    """Count a duration in a fixed upper-inclusive histogram, not an exact percentile."""
    bound = next(bound for bound in LATENCY_BOUNDS if duration <= bound)
    increment_dimension(bucket, key, str(bound))
