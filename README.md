# Cavi MCP Core

![A beaver supports a workbench while a hedgehog builds a clockwork dragonfly](https://raw.githubusercontent.com/cavi-ai/mcp-core/main/assets/mcp-core-hero.png)

## Build the interesting part.

Build your application's tools on a shared foundation. Cavi MCP Core extends the Python MCP server with typed result and error contracts, workspace and file utilities, and bounded process handling, so you can spend more time on what your tools do.

[![PyPI version](https://img.shields.io/pypi/v/cavi-mcp-core)](https://pypi.org/project/cavi-mcp-core/) [![PyPI weekly downloads](https://img.shields.io/pypi/dw/cavi-mcp-core?label=PyPI%20downloads%2Fweek)](https://pypi.org/project/cavi-mcp-core/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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

## Shared foundations

- Give tool callers consistent typed results and structured failures.
- Work with workspace paths, bounded files and private operation records.
- Run and supervise local processes with bounded output and deadlines.

Blender MCP, Substance MCP, Houdini MCP, Adobe MCP, Premiere MCP and After Effects MCP depend on Core.

<details>
<summary>Module reference</summary>

| Module | What it provides |
|---|---|
| `server` | `Server`, the `READ_ONLY`, `WRITES` and `DESTRUCTIVE` annotations, `need` and `reject_foreign`. |
| `contracts` | `Result`, `VariantResult`, `OperationRejected`, `OperationUnknown` and `failure`. |
| `schema` | Compact tool schemas and variant-shape validation. |
| `schema_refs` | Local `$ref` handling behind `schema`. |
| `workspace` | The workspace root and the input and output path rules. |
| `files` | Workspace-confined, bounded file reads and JSON parsing. |
| `journal` | Atomic private JSON records and completion receipts. |
| `media` | Animation frame receipts, the bounded command that fills one, and PNG image results. Needs `cavi-mcp-core[media]`. |
| `lock` | A cross-process file lock, re-entrant in the holding thread. |
| `apps` | Installed application discovery, command-line programs, versions and running processes. |
| `proc` | Bounded subprocesses that stop the whole process group. |
| `owned_exec` | Launch registration with a process owner. |
| `supervision` | One supervised command with checked process groups and limits. |
| `resident` | A supervised command that outlives its caller. |
| `retirement` | Helper cleanup before a worker publishes. |
| `processes` | Checked process identities and group cleanup. |
| `sampling` | Read-only macOS process census, identities and RSS. |

</details>

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks and the rules for changes. This project follows
the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
