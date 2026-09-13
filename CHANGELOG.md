# Gryphon Changelog

Notable changes to Gryphon, following
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0]

### Added

- Reversible **Suspend tenant / Resume tenant** controls via `PATCH /api/tenants/{tenant_id}` with strict boolean `{enabled:false|true}`: retain data/keys, block suspended access and invalidate tenant-user cookies; resume allows enabled channels' existing keys but never revives old browser sessions. Fresh platform checks precede owned `finish_cleanup` status/session updates and all-channel gathered invalidation at `before_revision=current_channel.revision`; failures cannot skip other cleanup or return success. Compact accessible SVG user name/status/password/delete, channel actions and Tenant controls have labels, tooltips and keyboard focus.
- Confirmed **tenant deletion removes the entire workspace**, including all assigned tenant users/password hashes, channels/key hashes/bindings, ALL specification versions and scoped usage/analytics, leaving other tenants/platform accounts alone. **Channel deletion** removes only its scoped channel/key/bindings/usage/analytics and dependent control rows, preserving users/specifications/other channels. **User deletion** requires the exact username, protects self and the last enabled platform administrator (409, including bootstrap), and removes only the browser account/password hash and sessions. Independently issued shared channel keys remain valid: UI warns that offboarding must also rotate/revoke exposed keys.
- For resource paths `/api/tenants/{tenant_id}`, `/api/tenants/{tenant_id}/channels/{channel_id}` and `/api/users/{user_id}`, `GET <resource>/deletion` previews `{kind,id,name,label,confirmation_token,impact}` (tenant also has child IDs); impact counts users/channels/spec versions. User `name` is the username, `label` the display name. `DELETE <resource>` takes closed `{confirm_name,confirmation_token}`; serialized SQL checks exact name/username without trimming/case folding/coercion and current public-state fingerprint before mutation. Changed revisions/configuration/bindings/tenant children return stale 409: fresh preview/token and blank retyping, never auto-retry. Malformed/wrong name 400, missing/foreign scope 404, forbidden platform actions/CSRF 403 and invalid sessions 401 retain existing authorization semantics; self/last-admin conflicts cannot be bypassed with fresh consent. Tenant/user deletion is platform-only; channel deletion allows enabled own-tenant members and platform admins/bootstrap, including disabled-tenant cleanup. Consent tokens never grant authority; fresh browser scope/CSRF checks and stale UI-context guards remain.
- Physical deletion frees applicable quotas and permits username reuse with a **new UUID**; historical actor IDs never resolve by username. Control rows/audit commit atomically, then owned cleanup revokes user sessions at `user.revision + 1` and gathers deleted Channel snapshots with `before_revision=deleted.revision + 1`, covering cold/loaded authority through deletion—not unconditional None invalidation. All attempts finish before 200 despite individual failure, never falsely returning success. Production injects `store.is_current_channel` under the manager lock before cached/new acquisition and after startup: exact enabled DB snapshots reject deleted/noncurrent authority with owned failed-start cleanup. Completed hosted watermarks are forgotten under lock to bound deleted-ID growth; standalone managers without validation retain legacy semantics. Fresh gateway key checks remain; DB mutations commit before requesting manager lock, never reverse that lock order. Removed tenant/channel endpoints/keys fail; local sandbox/cache/recipes, receipts/artifacts remain private, not auto-erased and inaccessible through removed endpoints. No secure erasure, backup deletion, undo of upstream effects or HA.
- Additive `saas_audit_archive` preserves affected audit history without rebuilding live tables or relaxing their foreign keys/event CHECK constraints. Combined live + archive retention remains **10,000 events per category** (resource/user), not 10,000 each or unlimited. Resource/user-deletion events retain saved public actor snapshots; legacy account events resolve current names by actor ID, labeled Current account name or unknown, never invented historical names. Schema/deletion verification uses disposable databases, never real production/operator data.
- Lifecycle verification instructions: `uv run --frozen --extra saas pytest tests/integration/test_admin_lifecycle.py tests/integration/test_saas_user_delete_http.py tests/unit/test_saas_resource_delete.py tests/unit/test_saas_resource_delete_audit.py tests/unit/test_saas_user_delete.py tests/unit/test_saas_audit_archive.py tests/unit/test_saas_runtime_validation.py tests/unit/test_saas_lifecycle_cleanup.py`; `node --test tests/unit/lifecycle_ui.test.js`; `node --check src/gryphon/static/lifecycle.js`; `node --check tests/integration/browser_lifecycle.cjs`. Opt-in `GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_postgres.py` covers user/channel/tenant deletion and archives on disposable PostgreSQL 17.6. Optional disposable browser fixture and full gates remain documented in AGENTS; commands do not claim browser checks or pending gates passed.
- Optional operator `GRYPHON_UCP_AGENT_PROFILE` (default `None`, excluded from settings repr): public HTTPS, at most 2048 characters, no userinfo/query/fragment, quotes/control/unsafe escapes, interpolation or prohibited addresses. Validate structure/current exact-domain policy at initialization/use and every DNS answer as public at use, without fetching the profile. Operators must supply a **real fetchable platform profile**, never a fabricated identity or merchant profile used as default; no real operator environment is changed.
- Bound native MCP alone fills **omitted required** `json_body.meta.ucp-agent.profile` and missing containers when the whole path is required in its schema. Explicit empty/invalid values are never replaced; a valid explicit body profile wins and matches the fixed `UCP-Agent: profile="URI"` session header, including initialization/discovery/owned cleanup as applicable. No ordinary OpenAPI header/body injection. `get_functions` adds parent `ucp_agent_profile` metadata (`required`, `operator_configured`, `input_path`, `guidance`) and generic input-based examples, not configured URLs or empty-profile examples; `doctor` exposes only `ucp_agent_profile_configured`. Policy fingerprints include the configured URI for recipe/receipt drift detection.
- Eleventh core tool `transform_artifact(artifact_id,code,description,inputs?)`: reduce saved owned JSON as `inputs['artifact']`, with optional parameters under `inputs['params']`, using restricted Monty without external functions, broker reference or network. Block `call_tool` references/aliases; Docker-configured execution rejects without downgrade. Integrity-checked bounded off-loop loading shares AST/resource limits, admission, local/shared slots, deadline, cancellation and run ledger. Results may become artifacts again; receipts remain but no replayable projection `cache_id`. `read_artifact` retains its 8192-byte cap; ordinary `run_cached_code` still reruns/refetches normal recipes.
- Artifact result/receipt summaries expose `json_type`, object `top_level_keys` (at most 32 complete keys / 512 serialized bytes, fewer under small budgets), `key_count`, `keys_truncated`, or array `length`, never value previews. Large-result guidance recommends local `transform_artifact` instead of replaying an upstream recipe. Traffic allowlists count eleven core/two optional tools; projection is execute-origin pure compute, not replay or an API-backed reduction baseline.
- UCP search workflow acceptance suite `tests/integration/test_ucp_search_workflow.py`, identity/broker integration and unit suites for identity, safe/AST diagnostics and projection; see AGENTS for targeted commands in addition to full gates. README demonstrates configured bedsheet search followed by local title/count projection instead of repeated chunk reads or a second upstream call. These use synthetic peers, not a live merchant search verified this cycle; added suites are not a claim pending gates passed. The user's report that a fetchable profile worked does not establish the merchant profile as our configured platform identity.
- Hosted File/OpenAPI URL/UCP URL imports at `POST /api/tenants/{tenant_id}/specs`: `{name,content}` or `{name,url,kind:"openapi"|"ucp"}`. Bounded DNS-pinned public-document reads inherit no auth, redirects, environment interpolation or proxies. URLs are at most 2048 characters, without userinfo/query/fragments; UCP requires HTTPS and expands root URLs to `/.well-known/ucp`. Relative OpenAPI server URLs normalize into saved self-contained snapshots; ordinary external references remain denied.
- Source provenance, compiled-JSON view/download and explicit total/available/filtered/unsupported counts/warnings. Ordinary OpenAPI unsupported request schemas reject import; native MCP unsupported tools are omitted with explicit warnings, not exposed with weakened validation. Old records default to file provenance/filtering on; no spec database schema migration.
- Strict boolean `read_only_filter` on import (default true) and refresh (omission preserves previous). Ordinary HTTP filtering excludes unclassified POSTs; false includes supported POSTs for **automatic bound-catalog execution, without separate permissions**. Native MCP uses a known-read subset when filtering, and includes supported non-read tools when off. POST can have side effects, not automatic read semantics. SaaS clones base config with `allow_catalog_posts=True`; local/operator default remains false. Hosted `allow_writes=False`/empty write permits still deny PUT/PATCH/DELETE; wrong-channel/unbound functions, unsupported methods, invalid schemas, DNS/TLS and budget violations remain denied.
- `POST /api/tenants/{tenant_id}/specs/{spec_id}/filter` accepts `{read_only_filter,update_channels?}` with strict booleans and API binding-update default false. It revalidates the saved document without replacement upload or URL fetch, preserving provenance. Filter changes create immutable successors and catalog/policy digest drift even for unchanged GET-only documents; opted-in exact bindings/revisions advance atomically and old versions remain intact. UI row filter icons open confirmation, with filter checkboxes also on import/refresh forms.
- Channel `include_function_summaries` (default false), optional strict boolean on create/PATCH with omitted PATCH preserving the value. Channel config overrides the operator base discovery setting; MCP callers cannot choose it. Enabled `list_servers` includes all function names/descriptions when they fit, otherwise byte-bounded pages with paired server/function continuation positions and marked text truncation; default compact summaries remain unchanged.
- Dismissible notifications with a close button and 10-second auto-dismiss, replacement-safe timer reset and quiet sign-out cleanup. Inline dialog errors survive banner dismissal.
- Immutable refresh at `POST /api/tenants/{tenant_id}/specs/{spec_id}/refresh`: URL `{}` refetches saved source; file `{content}` replaces bytes. Optional strict boolean `update_channels` defaults false in API, while the UI checkbox starts checked. Changed snapshots return 201 with `parent_id`; selected exact old bindings and channel revisions update atomically, then invalidated runtimes drain. Unchanged document and all import metadata returns 200 with no revisions; identity includes `mcp_bindings`, raw schema fingerprints and resolved provenance even when normalized OpenAPI is unchanged. Superseded refresh returns 409. Previous snapshots remain available.
- One active hosted import with no queue and a 25-second deadline. UCP adds at most 32 fetched schema documents, a min(HTTP timeout, 30 seconds) adapter deadline and an aggregate raw profile/schema budget capped by configured spec limits and 5 MiB, plus bounded expansion. Schema origin must be the profile origin, `https://ucp.dev`, or operator-approved; references stay on that schema origin.
- Bounded UCP REST/native MCP adapter for published 2026-01-11/01-23/04-08/08-25 shapes. Roots and explicit `/mcp` paths first probe same-origin `/.well-known/ucp`; prefer matching advertised REST, otherwise MCP; malformed advertised contracts fail closed. Explicit MCP routes alone fall back directly when the profile is unavailable. Public advertised delegations pass configured exact-domain and DNS/TLS policy; no arbitrary redirect or HTML-link following.
- Original REST subset remains advertised schema-path GETs `get_checkout`, `get_cart`, `get_order` (January checkout only), not broad REST writes. UCP-Agent/Request-Id stay caller-supplied; canonical January required signing rejects. Unsupported REST response validation omits with warnings, not claimed validation. No generated platform identity or extension composition. Saved document is compiled OpenAPI, not raw profile.
- Native metadata-only discovery via initialize/notifications/initialized/paginated tools/list. Known read filter: `get_checkout`, `get_cart`, `get_order`, `search_catalog`, `lookup_catalog`, `get_product` (catalog.lookup). Unknown/non-read tools hide by default; filter-off includes only supported native non-read tools for automatic execution, potentially mutating. Unsupported patterns/combinators/refs or native input/output contracts omit whole tools with warnings. Required arguments, including `meta.ucp-agent.profile`, remain inside `json_body`; omitted required identity may use the validated operator profile above, never a generated identity or inherited auth secrets.
- Separate `source_url` (original input), `resolved_profile_url`, `resolved_endpoint`, `source_transport` and trusted `mcp_bindings` outside uploaded OpenAPI. Synthetic POST paths are catalog identifiers, never actual upstream routes. Bindings retain endpoint/native name/raw input/output fingerprint. Fresh invocation initializes, rediscovers and verifies metadata before one tools/call; drift returns `conflict` requiring refresh, without side-effect retries.
- Bounded native JSON/SSE ID matching, notification/session limits and SDK handshake negotiation (2025-11-25 proposal with compatible 2025-03-26 selection), not Tasks or complete server-discovery/auth-platform support. Prefer structuredContent; decode one finite JSON text block; preserve non-JSON/multimodal blocks without resource fetches. Discovery: max 1000 tools / 100 pages / 5 MiB aggregate within configured/hosted caps and min(HTTP timeout, 30s), outer 25s. Invocation init/list/call share caller deadline/response cap. Only verified owned sessions get fixed-endpoint cleanup DELETE under separate 2s / 1 KiB nonfatal bounds; cancellation waits for local cleanup.
- Exact `GRYPHON_ALLOWED_READ_ONLY_POST_OPERATIONS` operator attestations (empty default), checked during compile and broker dispatch and included in policy/catalog identity. JSON tuples bind canonical server name, effective base URL including CDN overrides, literal route and POST; templates/globs and uploaded hints cannot grant authority. Permits intentionally apply deployment-wide, including any matching hosted tenant, not as per-tenant credentials/authorization.
- One compact specification entry per parent-linked lineage: ellipsized names/source, short versions/count badges, full details/provenance/diagnostics/warnings/bindings through detail icons for latest and historical middle versions in read-only History. Accessible SVG source/download/update-file-or-refresh-URL/filter/history/delete actions have tooltips, labels and keyboard focus. Unrelated same-name roots stay separate; channel selection prefers latest for new choices and labels pinned older bindings. Refresh/filter still create immutable 201 successors, never overwrite/delete history or claim stable version IDs; explicit confirmed whole-lineage deletion is the narrow retention exception, not an individual History delete.
- Any specification can be deleted by exact typed name after `GET /api/tenants/{tenant_id}/specs/{spec_id}/deletion` previews `{name,specification_id,spec_id,version_ids,version_count,channels:[{id,name,revision}],confirmation_token}` (root/requested IDs respectively). `DELETE /api/tenants/{tenant_id}/specs/{spec_id}` accepts closed `{confirm_name,confirmation_token}`; serialized SQL checks the exact name without trim/case/coercion and current CAS fingerprint before mutation. Token hashes tenant/root/requested ID/full version IDs/affected channel configs+revisions, not an auth capability. New version/binding/channel changes return 409 requiring fresh preview/retyping, never auto-retry; malformed/bad name 400, nonexistent/foreign 404. Blank-start UI confirmation, exact-match button gating and stale tenant/session/dialog guards complement browser auth/CSRF and fresh scoped authority checks. Enabled own-tenant users and platform admins/bootstrap are supported; platform admins can clean disabled tenants as with key revocation, while disabled-tenant users are denied.
- Deletion atomically removes the entire parent-linked lineage and all exact latest/older bindings, advances each affected channel revision once with audit, and drains runtimes through owned `finish_cleanup` before 200 `{deleted:true,specification_id,deleted_spec_ids,updated_channel_ids}`. Channels/IDs/keys/other bindings/config, unrelated same-name roots, usage, receipts and artifacts remain. No host-file/cache purge; stale recipes reject catalog drift. `spec_deleted` records verified actor/root through optional `AdminAudit.spec_id` (legacy default `None`); existing audits retain normal bounded retention, never unbounded pruning bypass. No migration or development deletion of real operator data; verification uses temporary stores.
- Legacy empty-default `approved_post_reads` metadata/helpers remain for old payload compatibility, with selected-spec tenant/binding validation and policy identity. Same canonical documents retain old grants; changed documents clear them, without a new approval workflow. Approval-workflow removal does not delete historical `post_reads_updated` audits or immutable snapshots or edit operator environments; audits keep normal bounded retention and snapshots remain unless explicitly confirmed whole-lineage or entire-tenant deletion is requested. Exact global read attestations are optional compatibility settings, not prerequisites for included hosted POST execution.
- Nested public `AuditActor` attribution (`id`, `username`, `name`, `kind:user/bootstrap/system/unknown`, `display_source:snapshot/current/unknown`). Resource events persist verified request-time snapshots; legacy unattributed records stay unknown, not bootstrap. User-deletion events retain saved public actor snapshots; legacy account audit `actor_id` resolves current public names by ID only, explicitly labeled **Current account name** or unknown after deletion, never invented historical names, a reused username's new account or the event subject. Actor identities are limited to authorized scoped events, without user-directory access for tenant users. The guard's post-auth audit `ContextVar` resets in `finally` and flows into owned cleanup tasks as metadata only, never authority.
- Closed scalar/scalar-array URL-encoded and multipart form support through `json_body`; nested/null/binary/file contracts reject. Multipart is text-only, never host-file access or caller filenames, bounded to 1024 parts and 2 MiB.
- Offline import/refresh/filter/UCP/form-policy/discovery suites plus `test_saas_automatic_posts`, legacy compatibility/route404 suites, `test_ucp_mcp*`, `test_mcp_client*`, `test_mcp_shopify`, audit/actor coverage, real Monty with mocked HTTP, isolation/replay and cleanup. Deletion coverage: `uv run --frozen --extra saas pytest tests/unit/test_saas_spec_delete.py tests/integration/test_saas_spec_delete_http.py`. `node --test tests/unit/specifications_ui.test.js` and optional `uv run --frozen --extra saas python tests/integration/browser_ui_fixture.py` (separately provisioned Puppeteer/Chromium) cover compact details/tooltips, exact-name deletion and stale-context guards alongside grouped history, absence of approval controls, actors, source/filter choices, no-fetch saved filtering, exact/pinned bindings, downloads, summaries, notifications and desktop/mobile dialogs/icons. Added tests and documented commands are not a claim that pending integration/full gates have passed.
- GitHub `publish.yml` release workflow for `gryphon-runtime`: matching version
  tags, full locked quality gates and 90% coverage, distribution validation and
  installed-wheel stdio MCP smoke, then a separate protected-environment PyPI
  Trusted Publishing job. Maintainer publisher/reviewer setup remains required;
  the workflow is not a claim that the package is already published.
