# Contributing

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

Install Python 3.15 or newer and [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/cavi-ai/mcp-core.git
cd mcp-core
uv sync --locked --dev
```

## Checks

```bash
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests
```

The documentation tests build and verify `docs/mcp-core/source` and fail when the README module table
and the package, or the error codes in `reference/results-and-errors.md` and the codes a client
receives, differ. Change the documentation in the same pull request as the code.

The MCP Eval check needs Node.js and the MCP Eval executable (`npm install`, or `MCPEVAL_EXECUTABLE` set
to a build of [cavi-ai/mcp-eval](https://github.com/cavi-ai/mcp-eval)):

```bash
npm install --no-audit --no-fund --package-lock=false
npm run eval:protocol
```

A change to a contract every server returns, such as a result shape or an error code, is a change for
every server that depends on this package; say so in the pull request.

Add a line under Unreleased in `CHANGELOG.md`. Versions come from git tags and are never edited in a pull request.
