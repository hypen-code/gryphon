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

That is enough for **empty-catalog offline compute**. The foreground process
waits for MCP on stdin; it is not a web server or an interactive Python prompt.
Usually your MCP client launches it instead. Stop this trial with **Ctrl+C**
before launching another process against the same state.

For API discovery, add the entry below to your MCP client. The first `uv run`
creates `.venv`; replace both checkout paths with actual absolute paths. No
`.env`, config copy, or separate `compile` step is needed:

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

Alternatively use your absolute `uv` executable with args
`["--directory", "/absolute/path/to/gryphon", "run", "--frozen", "gryphon", "stdio"]`
and the same `env`. Clients may not expand `~`, `$PWD`, or your shell's `PATH`.
`GRYPHON_SWAGGERS` is **JSON encoded as an environment string**: use absolute
local OpenAPI paths or policy-approved **public HTTPS specification URLs**.
The weather file describes Open-Meteo; compiling that local file needs neither
network access nor auth. Actually executing a weather call needs network access.
For private/authenticated local-mode APIs, see [credentials and writes](#credentials-and-writes).

`stdio` compiles and serves from the launch environment only; it never discovers
ambient `.env`. An explicit `--env-file /absolute/path/to/private.env` is optional.
Omit sources or use `GRYPHON_SWAGGERS="[]"` for compute only. An explicitly selected
`GRYPHON_SWAGGER_CONFIG_FILE` also works. Optional absolute `GRYPHON_STATE_DIR`
sets the private state root (default: absolute `$XDG_STATE_HOME/gryphon`, otherwise
`~/.local/state/gryphon`). Catalog/cache/receipts/artifacts are source-scoped;
explicit storage overrides are retained. No installed-package writes occur.
Run **one process per run database**; use separate state roots for simultaneous
clients. Stdio trusts its launcher (`local` owner). Logs use stderr, MCP uses stdout.

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

Create the SQLite parent directory **before** startup; SQLite does not create it.
Use a dedicated private directory; `umask` does not repair existing permissions.
The token command generates a token **only when absent/empty**. Save it in a
password manager and export that saved value in a new shell; do not generate a
new recovery token on every restart. `printf` displays it only in your **private
local terminal**: do not share it in chat, screenshots, logs, or a committed file.

Open **http://127.0.0.1:8000/**, expand **Bootstrap administrator token**, and paste
its **displayed value**, not `$GRYPHON_SAAS_ADMIN_TOKEN`. Keep the terminal running; **Ctrl+C**
stops the server. On restart, reuse the same database, state directory and token.
This explicit loopback-only HTTP exception disables Secure cookies; never use
it for public traffic. `saas` does not load ambient `.env`; use explicit
`--env-file /absolute/path/to/private-hosted.env` if preferred.

**No standalone compile is required:** upload OpenAPI JSON/YAML in the browser.
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
2. Upload immutable Swagger/OpenAPI **JSON or YAML** versions for that tenant.
3. Create a channel bound only to that tenant's versions (or none for offline
   compute). Choose restricted execution; Docker requires operator provisioning.
4. Generate/rotate its key and copy it **once**; only the hash is stored. Connect
   an MCP streamable-HTTP client to `https://your-origin/mcp/{channelUUID}` with
   `Authorization: Bearer <channel-key>` (use your local origin for development).
5. Manage channels, revoke keys, disable tenants, and inspect aggregate usage/audit.

Uploaded catalogs are read-only and **public-API-only**: no upstream host-auth
inheritance, tenant secret manager, external references, environment interpolation,
or caller-selected host paths. Channels have independent runtime stores/authority.
Admin `/api` sessions use bounded, in-memory **Secure, HttpOnly, SameSite=Strict**
cookies with session-bound CSRF checks for mutations; restart invalidates sessions.
Channel keys cannot administer `/api`; an admin login does not grant MCP access
without a separately issued channel key.

### Users and passwords

After bootstrap login, open **Users → Create user** and set a name, username,
password and fixed role. Create/enable the tenant before assigning a tenant user.
Keep the bootstrap token as recovery access; named users sign in with username/password.

| Role | Control-plane access |
|---|---|
| `platform_admin` | All tenants; create/list users, edit names, disable/enable accounts and reset passwords |
| `tenant_user` | Exactly one immutable, enabled tenant; its specs, channels, keys, usage, analytics and scoped audit only |

Tenant users cannot create tenants/users, list users, change roles/membership or
administer another tenant. There is no self-signup. Named users can select
**Change password** with their current password; sign in again after changing it.
Passwords use salted **PBKDF2-HMAC-SHA256, 600,000 iterations** (12–128 characters).
Resets, profile/status changes and tenant disabling invalidate affected sessions
by revision; re-enabling never revives old cookies. Roles/tenant assignments
cannot be edited. Account disable/reset does **not** revoke separately issued
channel keys: rotate/revoke exposed keys when offboarding. Browser/platform
authentication never replaces channel-key authentication for MCP.

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