- `gryphon stdio`: launch-environment source configuration through JSON
  `GRYPHON_SWAGGERS`, optional absolute `GRYPHON_STATE_DIR`, private source-scoped
  user state, and empty-catalog compute without ambient dotenv or package writes.
- `gryphon saas`: admin-managed tenants, immutable Swagger JSON/YAML uploads,
  revisioned channel bindings, one-time channel keys stored as hashes, and
  streamable-HTTP MCP at `/mcp/{channelUUID}`. Hosted settings are environment-only
  unless `--env-file` is explicit; upstream host credentials are never inherited.
- Named username/password accounts: all-tenant `platform_admin` and immutable
  single-tenant `tenant_user` roles, server-side scope enforcement, administrator
  create/list/name/status/reset controls and self-service password changes.
  Salted PBKDF2-HMAC-SHA256 uses 600,000 iterations with bounded off-loop hashing.
  Revision checks invalidate sessions after profile/status/password/tenant changes,
  including after re-enable; bootstrap-token recovery and channel keys remain
  independent. Additive account/audit tables preserve existing control data.
- Browser administration at `/`, session/CSRF-protected `/api`, database-readiness
  `/health`, key rotation/revocation, tenant/channel disabling, aggregate usage,
  and audit events. Admin and channel authentication are independent.
