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
