# Overview

MCP Core is the Python library the Cavi MCP servers are built on. It holds what every server shares, so
each server carries its own tools and nothing else:

- one server type with compact tool schemas, closed argument lists and one structured failure for every
  tool error;
- strict result models whose unset optional fields stay off the wire;
- workspace path rules for input files and new output directories;
- bounded file reads and an atomic, private operation journal;
- application discovery by environment override, default location and `PATH`;
- bounded subprocesses that stop the whole process group, with optional launch registration,
  supervision and inspection.

Blender MCP, Substance MCP, Houdini MCP, Adobe MCP, Premiere MCP and After Effects MCP depend on it.

## What it is not

MCP Core is a library for server authors. It runs no server of its own, starts no application and opens
no network port. Each server decides which applications it drives and what its tools do.

## Where to start

- [Installation](installation.md) adds the package to a server project.
- [Quickstart](quickstart.md) runs a read-only example server over stdio.
- [Write a server](../guides/writing-a-server.md) covers the server type and tool annotations.
- [Results and errors](../reference/results-and-errors.md) is the result and error contract every server
  returns to its clients.