- Tenant analytics at `GET /api/tenants/{tenant_id}/analytics`: 1–90 UTC days
  (default 7), optional tenant-checked channel filter and existing browser roles.
  UI offers 7/30/90 days, daily/channel views and selected-report JSON downloads
  including methodology and explicit window tenant/channel IDs (null channel means
  all channels). Additive bounded daily aggregates retain 90 UTC days and first-
  measurement metadata. Hashed dedup receipts are capped at 100,000 **per tenant**:
  other tenants cannot evict in-retention receipts; total storage is bounded by
  the hosted 100-tenant quota. Older lifetime usage is not backfilled.
- Separate best-effort terminal-run and recognized `tools/call` traffic observers:
  background completion, failures/cancellations, actual backend starts/replays,
  canonical source/input/API/full-result bytes, request-body and SDK-produced
  response-body bytes, structured bytes and wall timings—not proven client reception
  or model consumption. Retained idempotent duplicates do not add backend runs;
  observer failures/crashes can miss records, not a complete billing audit.
  Allowlisted request metrics and legacy usage attempt persistence before final
  ASGI body handoff, once per request with incomplete fallback, two-second writer
  timeouts and static nonfatal failures; this does not prove response delivery.
- Paired successful API-backed payload comparisons include full artifact data,
  with signed weighted reduction (negative expansion, missing baseline null/N/A).
  “Original” API JSON is broker-accepted validated decoded canonical JSON, not raw
  HTTP bodies or a no-framework model baseline. Token equivalents use the labeled
  `ceil(UTF-8 bytes / 4)` heuristic; actual model context/generation/reasoning and
  billing remain unobservable, not guaranteed dollar/CPU/time/round-trip savings.
  Replay source bytes mean code executed again, not measured generation avoided.
  UI shows queue/backend averages and response-production latency excluding its
  own observation persistence, not client end-to-end latency. Response-production/run
  p50/p95 are fixed-bin upper bounds, not exact percentiles. UI also shows source
  bytes/lines, top-level array totals (not semantic records), wire/structured estimates,
  request/run error categories and failed replay requests. Cache errors cover all
  requests, not exact cache-miss versus storage-failure counts. Execution success
  and payload reduction do not measure answer correctness or equivalent task quality.
