"""Shared response-schema normalization and action-shape validation."""

import math

from .schema_refs import ANNOTATIONS, inline_definitions, intern_definitions, preserve_structure


def _has_type(value, kind):
    if isinstance(kind, list):
        return any(_has_type(value, item) for item in kind)
    if kind == "integer":
        return type(value) is int or (type(value) is float and value.is_integer())
    if kind == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return {
        "null": value is None,
        "boolean": type(value) is bool,
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(kind, False)


def _compact_array(schema):
    items = schema.get("prefixItems")
    if not items or not all(item == items[0] for item in items):
        return
    tail = schema.get("items", True)
    maximum = schema.get("maxItems")
    bounded = isinstance(maximum, int) and maximum <= len(items)
    if tail is False:
        schema["maxItems"] = min(maximum, len(items)) if maximum is not None else len(items)
    elif tail != items[0] and not bounded:
        return
    schema["items"] = items[0]
    schema.pop("prefixItems")

def _disjoint_union(branches):
    """Lift assertions that apply to disjoint JSON types, never enum/const or scopes."""
    numeric = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    allowed = {
        "number": numeric,
        "integer": numeric,
        "string": {"minLength", "maxLength", "pattern"},
        "array": {"items", "prefixItems", "contains", "minContains", "maxContains",
                  "minItems", "maxItems", "uniqueItems"},
        "object": {"properties", "required", "additionalProperties", "patternProperties",
                   "propertyNames", "minProperties", "maxProperties", "dependentRequired",
                   "dependentSchemas"},
        "boolean": set(),
        "null": set(),
    }
    kinds = [branch.get("type") for branch in branches]
    if not all(isinstance(kind, str) and kind in allowed for kind in kinds):
        return None
    if len(set(kinds)) != len(kinds) or {"integer", "number"} <= set(kinds):
        return None
    merged = {"type": kinds}
    for branch, kind in zip(branches, kinds, strict=True):
        for key, value in branch.items():
            if key == "type":
                continue
            if key not in allowed[kind] or (key in merged and merged[key] != value):
                return None
            merged[key] = value
    return merged


def compact_union(schema):
    """Shorten equivalent union forms; output defaults are documentation only."""
    branches = schema.get("anyOf", [])
    if branches and "type" not in schema and all(
        set(item) == {"type"} and isinstance(item["type"], str) for item in branches
    ):
        types = list(dict.fromkeys(item["type"] for item in branches))
        if "number" in types and "integer" in types:
            types.remove("integer")
        schema.pop("anyOf")
        schema["type"] = types[0] if len(types) == 1 else types
    elif (len(branches) == 2 and {"type": "null"} in branches
          and not schema.keys() - ANNOTATIONS - {"anyOf", "$defs", "$schema", "title"}):
        other = next(item for item in branches if item != {"type": "null"})
        safe = {
            "type",
            "items",
            "additionalProperties",
            "properties",
            "required",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minLength",
            "maxLength",
            "pattern",
            "format",
            "minProperties",
            "maxProperties",
            "enum",
            "const",
        }
        merged = None
        if set(other) == {"const"}:
            value = other["const"]
            merged = {"enum": [value] if value is None else [value, None]}
        elif set(other) == {"enum"}:
            merged = {"enum": [*other["enum"]]}
            if not any(value is None for value in merged["enum"]):
                merged["enum"].append(None)
        if isinstance(other.get("type"), str) and set(other) <= safe:
            merged = {**other, "type": [other["type"], "null"]}
            if "const" in merged:
                merged["enum"] = [merged.pop("const"), None]
            elif "enum" in merged:
                merged["enum"] = [*merged["enum"], None]
        if merged and not any(
            key in schema and schema[key] != value for key, value in merged.items()
        ):
            schema.pop("anyOf")
            schema.update(merged)
    if (schema.get("anyOf")
            and not schema.keys() - ANNOTATIONS - {"anyOf", "$defs", "$schema", "title"}):
        merged = _disjoint_union(schema["anyOf"])
        if merged and not any(
            key in schema and schema[key] != value for key, value in merged.items()
        ):
            schema.pop("anyOf")
            schema.update(merged)
    return schema


def _compact_schema(schema, *, output=False):
    """Remove redundant schema titles without changing property names or literal data."""
    if not isinstance(schema, dict):
        return schema
    result = {
        key: value
        for key, value in schema.items()
        if key != "title" and not (key == "default" and (output or value is None))
    }
    if result.get("additionalProperties") is True:
        result.pop("additionalProperties")
    for key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
        if key in result:
            result[key] = {
                name: _compact_schema(value, output=output) for name, value in result[key].items()
            }
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [_compact_schema(value, output=output) for value in result[key]]
    for key in (
        "items",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "unevaluatedProperties",
        "propertyNames",
        "contentSchema",
    ):
        if key in result:
            result[key] = _compact_schema(result[key], output=output)
    _compact_array(result)
    result = compact_union(result)
    kind = result.get("type")
    values = result.get("enum")
    if "const" in result:
        values = [result["const"]]
    if kind and values and all(_has_type(value, kind) for value in values):
        result.pop("type")
    return result


def compact_schema(schema, *, output=False):
    """Shorten equivalent validation rules while retaining use-site descriptions."""
    if preserve_structure(schema):
        return schema
    normalized = _compact_schema(schema, output=output)
    inlined = inline_definitions(normalized)
    result = _compact_schema(inlined, output=output) if inlined != normalized else normalized
    return intern_definitions(result)


def validate_variant(value, fields, shapes, nullable):
    if isinstance(value, dict):
        if any(item is None and key in fields and key not in nullable
               for key, item in value.items()):
            raise ValueError("Result contains null for a non-null response field")
        if shapes and not any(all(key in value for key in shape) for shape in shapes):
            raise ValueError("Result is missing the fields of an action response")
    return value


def variant_schema(core_schema, handler, shapes, nullable):
    schema = handler(core_schema)
    for key, field in schema.get("properties", {}).items():
        field.pop("default", None)
        if key in nullable:
            continue
        variants = field.get("anyOf")
        if variants and {"type": "null"} in variants:
            rest = [item for item in variants if item != {"type": "null"}]
            field.pop("anyOf")
            field.update(rest[0] if len(rest) == 1 else {"anyOf": rest})
        if not field or set(field) <= {"title"}:
            field["not"] = {"type": "null"}
    if shapes:
        schema["anyOf"] = [{"required": list(shape)} for shape in shapes]
    return schema
