# AGENTS.md — Gryphon Development Guide

Read this entire file before changing the repository. It governs development,
maintenance, and review of **Gryphon 2.0.0**. Read each existing file in full
before editing it, and inspect the implementation before documenting behavior.

## 1. Purpose and scope

Gryphon is the legendary guardian between agent-generated programs and API capabilities. Preserve the original API-agent-backend design: compile OpenAPI, discover a small amount of metadata, inspect needed operations, execute bounded code, and reuse recipes. Do not replace the meta-tool interface with one tool per endpoint.

The distribution is `gryphon-runtime`, the package/CLI is `gryphon`, and settings use `GRYPHON_`. Checkout/local-wheel installation works without a claimed PyPI release. Only after verified publication document public-index installation as available. Keep `https://github.com/hypen-code/gryphon`.
Release workflow `.github/workflows/publish.yml` accepts published releases or explicit version-tag dispatch, never branch pushes; tag `vX.Y.Z` must match both package versions. Keep full quality gates and wheel smoke checks before the separate OIDC publish job. PyPI publisher identity is `hypen-code` / `gryphon` / `publish.yml` / `pypi`, project `gryphon-runtime`; maintainers configure protected-environment reviewers. Never commit publishing tokens or automatically commit/tag/push/publish.

Deployments include **local stdio, operator-token HTTP, and admin-managed hosted tenants/channels**. Hosted mode has exactly one active worker per control database, enforced by a lease; it is not horizontal/HA SaaS, billing, SSO, or user invitations. PostgreSQL holds control metadata, key hashes, aggregate usage, and audit only; per-channel recipes/receipts/artifacts remain on private local persistent storage. Persistent receipts are not exactly-once effects or a resumable workflow engine.

## 2. Locked technology and reproducibility

| Concern | Decision |
|---|---|
| Python | 3.13+; `from __future__ import annotations` |
| MCP | FastMCP exactly 4.0.2; SDK-managed protocol negotiation |
| Protocol | MCP 2026-07-28 plus tested legacy initialization |
| Default execution | `pydantic-monty` exactly 0.0.18, restricted Python |
| Optional execution | Offline per-run Docker via `aiodocker`; `runsc` default |
| Configuration | `GryphonConfig`, pydantic-settings, `SecretStr` for HTTP token |
| HTTP | Broker-owned `httpx`, DNS pinning, verified origin-specific pools |
| State | SQLite/`aiosqlite` recipes/receipts and private artifacts; hosted PostgreSQL control plane only |
| Hosted dependencies | `saas` extra; pure `psycopg==3.2.9` requires system libpq |
| Compiler | Deterministic manifests and Jinja2 SDK documentation |
| Logging | `structlog`, structured safe metadata to stderr |
| Quality | Ruff, strict mypy, pytest/pytest-asyncio, mandatory coverage gate |

`pyproject.toml` and `uv.lock` define the environment. Update both deliberately
when dependencies change; never resolve opportunistically in CI or hooks.
Do not introduce dependencies without declaring and reviewing them.
The retained LLM-enhancement compatibility surface is retired: enabling it
must fail explicitly, not call a provider or silently change compilation.

## 3. Repository map and authority

| Location | Responsibility |
|---|---|
| `src/gryphon/__main__.py` | CLI parsing, env selection, transport preflight, lifecycle |
| `src/gryphon/cli_setup.py` | Environment-only stdio, private source-scoped user state |
| `src/gryphon/saas.py`, `saas_config.py` | Single-worker hosted lifecycle and explicit operator settings |
| `src/gryphon/saas_api.py`, `saas_auth.py`, `saas_http.py` | Browser sessions, CSRF, bounded HTTP and UI API |
| `src/gryphon/saas_user_api.py`, `saas_access.py`, `saas_users.py`, `saas_passwords.py` | Platform/user authorization, revisioned accounts, bounded salted password hashing |
| `src/gryphon/saas_store.py`, `saas_database.py`, `saas_resource_delete.py`, `saas_resource_delete_api.py` | Control metadata, quotas/lease, confirmed workspace/channel deletion and owned access cleanup |
| `src/gryphon/saas_spec_import.py`, `saas_upload.py`, `saas_spec_versions.py`, `saas_spec_delete.py`, `saas_spec_delete_api.py` | Bounded imports, immutable refresh, atomic bindings and confirmed whole-lineage deletion |
| `src/gryphon/compiler/ucp.py`, `ucp_profile.py`, `ucp_refs.py`, `ucp_responses.py`, `ucp_discovery.py`, `ucp_mcp.py` | Bounded UCP REST/native MCP inference, schema adaptation/filtering and trusted bindings |
| `src/gryphon/models/specifications.py`, `models/mcp.py`, `operation_policy.py`, `saas_post_reads.py`, `saas_post_read_api.py` | Provenance/diagnostics/native bindings; legacy POST-attestation compatibility only, no mounted approval route |
| `src/gryphon/models/audit.py`, `saas_audit.py`, `saas_audit_archive.py`, `static/lifecycle.js` | Public actor snapshots/bounded archive, current-name projections and accessible lifecycle consent UI; never authority |
| `src/gryphon/security/form_encoding.py`, `static/specifications.js` | Closed scalar form wire encoding; browser import/refresh state |
| `src/gryphon/saas_gateway.py`, `saas_runtime.py`, `saas_catalog.py` | Verified channel auth, isolated automatic catalog POSTs, filter-controlled visibility |
| `src/gryphon/cli_doctor.py` | Read-only, allowlisted JSON diagnostics |
| `src/gryphon/cli_clean.py` | Recognized-output archival, never arbitrary deletion |
| `src/gryphon/config.py` | Validated operator settings |
| `src/gryphon/models/` | Shared Pydantic models; account models in `users.py`, public exports in `__init__.py` |
| `src/gryphon/errors.py` | Domain exception hierarchy |
| `src/gryphon/server.py` | Thin MCP adapters, tool schemas, one reusable-code prompt |
| `src/gryphon/runtime/context.py` | Ownership, bounded responses, dependency lifespan |
| `src/gryphon/compiler/` | Source parsing, bounded schema normalization, deterministic catalogs |
| `src/gryphon/runtime/registry.py` | Manifest-only catalog loading and inspection |
| `src/gryphon/runtime/executor.py` | Admission, execution, replay, cancellation, receipts |
| `src/gryphon/runtime/execution_validation.py` | Bounded JSON inputs and source preparation |
| `src/gryphon/runtime/execution_results.py` | Static error envelopes, policy digest, artifact handoff |
| `src/gryphon/runtime/sandboxes.py` | Fresh restricted VM and sole external capability |
| `src/gryphon/runtime/docker_sandbox.py` | Optional networkless CPython transport and cleanup |
| `src/gryphon/runtime/cache.py` | Owner-scoped recipes, exact-source identity, TTL/LRU |
| `src/gryphon/runtime/runs.py`, `recovery_lease.py` | Durable receipts/idempotency, stale-scoped recovery and advisory recovery ownership |
| `src/gryphon/runtime/artifacts.py`, `artifact_projection.py`, `models/artifacts.py`, `server_artifacts.py` | Owned integrity-checked storage, offline projection, bounded shape metadata and thin MCP adapter |
| `src/gryphon/security/broker.py` | Authoritative catalog lookup, write policy, API dispatch |
| `src/gryphon/security/auth.py`, `vault.py` | Host-only credential resolution and refresh |
| `src/gryphon/security/network.py`, `policies.py`, `mcp_client.py`, `mcp_protocol.py` | Pinned egress/DNS/TLS, native sessions/JSON/SSE, fingerprint checks and bounded cleanup |
| `src/gryphon/security/encoding.py`, `schema.py` | Closed request objects and bounded validation |
| `src/gryphon/security/ast_guard.py` | Mandatory static defense before execution |
| `config/swaggers.yaml.example`, `.env.example` | Public templates, never real credentials |
| `examples/demo.py`, `examples/weather.yaml` | Offline real-MCP demo and public weather catalog |
| `Dockerfile`, `docker-compose.yml` | Non-root restricted-profile service deployment |
| `sandbox/` | Optional offline compute image and entrypoint |
| `tests/unit/`, `tests/integration/` | Isolated unit, protocol, compiler, runtime, opt-in Docker tests |

