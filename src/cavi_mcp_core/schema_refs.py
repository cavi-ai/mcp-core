"""Normalize local schema references without traversing JSON literal data."""

import json
from collections import Counter

MAPS = ("properties", "patternProperties", "$defs", "dependentSchemas")
LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
SINGLES = (
    "items", "additionalProperties", "not", "if", "then", "else", "contains",
    "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contentSchema",
)
SCOPES = {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveRef"}
ANNOTATIONS = {"description", "default", "examples", "deprecated", "readOnly", "writeOnly"}


def map_schema(schema, visit, *, visit_root=True):
    """Map only schema positions; const, enum, examples and defaults are data."""
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for key in MAPS:
        if key in result:
            result[key] = {name: map_schema(value, visit) for name, value in result[key].items()}
    for key in LISTS:
        if key in result:
            result[key] = [map_schema(value, visit) for value in result[key]]
    for key in SINGLES:
        if key in result:
            result[key] = map_schema(result[key], visit)
    return visit(result) if visit_root else result


def preserve_structure(schema):
    """Keep scoped evaluation and descendant JSON Pointer targets in place."""
    found = []

    def check(node):
        ref = node.get("$ref", "")
        descendant = isinstance(ref, str) and ref.startswith("#/") and not (
            ref.startswith("#/$defs/") and ref[8:] and "/" not in ref[8:]
        )
        if (SCOPES & node.keys() or descendant
                or {"unevaluatedProperties", "unevaluatedItems"} & node.keys()):
            found.append(True)
        return node

    map_schema(schema, check)
    return bool(found)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validation(node):
    return {key: value for key, value in node.items() if key not in ANNOTATIONS}


def intern_definitions(schema):
    """Share repeated validation only when the complete serialized schema shrinks."""
    if not isinstance(schema, dict) or preserve_structure(schema):
        return schema
    result = schema
    while True:
        occurrences = Counter()

        def count(node, counts=occurrences):
            key = _encoded(_validation(node))
            if len(key) >= 48:
                counts[key] += 1
            return node

        map_schema(result, count, visit_root=False)
        names = result.get("$defs", {})
        number = 0
        while f"s{number}" in names:
            number += 1
        name = f"s{number}"
        reference = {"$ref": f"#/$defs/{name}"}
        reference_size = len(_encoded(reference).encode())
        ranked = []
        for key, count in occurrences.items():
            size = len(key.encode())
            saving = count * (size - reference_size) - size - len(name) - 16
            if count > 1 and saving > 0:
                ranked.append((saving, key))
        if not ranked:
            return result
        _, selected = max(ranked)

        def replace(node, target=selected, ref=reference):
            if _encoded(_validation(node)) != target:
                return node
            return {**ref, **{key: value for key, value in node.items() if key in ANNOTATIONS}}

        candidate = map_schema(result, replace, visit_root=False)
        candidate.setdefault("$defs", {})[name] = json.loads(selected)
        if len(_encoded(candidate).encode()) >= len(_encoded(result).encode()):
            return result
        result = candidate


def inline_definitions(schema):
    """Keep recursive, shared, scoped and conflicting references intact."""
    if not isinstance(schema, dict) or not schema.get("$defs"):
        return schema
    if preserve_structure(schema):
        return schema
    result = schema
    while result.get("$defs"):
        definitions = result["$defs"]
        pointers = {
            name: "#/$defs/" + name.replace("~", "~0").replace("/", "~1")
            for name in definitions
        }
        references = Counter()

        def count(node, refs=references):
            if isinstance(node.get("$ref"), str):
                refs[node["$ref"]] += 1
            return node

        map_schema(result, count)
        changed = False
        for name, definition in definitions.items():
            pointer = pointers[name]
            if references[pointer] != 1 or not isinstance(definition, dict):
                continue
            if any(ref.startswith(pointer + "/") for ref in references):
                continue
            inner = Counter()

            def count_inner(node, refs=inner):
                if isinstance(node.get("$ref"), str):
                    refs[node["$ref"]] += 1
                return node

            map_schema(definition, count_inner)
            if inner[pointer]:
                continue
            conflict = []

            def replace(node, target=pointer, body=definition, conflicts=conflict):
                if node.get("$ref") != target:
                    return node
                siblings = {key: value for key, value in node.items() if key != "$ref"}
                if siblings.keys() - ANNOTATIONS - {"$defs", "$schema"}:
                    conflicts.append(True)
                    return node
                if any(key in body and body[key] != value
                       for key, value in siblings.items()):
                    conflicts.append(True)
                    return node
                return {**body, **siblings}

            candidate = map_schema(result, replace)
            if conflict:
                continue
            candidate["$defs"].pop(name)
            if not candidate["$defs"]:
                candidate.pop("$defs")
            result = candidate
            changed = True
            break
        if not changed:
            break
    return result