- PostgreSQL control metadata through optional `saas` dependencies with
  `psycopg==3.2.9`; exclusive hosted-worker lease and private local per-channel
  recipes/receipts/artifacts. PostgreSQL is not execution storage.
- Opt-in hosted Compose profile with isolated PostgreSQL 17.6, explicit secrets,
  loopback publishing, separate persistent volumes, and no Docker socket. The
  shared application image installs the locked `saas` extra and system `libpq5`.
- Opt-in disposable PostgreSQL integration tests (`GRYPHON_TEST_POSTGRES=1`).
- Credential-free restricted Python execution using `pydantic-monty` 0.0.18,
  with a fresh VM, bounded resources, and a single two-argument capability:
  `await call_tool("server.function", arguments)`.
- A host-owned capability broker for schema-validated API calls, credential
  resolution, read-only enforcement, and exact administrator write permits.
- DNS-pinned, origin-pooled HTTP connections with TLS verification, disabled
  environment proxies/redirects, bounded responses, and metadata-address denial.
- Seven additional core meta-tools: `search_functions`, `submit_code`, `get_run`, `cancel_run`, `list_recipes`, `read_artifact`, and `transform_artifact`, bringing the core set to eleven while retaining `reusable_code_guide` as the reusable-program prompt.
- Bounded discovery/inspection with catalog fingerprints, native structured MCP
  results, and owner-scoped artifacts readable in chunks of at most 8192 bytes.
