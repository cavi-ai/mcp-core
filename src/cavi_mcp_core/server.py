"""The MCP server type for CAVI tool servers."""

import functools
import logging
from importlib.metadata import version

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations

from .contracts import UnknownToolError, failure
from .schema import compact_schema

# Failures a caller can act on: invalid arguments, refused paths, an application that is
# missing, busy or timed out. The SDK hides the text of any other exception, so these are
# re-raised as ToolError. Anything else (TypeError, AttributeError, ...) is a defect and
# keeps its details on the server.
ANTICIPATED = (ValueError, RuntimeError, OSError)

# Tool annotations, by what a tool does to the world. None of the tools reaches beyond the
# local machine and the application it drives, so openWorldHint is always false.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

logger = logging.getLogger(__name__)


def need(value, name: str, action: str):
    """``value`` itself, or a ValueError naming the argument ``action`` requires."""
    if value is None:
        raise ValueError(f"{name} is required for action={action}")
    return value


def reject_foreign(action: str, given: dict, allowed: set | frozenset) -> None:
    """Refuse the arguments in ``given`` that ``action`` does not accept.

    ``given`` maps argument names to the values the caller passed; a value of None counts
    as not passed. The error lists the offending names in sorted order:
    ``Not valid for action=info: directory, graph``.
    """
    foreign = sorted({key for key, value in given.items() if value is not None} - allowed)
    if foreign:
        raise ValueError(f"Not valid for action={action}: {', '.join(foreign)}")


def report_anticipated(fn, anticipated=ANTICIPATED):
    """``fn`` with ``anticipated`` exceptions re-raised as ToolError, keeping their text."""

    @functools.wraps(fn)
    async def tool(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except anticipated as exc:
            raise ToolError(str(exc)) from exc

    return tool


class Server(MCPServer):
    """MCPServer with compact tool schemas and one structured failure for every tool error.

    ``package`` names the distribution whose version the server reports. ``anticipated``
    lists the exceptions whose text reaches the caller; ``()`` leaves tool errors to the
    SDK. ``compact=False`` publishes the SDK's schemas unchanged.
    """

    def __init__(self, name: str, *, package: str | None = None,
                 anticipated: tuple[type[BaseException], ...] = ANTICIPATED,
                 compact: bool = True, **kwargs):
        if package is not None:
            kwargs.setdefault("version", version(package))
        super().__init__(name, **kwargs)
        self.anticipated_errors = tuple(anticipated)
        self.compact_schemas = compact

    async def list_tools(self):
        tools = await super().list_tools()
        if not self.compact_schemas:
            return tools
        return [
            tool.model_copy(
                update={
                    "input_schema": compact_schema(tool.input_schema),
                    "output_schema": compact_schema(tool.output_schema, output=True),
                }
            )
            for tool in tools
        ]

    async def call_tool(self, name, arguments, context=None):
        try:
            # The SDK uses generic ToolError for missing names. Classify at its
            # dispatch lookup, without interpreting tool/transport error text.
            if self._tool_manager.get_tool(name) is None:
                raise UnknownToolError(f"Unknown tool: {name}")
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            if context is None:
                raise
            logger.exception("Tool execution or output conversion failed")
            return self.failure(name, exc)
        except ToolError as exc:
            if context is None:
                raise
            return self.failure(name, exc)

    def failure(self, name, error):
        """The error result a client receives; override to point at a recovery tool."""
        return failure(name, error)

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)
        if not self.anticipated_errors:
            return register

        def decorator(fn):
            register(report_anticipated(fn, self.anticipated_errors))
            # The module-level function stays the undecorated coroutine, so direct
            # callers and tests see the original exception types.
            return fn

        return decorator
