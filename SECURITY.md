# Gryphon Security Policy

## Supported code line and reporting

Security work targets the **Gryphon 2.0.x** code line. Older versions do not
receive backports. Version 2.0.0 is installed from the repository checkout; this
policy does not imply a published PyPI distribution or an external security audit.

**Do not disclose vulnerabilities in public GitHub issues.** Submit a private
[security advisory](https://github.com/hypen-code/mcp-code-execution/security/advisories/new).
Include the affected commit/version, execution profile, transport, minimal
reproduction with synthetic data, impact, and any mitigation. Do not include
real tokens, private API responses, or another user's data. Coordinate disclosure
with maintainers; this document makes no guaranteed response-time commitment.

## Threat model

Gryphon is an API-agent backend for a **trusted local operator**. Agent code,
OpenAPI descriptions, skills guides, tool inputs, and upstream data are
untrusted. The host process, its configuration, catalog storage, and credential
vault are inside the trusted computing boundary. Protect the host accordingly.

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

### 7. Optional Docker is offline computation only

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
host execution. The default Compose service instead runs the restricted profile,
uses named storage volumes and a non-root host process, and mounts no Docker
socket. Its TCP health check is not authenticated protocol readiness.

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

- Run one process per run database under a dedicated, least-privileged OS user.
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
claim is made of multi-tenant cloud readiness, formal isolation verification,
exactly-once effects, or protection against all side channels.
