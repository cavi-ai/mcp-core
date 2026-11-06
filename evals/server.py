"""Representative Core server for wire-level evals; no application adapter is used."""

from typing import Annotated

from pydantic import Field

from cavi_mcp_core.contracts import Result
from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server(
    "cavi-mcp-core-eval", package="cavi-mcp-core",
    instructions="Echo text. The reserved input reject reports an invalid argument.",
)


class Echo(Result):
    text: str


class Status(Result):
    ready: bool


@mcp.tool(annotations=READ_ONLY)
async def status() -> Status:
    """Report whether this isolated contract-evaluation server is ready."""
    return Status(ready=True)


@mcp.tool(annotations=READ_ONLY)
async def echo(
    text: Annotated[str, Field(description="Text to echo; reject is reserved.")],
) -> Echo:
    """Echo text, or reject the reserved input reject."""
    if text == "reject":
        raise ValueError("text is reserved")
    return Echo(text=text)


if __name__ == "__main__":
    mcp.run(transport="stdio")
