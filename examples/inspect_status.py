"""Start the example server, inspect its catalog and read its status."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("status_server.py"))],
    )
    async with Client(server, read_timeout_seconds=5) as client:
        tools = await client.list_tools()
        if not any(tool.name == "status" for tool in tools.tools):
            raise RuntimeError("The server did not advertise status.")
        result = await client.call_tool("status", {})
        if result.is_error:
            raise RuntimeError("The status tool failed.")
        print(json.dumps(result.structured_content))


if __name__ == "__main__":
    asyncio.run(main())
