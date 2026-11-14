# Configuration

MCP Core reads one environment variable of its own. Every other variable is chosen by the server that
uses it: the workspace variable passed to `workspace.root` and the application overrides passed to the
`apps` functions.

## Environment variables

| Variable | Meaning |
|---|---|
| `CAVI_MCP_PROCESS_OWNER` | Path to a private owner configuration file. When set, `proc.run` and `proc.run_sync` register every command with that owner before it runs; see [Processes](../guides/processes.md#owned-execution). A child environment cannot drop or replace it. |

## Variables a server passes in

| Argument | Used by | Rule |
|---|---|---|
| `env` | `workspace.root`, `workspace.existing_root` | Absolute path; required when no default is given. |
| `env_dir`, `env_app`, `env_bin` | `apps.find_directory`, `apps.find_app`, `apps.find_executable` | An override is final: a wrong one is reported as not found or as the wrong application, never replaced. |
| `env` | `apps.bundle` | The `.app` to use; an override that names another application is refused. |

## Limits

- `sampling` calls accept `timeout` in seconds, capped at two seconds.
- The launch registration handshake has one five-second deadline and a 64 KiB response limit.
