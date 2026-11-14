# Quickstart

From a source checkout, run the included read-only example:

```sh
git clone https://github.com/cavi-ai/mcp-core.git
cd mcp-core
uv sync --locked --dev
uv run --locked python examples/inspect_status.py
```

Expected standard output:

```json
{"ready": true}
```

The client starts `examples/status_server.py` as a subprocess, lists its tools and calls `status` over
stdio. Exiting the client closes that server. No application, model, account or external service is
needed. The example checks the server connection; it does not establish any consuming application's
behavior.

The server is a few lines:

```python
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server("status-example", package="cavi-mcp-core", instructions="Read readiness.")


@mcp.tool(annotations=READ_ONLY)
async def status() -> dict:
    return {"ready": True}


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

Next, [write a server](../guides/writing-a-server.md) with typed results and read how its
[errors](../reference/results-and-errors.md) reach the client.
