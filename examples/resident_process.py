"""Start a neutral independent owner and wait through its durable handle."""

import argparse
import json
import sys
from pathlib import Path

from cavi_mcp_core import resident


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="New canonical private state directory"
    )
    args = parser.parse_args()
    handle = resident.start(
        [sys.executable, "-c", "print('ready')"],
        cwd=Path.cwd().resolve(),
        env={},
        directory=args.out,
        max_seconds=5,
        max_rss_kib=256 * 1024,
        minimum_available_kib=0,
    )
    print(json.dumps(handle), flush=True)
    result = resident.wait(handle["directory"], timeout=10)
    receipt = result["receipt"]
    completed = (
        result["state"] == "finished"
        and receipt["exit_code"] == 0
        and receipt["error"] is None
        and receipt["cleanup_error"] is None
    )
    print(json.dumps({"completed": completed, **result}))
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
