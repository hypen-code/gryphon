"""Real hosted MCP analytics with isolated SQLite and only synthetic broker network I/O."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import Client
from test_saas_http import _channel, _data
from test_saas_http import hosted as hosted
from test_saas_user_http import _publish
from test_saas_user_http import members as members

from gryphon.models import RunMetrics

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from test_saas_user_http import Members

    from gryphon.saas_analytics import AnalyticsStore

NETWORK = "gryphon.security.broker.NetworkClient.request"
PRIVATE = "fixture-upstream-private-é"
INPUT = "fixture-input-private-界"
FIRST = [{"n": 7, "private": PRIVATE * 30}]
SECOND = {"n": 8, "private": PRIVATE * 11}
CODE = (
    'private_literal = "fixture-source-private"\n'
    'left = await call_tool("weather.current", {})\n'
    'right = await call_tool("weather.current", {})\n'
    'result = [left[0]["n"], right["n"], inputs["marker"]]'
)


def _bytes(value: object) -> int:
    """Calculate expected canonical bytes independently of production measurement helpers."""
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


async def _report(
    http: httpx.AsyncClient, prefix: str, *, requests: int | None = None, runs: int | None = None, query: str = ""
) -> dict[str, Any]:
    """Wait only for a bounded observable post-response write, never with arbitrary sleeps."""
    async with asyncio.timeout(5):
        while True:
            response = await http.get(prefix + "/analytics" + query)
            assert response.status_code == 200
            report: dict[str, Any] = response.json()
            summary = report["summary"]
            if requests is not None:
                assert summary["requests"] <= requests
            if runs is not None:
                assert summary["terminal_runs"] <= runs
            if (requests is None or summary["requests"] == requests) and (
                runs is None or summary["terminal_runs"] == runs
            ):
                return report


def _comparison(report: dict[str, Any], upstream: list[int], results: list[object]) -> None:
    """Assert paired weighted bytes, per-run rounded estimates, and artifact-inclusive final JSON."""
    summary = report["summary"]
    sizes = [_bytes(value) for value in results]
    assert summary["comparison_upstream_bytes"] == summary["upstream_bytes"] == sum(upstream)
    assert summary["comparison_result_bytes"] == summary["result_bytes"] == sum(sizes)
    assert summary["comparison_upstream_estimated_tokens"] == sum((size + 3) // 4 for size in upstream)
    assert summary["comparison_result_estimated_tokens"] == sum((size + 3) // 4 for size in sizes)
    assert summary["estimated_token_reduction"] == sum((size + 3) // 4 for size in upstream) - sum(
        (size + 3) // 4 for size in sizes
    )
    assert summary["payload_reduction_bytes"] == sum(upstream) - sum(sizes)
    assert summary["payload_reduction_percent"] == pytest.approx(100 * (sum(upstream) - sum(sizes)) / sum(upstream))
    assert summary["comparable_runs"] == len(results)
    assert sum((size + 3) // 4 for size in sizes) != (sum(sizes) + 3) // 4
    assert summary == {key: report["channels"][0][key] for key in summary}
    assert summary == {key: report["daily"][-1][key] for key in summary}


async def test_analytics_real_monty_reduction_replay_and_full_artifact_bytes(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Count accepted API responses, not duplicated MCP text or summarized artifact envelopes."""
    http, app = hosted
    channel = await _channel(http, "weather")
    assert app.state.analytics is app.state.admin.analytics is app.state.runtimes._analytics
    empty = await _report(http, channel["tenant"], requests=0, runs=0)
    assert empty["window"]["recording_since"] is None
    network = AsyncMock(
        side_effect=[httpx.Response(200, json=value) for value in [FIRST, SECOND, FIRST, SECOND, FIRST]]
    )
    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch(NETWORK, network):
            first = await _data(
                client, "execute_code", {"code": CODE, "description": "Private reduction", "inputs": {"marker": INPUT}}
            )
            second = await _data(
                client, "run_cached_code", {"cache_id": first["cache_id"], "params": {"marker": False}}
            )
            artifact = await _data(
                client,
                "execute_code",
                {"code": 'await call_tool("weather.current", {})\nresult = "界" * 30000', "description": "Expansion"},
            )
        assert first["data"] == [7, 8, INPUT] and second["data"] == [7, 8, False]
        report = await _report(http, channel["tenant"], requests=3, runs=3)
        assert network.await_count == 5 and report["summary"]["artifacts_created"] == 1
        receipt = await _data(client, "get_run", {"run_id": artifact["run_id"]})
        chunk = await _data(client, "read_artifact", {"artifact_id": receipt["result"]["artifact_id"]})
        assert chunk["text"] and receipt["result"]["artifact_id"]
    report = await _report(http, channel["tenant"], requests=5, runs=3)
    upstream = [_bytes(FIRST) + _bytes(SECOND)] * 2 + [_bytes(FIRST)]
    _comparison(report, upstream, [[7, 8, INPUT], [7, 8, False], "界" * 30000])
    summary = report["summary"]
    assert summary["api_calls"] == summary["api_responses"] == 5 and summary["multi_call_runs"] == 2
    assert summary["pure_compute_runs"] == 0 and summary["backend_starts"] == 3
    assert summary["run_origins"] == {"execute": 2, "replay": 1}
    assert summary["replay_backend_starts"] == summary["replay_successes"] == 1
    assert summary["reused_source_bytes"] == len(CODE.encode()) and summary["payload_reduction_percent"] < 0
    assert summary["structured_payload_bytes"] == sum(
        _bytes(value) for value in [first, second, artifact, receipt, chunk]
    )
    assert summary["request_successes"] == 5 and summary["incomplete_requests"] == 0
    assert summary["upstream_items"] == 3 and summary["result_items"] == 6
    await _download(http, channel, report)


