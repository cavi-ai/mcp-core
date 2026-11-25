export const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

const types = new Map([
  ["object", isObject],
  ["array", Array.isArray],
  ["integer", Number.isInteger],
  ["number", Number.isFinite],
  ["string", (value) => typeof value === "string"],
  ["boolean", (value) => typeof value === "boolean"],
  ["null", (value) => value === null],
]);

const keywords = new Set([
  "type", "properties", "required", "additionalProperties", "items",
  "title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly",
]);

function compile(schema, location) {
  if (typeof schema === "boolean") return () => schema;
  if (!isObject(schema)) throw new TypeError(`${location} must be a schema object or boolean`);
  for (const key of Object.keys(schema)) {
    if (!keywords.has(key)) throw new TypeError(`${location} uses unsupported schema keyword ${key}`);
  }

  const { type, properties = {}, required = [], additionalProperties = true, items = true } = schema;
  const declaredTypes = type === undefined ? [] : [type].flat();
  if ((type !== undefined && declaredTypes.length === 0) || declaredTypes.some((kind) => !types.has(kind))) {
    throw new TypeError(`${location} has an invalid type`);
  }
  if (!isObject(properties) || !Array.isArray(required) || required.some((key) => typeof key !== "string")) {
    throw new TypeError(`${location} has invalid properties or required fields`);
  }

  const checks = declaredTypes.map((kind) => types.get(kind));
  const propertyChecks = new Map(Object.entries(properties).map(([key, child]) =>
    [key, compile(child, `${location}.properties[${JSON.stringify(key)}]`)]));
  const checkAdditional = additionalProperties === true ? () => true : compile(additionalProperties, `${location}.additionalProperties`);
  const checkItem = items === true ? undefined : compile(items, `${location}.items`);

  return (value) => {
    if (checks.length && !checks.some((check) => check(value))) return false;
    if (isObject(value)) {
      return required.every((key) => Object.hasOwn(value, key)) &&
        Object.entries(value).every(([key, child]) => (propertyChecks.get(key) ?? checkAdditional)(child));
    }
    return !checkItem || !Array.isArray(value) || value.every(checkItem);
  };
}

export function compileObjectSchema(schema, location) {
  if (schema?.type !== "object") throw new TypeError(`${location} must declare type "object"`);
  return compile(schema, location);
}