- Persistent owner-scoped run receipts, atomic retained-request idempotency,
  bounded admission, cancellation, and interrupted-state recovery under exclusive
  single-process run-ledger ownership.
- Required HTTP bearer authentication with a minimum 32-character `SecretStr`
  setting and a fixed verified operator identity, distinct from local stdio.
- Read-only `gryphon doctor` diagnostics and a `--version` command.
- A credential-free Open-Meteo example catalog and a real in-process MCP demo
  that performs offline computation and structured recipe reuse without a model.
- Modern MCP 2026-07-28 and legacy-initialization conformance tests, plus opt-in
  benign offline Docker transport/library checks.

### Changed

- Local **`stdio` now enables `allow_catalog_posts` by default**, matching hosted channels, so included catalog **POST** operations (for example the CSE data API) execute through the sandbox instead of returning `error_type:"security"`. PUT/PATCH/DELETE still require `GRYPHON_ALLOW_WRITES=true` and exact permits, and an unclassified POST on a read-only source still fails. Legacy `serve`/`run` keep the conservative default; set `GRYPHON_ALLOW_CATALOG_POSTS=false` to opt out.
- Local run-ledger recovery is **stale-scoped** and its POSIX lock is **advisory**. Startup recovery only interrupts active receipts older than `GRYPHON_RUN_RECOVERY_STALE_SECONDS` (default 600), so concurrent clients can share one `GRYPHON_STATE_DIR` without one process marking another's live runs interrupted, and a leftover owner no longer fails a new launch. A **stdio** server with no inbound MCP message for `GRYPHON_STDIO_IDLE_TIMEOUT_SECONDS` (default 1800; `0` disables) exits and releases the ledger, so a host that abandons a connection without closing its pipes cannot wedge later launches; it also requests a Linux parent-death signal. Hosted single-worker control-plane leases stay exclusive.
- Local `stdio` now defaults `GRYPHON_INCLUDE_FUNCTION_SUMMARIES` to **true**, so `list_servers` shows each server together with its function names and descriptions (all when they fit, otherwise bounded continuation). An explicit `GRYPHON_INCLUDE_FUNCTION_SUMMARIES=false` restores the compact shape. Hosted channels keep their per-channel default of false, and legacy `serve`/`run` remain compact unless the operator opts in.
- Branding is **Gryphon**, the legendary guardian: distribution `gryphon-runtime`,
  import package and CLI `gryphon`, environment prefix `GRYPHON_`. The existing
  GitHub repository URL is unchanged.
