"""Opt-in shipped hosted deployment; only unique disposable Compose resources are touched."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
import yaml
from fastmcp import Client
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ROOT = Path(__file__).resolve().parents[2]
LABEL = "io.gryphon.hosted-integration"
BUILD_TIMEOUT = 900
START_TIMEOUT = 180


def _object(value: object) -> dict[str, object]:
    """Validate decoded objects without printing their potentially sensitive contents."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        pytest.fail("Expected a JSON object", pytrace=False)
    return cast("dict[str, object]", value)


def _items(value: object) -> list[object]:
    """Validate decoded lists without including response values in diagnostics."""
    if not isinstance(value, list):
        pytest.fail("Expected a JSON list", pytrace=False)
    return cast("list[object]", value)


def _text(value: object) -> str:
    """Require strings without exposing credential-bearing response objects."""
    if not isinstance(value, str):
        pytest.fail("Expected a string", pytrace=False)
    return value


def _failure(output: str) -> str:
    """Classify captured Docker diagnostics; never emit raw build, daemon or container logs."""
    for pattern, category in (
        ("No matching distribution found for uv==0.12.9", "pinned uv==0.12.9 unavailable from build package index"),
        ("Temporary failure in name resolution", "build registry DNS resolution failed"),
        ("No matching distribution found", "pinned Python distribution unavailable"),
        ("manifest unknown", "pinned registry manifest unavailable"),
        ("not found", "required image, package or executable unavailable"),
        ("permission denied", "Docker or filesystem permission denied"),
        ("Cannot connect", "Docker daemon unavailable"),
    ):
        if pattern.lower() in output.lower():
            return category
    return "Docker command failed (raw diagnostics withheld)"


@dataclass
class _Deployment:
    """Keep generated secrets out of object representations and Compose source files."""

    project: str
    directory: Path
    origin: str
    environment: dict[str, str] = field(repr=False)
    phase: str = "Preflight"

    @property
    def image(self) -> str:
        """Return a test-only tag, never the shipped operator image tag."""
        return f"gryphon-hosted-test:{self.project}"

    async def docker(self, *arguments: str, timeout: int = START_TIMEOUT) -> str:
        """Capture both output streams and replace failures with static safe categories."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", *arguments],
                cwd=self.directory,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pytest.fail("Docker invocation unavailable or timed out; diagnostics withheld", pytrace=False)
        if result.returncode:
            pytest.fail(f"{self.phase}: {_failure(result.stdout + result.stderr)}", pytrace=False)
        return result.stdout.strip()

    async def compose(self, *arguments: str, timeout: int = START_TIMEOUT) -> str:
        """Always select the temporary public definition and an explicit unique project."""
        return await self.docker(
            "compose",
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(self.directory),
            "--project-name",
            self.project,
            "--file",
            str(self.directory / "compose.json"),
            "--profile",
            "hosted",
            *arguments,
            timeout=timeout,
        )

    async def resources(self, kind: str) -> str:
        """Query only the exact random project label, never enumerate operator resources."""
        options = ["--all"] if kind == "container" else []
        return await self.docker(
            kind, "ls", "-q", *options, "--filter", f"label=com.docker.compose.project={self.project}"
        )

    async def cleanup(self) -> None:
        """Remove this project's containers/volumes/networks; never prune or remove shared images."""
        await self.compose("down", "--volumes", "--timeout", "15")
        for kind in ("container", "network", "volume"):
            assert not await self.resources(kind), "Disposable project cleanup was incomplete"
        images = await self.docker("image", "ls", "-q", "--filter", f"reference={self.image}")
        if images:
            await self.docker("image", "rm", self.image)


def _definition(deployment: _Deployment) -> None:
    """Copy only public Compose data; drop inactive legacy mounts and isolate every resource name."""
    document = _object(yaml.safe_load((ROOT / "docker-compose.yml").read_text()))
    services = _object(document["services"])
    services.pop("gryphon")
    hosted = _object(services["gryphon-hosted"])
    hosted["build"] = {"context": str(ROOT), "dockerfile": "Dockerfile"}
    hosted["image"] = deployment.image
    port = deployment.origin.rsplit(":", 1)[1]
    hosted["ports"] = [f"127.0.0.1:{port}:8000"]
    for name, service in services.items():
        entry = _object(service)
        assert "env_file" not in entry
        entry["container_name"] = f"{deployment.project}-{name}"
        entry["labels"] = {LABEL: deployment.project}
        entry["restart"] = "no"
    for kind, names in (
        ("networks", ("default", "hosted_database")),
        ("volumes", ("gryphon_hosted_data", "gryphon_postgres_data")),
    ):
        original = _object(document[kind]) if kind in document else {}
        document[kind] = {
            name: {
                **_object(original.get(name) or {}),
                "name": f"{deployment.project}-{name}",
                "labels": {LABEL: deployment.project},
            }
            for name in names
        }
    (deployment.directory / "compose.json").write_text(json.dumps(document))