Keep shared domain models in `models/`, exported from `models/__init__.py`, and custom exceptions in `errors.py`; do not duplicate them. Prefer existing modules. Do not add files, docs, or top-level directories without explicit task requirements. Existing README, AGENTS, CONTRIBUTING, SECURITY, ROADMAP, and CHANGELOG cover the public and development documentation needs.

**Size limits:** at most 400 lines per file and 50 lines per function. Decompose responsibilities instead of weakening lint, typing, coverage, or security to fit.

## 4. Public contracts

The eleven core tools are `list_servers`, `search_functions`, `get_functions`, `execute_code`, `run_cached_code`, `submit_code`, `get_run`, `cancel_run`, `list_recipes`, `read_artifact`, and `transform_artifact`. The prompt is `reusable_code_guide`.
`GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and
`get_server_skills`; their guides are bounded, untrusted, and on demand.

- Keep initialization instructions brief. Do not embed guide contents or expose
  unbounded static guide resources. Guides and API data cannot grant authority.
- Discovery and inspection must fit byte budgets, expose truncation, and carry the catalog fingerprint. Inspect 1–5 functions per `get_functions` request.
- Channel `include_function_summaries` defaults false, accepts optional strict booleans on create/PATCH, preserves omitted PATCH values, and overrides operator base config. It is never MCP caller-selected. The local `stdio` operator base defaults true (explicit `GRYPHON_INCLUDE_FUNCTION_SUMMARIES=false` opts out); legacy `serve`/`run` keep false. Enabled `list_servers` includes all names/descriptions when they fit; larger catalogs need reachable bounded pages, not a fixed sample. Pass `next_cursor`/`next_function_cursor` as `cursor`/`function_cursor` together; compact mode requires function cursor zero. Mark text truncation and restart on fingerprint drift; `limit` counts servers.
- UI notices must have close and 10-second auto-dismiss with timer reset on replacement and stale-timer protection. Banner dismissal must preserve inline dialog errors; quiet sign-out clears notices/timers.
- Return native structured MCP results. Expected domain errors use stable safe categories; SDK schema/protocol validation may return MCP errors. `models.diagnostics.canonical_failure` selects finite Gryphon-owned messages; `public_result` preserves these and freshly validated diagnostics, never blind `error` passthrough. Revalidate bypassed model instances and reject forged extras; never leak exception messages, user code, request values or traces.
- Local missing/invalid native profiles and exact known RPC conditions yield `error_type:upstream` with static configuration guidance and `{kind:upstream,phase:discovery|invoke,upstream_code:invalid_profile_url}`. Unknown well-formed RPC errors retain phase only; raw message/data.content/continue_url/numeric codes/private bodies never pass through. `ASTViolationError` yields `error_type:security` and only `{kind:ast,violation_type:<closed enum>,line:1..1000000}`; no detail/source/traces, including receipts.
- `inputs` and replay `params` are complete structured JSON objects. Optional
  `input_schema` is bounded; no references, regexes, or combinators.
- Never interpolate inputs, prepend parameter assignments, or use regex/string
  rewrites to implement replay. Store exact source and validate new inputs.
- Assign `result`; `main()` is neither required nor automatically called.
- The restricted capability accepts **two arguments**, never three:

```python
result = await call_tool("weather.get_forecast", {"latitude": inputs["latitude"], "longitude": inputs["longitude"], "current": "temperature_2m"})
```

- No generated module imports, `sys.path` changes to compiled directories, or
  host `exec` of generated code. `top_level_functions` is not an active tool
  promotion mechanism.
- `submit_code`/`get_run`/`cancel_run` are application handles. Keep native MCP
  Tasks disabled and unadvertised until a real protocol implementation exists.
- `readOnlyHint` is metadata, never an authorization decision.
- `stdio` consumes JSON `GRYPHON_SWAGGERS`, permits empty-catalog compute, and
  derives private source-scoped user state under optional absolute `GRYPHON_STATE_DIR`.
  `stdio` and `saas` must not discover ambient dotenv; only explicit `--env-file`.
  README must lead with least-setup stdio and SQLite SaaS commands; development
  extras and standalone compile/config copies are not prerequisites for stdio.
  SaaS uploads compile through the UI; legacy `serve --transport http` has no UI.
  Copyable exports need separate lines; create the private SQLite parent first.
  Generate a bootstrap token only if absent, retain it across restarts, and never
  request its value in chat. Show it only in the user's private local terminal.
- Hosted UI `/`, admin `/api`, DB-readiness `/health`, and per-channel
  `/mcp/{channelUUID}` are distinct surfaces. Channel bearer keys cannot administer
  tenants; administrator sessions require HttpOnly/SameSite=Strict cookies and CSRF.
  Secure cookies/HTTPS are mandatory except explicitly selected loopback development.
- Named `platform_admin` accounts administer all tenants/users; `tenant_user`
  accounts have immutable membership in exactly one enabled tenant. Enforce scope
  server-side for every spec/channel/key/usage/analytics/audit route, never only in the UI.
  Tenant users cannot list/manage users, create tenants or elevate/change roles. No public POST-approval workflow remains.
  Keep bootstrap-token recovery. Passwords use salted PBKDF2-HMAC-SHA256 with
  600,000 iterations and bounded off-loop hashing; never expose passwords/hashes.
  Self-service changes require current credentials. Profile/status/reset/change
  and tenant status revisions invalidate cookies, including after re-enable.
  Separately issued channel keys remain independent: document offboarding key
  rotation. Platform/API authentication never substitutes for MCP channel keys.
- Resource and user-deletion events retain saved public `AuditActor` snapshots (`id`, `username`, `name`, `kind:user/bootstrap/system/unknown`, `display_source:snapshot/current/unknown`). Legacy unknown is not bootstrap. Legacy account events resolve CURRENT names by `actor_id` only, labeled Current account name or unknown after deletion; never invent historic names, resolve IDs by reused username or substitute subject/self for actor. Expose actors only on authorized events, not a user directory. Additive `saas_audit_archive` preserves affected history without rebuilding live tables/relaxing foreign keys or event CHECK constraints; COMBINED live + archive stays normally bounded to 10,000 per category (resource/user).
- Tenant Suspend/Resume uses strict boolean `PATCH /api/tenants/{tenant_id}` `{enabled:false|true}`: reversible, retains data/keys, invalidates old tenant-user cookies; resume permits enabled channels' valid keys, never restores sessions. Platform-only typed TENANT NAME deletion removes the ENTIRE workspace: all assigned tenant users/password hashes, channels/key hashes/bindings, ALL spec versions and scoped usage/analytics; other tenants/platform accounts remain. Typed CHANNEL NAME deletion removes only its scoped channel/key/bindings/usage/analytics and FK-dependent rows, not users/specs/other channels. Typed USERNAME deletion is platform-only, protects self/last enabled platform admin with 409 (bootstrap too), and revokes ONLY browser account/sessions: independent shared channel keys REMAIN VALID; UI must warn to rotate/revoke exposed keys for offboarding.
- Tenant/channel/user resources `/api/tenants/{tenant_id}`, `/api/tenants/{tenant_id}/channels/{channel_id}`, `/api/users/{user_id}` expose `GET <resource>/deletion` `{kind,id,name,label,confirmation_token,impact}` (tenant adds child IDs; impact users/channels/spec versions, user name=username/label=display name) and `DELETE <resource>` closed `{confirm_name,confirmation_token}`. Check exact stored name/username with no trim/case/coercion and public-state CAS before mutation in serialized SQL. Stale 409 requires fresh blank preview/retyping/new token, never auto-retry. Keep existing malformed/wrong name 400, missing/foreign 404, platform/CSRF 403, invalid session 401 semantics. Fresh scope/CSRF rechecks remain: enabled own-tenant members can delete their channels; platform/bootstrap can clean disabled tenants. Tokens/actor metadata never authorize.
- Physical control-row/audit deletion commits atomically, frees quotas and allows username reuse with NEW UUIDs. Owned `finish_cleanup` revokes deleted users' sessions at `user.revision + 1` and gathers ALL deleted Channel snapshots with `invalidate(before_revision=deleted.revision + 1)`, covering cold/loaded authority through the deleted revision, NOT unconditional None. Suspend/Resume rechecks platform authority, then owns mutation/session cleanup and gathers `before_revision=current_channel.revision`. Attempt every channel despite one failure; finish before 200, never falsely return success. Production factory injects `store.is_current_channel`: under the manager lock validate exact enabled DB snapshot BEFORE cached/new acquisition and AFTER startup; reject deleted/noncurrent authority with owned failed-start cleanup. Forget completed hosted watermarks under lock to bound deleted-ID churn; standalone managers without validators retain legacy semantics. Fresh gateway key checks remain mandatory. DB mutations MUST COMMIT before requesting manager lock; never wait for manager while holding DB transaction (no reverse lock order). Removed tenant/channel keys/endpoints fail; local sandbox/cache/recipes/receipts/artifacts are NOT auto-erased, remain private/inaccessible via removed endpoints. No secure erasure, backup deletion or HA claim; no real production/operator DB operations during development, only disposable schema verification.
- Keep compact accessible SVG user name/status/password/delete and channel actions plus Tenant controls; labels/tooltips/keyboard focus, blank exact-match deletion gating and stale tenant/session/dialog guards. Preserve existing notices, UCP profile/diagnostics/artifact contracts and document verification commands without claiming pending browser/full gates passed.
- Audit `ContextVar` is metadata only, never authority. Set it in the HTTP guard after auth; reset in `finally` on success/failure/cancellation and preserve metadata in owned `finish_cleanup` tasks. Persist only public attribution, not credentials or caller-supplied actor bodies/headers.
- Successful standalone `compile` (including unchanged catalogs) prints non-secret
  MCP client JSON to stdout; logs stay on stderr. Dry runs, failures, and startup
  compilation inside `serve`/`run` must not emit client JSON on MCP stdout.

## 5. Security invariants — never weaken

### Execution

1. Enforce source byte limits before analysis. Run AST validation on every
   execution and replay, before backend execution; never treat AST checks alone
   as the isolation boundary.
2. Restricted execution uses a fresh bounded Monty VM. No imports, host
   filesystem, host environment, or direct network; only the revocable broker
   capability is external. Bound time, memory, recursion, calls, and admission.
3. Optional Docker is **offline computation**: no broker, credentials, network,
   host mounts, or shared warm-container state. Use UID 1000, drop all
   capabilities, no-new-privileges, read-only root, bounded tmpfs, PID/RAM/swap/CPU
   limits, and the configured isolation runtime. `runsc` is the default. Missing
   daemon/image/runtime must fail closed; never autostart or silently downgrade.
4. Cancellation revokes broker scope before cancelling work and awaits workers
   and owned-container cleanup. It does not undo accepted upstream effects.
5. Bound output at production and serialization boundaries. Omit raw prints,
   stderr, upstream failure bodies, and traces. Artifacts must remain bounded
   and owner-scoped; chunks cannot exceed 8192 bytes.
6. `transform_artifact(artifact_id,code,description,inputs?)` loads bounded owned integrity-checked JSON off-loop as `inputs['artifact']`, caller parameters as `inputs['params']`. Reuse AST/resource limits, admission, local/shared slots, deadline/cancellation and run ledger. Fresh Monty has no external functions, broker reference or network; reject `call_tool` references/aliases. Docker config rejects, never downgrade. Results may artifact again; receipts remain, but no replayable projection `cache_id`. Normal `run_cached_code` still reruns/refetches recipes.
7. Use `models.artifacts.artifact_shape` for result/receipt summaries: `json_type`; objects add complete `top_level_keys` ≤32/512 serialized bytes (fewer under small budgets), `key_count`, `keys_truncated`; arrays add `length`, never values. Large-artifact `next` recommends `transform_artifact`, not upstream replay.

### Broker and credentials

1. Credentials live only in the trusted host broker/vault. Do not put them in
   generated code, either sandbox's environment, logs, tool inputs, recipes,
   test assertions, or public client snippets. Never read real env files or
   secret material during development unless explicitly authorized and needed.
2. Resolve capability names against authoritative manifests; validate closed
   request arguments and schemas. Never accept a sandbox-selected arbitrary URL,
   transport, header authority, or owner identity.
3. Enforce source filtering at compilation **and** dispatch. `allow_catalog_posts` defaults false for legacy `serve`/`run`; **local `stdio` and SaaS default true**, so included catalog POSTs execute without separate approval and **may have side effects**. Ordinary writes otherwise require a write-enabled source, `GRYPHON_ALLOW_WRITES=true` and exact `GRYPHON_ALLOWED_WRITE_OPERATIONS` permits; PUT/PATCH/DELETE stay denied without them.
   Unclassified POSTs on a read-only source still fail; native MCP has its own known-read filter. Never call this automatic read-only execution; set `GRYPHON_ALLOW_CATALOG_POSTS=false` to opt out.
   No model approval flag, guide, hint or idempotency key grants authority; preserve exact catalog/channel ownership, supported methods, schema/DNS/TLS and budgets.
4. Enforce exact-domain policy and validate all DNS answers before connecting to
   a pinned address. Public destinations are default; private/loopback needs
   explicit opt-in. Metadata and prohibited address classes remain denied.
5. Preserve TLS verification, original Host/SNI, origin-isolated pools, no
   environment proxies, no redirects, and bounded auth/API responses. Recheck
   scope around asynchronous credential resolution and request dispatch.
6. HTTP startup requires a token of at least 32 characters. Derive owner from
   verified auth, not arguments/headers containing unverified identity. Current
   static auth maps to `operator`; stdio uses trusted `local`. Public deployment
   needs TLS termination and additional operator controls. Hosted channel identity
   must come from database-verified keys/status, never the URL alone or client claims.
7. Hosted channel config must force `allow_writes=False` and `allowed_write_operations=[]`, denying PUT/PATCH/DELETE API operations even when visible; automatic catalog POSTs are the explicit exception. `read_only_filter` defaults true, excluding ordinary HTTP POSTs unless already exactly read-classified; false includes supported POSTs for automatic execution. No source-auth or host credential/header inheritance; public APIs only, no tenant secret manager. Versions remain immutable/tenant-bound; deny ordinary OpenAPI external refs, environment interpolation and caller-selected host paths. Only bounded UCP resolves approved refs.
8. Hosted Docker requires explicit operator enablement and manually provisioned
   daemon/image/runsc. Channel imports only narrow preinstalled approved libraries;
   no arbitrary pip installs. Shipped Compose profiles must not mount a Docker socket.

### Persistence and cleanup

1. Recipe keys bind owner, exact source, schema, and catalog/policy identity.
   Reject replay on drift; run the complete guard/broker pipeline again.
2. Hold advisory single-process run-ledger ownership during recovery. Recovery
   only interrupts active receipts older than the configured stale window, so a
   concurrent process never marks a live process's jobs interrupted and a leftover
   owner cannot block a new one. Stdio reaps a host that abandons a connection.
3. Persist admission/idempotency transactionally. Matching keys deduplicate only
   while their receipt exists; conflicting requests fail. Interrupted work is
   marked `interrupted`, never automatically replayed, especially writes.
4. Storage must enforce ownership, quotas, safe paths, and sanitized failures.
   Artifact cleanup touches only indexed owned files; never arbitrary neighbors.
5. `clean --yes` requires the operator to stop the server, validates all targets,
   and archives recognized compiled/cache output. Retain runs, artifacts, and
   config; reject links, unknown content, overlapping paths, and DB sidecars.
6. Close all partially initialized dependencies. Do not use process-global
   mutable state for credentials or execution authority.
7. Hold the hosted control-database lease for the entire single-worker lifespan.
   Leases are automatic; never remove `.lock` files to free live workers/jobs.
   PostgreSQL session advisory locks require direct/session-pooled connections,
   not transaction pooling. PostgreSQL does not replace local run-ledger locks
   or store execution data. Stop the worker for coordinated database/local-state
   backups; no automatic HA/backup guarantee exists. Verify additive schema
   upgrades only against disposable databases, never the operator's real store.

### Analytics measurement contract

- Keep `models/analytics.py`, `models/traffic.py`, runtime `execution_metrics.py`,
  `saas_traffic.py` and `saas_analytics*.py` content-free and optional to execution.
  Persist scalar counts/timing/static dimensions and hashed event IDs, not code,
  inputs, results or credentials; log static failure categories only.
- API reports require existing tenant authorization and exact channel scope.
  Keep 1–90 UTC days, bounded aggregate JSON, first `recording_since`, no lifetime
  backfill and the 100,000-**per-tenant** receipt cap. Other tenants must not evict
  in-retention receipts; total storage is bounded by the hosted tenant quota (100).
  Best-effort terminal observers cover background completion, but failures can miss records;
  never claim complete billing auditing or exactly-once telemetry/effects.
- Compare only paired successful eligible API-backed runs: accepted validated
  decoded canonical API JSON before user reduction versus **full final JSON**,
  including artifact content. “Original” is not raw HTTP or a no-framework LLM
  counterfactual. Signed weighted reduction permits expansion; no baseline is N/A.
- Count fixed allowlisted `tools/call` names (eleven core/two optional), including SDK-rejected known names, discovery, polls, artifact reads and transformations, separately from runs. Projection is execute-origin pure compute, not replay or a new API-backed reduction baseline.
  Exclude initialization, `tools/list`/SDK negotiation, HTTP headers and agent
  context. Wire bytes retain duplicate text/structured representations; only
  `structured_payload_bytes` counts the canonical structured payload once.
  Response bytes are SDK-produced, not proven client reception/model consumption.
  Attempt legacy usage/request-metric persistence once before final ASGI body
  handoff, with incomplete fallback if the SDK stops early. Each writer has a
  two-second timeout and static nonfatal failures. Request durations measure
  response production excluding their own persistence, not client end-to-end latency.
- `api_calls` is broker attempts, not HTTP dispatches: some fail before the wire.
  Accepted validated responses are separate. Pure compute requires a backend
  start and **zero broker attempts**, not merely zero accepted API responses.
  Replay reuse is replay backend starts / all backend starts, not request counts;
  retained idempotent duplicates do not start extra runs. Reused source executes
  again and does not prove LLM generation avoided; source lines are static lines.
- Backend wall time includes awaited network work. Broker timing wraps `_send`:
  credential resolution/origin checks, network wait and response decoding/validation,
  excluding earlier lookup/authorization/argument encoding/domain guards. Queue
  time includes admission-to-backend preparation, not only semaphore wait.
  Concurrent wall times overlap; fixed-bin p50/p95 are upper bounds, not exact.
- Label `ceil(UTF-8 bytes / 4)` as heuristic token equivalents, never actual model
  context/generation/reasoning/billing. Keep unobservable values null/N/A; do not
  claim tool definitions/all client context counted by response budgets, or dollar,
  CPU, time or round-trip savings. Downloads retain methodology and explicit
  window tenant/channel IDs (null channel means all). Operational run success and
  payload reduction do not measure answer correctness or equivalent task quality.
- UI item totals count top-level JSON arrays, not semantic records. Cache errors
  span all requests, not exact misses versus storage failures; failed replay
  requests and request/run error categories remain separate from backend reuse.

### Specification imports, automatic POSTs and native MCP

- Keep File `{name,content}` and URL `{name,url,kind:"openapi"|"ucp"}` imports under scoped `/api/tenants/{tenant_id}/specs`; strict boolean `read_only_filter` defaults true. URLs max 2048 characters, no query/userinfo/fragment. Use bounded DNS-pinned clients, no host auth, redirects, env interpolation/proxies. Normalize relative OpenAPI servers; ordinary external refs stay denied.
- Refresh URL `{}` or file `{content}` accepts optional strict boolean `read_only_filter`, defaulting to previous. `/specs/{spec_id}/filter` requires `{read_only_filter}` and revalidates saved bytes without upload/fetch, preserving provenance/native bindings. Both accept strict boolean `update_channels` (API false, UI checked); row changes require confirmation, with import/refresh checkboxes too.
- Changed document OR any import metadata creates an immutable successor (`parent_id`, 201): include diagnostics/warnings/filter, legacy grants, `mcp_bindings` and resolved provenance even when normalized document SHA-256 is unchanged. Opted-in exact bindings/revisions commit atomically and runtimes drain; unchanged 200 without revisions, superseded 409. Keep old snapshots, defaults (file/filter true/grants empty), no spec schema migration, and post-validation authority checks. Identical canonical documents retain legacy grants; document changes clear them, with no new approval workflow.
- UI groups parent-linked lineage into one compact entry: ellipsized name/source, short versions/count badges; detail icons expose full identity/provenance/diagnostics/warnings/bindings for latest and all historical versions, including middle versions via History. Use accessible SVG details/source/download/update-file-or-refresh-URL/filter/history/delete actions with tooltips, labels and visible keyboard focus. History is read-only, never individual-version deletion; main trash deletes the whole lineage. Never merge same-name unrelated roots. Channel selector uses latest for new choices, explicit pinned older for existing selections; no stable-ID or in-place overwrite claim. Preserve actors and 10-second notices. Refresh/filter must never delete/mutate snapshots; only explicit confirmed whole-lineage or entire-tenant deletion overrides snapshot retention.
- `GET /api/tenants/{tenant_id}/specs/{spec_id}/deletion` previews `{name,specification_id,spec_id,version_ids,version_count,channels:[{id,name,revision}],confirmation_token}`: root `specification_id`, requested `spec_id`. `DELETE /api/tenants/{tenant_id}/specs/{spec_id}` takes closed `{confirm_name,confirmation_token}`. Check EXACT stored name (no trim/case/coercion) and current CAS fingerprint BEFORE any mutation in a serialized SQL transaction. Token hashes tenant/root/requested ID/full version IDs/affected channel configs+revisions, never auth capability. New version/binding/channel change returns stale 409: require fresh preview/retyping from blank, no auto-retry; malformed/bad name 400, nonexistent/foreign 404. Browser auth/CSRF and fresh `AccessControl` recheck remain mandatory; enabled own-tenant membership only for tenant users, platform admins/bootstrap may clean disabled tenants as with revocation. Guard stale browser tenant/session/dialog context; exact-match button gating is not server authority.
- Delete all parent-linked lineage rows and ALL exact older/latest bindings, preserving unrelated same-name roots, channels/IDs/keys/other bindings/config, usage/receipts/artifacts. Increment each affected channel revision once with audit; owned `finish_cleanup` drains runtimes before 200 `{deleted:true,specification_id,deleted_spec_ids,updated_channel_ids}`. No host-file/cache purge: stale recipes fail catalog drift, retained receipts keep normal retention. `spec_deleted` persists verified actor/root via optional `AdminAudit.spec_id` default `None`; existing audits remain subject to NORMAL bounded retention, never bypass pruning. No migration or real operator-data deletion during development; tests use temporary stores only.
- Remove manual POST approval UI and route: GET/POST `/api/tenants/{tenant_id}/specs/{spec_id}/post-reads` return 404. Retain `approved_post_reads` metadata/helpers for old payload compatibility and historical `post_reads_updated` audits under normal bounded retention; approval removal itself deletes no DB data, distinct from explicit confirmed lineage deletion. Legacy exact global permits remain optional, deployment-wide, not credentials/tenant authority; canonical server/effective base/literal POST tuples, no globs/templates, checked by parser/broker and included in identity. Merge selected-spec grants only after tenant/binding validation; never change operator env automatically.
- Diagnostics mean total/available/filtered/unsupported, not permission for every HTTP method. Ordinary OpenAPI unsupported request schemas reject; native MCP unsupported tools omit with warnings, never weakened validation. One active import, no queue, 25-second deadline and cancellation-owned cleanup. UCP errors use HTTP 400 `ucp_discovery`, not obsolete approval advice; normal validation stays separate.
- UCP accepts published 2026-01-11/01-23/04-08/08-25 profiles. Root or explicit `/mcp` first probes same-origin `/.well-known/ucp`; prefer matching advertised REST, else MCP; malformed advertised contracts fail closed. Only explicit MCP routes fall back directly if profile unavailable. Never arbitrary redirect/HTML-follow; public advertised delegations pass allowedDomains and pinned DNS/TLS policy at every step. Original `source_url` stays separate from `resolved_profile_url`, `resolved_endpoint`, `source_transport`.
- Preserve REST advertised schema-path GET subset: checkout; April/August cart/order too. Required auth/signing (canonical January) rejects; UCP-Agent/Request-Id stay caller-supplied, no real platform identity. Unsupported REST response schemas omit with warnings, not claimed validation. No broad REST writes/extension composition or arbitrary callback/capability fetches. Max 32 schema documents, approved same-origin refs (profile origin/ucp.dev/operator-approved), structural/expansion bounds.
- `GRYPHON_UCP_AGENT_PROFILE` is optional (`None`, `repr=False`): require a REAL fetchable public HTTPS PLATFORM profile from the operator, never fabricate one/use merchant identity as default or edit real env. Enforce ≤2048 characters, no userinfo/query/fragment/quotes/control/unsafe escapes/interpolation/meta or prohibited IPs; validate structure/current exact-domain policy at init/use and ALL DNS answers public at use, even if private networks enabled, without fetching the profile. Include configured URI in policy fingerprint; `doctor` exposes only configured boolean.
- Bound native MCP alone fills omitted `json_body.meta.ucp-agent.profile`/containers only when the whole path is required in schema. Explicit empty/invalid values never overwritten; valid explicit body wins with matching fixed `UCP-Agent: profile="URI"` on session init/discovery/invocation/owned cleanup as applicable, never ordinary OpenAPI. `get_functions` parent `ucp_agent_profile` is `{required,operator_configured,input_path,guidance}` with generic `call_tool('shop.search_catalog', {'json_body': inputs})`; never leak configured URL or suggest empty profile.
- Native import runs initialize/notifications/initialized/paginated tools/list, never business tools/call. Known reads: get_checkout/get_cart/get_order/search_catalog/lookup_catalog/get_product (catalog.lookup). Filter true hides unknown/non-read; false includes supported native non-read tools for automatic execution, **potentially mutating**. Unsupported patterns/combinators/refs/input/output semantics omit the tool with warnings. Preserve descriptions and all required native inputs (including meta.ucp-agent.profile) inside `json_body`; no generated identity or inherited secrets.
- Save trusted native endpoint/name/raw input/output schema fingerprint in `mcp_bindings` outside untrusted OpenAPI; synthetic `/__mcp__/...` POST paths NEVER actual routes. Fresh invocation reinitializes and rediscovers metadata, checks fingerprint BEFORE one tools/call; stale `conflict` requires refresh, no side-effect retries. Metadata-only filter changes remain network-free; endpoint/raw schema drift must change refresh identity even if normalized OpenAPI is unchanged.
- Native JSON/SSE must match request IDs, bound notifications/session headers and reject protocol drift. Prefer structuredContent; decode one finite JSON text block; retain non-JSON/multimodal blocks without fetching resources. SDK handshake negotiation (2025-11-25 proposal/2025-03-26 compatible selection) is not Tasks/modern server-discovery/auth-platform completion. Discovery: max 1000 tools / 100 pages / 5 MiB aggregate within configured/hosted caps and min(HTTP timeout, 30s), outer 25s. Invocation init/list/call share caller deadline/response cap; only validated owned sessions get fixed-endpoint DELETE cleanup under a separate 2s / 1 KiB nonfatal budget; cancellation awaits local cleanup. Not arbitrary DELETE authority.
- Coolbudget evidence: actual https://coolbudget.lk/api/ucp/mcp GET 301 to canonical WWW HTML 404; same-origin profile delegates https://qhhihh-tw.myshopify.com/api/ucp/mcp. Metadata-only import succeeds after initialized ACK `200 {}` compatibility: 13 discovered / six reads / two supported cancel tools hidden / five unsupported. No live business/payment calls or full-commerce claim. Original CSE: 1 GET + 25 POST (23 URL-encoded/two scalar multipart); filter-off parsing exposes 26, real Monty/mocked HTTP covers synthetic calls/replay, not universal/live proof. `cse` namespace is not fixture `cse_api`. Forms use closed scalar/scalar-array `json_body`, reject nested/null/binary/files; no host-file reads/filenames (1024 parts / 2 MiB).

## 6. Code quality

- Fully annotate signatures; use modern `X | None`, `list[str]`, and `dict` types.
  Strict mypy covers **src and tests**. Avoid unreviewed `Any` or suppression.
- Use Google-style docstrings for public classes/functions/methods; private
  helpers need at least a concise docstring. Document invariants and failure
  behavior, not just syntax.
- Keep named constants instead of magic values. Use `Field(default_factory=...)`
  for mutable Pydantic defaults and validate trust-boundary data explicitly.
- Pass `GryphonConfig` and dependencies instead of consulting environment state
  throughout business logic. Credential environment access belongs in the vault
  and CLI/config loading boundary.
- No bare `except:`. Catch domain failures specifically; translate unexpected
  failures to safe envelopes and log a static event/category, not raw traces or
  `str(exc)` that could contain secrets. Never silently swallow cleanup failure.
- Use `get_logger(__name__)` and structured key/value events. Log lifecycle,
  compiler phases, safe sizes, policy categories, cache activity, and owned
  container lifecycle without code, input values, credential-bearing URLs, or
  headers. Do not log whole settings/models.
- No `print()` in server/runtime code. CLI diagnostics/demo output must be
  deliberately separated from MCP stdio. Do not add decorative output or emojis.
- Keep the event loop responsive: no `time.sleep()` or blocking network/DB work
  inside async execution. Offload necessary filesystem/worker operations and
  wait for them on cancellation. Use `httpx` through the approved network layer
  for I/O; do not add `requests` or bypass policy with another client.

## 7. Testing and coverage

**90% is the mandatory coverage floor; 100% is the target.** Do not lower the
floor, omit security modules, delete tests, broaden suppressions, skip failing
checks, or weaken controls to make a run pass. Regressions need fixes and tests.
Report actual command results, not invented test counts or performance numbers.

```bash
uv sync --frozen --extra dev --extra saas
uv run --frozen --extra saas ruff check src/ tests/
uv run --frozen --extra saas ruff format --check src/ tests/
uv run --frozen --extra saas mypy --strict src/ tests/
uv run --frozen --extra saas pytest --cov-fail-under=90
uv run --frozen --extra saas pytest tests/unit/test_saas_spec_delete.py tests/integration/test_saas_spec_delete_http.py
uv run --frozen --extra saas pytest tests/integration/test_admin_lifecycle.py tests/integration/test_saas_user_delete_http.py tests/unit/test_saas_resource_delete.py tests/unit/test_saas_resource_delete_audit.py tests/unit/test_saas_user_delete.py tests/unit/test_saas_audit_archive.py tests/unit/test_saas_runtime_validation.py tests/unit/test_saas_lifecycle_cleanup.py
uv run --frozen --extra saas pre-commit install
uv run --frozen --extra saas pre-commit run --all-files
node --test tests/unit/specifications_ui.test.js tests/unit/lifecycle_ui.test.js
node --check src/gryphon/static/admin.js
node --check src/gryphon/static/specifications.js && node --check src/gryphon/static/lifecycle.js
node --check tests/integration/browser_ui.cjs && node --check tests/integration/browser_lifecycle.cjs
# Opt-in: separately provision Node, Puppeteer and its working Chromium (no sandbox downgrade).
uv run --frozen --extra saas python tests/integration/browser_ui_fixture.py
```
The browser fixture uses temporary state, generated credentials via stdin, real loopback cookies/CSRF and synthetic pinned upstream HTTP; never point it at operator stores or live APIs. Its lifecycle flow exercises Suspend/Resume, compact accessible user/channel/tenant actions, exact typed tenant/channel names and usernames, stale-consent/context guards and shared-key offboarding warnings; this is coverage intent, not a claim browser verification has already passed. It covers mobile/desktop compact rows, full latest/middle-version details, accessible action icons/tooltips/focus, read-only grouped History, typed-name whole-lineage deletion/fresh-preview stale-context guards, absence of POST approval controls and preserved actors, import/refresh/filter choices, no-fetch saved filter changes, exact/pinned bindings, warnings, retained downloads, channel summary create/edit and notification close/expiry. Puppeteer is an optional external prerequisite, not a Python runtime dependency. Node also checks notice expiry, manual close while busy, replacement/stale timers, inline errors and quiet sign-out cleanup.
UCP search/diagnostic/projection acceptance: `uv run --frozen --extra saas pytest tests/integration/test_ucp_search_workflow.py tests/integration/test_ucp_identity_broker.py tests/unit/test_ucp_identity.py tests/unit/test_safe_diagnostics.py tests/unit/test_ast_diagnostics.py tests/unit/test_artifact_projection.py`. Use synthetic peers/temporary stores, not live business calls or operator env. README must show real operator-provided profile setup (placeholder explicitly labeled), search with catalog-only inputs, then projection using returned artifact ID/actual shape metadata; no second upstream call or 21-chunk assembly. Do not assume universal `products`/`title` shape. User-reported fetchable-profile success is not our live-search verification or authority to default to merchant identity; preserve earlier metadata-only evidence. Run full gates too; never claim pending tests passed.
Targeted suites: `tests/unit/test_ucp*.py`, `test_mcp_client*.py`, `test_mcp_shopify.py`, `test_saas_post_reads.py`, `test_saas_audit.py`, `test_saas_spec_import.py`, `test_saas_spec_versions.py`, `test_cse_posts.py`, `test_form_contracts.py`, `test_multipart_posts.py`, `test_discovery_summaries.py`; integration `tests/integration/test_saas_automatic_posts.py`, `test_saas_post_read_api.py`, `test_saas_post_read_runtime.py`, `test_saas_audit_actor.py`, `test_saas_spec_refresh.py`, `test_saas_ucp_http.py`, `test_saas_spec_filter.py`, `test_saas_discovery_summaries.py`. Run selected paths with `uv run --frozen --extra saas pytest <paths>` in addition to—not instead of—full gates. Verify route404, no-fetch filtering, raw-schema/endpoint drift, native dispatch/cleanup and all remaining method/channel/schema/network denials; never claim pending gates passed.

Local pre-commit hooks invoke locked `uv run --frozen` commands; mypy and pytest
include `--extra saas`. Mypy covers `src` and `tests`, and `pytest-coverage`
enforces `--cov=gryphon --cov-fail-under=90`. Do not bypass hooks with
`--no-verify` or disable the coverage gate.

- Install `--extra saas` and system libpq even for default tests: hosted modules
  are imported during collection. Normal tests need no live PostgreSQL.
- `GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_postgres.py`
  opts into disposable PostgreSQL 17.6 Docker tests, including user/channel/tenant deletion and audit archives,
  with temporary storage, generated credentials and a loopback port. Read the fixture first; never use an operator
  database URL or run Compose with real operator env/config for verification.
- `GRYPHON_TEST_HOSTED_DOCKER=1 uv run --frozen --extra saas pytest tests/integration/test_saas_container.py`
  builds the shipped image and verifies a disposable hosted Compose stack with
  PostgreSQL, real MCP calls, and UI assets. It removes only its own test resources.
- Hosted runtime managers share the operator's `max_concurrent_executions` budget
  across channels; retain independent per-channel admission queues and store ownership.
- Normal tests need no live Docker or upstream service. Use `tmp_path` for all
  files/databases and isolated settings with `_env_file=None`; never touch an
  operator's compiled catalog, cache, receipts, artifacts, or secrets.
- Reuse fixtures from `tests/conftest.py` and specs in `tests/fixtures/`.
  Name tests `test_{unit}_{condition}_{expected_outcome}` and keep one behavior
  per test. Use async tests with the configured `asyncio_mode = "auto"`.
- Mock network/DNS and async Docker clients; use `respx` or injected transports
  as appropriate. Conformance tests may use real loopback HTTP, not live APIs.
- Every blocked security pattern needs a rejection test; every supported case
  needs a positive test. Cover traversal, schema abuse, DNS rebinding, identity
  isolation, request encoding, write denial, timeout/cancellation, drift,
  idempotency conflicts, interrupted recovery, ownership locks, and cleanup.
- Preserve `tests/integration/test_protocol.py` real modern/legacy MCP coverage,
  including HTTP authentication, structured results, and no Tasks advertising.
- `tests/unit/test_cli_lifecycle.py` also runs real CLI compilation, stdio, and
  the shipped demo using temporary configuration/stores and no upstream calls.
  Run it with `uv run --frozen --extra saas pytest tests/unit/test_cli_lifecycle.py`.
- Live offline Docker smoke tests are opt-in with `GRYPHON_TEST_DOCKER=1` after
  building `gryphon-sandbox:2.0.0`. They explicitly choose `runc` for benign
  transport checks, **not proof of gVisor isolation**. Never change deployment
  defaults or weaken security because those opt-in checks lack infrastructure.

## 8. Change and completion checklist

1. Read affected files and check `git status`; respect concurrent agents' owned
   files. Do not revert unrelated work. Use `git`, not `gh`, for Git operations.
2. Keep changes focused; do not commit, publish, or create extra files unless
   requested. Never commit `.env`, credentials, private configs, or runtime data.
3. Add tests for changed behavior, including negative and cleanup paths. Compiler
   changes need fixture coverage and a safe configured `compile --dry-run` check.
4. Review README for **every** code change and update affected public contracts
   in the same change. Update SECURITY for boundary changes and CHANGELOG for
   release changes. ROADMAP is only work that is not implemented.
5. Verify examples against current signatures and default config. Distinguish
   offline demo, public API calls, optional Docker, and authenticated HTTP.
6. Run lint, format check, strict types, full tests with the 90% floor, and hooks.
   Report blockers truthfully with reproduction steps; a failing gate is not done.
7. Summarize files changed, validation actually run, and remaining risks. Do not
   claim complete SaaS/HA readiness, exactly-once behavior, automatic crash resume,
   published distributions, zero vulnerabilities, isolation certification, or
   unmeasured speedups. Document implemented admin-managed tenancy precisely.
