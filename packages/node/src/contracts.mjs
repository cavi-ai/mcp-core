// Tool annotations by what a tool does to the world; the hint values match the Python core.
export const READ_ONLY = Object.freeze({ readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false });
export const WRITES = Object.freeze({ readOnlyHint: false, destructiveHint: false, openWorldHint: false });
export const DESTRUCTIVE = Object.freeze({ readOnlyHint: false, destructiveHint: true, openWorldHint: false });

/** Refused before any effect; `errorCode` names the reason. */
export class OperationRejected extends Error {
  constructor(message, errorCode = "INVALID_ARGUMENT") {
    super(message);
    this.name = "OperationRejected";
    this.errorCode = errorCode;
  }
}

/** The effect may or may not have happened; inspect before any retry. */
export class OperationUnknown extends Error {
  constructor(message, errorCode = "OUTCOME_UNKNOWN") {
    super(message);
    this.name = "OperationUnknown";
    this.errorCode = errorCode;
  }
}

/** The error result for `error` raised by tool `operation`; never authorizes a retry. */
export function failure(operation, error, unknownNextAction = "inspect_application_and_outputs") {
  const rejected = error instanceof OperationRejected;
  const code = error instanceof OperationRejected || error instanceof OperationUnknown ? error.errorCode : "OUTCOME_UNKNOWN";
  return {
    isError: true,
    content: [{ type: "text", text: error instanceof Error ? error.message : String(error) }],
    structuredContent: {
      error: {
        version: 1,
        code,
        operation,
        outcome: rejected ? "rejected" : "unknown",
        retry_safe: false,
        next_action: rejected ? "inspect_inputs" : unknownNextAction,
      },
    },
  };
}
