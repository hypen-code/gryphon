"""Pydantic models for Gryphon — swagger/OpenAPI, functions, execution, and cache."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from gryphon.models.analytics import RunMetrics as RunMetrics
from gryphon.models.operation_policy import ReadOnlyPostOperation as ReadOnlyPostOperation
from gryphon.models.specifications import SaaSSpec as SaaSSpec
from gryphon.models.specifications import SpecDiagnostics as SpecDiagnostics
from gryphon.models.specifications import SpecImport as SpecImport
from gryphon.models.users import UserAccount as UserAccount
from gryphon.models.users import UserAudit as UserAudit
from gryphon.models.users import UserRole as UserRole

type RequestBodyMediaType = Literal["application/json", "application/x-www-form-urlencoded", "multipart/form-data"]

# ---------------------------------------------------------------------------
# Swagger / OpenAPI models (swagger.py namespace)
# ---------------------------------------------------------------------------


class ParamSchema(BaseModel):
    """Represents a single parameter to an API endpoint."""

    name: str
    location: str  # "query" | "path" | "header" | "body"
    param_type: str  # "string" | "integer" | "number" | "boolean" | "object" | "array"
    required: bool = False
    description: str = ""
    default: str | None = None
    enum: list[str] | None = None
    json_schema: dict[str, Any] = Field(default_factory=dict)


class ResponseField(BaseModel):
    """Represents a field in an API response schema."""

    name: str
    field_type: str
    description: str = ""
    required: bool = True  # Whether this field is required per swagger "required" array
    nested: list[ResponseField] | None = None  # 1 level only


class EndpointSpec(BaseModel):
    """Normalized representation of a single API endpoint."""

    path: str
    method: str
    operation_id: str
    summary: str
    description: str = ""
    parameters: list[ParamSchema] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    request_body_media_type: RequestBodyMediaType = "application/json"
    read_only_post: bool = False
    response_schema: list[ResponseField] = Field(default_factory=list)
    response_json_schema: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    base_url: str = ""  # Override base URL for this endpoint (from operation-level servers)


class ServerSpec(BaseModel):
    """Normalized representation of a complete API server from a swagger doc."""

    name: str
    description: str
    base_url: str
    is_read_only: bool
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    swagger_hash: str
    # Maps each distinct non-primary server URL to a module-level variable name
    # (e.g. "https://cdn.cse.lk" → "_BASE_URL_1").  Empty when the spec has only
    # one server and no operation/path-level overrides differ from the primary.
    server_url_vars: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Auth config models (discriminated union)
# ---------------------------------------------------------------------------


class StaticAuthConfig(BaseModel):
    """Static Authorization header value — passed through as-is (supports ${VAR} refs)."""

    type: Literal["static"] = "static"
    value: str = Field(repr=False)  # e.g. "Bearer glsa_xxx" or "Basic base64=="


class JwtAuthConfig(BaseModel):
    """Static JWT token — wrapped as 'Bearer <token>' at resolution time."""

    type: Literal["jwt"] = "jwt"
    token: str = Field(repr=False)  # raw JWT string; supports ${VAR} references


class OAuth2AuthConfig(BaseModel):
    """OAuth2 client credentials flow — token is fetched and cached automatically."""

    type: Literal["oauth2"] = "oauth2"
    token_url: str
    client_id: str
    client_secret: str = Field(repr=False)  # supports ${VAR} references
    scope: str = ""


class KeycloakAuthConfig(BaseModel):
    """Keycloak OIDC client credentials — token URL is built from base_url + realm."""

    type: Literal["keycloak"] = "keycloak"
    base_url: str  # e.g. https://keycloak.example.com/auth
    realm: str
    client_id: str
    client_secret: str = Field(repr=False)  # supports ${VAR} references
    scope: str = ""


class BasicAuthConfig(BaseModel):
    """HTTP Basic auth — base64-encodes username:password into the Authorization header."""

    type: Literal["basic"] = "basic"
    username: str = Field(repr=False)  # supports ${VAR} references
    password: str = Field(repr=False)  # supports ${VAR} references


class SessionAuthConfig(BaseModel):
    """Session-based auth — logs in and caches the session cookie or bearer token.

    Covers apps that use HTTP cookies for auth (e.g. JSESSIONID, PHPSESSID, custom
    session tokens). POSTs credentials to login_url; the response cookie (or a token
    field in the JSON body) is extracted and cached for expires_seconds.
    """

    type: Literal["session"] = "session"
    login_url: str  # POST endpoint to authenticate
    username: str = Field(repr=False)  # supports ${VAR} references
    password: str = Field(repr=False)  # supports ${VAR} references
    username_field: str = "username"  # request body field name for username
    password_field: str = "password"  # request body field name for password
    content_type: Literal["json", "form"] = "json"  # login request encoding
    cookie_name: str = ""  # extract a specific cookie; empty = collect all cookies
    token_field: str = ""  # if set, extract Bearer token from this JSON response field
    expires_seconds: int = 3600  # session TTL (cookies have no standard expiry header)


AuthConfig = Annotated[
    StaticAuthConfig | JwtAuthConfig | BasicAuthConfig | OAuth2AuthConfig | KeycloakAuthConfig | SessionAuthConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Swagger source config model
# ---------------------------------------------------------------------------


class SwaggerSource(BaseModel):
    """Configuration for a single swagger/OpenAPI source."""

    name: str
    swagger_url: str
    base_url: str = ""  # Optional: falls back to servers[].url in the OpenAPI spec
    auth_header: str = Field(default="", repr=False)  # legacy: static "Bearer ..." or "Basic ..." string
    auth: AuthConfig | None = None  # typed auth block (takes precedence over auth_header)
    is_read_only: bool = False
    extra_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    headers: str = Field(default="", repr=False)  # "[key1:value1,key2:value2]" format; parsed into extra_headers
    skills_url: str | None = None  # Optional: local path or HTTP URL to a skills.md document
    top_level_functions: list[str] = Field(
        default_factory=list
    )  # Optional: function names to expose as direct MCP tools

    @model_validator(mode="after")
    def _parse_headers(self) -> SwaggerSource:
        if self.headers:
            raw = self.headers.strip().strip("[]")
            for pair in raw.split(","):
                if ":" in pair:
                    k, _, v = pair.partition(":")
                    self.extra_headers[k.strip()] = v.strip()
        # Compat: promote legacy auth_header string to StaticAuthConfig
        if self.auth_header and self.auth is None:
            self.auth = StaticAuthConfig(value=self.auth_header)
        return self


# ---------------------------------------------------------------------------
# Function info models (function.py namespace)
# ---------------------------------------------------------------------------


class FunctionInfo(BaseModel):
    """Complete metadata for a compiled API function."""

    server_name: str
    function_name: str
    summary: str
    description: str = ""
    parameters: list[ParamSchema] = Field(default_factory=list)
    response_fields: list[ResponseField] = Field(default_factory=list)
    return_type: str = "Any"
    source_code: str
    method: str = ""
    path: str = ""
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ServerInfo(BaseModel):
    """Summary metadata for a compiled server (used in list_servers response)."""

    name: str
    description: str
    functions: list[str] = Field(default_factory=list)
    function_summaries: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Execution models (execution.py namespace)
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Result from sandboxed code execution."""

    success: bool
    data: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: str | None = None
    traceback: str | None = None  # Only populated in debug mode
    prints: str | None = None  # Captured stdout from print() calls in user code
    execution_time_ms: int = 0
    cache_id: str | None = None
    run_id: str | None = None
    error_type: str | None = None
    artifact_id: str | None = None
    truncated: bool = False
    tool_calls: int = 0


