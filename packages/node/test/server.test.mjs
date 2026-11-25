import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import { OperationRejected, OperationUnknown, READ_ONLY, createRouter, runStdio } from "../src/index.mjs";
import { connect, options, request, until } from "./harness.mjs";

const schema = { type: "object", properties: {}, additionalProperties: false };
const echo = { name: "echo", description: "Echo.", inputSchema: schema, annotations: READ_ONLY, handler: async (args) => ({ args }) };

const call = (id, name, args) => request(id, "tools/call", args === undefined ? { name } : { name, arguments: args });

test("initialize reports the configured identity", async () => {
  const route = createRouter(options([echo]));
  const { result } = await route(request(1, "initialize", {}));
  assert.deepEqual(result.serverInfo, { name: "example", version: "1.2.3" });
  assert.equal(result.protocolVersion, "2025-06-18");
  assert.equal(result.instructions, "Use the example tools.");
  assert.deepEqual(result.capabilities, { tools: {} });
});

test("ping answers and tools/list hides handlers", async () => {
  const route = createRouter(options([echo]));
  assert.deepEqual((await route(request(1, "ping"))).result, {});
  const { result } = await route(request(2, "tools/list"));
  assert.deepEqual(result.tools, [{ name: "echo", description: "Echo.", inputSchema: schema, annotations: READ_ONLY }]);
});

test("tools/call returns text and structured content", async () => {
  const route = createRouter(options([echo]));
  const { result } = await route(call(1, "echo", {}));
  assert.deepEqual(result.structuredContent, { args: {} });
  assert.equal(result.content[0].text, JSON.stringify({ args: {} }));
  assert.equal(result.isError, undefined);
});

test("protocol errors use the JSON-RPC codes", async () => {
  const route = createRouter(options([echo]));
  assert.equal((await route(request(1, "nope"))).error.code, -32601);
  assert.equal((await route(call(2, "missing", {}))).error.code, -32601);
  assert.equal((await route(call(3, ""))).error.code, -32602);
  assert.equal((await route(call(4, "echo", []))).error.code, -32602);
  assert.equal((await route(request(5, "tools/call", []))).error.code, -32602);
});

test("an anticipated tool error returns the structured failure", async () => {
  const tools = [
    { name: "bad", inputSchema: schema, handler: async () => { throw new OperationRejected("count must be positive"); } },
    { name: "lost", inputSchema: schema, handler: async () => { throw new OperationUnknown("gone", "APP_EXITED"); } },
  ];
  const route = createRouter(options(tools, { unknownNextAction: "inspect_project" }));
  const bad = (await route(call(1, "bad", {}))).result;
  assert.equal(bad.isError, true);
  assert.equal(bad.content[0].text, "count must be positive");
  assert.deepEqual(bad.structuredContent.error, { version: 1, code: "INVALID_ARGUMENT", operation: "bad", outcome: "rejected", retry_safe: false, next_action: "inspect_inputs" });
  const lost = (await route(call(2, "lost", {}))).result.structuredContent.error;
  assert.deepEqual([lost.code, lost.outcome, lost.next_action], ["APP_EXITED", "unknown", "inspect_project"]);
});

test("a defect gives the caller a generic message and logs details to stderr", async () => {
  const tools = [{ name: "boom", inputSchema: schema, handler: async () => { throw new TypeError("secret detail"); } }];
  const logged = [];
  const route = createRouter(options(tools, { stderr: { write: (text) => logged.push(text) } }));
  const { result } = await route(call(1, "boom", {}));
  assert.equal(result.isError, true);
  assert.ok(!JSON.stringify(result).includes("secret detail"));
  assert.equal(result.structuredContent.error.code, "OUTCOME_UNKNOWN");
  assert.equal(result.structuredContent.error.outcome, "unknown");
  assert.ok(logged.join("").includes("secret detail"));
});

test("stdio rejects malformed input with -32700 and -32600", async () => {
  const server = connect(runStdio, options([echo]));
  server.send("{not json");
  server.send({ jsonrpc: "1.0", id: 1, method: "ping" });
  server.send({ jsonrpc: "2.0", id: {}, method: "ping" });
  server.send([]);
  const replies = await server.finish();
  assert.deepEqual(replies.map((reply) => reply.error.code), [-32700, -32600, -32600, -32600]);
  assert.ok(replies.every((reply) => reply.id === null));
});

test("stdio answers requests in order and ignores other notifications", async () => {
  const server = connect(runStdio, options([echo]));
  server.send(request(1, "ping"));
  server.send({ jsonrpc: "2.0", method: "notifications/initialized" });
  server.send(call("b", "echo", {}));
  const replies = await server.finish();
  assert.deepEqual(replies.map((reply) => reply.id), [1, "b"]);
});

test("notifications/cancelled aborts the pending request and sends no reply", async () => {
  let aborted = false;
  let started = false;
  const slow = {
    name: "slow", inputSchema: schema,
    handler: (args, { signal }) => new Promise((resolve, reject) => {
      started = true;
      signal.addEventListener("abort", () => { aborted = true; reject(signal.reason); });
    }),
  };
  const server = connect(runStdio, options([slow]));
  server.send(call(7, "slow", {}));
  await until(() => started);
  server.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: 7 } });
  server.send(request(8, "ping"));
  const replies = await server.finish();
  assert.equal(aborted, true);
  assert.deepEqual(replies.map((reply) => reply.id), [8]);
});

test("a duplicate pending id and a 33rd pending request are refused", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const held = { name: "held", inputSchema: schema, handler: async () => { await gate; return {}; } };
  const server = connect(runStdio, options([held]));
  for (let id = 1; id <= 32; id += 1) server.send(call(id, "held", {}));
  server.send(call(1, "held", {}));
  server.send(call(33, "held", {}));
  await new Promise((resolve) => setTimeout(resolve, 50));
  release();
  const replies = await server.finish();
  const refused = replies.filter((reply) => reply.error);
  assert.deepEqual(refused.map((reply) => [reply.id, reply.error.code, reply.error.message]), [
    [1, -32600, "request ID is already pending"],
    [33, -32600, "too many pending requests"],
  ]);
  assert.equal(replies.filter((reply) => reply.result).length, 32);
  assert.deepEqual(replies.slice(0, 32).map((reply) => reply.id), Array.from({ length: 32 }, (_, index) => index + 1));
});

test("a failed write ends the run without a later reply or a hang", async () => {
  const written = [];
  const output = new Writable({ write(chunk, encoding, callback) { written.push(String(chunk)); callback(new Error("broken pipe")); } });
  const input = new PassThrough();
  const done = runStdio({ ...options([echo]), input, output });
  input.write(`${JSON.stringify(request(1, "ping"))}\n`);
  input.write(`${JSON.stringify(request(2, "ping"))}\n`);
  input.end();
  await done;
  assert.equal(written.length, 1);
});
