"""Hosted revision races, shared VM admission, dotted imports and environment-only CLI regressions."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from gryphon.__main__ import _build_parser, _cmd_saas, _config_for, main
from gryphon.config import GryphonConfig
from gryphon.errors import SecurityViolationError
from gryphon.models import ExecutionResult
from gryphon.saas import create_app
from gryphon.saas_config import SaaSConfig
from gryphon.saas_store import TOOLS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.applications import Starlette

    from gryphon.models import Channel, ExecutionScope

POLICY = {"name": "Compute", "spec_ids": [], "sandbox_mode": "restricted", "allowed_imports": []}


@pytest.fixture
async def hosted(
    gryphon_config: GryphonConfig, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, Starlette, str]]:
    """Own an actual app, administrator session and key-bearing channel using temporary stores only."""
    config = SaaSConfig(
        admin_token=SecretStr(secrets.token_urlsafe(48)),
        database_url=SecretStr("sqlite:///:memory:"),
        public_origin="https://testserver",
        state_dir=tmp_path / "hosted",
        docker_enabled=True,
    )
    base = gryphon_config.model_copy(update={"max_concurrent_executions": 1})
    app = create_app(config, base)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=config.public_origin,
        ) as http,
    ):
        login = await http.post("/api/login", json={"token": config.admin_token.get_secret_value()})
        assert login.status_code == 200
        http.headers["x-csrf-token"] = login.json()["csrf_token"]
        tenant = await app.state.store.create_tenant("Tenant")
        channel = await app.state.store.create_channel(tenant.id, "Compute")
        await app.state.store.rotate_key(tenant.id, channel.id)
        yield http, app, f"/api/tenants/{tenant.id}/channels/{channel.id}"


async def _call(http: httpx.AsyncClient, channel: Channel, token: str, tool: str) -> httpx.Response:
    """Send a genuine stateless MCP tool request through the hosted authentication gateway."""
    arguments = {"code": "result = 42", "description": "Compute"} if tool == "execute_code" else {}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    return await http.post(
        f"/mcp/{channel.id}",
        json=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        },
    )


@pytest.mark.parametrize("mutation", ["channel", "tenant", "rotate", "revoke"])
async def test_saas_committed_revision_survives_delayed_api_invalidation(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
    mutation: str,
) -> None:
    """Every mutation cutoff preserves a new runtime initialized between its DB commit and invalidation."""
    http, app, path = hosted
    manager, store = app.state.runtimes, app.state.store
    tenant_id, channel_id = path.split("/")[3], path.split("/")[-1]
    previous = await store.get_channel(tenant_id, channel_id)
    async with manager.acquire(previous, []):
        old_runtime = manager._runtimes[channel_id]
    original, revisions = manager.invalidate, []

    async def interleaved(id: str, before_revision: int | None = None) -> None:
        """Initialize the just-committed revision before letting administrative invalidation proceed."""
        committed = await store.get_channel(tenant_id, id)
        revisions.append(committed.revision)
        assert before_revision == committed.revision > previous.revision
        async with manager.acquire(committed, []):
            fresh = manager._runtimes[id]
        await original(id, before_revision=before_revision)
        assert manager._runtimes.get(id) is fresh and fresh.verifier.active

    with patch.object(manager, "invalidate", side_effect=interleaved):
        if mutation == "channel":
            response = await http.patch(path, json={**POLICY, "enabled": True})
        elif mutation == "tenant":
            response = await http.patch(path.split("/channels")[0], json={"enabled": True})
        else:
            response = await http.post(path + "/" + mutation)
    assert response.status_code == 200 and len(revisions) == 1
    assert not old_runtime.verifier.active
    current = await store.get_channel(tenant_id, channel_id)
    async with manager.acquire(current, []):
        assert manager._runtimes[channel_id].channel.revision == revisions[0]


class _SlowSandbox:
    """Measure concurrent backend entries while one real hosted semaphore controls admission."""

    def __init__(self) -> None:
        """Keep deterministic entry and release signals instead of relying on arbitrary sleeps."""
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.active = self.maximum = self.calls = 0

    async def run(self, code: str, inputs: dict[str, object], scope: ExecutionScope) -> ExecutionResult:
        """Hold admitted work until released and count live VMs even on failed test cleanup."""
        self.calls += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return ExecutionResult(success=True, data=42)
        finally:
            self.active -= 1


async def test_saas_manager_injects_one_global_execution_slot_into_two_channels(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
) -> None:
    """Real independently constructed runtimes cannot multiply configured global VM concurrency."""
    http, app, path = hosted
    store, manager = app.state.store, app.state.runtimes
    tenant_id = path.split("/")[3]
    channels = [
        await store.get_channel(tenant_id, path.split("/")[-1]),
        await store.create_channel(tenant_id, "Second"),
    ]
    tokens = [await store.rotate_key(tenant_id, channel.id) for channel in channels]
    slots, probe = manager._execution_slots, _SlowSandbox()
    waiting = asyncio.Event()
    acquire, attempts = slots.acquire, 0

    async def observed() -> bool:
        """Signal the second global admission attempt before it can enter its backend."""
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            waiting.set()
        return bool(await acquire())

    with (
        patch.object(slots, "acquire", side_effect=observed),
        patch(
            "gryphon.runtime.sandboxes.RestrictedSandbox.run",
            side_effect=probe.run,
        ),
    ):
        first = asyncio.create_task(_call(http, channels[0], tokens[0], "execute_code"))
        tasks = [first]
        try:
            await asyncio.wait_for(probe.entered.wait(), 5)
            tasks.append(asyncio.create_task(_call(http, channels[1], tokens[1], "execute_code")))
            await asyncio.wait_for(waiting.wait(), 5)
            assert probe.calls == probe.maximum == 1 and slots.locked()
        finally:
            probe.release.set()
            responses = await asyncio.gather(*tasks)
        assert all(response.json()["result"]["structuredContent"]["success"] for response in responses)
        assert probe.calls == 2 and probe.maximum == 1 and probe.active == 0
        assert not slots.locked()
        assert (await _call(http, channels[0], tokens[0], "execute_code")).status_code == 200


@pytest.mark.parametrize("module,status", [("collections.abc", 201), ("os.path", 400), ("collections.evil", 400)])
async def test_saas_api_dotted_import_selection_preserves_exact_allowlist(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
    module: str,
    status: int,
) -> None:
    """Reviewed dotted imports round-trip through storage without allowing sibling or parent capabilities."""
    http, app, path = hosted
    prefix = path.rsplit("/", 1)[0]
    response = await http.post(prefix, json={**POLICY, "sandbox_mode": "docker", "allowed_imports": [module]})
    assert response.status_code == status
    if status == 201:
        saved = await app.state.store.get_channel(path.split("/")[3], response.json()["id"])
        assert saved.allowed_imports == [module]
        listing = (await http.get(prefix)).json()["items"]
        assert any(item["id"] == saved.id and item["allowed_imports"] == [module] for item in listing)
    else:
        assert response.json() == {"error": "validation"}
    assert not app.state.runtimes._runtimes


@pytest.mark.parametrize("explicit", [False, True])
def test_saas_cli_config_ignores_ambient_dotenv_but_honors_explicit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    """Hosted CLI defaults never discover .env; explicitly selected configuration remains supported."""
    ambient, selected = tmp_path / ".env", tmp_path / "selected.env"
    ambient.write_text("GRYPHON_MAX_TOOL_CALLS=13\n", encoding="utf-8")
    selected.write_text("GRYPHON_MAX_TOOL_CALLS=47\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRYPHON_MAX_TOOL_CALLS", raising=False)
    monkeypatch.setitem(GryphonConfig.model_config, "env_file", str(ambient))
    args = _build_parser().parse_args(["saas", *(["--env-file", str(selected)] if explicit else [])])
    config = _config_for(args)
    expected = 47 if explicit else GryphonConfig.model_fields["max_tool_calls"].default
    assert args.command == "saas" and config.max_tool_calls == expected
    monkeypatch.setenv("GRYPHON_MAX_TOOL_CALLS", "99")
    assert _config_for(args) is config and config.max_tool_calls == expected


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Install only freshly generated test credentials and temporary environment-only hosted settings."""
    token = secrets.token_urlsafe(48)
    for name, value in {
        "ADMIN_TOKEN": token,
        "DATABASE_URL": "sqlite:///:memory:",
        "PUBLIC_ORIGIN": "https://testserver",
        "STATE_DIR": str(tmp_path / "hosted"),
    }.items():
        monkeypatch.setenv("GRYPHON_SAAS_" + name, value)
    return token


