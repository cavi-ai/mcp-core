# Processes

## Bounded runs

`proc.run(args, timeout)` runs a command with bounded output and one deadline. On a timeout or a
cancellation it stops the whole process group (process tree on Windows) and returns once the process
exits. `proc.run_sync` is the blocking form and `proc.diagnostics(result)` formats what a failed run
printed.

## Owned execution

On POSIX, `proc.run` and `proc.run_sync` register commands when `CAVI_MCP_PROCESS_OWNER` names a private
owner configuration file. Other subprocess APIs can use `owned_exec.command(args, cwd=..., env=...)` with
the same working directory and environment. Embedded hosts supply a trusted Python interpreter through
`python=...`. A configured owner cannot be dropped or replaced through a child environment.

The configuration contains `version: 1`, an `attempt` identifier, absolute `socket` and `records` paths,
and a per-attempt `token`. Files and the Unix socket must belong to the current user; configuration and
records allow no group or other access. Paths must be canonical and free of symbolic links.

Before executing the command, the gate sends one newline-delimited JSON frame:
`{"token": "...", "request": {...}}`. The version 1 request contains its attempt, launch ID, PID, parent
PID, isolated process group, command, working directory and environment digest. The owner validates the
fixed workload and process identity, then syncs a private record containing the exact `request` and its
OS-derived `birth_identity`, including the record's directory entry.

The acknowledgment requires `launch_permitted: true`, the canonical request's `request_sha256`, a relative
JSON `record` filename and its `record_sha256`. Canonical request bytes use sorted compact JSON followed
by a newline. Record hashes cover the exact file bytes. The gate checks the record and unchanged owner
configuration before exec; the PID and group remain unchanged. Missing, changed or refused registration
exits with code 78. One absolute five-second deadline and a 64 KiB response limit bound the handshake.

The consuming supervisor owns admission, idempotent launch IDs, process lifetime and cleanup. This gate
supports audited launch sites; it does not contain arbitrary daemonizing programs. Keep owner
configuration, capabilities and registration records private.

[Process supervision](supervision.md) provides an owner for one command.
