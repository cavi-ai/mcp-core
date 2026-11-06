import copy
import json
import unittest

from jsonschema import Draft202012Validator

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

    def test_nullable_literals_become_enums_without_null_default_annotations(self):
        schema = {"properties": {
            "mode": {"anyOf": [{"const": "fast", "type": "string"}, {"type": "null"}],
                     "default": None},
            "extra": {"type": "object", "additionalProperties": True}}}
        result = compact_schema(schema)
        self.assertEqual(result["properties"]["mode"],
                         {"enum": ["fast", None]})
        self.assertEqual(result["properties"]["extra"], {"type": "object"})

    def test_unsafe_union_branches_are_kept(self):
        branch = {"type": "object", "title": "Inner", "properties": {"a": {"type": "string"}},
                  "description": "kept"}
        result = compact_schema({"anyOf": [branch, {"type": "null"}]})
        self.assertEqual(len(result["anyOf"]), 2)
        self.assertNotIn("title", result["anyOf"][0])

    def test_compacts_homogeneous_tuples_without_relaxing_their_length(self):
        schema = {"type": "array", "prefixItems": [{"type": "number"}] * 3,
                  "minItems": 3, "maxItems": 3}
        self.assertEqual(compact_schema(schema), {
            "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
        })

    def test_omits_only_null_defaults_and_redundant_enum_types(self):
        schema = {"properties": {
            "optional": {"type": ["string", "null"], "default": None},
            "dry_run": {"type": "boolean", "default": True},
            "mode": {"type": "string", "enum": ["read", "write"]},
            "not_redundant": {"type": "integer", "enum": [True, 1]},
            "literal": {"type": "object", "const": {"title": "literal"}},
        }}
        result = compact_schema(schema)["properties"]
        self.assertNotIn("default", result["optional"])
        self.assertIs(result["dry_run"]["default"], True)
        self.assertNotIn("type", result["mode"])
        self.assertEqual(result["not_redundant"], schema["properties"]["not_redundant"])
        self.assertEqual(result["literal"], {"const": {"title": "literal"}})


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


