# Gryphon

> **APIs were designed for developers. Gryphon makes them usable by AI agents.**

[![CI](https://github.com/hypen-code/gryphon/actions/workflows/ci.yml/badge.svg)](https://github.com/hypen-code/gryphon/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Gryphon is the guardian between agent-generated code and API authority:
**compile OpenAPI → discover → inspect → execute → reuse**, with a small set of
meta-tools, not one MCP tool per endpoint. Credentials stay in the host broker.

| Choose a mode | Easiest start | What you get |
|---|---|---|
| [Local stdio](#1-run-local-stdio) | Checkout + `uv run --frozen gryphon stdio` | One MCP entry; no Docker, database server, model key, or web login |
| [SaaS locally](#2-run-saas-with-the-web-ui) | Checkout + SQLite + `gryphon saas` | Browser UI, tenants, uploads, channels and keys on loopback |
| [Hosted server](#docker--postgresql-hosted-server) | Docker Compose hosted profile | Same UI with PostgreSQL; provision TLS yourself |
| [Legacy operator HTTP](#legacy-operator-http-and-maintenance) | `serve --transport http` | Single operator MCP endpoint; **no web UI or tenant management** |

**2.0.0** · Python **3.13+** · FastMCP **4.0.2** · `pydantic-monty` **0.0.18**.
Real-client tests cover MCP **2026-07-28** and legacy initialization; native MCP
Tasks are not implemented. Distribution: **`gryphon-runtime`**; package/CLI:
**`gryphon`**. No PyPI or container-registry publication is claimed.

## 1. Run local stdio

Install [uv](https://docs.astral.sh/uv/) and use Python 3.13+ on Linux/macOS
(POSIX storage locks); Windows users can use containers. From a new checkout:

```bash
git clone https://github.com/hypen-code/gryphon.git
cd gryphon
uv run --frozen gryphon stdio
```

That is enough for **empty-catalog offline compute**. The foreground process waits for MCP on stdin; it is not a web server or an interactive Python prompt. Usually your MCP client launches it instead. Stop this trial with **Ctrl+C** before launching another process against the same state.

For API discovery, add the entry below to your MCP client. The first `uv run` creates `.venv`; replace both checkout paths with actual absolute paths. No `.env`, config copy, or separate `compile` step is needed:

```json
{
  "mcpServers": {
    "gryphon": {
      "command": "/absolute/path/to/gryphon/.venv/bin/gryphon",
      "args": ["stdio"],
      "env": {
        "GRYPHON_SWAGGERS": "[{\"name\":\"weather\",\"swagger_url\":\"/absolute/path/to/gryphon/examples/weather.yaml\",\"is_read_only\":true}]"
      }
    }
  }
}
```

Alternatively use your absolute `uv` executable with args `["--directory", "/absolute/path/to/gryphon", "run", "--frozen", "gryphon", "stdio"]` and the same `env`. Clients may not expand `~`, `$PWD`, or your shell's `PATH`.
`GRYPHON_SWAGGERS` is **JSON encoded as an environment string**: use absolute local OpenAPI paths or policy-approved **public HTTPS specification URLs**. The weather file describes Open-Meteo; compiling that local file needs neither network access nor auth. Actually executing a weather call needs network access. For private/authenticated local-mode APIs, see [credentials and writes](#credentials-and-writes).

`stdio` compiles and serves from the launch environment only; it never discovers ambient `.env`. An explicit `--env-file /absolute/path/to/private.env` is optional. Omit sources or use `GRYPHON_SWAGGERS="[]"` for compute only. An explicitly selected `GRYPHON_SWAGGER_CONFIG_FILE` also works.
Optional absolute `GRYPHON_STATE_DIR` sets the private state root (default: absolute `$XDG_STATE_HOME/gryphon`, otherwise `~/.local/state/gryphon`). Catalog/cache/receipts/artifacts are source-scoped; explicit storage overrides are retained. No installed-package writes occur. Run **one process per run database**; use separate state roots for simultaneous clients. Stdio trusts its launcher (`local` owner). Logs use stderr, MCP uses stdout.

Development dependencies are **optional for running stdio**:
`uv sync --frozen --extra dev --extra saas` installs the full test environment;
the `saas` extra requires system **libpq** (Debian/Ubuntu: `libpq5`).

## 2. Run SaaS with the web UI

### Least setup: native SQLite on your machine

From the checkout root, install system **libpq** first (the hosted modules import
pure `psycopg==3.2.9` even with SQLite). No PostgreSQL server or Docker is needed.
Paste this as **separate lines**, not as one long `export` command:

```bash
uv sync --frozen --extra saas
umask 077
mkdir -p "$PWD/data/saas"
export GRYPHON_SAAS_DATABASE_URL="sqlite:///$PWD/data/saas/control.db"
export GRYPHON_SAAS_STATE_DIR="$PWD/data/saas/state"
export GRYPHON_SAAS_HOST=127.0.0.1
export GRYPHON_SAAS_PORT=8000
export GRYPHON_SAAS_PUBLIC_ORIGIN=http://127.0.0.1:8000
export GRYPHON_SAAS_ALLOW_INSECURE_HTTP=true
export GRYPHON_SAAS_ADMIN_TOKEN="${GRYPHON_SAAS_ADMIN_TOKEN:-$(uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(48))')}"
printf '%s\n' "$GRYPHON_SAAS_ADMIN_TOKEN"
uv run --frozen --extra saas gryphon saas
```

Create the SQLite parent directory **before** startup; SQLite does not create it. Use a dedicated private directory; `umask` does not repair existing permissions.
The token command generates a token **only when absent/empty**. Save it in a password manager and export that saved value in a new shell; do not generate a new recovery token on every restart. `printf` displays it only in your **private local terminal**: do not share it in chat, screenshots, logs, or a committed file.

Open **http://127.0.0.1:8000/**, expand **Bootstrap administrator token**, and paste
its **displayed value**, not `$GRYPHON_SAAS_ADMIN_TOKEN`. Keep the terminal running; **Ctrl+C**
stops the server. On restart, reuse the same database, state directory and token.
This explicit loopback-only HTTP exception disables Secure cookies; never use
it for public traffic. `saas` does not load ambient `.env`; use explicit
`--env-file /absolute/path/to/private-hosted.env` if preferred.

**No standalone compile is required:** choose **File**, **OpenAPI URL** or **UCP URL** in the browser.
`gryphon serve --transport http` is the legacy MCP-only server, **not this UI**.

### Docker + PostgreSQL hosted server

This is the easiest server deployment if you already have Docker and Compose
**2.24+**; the image includes hosted dependencies and `libpq5`. Provision a TLS
reverse proxy forwarding to **127.0.0.1:8001**, preserving the canonical **Host**
header. Set your actual HTTPS origin below; proxy headers are not trusted.
Run from the checkout root, retaining both generated secrets across restarts:

```bash
export GRYPHON_SAAS_ADMIN_TOKEN="${GRYPHON_SAAS_ADMIN_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')}"
export GRYPHON_POSTGRES_PASSWORD="${GRYPHON_POSTGRES_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export GRYPHON_SAAS_PUBLIC_ORIGIN=https://gryphon.example.com
export GRYPHON_SAAS_ALLOW_INSECURE_HTTP=false
docker compose --env-file /dev/null --profile hosted up --build -d gryphon-hosted
```

Open your canonical origin and log in with the saved admin token. The explicit
service target avoids starting legacy HTTP. Compose derives the database URL and
starts isolated **PostgreSQL 17.6**, with no published DB port and separate named
PostgreSQL/local-state volumes. The app is non-root, read-only, resource-limited,
restricted by default, and has **no Docker socket**. Secrets are checked when
selected services start, not during inactive-profile interpolation. Use a
URL-safe DB password; changing an env value does **not** rotate an initialized
PostgreSQL password. Native PostgreSQL deployments supply
`GRYPHON_SAAS_DATABASE_URL="postgresql://..."` separately.
For container loopback development only, use origin `http://127.0.0.1:8001` and
`GRYPHON_SAAS_ALLOW_INSECURE_HTTP=true`. `/health` checks DB readiness and requires
the canonical Host; it does not test all channels or upstream APIs.

### Tenant and channel workflow

1. Log in at `/` with the bootstrap admin token and create a tenant.
2. Import immutable versions: Swagger/OpenAPI **JSON/YAML file**, **OpenAPI URL**, or the bounded **UCP URL** subset below.
3. Create a channel bound only to that tenant's versions (or none for offline
   compute). Choose restricted execution; Docker requires operator provisioning.
4. Generate/rotate its key and copy it **once**; only the hash is stored. Connect
   an MCP streamable-HTTP client to `https://your-origin/mcp/{channelUUID}` with
   `Authorization: Bearer <channel-key>` (use your local origin for development).
5. Manage channels/users, revoke keys, suspend/resume tenants or confirm deletion, and inspect aggregate usage/audit.

Hosted **catalog-bound POST operations execute automatically**, without separate POST permissions or approval. **POST can have side effects; automatic execution does not mean read-only semantics.** The **Read-only filter** defaults checked: ordinary HTTP POSTs are excluded unless already classified by retained exact read attestations; uncheck to include supported POSTs for execution. Native MCP filtering uses the known read-method subset below. Hosted config enables `allow_catalog_posts=True` but still forces `allow_writes=False` and empty write permits: **PUT/PATCH/DELETE remain denied**, even if visible. Unbound/wrong-channel operations, unsupported methods, invalid arguments and network/budget violations remain blocked. Upstream access is **public-API-only**, without host-auth inheritance, tenant secrets, environment interpolation or caller-selected host paths. Ordinary OpenAPI external refs stay denied; bounded UCP may resolve approved refs. Channels retain independent stores/authority.
Channel create/edit offers **Include function names and descriptions** (`include_function_summaries`, default false): optional strict boolean on channel create/PATCH; omission on PATCH preserves the saved value. This channel-owned discovery choice overrides the operator base setting; it is not an MCP caller option. Compact mode remains the default; enabled mode includes all function summaries when they fit, otherwise requires bounded continuation (see [MCP interface](#the-mcp-interface)).
Admin `/api` sessions use bounded, in-memory **Secure, HttpOnly, SameSite=Strict** cookies with session-bound CSRF checks for mutations; restart invalidates sessions. Channel keys cannot administer `/api`; admin login does not grant MCP access without a separately issued channel key.

### Import, inspect and refresh specifications

Navigation uses consistent outlined icons. **API specifications** shows **one compact entry per parent-linked lineage**: ellipsized names/source, short version labels and count badges. **Version details** exposes full IDs, diagnostics/warnings, provenance and bindings for the latest or any historical version, including middle versions through **History**. Source/download, update-file/refresh-URL, filter, History and delete use accessible SVG buttons with labels, tooltips and keyboard focus. History is read-only, with no individual-version delete. Unrelated same-name roots stay separate. Channel selection prefers latest for new choices and labels existing **pinned older versions**; grouping is not a stable version ID or in-place overwrite. Browser-session/CSRF APIs (all under `/api/tenants/{tenant_id}`):
- `POST /specs`: file `{"name":"cse","content":"<OpenAPI JSON or YAML>"}`; URL `{"name":"store","url":"https://merchant.example","kind":"ucp"}` (or `kind:"openapi"`). Optional strict boolean `read_only_filter` defaults **true**. Never submit a host file path.
- URLs are at most **2048 characters**, without queries, userinfo or fragments. UCP requires HTTPS; a root URL becomes `/.well-known/ucp`. Fetches use bounded DNS-pinned HTTP, no auth inheritance, redirects, environment interpolation or proxies. Public OpenAPI relative server URLs resolve against the fetched document and are saved in the self-contained snapshot.
- `POST /specs/{spec_id}/refresh`: URL source `{}` refetches its saved URL; file source `{"content":"<replacement document>"}` requires replacement bytes. Optional strict boolean `read_only_filter` defaults to the previous version's choice; import/refresh dialogs expose the checkbox.
- `POST /specs/{spec_id}/filter`: `{"read_only_filter":false,"update_channels":false}` revalidates the **saved document**, without upload or remote fetch, and preserves source provenance. The row's **Read-only filter** icon opens a confirmation dialog. Both refresh/filter accept optional strict boolean `update_channels`, **false by default in the API**; the UI's **Update bound channels** starts **checked**.
- Changed refresh/filter returns **201**, creating an immutable successor with `parent_id`. A filter change counts even for unchanged GET-only bytes and changes catalog/policy identity (not necessarily document SHA-256). On opt-in, only exact old bindings/revisions advance atomically; invalidated runtimes drain before returning. Other settings and old snapshots stay intact.
- Unchanged document **and all import metadata** returns **200** with the old ID and no channel revisions, even if updating was requested. Identity includes diagnostics/warnings/filter, legacy grants, `mcp_bindings` and resolved provenance: raw native schema/endpoint changes create successors even when normalized OpenAPI is unchanged. Superseded updates return **409**; use the latest successor. Legacy uploads default to file provenance, filtering on and empty grants; no spec database schema migration. Retained legacy grants survive identical canonical documents and clear on document change; there is no new approval workflow.
- Diagnostics report `total_operations`, `available_operations`, `filtered_operations` and `unsupported_operations`. Availability is discovery inclusion, not authorization for every HTTP method. Unsupported ordinary OpenAPI request/schema semantics reject import. Native MCP explicitly **omits unsupported tools with warnings**; filter-off includes only supported tools, never full commerce by implication. REST UCP retains its GET subset.
- One active import, **no queued imports**, with a **25-second** import deadline and cancellation cleanup. Existing spec/storage quotas still apply.
- **Delete any specification** with the main-row trash icon: this permanently deletes its **whole parent-linked lineage**, not one version, and detaches every exact latest/older binding. `GET /specs/{spec_id}/deletion` previews `{name,specification_id,spec_id,version_ids,version_count,channels:[{id,name,revision}],confirmation_token}`; `specification_id` is the root, `spec_id` the requested version. `DELETE /specs/{spec_id}` requires the closed JSON object `{confirm_name,confirmation_token}`. Type the stored name **exactly**, including case/spaces; no trimming or coercion. The dialog starts blank, enables Delete only on exact match and guards stale tenant/session/dialog context. New versions or affected channel/binding changes invalidate consent: **409 requires a fresh preview and retyping, never automatic retry**. Malformed consent/bad names return **400**; nonexistent/foreign resources **404**. The token fingerprints tenant/root/requested ID, all version IDs and affected channel configurations/revisions; it is not an authorization capability. Browser auth/CSRF and fresh scoped authority checks still apply: tenant users only in their enabled tenant; platform admins/bootstrap can also clean up disabled tenants, as with key revocation.
- Exact name and current fingerprint are checked **before mutation in a serialized SQL transaction**. All lineage rows/bindings are removed atomically; each affected channel revision advances once with audit, then owned `finish_cleanup` drains affected runtimes before **200** `{deleted:true,specification_id,deleted_spec_ids,updated_channel_ids}`. Unrelated same-name roots, channel IDs/keys/other bindings/settings, usage, receipts and artifacts remain; no host-file/cache purge. Stale recipes reject catalog drift; receipts remain under normal retention. `spec_deleted` records the verified actor and root through optional `AdminAudit.spec_id` (legacy default `None`); old audits remain subject to **normal bounded retention**, not an unlimited archive. Only explicit confirmed lineage or entire-tenant deletion overrides snapshot retention; refresh/filter still never overwrite history. No migration or development deletion of operator data; tests use temporary stores.
Notifications have a close button and **10-second auto-dismiss**; replacement messages restart the timer. Inline dialog errors remain after banner dismissal; quiet sign-out clears pending notifications.

**UCP is a bounded REST/native MCP adapter, not full commerce or a platform identity provider.** Published **2026-01-11, 2026-01-23, 2026-04-08 and 2026-08-25** profile shapes are recognized. Supply a root, JSON profile URL, or explicit `/mcp` endpoint. Roots and explicit MCP routes probe the **same-origin `/.well-known/ucp` first**; a matching advertised shopping REST binding is preferred, otherwise MCP; malformed advertised contracts fail closed. Only explicit MCP routes fall back directly when the profile is unavailable. No arbitrary redirects or HTML links are followed. Public profile-advertised delegations are allowed subject to `GRYPHON_ALLOWED_DOMAINS` and DNS/TLS policy at every step.
REST remains the original subset: matching advertised GET IDs `get_checkout`, `get_cart`, `get_order` (January: checkout only), with schema-defined paths. Required **UCP-Agent/Request-Id** remain caller-supplied; required auth/signing rejects, including canonical January signing. Unsupported REST response schemas are omitted with explicit warnings, not claimed validation. No broad REST write adaptation or extension composition is added.
Native MCP import performs **initialize → notifications/initialized → paginated tools/list**, never business `tools/call`. The filter recognizes six read methods: `get_checkout`, `get_cart`, `get_order`, `search_catalog`, `lookup_catalog`, `get_product` (catalog.lookup). Unknown/non-read tools are hidden with filtering on; off includes **supported** native non-read tools for automatic execution, potentially with side effects. Unsupported native schema constraints (including patterns, combinators and refs) omit the entire tool with warnings rather than silently weaken validation. Preserve all required arguments, including `meta.ucp-agent.profile`, inside **`json_body`**; Gryphon does not generate a real platform identity or inherit auth secrets.
For native MCP, the operator may set **`GRYPHON_UCP_AGENT_PROFILE`** (default unset/`None`) to their **real, publicly fetchable platform profile**, not a fabricated URL or the merchant's profile as default. It must be public HTTPS, at most 2048 characters, without userinfo/query/fragment, quotes/control/unsafe escapes, interpolation or prohibited IP/metadata addresses. Structure/current `GRYPHON_ALLOWED_DOMAINS` policy are checked at initialization/use, and **all DNS answers must be public at use**, even if private networks are enabled. Gryphon does not fetch this identity document or prove it works; the operator must verify it. Never overwrite an existing real operator environment during setup/development.
Only bound native tools with the **entire profile path required in schema** receive omitted `meta`/`ucp-agent`/`profile` defaults. Explicit empty/invalid values are **never overwritten**; a valid explicit body profile wins with a matching `UCP-Agent: profile="URI"` header on native session requests, including initialization/discovery/owned cleanup as applicable. Ordinary OpenAPI receives no injection. `get_functions` adds parent `ucp_agent_profile` metadata (`required`, `operator_configured`, `input_path`, `guidance`) and an input-based usage example, never the configured URI or an empty-profile example. Changing the configured URI changes policy identity for recipes/receipts.
Saved `source_url` remains the original input; `resolved_profile_url`, `resolved_endpoint` and `source_transport` record resolution separately. Trusted `mcp_bindings` store the native name, endpoint and raw input/output schema fingerprint **outside the untrusted OpenAPI document**. View/download is the compiled catalog document, not a portable transport authorization: synthetic `/__mcp__/...` POST paths are **never actual upstream routes**. Refresh rediscovers metadata; saved filter changes need no remote fetch.
Each native invocation starts a fresh session, rediscovers tools and checks the raw name/input/output fingerprint **before one `tools/call`**; stale bindings return `conflict` and require refresh. No side-effect retries. JSON/SSE replies must match request IDs, with notification/session bounds. `structuredContent` is preferred; one finite JSON text block is decoded, while non-JSON/multimodal content blocks are retained without resource fetching. Known SDK handshake versions are negotiated (2025-11-25 proposal, compatible 2025-03-26 selection), not a claim of Tasks, modern server discovery or complete authentication support.
Discovery permits at most **1000 tools / 100 pages**, **32 REST schema documents**, and aggregate raw response bytes bounded by hosted/configured spec limits and **5 MiB**. MCP discovery has a **min(HTTP timeout, 30 seconds)** deadline within the outer **25-second** import limit; invocation initialize/list/call share the caller deadline and response-byte cap. Verified owned sessions may receive one fixed-endpoint cleanup DELETE under a separate **2-second / 1-KiB** nonfatal budget; cancellation awaits owned local cleanup. This protocol cleanup is not authorization for arbitrary DELETE operations. REST refs stay on the approved schema origin (profile origin, `https://ucp.dev`, or operator-approved), with structural/expansion limits.
The reported `https://coolbudget.lk/api/ucp/mcp` GET redirects to canonical WWW whose route returns HTML 404; following that redirect is not the solution. Its same-origin profile advertises `https://qhhihh-tw.myshopify.com/api/ucp/mcp`. Metadata-only import now resolves it, including compatibility with initialized ACK `200 {}`: **13 discovered tools, six reads, two supported cancel tools hidden by default, five unsupported schemas omitted**. This is not full commerce validation; no live business or payment calls were used. UCP discovery failures return useful HTTP **400 `ucp_discovery`** diagnostics, not POST-approval instructions.

### Users, passwords and workspace lifecycle

After bootstrap login, open **Users → Create user** and set a name, username, password and fixed role. Create/enable the tenant before assigning a tenant user. Keep the bootstrap token as recovery access; named users sign in with username/password.

| Role | Control-plane access |
|---|---|
| `platform_admin` | All tenants; create/list users, edit names, suspend/enable accounts, reset passwords and confirmed user/tenant deletion |
| `tenant_user` | Exactly one immutable, enabled tenant; its specs, channels (including confirmed deletion), keys, usage, analytics and scoped audit only |

Tenant users cannot create tenants/users, list/manage users, change roles/membership or administer another tenant. There is no self-signup. **Change password** requires the current password; sign in again afterward. Passwords use salted **PBKDF2-HMAC-SHA256, 600,000 iterations** (12–128 characters). Resets, profile/status changes and tenant status revisions invalidate affected sessions; re-enabling never revives old cookies. Roles/tenant assignments cannot be edited. Browser/platform authentication never replaces channel-key authentication for MCP.
Compact accessible SVG actions provide user name/status/password/delete, channel actions and **Tenant controls**, with labels, tooltips and keyboard focus. **Suspend tenant / Resume tenant** uses `PATCH /api/tenants/{tenant_id}` with strict boolean `{enabled:false|true}`: reversible access blocking, not deletion. Data and channel keys remain; resuming allows enabled channels' existing valid keys again, but tenant users must sign in again. After a fresh platform check, owned cleanup completes status/session updates and attempts every channel's revision-cutoff invalidation before success, even if one cleanup fails.
- **Delete tenant**: type the exact **tenant name** to permanently remove the **entire workspace**, including all assigned tenant users/password hashes, channels/key hashes/bindings, **all specification versions** and scoped usage/analytics. All workspace endpoints/keys stop working; other tenants and platform accounts remain. Suspend is the reversible alternative.
- **Delete channel**: type the exact **channel name**. Only that scoped channel, its key, bindings, usage/analytics and dependent control rows are removed. Other channels, tenant users and specifications remain; its MCP endpoint stops accepting access.
- **Delete user**: a platform administrator types the exact **username**, not display name. Deleting yourself or the **last enabled platform administrator** returns **409**; bootstrap also cannot delete the last enabled administrator. Use another administrator for self-offboarding; create/enable another before deleting the last enabled administrator. **User deletion revokes only that browser account and its sessions; independently issued shared channel keys remain valid. Offboarding must also rotate/revoke exposed channel keys.** Disable/reset likewise does not revoke channel keys; shared channels/specifications remain.
- Resource paths are `/api/tenants/{tenant_id}`, `/api/tenants/{tenant_id}/channels/{channel_id}` and `/api/users/{user_id}`. `GET <resource>/deletion` returns `{kind,id,name,label,confirmation_token,impact}` (tenant previews also include child IDs); `impact` counts `users`, `channels`, `specs` (versions), and user `name` is the username while `label` is the display name. `DELETE <resource>` accepts only `{confirm_name,confirmation_token}`. Exact stored name/username and current public-state fingerprint are checked before mutation in serialized SQL: **no trimming, case folding or coercion**. Tokens are consent fingerprints, never authorization.
- Changed configuration/revision/bindings or tenant children invalidates consent: **409 requires a fresh preview/token and retyping from blank, never automatic retry**. Malformed consent/wrong name returns **400**, missing/foreign scoped resources **404**, forbidden platform actions/CSRF **403**, invalid sessions **401**, according to existing authorization checks. Browser auth/CSRF and fresh server-side scope checks remain mandatory: tenant/user deletion is platform-only; channel deletion permits enabled own-tenant members or platform admins/bootstrap, including platform cleanup of disabled tenants. Dialogs guard stale session/tenant/context and gate Delete on exact match.
- Deletion commits control rows/audit atomically, freeing corresponding quotas. Owned cleanup then revokes deleted users' sessions with cutoff `user.revision + 1` and invalidates channels using deleted snapshots with `before_revision=deleted.revision + 1`, covering every revision through deletion, including cold snapshots—not unconditional `None` invalidation. Every cleanup is attempted **before 200** `{deleted:true,tenant_id|channel_id|user_id}`, even if one fails; failure is not success. Hosted runtime acquisition validates the exact enabled database snapshot under its lock before cached/new acquisition and after startup; stale/deleted authority rejects with owned failed-start cleanup. Completed hosted revocation markers are forgotten under that lock, preventing unbounded deleted-ID growth; standalone managers without a validator retain legacy watermarks. Gateway key checks remain mandatory; database mutations commit before acquiring the runtime-manager lock, never wait for it while holding a database transaction. Recreated usernames get **new UUIDs**; historical actor IDs never resolve by username.
- Local sandbox/cache/recipes, execution receipts and artifacts are **not automatically erased**. For tenant/channel deletion they remain private on disk but inaccessible through removed endpoints. This is not secure erasure, backup deletion, undo of upstream effects or HA; protect retained local state/backups separately.
**Audit** separates actor from subject: nested `actor` has `id`, `username`, `name`, `kind` (`user/bootstrap/system/unknown`) and `display_source` (`snapshot/current/unknown`). Resource and user-deletion events retain saved public actor snapshots; legacy account events resolve current names by actor ID only, labeled **Current account name**, or unknown after deletion—never invented historical names or inferred bootstrap. An additive `saas_audit_archive` preserves affected history while retaining existing live foreign keys/CHECK constraints, without rebuilding tables. Live + archive together share normal **10,000-event retention per category** (resource/user), not unlimited retention. Only authorized scoped events expose actors, never a tenant-accessible user directory.

### Tenant analytics: measured value, not billing

Select **Analytics**, a **7/30/90-day UTC** window and optionally a channel;
**Download raw JSON** includes methodology and `window.tenant_id` / `window.channel_id` (null for all channels).
Browser API: `GET /api/tenants/{tenant_id}/analytics?days=7&channel_id=<id>`;
`days` accepts **1–90** (default 7), `channel_id` is optional and tenant-checked.
Existing browser roles apply: platform admins can select any tenant; tenant users
see only their enabled tenant. Channel keys cannot read this API.

| Evidence | Meaning and limits |
|---|---|
| Canonical payload bytes | Paired successful eligible API-backed runs: accepted API JSON before code reduction versus **full final JSON, including artifact data**, not inline summaries. |
| Signed payload reduction | `100 × (sum upstream − sum final) / sum upstream` over those same pairs. Negative means expansion; no API baseline is **null / N/A**, never 100%. |
| Actual code reuse | Recorded replay backend starts / all recorded backend starts, separate from failed replay requests. UI shows request/run error categories; cache errors span all requests, not an exact cache-miss versus storage-failure count. Idempotent duplicates do not add runs. |
| Source/workload | UI shows source bytes/static lines and top-level JSON-array item totals, not executed instructions or semantic records. `reused_source_bytes` is code executed again, not measured LLM generation avoided. |
| Broker activity | `api_calls` counts broker attempts, including some blocked before HTTP dispatch; `api_responses` counts accepted validated returns only. Pure compute means a backend start with zero broker attempts. |
| Traffic | Observed request-body bytes and **SDK-produced response-body** bytes for allowlisted `tools/call` names—not proven client reception/model consumption. `structured_payload_bytes` counts canonical structured output once. |
| Token equivalents | UI separates **ESTIMATED** wire and structured-payload equivalents: `ceil(UTF-8 bytes / 4)`, not a tokenizer. Round per observed request field / comparable run then sum; reused-source estimate rounds the total. |
| Timing | UI shows queue/backend averages and **response-production latency**, excluding its own observation persistence—not client end-to-end latency. Response-production/run histogram p50/p95 are **upper bounds**, not exact percentiles. Wall times overlap, not CPU/time saved; backend includes network; broker wraps credentials/dispatch/response checks, not earlier argument/policy guards; queue includes preparation. |

“Original”/“raw API” means broker-accepted, validated, decoded canonical JSON, **not raw HTTP response bodies or an LLM-without-Gryphon counterfactual**.
Traffic allowlists **13 tool names (eleven core/two optional)**; SDK-rejected known names can count as errors. Discovery, metadata, `get_run` polls, artifact reads and transformations count; exclusions include initialization, `tools/list`/SDK negotiation, HTTP headers and agent context. Wire bytes include duplicate structured/text representations; only the structured-payload counter counts that payload once. Artifact projection is execute-origin pure compute, not replay or a new API-backed reduction baseline.
Tool definitions and all client context are **not** claimed to fit or be counted by Gryphon's response/context budgets. Wire token estimates are not actual model tokens. Actual model context, generation, reasoning and billing are unobservable (**null / N/A**); no dollar, CPU, time or round-trip savings are guaranteed. Request count is not run count.

Terminal observation includes background submissions when they finish, even without
polling; failures/cancellations remain separate. Observation is **best effort**:
crashes/failures can miss records, not a complete billing audit. Legacy usage/request metrics attempt persistence before final body handoff (two-second timeout per writer); failures are nonfatal, incomplete SDK responses use a fallback observation.
Daily bounded aggregate JSON covers **90 UTC days**; `recording_since` marks the
first measurement, with no lifetime backfill. At most **100,000 deduplication receipts per tenant** are retained;
other tenants cannot evict in-retention receipts. Deduplication lasts only while retained; total storage is bounded by the hosted 100-tenant quota.

For marketing, replace placeholders only with the selected report's evidence:
“Across **N** successful paired API-backed runs in UTC window **[start, end)**,
canonical payload reduced **X%**, with **R%** of recorded backend starts using cached
code. Tokens are estimated, not billing, and exclude agent context.” Report expansion
honestly, not as savings. Operational success/reduction do not establish answer correctness or equivalent task quality. Review exports for activity metadata.

### Restart, upgrade, leases and backups

Normal startup adds user/account-audit, analytics and `saas_audit_archive` tables, preserving existing live foreign keys/CHECK constraints, tenants, uploads, channels, keys and configuration without table rebuilds. Stop and back up first; no separate migration command is required.
Never test schema upgrades/development migrations against an operator's real database.

**Exactly one active SaaS process per control database.** Leases are automatic; no lock installer or separate service. Stop the existing server with **Ctrl+C** before starting another; never delete `.lock` files to release live workers/jobs.
PostgreSQL uses a **session-level advisory lock**: direct connection or session pooling, **not transaction pooling**. Local per-channel run ledgers retain exclusive POSIX locks.

PostgreSQL stores control metadata, immutable specs, key hashes, aggregate usage/analytics and audit—not execution recipes, run receipts or artifacts.
Back up/restore the database **and private local state volume together while the worker is stopped**. Protect disks, backups and secrets; test restoration. TLS, monitoring and incident response are operator responsibilities.
This is not HA/horizontal SaaS, billing, SSO, user invitations, encryption-at-rest assurance or security certification. See [SECURITY.md](SECURITY.md).

## Install a built package or release through GitHub

Checkout installs work today. To install a locally built wheel into a Python
3.13+ virtual environment (the second command selects hosted dependencies):

```bash
uv build
python -m pip install /absolute/path/to/gryphon/dist/gryphon_runtime-2.0.0-py3-none-any.whl
python -m pip install '/absolute/path/to/gryphon/dist/gryphon_runtime-2.0.0-py3-none-any.whl[saas]'
```

The distribution is **`gryphon-runtime`**, not `gryphon`. **Only after version 2.0.0 has actually been published to PyPI**, use `uvx --from gryphon-runtime==2.0.0 gryphon stdio` or `python -m pip install gryphon-runtime==2.0.0`. An MCP entry can then use your absolute `uvx` executable, args `["--from", "gryphon-runtime==2.0.0", "gryphon", "stdio"]`, and the same `GRYPHON_SWAGGERS` environment (provide your own spec file or public HTTPS spec URL). Hosted package installs use `gryphon-runtime[saas]==2.0.0`. These are conditional instructions, **not a publication claim**.
See [CONTRIBUTING.md](CONTRIBUTING.md) for maintainer release setup and quality gates.

## The MCP interface

| Core tool | Purpose |
|---|---|
| `list_servers` | Compact, paginated API summaries |
| `search_functions` | Discover capabilities without loading the whole catalog |
| `get_functions` | Inspect schemas/invocation metadata for 1–5 functions |
| `execute_code` | Run code with structured `inputs` and optional `input_schema` |
| `run_cached_code` | Reuse exact source with a complete new `params` object |
| `submit_code` | Submit work and receive a persistent run receipt |
| `get_run` / `cancel_run` | Poll caller-owned work / revoke and await cleanup |
| `list_recipes` | Search caller-owned cached recipe summaries |
| `read_artifact` | Read caller-owned JSON in bounded chunks |
| `transform_artifact` | Reduce an owned saved artifact offline; no upstream calls or replayable recipe |

`reusable_code_guide` explains execution on demand. Optional `GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and `get_server_skills` in local/operator mode. Guides are bounded, untrusted data, not write approval. Discovery includes fingerprints/truncation; follow continuation or narrow searches. MCP `readOnlyHint` is **not authorization**.
`list_servers` stays compact by default. Hosted channels choose `include_function_summaries`; local/operator config uses `GRYPHON_INCLUDE_FUNCTION_SUMMARIES=true`. When enabled, server rows include `functions` with names/descriptions, all when they fit—not a fixed sample. Larger collections paginate within response budgets: pass **both** `next_cursor` and `next_function_cursor` back as `cursor` and `function_cursor`; a nonzero function cursor continues the same server. `limit` counts servers, not functions; compact mode requires `function_cursor=0`. Oversized text is marked truncated. Restart pagination if the registry fingerprint changes; use `get_functions` for schemas.

### Execute, then reuse

Search for weather and inspect with `get_functions`:
`{"functions":[{"server_name":"weather","function_name":"get_forecast"}]}`.
This `execute_code` request actually calls the public API:

```json
{
  "code": "result = await call_tool(\"weather.get_forecast\", {\"latitude\": inputs[\"latitude\"], \"longitude\": inputs[\"longitude\"], \"current\": \"temperature_2m\"})",
  "description": "current weather by coordinates",
  "inputs": {"latitude": 51.5074, "longitude": -0.1278}
}
```

The restricted capability takes **two arguments**:
`await call_tool("server.function", arguments)`. Pass the returned `cache_id`
to `run_cached_code` with a complete new `params` object:
`{"cache_id":"<returned-cache-id>","params":{"latitude":48.8566,"longitude":2.3522}}`.
Inputs are never rewritten into source. A recipe caches code, not API results.
Assign JSON-native `result`; `main()` is neither required nor auto-called.
Restricted Python has no imports, filesystem, direct network or host environment.
Optional bounded `input_schema` rejects references, regexes and combinators
(`$ref`, `pattern`, `allOf`, `anyOf`, `oneOf`). Recipe identity binds owner, exact
source, schema and catalog/policy digest; replay revalidates and rejects drift.
Reordering/duplicating exact write permits does not change identity; changing the set does.

### UCP search, then reduce the saved result

Before launching/restarting Gryphon, have the operator supply their real fetchable **platform** profile. The following URL is a **placeholder**, not a working identity; replace it, and preserve any existing real setting:
```bash
export PUBLIC_PROFILE='https://your-platform.example/.well-known/ucp'
export GRYPHON_UCP_AGENT_PROFILE="$PUBLIC_PROFILE"
```
After importing/binding the merchant UCP source as `shop`, inspect `shop.search_catalog` with `get_functions`; confirm the actual native schema and `ucp_agent_profile.operator_configured` (no URI is exposed). `doctor` also reports only `ucp_agent_profile_configured`, not fetchability.
1. Send `execute_code` with catalog inputs only; omitted required identity is filled by the broker:
```json
{"code":"result = await call_tool('shop.search_catalog', {'json_body': inputs})","description":"Search catalog","inputs":{"catalog":{"query":"bedsheets"}}}
```
2. If `response.truncated` and `response.artifact_id` are present, inspect `response.data.json_type` / `top_level_keys` and send `transform_artifact` using that returned ID. This **synthetic shape example** assumes an object with `products` containing `title`; adapt to the actual inspected shape, not every merchant's response:
```json
{"artifact_id":"<response.artifact_id>","code":"products = inputs['artifact']['products']\nresult = {'count': len(products), 'titles': [p['title'] for p in products[:inputs['params']['take']]]}","description":"Summarize saved search","inputs":{"take":5}}
```
This reduces already saved JSON without a second upstream call or assembling, for example, 21 `read_artifact` chunks. Normal `run_cached_code` is different: it reruns the original recipe and refetches. `tests/integration/test_ucp_search_workflow.py` exercises this with a synthetic peer, not a live merchant search verified this cycle. The user's report that a fetchable profile worked does not make the merchant profile our configured platform identity; earlier live evidence above remains metadata-only.

### Results, receipts and cancellation

Native structured results contain `success`, result `data`, available handles and stable `error_type` categories, including `sandbox_unavailable`. Failure messages come from a finite Gryphon-owned catalog; public results and nested receipts preserve only freshly validated diagnostics, never raw error text or forged extra fields. Missing/invalid native profiles and the exact known RPC condition return `error_type:"upstream"`, static configuration guidance and `diagnostic:{kind:"upstream",phase:"discovery"|"invoke",upstream_code:"invalid_profile_url"}`. Other well-formed RPC errors expose phase only, never raw messages, `data.content`, `continue_url`, numeric codes or private bodies.
AST rejection remains `error_type:"security"`, with `diagnostic:{kind:"ast",violation_type:<closed enum>,line:1..1000000}`; imports/calls/attributes/global/nonlocal remain guarded. No source, detail, prints, stderr or traces are exposed.
Large permitted JSON becomes owner-scoped artifacts. Summary `data` includes `json_type`; objects add `top_level_keys` (at most 32 complete keys / 512 serialized bytes, fewer under small budgets), `key_count`, `keys_truncated`; arrays add `length`, never value previews. Prefer `transform_artifact` for local reduction. `read_artifact` remains available: follow `next_offset` to `eof`, at most **8192 bytes** per chunk, subject to context/full-result limits.
`transform_artifact` exposes saved JSON as `inputs['artifact']` and caller parameters as `inputs['params']`. It uses a fresh restricted Monty VM with **no external functions, broker reference or network**; `call_tool` references/aliases and imports are blocked. Owned integrity-checked loading is bounded/off-loop, with normal AST/resource limits, admission, local/shared slots, deadline, cancellation and run ledger. Docker configurations reject without downgrade. Results can become artifacts again; receipts remain, but projections return **no replayable `cache_id`**.
`submit_code` returns an `id`; poll `get_run(run_id=...)`. States: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `interrupted`. Optional owner-scoped `idempotency_key` deduplicates matching requests only while retained; conflicts fail. Restart marks queued/running work **interrupted**, with **no automatic replay, crash resume or exactly-once effects**. Inspect upstream state before retrying. Cancellation revokes authority and awaits cleanup but cannot undo accepted API actions. Receipts/artifacts are not a workflow engine or permanent audit archive.

## Configure APIs and policy

Local/operator YAML uses `servers: [{name: weather, swagger_url: ./examples/weather.yaml, is_read_only: true}]`.
`swagger_url` accepts local files or policy-approved HTTP(S) documents; relative paths use the compilation directory. `base_url` overrides the spec URL.
OpenAPI **3.0/3.1** and Swagger **2.0** produce deterministic v2 manifests; 3.2 is unsupported. Unsupported request semantics fail closed; unsupported response schemas are reported/omitted, not falsely validated. Supported responses are validated.
Optional query/header null means omission; required/path null and null query-array elements fail. Declared JSON-body null works.
Generated Python is SDK documentation, **never host-executed**; `top_level_functions` promotes no MCP tools. LLM enhancement (`--llm-enhance` / `GRYPHON_LLM_ENHANCE=true`) is retired and explicitly rejected.

### Credentials and writes

Public APIs need no auth block. **Local/operator mode** supports trusted source auth such as `auth: {type: static, value: "Bearer ${UPSTREAM_API_TOKEN}"}`; export real credentials only to the broker process, never commit them.
Host auth supports `static`, `jwt`, `basic`, OAuth2 client credentials (`oauth2`), `keycloak` and `session`, with refresh. Alternatives: `GRYPHON_{SERVER}_AUTH` and JSON `GRYPHON_{SERVER}_EXTRA_HEADERS`.
Neither sandbox receives credentials. **Hosted channels cannot use these credentials**; public upstream APIs only.

`is_read_only: true` filters ordinary HTTP writes at compilation and dispatch; retained exact read-only POST classifications remain compatible. **Local/operator** `GRYPHON_ALLOW_CATALOG_POSTS` defaults **false**: ordinary writes require a write-enabled source, `GRYPHON_ALLOW_WRITES=true` and exact `GRYPHON_ALLOWED_WRITE_OPERATIONS` permits. An operator may explicitly enable catalog POST execution; it never bypasses an unclassified read-only source. **SaaS enables catalog POSTs on a private config copy**, not by changing operator settings, while PUT/PATCH/DELETE remain denied. No model flag, guide, idempotency key or hint grants authority.

### Automatic catalog POSTs and CSE forms

For hosted POST APIs, import with **Read-only filter unchecked**, bind the version to the channel, inspect its canonical functions, then execute: **no separate POST permissions step**. Included supported POSTs can mutate upstream state; names and uploaded hints do not prove read semantics. The original CSE document has **1 GET + 25 POST** (**23 URL-encoded + 2 scalar multipart**); document-only parsing exposes all 26 with filtering off. Synthetic CSE-shaped calls/replay use real Monty and mocked HTTP, not live CSE calls or proof every endpoint works.
The manual **POST read permissions** UI and public route are removed: **GET and POST `/api/tenants/{tenant_id}/specs/{spec_id}/post-reads` return 404**. Legacy `approved_post_reads` metadata/helpers remain for old payload compatibility, and removal of that approval workflow does not delete historical `post_reads_updated` audits or immutable versions. Audits remain under normal bounded retention; versions remain unless explicitly confirmed whole-lineage or entire-tenant deletion is requested. There is no replacement approval UI or route.
`GRYPHON_ALLOWED_READ_ONLY_POST_OPERATIONS` remains an optional empty-default operator compatibility setting: exact canonical server/effective base/literal path/POST tuples, checked by parser/broker and included in policy identity. It is deployment-wide, not credentials or tenant authorization, and is **not required for included hosted POSTs**. Example tuple shape: `{"server_name":"cse","base_url":"https://market.example/api","path":"/companyInfoSummery","method":"POST"}`. No templates/globs or automatic operator-env edits.
For source **cse**, use namespace **`cse`**, not fixture `cse_api`. Use `list_servers`, `search_functions` and `get_functions` to inspect canonical names, schemas, method/path and effective destination. After inspection, the two-argument shape is:
```python
result = await call_tool("cse.get_company_info_summery", {"json_body": {"symbol": inputs["symbol"]}})
```
The illustrative name must match the channel catalog. URL-encoded and multipart forms accept a closed **`json_body` object** of scalars/scalar arrays (repeated fields); nested/null/binary/file values reject. The broker chooses encoding; multipart never reads host files or emits caller-selected filenames, with **1024 parts / 2 MiB** bounds. Automatic POSTs still enforce source/catalog/channel scope, schemas, DNS/TLS, deadlines and byte/call budgets.

### Runtime settings

See [`.env.example`](.env.example), [`GryphonConfig`](src/gryphon/config.py), and [`SaaSConfig`](src/gryphon/saas_config.py). Explicit environment wins over dotenv.
Legacy `compile`/`serve`/`run` discover working-directory `.env` with checkout-root fallback; **`stdio` and `saas` load only explicit `--env-file`**.
Defaults: restricted VM, 30-second deadline, 64,000,000-byte memory, four active runs **shared across hosted channels**, five-second admission wait, 50 calls;
65,536-byte source/inline, 2,097,152-byte full-result and 16,384-byte discovery budgets, ten items/page. Recipes: 3600 seconds/500 entries; receipts: 86400 seconds/1000 entries; artifacts: 100 per owner.
Configure through `GRYPHON_*`. Public API/auth/spec destinations **require HTTPS**; `GRYPHON_ALLOWED_DOMAINS` is an additional exact-host JSON array, not wildcard/CSV authority.
Private/loopback HTTP needs `GRYPHON_ALLOW_PRIVATE_NETWORKS=true` and approved DNS answers. Metadata, link-local, reserved and mixed public/private destinations stay denied.
DNS pinning, original Host/SNI, origin-isolated pools, verified TLS, no environment proxies/redirects and response bounds remain mandatory.

## Legacy operator HTTP and maintenance

For existing file-based local/operator deployments, copy `.env.example` and `config/swaggers.yaml.example` only into **new private configuration files**; never overwrite existing configuration. Review before `gryphon compile`.
Successful standalone `compile` prints non-secret MCP client JSON even when unchanged, pinning absolute storage/env-file paths and disabling recompilation.
Dry runs, failures, empty/startup compilations emit no client JSON; client config is never automatically changed or populated with credentials.
`serve` respects `GRYPHON_COMPILE_ON_STARTUP`; `run` compiles once then serves. `--env-file` works before or after the subcommand.

```bash
export GRYPHON_HTTP_AUTH_TOKEN="${GRYPHON_HTTP_AUTH_TOKEN:-$(uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(48))')}"
uv run --frozen gryphon serve --transport http
```

Connect to `http://127.0.0.1:8000/mcp` with `Authorization: Bearer <your-token>` (at least 32 characters). The fixed owner is `operator`, not a tenant/user identity; sharing this token shares ownership. **No web UI**.
Public exposure requires TLS and operator controls. After preparing file-based config/token, `docker compose up --build -d gryphon` starts legacy HTTP:
loopback port 8000, private named volumes, read-only config, no Docker socket. Its TCP check is not authenticated readiness or hosted `/health`.

`gryphon --version` reports the version; `doctor` emits read-only safe JSON, including boolean `ucp_agent_profile_configured`, without compilation, store opens, API calls or Docker probing; it never reports the configured profile URI or proves fetchability.
`compile --dry-run` validates without output writes but may fetch remote specs. **Stop before `clean --yes`**: it archives recognized compiled output/closed recipe caches to adjacent `.gryphon-archive-...` paths, retaining runs/artifacts/config.
`clean --yes --dry-run` only validates; `clean compile --yes` archives then compiles. Links, unknown files, overlapping paths and SQLite sidecars are refused.
Restore manually while stopped without overwriting newer data; this is not arbitrary deletion or hosted backup.

## Optional offline full Python

Docker is only for offline CPython using [`sandbox/requirements.txt`](sandbox/requirements.txt). Provision daemon, image (`docker build -t gryphon-sandbox:2.0.0 sandbox/`) and **gVisor `runsc`** yourself; select `GRYPHON_SANDBOX_MODE=docker`.
Missing requirements fail closed, never autostart/downgrade. Runs use UID 1000, dropped capabilities, no-new-privileges, read-only root, bounded tmpfs, 64-PID/256-MiB RAM-and-swap/half-core limits; no host mounts, network, credentials or `call_tool`.
AST checks still apply. Hosted Docker requires `GRYPHON_SAAS_DOCKER_ENABLED=true` and manual daemon access outside Compose.
`GRYPHON_SANDBOX_ALLOWED_IMPORTS` is a JSON list: `[]` denies imports; unset preserves offline defaults. Only approved preinstalled imports, never arbitrary installs.

## Development and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for locked Ruff, strict mypy, pytest/pre-commit commands, optional PostgreSQL/Docker checks and release instructions.
The **90% coverage floor** is mandatory; target **100%**. Normal tests need no live upstream, Docker or PostgreSQL but import the `saas` extra. The offline real-MCP demo is `uv run --frozen python examples/demo.py` (use isolated config/stores).
UI state tests: `node --test tests/unit/specifications_ui.test.js`; `node --check src/gryphon/static/admin.js`; `node --check src/gryphon/static/specifications.js`; `node --check tests/integration/browser_ui.cjs`. With separately provisioned Puppeteer/Chromium, opt in to `uv run --frozen --extra saas python tests/integration/browser_ui_fixture.py` for disposable browser checks (compact details/action tooltips, grouped read-only history, exact-name whole-lineage deletion/stale-context guards, absence of POST approval controls, actors, filters, bindings, channel summaries, notifications). Deletion suites: `uv run --frozen --extra saas pytest tests/unit/test_saas_spec_delete.py tests/integration/test_saas_spec_delete_http.py`; see [AGENTS.md](AGENTS.md) for prerequisites and targeted automatic-POST/native-MCP/audit suites. These commands are verification instructions, not a claim that pending full gates have passed.
Lifecycle checks: `uv run --frozen --extra saas pytest tests/integration/test_admin_lifecycle.py tests/integration/test_saas_user_delete_http.py tests/unit/test_saas_resource_delete.py tests/unit/test_saas_resource_delete_audit.py tests/unit/test_saas_user_delete.py tests/unit/test_saas_audit_archive.py tests/unit/test_saas_runtime_validation.py tests/unit/test_saas_lifecycle_cleanup.py`; `node --test tests/unit/lifecycle_ui.test.js`; `node --check src/gryphon/static/lifecycle.js`; `node --check tests/integration/browser_lifecycle.cjs`. Opt-in `GRYPHON_TEST_POSTGRES=1 uv run --frozen --extra saas pytest tests/integration/test_saas_postgres.py` covers user/channel/tenant deletion and archives on disposable PostgreSQL 17.6. The disposable browser fixture above exercises lifecycle actions, exact-name consent and offboarding warnings; commands do not claim browser/full gates passed.
For offline measurements including receipts, run
`uv run --frozen python examples/benchmark.py --iterations 25 --concurrency 1`;
these are not model/API or end-to-end agent benchmarks.
See [AGENTS.md](AGENTS.md), [CHANGELOG.md](CHANGELOG.md), and [ROADMAP.md](ROADMAP.md).
Report [bugs](https://github.com/hypen-code/gryphon/issues) publicly and
[vulnerabilities](SECURITY.md) privately. MIT licensed; see [LICENSE](LICENSE).
