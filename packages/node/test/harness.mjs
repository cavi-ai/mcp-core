import { PassThrough } from "node:stream";

export const options = (tools, extra = {}) => ({
  name: "example",
  version: "1.2.3",
  protocolVersion: "2025-06-18",
  instructions: "Use the example tools.",
  tools,
  ...extra,
});

export const request = (id, method, params) => ({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) });

// Starts runStdio on in-memory streams; `send` writes one JSON line, `replies` reads parsed lines.
export function connect(runStdio, serverOptions) {
  const input = new PassThrough();
  const output = new PassThrough();
  const lines = [];
  let buffered = "";
  output.on("data", (chunk) => {
    buffered += chunk;
    const parts = buffered.split("\n");
    buffered = parts.pop();
    for (const part of parts) lines.push(JSON.parse(part));
  });
  const done = runStdio({ ...serverOptions, input, output });
  return {
    send: (message) => input.write(`${typeof message === "string" ? message : JSON.stringify(message)}\n`),
    finish: async () => {
      input.end();
      await done;
      return lines;
    },
    lines,
  };
}

export const until = async (condition) => {
  for (let tries = 0; tries < 200 && !condition(); tries += 1) await new Promise((resolve) => setImmediate(resolve));
  if (!condition()) throw new Error("condition not reached");
};
