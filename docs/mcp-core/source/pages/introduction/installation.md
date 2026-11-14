# Installation

## Requirements

- Python 3.15 or later and [uv](https://docs.astral.sh/uv/).
- The MCP Python SDK 2 and pydantic 2, installed with the package.
- Process supervision and launch registration need Linux or macOS; process inspection needs macOS.

## Add it to a server project

```sh
uv add cavi-mcp-core
```

The package has no command of its own. Import its modules from your server:

```python
from cavi_mcp_core.server import READ_ONLY, Server
from cavi_mcp_core.contracts import Result
```

## Work from a checkout

```sh
git clone https://github.com/cavi-ai/mcp-core.git
cd mcp-core
uv sync --locked --dev
```

The checks are in [Testing](../reference/testing.md).
