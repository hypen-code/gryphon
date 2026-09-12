# Gryphon

> **APIs were designed for developers. Gryphon makes them usable by AI agents.**

[![CI](https://github.com/hypen-code/gryphon/actions/workflows/ci.yml/badge.svg)](https://github.com/hypen-code/gryphon/actions)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Named for the legendary guardian, **Gryphon** sits between an agent's code and
its API authority. Compile OpenAPI → discover → inspect → execute → reuse:
**a small set of meta-tools, not one MCP tool per API endpoint**. Compose calls
and reduce data in bounded Python through a host-owned capability broker.

**Version 2.0.0** · Python **3.13+** · FastMCP **4.0.2** ·
`pydantic-monty` **0.0.18**. Real-client tests cover MCP **2026-07-28** and legacy
initialization. Native MCP Tasks are not implemented or advertised.

Choose **local stdio**, **operator-token HTTP**, or **admin-managed hosted tenants
and channels**. Hosted mode is single-worker, not a horizontally scalable or
certified SaaS platform. The distribution is `gryphon-runtime`; package/CLI:
`gryphon`. No PyPI or container-registry publication is claimed.

## Install and try it

Use Python 3.13+ on Linux/macOS; persistent-store locks require POSIX support.
Windows users can use containers. Restricted execution needs **no Docker or
model API key**. From the repository root:

```bash
git clone https://github.com/hypen-code/gryphon.git
cd gryphon
uv sync --frozen --extra dev --extra saas
cp .env.example .env
cp config/swaggers.yaml.example config/swaggers.yaml
uv run --frozen gryphon compile
uv run --frozen python examples/demo.py
```

The local [`examples/weather.yaml`](examples/weather.yaml) describes the public
Open-Meteo API. Compilation does not call its forecast endpoint. The demo uses
a **real in-process MCP client** to sum inputs offline and reuse the recipe;
compilation is optional for computation and adds weather discovery only.
Development's `saas` extra requires system **libpq** (Debian/Ubuntu: `libpq5`).

Alternatively, build with `uv build`, then install a local wheel:

```bash
python -m pip install /absolute/path/to/gryphon_runtime-2.0.0-py3-none-any.whl
python -m pip install '/absolute/path/to/gryphon_runtime-2.0.0-py3-none-any.whl[saas]'
```

The second command includes hosted dependencies (`psycopg==3.2.9`, using system
libpq). **Only after an actual public-index release** would
`python -m pip install gryphon-runtime` or `python -m pip install 'gryphon-runtime[saas]'`
be public-index installation instructions. They are not current release claims.

### One MCP entry: stdio

After installing, add this entry to your MCP client. Replace both paths with
absolute paths on your machine; clients may not expand `~` or find your shell's
`PATH`. `GRYPHON_SWAGGERS` is a JSON array encoded as an environment string:

```json
{
  "mcpServers": {
    "gryphon": {
      "command": "/absolute/path/to/venv/bin/gryphon",
      "args": ["stdio"],
      "env": {
        "GRYPHON_SWAGGERS": "[{\"name\":\"weather\",\"swagger_url\":\"/absolute/path/to/gryphon/examples/weather.yaml\",\"is_read_only\":true}]",
        "GRYPHON_STATE_DIR": "/absolute/path/to/private/gryphon-state"
      }
    }
  }
}
```

`gryphon stdio` compiles then serves, using **launch environment only** unless
`--env-file /absolute/path/to/private.env` is explicit. It never discovers an
ambient `.env` or writes into an installed package. `GRYPHON_STATE_DIR` is an
optional absolute root; default: `$XDG_STATE_HOME/gryphon` when XDG is absolute,
otherwise `~/.local/state/gryphon`. Default catalog/cache/receipt/artifact paths
are private, source-scoped subdirectories; explicit storage overrides are kept.
Use `GRYPHON_SWAGGERS="[]"` (or omit sources) for **empty-catalog offline compute**.
An explicitly configured `GRYPHON_SWAGGER_CONFIG_FILE` also works. Local paths
should be absolute to avoid depending on the client's launch directory.

Stdio trusts its launcher (`local` owner). Run **one process per run database**;
use separate state roots for simultaneous clients. Logs stay on stderr; stdout
is MCP only. The legacy `serve`/`run` commands remain supported. Standalone
`compile` prints non-secret MCP client JSON even for unchanged catalogs, with
absolute storage paths, a referenced env-file path, and recompilation disabled.
Dry runs, failed/empty compilations, and startup compilation emit no client JSON.
No client configuration is changed automatically or populated with credentials.

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

`reusable_code_guide` explains execution on demand.
`GRYPHON_ENABLE_ADDITIONAL_TOOLS=true` adds only `list_skills` and
`get_server_skills` in local/operator mode. Guides are bounded, untrusted data,
not initialization instructions or write approval. Discovery includes registry
fingerprints and truncation metadata: follow `next_cursor` or narrow searches.
MCP `readOnlyHint` is a client hint, **not authorization**.

### Execute, then reuse
Search for weather, then inspect:

```json
{"functions": [{"server_name": "weather", "function_name": "get_forecast"}]}
```

Use inspected names; unlike the demo, this `execute_code` calls a public API:

```json
{
  "code": "result = await call_tool(\"weather.get_forecast\", {\"latitude\": inputs[\"latitude\"], \"longitude\": inputs[\"longitude\"], \"current\": \"temperature_2m\"})",
  "description": "current weather by coordinates",
  "inputs": {"latitude": 51.5074, "longitude": -0.1278},
  "input_schema": {
    "type": "object",
    "properties": {
      "latitude": {"type": "number", "minimum": -90, "maximum": 90},
      "longitude": {"type": "number", "minimum": -180, "maximum": 180}
    },
    "required": ["latitude", "longitude"],
    "additionalProperties": false
  }
}
```

The only restricted external capability takes **two arguments**:
`await call_tool("server.function", arguments)`. Pass the returned `cache_id` to
`run_cached_code`:

```json
{"cache_id": "<returned-cache-id>", "params": {"latitude": 48.8566, "longitude": 2.3522}}
```

- `inputs`/`params` are JSON objects; replay replaces the **entire** input object.
  Values are never rewritten into source. A recipe caches code, not API results.
- Assign JSON-native `result`. `main()` is neither required nor auto-called.
- Restricted Python has no imports, filesystem, direct network, or host
  environment access. It is not a drop-in CPython environment.
- Bounded `input_schema` rejects references, regexes, and combinators including
  `$ref`, `pattern`, `allOf`, `anyOf`, and `oneOf`.
- Recipe identity binds owner, exact source, schema, and catalog/policy digest.
  Replay reruns validation/authorization; drift rejects stale recipes. Reordering
  or duplicating exact write permits does not change identity; changing the set does.

### Results, receipts, and cancellation

Native structured results contain `success`, result `data`, available handles,
and stable `error_type` categories. `sandbox_unavailable` survives sanitization
and receipt storage. Raw prints, stderr, upstream errors, and traces are omitted;
a print-byte summary is not a way to deliver results. Large permitted JSON uses
owner-scoped artifacts; follow `next_offset` to `eof` with `read_artifact`.
Chunks are at most **8192 bytes**, possibly smaller under the context budget;
artifacts never bypass the full-result limit.

`submit_code` accepts the execution contract, returns an `id`, and is polled
with `get_run(run_id=...)`. States: `queued`, `running`, `succeeded`, `failed`,
`cancelled`, `interrupted`. Optional `idempotency_key` deduplicates matching
owner-scoped requests only while retained; conflicting requests fail.
Restart marks queued/running work **interrupted**, with **no automatic replay,
crash resume, or exactly-once external effects**. Check upstream state before
retrying. Cancellation revokes authority and awaits cleanup, but cannot undo
accepted upstream actions. Bounded receipts/artifacts are not a workflow engine
or permanent audit archive.

## Configure APIs and policy

Local/operator catalog example:

```yaml
servers:
  - name: weather
    swagger_url: ./examples/weather.yaml
    is_read_only: true
```

`swagger_url` accepts local files or policy-approved HTTP(S) documents; relative
paths use the compilation directory. `base_url` overrides the spec URL.
OpenAPI **3.0/3.1** and Swagger **2.0** produce deterministic v2 manifests;
OpenAPI 3.2 is not supported. Unsupported request semantics fail closed;
unsupported response schemas are reported/omitted rather than falsely validated.
Supported response schemas are broker-validated. Optional query/header null
means omission; required/path null and null query-array elements are rejected.
Declared JSON-body null is supported. Generated Python is SDK documentation,
**never imported/executed by the host**. Legacy `top_level_functions` promotes no
MCP tools. `--llm-enhance` / `GRYPHON_LLM_ENHANCE=true` are explicitly rejected.

### Credentials and writes

For **local/operator mode**, public APIs need no auth block. Authenticated APIs
use trusted environment references, not secrets in code or committed YAML:

```yaml
auth:
  type: static
  value: "Bearer ${UPSTREAM_API_TOKEN}"
```

Host auth supports `static`, `jwt`, `basic`, OAuth2 client credentials (`oauth2`),
`keycloak`, and `session`, with broker-managed refresh. Host-only alternatives:
`GRYPHON_{SERVER}_AUTH` and JSON `GRYPHON_{SERVER}_EXTRA_HEADERS`. Neither sandbox
receives credentials. **Hosted channels currently support public APIs only**:
no host credential inheritance or tenant upstream secret manager is implemented.

`is_read_only: true` filters writes at compile time and dispatch. Local/operator
writes additionally require a write-enabled source and **both** administrator
settings, using exact inspected operation names (illustrative):

```dotenv
GRYPHON_ALLOW_WRITES=true
GRYPHON_ALLOWED_WRITE_OPERATIONS=["orders.create_order"]
```

No model flag, guide, idempotency key, or tool hint authorizes writes; there is
no interactive write-approval UI. **Hosted catalogs remain read-only**, even if
the base operator write settings are enabled.

### Runtime settings

See [`.env.example`](.env.example), [`GryphonConfig`](src/gryphon/config.py), and
[`SaaSConfig`](src/gryphon/saas_config.py). Explicit environment wins over dotenv.
Legacy `compile`/`serve`/`run` discover `.env` in the working directory with
checkout-root fallback; **`stdio` and `saas` require explicit `--env-file`**.

Key defaults: restricted execution; 30-second deadline (also Monty's hard
ceiling), 64,000,000-byte VM memory, four active runs **shared across hosted channels**,
five-second admission wait, 50 calls, 65,536-byte source/inline and 2,097,152-byte full-result budgets,
16,384-byte discovery context, ten items per discovery page. Recipe retention:
3600 seconds/500 entries; receipts: 86400 seconds/1000 entries; artifacts: 100
per owner. Configure these through the documented `GRYPHON_*` settings.

Public API/auth/spec destinations **require HTTPS**. `GRYPHON_ALLOWED_DOMAINS`
is an additional exact-host JSON array, not wildcard/CSV authority. Private or
loopback HTTP needs explicit `GRYPHON_ALLOW_PRIVATE_NETWORKS=true` and approved
DNS answers. Metadata, link-local, reserved, and mixed public/private destinations
remain denied. DNS pinning, original Host/SNI, origin-specific pools, verified
TLS, no environment proxies/redirects, and response bounds remain mandatory.

## Operate local/operator mode

`gryphon --version` reports the version; `doctor` emits read-only JSON without
compiling, opening runtime stores, calling APIs, or probing/starting Docker.
`compile --dry-run` validates without output writes (remote specs may be fetched).
`serve` respects `GRYPHON_COMPILE_ON_STARTUP`; `run` compiles once then serves.
`--env-file` works before or after the subcommand.

**Stop before `clean --yes`.** It archives recognized compiled output and a
closed recipe cache to adjacent `.gryphon-archive-...` paths; it retains runs,
artifacts, and config. `clean --yes --dry-run` only validates; `clean compile
--yes` archives then compiles. Links, unknown files, unsafe/overlapping paths,
and SQLite sidecars are refused. Restore manually while stopped without
overwriting newer data. This is not arbitrary deletion or a hosted backup tool.

For operator-token HTTP:

```bash
export GRYPHON_HTTP_AUTH_TOKEN="$(uv run --frozen python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uv run --frozen gryphon serve --transport http
```

Connect to `http://127.0.0.1:8000/mcp` with `Authorization: Bearer <your-token>`.
Startup rejects missing/short tokens (minimum 32 characters, `SecretStr`). The
verified owner is fixed `operator`, not tenant identity; sharing tokens shares
ownership. Public exposure requires TLS termination and operator controls.

For the existing container service, prepare the quick-start config and token,
then `docker compose up --build -d gryphon`. Compose **2.24+** is required.
The UID-1000 image includes the locked `saas` extra and system `libpq5`; legacy
HTTP remains the default command. The service keeps read-only configuration,
private named data/catalog volumes, resource limits, loopback port 8000, and
**no Docker socket**. `.env` is optional; the HTTP token is checked at application
startup. Its TCP health check is not authenticated MCP readiness or `/health`.

## Hosted tenants and channels

Install the `saas` extra and libpq, or use the same container image above. Supply
`GRYPHON_SAAS_ADMIN_TOKEN` (random, at least 32 characters),
`GRYPHON_SAAS_DATABASE_URL` (`postgresql://...`), and
`GRYPHON_SAAS_PUBLIC_ORIGIN` (canonical HTTPS origin, no path/credentials/query).
Set `GRYPHON_SAAS_STATE_DIR` to private persistent storage; native defaults are
`./data/saas`, host `127.0.0.1`, port `8000`. Start with `gryphon saas`, or
`gryphon saas --env-file /absolute/path/to/private-hosted.env`. No ambient dotenv
is loaded. Protect the admin token and database URL; example values are blank.

### Compose hosted profile

Provision a TLS reverse proxy yourself, forwarding to `127.0.0.1:8001` and
preserving the canonical **Host** header. The app does not trust proxy headers.
Set a real origin before running these commands; secrets remain in your shell:

```bash
export GRYPHON_SAAS_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export GRYPHON_POSTGRES_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export GRYPHON_SAAS_PUBLIC_ORIGIN=https://gryphon.example.com
docker compose --env-file /dev/null --profile hosted up --build -d gryphon-hosted
```

The explicit service target avoids starting legacy HTTP. Hosted dependencies
include isolated **PostgreSQL 17.6**, no published DB port, and separate named
volumes for PostgreSQL and local channel state. The app is non-root, read-only,
resource-limited, restricted by default, and has **no Docker socket**. Password,
admin token, and origin are required when selected services start, not during
inactive-profile interpolation; missing hosted settings do not break legacy mode.
Use a URL-safe database password (the command generates hex); retain it across
restarts. Changing the environment does not rotate an initialized DB password.
For native startup, supply the database URL separately; Compose derives its URL.

Only for loopback development, set `GRYPHON_SAAS_PUBLIC_ORIGIN=http://127.0.0.1:8001`
and `GRYPHON_SAAS_ALLOW_INSECURE_HTTP=true` (native default port: 8000). This
explicit exception disables Secure cookies; never use it for public traffic.
`/health` checks database readiness. Canonical Host validation applies there too;
the hosted container health check sends that Host, not a channel/admin token.

### Admin workflow and limits

1. Open `/` on the canonical origin and exchange the admin token for a session.
2. Create a tenant; upload immutable Swagger/OpenAPI **JSON or YAML** versions.
3. Create a channel and bind only that tenant's uploaded versions (or none for
   offline computation). Choose restricted mode, or operator-enabled Docker and
   a narrowed subset of approved preinstalled imports.
4. Generate/rotate its key and copy it **once**. Only the hash is stored; lost
   keys must be rotated. Connect an MCP streamable-HTTP client to
   `https://your-origin/mcp/{channelUUID}` with `Authorization: Bearer <channel-key>`.
5. Use the UI to revise/disable channels, revoke keys, disable tenants, and view
   aggregate usage/audit events. Channel keys cannot administer `/api`.

Admin `/api` sessions use **Secure, HttpOnly, SameSite=Strict** cookies, bounded
lifetimes, and session-bound CSRF checks on mutations. Restart invalidates admin
sessions. Host/origin validation and bounded requests complement authentication.
Uploads cannot reference external documents or interpolate host environment.
Channel auth and ownership are verified against the control database, not IDs
asserted by a client; channels have independent runtime stores and authority.

**Exactly one active hosted worker per database**, enforced by a lease. Do not
scale replicas: PostgreSQL stores control metadata (including immutable specs),
key hashes, aggregate usage, and audit events—not recipe code, receipts, or
artifacts. Those remain in a private **local persistent volume per channel**.
Back up/restore the database **and** state volume together while the worker is
stopped; protect disks/backups and test restoration. TLS, backup scheduling,
key rotation, monitoring, and incident response are operator responsibilities.
There is no HA/horizontal SaaS, billing, SSO, user invitation flow, tenant secret
manager, encryption-at-rest guarantee, or security certification. See [SECURITY.md](SECURITY.md).

## Optional offline full Python

Docker is only for offline CPython using [`sandbox/requirements.txt`](sandbox/requirements.txt).
Provision the daemon, image, and **gVisor `runsc`** manually:

```bash
docker build -t gryphon-sandbox:2.0.0 sandbox/
GRYPHON_SANDBOX_MODE=docker uv run --frozen gryphon serve
```

Missing daemon/image/runtime fails closed, never autostarts or downgrades.
Each run uses UID 1000, dropped capabilities, no-new-privileges, read-only root,
bounded tmpfs, 64-PID/256-MiB RAM-and-swap/half-core limits, and no host mounts,
network, credentials, or `call_tool`. AST checks still apply. Hosted Docker also
requires `GRYPHON_SAAS_DOCKER_ENABLED=true` and manual daemon access provisioning
outside Compose. `GRYPHON_SANDBOX_ALLOWED_IMPORTS` is a JSON list: `[]` denies
imports; unset preserves offline defaults. Only approved preinstalled imports can be selected.

## Development and support

```bash
uv sync --frozen --extra dev --extra saas
uv run --frozen --extra saas ruff check src/ tests/
uv run --frozen --extra saas ruff format --check src/ tests/
uv run --frozen --extra saas mypy --strict src/ tests/
uv run --frozen --extra saas pytest --cov-fail-under=90
uv run --frozen --extra saas pre-commit install
uv run --frozen --extra saas pre-commit run --all-files
```

The **90% coverage floor** is mandatory; target **100%**. Normal tests need no
live upstream, Docker, or PostgreSQL, but import hosted dependencies. Optional
`GRYPHON_TEST_POSTGRES=1` / Docker checks are in [CONTRIBUTING.md](CONTRIBUTING.md).
For offline startup/median/p95/throughput measurements including receipts, run
`uv run --frozen python examples/benchmark.py --iterations 25 --concurrency 1`;
use `--iterations 100 --concurrency 4` for bounded concurrency. These are not
model/API or end-to-end agent benchmarks.

See [AGENTS.md](AGENTS.md), [CHANGELOG.md](CHANGELOG.md), and [ROADMAP.md](ROADMAP.md).
Report [bugs](https://github.com/hypen-code/gryphon/issues) publicly and
[vulnerabilities](SECURITY.md) privately. MIT licensed; see [LICENSE](LICENSE).
