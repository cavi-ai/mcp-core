# Process supervision

## One supervised command

On Linux and macOS, `supervision.run` owns the registration endpoint and checked process groups for one
command. Call it from the resident process that owns the resource reservation:

```python
from cavi_mcp_core import supervision

receipt = supervision.run(
    command, cwd=workspace, env=environment, directory=new_record_directory,
    max_seconds=1800, max_rss_kib=48 * 1024 * 1024,
    minimum_available_kib=2 * 1024 * 1024,
)
```

The caller selects all three limits; zero `minimum_available_kib` disables the headroom policy. The API
checks registered PID/group/birth identities and socket peers, persists records before acknowledgment,
bounds registration reads, and retires checked groups after exit, cancellation or a limit. New owner
directories must be canonical and unused; their new parent links are synced. A configured owner cannot be
replaced. Python's character locale and macOS startup text-encoding fields are normalized without
importing unrelated ambient environment values.

The receipt includes `exit_code`, `error`, `cleanup_error`, `registered_groups`, `peak_rss_kib` and
elapsed `seconds`. Cleanup uncertainty requires retaining the resource reservation and records.
Admission, workload inputs and final application reports stay with the consuming adapter;
[agent-sessions](https://github.com/cavi-ai/agent-sessions) supplies queue state.

For a neutral Python command with retained records:

```sh
uv run --locked python examples/supervised_process.py --out /absolute/new-output
```

## A resident owner

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

The separate POSIX session uses `supervision.run` and retains the original execution deadline after
caller loss. It cannot execute a worker until the submitting process syncs its handle and acknowledges
that exact handle. State and supplied environment are private, bounded records; unrelated ambient values
are not inherited. An existing directory refuses another launch.

Keep the token-free handle or directory to reattach with `resident.inspect`. `resident.wait` has its own
observation timeout; timeout does not cancel the operation. `resident.cancel` writes one attempt-bound
cancellation request and the resident retires checked processes. Both calls preserve earlier evidence.

A durable result binds the resident PID/birth identity, attempt and sealed input hash. Observers sync the
receipt and its directory before accepting completion. `state: finished` means the owner recorded a
receipt; check `exit_code`, `error` and `cleanup_error` before declaring application success or releasing
admission. Cleanup uncertainty, or owner exit without a result, yields `state: unresolved`. A reused PID
is never adopted. The consuming adapter keeps its broker reservation until cleanup is verified. These
APIs cover registered launch sites; engine-created helpers do not automatically register.

```sh
uv run --locked python examples/resident_process.py --out /absolute/new-output
```

## Checked groups

`processes.inspect`, `processes.group` and `processes.cleanup` expose the same checked identity/group
operations for consuming adapters. With `known` identities, `processes.members` also rechecks previously
observed processes missing from the current groups.

If the same process is still alive outside the checked inventory, sampling and cleanup refuse a clear
result. The error records its PID, birth identity and previous/current groups; retain the reservation and
records for inspection. Unregistered groups are not adopted or signalled. Movement into another
registered group remains covered after identity checks. An exited or reused PID does not transfer
ownership. Helpers never observed by the owner still require an audited registration path or separate
containment.

## Helper retirement before publication

A registered root worker can ask its independent owner to close helper execution before publishing a
report or releasing broker admission:

```python
from cavi_mcp_core import retirement

proof = retirement.close(timeout=15)
if proof["cleanup_error"] is not None:
    raise RuntimeError(proof["cleanup_error"])
# Retain proof["path"] and proof["sha256"] with the consuming adapter's evidence.
```

The owner denies further helper launches before cleanup, retires registered helper groups and rechecks
its previously observed identities. The root worker remains alive for publication. The private receipt
binds its attempt, PID/birth identity, request and group identities; it is synced before acknowledgment.
Repeating the same root request returns the same receipt without repeating cleanup.

Timeouts, missing receipts and non-null `cleanup_error` retain admission. Socket peer checks prevent
another helper from retiring the root's scope. Helpers that detached before any observation remain
outside this proof. Broker policy and application acceptance remain with the consuming adapter.
