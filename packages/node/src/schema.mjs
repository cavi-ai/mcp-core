import Ajv from "ajv";

export const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

/** One Ajv per router; strict mode refuses at registration any keyword it would not enforce. */
export function schemaCompiler() {
  const ajv = new Ajv({ strict: true, coerceTypes: false, useDefaults: false, removeAdditional: false });
  /** Returns a check that answers undefined for a valid value and the validation errors text otherwise. */
  return function compileObjectSchema(schema, location) {
    if (schema?.type !== "object") throw new TypeError(`${location} must declare type "object"`);
    let validate;
    try {
      validate = ajv.compile(schema);
    } catch (error) {
      throw new TypeError(`${location}: ${error.message}`);
    }
    return (value) => (validate(value) ? undefined : ajv.errorsText(validate.errors));
  };
}
