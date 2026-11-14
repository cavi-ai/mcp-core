# Security model

## What the library does

MCP Core runs inside the server that imports it, with that server's permissions. It opens no network
port, sends no telemetry and uses no accounts. A server built on it speaks MCP over the transport the
server chooses; the included examples use stdio.

## Inputs

- Tool arguments are closed: an undeclared argument is refused before the tool runs.
- `workspace` and `files.inside` confine paths to a workspace root and follow symbolic links before the
  check, so a link cannot lead out of it.
- `files` reads are bounded and refuse a file that changes while it is read; JSON parsing refuses NaN and
  Infinity.

## Errors

An exception outside the anticipated types keeps its text and traceback on the server; the caller gets
only `Error executing tool <name>`. Anticipated exceptions reach the caller with their text, so a server
keeps private values out of its error messages. The error contract is in
[Results and errors](../reference/results-and-errors.md).

## Processes

`proc.run` bounds output and time and stops the whole process group. With `CAVI_MCP_PROCESS_OWNER` set,
every command is registered with its owner before it runs, and a refused or missing registration stops
the launch with exit code 78. Owner configuration, capabilities and registration records are private to
the user: configuration and records allow no group or other access, and paths must be canonical and free
of symbolic links. Journal records are written readable only by their owner.

## Reporting a vulnerability

Report vulnerabilities privately as described in `SECURITY.md` in the repository.
