# Workspaces

A server confines the files it reads and the directories it writes to one workspace root. The
`workspace` module resolves that root and checks every path against it. It uses the standard library
only and imports nothing else from the package.

## The root

`workspace.root(env, explicit=None, *, default=None, create=False)` returns the resolved workspace root:
`explicit`, else the environment variable `env`, else `default`.

- The variable must hold an absolute path; with no `default` it is required.
- An empty variable counts as unset.
- Nothing is touched unless `create` is true. With `create` the default is made on first use, and a root
  chosen by argument or variable must already be a directory, so a mistyped path is refused instead of
  becoming a new empty workspace.

`workspace.existing_root(env, explicit=None, *, default=None)` resolves a root that must exist and never
creates it. `workspace.existing(path)` does the same for a root you already hold.

```python
from pathlib import Path

from cavi_mcp_core import workspace

ROOT_ENV = "CAVI_EXAMPLE_WORKSPACE"
root = workspace.root(ROOT_ENV, default=Path.home() / "Documents" / "ExampleMCP", create=True)
```

## Inputs and outputs

`workspace.input_file(root, name, extensions)` resolves an existing file with a supported suffix inside
the root. Symbolic links are followed before the check, so a link that points out of the workspace is
refused. `extensions` holds lower-case suffixes with the dot; the match ignores case. Every refusal has
the same message: `Input must be a supported file inside the workspace`.

`workspace.output_path(root, name, *, parents=False)` validates a new output directory inside the root
without creating it. It may not be the root itself, leave it (including through links) or exist, and its
parent must exist unless `parents` is true, in which case the missing parents are created.
`workspace.new_output_dir` validates and creates it.

`workspace.rel(root, path)` gives a path relative to the root with forward slashes.

| Refusal | Raised | Reported to the caller |
|---|---|---|
| Not a supported file inside the workspace | `ValueError` | `INVALID_ARGUMENT`, rejected |
| Output outside the workspace or the root itself | `ValueError` | `INVALID_ARGUMENT`, rejected |
| Output parent missing | `ValueError` | `INVALID_ARGUMENT`, rejected |
| Root missing or not a directory | `ValueError` | `INVALID_ARGUMENT`, rejected |
| Output already exists | `FileExistsError` | `OUTCOME_UNKNOWN`, unknown |

Each refusal happens before anything is written. A tool that checks its output before it acts and wants
the caller told so raises `OperationRejected` for an existing output; see
[Results and errors](../reference/results-and-errors.md).
