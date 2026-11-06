"""Probe a representative Core server over stdio with the installed MCP evaluator."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    evaluator = Path(os.environ.get("MCPEVAL_EXECUTABLE", ROOT / "node_modules/.bin/mcpeval"))
    if not evaluator.exists():
        raise SystemExit("Install MCP Eval or set MCPEVAL_EXECUTABLE to its executable")
    reports = ROOT / ".tmp/evals"
    reports.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="protocol-", dir=reports))
    env = dict(os.environ, MCPEVAL_HOME=str(output / "store"))
    version = subprocess.check_output([str(evaluator), "--version"], text=True).strip()
    provenance = {"version": version, "source_revision": env.get("MCPEVAL_SOURCE_REVISION"),
                  "executable_sha256": hashlib.sha256(evaluator.resolve().read_bytes()).hexdigest()}
    (output / "evaluator.json").write_text(json.dumps(provenance, indent=2) + "\n")
    with (output / "mcpeval.json").open("w") as stream:
        result = subprocess.run([
            str(evaluator), "probe", "--server", "mcp-core-fixture",
            "--manifest", str(ROOT / "evals/manifest.json"), "--format", "json",
            "--min-readiness", "100", "--exclude-latency",
            "--", sys.executable, str(ROOT / "evals/server.py"),
        ], cwd=ROOT, env=env, stdout=stream, check=False)
    print(json.dumps({"mcpeval_exit": result.returncode, "evaluator": version,
                      "artifacts": str(output)}))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
