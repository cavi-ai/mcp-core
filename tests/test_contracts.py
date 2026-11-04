import unittest

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import Field, ValidationError

from cavi_mcp_core.contracts import (
    OperationRejected,
    OperationUnknown,
    Result,
    UnknownToolError,
    VariantResult,
    failure,
)


def wrapped(error):
    try:
        raise ToolError(str(error)) from error
    except ToolError as exc:
        return exc


class FailureTests(unittest.TestCase):
    def error(self, name, error, **kwargs):
        result = failure(name, error, **kwargs)
        self.assertTrue(result.is_error)
        self.assertEqual(result.content[0].text, str(error))
        return result.structured_content["error"]

    def test_classifies_the_root_cause_of_a_tool_error(self):
        cases = (
            (ValueError("bad"), "INVALID_ARGUMENT", "rejected", "inspect_inputs"),
            (FileNotFoundError("gone"), "INPUT_NOT_FOUND", "rejected", "inspect_inputs"),
            (RuntimeError("busy"), "OUTCOME_UNKNOWN", "unknown",
             "inspect_application_and_outputs"),
            (UnknownToolError("Unknown tool: x"), "TOOL_NOT_FOUND", "rejected", "list_tools"),
        )
        for cause, code, outcome, next_action in cases:
            with self.subTest(cause=type(cause).__name__):
                error = self.error("tool", wrapped(cause))
                self.assertEqual(error, {"version": 1, "code": code, "operation": "tool",
                                         "outcome": outcome, "retry_safe": False,
                                         "next_action": next_action})

    def test_validation_errors_are_rejections(self):
        class Model(Result):
            count: int

        with self.assertRaises(ValidationError) as caught:
            Model(count="1")
        self.assertEqual(self.error("tool", wrapped(caught.exception))["code"], "INVALID_ARGUMENT")

    def test_operation_errors_carry_their_code(self):
        rejected = self.error("tool", wrapped(OperationRejected("locked", "PROJECT_LOCKED")))
        unknown = self.error("tool", wrapped(OperationUnknown("lost", "APP_EXITED")))
        self.assertEqual((rejected["code"], rejected["outcome"]), ("PROJECT_LOCKED", "rejected"))
        self.assertEqual((unknown["code"], unknown["outcome"]), ("APP_EXITED", "unknown"))
        self.assertEqual(OperationRejected("x").error_code, "INVALID_ARGUMENT")
        self.assertEqual(OperationUnknown("x").error_code, "OUTCOME_UNKNOWN")

    def test_unexpected_errors_stay_unknown_and_name_the_recovery_tool(self):
        try:
            raise UnexpectedToolError("hidden") from ValueError("details")
        except UnexpectedToolError as exc:
            error = self.error("apply", exc, unknown_next_action="inspect_transaction")
        self.assertEqual((error["code"], error["outcome"], error["next_action"]),
                         ("OUTCOME_UNKNOWN", "unknown", "inspect_transaction"))
        rejected = self.error("apply", wrapped(ValueError("bad")),
                              unknown_next_action="inspect_transaction")
        self.assertEqual(rejected["next_action"], "inspect_inputs")


class ResultTests(unittest.TestCase):
    def test_unset_optional_fields_stay_off_the_wire(self):
        class Status(Result):
            ready: bool
            note: str | None = None
            passed: bool | None = Field(default=None, alias="pass")

        self.assertEqual(Status(ready=True).model_dump(), {"ready": True})
        self.assertEqual(Status(ready=True, note=None).model_dump(), {"ready": True, "note": None})
        self.assertEqual(Status.model_validate({"ready": True, "pass": False}).model_dump(
            by_alias=True), {"ready": True, "pass": False})
        self.assertEqual(Status(ready=True, extra=1).model_dump(), {"ready": True, "extra": 1})

    def test_results_are_strict(self):
        class Status(Result):
            ready: bool

        with self.assertRaises(ValidationError):
            Status(ready="yes")

    def test_variant_results_require_a_declared_shape(self):
        class Project(VariantResult):
            shapes = (("path",), ("closed",))
            nullable_shape_fields = frozenset({"file"})
            path: str | None = None
            closed: bool | None = None
            file: str | None = None

        self.assertEqual(Project(closed=True).model_dump(), {"closed": True})
        self.assertEqual(Project(path="a", file=None).model_dump(), {"path": "a", "file": None})
        with self.assertRaises(ValidationError):
            Project(file="x")
        with self.assertRaises(ValidationError):
            Project(path=None, closed=True)
        schema = Project.model_json_schema()
        self.assertEqual(schema["anyOf"], [{"required": ["path"]}, {"required": ["closed"]}])
        self.assertEqual(schema["properties"]["path"]["type"], "string")


if __name__ == "__main__":
    unittest.main()