async def test_saas_cli_passes_environment_config_and_isolated_base_to_serve(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The saas command delegates once with validated settings and propagates the server exit code."""
    token = _environment(monkeypatch, tmp_path)
    args = _build_parser().parse_args(["saas"])
    args._config = gryphon_config
    with patch("gryphon.saas.serve", new=AsyncMock(return_value=7)) as serve:
        assert await _cmd_saas(args) == 7
    serve.assert_awaited_once()
    assert serve.await_args is not None
    config, base = serve.await_args.args
    assert isinstance(config, SaaSConfig) and base is gryphon_config
    assert secrets.compare_digest(config.admin_token.get_secret_value(), token)
    assert config.database_url.get_secret_value() == "sqlite:///:memory:"
    assert config.public_origin == "https://testserver" and config.state_dir == tmp_path / "hosted"


@pytest.mark.parametrize("failure", ["settings", "serve"])
def test_saas_cli_failure_logs_are_generic_and_never_expose_tokens(
    gryphon_config: GryphonConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """Neither Pydantic input diagnostics nor backend exceptions may reveal hosted credentials."""
    token = _environment(monkeypatch, tmp_path)
    if failure == "settings":
        monkeypatch.setenv("GRYPHON_SAAS_PORT", token)
    monkeypatch.setattr("sys.argv", ["gryphon", "saas"])
    with (
        capture_logs() as logs,
        patch("gryphon.__main__._config_for", return_value=gryphon_config),
        patch(
            "gryphon.__main__.setup_logging",
        ),
        patch("gryphon.__main__.load_dotenv") as dotenv,
        patch(
            "gryphon.saas.serve",
            new=AsyncMock(side_effect=RuntimeError(token)),
        ) as serve,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()
    assert exit_info.value.code == 1
    dotenv.assert_not_called()
    assert serve.await_count == (1 if failure == "serve" else 0)
    assert logs == [
        {
            "event": "command_failed",
            "action": "Check configuration and local resources; details withheld",
            "log_level": "error",
        }
    ]
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err + json.dumps(logs)
    assert not captured.out


async def test_saas_usage_includes_all_pages_without_exposing_request_data(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
) -> None:
    """Eleven channels with ten aggregate tool rows each cannot be silently truncated to one page."""
    http, app, path = hosted
    store, tenant_id = app.state.store, path.split("/")[3]
    tools = sorted(TOOLS)[:10]
    channel_ids = set()
    for number in range(11):
        channel = await store.create_channel(tenant_id, f"Usage {number}")
        channel_ids.add(channel.id)
        for tool in tools:
            await store.record_usage(tenant_id, channel.id, tool, "success", 2.5)
    with patch.object(store, "list_usage", wraps=store.list_usage) as pages:
        response = await http.get(path.split("/channels")[0] + "/usage")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert len(rows) == sum(row["calls"] for row in rows) == 110
    assert sum(row["total_ms"] for row in rows) == 275
    assert {row["channel_id"] for row in rows} == channel_ids
    assert {(row["channel_id"], row["tool"]) for row in rows} == {
        (channel_id, tool) for channel_id in channel_ids for tool in tools
    }
    assert all(set(row) == {"channel_id", "tool", "status", "calls", "total_ms"} for row in rows)
    assert [call.kwargs["offset"] for call in pages.await_args_list] == [0, 100]


async def test_saas_runtime_startup_creates_private_channel_storage(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
) -> None:
    """The channel leaf is owner-only before normal SQLite stores become visible."""
    _, app, path = hosted
    channel = await app.state.store.get_channel(path.split("/")[3], path.split("/")[-1])
    root = app.state.admin.config.state_dir / "channels" / channel.id
    assert not root.exists()
    async with app.state.runtimes.acquire(channel, []):
        metadata = root.stat()
        assert stat.S_IMODE(metadata.st_mode) == 0o700 and metadata.st_uid == os.getuid()
        assert (root / "cache.db").is_file() and (root / "runs.db").is_file()


@pytest.mark.parametrize("failure", ["group", "other", "public", "owner"])
async def test_saas_runtime_insecure_storage_fails_before_creating_files(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
    failure: str,
) -> None:
    """Existing group/other-readable or foreign-owned leaves cannot receive private tenant state."""
    _, app, path = hosted
    channel = await app.state.store.get_channel(path.split("/")[3], path.split("/")[-1])
    root = app.state.admin.config.state_dir / "channels" / channel.id
    root.mkdir(parents=True, mode=0o700)
    root.chmod({"group": 0o750, "other": 0o705, "public": 0o755, "owner": 0o700}[failure])
    expected_uid = os.getuid() + (1 if failure == "owner" else 0)
    with (
        patch("gryphon.saas_catalog.os.getuid", return_value=expected_uid),
        patch(
            "gryphon.saas_runtime.compile_catalog",
            new=AsyncMock(),
        ) as compile_catalog,
    ):
        with pytest.raises(SecurityViolationError):
            async with app.state.runtimes.acquire(channel, []):
                pytest.fail("An insecure channel directory became ready")
        compile_catalog.assert_not_awaited()
    assert list(root.iterdir()) == []
    assert not app.state.runtimes._runtimes


async def test_saas_usage_pagination_has_a_hard_aggregate_bound(
    hosted: tuple[httpx.AsyncClient, Starlette, str],
) -> None:
    """Even a backend returning endless full pages cannot exceed the 2400-row authority bound."""
    http, app, path = hosted
    store, tenant_id = app.state.store, path.split("/")[3]
    await store.record_usage(tenant_id, path.split("/")[-1], "execute_code", "success", 1)
    page = (await store.list_usage(tenant_id)) * 100
    with patch.object(store, "list_usage", new=AsyncMock(return_value=page)) as pages:
        response = await http.get(path.split("/channels")[0] + "/usage")
    assert response.status_code == 200 and len(response.json()["items"]) == 2400
    assert [call.kwargs["offset"] for call in pages.await_args_list] == list(range(0, 2400, 100))