# ---------------------------------------------------------------------------
# Cache models (cache.py namespace)
# ---------------------------------------------------------------------------


class CacheEntry(BaseModel):
    """A single entry in the code cache."""

    id: str
    description: str
    code: str
    servers_used: list[str] = Field(default_factory=list)
    swagger_hash: str
    created_at: float
    last_used_at: float
    use_count: int = 1
    ttl_seconds: int = 3600
    owner: str = "local"
    input_schema: dict[str, Any] = Field(default_factory=dict)


class CacheSummary(BaseModel):
    """Compact cache entry for listing (without full code)."""

    id: str
    description: str
    servers_used: list[str]
    use_count: int
    created_at: float


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


class EndpointManifest(BaseModel):
    """Manifest entry for a single compiled endpoint."""

    function_name: str
    summary: str
    method: str
    path: str
    parameters_summary: str
    response_summary: str
    return_type: str = "Any"
    parameters: list[ParamSchema] = Field(default_factory=list)
    response_fields: list[ResponseField] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    request_body_media_type: RequestBodyMediaType = "application/json"
    read_only_post: bool = False
    base_url: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ServerManifest(BaseModel):
    """Compiled server manifest written to disk."""

    server_name: str
    description: str
    swagger_hash: str
    template_hash: str = ""
    compiled_at: str
    base_url: str
    is_read_only: bool
    endpoints: list[EndpointManifest] = Field(default_factory=list)
    format_version: int = 2


class ExecutionScope(BaseModel):
    """Server-owned authority and counters for a single execution."""

    run_id: str
    owner: str = "local"
    max_calls: int = 50
    calls: int = 0
    deadline: float
    cancelled: bool = False


class RunRecord(BaseModel):
    """Durable, owner-scoped execution receipt without raw credentials or inputs."""

    id: str
    owner: str = "local"
    request_hash: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"] = "queued"
    created_at: float
    updated_at: float
    result: ExecutionResult | None = None


class ArtifactRecord(BaseModel):
    """Metadata describing an owner-scoped, content-addressed execution result."""

    id: str
    owner: str
    size_bytes: int
    sha256: str
    media_type: str = "application/json"


class Tenant(BaseModel):
    """Administrative tenant identity; disabled tenants cannot authenticate."""

    id: str
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    created_at: float


class Channel(BaseModel):
    """Server-owned execution namespace and revisioned channel configuration."""

    id: str
    tenant_id: str
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    spec_ids: list[str] = Field(default_factory=list, max_length=100)
    sandbox_mode: Literal["restricted", "docker"] = "restricted"
    allowed_imports: list[str] = Field(default_factory=list, max_length=100)
    include_function_summaries: bool = Field(default=False, strict=True)
    revision: int = 1
    key_active: bool = False
    created_at: float


class ChannelUsage(BaseModel):
    """Aggregate counters with no request, code, result, or credential content."""

    channel_id: str
    tool: str
    status: Literal["success", "error"]
    calls: int
    latency_ms: float


AuditEvent = Literal[
    "tenant_created",
    "tenant_disabled",
    "tenant_enabled",
    "spec_created",
    "channel_created",
    "channel_updated",
    "key_rotated",
    "key_revoked",
]


class AdminAudit(BaseModel):
    """Static administrative event containing only server-owned identifiers."""

    id: str
    tenant_id: str
    channel_id: str | None = None
    event: AuditEvent
    created_at: float
