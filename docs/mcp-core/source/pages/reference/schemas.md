# Schemas

`Server` publishes compact tool schemas unless it is created with `compact=False`.

`schema.compact_schema(schema, output=False)` returns a copy without titles or
`additionalProperties: true`, with equivalent union forms shortened, and validates the same values.
`output=True` also drops defaults, which are documentation only in a response.

`schema.validate_variant` and `schema.variant_schema` back `contracts.VariantResult`: a value must carry
the fields of one declared shape, a shape field may be null only when it is listed in
`nullable_shape_fields`, and the schema states each shape as a required set.

Input schemas are closed: `Server` sets `additionalProperties: false` on every tool's arguments, and the
dispatcher refuses an undeclared argument before the tool runs.
