# AGENTS.md — Gryphon Development Guide

Read this entire file before changing the repository. It governs development,
maintenance, and review of **Gryphon 2.0.0**. Read each existing file in full
before editing it, and inspect the implementation before documenting behavior.

## 1. Purpose and scope

Gryphon is the legendary guardian between agent-generated programs and API capabilities. Preserve the original API-agent-backend design: compile OpenAPI, discover a small amount of metadata, inspect needed operations, execute bounded code, and reuse recipes. Do not replace the meta-tool interface with one tool per endpoint.

The distribution is `gryphon-runtime`, the package/CLI is `gryphon`, and settings use `GRYPHON_`. Checkout/local-wheel installation works without a claimed PyPI release. Only after verified publication document public-index installation as available. Keep `https://github.com/hypen-code/gryphon`.
Release workflow `.github/workflows/publish.yml` accepts published releases or explicit version-tag dispatch, never branch pushes; tag `vX.Y.Z` must match both package versions. Keep full quality gates and wheel smoke checks before the separate OIDC publish job. PyPI publisher identity is `hypen-code` / `gryphon` / `publish.yml` / `pypi`, project `gryphon-runtime`; maintainers configure protected-environment reviewers. Never commit publishing tokens or automatically commit/tag/push/publish.

Deployments include **local stdio, operator-token HTTP, and admin-managed hosted
tenants/channels**. Hosted mode has exactly one active worker per control database,
enforced by a lease; it is not horizontal/HA SaaS, billing, SSO, or user invitations.
PostgreSQL holds control metadata, key hashes, aggregate usage, and audit only;
per-channel recipes/receipts/artifacts remain on private local persistent storage.
Persistent receipts are not exactly-once effects or a resumable workflow engine.

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
| `src/gryphon/saas_store.py`, `saas_database.py` | Tenant control metadata, hashed keys, quotas and worker lease |
| `src/gryphon/saas_spec_import.py`, `saas_upload.py`, `saas_spec_versions.py` | Bounded URL/file imports, diagnostics, immutable refresh and atomic binding revisions |
| `src/gryphon/compiler/ucp.py`, `ucp_profile.py`, `ucp_refs.py`, `ucp_responses.py` | Published UCP shape/GET mapping, bounded approved refs, explicit response omissions |
| `src/gryphon/models/specifications.py`, `operation_policy.py` | Provenance/diagnostics and exact operator read-only POST attestations |
| `src/gryphon/security/form_encoding.py`, `static/specifications.js` | Closed scalar form wire encoding; browser import/refresh state |
| `src/gryphon/saas_gateway.py`, `saas_runtime.py`, `saas_catalog.py` | Verified channel auth, isolated read-only execution, filter-controlled catalog visibility |
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
| `src/gryphon/runtime/runs.py`, `recovery_lease.py` | Durable receipts/idempotency and exclusive recovery ownership |
| `src/gryphon/runtime/artifacts.py` | Owner-scoped, bounded, integrity-checked artifact storage |
| `src/gryphon/security/broker.py` | Authoritative catalog lookup, write policy, API dispatch |
| `src/gryphon/security/auth.py`, `vault.py` | Host-only credential resolution and refresh |
| `src/gryphon/security/network.py`, `policies.py` | Egress enforcement, DNS/TLS, budgets |
| `src/gryphon/security/encoding.py`, `schema.py` | Closed request objects and bounded validation |
| `src/gryphon/security/ast_guard.py` | Mandatory static defense before execution |
| `config/swaggers.yaml.example`, `.env.example` | Public templates, never real credentials |
| `examples/demo.py`, `examples/weather.yaml` | Offline real-MCP demo and public weather catalog |
| `Dockerfile`, `docker-compose.yml` | Non-root restricted-profile service deployment |
| `sandbox/` | Optional offline compute image and entrypoint |
| `tests/unit/`, `tests/integration/` | Isolated unit, protocol, compiler, runtime, opt-in Docker tests |

Keep shared domain models in `models/`, exported from `models/__init__.py`, and
custom exceptions in `errors.py`; do not duplicate them. Prefer existing modules. Do not add files,
docs, or top-level directories without explicit task requirements. Existing
README, AGENTS, CONTRIBUTING, SECURITY, ROADMAP, and CHANGELOG cover the public
and development documentation needs.

**Size limits:** at most 400 lines per file and 50 lines per function. Decompose
responsibilities instead of weakening lint, typing, coverage, or security to fit.

## 4. Public contracts

