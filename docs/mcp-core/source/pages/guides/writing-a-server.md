# Write a server

## The server type

```python
from cavi_mcp_core.contracts import Result
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server("status-example", package="cavi-mcp-core", instructions="Read readiness.")


class Status(Result):
    ready: bool
    note: str | None = None


@mcp.tool(annotations=READ_ONLY)
async def status() -> Status:
    """Report readiness."""
    return Status(ready=True)


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

`Server` extends `mcp.server.mcpserver.MCPServer`:

- `package` sets the reported server version from that distribution's metadata.
- Tool schemas are compacted (`compact=False` keeps the generated schemas unshortened); see
  [Schemas](../reference/schemas.md).
- Undeclared arguments are rejected before tool execution, including for tools with no parameters.
- A tool's docstring becomes its description, without the source indent.
- `ValueError`, `RuntimeError` and `OSError` raised by a tool reach the caller with their text; any other
  exception is logged on the server and reported without details. `anticipated=(...)` changes that list,
  and `anticipated=()` leaves tool errors to the SDK.
- Every tool error, including an unknown tool name, returns one structured result. Which exception to
  raise, and what the caller is told, is in [Results and errors](../reference/results-and-errors.md).

Called directly, `Server.call_tool` raises instead of returning a failure, and a decorated tool function
stays the original coroutine, so tests see the original exception types.

## Annotations

`READ_ONLY`, `WRITES` and `DESTRUCTIVE` are the tool annotations, chosen by what a tool does to the world:

| Annotation | `readOnlyHint` | `destructiveHint` | `idempotentHint` |
|---|---|---|---|
| `READ_ONLY` | true | false | true |
| `WRITES` | false | false | unset |
| `DESTRUCTIVE` | false | true | unset |

`openWorldHint` is always false: no tool reaches beyond the local machine and the application it drives.

## Action-style arguments

`need(value, name, action)` returns `value`, or raises a `ValueError` naming the argument `action`
requires. `reject_foreign(action, given, allowed)` refuses the arguments in `given` that `action` does
not accept; a value of None counts as not passed, and the error lists the names in sorted order:
`Not valid for action=info: directory, graph`.

## A recovery tool

Override `Server.failure(name, error)` to point unknown outcomes at the tool that inspects your
application:

```python
from cavi_mcp_core.contracts import failure


class Recovering(Server):
    def failure(self, name, error):
        return failure(name, error, unknown_next_action="inspect_transaction")
```
