"""Response models and the structured failure every tool error reports."""

from typing import ClassVar

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, ValidationError, model_serializer, model_validator

from .schema import validate_variant, variant_schema


class Result(BaseModel):
    """A tool result. Optional fields the tool did not set stay off the wire."""

    model_config = ConfigDict(extra="allow", strict=True)

    @model_serializer(mode="wrap")
    def preserve_shape(self, handler):
        present = set(self.model_fields_set)
        for name in self.model_fields_set:
            field = type(self).model_fields.get(name)
            if field is not None and field.alias:
                present.add(field.alias)
        return {key: value for key, value in handler(self).items() if key in present}


class VariantResult(Result):
    """A result with several valid shapes, validated without adding a discriminator field."""

    shapes: ClassVar[tuple[tuple[str, ...], ...]] = ()
    nullable_shape_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def require_shape(cls, value):
        fields = {field.alias or name for name, field in cls.model_fields.items()}
        return validate_variant(value, fields, cls.shapes, cls.nullable_shape_fields)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return variant_schema(core_schema, handler, cls.shapes, cls.nullable_shape_fields)


class UnknownToolError(ToolError):
    """The requested name was absent before any tool execution."""


class OperationRejected(ValueError):
    """Refused before any effect; ``error_code`` names the reason."""

    def __init__(self, message, code="INVALID_ARGUMENT"):
        super().__init__(message)
        self.error_code = code


class OperationUnknown(RuntimeError):
    """The effect may or may not have happened; inspect before any retry."""

    def __init__(self, message, code="OUTCOME_UNKNOWN"):
        super().__init__(message)
        self.error_code = code


def failure(name, error, unknown_next_action="inspect_application_and_outputs"):
    """The error result for ``error`` raised by tool ``name``; never authorizes a retry.

    Only local validation, a missing input or a missing tool counts as rejected. Anything
    else leaves the outcome unknown and points the caller at ``unknown_next_action``.
    """
    cause = error
    while (isinstance(cause, ToolError) and not isinstance(cause, UnexpectedToolError)
           and cause.__cause__ is not None):
        cause = cause.__cause__
    missing_tool = isinstance(cause, UnknownToolError)
    rejected = missing_tool or isinstance(cause, (ValueError, ValidationError, FileNotFoundError))
    if isinstance(cause, (OperationRejected, OperationUnknown)):
        code = cause.error_code
    else:
        code = (
            "TOOL_NOT_FOUND"
            if missing_tool
            else "INVALID_ARGUMENT"
            if isinstance(cause, (ValueError, ValidationError))
            else "INPUT_NOT_FOUND"
            if isinstance(cause, FileNotFoundError)
            else "OUTCOME_UNKNOWN"
        )
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=str(error))],
        structuredContent={
            "error": {
                "version": 1,
                "code": code,
                "operation": name,
                "outcome": "rejected" if rejected else "unknown",
                "retry_safe": False,
                "next_action": "list_tools"
                if missing_tool
                else "inspect_inputs"
                if rejected
                else unknown_next_action,
            }
        },
    )
