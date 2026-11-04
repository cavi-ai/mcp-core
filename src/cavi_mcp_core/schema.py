"""Shared response-schema normalization and action-shape validation."""

def compact_union(schema):
    """Shorten equivalent union forms; output defaults are documentation only."""
    branches = schema.get("anyOf", [])
    if branches and all(
        set(item) == {"type"} and isinstance(item["type"], str) for item in branches
    ):
        types = list(dict.fromkeys(item["type"] for item in branches))
        if "number" in types and "integer" in types:
            types.remove("integer")
        schema.pop("anyOf")
        schema["type"] = types[0] if len(types) == 1 else types
    elif len(branches) == 2 and {"type": "null"} in branches:
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
        if isinstance(other.get("type"), str) and set(other) <= safe:
            schema.pop("anyOf")
            schema.update(other)
            schema["type"] = [other["type"], "null"]
            if "const" in schema:
                schema["enum"] = [schema.pop("const"), None]
            elif "enum" in schema:
                schema["enum"] = [*schema["enum"], None]
    return schema


def compact_schema(schema, *, output=False):
    """Remove redundant schema titles without changing property names or literal data."""
    if not isinstance(schema, dict):
        return schema
    result = {
        key: value
        for key, value in schema.items()
        if key != "title" and not (output and key == "default")
    }
    if result.get("additionalProperties") is True:
        result.pop("additionalProperties")
    for key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
        if key in result:
            result[key] = {
                name: compact_schema(value, output=output) for name, value in result[key].items()
            }
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key in result:
            result[key] = [compact_schema(value, output=output) for value in result[key]]
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
            result[key] = compact_schema(result[key], output=output)
    return compact_union(result)


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
