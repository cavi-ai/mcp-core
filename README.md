# Cavi MCP Core

[![PyPI version](https://img.shields.io/pypi/v/cavi-mcp-core)](https://pypi.org/project/cavi-mcp-core/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

The Python library the Cavi MCP servers are built on: one server type with compact schemas and one
structured failure for every tool error, strict result models, workspace path rules, bounded files and
processes, and an operation journal. [Blender MCP](https://github.com/cavi-ai/blender-mcp),
[Substance MCP](https://github.com/cavi-ai/substance-mcp),
[Houdini MCP](https://github.com/cavi-ai/houdini-mcp), [Adobe MCP](https://github.com/cavi-ai/adobe-mcp),
[Premiere MCP](https://github.com/cavi-ai/premiere-mcp) and
[After Effects MCP](https://github.com/cavi-ai/after-effects-mcp) depend on it.

## Install

Requires Python 3.15 or later with [uv](https://docs.astral.sh/uv/).

```sh
uv add cavi-mcp-core
```

## Quickstart

```python
from cavi_mcp_core.contracts import Result
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server("status-example", package="cavi-mcp-core", instructions="Read readiness.")


class Status(Result):
    ready: bool


@mcp.tool(annotations=READ_ONLY)
async def status() -> Status:
    """Report readiness."""
    return Status(ready=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

From a checkout, `uv run --locked python examples/inspect_status.py` starts the example server over
stdio, calls `status` and prints `{"ready": true}`. The
[quickstart](docs/mcp-core/source/pages/introduction/quickstart.md) walks through it.

## What it provides

| Module | What it provides |
|---|---|
| `server` | `Server`, the `READ_ONLY`, `WRITES` and `DESTRUCTIVE` annotations, `need` and `reject_foreign`. |
| `contracts` | `Result`, `VariantResult`, `OperationRejected`, `OperationUnknown` and `failure`. |
| `schema` | Compact tool schemas and variant-shape validation. |
| `schema_refs` | Local `$ref` handling behind `schema`. |
| `workspace` | The workspace root and the input and output path rules. |
| `files` | Workspace-confined, bounded file reads and JSON parsing. |
| `journal` | Atomic private JSON records and completion receipts. |
| `apps` | Installed application discovery, versions and running processes. |
| `proc` | Bounded subprocesses that stop the whole process group. |
| `owned_exec` | Launch registration with a process owner. |
| `supervision` | One supervised command with checked process groups and limits. |
| `resident` | A supervised command that outlives its caller. |
| `retirement` | Helper cleanup before a worker publishes. |
| `processes` | Checked process identities and group cleanup. |
| `sampling` | Read-only macOS process census, identities and RSS. |

## Results and errors

Every tool error returns one structured result:

```json
{"error": {"version": 1, "code": "INVALID_ARGUMENT", "operation": "status",
           "outcome": "rejected", "retry_safe": false, "next_action": "inspect_inputs"}}
```

`outcome` is `rejected` only when nothing happened; anything else is `unknown`, and `retry_safe` is
always false. [Results and errors](docs/mcp-core/source/pages/reference/results-and-errors.md) lists the
codes and which exception a tool raises before and after it acts. Servers link to that page instead of
restating it.

## Documentation

The documentation source is in `docs/mcp-core/source`:

- [Overview](docs/mcp-core/source/pages/introduction/overview.md),
  [installation](docs/mcp-core/source/pages/introduction/installation.md) and
  [quickstart](docs/mcp-core/source/pages/introduction/quickstart.md)
- Guides: [write a server](docs/mcp-core/source/pages/guides/writing-a-server.md),
  [workspaces](docs/mcp-core/source/pages/guides/workspaces.md),
  [files and journals](docs/mcp-core/source/pages/guides/files-and-journals.md),
  [applications](docs/mcp-core/source/pages/guides/applications.md),
  [processes](docs/mcp-core/source/pages/guides/processes.md),
  [process supervision](docs/mcp-core/source/pages/guides/supervision.md) and
  [process inspection](docs/mcp-core/source/pages/guides/process-inspection.md)
- Reference: [results and errors](docs/mcp-core/source/pages/reference/results-and-errors.md),
  [schemas](docs/mcp-core/source/pages/reference/schemas.md),
  [configuration](docs/mcp-core/source/pages/reference/configuration.md) and
  [testing](docs/mcp-core/source/pages/reference/testing.md)
- [Security model](docs/mcp-core/source/pages/security/model.md) and
  [version and support](docs/mcp-core/source/pages/release/version-and-support.md)

## Configuration

| Variable | Meaning |
|---|---|
| `CAVI_MCP_PROCESS_OWNER` | A private owner configuration file; when set, `proc.run` registers every command with that owner before it runs. |

Every other variable, such as a workspace root or an application override, is named by the server that
uses the library.

## Security

The library opens no network port, sends no telemetry and uses no accounts. Tool arguments are closed,
paths are confined to the workspace, and an unexpected exception keeps its details on the server. See
the [security model](docs/mcp-core/source/pages/security/model.md) and [SECURITY.md](SECURITY.md).

## Tests

```sh
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests
npm install --no-audit --no-fund --package-lock=false
npm run eval:protocol
```

The tests also fail when this README's module table and the package differ, and when the error codes in
the results and errors reference and the codes a client receives differ. `eval:protocol` runs a
representative stdio server with MCP Eval.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks and the rules for changes. This project follows
the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