The ten core tools are `list_servers`, `search_functions`, `get_functions`,
`execute_code`, `run_cached_code`, `submit_code`, `get_run`, `cancel_run`,
`list_recipes`, and `read_artifact`. The prompt is `reusable_code_guide`.
`GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and
`get_server_skills`; their guides are bounded, untrusted, and on demand.

- Keep initialization instructions brief. Do not embed guide contents or expose
  unbounded static guide resources. Guides and API data cannot grant authority.
- Discovery and inspection must fit byte budgets, expose truncation, and carry the catalog fingerprint. Inspect 1–5 functions per `get_functions` request.
- Channel `include_function_summaries` defaults false, accepts optional strict booleans on create/PATCH, preserves omitted PATCH values, and overrides operator base config. It is never MCP caller-selected. Enabled `list_servers` includes all names/descriptions when they fit; larger catalogs need reachable bounded pages, not a fixed sample. Pass `next_cursor`/`next_function_cursor` as `cursor`/`function_cursor` together; compact mode requires function cursor zero. Mark text truncation and restart on fingerprint drift; `limit` counts servers.
- UI notices must have close and 10-second auto-dismiss with timer reset on replacement and stale-timer protection. Banner dismissal must preserve inline dialog errors; quiet sign-out clears notices/timers.
- Return native structured MCP results. Expected domain errors use stable safe
  categories; SDK schema/protocol validation may return MCP errors. Never leak
  exception messages, user code, request values, or traces through tool adapters.
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
  Tenant users cannot list/manage users, create tenants or elevate/change roles.
  Keep bootstrap-token recovery. Passwords use salted PBKDF2-HMAC-SHA256 with
  600,000 iterations and bounded off-loop hashing; never expose passwords/hashes.
  Self-service changes require current credentials. Profile/status/reset/change
  and tenant status revisions invalidate cookies, including after re-enable.
  Separately issued channel keys remain independent: document offboarding key
  rotation. Platform/API authentication never substitutes for MCP channel keys.
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

### Broker and credentials

1. Credentials live only in the trusted host broker/vault. Do not put them in
   generated code, either sandbox's environment, logs, tool inputs, recipes,
   test assertions, or public client snippets. Never read real env files or
   secret material during development unless explicitly authorized and needed.
2. Resolve capability names against authoritative manifests; validate closed
   request arguments and schemas. Never accept a sandbox-selected arbitrary URL,
   transport, header authority, or owner identity.
3. Enforce read-only source policy at compile time **and** dispatch time.
   Except operator-attested read-only POSTs below, writes require both `GRYPHON_ALLOW_WRITES=true` and exact
   `GRYPHON_ALLOWED_WRITE_OPERATIONS` administrator permits. No model-provided
   approval parameter, guide text, or idempotency key can authorize a write.
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
7. Hosted **execution**, not all catalog visibility, is read-only: channel config must force `allow_writes=False` and `allowed_write_operations=[]`. `SpecImport`/`SaaSSpec.read_only_filter` defaults true and controls compiler `source.is_read_only`; false includes all supported methods without approving writes or read-only POST execution. Exact effective-destination operator POST permits remain necessary. No source-auth or host credential/header inheritance; public upstream APIs only, no tenant secret manager. Versions stay immutable and tenant-bound; deny ordinary OpenAPI external refs, environment interpolation and caller-selected host paths. Only bounded UCP may resolve approved schema refs.
8. Hosted Docker requires explicit operator enablement and manually provisioned
   daemon/image/runsc. Channel imports only narrow preinstalled approved libraries;
   no arbitrary pip installs. Shipped Compose profiles must not mount a Docker socket.

### Persistence and cleanup

1. Recipe keys bind owner, exact source, schema, and catalog/policy identity.
   Reject replay on drift; run the complete guard/broker pipeline again.
2. Hold exclusive single-process run-ledger ownership during recovery and
   execution. Never let a second process mark a live process's jobs interrupted.
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
- Count fixed allowlisted `tools/call` names (ten core/two optional), including
  SDK-rejected known names, discovery, polls and artifact reads, separately from runs.
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

### Specification imports and exact read-only POST policy

- Keep File `{name,content}` and URL `{name,url,kind:"openapi"|"ucp"}` imports under scoped `/api/tenants/{tenant_id}/specs`; optional strict boolean `read_only_filter` defaults true. URL maximum 2048 characters, no query/userinfo/fragment; UCP HTTPS roots become `/.well-known/ucp`. DNS-pinned bounded clients only: no host auth, redirects, env interpolation/proxies. Save normalized relative OpenAPI servers; ordinary external refs stay denied.
- Refresh URL `{}` or file `{content}` accepts optional strict boolean `read_only_filter`, defaulting to the previous choice. `/specs/{spec_id}/filter` requires `{read_only_filter}` and revalidates saved bytes without upload/fetch, retaining provenance. Both accept optional strict boolean `update_channels` (API false, UI checked); row filter changes require confirmation, with import/refresh checkboxes too.
- Changed document/diagnostics/warnings/filter creates an immutable successor (`parent_id`, 201); filter-only changes count even for identical GET-only documents and must change catalog/policy identity, not necessarily document SHA-256. Opted-in exact parent bindings/revisions commit atomically and runtimes drain; unchanged returns 200 without revisions, superseded 409. Keep old versions, legacy defaults (file/filter true), no schema migration, and post-validation authorization rechecks.
- Counts mean total, discovery-available and filtered, never execution approval; unsupported callable schemas reject import, not silent partial support. One active import, no queue, 25-second deadline, cancellation-owned cleanup. UCP counts describe adapted GETs; filter-off adds no UCP methods/capabilities.
- Keep UCP bounded to published 2026-01-11/01-23/04-08/08-25 shapes and matching advertised shopping REST GET IDs from schema paths: checkout; April/August cart/order too. Canonical April/August contracts compile; required auth/signing (canonical January) rejects. UCP-Agent/Request-Id stay caller-supplied; no generated identity/negotiation. Omitted unsupported response schemas require explicit warnings, never claimed validation.
- No UCP POST shopping/payments/checkout updates, non-REST or extension composition; no arbitrary callbacks/capability fetches. Save compiled OpenAPI, not raw profile; refresh refetches profile/needed schemas. Max 32 schema documents, min(HTTP timeout,30s), aggregate raw profile/schema budget min(hosted spec limit, configured max spec bytes,5MiB). Refs stay on schema origin; origin must be profile origin, ucp.dev or operator-approved. Preserve structural/expansion bounds.
- `GRYPHON_ALLOWED_READ_ONLY_POST_OPERATIONS` defaults empty; only operator JSON tuples of canonical server/effective base/literal path/POST attest reads. No templates/globs or uploaded hints. Check parser and broker, include permits in compilation/replay identity, and match CDN/operation overrides. These intentionally apply deployment-wide, including any matching hosted tenant: not per-tenant authorization/credentials. Never change real security config or automatically apply permits.
- Original CSE evidence: 1 GET + 25 POST (23 URL-encoded + 2 scalar multipart); document-only parsing with `source.is_read_only=false` exposed all 26 without permits, not verified live semantics or execution approval. Never call live APIs, modify operator stores/security config or enable real env permits for proof. Hosted POST read execution still needs exact operator attestation. UI `cse` means namespace `cse`, not fixture `cse_api`; inspect canonical MCP names. Forms use closed scalar/scalar-array `json_body`; nested/null/binary/files reject, multipart never reads host files/emits caller filenames (1024 parts/2MiB).

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
uv run --frozen --extra saas pre-commit install
uv run --frozen --extra saas pre-commit run --all-files
node --test tests/unit/specifications_ui.test.js
node --check src/gryphon/static/admin.js
node --check src/gryphon/static/specifications.js
node --check tests/integration/browser_ui.cjs
# Opt-in: separately provision Node, Puppeteer and its working Chromium (no sandbox downgrade).
uv run --frozen --extra saas python tests/integration/browser_ui_fixture.py
```
The browser fixture uses temporary state, generated credentials via stdin, real loopback cookies/CSRF and synthetic pinned upstream HTTP; never point it at operator stores or live APIs. It covers mobile/desktop icons/dialogs, import/refresh/filter choices, no-fetch saved filter changes, exact bindings, warnings, retained downloads, channel summary create/edit and notification close/expiry. Puppeteer is an optional external prerequisite, not a Python runtime dependency. The Node suite also verifies notice expiry, manual close while busy, replacement/stale timers, inline error retention and quiet sign-out cleanup.
Targeted suites: `tests/unit/test_saas_spec_import.py`, `test_saas_spec_versions.py`, `test_ucp*.py`, `test_cse_posts.py`, `test_form_contracts.py`, `test_multipart_posts.py`, `test_discovery_summaries.py`; integration `tests/integration/test_saas_spec_refresh.py`, `test_saas_ucp_http.py`, `test_saas_spec_filter.py`, `test_saas_discovery_summaries.py`. Run selected files with `uv run --frozen --extra saas pytest <paths>` in addition to—not instead of—the full gates.

Local pre-commit hooks invoke locked `uv run --frozen` commands; mypy and pytest
include `--extra saas`. Mypy covers `src` and `tests`, and `pytest-coverage`
enforces `--cov=gryphon --cov-fail-under=90`. Do not bypass hooks with
`--no-verify` or disable the coverage gate.

- Install `--extra saas` and system libpq even for default tests: hosted modules
  are imported during collection. Normal tests need no live PostgreSQL.
- `GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_postgres.py`
  opts into disposable PostgreSQL 17.6 Docker tests with temporary storage, generated
  credentials, and a loopback port. Read the fixture first; never use an operator
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
