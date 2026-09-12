"""Import budgets match runtime compilation and bound the initial UCP profile fetch."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gryphon.errors import CompileError, ExecutionError, InputValidationError
from gryphon.saas_spec_import import SpecImporter
from gryphon.saas_upload import inspect_upload

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


async def test_hosted_import_respects_smaller_runtime_document_limit(gryphon_config: GryphonConfig) -> None:
    """An upload cannot succeed with document bytes the runtime is configured to reject."""
    gryphon_config.max_spec_size_bytes = 1024
    importer = SpecImporter(gryphon_config, 8192)
    assert importer.limit == 1024
    with pytest.raises(InputValidationError):
        await importer.load("api", content=" " * 1025)


async def test_ucp_initial_fetch_uses_adapter_budget_before_reading_profile(gryphon_config: GryphonConfig) -> None:
    """The first fetch cannot spend the larger hosted quota before adapter checks run."""
    gryphon_config.max_spec_size_bytes = 16 * 1024 * 1024
    request = AsyncMock(side_effect=ExecutionError("bounded"))
    with patch("gryphon.saas_spec_import.NetworkClient.request", request), pytest.raises(CompileError):
        await SpecImporter(gryphon_config, 16 * 1024 * 1024).load("shop", url="https://example.com", kind="ucp")
    assert request.call_args.kwargs["max_bytes"] == 5 * 1024 * 1024
    assert request.call_args.kwargs["headers"] == {"Accept": "application/json"}


@pytest.mark.parametrize("profile", [b"not JSON", b"[]", b"null"])
async def test_ucp_malformed_profiles_never_reach_schema_discovery(
    gryphon_config: GryphonConfig, profile: bytes
) -> None:
    """Require strict JSON objects and never discover schemas from malformed input."""
    with (
        patch(
            "gryphon.saas_spec_import.NetworkClient.request",
            AsyncMock(return_value=httpx.Response(200, content=profile)),
        ),
        patch("gryphon.compiler.ucp_discovery.profile_to_openapi", AsyncMock()) as adapter,
        pytest.raises(CompileError),
    ):
        await SpecImporter(gryphon_config, 8192).load("shop", url="https://example.com", kind="ucp")
    adapter.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"paths": []},
        {"servers": "invalid"},
        {"servers": [1]},
        {"servers": [{"url": 42}]},
    ],
)
async def test_relative_server_normalization_rejects_malformed_contexts(
    gryphon_config: GryphonConfig, changes: dict[str, object]
) -> None:
    """Resolve only valid server arrays and strings, retaining closed import failure behavior."""
    document = {"openapi": "3.0.3", "info": {"title": "API", "version": "1"}, "paths": {}} | changes
    with pytest.raises(InputValidationError):
        await inspect_upload(json.dumps(document), gryphon_config, 8192, "api", origin="https://example.com/spec")


async def test_remote_default_and_relative_operation_servers_use_document_origin(gryphon_config: GryphonConfig) -> None:
    """Root defaults and operation overrides are snapshotted as explicit absolute origins."""
    document = {
        "openapi": "3.0.3",
        "info": {"title": "API", "version": "1"},
        "paths": {"/data": {"get": {"operationId": "data", "servers": [{"url": "../api"}], "responses": {}}}},
    }
    imported = await inspect_upload(
        json.dumps(document), gryphon_config, 8192, "api", origin="https://example.com/docs/spec.json"
    )
    assert imported.document["servers"] == [{"url": "https://example.com/"}]
    assert imported.document["paths"]["/data"]["get"]["servers"] == [{"url": "https://example.com/api"}]
