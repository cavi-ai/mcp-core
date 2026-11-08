# Cavi MCP Core

Build Python MCP servers with typed results, compact schemas and structured failures.
Cavi MCP Core also provides bounded process and file operations and an operation journal.
It is a library for server authors, used by [Blender MCP](https://github.com/cavi-ai/blender-mcp),
[Substance MCP](https://github.com/cavi-ai/substance-mcp) and
[Adobe MCP](https://github.com/cavi-ai/adobe-mcp).

## Install

```sh
uv add cavi-mcp-core
```

The current source checkout requires Python 3.15+ and the MCP Python SDK 2.
Release [v0.1.1](https://github.com/cavi-ai/mcp-core/releases/tag/v0.1.1) requires
Python 3.11+; the installed release's package metadata determines its requirements.

## Run your first tool

From a source checkout, run the included read-only example:

```sh
git clone https://github.com/cavi-ai/mcp-core.git
cd mcp-core
uv sync --locked --dev
uv run --locked python examples/inspect_status.py
```

Expected standard output:

```json
{"ready": true}
```

The client starts [status_server.py](examples/status_server.py) as a subprocess,
lists its tools and calls `status` over stdio. Exiting the client closes that server.
No application, model, account or external service is needed. The example checks the
server connection; it does not establish any consuming application's behavior.

## Server

```python
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server("status-example", package="cavi-mcp-core", instructions="Read readiness.")


@mcp.tool(annotations=READ_ONLY)
async def status() -> dict:
    return {"ready": True}


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

`Server` extends `mcp.server.mcpserver.MCPServer`:

- `package` sets the reported server version from that distribution's metadata.
- Tool schemas are compacted (`compact=False` keeps the generated schemas unshortened).
- Undeclared arguments are rejected before tool execution, including for tools
  with no parameters.
- `ValueError`, `RuntimeError` and `OSError` raised by a tool reach the caller with their
  text; any other exception is logged on the server and reported without details. Pass
  `anticipated=()` to leave tool errors to the SDK.
- Every tool error, including an unknown tool name, returns one structured result:

```json
{"error": {"version": 1, "code": "INVALID_ARGUMENT", "operation": "status",
           "outcome": "rejected", "retry_safe": false, "next_action": "inspect_inputs"}}
```

`outcome` is `rejected` only for invalid arguments, missing inputs and unknown tools;
anything else is `unknown`, and `retry_safe` is always false. Raise
`contracts.OperationRejected(message, code)` or `contracts.OperationUnknown(message, code)`
to report a specific code. Override `Server.failure` to name a different recovery tool
for unknown outcomes.

`need(value, name, action)` and `reject_foreign(action, given, allowed)` validate
action-style arguments. `READ_ONLY`, `WRITES` and `DESTRUCTIVE` are the tool annotations.

## Contracts

`contracts.Result` is a strict pydantic model whose unset optional fields stay off the
wire. `contracts.VariantResult` accepts one of several declared field sets (`shapes`)
without a discriminator field.

## Schemas

`schema.compact_schema(schema, output=False)` returns a copy without titles or
`additionalProperties: true`, with equivalent union forms shortened, and validates the same
values; `output=True` also drops defaults, which are documentation only in a response.
`schema.validate_variant` and `schema.variant_schema` back `VariantResult`.

## Processes

`proc.run(args, timeout)` runs a command with bounded output and one deadline. On a
timeout or a cancellation it stops the whole process group (process tree on Windows) and
returns once the process exits. `proc.run_sync` is the blocking form and
`proc.diagnostics(result)` formats what a failed run printed.

### Owned execution

On POSIX, `proc.run` and `proc.run_sync` register commands when
`CAVI_MCP_PROCESS_OWNER` names a private owner configuration file. Other
subprocess APIs can use `owned_exec.command(args, cwd=..., env=...)` with the
same working directory and environment. Embedded hosts supply a trusted Python
interpreter through `python=...`. A configured owner cannot be dropped or
replaced through a child environment.

The configuration contains `version: 1`, an `attempt` identifier, absolute
`socket` and `records` paths, and a per-attempt `token`. Files and the Unix socket
must belong to the current user; configuration and records allow no group or
other access. Paths must be canonical and free of symbolic links.

Before executing the command, the gate sends one newline-delimited JSON frame:
`{"token": "...", "request": {...}}`. The version 1 request contains its attempt,
launch ID, PID, parent PID, isolated process group, command, working directory and
environment digest. The owner validates the fixed workload and process identity,
then syncs a private record containing the exact `request` and its OS-derived
`birth_identity`, including the record's directory entry.

The acknowledgment requires `launch_permitted: true`, the canonical request's
`request_sha256`, a relative JSON `record` filename and its `record_sha256`.
Canonical request bytes use sorted compact JSON followed by a newline. Record
hashes cover the exact file bytes. The gate checks the record and unchanged owner
configuration before exec; the PID and group remain unchanged. Missing, changed
or refused registration exits with code 78. One absolute five-second deadline
and a 64 KiB response limit bound the handshake.

The consuming supervisor owns admission, idempotent launch IDs, process lifetime
and cleanup. This gate supports audited launch sites; it does not contain
arbitrary daemonizing programs. Keep owner configuration, capabilities and
registration records private.

### Process supervision

On Linux and macOS, `supervision.run` owns the registration endpoint and checked
process groups for one command. Call it from the resident process that owns the
resource reservation:

```python
from cavi_mcp_core import supervision

receipt = supervision.run(
    command, cwd=workspace, env=environment, directory=new_record_directory,
    max_seconds=1800, max_rss_kib=48 * 1024 * 1024,
    minimum_available_kib=2 * 1024 * 1024,
)
```

The caller selects all three limits; zero `minimum_available_kib` disables the
headroom policy. The API checks registered PID/group/birth identities and socket
peers, persists records before acknowledgment, bounds registration reads, and
retires checked groups after exit, cancellation or a limit. New owner directories
must be canonical and unused; their new parent links are synced. A configured
owner cannot be replaced. Python's character locale and macOS startup text-encoding
fields are normalized without importing unrelated ambient environment values.

The receipt includes `exit_code`, `error`, `cleanup_error`, `registered_groups`,
`peak_rss_kib` and elapsed `seconds`. Cleanup uncertainty requires retaining the
resource reservation and records. Admission, workload inputs and final
application reports stay with the consuming adapter;
[agent-sessions](https://github.com/cavi-ai/agent-sessions) supplies queue state.

For a neutral Python command with retained records:

```sh
uv run --locked python examples/supervised_process.py --out /absolute/new-output
```

For a lifetime independent of the submitting caller, use `resident.start`:

```python
from cavi_mcp_core import resident

handle = resident.start(
    command, cwd=workspace, env=environment, directory=new_record_directory,
    max_seconds=1800, max_rss_kib=48 * 1024 * 1024,
    minimum_available_kib=2 * 1024 * 1024,
)
result = resident.wait(handle["directory"], timeout=30)
```

The separate POSIX session uses `supervision.run` and retains the original
execution deadline after caller loss. It cannot execute a worker until the
submitting process syncs its handle and acknowledges that exact handle. State
and supplied environment are private, bounded records; unrelated ambient values
are not inherited. An existing directory refuses another launch.

Keep the token-free handle or directory to reattach with `resident.inspect`.
`resident.wait` has its own observation timeout; timeout does not cancel the
operation. `resident.cancel` writes one attempt-bound cancellation request and
the resident retires checked processes. Both calls preserve earlier evidence.

A durable result binds the resident PID/birth identity, attempt and sealed input
hash. `state: finished`
means the owner recorded a receipt; check `exit_code`, `error` and
`cleanup_error` before declaring application success or releasing admission.
Cleanup uncertainty, or owner exit without a result, yields `state: unresolved`.
A reused PID is never adopted. The consuming adapter keeps its broker
reservation until cleanup is verified. These APIs cover registered launch sites;
engine-created helpers do not automatically register.

```sh
uv run --locked python examples/resident_process.py --out /absolute/new-output
```

`processes.inspect`, `processes.group` and `processes.cleanup` expose the same
checked identity/group operations for consuming adapters. `journal.create_json`
creates an exclusive private record, syncs its file and directory, and returns
its exact-byte SHA-256. Existing records are preserved. `journal.create_directory`
durably links new private directories and any missing parents.

### Process inspection

On macOS, `sampling` reads process census, checked birth identities, descendant RSS
and socket-descriptor counts. Each call accepts `timeout` in seconds, capped at two
seconds. Census, RSS, cleanup identities and descriptor reads use independent
read-only workers; an interrupted or timed-out worker refuses subsequent calls.

```python
from cavi_mcp_core import sampling

rows = sampling.inventory(timeout=1)  # PID -> (parent PID, kernel name)
family = sampling.tree(pid, timeout=1)  # PID -> (RSS KiB, opaque birth identity)
identities = sampling.identities(family, timeout=1)
sockets = sampling.socket_count(pid, timeout=1)
```

Pass `children` to `tree` to reuse census discovery; each descendant still needs
its current parent and birth identity checked. Other processes' RSS is not read.
Invalid or denied native reads raise; exited processes are absent. `sampling.birth`
returns the same `darwin:` token as `processes.birth`, or `None` after exit.
Unsupported platforms refuse before starting a native worker.

Inspection does not launch, signal or adopt processes. A sampled tree cannot prove
that an unregistered detached helper exited. Keep admission, ownership, aggregate
memory policy and restoration in the consuming supervisor. See
[`examples/inspect_process.py`](examples/inspect_process.py) for a current-process
example.

## Applications

`apps.find_directory`, `apps.find_app` and `apps.find_executable` locate an installed
application in one order: an environment override, which is final, then the default
location the caller passes, then `PATH`. `apps.bundle(name, identifier, pattern, env)`
finds a macOS application by its bundle identifier: the override in `env` when set, else
the newest matching bundle under `/Applications`; it raises `AppUnavailable` or
`AppMismatch` with a stable `error_code`. `apps.version` reads a bundle's version without
running it, `apps.running` asks whether an executable has a process, and
`apps.processes` lists the processes of one executable on macOS.

## Files and journals

`files.inside(workspace, path)` resolves a path inside an existing workspace and refuses
`..`, escapes through symbolic links and the workspace root. `files.read_bytes`,
`files.load_json` and `files.digest` read bounded regular files and refuse a file that
changes while it is read; `files.parse_json` refuses NaN and Infinity.

`journal.write_json` replaces a JSON file atomically, durably and readable only by its
owner. `journal.completion` reads the completion receipt of one operation,
`journal.archive` records how an interrupted operation was resolved, and
`journal.fingerprint` hashes canonical JSON.

## Development

```sh
uv sync --locked --dev
uv run --locked python -m unittest discover -s tests
uv run --locked ruff check .
```

The CI protocol gate runs MCP Eval against a representative stdio server using
Core's result and error contracts:

```sh
npm install --no-audit --no-fund --package-lock=false
npm run eval:protocol
```

CI builds the evaluator from `cavi-ai/mcp-eval` on GitHub `main` and records its
resolved revision with the JSON reports under `.tmp/evals/`. The gate requires
all manifest cases and a readiness score of 100, excluding runner latency.
The fixture checks the shared server layer; application behavior is evaluated
in each consuming server. Locally, `MCPEVAL_EXECUTABLE` selects a source-built
binary; the default npm wrapper installs its checksum-verified release.

## Help and contributing

[Report a reproducible issue](https://github.com/cavi-ai/mcp-core/issues) with the
Python and package versions, command, expected result and actual result. Remove private
paths and tool values before sharing. For a pull request, run the development checks
above and include a focused test for changed behavior.

Report vulnerabilities privately through the [security policy](https://github.com/cavi-ai/.github/blob/main/SECURITY.md).

## License

[MIT](LICENSE).
