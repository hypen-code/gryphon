# Gryphon Security Policy

## Supported code line and reporting

Security work targets the **Gryphon 2.0.x** code line. Older versions do not
receive backports. Install from a checkout or a locally built wheel until an
actual PyPI release is verified; this policy implies neither publication nor an
external security audit. The GitHub release workflow uses a separate protected
`pypi` environment job and PyPI Trusted Publishing (OIDC), not committed API
tokens. Maintainers must configure the publisher, environment reviewers and tag
protections; see [CONTRIBUTING.md](CONTRIBUTING.md). Publishing is gated on version
agreement, full quality checks with 90% minimum coverage, and installed-wheel
MCP smoke tests. A green workflow is not an independent security assessment.

**Do not disclose vulnerabilities in public GitHub issues.** Submit a private
[security advisory](https://github.com/hypen-code/gryphon/security/advisories/new).
Include the affected commit/version, execution profile, transport, minimal
reproduction with synthetic data, impact, and any mitigation. Do not include
real tokens, private API responses, or another user's data. Coordinate disclosure
with maintainers; this document makes no guaranteed response-time commitment.

## Threat model

Gryphon is an API-agent backend with local/operator modes and **admin-managed
hosted tenants and channels**. Agent code, uploaded OpenAPI descriptions, skills
guides, tool inputs, and upstream data are untrusted. The host process, operator
configuration, control database, catalog storage, and local-mode credential
vault are trusted. Hosted tenant/channel identity is server-verified, not a claim
from tool arguments. The administrator controls every tenant: this is not
self-service identity federation or an independently certified SaaS boundary.

The default runtime is a bounded Python subset in **pydantic-monty 0.0.18**,
not host CPython. The only external capability is an asynchronous broker call
with a joined catalog identifier and a JSON argument object:

```python
result = await call_tool("weather.get_forecast", {"latitude": inputs["latitude"], "longitude": inputs["longitude"], "current": "temperature_2m"})
```

FastMCP **4.0.2** handles MCP negotiation and validation. Tests exercise modern
MCP **2026-07-28** and legacy initialization; protocol compatibility is not an
isolation certification. Native MCP Tasks are disabled and unadvertised.

## Enforced boundaries

### 1. Programs do not own credentials or transports

- Restricted code cannot import modules, access a filesystem, read the host
  environment, open sockets, or choose an arbitrary upstream URL. It gets
  structured `inputs`, supported Python operations, and the broker capability.
- A fresh VM is created for each run. Source/JSON size, structure, execution
  duration, memory, recursion, admission, and capability-call budgets are bounded.
- Mandatory AST checks precede execution and replay as an additional defense;
  the AST denylist is not presented as the sole isolation boundary.
- The broker interprets validated v2 manifests. Generated Python and legacy
  `top_level_functions` files are never imported or executed by the MCP host.
- Credentials are resolved only in the host-side broker/vault. Neither Monty
  nor optional Docker receives credential environment variables. Do not pass
  credentials in source, inputs, descriptions, or recipe metadata yourself.

### 2. Catalog entries are capabilities, not blanket network authority

- Every call resolves an exact registered server/function and validates closed
  request arguments before encoding parameters and JSON bodies.
- `is_read_only: true` filters write methods during compilation and is checked
  again at dispatch. Writes otherwise require **both** `GRYPHON_ALLOW_WRITES=true`
  and the exact `server.function` in `GRYPHON_ALLOWED_WRITE_OPERATIONS`.
- Write permits are administrator configuration, not a model-supplied approval
  flag. There is no interactive approval UI. Guides, descriptions, MCP
  `readOnlyHint`, and idempotency keys cannot authorize a write.
- Scope expiry/cancellation is checked around asynchronous credential resolution
  and request sending. Primary-origin credentials cannot be delegated to a
  different endpoint origin.
- Read-only classification is based on HTTP method and source policy, not proof
  of an API's semantics. An upstream GET can still have side effects if the
  service is designed that way. Review catalogs and grant least privilege.

### 3. Broker egress is validated and bounded

Registered public API origins require **HTTPS**, including authentication and
remote-document requests. A nonempty `GRYPHON_ALLOWED_DOMAINS` adds an exact-host
restriction; use a JSON array, not wildcards or suffix matching. Include
separate token/login and specification hosts when they are required.

Private/loopback destinations require `GRYPHON_ALLOW_PRIVATE_NETWORKS=true`.
Only explicitly approved private/loopback destinations may use HTTP; every DNS
answer must qualify. Mixed public/private answers fail closed. Metadata aliases,
link-local, multicast, unspecified, and reserved addresses remain denied.

The network layer connects to a validated numeric address while preserving
original HTTP Host and TLS SNI/certificate identity. Pools are separated by
original origin; TLS verification stays enabled. Environment proxies and
redirects are disabled. Responses have hard deadlines/byte budgets, and raw
upstream failure bodies are never returned. Local HTTP remains unencrypted.

### 4. Inputs and replay remain data

`inputs` and replay `params` are detached JSON objects, never Python assignments
or source interpolation. `params` replaces the complete input object. Optional
`input_schema` validation is structurally bounded and rejects references, regex
constraints, and combinators. Program source is never parameterized with
string/regex replacement, and defining `main()` does not automatically invoke it.

Recipe identity includes owner, exact source, schema, and catalog/policy digest.
Replay checks that identity, revalidates inputs, and repeats the full execution
and broker policy path. Drift causes rejection rather than execution under
stale assumptions. Cache reuse reruns code; it is not response memoization.

### 5. Outputs and persistent stores are bounded and owner-scoped

Execution returns strict JSON-native values and safe error categories. Raw
prints, stderr, exception traces, and upstream error bodies are omitted; only
print-byte summaries may appear. Oversized but permitted successful results
become private, owner-scoped JSON artifacts. `read_artifact` allows at most
**8192 bytes** per requested chunk, possibly fewer under the MCP context budget.
Artifacts cannot bypass the full-result size cap.

Recipes, run receipts, and artifacts enforce caller ownership. Foreign handles
do not confer access. Artifact storage uses private directories/files, bounded
indexes, integrity checks, no-follow path handling, and owned-file-only cleanup.
Retention is limited; these stores are not a permanent audit archive.

The run ledger has exclusive single-process ownership during recovery and
execution. Do not share a live run database between workers or assume a network
filesystem implements the required local locking semantics. Native storage uses
POSIX locks; use the container deployment rather than native Windows storage.

These files may contain source, descriptions, and returned API data. There is
**no claim of encryption at rest or complete data-loss prevention**. If an
allowed API returns sensitive fields, code can return them to its caller. Keep
credentials least-privileged, protect disks/backups, and minimize returned data.

### 6. HTTP and stdio have distinct trust assumptions

Stdio treats its launcher as trusted and uses the `local` namespace. HTTP CLI
startup refuses a missing or shorter-than-32-character `GRYPHON_HTTP_AUTH_TOKEN`.
Settings represent that token as `SecretStr`; the verified token maps to a fixed
`operator` identity. Ownership is not taken from caller-supplied arguments.
Sharing the token shares the identity—this is not tenant provisioning or
per-user authorization.

Standalone `compile` emits client configuration on stdout, separately from stderr
logs; `serve`/`run` reserve stdout for MCP. Client JSON references the selected
private env-file path and absolute storage paths, never resolved credentials.
Protect the referenced file and provide any environment-only settings to the
launcher; they are not copied wholesale into the client configuration.

The HTTP listener defaults to loopback. Before public exposure, require a
**TLS reverse proxy**, protect the bearer token, and add appropriate access,
network, and operational controls. Do not mistake bearer authentication for
transport encryption or a complete public hosting security model.

### 7. Hosted administration and channels

`gryphon saas` uses independent `GRYPHON_SAAS_ADMIN_TOKEN` (at least 32 random
characters), `GRYPHON_SAAS_DATABASE_URL`, and `GRYPHON_SAAS_PUBLIC_ORIGIN` settings.
Like `gryphon stdio`, it reads no ambient dotenv; `--env-file` must be explicit.
Stdio derives private source-scoped paths below an optional absolute
`GRYPHON_STATE_DIR`, or the XDG/home state directory, without package writes.
Protect explicit storage overrides and stop competing processes using a ledger.

Hosted `/api/login` exchanges either the recovery bootstrap token or a named
user's username/password for a bounded in-memory session. Cookies are Secure,
HttpOnly, SameSite=Strict; mutations require the session-bound CSRF token.
Sessions expire and are lost on restart. Each named account has at most four
sessions; tenant logins leave one platform slot free when the total limit exceeds
one. Account changes proactively discard older sessions while retaining any
concurrently authenticated newer revision. Canonical Host and browser Origin checks,
CSP, no-store responses,
request-size/deadline/concurrency limits, and bounded login/request rate limits
complement authentication; they are not comprehensive public-service DDoS defense.
Plain HTTP requires **both** a loopback canonical origin and explicit
`GRYPHON_SAAS_ALLOW_INSECURE_HTTP=true`; that development exception removes Secure
cookies. Never use it for public traffic. Public deployments need manual TLS
termination preserving canonical Host; forwarded proxy headers are not trusted.

Each `/mcp/{channelUUID}` request authenticates a channel key against the control
database and verifies tenant/channel status. Keys are shown once when generated,
stored only as hashes, and cannot access the administrator API. Rotate lost keys;
revocation, tenant disable, and policy revision invalidate runtime authority.
Multiple clients with the same channel key share that channel's ownership.
The bootstrap operator and named **`platform_admin`** accounts can manage all
tenants. Named **`tenant_user`** accounts are immutably bound to exactly one tenant
and require both account and tenant to be enabled. Server-side checks restrict
specs, channels, keys, usage, analytics and audit to that tenant, independent of UI visibility.
Tenant users cannot create tenants, list/create/manage users, change their role
or membership, or access foreign-tenant resources. Only platform administrators
can create accounts, edit display names, change status or reset another password.
There is no self-signup, invitation flow, SSO or custom/multi-tenant account role.

Passwords are salted **PBKDF2-HMAC-SHA256 with 600,000 iterations** and fresh
32-byte cryptographic salts; password/hash material is not returned in API
profiles or audits. Passwords must be 12–128 characters, at most 512 UTF-8 bytes.
Hashing runs off the event loop with bounded admission and cancellation-safe
cleanup. Rejected/unknown identities still perform fixed-cost hash verification.
Named users can change their own password only with their current password.
Account profile/status changes and password reset/change advance the revision;
every authenticated API request rechecks revision, account status and tenant
status. Tenant status changes advance bound users' revisions too. Old cookies
remain invalid after re-enable; self-service password change signs the user out.
Bootstrap-token recovery remains independent and cannot be reset in the user UI.

**User disable/reset does not revoke separately issued channel keys.** Offboarding
must also rotate/revoke exposed shared keys. A browser/platform login grants no
MCP access without a valid channel key; channel keys grant no browser API authority.
Keep bootstrap credentials, passwords and session cookies out of MCP clients.

Uploaded JSON/YAML versions are immutable and tenant-bound. External references,
environment interpolation, and caller-supplied host paths/auth configuration are
rejected. The compiler constructs **read-only** sources and the channel runtime
disables writes regardless of base permits. Hosted upstream access is currently
**public-API-only**: broker host environment credential/header inheritance is
explicitly disabled. No tenant credential vault or secret manager is implemented.
Base AST, execution, egress, output, and ownership restrictions still apply.
A manager-owned execution budget is shared across channels, including background
runs and result serialization; independent bounded channel queues remain in place.
Channel state directories are created private (0700) before opening SQLite;
existing non-private or foreign-owned channel directories are rejected.
Docker channel settings can only narrow the approved preinstalled import list;
restricted mode permits no imports. No arbitrary pip installation is supported.

Exactly **one hosted worker per database** is enforced by an automatic exclusive
lease; no installer is needed. PostgreSQL uses a **session-level advisory lock**,
requiring a direct connection or session pooling, never transaction pooling.
SQLite and local run ledgers use POSIX locks. Stop the existing worker before
starting another; **never delete `.lock` files** to release a live process or
repair live jobs. The OS/connection lifecycle releases ownership on shutdown.
For native SQLite, create a dedicated private parent directory before startup;
SQLite cannot create its parent and `umask 077` does not fix existing permissions.
Normal startup adds account/audit and analytics tables to control databases without
replacing tenants, uploads, channels or keys. Stop/back up before upgrading;
verify upgrades only with disposable test databases, never operator stores.
Retain the bootstrap admin token across restarts in a password manager or private
operator environment; show it only in a private terminal, never shared logs/chat.
PostgreSQL holds tenant/channel metadata, immutable specs, hashed keys, aggregate
usage/analytics and audit events. Recipe source, run receipts, and artifacts remain in
private per-channel local storage, not PostgreSQL. Both stores may contain
sensitive information; hashed keys do not imply encryption at rest. Do not scale
replicas or replace the local volume with an unreviewed network filesystem.
`/health` checks database readiness only—not all channels, TLS, or API reachability.

### Tenant analytics and activity privacy

`GET /api/tenants/{tenant_id}/analytics` uses the existing browser-session and
revision/status checks. Platform administrators can select tenants; tenant users
are restricted server-side to their one enabled tenant. Optional `channel_id`
requires exact tenant membership even for empty windows. Channel bearer keys
cannot read reports. JSON downloads contain methodology and explicit
`window.tenant_id` / `window.channel_id` (null means all channels); protect them
as tenant activity metadata, not anonymous public statistics.

Analytics persist numeric counts, byte sizes, timings and fixed tool/status/error/
origin/sandbox dimensions, not source, inputs, result bodies or credentials.
Tenant/channel identifiers and first-observation metadata remain scoped control
metadata; run/request identifiers become SHA-256 deduplication digests. Reports
also include existing channel names. Hashing does not make activity anonymous:
size, timing, reuse and volume can reveal workload patterns. Normal execution
stores still contain source/results under their separate retention policies.
The traffic observer temporarily buffers a bounded response for structured-payload
measurement, then clears it; it does not persist those contents in analytics.
Measurement/observer failures log static events, not payloads or exception text.
Response bytes measure SDK-produced bodies, not proven client reception or model
consumption. For the fixed 12 allowlisted `tools/call` names, request metrics and
legacy usage each attempt persistence before the final ASGI body handoff, once per
request; early SDK termination triggers an incomplete fallback observation.
Each writer has a two-second timeout; failures remain static and nonfatal, not
an unbounded persistence wait or a delivery guarantee. Response-production timing
excludes its own observation persistence; it is not client end-to-end latency.
Allowlisted names can count as errors even when unsupported by the current SDK.

Daily aggregates use fixed, bounded JSON and a 90-UTC-day reporting/retention
window, pruned on recording. Report building admits one active build and one
queued request per shared analytics store, with bounded waiting; cancellation
drains an in-progress off-loop build before releasing its slot. These bounds are
not a general DDoS guarantee. The **100,000-receipt cap is per tenant**, so another
tenant's activity cannot evict still-in-retention receipts. A tenant's own volume
can shorten its deduplication window; deduplication applies only while receipts
remain. Total receipt storage is bounded by that cap times the hosted tenant quota
(currently 100 tenants), not a single shared 100,000-receipt pool.
First-observation metadata is retained; old lifetime usage is not backfilled.
The terminal observer runs after receipt persistence, including background work;
crashes/timeouts/observer failures can miss observations. Retained duplicate
idempotency requests do not start another run, but this is **not** a complete
billing audit or exactly-once event/effect guarantee. Incomplete request traffic
is explicit; requests minus runs must never be treated as saved round trips.

Only paired successful eligible runs compare accepted, validated canonical API
JSON to full final JSON, including artifact content. This is not raw HTTP bytes,
all model context, or a no-framework counterfactual. Signed reduction may be
negative; no API baseline yields null/N/A. Heuristic `ceil(UTF-8 bytes / 4)` token
equivalents do not observe model context, generation, reasoning or billing, which
remain null/N/A. No dollar, CPU, elapsed-time or round-trip savings are guaranteed.

### 8. Optional Docker is offline computation only

Docker is not required for normal restricted execution. If selected explicitly,
it requires a reachable daemon, existing image, and the configured runtime
(default **gVisor `runsc`**). Missing requirements fail closed. Gryphon does not
start Docker or silently fall back to another isolation runtime.

Each run creates its own container with:

- UID/GID 1000, all capabilities dropped, and no-new-privileges;
- network disabled, no broker capability, no credentials, no host volume mounts;
- read-only root and 64 MiB `/tmp` tmpfs with noexec/nosuid/nodev;
- 64-PID limit, 256 MiB default memory/swap cap, and half-core CPU quota;
- bounded execution/output and cleanup of only the executor's own containers.

AST policy remains active even with CPython libraries. This is not unrestricted
host execution. Both shipped Compose modes use restricted execution, named
storage volumes, non-root application processes, and no Docker socket. Legacy
HTTP's TCP check is not authenticated readiness; hosted `/health` checks the DB.
Hosted Docker additionally requires operator `GRYPHON_SAAS_DOCKER_ENABLED=true`
and separately provisioned daemon access/image/runsc; the UI does not provision
infrastructure or install arbitrary libraries.

## Persistence, cancellation, and external effects

`submit_code`, `get_run`, and `cancel_run` expose portable application receipts,
not native MCP Tasks or automatic workflow continuation. Matching owner-scoped
idempotency keys deduplicate requests only while the corresponding receipt is
retained. A changed request with the same key conflicts.

A process restart marks persisted `queued`/`running` work **`interrupted`**;
Gryphon does not automatically resume code or replay writes. This is not
exactly-once delivery: an API may accept an action before a crash prevents the
result from being recorded. Inspect upstream state before retrying.

Cancellation revokes broker authority and waits for the worker/cleanup. It
**cannot undo an already accepted API action**. Application-level compensation,
upstream idempotency, and operator reconciliation remain separate concerns.

## Operator checklist

- Run one process per run database and one hosted worker per control database
  under dedicated least-privileged OS/database users. The included PostgreSQL
  container is a dedicated instance; restrict its credentials/network access.
- Back up hosted PostgreSQL **and** the local channel-state volume together while
  the worker is stopped. Protect backups and test restoring both to one worker;
  a database-only backup loses recipes/receipts/artifacts. Backup scheduling,
  encryption, TLS termination, certificate renewal, and incident response are
  manual operator responsibilities, not automatic platform features.
- Keep the database password explicit and URL-safe in the hosted Compose profile.
  It has no external DB port. Changing an env value does not rotate an initialized
  PostgreSQL role's password; coordinate DB and application credential changes.
- Review trusted catalogs and auth configuration; keep secrets in private env
  files or the operator environment, never committed YAML or client examples.
- Leave private-network access and writes disabled unless deliberately required.
- Use exact allowlists, HTTPS upstreams, strong random HTTP tokens, and TLS before
  public exposure. Do not expose the Docker daemon or mount its socket in Compose.
- Keep `pyproject.toml`/`uv.lock`, images, host, and isolation runtime current.
  Dependency vulnerabilities can affect Gryphon's boundary even when the fix
  belongs upstream.
- Stop the server before `clean --yes`. Cleaning archives only recognized
  compiled output and closed recipe caches; it retains runs/artifacts and rejects
  unsafe paths, links, unknown content, and active SQLite sidecars. It is not
  secure erasure, and archives may still contain private data.
- Review `doctor` output before sharing: it omits secrets but includes paths and
  configuration metadata. It does not probe daemon health or validate API access.

## In-scope reports and limitations

Report sandbox/VM escapes, generated-host-code execution, credential disclosure,
SSRF/DNS-policy bypasses, unauthorized writes, owner-isolation failures, unsafe
cleanup/path traversal, schema/resource-bound bypasses, and cancellation or
recovery bugs that extend authority. Malicious specs/guides are in scope when
they bypass a Gryphon boundary; their mere existence is not a vulnerability.

A compromised host/operator, malicious behavior inside an already authorized
upstream service, and deployment outside the documented trust model cannot be
made safe by tool annotations or AST filtering. Third-party vulnerabilities that
affect Gryphon should be coordinated with both maintainers and upstream. No
claim is made of complete SaaS certification, zero vulnerabilities, formal
isolation verification, exactly-once effects, or protection against all side
channels. Admin-managed tenancy is implemented, but HA/horizontal scaling,
billing, SSO, user invitations, and tenant upstream secret management are not.
