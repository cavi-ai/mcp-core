import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { DESTRUCTIVE, OperationRejected, OperationUnknown, READ_ONLY, WRITES, failure } from "../src/index.mjs";

const cases = JSON.parse(readFileSync(new URL("../../../tests/fixtures/failure-result.json", import.meta.url), "utf8"));

test("failure matches the fixture shared with the Python core", () => {
  assert.ok(cases.length > 0);
  for (const { kind, code, operation, message, result } of cases) {
    const Kind = kind === "rejected" ? OperationRejected : OperationUnknown;
    const error = code === null ? new Kind(message) : new Kind(message, code);
    assert.deepEqual(failure(operation, error), result);
  }
});

test("an unknown outcome can name a recovery tool", () => {
  const result = failure("apply", new OperationUnknown("lost"), "inspect_transaction");
  assert.equal(result.structuredContent.error.next_action, "inspect_transaction");
  assert.equal(failure("apply", new OperationRejected("bad"), "inspect_transaction").structuredContent.error.next_action, "inspect_inputs");
});

test("annotations carry the Python core's hint values", () => {
  assert.deepEqual(READ_ONLY, { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
  assert.deepEqual(WRITES, { readOnlyHint: false, destructiveHint: false, openWorldHint: false });
  assert.deepEqual(DESTRUCTIVE, { readOnlyHint: false, destructiveHint: true, openWorldHint: false });
});
