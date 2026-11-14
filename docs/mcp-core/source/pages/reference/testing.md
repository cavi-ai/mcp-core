# Testing

## Checks

```sh
uv sync --locked --dev
uv run --locked python -m unittest discover -s tests
uv run --locked ruff check .
uv run --locked python -m scripts.docs.build
uv run --locked python -m scripts.docs.verify
```

## Protocol evaluation

The CI protocol gate runs MCP Eval against a representative stdio server using Core's result and error
contracts:

```sh
npm install --no-audit --no-fund --package-lock=false
npm run eval:protocol
```

CI builds the evaluator from `cavi-ai/mcp-eval` on GitHub `main` and records its resolved revision with
the JSON reports under `.tmp/evals/`. The gate requires all manifest cases and a readiness score of 100,
excluding runner latency. The fixture checks the shared server layer; application behavior is evaluated
in each consuming server. Locally, `MCPEVAL_EXECUTABLE` selects a source-built binary; the default npm
wrapper installs its checksum-verified release.
