# Results and errors

Every server built on MCP Core returns the same result and error shapes. A server's own documentation
links here for the contract and lists only its own codes and recovery tool.

## Results

`contracts.Result` is a strict pydantic model for a tool's structured content. Optional fields the tool
did not set stay off the wire; a field set to None is sent as null. Aliased fields follow the same
rule, and extra fields are kept.

```python
from cavi_mcp_core.contracts import Result


class Status(Result):
    ready: bool
    note: str | None = None


Status(ready=True).model_dump()             # {"ready": True}
Status(ready=True, note=None).model_dump()  # {"ready": True, "note": None}
```

`contracts.VariantResult` accepts one of several declared field sets (`shapes`) without a discriminator
field. `nullable_shape_fields` names the fields that may be null inside a shape. Its JSON schema lists
the shapes as an `anyOf` of required fields.

## Errors

A failed call is a tool error: `isError` is true, the content is the cause as readable text, and the
structured content has this shape:

```json
{"error": {"version": 1, "code": "INVALID_ARGUMENT", "operation": "status",
           "outcome": "rejected", "retry_safe": false, "next_action": "inspect_inputs"}}
```

| Field | Meaning |
|---|---|
| `version` | The envelope version, 1. |
| `code` | Why the call failed. |
| `operation` | The tool that was called. |
| `outcome` | `rejected`: nothing happened. `unknown`: the effect may or may not have happened. |
| `retry_safe` | Always false: the server never claims that repeating a call is harmless. |
| `next_action` | What the caller does before calling again. |

### Codes

| `code` | `outcome` | `next_action` | When |
|---|---|---|---|
| `TOOL_NOT_FOUND` | `rejected` | `list_tools` | The tool name is not registered. |
| `INVALID_ARGUMENT` | `rejected` | `inspect_inputs` | The arguments fail the input schema or name an undeclared argument, or the tool raised `ValueError`. |
| `INPUT_NOT_FOUND` | `rejected` | `inspect_inputs` | The tool raised `FileNotFoundError`. |
| `OUTCOME_UNKNOWN` | `unknown` | `inspect_application_and_outputs` | Anything else: `RuntimeError`, any other `OSError`, a `Result` that fails its own validation, or a defect. |

`contracts.OperationRejected(message, code)` reports its own code as `rejected` with `inspect_inputs`;
`contracts.OperationUnknown(message, code)` reports its own code as `unknown`. `apps.AppUnavailable`
(`APP_UNAVAILABLE`) and `apps.AppMismatch` (`APP_IDENTITY_MISMATCH`) are rejections.

## Which exception to raise

The outcome comes from the exception type, so a tool raises by where it is in its work:

- Before the tool or its application changes anything, raise `ValueError`, `OperationRejected` or
  `FileNotFoundError`. The caller fixes its inputs and calls again.
- Once anything may have changed, raise `OperationUnknown`, or let a `RuntimeError` or `OSError` through.
  The caller inspects the application and the outputs before calling again. A report that cannot be read
  after a render is `OperationUnknown`, not `FileNotFoundError`.
- Any other exception is a defect. The server logs it with its traceback, and the caller gets
  `Error executing tool <name>` and `unknown`. A `Result` that fails its own validation counts as a
  defect, because the tool may already have acted. A pydantic error from an input model the tool
  validates itself stays `INVALID_ARGUMENT` with its text.

The text of an anticipated exception reaches the caller after `Error executing tool <name>: `, so it
names the cause without private values. `Server(anticipated=...)` changes which exceptions keep their
text; `anticipated=()` leaves tool errors to the SDK.

## A recovery tool

`unknown` outcomes point at `inspect_application_and_outputs` unless the server names its own inspection
tool by overriding `Server.failure`; see [Write a server](../guides/writing-a-server.md#a-recovery-tool).
`contracts.failure(name, error, unknown_next_action=...)` builds the result for any exception and is what
`Server.failure` returns.
