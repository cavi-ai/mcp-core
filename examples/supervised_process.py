"""Supervise one neutral Python command and retain its process records."""

import argparse
import json
import os
import sys
from pathlib import Path

from cavi_mcp_core import supervision
from cavi_mcp_core.journal import create_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New canonical output directory")
    args = parser.parse_args()
    receipt = supervision.run(
        [sys.executable, "-c", "print('ready')"],
        cwd=Path.cwd().resolve(),
        env=dict(os.environ),
        directory=args.out,
        max_seconds=5,
        max_rss_kib=256 * 1024,
        minimum_available_kib=0,
    )
    create_json(args.out / "result.json", receipt)
    completed = (
        receipt["exit_code"] == 0 and receipt["error"] is None and receipt["cleanup_error"] is None
    )
    print(
        json.dumps({"completed": completed, "registered_groups": len(receipt["registered_groups"])})
    )
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