- Startup documentation leads with least-setup stdio and native SQLite SaaS,
  optional development dependencies, private/stable bootstrap-token handling,
  Docker/PostgreSQL hosting, automatic leases and coordinated stopped backups.
- Version 2.0.0 can be installed **from the checkout or a local wheel**; public
  index commands are conditional on an actual PyPI release, not a publication claim.
  Python 3.13+ remains required; FastMCP is pinned to exactly 4.0.2. `uv.lock`,
  CI, container builds, and local hooks support locked environments.
- Restricted execution is the default. Optional full-Python Docker is a separate
  **offline** profile: no credentials, network, API broker, or host mounts; fresh
  non-root containers with resource limits and `runsc` selected by default.
  Missing daemon, image, or isolation runtime fails closed without fallback.
- Dynamic values are explicit JSON `inputs`; replay `params` replaces the full
  input object. Optional input schemas are bounded and reject references,
  regexes, and combinators. Input values are never substituted into source.
- Recipe identity now binds owner, exact source, input schema, and catalog/policy
  digest. Replays rerun guards and authorization and reject stale identities.
- Compiler output includes deterministic v2 manifests with request/body schemas,
  encoding metadata, source policy, and bounded response descriptions. Unsupported
  request semantics fail closed. Generated SDK source is not host execution code.