@pytest.fixture
async def deployment(tmp_path: Path) -> AsyncIterator[_Deployment]:
    """Build the exact shipped Dockerfile without ambient dotenv or preexisting Compose resources."""
    if os.environ.get("GRYPHON_TEST_HOSTED_DOCKER") != "1":
        pytest.skip("Set GRYPHON_TEST_HOSTED_DOCKER=1 for the disposable hosted deployment")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{reservation.getsockname()[1]}"
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("GRYPHON_", "COMPOSE_"))}
    environment.update(
        {
            "GRYPHON_SAAS_ADMIN_TOKEN": secrets.token_urlsafe(48),
            "GRYPHON_POSTGRES_PASSWORD": secrets.token_hex(32),
            "GRYPHON_SAAS_PUBLIC_ORIGIN": origin,
            "GRYPHON_SAAS_ALLOW_INSECURE_HTTP": "true",
        }
    )
    instance = _Deployment(f"gryphon-hosted-test-{uuid4().hex}", tmp_path, origin, environment)
    _definition(instance)
    for kind in ("container", "network", "volume"):
        assert not await instance.resources(kind), "Refusing a preexisting project"
    assert not await instance.docker("image", "ls", "-q", "--filter", f"reference={instance.image}")
    try:
        instance.phase = "PostgreSQL startup"
        await instance.compose("up", "--detach", "--wait", "--wait-timeout", "120", "postgres")
        await _postgres(instance)
        instance.phase = "Image build (PostgreSQL UID999 initialization verified)"
        await instance.compose("build", "gryphon-hosted", timeout=BUILD_TIMEOUT)
        instance.phase = "Hosted startup"
        await instance.compose("up", "--detach", "--wait", "--wait-timeout", "150", "gryphon-hosted")
        instance.phase = "Hosted workflow"
        yield instance
    finally:
        instance.phase = "Owned resource cleanup"
        await instance.cleanup()


async def _hardening(deployment: _Deployment) -> None:
    """Inspect only non-secret fields; prove UID-999 PostgreSQL initialized its fresh named volume."""
    template = (
        '{"user":{{json .Config.User}},"host":{{json .HostConfig}},'
        '"mounts":{{json .Mounts}},"health":{{json .State.Health.Status}},'
        '"labels":{{json .Config.Labels}}}'
    )
    for service, uid in (("gryphon-hosted", "1000:1000"), ("postgres", "999:999")):
        name = f"{deployment.project}-{service}"
        info = _object(json.loads(await deployment.docker("inspect", "--format", template, name)))
        host = _object(info["host"])
        assert info["user"] == uid and info["health"] == "healthy"
        assert _object(info["labels"])[LABEL] == deployment.project
        assert host["ReadonlyRootfs"] and not host["Privileged"]
        assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
        assert "no-new-privileges:true" in _items(host["SecurityOpt"])
        assert not host["Devices"] and host["PidMode"] != "host"
        assert isinstance(host["PidsLimit"], int) and host["PidsLimit"] > 0
        assert isinstance(host["Memory"], int) and host["Memory"] > 0
        mounts = [_object(item) for item in _items(info["mounts"])]
        assert all(item["Type"] in {"volume", "tmpfs"} for item in mounts)
        assert all(
            _text(item["Name"]).startswith(deployment.project + "-") for item in mounts if item["Type"] == "volume"
        )
        assert all("docker.sock" not in _text(item["Destination"]) for item in mounts)
        if service == "postgres":
            assert not host["PortBindings"]
            assert any(item["Destination"] == "/var/lib/postgresql/data" for item in mounts)
        else:
            bindings = _items(_object(host["PortBindings"])["8000/tcp"])
            assert len(bindings) == 1 and _object(bindings[0])["HostIp"] == "127.0.0.1"
    assert (
        await deployment.docker(
            "network", "inspect", "--format", "{{.Internal}}", f"{deployment.project}-hosted_database"
        )
        == "true"
    )
    await deployment.compose(
        "exec",
        "-T",
        "gryphon-hosted",
        "python",
        "-c",
        "import os, pathlib, gryphon; assert os.getuid() == 1000; assert '/.venv/' in gryphon.__file__; "
        "assert not pathlib.Path('/var/run/docker.sock').exists(); assert not pathlib.Path('/app/.env').exists()",
    )
    await _postgres(deployment)


async def _postgres(deployment: _Deployment) -> None:
    """Prove readiness and fresh-volume ownership independently of application image availability."""
    assert await deployment.compose("exec", "-T", "postgres", "id", "-u") == "999"
    assert (
        await deployment.compose("exec", "-T", "postgres", "stat", "-c", "%u", "/var/lib/postgresql/data/PG_VERSION")
        == "999"
    )
    assert (
        await deployment.compose(
            "exec", "-T", "postgres", "psql", "-U", "gryphon", "-d", "gryphon", "-Atc", "SHOW server_version_num"
        )
        == "170006"
    )


