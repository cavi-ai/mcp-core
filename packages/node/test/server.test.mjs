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

test("tools/call rejects arguments that violate the published schema before dispatch", async () => {
  const received = [];
  const inputSchema = {
    type: "object",
    properties: {
      profile: {
        type: "object",
        properties: { labels: { type: "array", items: { type: "string" } } },
        required: ["labels"],
        additionalProperties: false,
      },
    },
    required: ["profile"],
    additionalProperties: false,
  };
  const tool = { ...echo, inputSchema, handler: (args) => { received.push(args); return {}; } };
  const route = createRouter(options([tool]));
  for (const args of [{}, { profile: [] }, { profile: { labels: [1] } }, { profile: { labels: [], extra: true } }]) {
    assert.equal((await route(call(1, tool.name, args))).error?.code, -32602);
  }
  assert.deepEqual(received, []);
  const valid = { profile: { labels: ["ready"] } };
  assert.deepEqual((await route(call(2, tool.name, valid))).result.structuredContent, {});
  assert.deepEqual(received, [valid]);
});

test("router rejects unsupported schema assertions at registration", () => {
  const inputSchema = {
    type: "object",
    properties: { path: { $ref: "#/$defs/path" } },
    $defs: { path: { type: "string" } },
  };
  assert.throws(() => createRouter(options([{ ...echo, inputSchema }])), TypeError);
});

test("tools without an output schema return text for non-object results", async () => {
  for (const [value, text] of [["ready", '"ready"'], [[1, 2], "[1,2]"], [null, "null"], [undefined, "null"]]) {
    const route = createRouter(options([{ ...echo, handler: () => value }]));
    const { result } = JSON.parse(JSON.stringify(await route(call(1, echo.name))));
    assert.deepEqual(result, { content: [{ type: "text", text }] });
  }
});

test("tools/call validates the serialized result against the published output schema", async () => {
  const outputSchema = { type: "object", properties: { ready: { type: "boolean" } }, required: ["ready"] };
  const route = createRouter(options([
    { ...echo, name: "valid", outputSchema, handler: () => ({ ready: true }) },
    { ...echo, name: "invalid", outputSchema, handler: () => ({ ready: true, toJSON: () => ({ ready: "yes" }) }) },
  ], { stderr: new PassThrough() }));
  assert.deepEqual((await route(call(1, "valid"))).result.structuredContent, { ready: true });
  const { result } = await route(call(2, "invalid"));
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error.outcome, "unknown");
  assert.equal(result.structuredContent.error.retry_safe, false);
});

test("protocol errors use the JSON-RPC codes", async () => {
  const route = createRouter(options([echo]));
  assert.equal((await route(request(1, "nope"))).error.code, -32601);
  assert.equal((await route(call(2, "missing", {}))).error.code, -32601);
  assert.equal((await route(call(3, ""))).error.code, -32602);
  assert.equal((await route(call(4, "echo", []))).error.code, -32602);
  assert.equal((await route(request(5, "tools/call", []))).error.code, -32602);
  assert.equal((await route(request(null, "ping"))).error?.code, -32600);
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
  server.send(request(null, "ping"));
  server.send(request(1.5, "ping"));
  server.send([]);
  const replies = await server.finish();
  assert.deepEqual(replies.map((reply) => reply.error?.code), [-32700, -32600, -32600, -32600, -32600, -32600]);
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

test("a failed write ends the run while request input stays open", async () => {
  const written = [];
  const output = new Writable({ write(chunk, encoding, callback) { written.push(String(chunk)); callback(new Error("broken pipe")); } });
  const input = new PassThrough();
  let finished = false;
  const done = runStdio({ ...options([echo]), input, output }).then(() => { finished = true; });
  input.write(`${JSON.stringify(request(1, "ping"))}\n`);
  input.write(`${JSON.stringify(request(2, "ping"))}\n`);
  try {
    await until(() => finished);
    assert.equal(written.length, 1);
  } finally {
    input.end();
    await done;
  }
});
