import copy
import unittest

from cavi_mcp_core.schema import compact_schema, validate_variant


class CompactSchemaTests(unittest.TestCase):
    def test_compaction_preserves_literals_bounds_and_input_schema(self):
        schema = {"title": "Wrapper", "properties": {
            "value": {"title": "Value", "anyOf": [{"type": "number", "minimum": 0},
                                                  {"type": "null"}]},
            "literal": {"const": {"title": "User data"}},
            "count": {"anyOf": [{"type": "integer"}, {"type": "number"}], "default": 2}}}
        original = copy.deepcopy(schema)
        result = compact_schema(schema, output=True)
        self.assertEqual(result["properties"]["value"], {"type": ["number", "null"], "minimum": 0})
        self.assertEqual(result["properties"]["literal"], {"const": {"title": "User data"}})
        self.assertEqual(result["properties"]["count"], {"type": "number"})
        self.assertEqual(schema, original)

    def test_input_schemas_keep_defaults_and_nullable_literals_become_enums(self):
        schema = {"properties": {
            "mode": {"anyOf": [{"const": "fast", "type": "string"}, {"type": "null"}],
                     "default": None},
            "extra": {"type": "object", "additionalProperties": True}}}
        result = compact_schema(schema)
        self.assertEqual(result["properties"]["mode"],
                         {"type": ["string", "null"], "enum": ["fast", None], "default": None})
        self.assertEqual(result["properties"]["extra"], {"type": "object"})

    def test_unsafe_union_branches_are_kept(self):
        branch = {"type": "object", "title": "Inner", "properties": {"a": {"type": "string"}},
                  "description": "kept"}
        result = compact_schema({"anyOf": [branch, {"type": "null"}]})
        self.assertEqual(len(result["anyOf"]), 2)
        self.assertNotIn("title", result["anyOf"][0])


class VariantTests(unittest.TestCase):
    def test_requires_one_declared_shape_and_refuses_unexpected_nulls(self):
        shapes = (("path",), ("closed",))
        self.assertEqual(validate_variant({"closed": True}, {"path", "closed"}, shapes, set()),
                         {"closed": True})
        with self.assertRaisesRegex(ValueError, "missing the fields"):
            validate_variant({"other": 1}, {"path", "closed"}, shapes, set())
        with self.assertRaisesRegex(ValueError, "null"):
            validate_variant({"path": None}, {"path", "closed"}, shapes, set())
        self.assertEqual(validate_variant({"path": None}, {"path"}, shapes, {"path"}),
                         {"path": None})


if __name__ == "__main__":
    unittest.main()
