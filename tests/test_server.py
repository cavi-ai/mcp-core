import asyncio
import unittest
from importlib.metadata import version

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from pydantic import BaseModel

from cavi_mcp_core.contracts import OperationRejected, Result, failure
from cavi_mcp_core.server import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITES,
    Server,
    need,
    reject_foreign,
)


class Echo(Result):
    text: str
    note: str | None = None


def build(**kwargs):
    mcp = Server("demo", package="cavi-mcp-core", **kwargs)

    @mcp.tool(annotations=READ_ONLY)
    async def echo(text: str, note: str | None = None) -> Echo:
        """Echo text."""
        if text == "bad":
            raise ValueError("text must not be bad")
        if text == "busy":
            raise RuntimeError("application is busy")
        if text == "locked":
            raise OperationRejected("project is locked", "PROJECT_LOCKED")
        if text == "defect":
            raise TypeError("internal detail")
        return Echo(text=text)

    return mcp, echo


async def session_call(mcp, name, arguments):
    async with Client(mcp) as client:
        return await client.call_tool(name, arguments)


async def session_tools(mcp):
    async with Client(mcp) as client:
        return (await client.list_tools()).tools


def call(mcp, name, arguments):
    return asyncio.run(session_call(mcp, name, arguments))


class ServerTests(unittest.TestCase):
    def test_reports_the_package_version(self):
        mcp, _ = build()
        self.assertEqual(mcp.version, version("cavi-mcp-core"))
        self.assertEqual(Server("demo", version="9.9.9").version, "9.9.9")

    def test_success_returns_only_the_fields_the_tool_set(self):
        mcp, _ = build()
        result = call(mcp, "echo", {"text": "hi"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"text": "hi"})

    def test_anticipated_errors_keep_their_text_and_classification(self):
        mcp, _ = build()
        cases = (("bad", "INVALID_ARGUMENT", "rejected", "text must not be bad"),
                 ("busy", "OUTCOME_UNKNOWN", "unknown", "application is busy"),
                 ("locked", "PROJECT_LOCKED", "rejected", "project is locked"))
        for text, code, outcome, message in cases:
            with self.subTest(text=text):
                result = call(mcp, "echo", {"text": text})
                self.assertTrue(result.is_error)
                self.assertIn(message, result.content[0].text)
                error = result.structured_content["error"]
                self.assertEqual((error["code"], error["outcome"], error["operation"]),
                                 (code, outcome, "echo"))

    def test_a_defect_keeps_its_details_on_the_server(self):
        mcp, _ = build()
        with self.assertLogs("cavi_mcp_core.server", "ERROR"):
            result = call(mcp, "echo", {"text": "defect"})
        self.assertTrue(result.is_error)
        self.assertNotIn("internal detail", result.content[0].text)
        self.assertEqual(result.structured_content["error"]["outcome"], "unknown")

    def test_a_result_that_breaks_its_contract_is_an_unknown_outcome(self):
        mcp = Server("demo", version="1")
        written = []

        class Recipe(BaseModel):
            count: int

        @mcp.tool(annotations=WRITES)
        async def save(text: str) -> Echo:
            written.append(text)
            return Echo(text=len(text))

        @mcp.tool(annotations=WRITES)
        async def plan(recipe: dict) -> Echo:
            return Echo(text=str(Recipe.model_validate(recipe).count))

        with self.assertLogs("cavi_mcp_core.server", "ERROR"):
            result = call(mcp, "save", {"text": "hi"})
        self.assertEqual(written, ["hi"])
        error = result.structured_content["error"]
        self.assertEqual((error["code"], error["outcome"], error["next_action"]),
                         ("OUTCOME_UNKNOWN", "unknown", "inspect_application_and_outputs"))
        error = call(mcp, "plan", {"recipe": {"count": "many"}}).structured_content["error"]
        self.assertEqual((error["code"], error["outcome"]), ("INVALID_ARGUMENT", "rejected"))

    def test_unknown_tools_are_rejected_with_a_recovery_hint(self):
        mcp, _ = build()
        result = call(mcp, "missing", {})
        error = result.structured_content["error"]
        self.assertEqual((error["code"], error["outcome"], error["next_action"]),
                         ("TOOL_NOT_FOUND", "rejected", "list_tools"))

    def test_invalid_arguments_are_rejections(self):
        mcp, _ = build()
        result = call(mcp, "echo", {"text": 3})
        self.assertEqual(result.structured_content["error"]["code"], "INVALID_ARGUMENT")

    def test_unknown_arguments_are_rejected_instead_of_silently_ignored(self):
        mcp, _ = build()
        result = call(mcp, "echo", {"text": "hi", "unexpected": 1})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "INVALID_ARGUMENT")
        tool = asyncio.run(session_tools(mcp))[0]
        self.assertIs(tool.input_schema["additionalProperties"], False)
        self.assertFalse(call(mcp, "echo", {"text": "hi"}).is_error)

    def test_parameterless_tools_refuse_unknown_arguments_without_running(self):
        mcp = Server("status", version="1")
        calls = []

        @mcp.tool(name="read_status", annotations=READ_ONLY)
        async def status() -> Echo:
            calls.append(True)
            return Echo(text="ready")

        result = call(mcp, "read_status", {"unexpected": 1})
        self.assertTrue(result.is_error)
        self.assertEqual(calls, [])
        self.assertEqual(result.structured_content["error"]["code"], "INVALID_ARGUMENT")
        tool = asyncio.run(session_tools(mcp))[0]
        self.assertIs(tool.input_schema["additionalProperties"], False)
        self.assertEqual(call(mcp, "read_status", {}).structured_content, {"text": "ready"})
        self.assertEqual(calls, [True])

    def test_directly_added_tools_have_the_same_closed_input_contract(self):
        mcp = Server("direct", version="1")

        async def status() -> dict:
            return {"ready": True}

        mcp.add_tool(status, name="read_status")
        self.assertTrue(call(mcp, "read_status", {"unexpected": 1}).is_error)
        self.assertFalse(call(mcp, "read_status", {}).is_error)

    def test_constructor_tools_have_the_same_closed_input_contract(self):
        async def status() -> Echo:
            return Echo(text="ready")

        mcp = Server("prebuilt", version="1", tools=[Tool.from_function(status)])
        self.assertTrue(call(mcp, "status", {"unexpected": 1}).is_error)
        self.assertEqual(call(mcp, "status", {}).structured_content, {"text": "ready"})

    def test_python_docstrings_drop_source_indent_and_keep_relative_code_indent(self):
        mcp = Server("docs", version="1")

        @mcp.tool(annotations=READ_ONLY)
        async def read_status() -> Echo:
            """Read the status.

            Example::

                read_status()
            """
            return Echo(text="ready")

        description = asyncio.run(session_tools(mcp))[0].description
        self.assertEqual(description, "Read the status.\n\nExample::\n\n    read_status()")

    def test_explicit_descriptions_are_not_rewritten_by_registration(self):
        mcp = Server("docs", version="1", warn_on_duplicate_tools=False)
        description = "    Literal code\n        preserved indentation\n"

        async def read_status() -> Echo:
            """Different fallback text."""
            return Echo(text="ready")

        mcp.add_tool(read_status, description=description)
        mcp.add_tool(read_status)
        self.assertEqual(asyncio.run(session_tools(mcp))[0].description, description)

    def test_without_anticipated_errors_the_sdk_hides_tool_error_text(self):
        mcp, _ = build(anticipated=())
        result = call(mcp, "echo", {"text": "bad"})
        self.assertNotIn("text must not be bad", result.content[0].text)
        self.assertEqual(result.structured_content["error"]["outcome"], "unknown")

    def test_failure_can_name_a_recovery_tool(self):
        class Recovering(Server):
            def failure(self, name, error):
                return failure(name, error, unknown_next_action="inspect_job")

        mcp = Recovering("demo", version="1")

        @mcp.tool(annotations=WRITES)
        async def start() -> dict:
            raise RuntimeError("lost")

        error = call(mcp, "start", {}).structured_content["error"]
        self.assertEqual(error["next_action"], "inspect_job")

    def test_tool_schemas_are_compact_unless_disabled(self):
        compact, _ = build()
        full, _ = build(compact=False)
        compact_tool = asyncio.run(session_tools(compact))[0]
        full_tool = asyncio.run(session_tools(full))[0]
        self.assertNotIn("title", compact_tool.input_schema)
        self.assertEqual(compact_tool.input_schema["properties"]["note"]["type"],
                         ["string", "null"])
        self.assertNotIn("default", compact_tool.output_schema["properties"]["note"])
        self.assertIn("title", full_tool.input_schema)

    def test_decorated_tools_stay_directly_callable(self):
        _, echo = build()
        with self.assertRaises(ValueError):
            asyncio.run(echo("bad"))
        self.assertEqual(asyncio.run(echo("hi")), Echo(text="hi"))

    def test_direct_server_calls_raise_instead_of_returning_failures(self):
        mcp, _ = build()
        with self.assertRaises(ToolError):
            asyncio.run(mcp.call_tool("echo", {"text": "bad"}))
        with self.assertRaises(ToolError):
            asyncio.run(mcp.call_tool("missing", {}))


class HelperTests(unittest.TestCase):
    def test_need_returns_a_passed_value_and_names_a_missing_one(self):
        for value in ("a.png", 0, False, "", []):
            self.assertIs(need(value, "path", "info"), value)
        with self.assertRaisesRegex(ValueError, r"^path is required for action=info$"):
            need(None, "path", "info")

    def test_reject_foreign_lists_unexpected_arguments_in_order(self):
        reject_foreign("info", {"path": "a", "graph": None}, {"path"})
        with self.assertRaisesRegex(ValueError, r"^Not valid for action=info: directory, graph$"):
            reject_foreign("info", {"path": "a", "graph": "g", "directory": "d"}, {"path"})

    def test_annotations_never_reach_beyond_the_machine(self):
        for annotation in (READ_ONLY, WRITES, DESTRUCTIVE):
            self.assertFalse(annotation.open_world_hint)
        self.assertTrue(READ_ONLY.read_only_hint and READ_ONLY.idempotent_hint)
        self.assertTrue(DESTRUCTIVE.destructive_hint)
        self.assertFalse(WRITES.read_only_hint or WRITES.destructive_hint)


if __name__ == "__main__":
    unittest.main()
