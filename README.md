# Cavi MCP Core

Shared plumbing for the CAVI MCP servers ([blender-mcp](https://github.com/cavi-ai/blender-mcp),
[substance-mcp](https://github.com/cavi-ai/substance-mcp),
[houdini-mcp](https://github.com/cavi-ai/houdini-mcp),
[adobe-mcp](https://github.com/cavi-ai/adobe-mcp)): one server type, one failure contract,
one schema compaction, one bounded process runner, bounded file reads and an operation
journal.

## Install

```sh
uv add cavi-mcp-core
```

Requires Python 3.11+ and the MCP Python SDK 2.

## Server

```python
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server("my-mcp", package="my-mcp", instructions="Inspect before editing.")


@mcp.tool(annotations=READ_ONLY)
async def status() -> dict:
    return {"ready": True}
```

`Server` extends `mcp.server.mcpserver.MCPServer`:

- `package` sets the reported server version from that distribution's metadata.
- Tool schemas are compacted (`compact=False` keeps the SDK's schemas).
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

## License

MIT
