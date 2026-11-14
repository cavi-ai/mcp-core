"""The MCP server type for CAVI tool servers."""

import functools
import inspect
import logging
from importlib.metadata import version

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .contracts import Result, UnknownToolError, failure
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


def _result_titles() -> set[str]:
    """The validation-error titles of every ``Result`` model defined so far."""
    titles, pending = set(), [Result]
    while pending:
        for model in pending.pop().__subclasses__():
            titles.update({model.__name__, model.model_config.get("title") or model.__name__})
            pending.append(model)
    return titles


def report_anticipated(fn, anticipated=ANTICIPATED):
    """``fn`` with ``anticipated`` exceptions re-raised as ToolError, keeping their text.

    A ``Result`` that fails its own validation is a defect, and the tool may already have
    acted, so it is reported like any other defect: logged, with an unknown outcome.
    """

    @functools.wraps(fn)
    async def tool(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except anticipated as exc:
            if isinstance(exc, ValidationError) and exc.title in _result_titles():
                raise
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
        for tool in self._tool_manager.list_tools():
            self._close_arguments(tool)

    @staticmethod
    def _close_arguments(tool):
        model = tool.fn_metadata.arg_model
        model.model_config = {**model.model_config, "extra": "forbid"}
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema()

    def add_tool(self, fn, name=None, *args, **kwargs):
        """Reject undeclared arguments in both the catalog and the actual dispatcher."""
        registered_name = name or fn.__name__
        existing = self._tool_manager.get_tool(registered_name)
        description = args[1] if len(args) > 1 else kwargs.get("description")
        super().add_tool(fn, name, *args, **kwargs)
        tool = self._tool_manager.get_tool(registered_name)
        self._close_arguments(tool)
        if existing is None and description is None and fn.__doc__:
            tool.description = inspect.cleandoc(fn.__doc__)

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
