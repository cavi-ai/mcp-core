import { createInterface } from "node:readline";
import { OperationRejected, OperationUnknown, failure } from "./contracts.mjs";
import { compileObjectSchema, isObject } from "./schema.mjs";

const MAX_PENDING = 32;

const rpcError = (message, code) => Object.assign(new Error(message), { code });
const isRequestId = (value) => typeof value === "string" || Number.isInteger(value);
const invalidRequest = () => ({ jsonrpc: "2.0", id: null, error: { code: -32600, message: "invalid request" } });

/**
 * Builds `route(request, { signal })`, which answers one JSON-RPC request object.
 *
 * Options: name, version, protocolVersion, instructions, tools, unknownNextAction, stderr, anticipated.
 * Each tool is { name, description, inputSchema, outputSchema?, annotations?, handler(args, { signal }) }.
 * A handler that throws one of `anticipated` (default OperationRejected, OperationUnknown) returns the
 * structured failure; any other error is a defect: the caller gets a generic message and `stderr` the details.
 */
export function createRouter({ name, version, protocolVersion, instructions, tools = [], unknownNextAction, stderr = process.stderr,
  anticipated = [OperationRejected, OperationUnknown] }) {
  const registry = new Map();
  const listed = tools.map(({ handler, ...metadata }) => {
    const definition = structuredClone(metadata);
    const { name: toolName, inputSchema, outputSchema } = definition;
    registry.set(toolName, {
      handler,
      acceptsInput: compileObjectSchema(inputSchema, `${toolName}.inputSchema`),
      acceptsOutput: outputSchema === undefined ? undefined : compileObjectSchema(outputSchema, `${toolName}.outputSchema`),
    });
    return definition;
  });
  const capabilities = { tools: {} };
  return async function route(request, { signal } = {}) {
    const { id, method, params = {} } = request;
    if (!isRequestId(id)) return invalidRequest();
    try {
      if (!isObject(params)) throw rpcError("params must be an object", -32602);
      let result;
      if (method === "initialize") {
        result = { protocolVersion, capabilities, serverInfo: { name, version }, ...(instructions ? { instructions } : {}) };
      } else if (method === "ping") result = {};
      else if (method === "tools/list") result = { tools: structuredClone(listed) };
      else if (method === "tools/call") {
        if (typeof params.name !== "string" || !params.name) throw rpcError("tool name is required", -32602);
        if (!registry.has(params.name)) throw rpcError(`unknown tool: ${params.name}`, -32601);
        const { handler, acceptsInput, acceptsOutput } = registry.get(params.name);
        const args = params.arguments === undefined ? {} : params.arguments;
        if (!isObject(args)) throw rpcError("arguments must be an object", -32602);
        if (!acceptsInput(args)) throw rpcError(`arguments do not match ${params.name} inputSchema`, -32602);
        try {
          const value = await handler(args, { signal });
          const text = JSON.stringify(value === undefined ? null : value);
          const serialized = JSON.parse(text);
          if (acceptsOutput && !acceptsOutput(serialized)) {
            throw new TypeError(`result does not match ${params.name} outputSchema`);
          }
          result = { content: [{ type: "text", text }] };
          if (isObject(serialized)) result.structuredContent = serialized;
        } catch (error) {
          if (anticipated.some((kind) => error instanceof kind)) result = failure(params.name, error, unknownNextAction);
          else if (!signal?.aborted) {
            stderr.write(`tool ${params.name} failed: ${error instanceof Error ? error.stack : String(error)}\n`);
            result = failure(params.name, new Error(`Error executing tool ${params.name}`), unknownNextAction);
          } else result = failure(params.name, new Error("request cancelled"), unknownNextAction);
        }
      } else throw rpcError(`method not found: ${method}`, -32601);
      return { jsonrpc: "2.0", id, result };
    } catch (error) {
      return { jsonrpc: "2.0", id, error: { code: typeof error.code === "number" ? error.code : -32603, message: error.message } };
    }
  };
}

/** Serves newline-delimited JSON-RPC until input ends or output disconnects. */
export async function runStdio({ input = process.stdin, output = process.stdout, ...options }) {
  const route = createRouter(options);
  const lines = createInterface({ input, crlfDelay: Infinity });
  const pending = new Map();
  let queue = Promise.resolve();
  let disconnected = false;
  const disconnect = () => {
    if (disconnected) return;
    disconnected = true;
    lines.close();
    for (const active of pending.values()) active.controller.abort(new Error("MCP output disconnected"));
  };
  output.on("close", disconnect);
  output.on("error", disconnect);
  const send = (reply) => {
    if (disconnected) return;
    return new Promise((resolve) => {
      const finish = () => {
        output.off("error", finish);
        output.off("close", finish);
        resolve();
      };
      output.once("error", finish);
      output.once("close", finish);
      // A failed write emits "error" after its callback; hold ownership until then or close.
      output.write(`${JSON.stringify(reply)}\n`, (error) => { if (!error) finish(); });
    });
  };
  const enqueue = (callback) => { queue = queue.then(callback); };
  for await (const line of lines) {
    if (disconnected) break;
    if (!line.trim()) continue;
    let request;
    try { request = JSON.parse(line); }
    catch { enqueue(() => send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } })); continue; }
    if (!isObject(request) || request.jsonrpc !== "2.0" || typeof request.method !== "string" || !request.method ||
      (Object.hasOwn(request, "id") && !isRequestId(request.id))) {
      enqueue(() => send(invalidRequest()));
      continue;
    }
    if (!Object.hasOwn(request, "id")) {
      if (request.method === "notifications/cancelled") {
        const id = request.params?.requestId;
        const active = isRequestId(id) ? pending.get(id) : undefined;
        if (active && active.method !== "initialize") active.controller.abort(new Error("MCP request cancelled"));
      }
      continue;
    }
    if (pending.has(request.id) || pending.size >= MAX_PENDING) {
      const message = pending.has(request.id) ? "request ID is already pending" : "too many pending requests";
      enqueue(() => send({ jsonrpc: "2.0", id: request.id, error: { code: -32600, message } }));
      continue;
    }
    const active = { controller: new AbortController(), method: request.method };
    pending.set(request.id, active);
    enqueue(async () => {
      try {
        if (disconnected || active.controller.signal.aborted) return;
        const reply = await route(request, { signal: active.controller.signal });
        if (!active.controller.signal.aborted) await send(reply);
      } finally {
        pending.delete(request.id);
      }
    });
  }
  // A caller may half-close input after sending its requests.
  await queue;
  output.off("close", disconnect);
  output.off("error", disconnect);
}
