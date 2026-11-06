"""The first-result example must reach its server over stdio."""

import json
import subprocess
import sys
import unittest
from pathlib import Path


class ExampleTests(unittest.TestCase):
    def test_status_example_returns_the_tool_result(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "examples" / "inspect_status.py")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ready": True})


if __name__ == "__main__":
    unittest.main()