async def _post(http: httpx.AsyncClient, path: str, body: dict[str, object], status: int) -> dict[str, object]:
    """Validate HTTP success without logging response bodies, cookies or generated keys."""
    response = await http.post(path, json=body)
    assert response.status_code == status
    return _object(response.json())


async def _admin(http: httpx.AsyncClient, deployment: _Deployment) -> tuple[str, str, SecretStr]:
    """Use the browser API for actual login, CSRF, public YAML upload, channel creation and key issuance."""
    assert (await http.get("/api/settings")).status_code == 401
    login = await _post(http, "/api/login", {"token": deployment.environment["GRYPHON_SAAS_ADMIN_TOKEN"]}, 200)
    assert (await http.post("/api/tenants", json={"name": "denied"})).status_code == 403
    http.headers["x-csrf-token"] = _text(login["csrf_token"])
    settings = _object((await http.get("/api/settings")).json())
    assert settings["docker_enabled"] is False
    tenant = await _post(http, "/api/tenants", {"name": "Disposable hosted test"}, 201)
    prefix = "/api/tenants/" + _text(tenant["id"])
    spec = await _post(
        http,
        prefix + "/specs",
        {
            "name": "weather",
            "content": (ROOT / "examples/weather.yaml").read_text(),
        },
        201,
    )
    channel = await _post(
        http,
        prefix + "/channels",
        {
            "name": "offline",
            "spec_ids": [spec["id"]],
            "sandbox_mode": "restricted",
            "allowed_imports": [],
        },
        201,
    )
    key = await _post(http, prefix + "/channels/" + _text(channel["id"]) + "/rotate", {}, 200)
    return prefix, deployment.origin + _text(key["endpoint"]), SecretStr(_text(key["token"]))


async def _mcp(endpoint: str, token: SecretStr) -> None:
    """Discover the uploaded weather catalog and execute/replay actual Monty code without upstream calls."""
    async with Client(endpoint, auth=token.get_secret_value()) as client:
        assert len(await client.list_tools()) == 10
        assert client.server_capabilities is not None and client.server_capabilities.tasks is None
        catalog = _object((await client.call_tool("list_servers")).structured_content)
        assert [_object(item)["name"] for item in _items(catalog["servers"])] == ["weather"]
        details = _object(
            (
                await client.call_tool(
                    "get_functions",
                    {
                        "functions": [{"server_name": "weather", "function_name": "get_forecast"}],
                    },
                )
            ).structured_content
        )
        function = _object(_items(details["functions"])[0])
        assert _object(function["invocation"])["capability"] == "weather.get_forecast"
        executed = _object(
            (
                await client.call_tool(
                    "execute_code",
                    {
                        "code": 'result = inputs["n"] * 2',
                        "description": "Offline double",
                        "inputs": {"n": 21},
                    },
                )
            ).structured_content
        )
        assert executed["success"] and executed["data"] == 42
        replayed = _object(
            (
                await client.call_tool(
                    "run_cached_code",
                    {
                        "cache_id": executed["cache_id"],
                        "params": {"n": 4},
                    },
                )
            ).structured_content
        )
        assert replayed["success"] and replayed["data"] == 8


async def _assets(http: httpx.AsyncClient) -> None:
    """Require wheel-packaged HTML, JavaScript and CSS with an external-assets-only CSP."""
    for path, content_type in (
        ("/", "text/html"),
        ("/static/admin.js", "javascript"),
        ("/static/admin.css", "text/css"),
    ):
        response = await http.get(path)
        assert response.status_code == 200 and response.content
        assert content_type in response.headers["content-type"]
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy and "script-src 'self'" in policy
        assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
        assert response.headers["x-content-type-options"] == "nosniff"
        if path == "/":
            assert "/static/admin.js" in response.text and "/static/admin.css" in response.text


async def test_shipped_hosted_container_workflow(deployment: _Deployment) -> None:
    """Verify production Dockerfile/Compose, real PostgreSQL and HTTP/MCP without operator resources."""
    await _hardening(deployment)
    async with httpx.AsyncClient(base_url=deployment.origin, trust_env=False, timeout=30) as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/health", headers={"Host": "untrusted.example"})).status_code == 400
        await _assets(http)
        prefix, endpoint, token = await _admin(http, deployment)
        await _mcp(endpoint, token)
        usage = _object((await http.get(prefix + "/usage")).json())
        counts = {
            (_object(item)["tool"], _object(item)["status"]): _object(item)["calls"] for item in _items(usage["items"])
        }
        assert counts["execute_code", "success"] == 1
        assert counts["run_cached_code", "success"] == 1