class SchemaSemanticsTests(unittest.TestCase):
    def assert_equivalent(self, schema, values):
        original = copy.deepcopy(schema)
        candidate = compact_schema(schema)
        Draft202012Validator.check_schema(candidate)
        before, after = Draft202012Validator(schema), Draft202012Validator(candidate)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(before.is_valid(value), after.is_valid(value))
        self.assertEqual(schema, original)
        self.assertEqual(compact_schema(candidate), candidate)
        return candidate

    def test_inline_single_use_definitions_and_keep_literal_data(self):
        schema = {"type": "object", "properties": {
            "value": {"$ref": "#/$defs/Value", "description": "A named value."},
            "literal": {"const": {"$ref": "#/$defs/Value"}},
        }, "$defs": {"Value": {"type": "string", "minLength": 2}}}
        candidate = self.assert_equivalent(schema, [
            {}, {"value": "hi"}, {"value": "x"}, {"value": 1},
            {"literal": {"$ref": "#/$defs/Value"}},
        ])
        self.assertNotIn("$defs", candidate)
        self.assertEqual(candidate["properties"]["value"], {
            "type": "string", "minLength": 2, "description": "A named value.",
        })

    def test_recursive_definitions_and_pointer_escaping_keep_their_meaning(self):
        schema = {"$ref": "#/$defs/a~1b", "$defs": {"a/b": {
            "type": "object", "properties": {"child": {"$ref": "#/$defs/a~1b"}},
            "additionalProperties": False,
        }}}
        candidate = self.assert_equivalent(schema, [{}, {"child": {}}, {"child": 3},
                                                    {"unexpected": 1}])
        self.assertIn("a/b", candidate["$defs"])

    def test_typed_union_compaction_preserves_all_branches(self):
        schema = {"anyOf": [
            {"type": "array", "items": {"type": "string"}, "minItems": 1},
            {"type": "object", "additionalProperties": {"type": "string"},
             "maxProperties": 2},
            {"type": "null"},
        ]}
        candidate = self.assert_equivalent(schema, [None, [], ["x"], [1], {},
                                                    {"x": "ok"}, {"x": 1}, "x", 1])
        self.assertNotIn("anyOf", candidate)

    def test_outer_union_constraints_are_not_overwritten(self):
        schema = {"type": "string", "maxLength": 2,
                  "anyOf": [{"type": "string", "maxLength": 10}, {"type": "null"}]}
        self.assert_equivalent(schema, [None, "", "hi", "long", 1])

    def test_closed_and_bounded_arrays_remain_equivalent(self):
        for schema in [
            {"type": "array", "prefixItems": [{"type": "number"}] * 2, "items": False},
            {"type": "array", "prefixItems": [{"type": "number"}] * 3, "maxItems": 2},
            {"type": "array", "prefixItems": [{"type": "number"}] * 2},
        ]:
            self.assert_equivalent(schema, [[], [1], [1, 2], [1, "x"], [1, 2, "x"], None])

    def test_shared_validation_is_factored_without_moving_parameter_descriptions(self):
        shape = {"type": "array", "items": {"type": "number", "minimum": -100,
                                           "maximum": 100}, "minItems": 3, "maxItems": 3}
        schema = {"type": "object", "properties": {
            name: {**shape, "description": f"{name} coordinates."}
            for name in ("position", "rotation", "scale", "origin")
        }, "additionalProperties": False}
        candidate = self.assert_equivalent(schema, [
            {}, {"position": [1, 2, 3]}, {"rotation": [1, 2]}, {"scale": [1, 2, 101]},
            {"origin": [1, True, 3]}, {"unexpected": [1, 2, 3]},
        ])
        self.assertLess(len(json.dumps(candidate)), len(json.dumps(schema)))
        self.assertIn("$defs", candidate)
        for name, value in candidate["properties"].items():
            self.assertEqual(value["description"], f"{name} coordinates.")
            self.assertIn("$ref", value)

    def test_scoped_references_are_not_relocated(self):
        shape = {"$id": "https://example.test/coordinates", "type": "array",
                 "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
        schema = {"properties": {"a": shape, "b": shape}}
        candidate = self.assert_equivalent(schema, [{"a": [1, 2, 3]}, {"b": [1]}, {}])
        self.assertNotIn("$defs", candidate)

    def test_reference_siblings_do_not_change_property_evaluation_scope(self):
        schema = {"type": "object", "$defs": {"Value": {
            "type": "object", "properties": {"x": {"type": "integer"}},
        }}, "properties": {"value": {"$ref": "#/$defs/Value", "additionalProperties": False}}}
        self.assert_equivalent(schema, [{}, {"value": {}}, {"value": {"x": 1}},
                                       {"value": {"x": "bad"}}, {"value": {"y": 1}}])

    def test_union_unevaluated_constraints_keep_their_branch_scope(self):
        schema = {"allOf": [{"properties": {"x": {"type": "integer"}}}], "anyOf": [
            {"type": "object", "unevaluatedProperties": False}, {"type": "null"},
        ]}
        self.assert_equivalent(schema, [None, {}, {"x": 1}, {"x": "bad"}, {"y": 1}])

    def test_lifted_object_assertions_do_not_see_outer_properties(self):
        for alternate in ({"type": "null"}, {"type": "array"}):
            schema = {"properties": {"x": {"type": "integer"}}, "anyOf": [
                {"type": "object", "additionalProperties": False}, alternate,
            ]}
            self.assert_equivalent(schema, [None, [], {}, {"x": 1}, {"x": "bad"}])
        schema = {"additionalProperties": False, "anyOf": [
            {"type": "object", "properties": {"x": {"type": "integer"}}},
            {"type": "null"},
        ]}
        self.assert_equivalent(schema, [None, {}, {"x": 1}, {"y": 1}])

    def test_compaction_preserves_descendant_pointer_targets(self):
        shape = {"type": "object", "properties": {
            "long_property_name": {"type": "string", "minLength": 20}},
            "additionalProperties": False}
        for pointer, definitions in (
            ("#/properties/a/properties/long_property_name", {}),
            ("#/$defs/A/properties/long_property_name", {"A": shape}),
        ):
            schema = {"type": "object", "properties": {
                "a": shape, "b": shape, "c": {"$ref": pointer}}, "$defs": definitions}
            self.assert_equivalent(schema, [{}, {"c": "short"}, {"c": "x" * 20}, {"c": 1}])
        schema = {"properties": {
            "a": {"type": "array", "prefixItems": [{"type": "number"}] * 2,
                  "maxItems": 2},
            "b": {"$ref": "#/properties/a/prefixItems/0"},
        }}
        self.assert_equivalent(schema, [{"a": [1, 2], "b": 1}, {"b": "bad"}])

    def test_explicit_additional_properties_still_marks_evaluated_properties(self):
        schema = {"type": "object", "additionalProperties": True,
                  "unevaluatedProperties": False}
        self.assert_equivalent(schema, [{}, {"x": 1}, {"x": None}, None])

    def test_boolean_union_branches_remain_valid_schemas(self):
        for branches in (
            [{"type": "string", "minLength": 1}, False],
            [False, {"type": "string"}],
            [True, {"type": "null"}],
            [False, {"type": "null"}],
        ):
            self.assert_equivalent({"anyOf": branches}, [None, "", "x", 0, {}, []])


if __name__ == "__main__":
    unittest.main()