async def _download(http: httpx.AsyncClient, channel: dict[str, str], report: dict[str, Any]) -> None:
    """Require downloads to retain methodology and omit every private fixture value."""
    assert datetime.fromisoformat(report["window"]["recording_since"]).tzinfo is not None
    assert report["methodology"]["actual_model_tokens"] is None
    assert report["methodology"]["estimator"] == "ceil(utf8_bytes / 4)"
    downloaded = await http.get(channel["tenant"] + "/analytics", headers={"accept": "application/json"})
    assert downloaded.json() == report
    private_values = [PRIVATE, INPUT, "fixture-source-private", "Private reduction", '"界界', channel["token"]]
    assert all(value not in downloaded.text for value in [*private_values, *CODE.splitlines()])


async def test_analytics_idempotency_and_cache_rejections_never_invent_actual_replays(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Duplicate calls count traffic only; missing and fingerprint-stale recipes do not start backends."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    arguments: dict[str, object] = {
        "code": "result = 42",
        "description": "Once",
        "idempotency_key": "fixture-deduplication-key",
    }
    async with Client(channel["url"], auth=channel["token"]) as client:
        first = await _data(client, "execute_code", arguments)
        duplicate = await _data(client, "execute_code", arguments)
        assert duplicate == first
        conflict = await _data(client, "execute_code", arguments | {"code": "result = 43"})
        missing = await _data(client, "run_cached_code", {"cache_id": "missing-recipe"})
        assert not conflict["success"] and conflict["error_type"] == "conflict"
        assert not missing["success"] and missing["error_type"] == "cache"
    policy = {"name": "weather", "spec_ids": [], "sandbox_mode": "restricted", "allowed_imports": [], "enabled": True}
    assert (await http.patch(channel["path"], json=policy)).status_code == 200
    async with Client(channel["url"], auth=channel["token"]) as client:
        stale = await _data(client, "run_cached_code", {"cache_id": first["cache_id"]})
        assert not stale["success"] and stale["error_type"] == "conflict"
    summary = (await _report(http, channel["tenant"], requests=5, runs=1))["summary"]
    assert summary["backend_starts"] == summary["pure_compute_runs"] == 1
    assert summary["replay_backend_starts"] == summary["replay_successes"] == summary["reused_source_bytes"] == 0
    assert summary["run_origins"] == {"execute": 1} and summary["request_failures"] == 3
    assert summary["request_errors"] == {"conflict": 2, "cache": 1}
    assert summary["requests_by_tool"] == {"execute_code": 3, "run_cached_code": 2}