- Credentials and dynamic token refresh stay in the trusted host broker/vault, never either sandbox. Local/operator ordinary writes default off and require the global switch/exact permits plus source policy; SaaS automatic catalog POSTs are the explicit exception, not a claim of read-only semantics.
- Raw prints, stderr, upstream failure bodies, and traces are omitted from public
  execution output; successful large data uses bounded owner-scoped artifacts.
- `serve` respects `compile_on_startup`; `run` compiles once before serving.
  `--env-file` works before or after the subcommand. HTTP defaults to loopback.
- `clean --yes` archives only recognized generated output and a closed recipe
  cache after the operator stops the server. Unsafe/unknown targets are refused;
  run receipts, artifacts, and configuration are retained.
- Optional skills guides are untrusted, bounded, and fetched on demand through
  `list_skills`/`get_server_skills`, never embedded as initialization authority.
- Compose uses a non-root restricted-profile service, required auth, named storage
  volumes, resource limits, and no Docker socket. Its health check is TCP only.
- Strict typing covers source and tests. The mandatory **90% coverage floor**
  remains in place, with **100%** as the target and locked local pre-commit hooks.

### Fixed

- An **abandoned stdio server** no longer blocks every later MCP launch. When a previous client died without closing the child's stdin, the leftover process kept the exclusive run-ledger lock, so new launches failed inside `executor.startup()` and the client only saw `connection closed: initialize response`. Stdio now requests a Linux **parent-death signal** (`PR_SET_PDEATHSIG`) so it exits with the process that launched it, and a held run-ledger lock is reported as an actionable `run_ledger_in_use` error naming the run database instead of only the generic `command_failed`.
- Real upstream responses are no longer rejected for **JSON `null`** in fields their OpenAPI document types without marking nullable (for example the CSE API returning `logoUrl: null`, `indexCode: null` or `sectorTradeToday: null`). Output contracts now accept null for any declared value, including nested objects/arrays and scalar `enum`/`const`, while input validation stays strict and wrong non-null types are still rejected.
- The output validation-work budget counts only validation-relevant schema nodes, so **documentation-heavy schemas** (`description`/`title`/`example`/`format` and other annotations) no longer inflate the estimate and reject legitimate large responses such as CSE `get_detailed_trades`. All 26 CSE operations now execute instead of four returning the generic `error_type:"execution"` "Sandbox execution failed".
- Native MCP endpoints that reject a call with a non-2xx status **and** a bounded JSON-RPC error body (for example a Shopify UCP merchant returning HTTP 422 for `search_catalog`) are now classified as structured upstream failures instead of the generic `error_type:"execution"` "Sandbox execution failed" envelope. The bounded body stays internal to the protocol layer: a known `invalid_profile_url` code keeps the static configuration guidance, every other status/body/identity yields phase-only `{kind:"upstream",phase:"discovery"|"invoke"}`, and upstream messages, `data.content`, `continue_url` and private bodies are never returned. Non-MCP HTTP failures keep rejecting error bodies, and native-session timeouts now surface as `error_type:"timeout"`.
- Actionable, content-free native UCP failures: local missing/invalid profile and exact known RPC `invalid_profile_url` return `error_type:"upstream"`, static Gryphon-owned configuration guidance and `{kind:"upstream",phase:"discovery"|"invoke",upstream_code:"invalid_profile_url"}`. Unknown well-formed RPC errors retain phase only; messages, `data.content`, `continue_url`, numeric codes and private bodies never pass through. `models.diagnostics.canonical_failure` and public adapters preserve canonical failure messages/validated diagnostics, not untrusted `error` text, with fresh validation even for bypassed models and no extra fields.
- `ASTViolationError` preserves `error_type:"security"` with only `{kind:"ast",violation_type:<closed enum>,line:1..1000000}`. Blocked imports/calls/attributes/global/nonlocal remain blocked without exposing details, source or traces, including retained receipts.
- Consistent 20×20 outlined navigation icons across Overview, Analytics, Channels, API specifications, Audit and Users, including mobile layout.
- Included hosted POSTs no longer require manual approval. CSE's original 1 GET + 25 POSTs (23 URL-encoded/two scalar multipart) parse as 26 operations with filtering off; supported bound POSTs execute automatically. Synthetic CSE calls/replay use real Monty/mocked HTTP, not live CSE or proof all endpoints work. Namespace `cse` is not fixture `cse_api`; README shows canonical inspection and forms without a permissions detour.
- Coolbudget UCP import: actual `https://coolbudget.lk/api/ucp/mcp` GET 301 leads to canonical WWW HTML 404, but same-origin `/.well-known/ucp` advertises delegated `https://qhhihh-tw.myshopify.com/api/ucp/mcp`. Metadata-only discovery now succeeds, including initialized ACK `200 {}` compatibility: 13 discovered, six reads, two supported cancel tools hidden by default, five unsupported schemas omitted. No live business/payment calls or full-commerce support claimed. Failures now yield useful HTTP 400 `ucp_discovery` diagnostics instead of obsolete POST-approval advice; normal validation unchanged.
- Standalone compilation prints credential-free MCP client JSON even for unchanged
  catalogs; startup compilation remains silent on stdout. Generated entries pin
  storage/env-file paths and disable recompilation from the client's directory.