“Original”/“raw API” here means broker-accepted, validated, decoded canonical JSON,
**not raw HTTP response bodies or an LLM-without-Gryphon counterfactual**.
Traffic allowlists 12 tool names (ten core/two optional); SDK-rejected known names can count as errors.
Discovery, metadata, `get_run` polls and artifact reads count; exclusions include initialization,
`tools/list`/SDK negotiation, HTTP headers and agent context. Response wire bytes
include any duplicate structured/text representations; only the structured-payload
counter counts that payload once. Wire token estimates are not actual model tokens.
Tool definitions and all client context are **not** claimed to fit or be counted
by Gryphon's response/context budgets. Actual model context, generation, reasoning
and billing are unobservable (**null / N/A**); no dollar, CPU, time or round-trip
savings are guaranteed. Request count is not run count.

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

Normal startup adds user/account-audit and analytics tables, preserving tenants, uploads, channels, keys and configuration. Stop and back up first; no separate migration command is required.
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

The distribution is **`gryphon-runtime`**, not `gryphon`. **Only after version
2.0.0 has actually been published to PyPI**, the no-checkout stdio command is
`uvx --from gryphon-runtime==2.0.0 gryphon stdio`, or install with
`python -m pip install gryphon-runtime==2.0.0`. An MCP entry can then use your
absolute `uvx` executable, args `["--from", "gryphon-runtime==2.0.0", "gryphon", "stdio"]`,
and the same `GRYPHON_SWAGGERS` environment (provide your own spec file or public
HTTPS spec URL). Hosted package installs use `gryphon-runtime[saas]==2.0.0`.
These are conditional instructions, **not a publication claim**.
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

`reusable_code_guide` explains execution on demand. Optional
`GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and
`get_server_skills` in local/operator mode. Guides are bounded, untrusted data,
not write approval. Discovery includes fingerprints/truncation; follow
`next_cursor` or narrow searches. MCP `readOnlyHint` is **not authorization**.

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

### Results, receipts and cancellation

Native structured results contain `success`, result `data`, available handles,
and stable `error_type` categories, including `sandbox_unavailable`. Raw prints,
stderr, upstream errors and traces are omitted. Large permitted JSON becomes
owner-scoped artifacts: follow `read_artifact`'s `next_offset` to `eof` in chunks
of at most **8192 bytes**, still subject to context and full-result limits.
`submit_code` returns an `id`; poll `get_run(run_id=...)`. States are `queued`,
`running`, `succeeded`, `failed`, `cancelled`, `interrupted`. Optional owner-scoped
`idempotency_key` deduplicates matching requests only while retained; conflicts fail.
Restart marks queued/running work **interrupted**, with **no automatic replay,
crash resume or exactly-once effects**. Inspect upstream state before retrying.
Cancellation revokes authority and awaits cleanup, but cannot undo accepted API
actions. Receipts/artifacts are not a workflow engine or permanent audit archive.

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

`is_read_only: true` filters writes at compile time and dispatch. Local/operator
writes require a write-enabled source, `GRYPHON_ALLOW_WRITES=true`, **and** exact
administrator permits such as `GRYPHON_ALLOWED_WRITE_OPERATIONS=["orders.create_order"]`.
No model flag, guide, idempotency key or tool hint authorizes writes; there is no
interactive write-approval UI. Hosted catalogs remain read-only regardless of base permits.

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

`gryphon --version` reports the version; `doctor` emits read-only safe JSON without compilation, store opens, API calls or Docker probing.
`compile --dry-run` validates without output writes but may fetch remote specs. **Stop before `clean --yes`**: it archives recognized compiled output/closed recipe caches to adjacent `.gryphon-archive-...` paths, retaining runs/artifacts/config.
`clean --yes --dry-run` only validates; `clean compile --yes` archives then compiles. Links, unknown files, overlapping paths and SQLite sidecars are refused.
Restore manually while stopped without overwriting newer data; this is not arbitrary deletion or hosted backup.

## Optional offline full Python

Docker is only for offline CPython using [`sandbox/requirements.txt`](sandbox/requirements.txt). Provision daemon, image (`docker build -t gryphon-sandbox:2.0.0 sandbox/`) and **gVisor `runsc`** yourself; select `GRYPHON_SANDBOX_MODE=docker`.
Missing requirements fail closed, never autostart/downgrade. Runs use UID 1000, dropped capabilities, no-new-privileges, read-only root, bounded tmpfs, 64-PID/256-MiB RAM-and-swap/half-core limits; no host mounts, network, credentials or `call_tool`.
AST checks still apply. Hosted Docker requires `GRYPHON_SAAS_DOCKER_ENABLED=true` and manual daemon access outside Compose.
`GRYPHON_SANDBOX_ALLOWED_IMPORTS` is a JSON list: `[]` denies imports; unset preserves offline defaults. Only approved preinstalled imports, never arbitrary installs.

## Development and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for full locked Ruff, strict mypy, pytest and
pre-commit commands, optional PostgreSQL/Docker checks and release instructions.
The **90% coverage floor** is mandatory; target **100%**. Normal tests need no live
upstream, Docker or PostgreSQL but import the `saas` extra. The offline real-MCP
demo is `uv run --frozen python examples/demo.py` (use isolated config/stores).
For offline measurements including receipts, run
`uv run --frozen python examples/benchmark.py --iterations 25 --concurrency 1`;
these are not model/API or end-to-end agent benchmarks.
See [AGENTS.md](AGENTS.md), [CHANGELOG.md](CHANGELOG.md), and [ROADMAP.md](ROADMAP.md).
Report [bugs](https://github.com/hypen-code/gryphon/issues) publicly and
[vulnerabilities](SECURITY.md) privately. MIT licensed; see [LICENSE](LICENSE).
