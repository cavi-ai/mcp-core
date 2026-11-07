"""The first-result example must reach its server over stdio."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ExampleTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "requires Linux or macOS process ownership",
    )
    def test_supervised_example_retains_a_successful_checked_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve() / "owner"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "examples/supervised_process.py"),
                    "--out",
                    str(directory),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"completed": True, "registered_groups": 1})
            receipt = json.loads((directory / "result.json").read_text())
            self.assertEqual(receipt["exit_code"], 0)
            self.assertIsNone(receipt["error"])
            self.assertIsNone(receipt["cleanup_error"])

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