- Preserve `sandbox_unavailable` through MCP error sanitization and nested run
  receipts instead of misclassifying Docker failures as internal errors.
- Canonicalize exact write-permit sets for replay/idempotency identity without
  changing authorization; reordered or duplicated permits no longer cause drift.
- Isolate shared test settings from operator dotenv files. Add actual CLI
  compile/stdio/demo smoke coverage and concurrent idempotency verification.

### Removed or retired

- Manual **POST read permissions** UI and public approval endpoints: GET/POST `/api/tenants/{tenant_id}/specs/{spec_id}/post-reads` now return 404. Legacy metadata/helpers and old audits remain for compatibility under normal bounded retention; no replacement public approval flow and no database deletion caused by approval removal (explicit confirmed lineage deletion is separate).
- Host execution of generated/promoted endpoint tools. Legacy
  `top_level_functions` selections do not register direct MCP API tools.
- Automatic `main()` invocation and source-rewriting parameter injection.
- Credential-bearing, network-enabled, or shared warm Docker execution.
- LLM-enhanced compilation: `--llm-enhance` and enabling its setting are explicitly
  rejected. Deterministic compilation requires no model provider key.
- Automatic skills embedding and unbounded static guide resources.

### Operational limits

- Run receipts are portable application handles, **not native MCP Tasks**;
  the Tasks extension is neither implemented nor advertised.
- Persisted queued/running work becomes `interrupted` on restart. No automatic
  crash resume or write replay occurs, and no exactly-once external effect is
  promised. Cancellation cannot undo API actions already accepted upstream.
- Local/operator modes remain supported separately from admin-managed hosted
  tenants/channels. Hosted mode permits exactly one active worker per database,
  automatically executes supported bound POSTs (potential side effects), denies PUT/PATCH/DELETE API operations, remains public-API-only, and is not horizontal/HA SaaS.
  No billing, SSO, user invitations, tenant secret manager, independent security
  certification, or zero-vulnerability claim is provided. Operators must provision
  TLS and coordinated PostgreSQL/local-state backups themselves.

## [0.1.0] — 2026-03-14

Initial public release. This section records historical Gryphon behavior;
features superseded above are not instructions for running version 2.0.0.

### Added

- Four MCP meta-tools: `list_servers`, `get_functions`, `execute_code`, and
  `run_cached_code`; one `reusable_code_guide` prompt.
- Swagger/OpenAPI compilation to typed Python functions using Jinja2 templates.
- Docker-based execution with memory/CPU limits, a non-root user, and timeouts,
  backed by an AST guard for dangerous imports and calls.
- A credential vault whose initial execution design used Docker environment
  injection; **replaced by host-only broker credential handling in v2**.
- Async SQLite code caching with TTL/LRU and reusable-code execution with changed
  parameters; **replaced by structured-input, owner/policy-bound recipes in v2**.
- Optional directly promoted endpoint tools and server skills resources;
  **direct host tools are disabled and guides are on demand in v2**.
- Optional LLM docstring/example enhancement; **retired and rejected in v2**.
- Domain allowlists and compile-time read-only method filtering.
- `gryphon compile`, `serve`, `run`, and `clean` commands.
- CI and local hooks for Ruff, strict mypy, tests, and a 90% coverage gate.

### Removed

- `get_cached_code`, superseded by returning a `cache_id` from `execute_code`
  for subsequent `run_cached_code` calls.