async def test_analytics_background_failure_is_terminal_before_polling(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Submitting is successful traffic; a later failed run is recorded without any get_run call."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    entered, release = asyncio.Event(), asyncio.Event()

    async def network(*args: object, **kwargs: object) -> httpx.Response:
        """Hold only the external capability until the test permits a synthetic upstream failure."""
        entered.set()
        await release.wait()
        return httpx.Response(200, json=FIRST)

    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch(NETWORK, side_effect=network):
            try:
                submitted = await _data(
                    client,
                    "submit_code",
                    {
                        "code": 'await call_tool("weather.current", {})\nresult = 1 / 0',
                        "description": "Deferred failure",
                    },
                )
                async with asyncio.timeout(3):
                    await entered.wait()
                initial = await _report(http, channel["tenant"], requests=1, runs=0)
                assert initial["summary"]["request_successes_by_tool"] == {"submit_code": 1}
                release.set()
                terminal = await _report(http, channel["tenant"], requests=1, runs=1)
                assert terminal["summary"]["failed_runs"] == 1
            finally:
                release.set()
        receipt = await _data(client, "get_run", {"run_id": submitted["id"]})
        assert receipt["status"] == "failed" and not receipt["result"]["success"]
        assert (await _data(client, "get_run", {"run_id": submitted["id"]})) == receipt
    summary = (await _report(http, channel["tenant"], requests=3, runs=1))["summary"]
    assert summary["request_successes_by_tool"] == {"submit_code": 1, "get_run": 2}
    assert summary["request_failures"] == 0 and summary["run_origins"] == {"submit": 1}
    assert summary["api_calls"] == summary["api_responses"] == 1 and summary["multi_call_runs"] == 0
    assert summary["pure_compute_runs"] == summary["comparable_runs"] == 0


async def test_analytics_blocked_attempts_are_not_accepted_responses_or_compute(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Scope calls include blocked dispatches while multi-call labels require accepted responses."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    code = 'await call_tool("weather.current", {})\nresult = await call_tool("weather.current", {"forged": 1})'
    async with Client(channel["url"], auth=channel["token"]) as client:
        with patch(NETWORK, AsyncMock(return_value=httpx.Response(200, json=FIRST))) as network:
            failed = await _data(client, "execute_code", {"code": code, "description": "Blocked attempt"})
            blocked = await _data(
                client,
                "execute_code",
                {"code": 'result = await call_tool("weather.current", {"forged": 1})', "description": "Blocked only"},
            )
        assert not failed["success"] and not blocked["success"] and network.await_count == 1
    summary = (await _report(http, channel["tenant"], requests=2, runs=2))["summary"]
    assert summary["api_calls"] == 3 and summary["api_responses"] == 1
    assert summary["multi_call_runs"] == summary["pure_compute_runs"] == summary["comparable_runs"] == 0
    assert summary["failed_runs"] == summary["request_failures"] == 2


async def test_analytics_utf8_nonstring_compute_has_no_fabricated_comparison(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Null, boolean, integer and Unicode outputs use JSON bytes, not Python string conversion."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    values = [None, False, 7, "é"]
    async with Client(channel["url"], auth=channel["token"]) as client:
        for value in values:
            result = await _data(
                client,
                "execute_code",
                {"code": 'result = inputs["value"]', "description": "Compute", "inputs": {"value": value}},
            )
            assert result["success"]
            assert ("data" not in result) if value is None else result["data"] == value
    summary = (await _report(http, channel["tenant"], requests=4, runs=4))["summary"]
    assert summary["result_bytes"] == 14 and summary["pure_compute_runs"] == 4
    assert summary["input_bytes"] == sum(_bytes({"value": value}) for value in values)
    assert summary["api_calls"] == summary["api_responses"] == summary["comparable_runs"] == 0
    assert summary["comparison_result_estimated_tokens"] == 0 and summary["payload_reduction_percent"] is None


@pytest.mark.parametrize(
    "query",
    [
        "days=0",
        "days=91",
        "days=-1",
        "days=1.5",
        "days=",
        "days=%D9%A1",
        "days=1&days=2",
        "channel_id=a&channel_id=b",
        "role=platform_admin",
        "tenant_id=forged",
        "days=001",
    ],
)
async def test_analytics_closed_query_rejects_invalid_and_forged_filters(
    hosted: tuple[httpx.AsyncClient, Starlette],
    query: str,
) -> None:
    """Invalid windows, duplicate selectors and authority-like extras fail rather than coerce."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    response = await http.get(channel["tenant"] + "/analytics?" + query)
    assert response.status_code == 400 and response.json() == {"error": "validation"}


async def test_analytics_member_scope_and_empty_foreign_channel_filters(members: Members) -> None:
    """Server-side tenant membership precedes analytics filtering even when no data exists."""
    first = await _publish(members.first, members.tenants[0], "weather")
    foreign = await _publish(members.second, members.tenants[1], "inventory")
    own, other = ["/api/tenants/" + tenant for tenant in members.tenants]
    report = await _report(members.first, own, requests=0, runs=0, query="?days=90&channel_id=" + first["channel"])
    assert len(report["daily"]) == 90 and [item["channel_id"] for item in report["channels"]] == [first["channel"]]
    claims = {"x-tenant-id": members.tenants[1], "x-role": "platform_admin", "x-user-id": members.users[1].id}
    for path in [
        other + "/analytics",
        other + "/analytics?days=invalid",
        own + "/analytics?channel_id=" + foreign["channel"],
    ]:
        response = await members.first.get(path, headers=claims)
        assert response.status_code == 404 and response.json() == {"error": "not_found"}
    assert (await members.admin.get(own + "/analytics?channel_id=" + foreign["channel"])).status_code == 404
    assert (await members.admin.get(other + "/analytics")).status_code == 200
    async with httpx.AsyncClient(base_url=members.first.base_url) as anonymous:
        response = await anonymous.get(
            own + "/analytics", headers=claims | {"authorization": "Bearer " + first["token"]}
        )
        assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


async def test_analytics_utc_days_metadata_legacy_usage_and_disabled_history(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Only observed history is dated; first recording survives filtering and channel disablement."""
    http, app = hosted
    channel = await _channel(http, "weather")
    tenant = channel["tenant"].rsplit("/", 1)[-1]
    await app.state.store.record_usage(tenant, channel["id"], "execute_code", "success", 99)
    analytics: AnalyticsStore = app.state.analytics
    now = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    analytics._clock = lambda: now.timestamp()
    assert (await _report(http, channel["tenant"], requests=0, runs=0))["window"]["recording_since"] is None
    async with Client(channel["url"], auth=channel["token"]) as client:
        await _data(client, "execute_code", {"code": "result = 1", "description": "Day one"})
        await _report(http, channel["tenant"], requests=1, runs=1)
        now += timedelta(minutes=2)
        await _data(client, "execute_code", {"code": "result = 2", "description": "Day two"})
    report = await _report(http, channel["tenant"], requests=2, runs=2, query="?days=2")
    assert report["window"]["recording_since"] == "2026-01-01T23:59:00+00:00"
    assert [(day["date"], day["terminal_runs"]) for day in report["daily"]] == [("2026-01-01", 1), ("2026-01-02", 1)]
    today = await _report(http, channel["tenant"], requests=1, runs=1, query="?days=1&channel_id=" + channel["id"])
    assert today["window"]["start"] == "2026-01-02" and today["window"]["end"] == "2026-01-03"
    policy = {
        "name": "weather",
        "spec_ids": [channel["spec"]],
        "sandbox_mode": "restricted",
        "allowed_imports": [],
        "enabled": False,
    }
    assert (await http.patch(channel["path"], json=policy)).status_code == 200
    assert (await http.patch(channel["tenant"], json={"enabled": False})).status_code == 200
    assert await _report(http, channel["tenant"], query="?days=2") == report


async def test_analytics_unavailable_storage_never_replaces_actual_execution_response(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """Root-wired optional callbacks fail safely against a deliberately broken disposable analytics table."""
    http, app = hosted
    channel = await _channel(http, "weather")
    async with app.state.store._db.transaction():
        await app.state.store._db.execute("DROP TABLE saas_analytics_daily")
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(client, "execute_code", {"code": "result = 42", "description": "Telemetry unavailable"})
        receipt = await _data(client, "get_run", {"run_id": result["run_id"]})
    assert result["success"] and result["data"] == receipt["result"]["data"] == 42
    assert receipt["status"] == "succeeded" and (await http.get("/health")).status_code == 200
    response = await http.get(channel["tenant"] + "/analytics")
    assert response.status_code == 503 and response.json() == {"error": "storage_unavailable"}


async def test_analytics_terminal_precedes_traffic_and_receipt_redelivery_is_deduplicated(
    hosted: tuple[httpx.AsyncClient, Starlette],
) -> None:
    """The root callback commits terminal counters first and repeated delivery cannot add another run."""
    http, app = hosted
    channel = await _channel(http, "weather")
    async with Client(channel["url"], auth=channel["token"]) as client:
        result = await _data(client, "execute_code", {"code": "result = 42", "description": "Receipt order"})
    report = await _report(http, channel["tenant"], requests=1, runs=1)
    async with app.state.store._db.transaction():
        rows = await app.state.store._db.execute(
            "SELECT event_type,recorded_at FROM saas_analytics_receipts WHERE channel_id=?", (channel["id"],)
        )
    times = {row["event_type"]: row["recorded_at"] for row in rows}
    assert len(rows) == 2 and times["run"] <= times["request"]
    metrics = RunMetrics(run_id=result["run_id"], origin="execute", status="succeeded", sandbox_mode="restricted")
    analytics: AnalyticsStore = app.state.analytics
    assert not await analytics.record_run(channel["tenant"].rsplit("/", 1)[-1], channel["id"], metrics)
    await app.state.runtimes._runtimes[channel["id"]]._record_run(metrics)
    assert await _report(http, channel["tenant"], requests=1, runs=1) == report
    assert result["run_id"] not in json.dumps(report)


async def test_analytics_sdk_schema_error_is_traffic_not_a_run(hosted: tuple[httpx.AsyncClient, Starlette]) -> None:
    """Recognized tool requests rejected by real SDK validation count traffic but never admit a run."""
    http, _ = hosted
    channel = await _channel(http, "weather")
    async with Client(channel["url"], auth=channel["token"]) as client:
        response = await client.call_tool("execute_code", {}, raise_on_error=False)
        assert response.is_error
    summary = (await _report(http, channel["tenant"], requests=1, runs=0))["summary"]
    assert summary["request_failures"] == 1 and summary["request_errors"] == {"protocol": 1}
    assert summary["backend_starts"] == summary["structured_payload_bytes"] == 0
