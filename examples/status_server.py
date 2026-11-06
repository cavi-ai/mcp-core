"""A read-only status tool served over stdio."""

from cavi_mcp_core.server import READ_ONLY, Server

mcp = Server(
    "status-example", package="cavi-mcp-core", instructions="Read the server's readiness."
)


@mcp.tool(annotations=READ_ONLY)
async def status() -> dict[str, bool]:
    """Report whether this example is ready."""
    return {"ready": True}


if __name__ == "__main__":
    mcp.run(transport="stdio")
