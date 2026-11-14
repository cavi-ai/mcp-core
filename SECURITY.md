# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting:
<https://github.com/cavi-ai/mcp-core/security/advisories/new>. Do not open a public issue.
Include affected versions, reproduction steps and impact.

## Supported versions

Until 1.0, security fixes go to the latest minor release and `main`.

## Scope

- The workspace and file rules: a path that leaves the workspace, or a read that is not bounded.
- Tool argument handling and the error result: an undeclared argument that reaches a tool, or an
  unexpected exception whose details reach the caller.
- Process execution, launch registration, supervision and cleanup.
- The library opens no network port, sends no telemetry and uses no accounts. The applications a server
  drives are out of scope; report those to their vendors.
