import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cavi_mcp_core.journal import archive, completion, fingerprint, write_json


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_write_json_replaces_atomically_and_privately(self):
        path = self.root / "state.json"
        write_json(path, {"a": 1})
        write_json(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 2})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.root.iterdir()], ["state.json"])
        with self.assertRaises(ValueError):
            write_json(self.root / "nan.json", {"a": float("nan")})
        self.assertEqual([p.name for p in self.root.iterdir()], ["state.json"])

    def test_fingerprint_ignores_key_order(self):
        self.assertEqual(fingerprint({"a": 1, "b": [1, 2]}), fingerprint({"b": [1, 2], "a": 1}))
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))

    def test_completion_reads_only_the_matching_receipt(self):
        path = self.root / "done.json"
        self.assertIsNone(completion(path, "op-1"))
        path.write_text(json.dumps({"operation_id": "op-1", "ok": True}))
        self.assertEqual(completion(path, "op-1"), {"operation_id": "op-1", "ok": True})
        with self.assertRaisesRegex(ValueError, "does not match"):
            completion(path, "op-2")
        path.write_text(json.dumps({"operation_id": "op-1", "ok": "yes"}))
        with self.assertRaisesRegex(ValueError, "does not match"):
            completion(path, "op-1")

    def test_completion_refuses_symbolic_links_and_empty_files(self):
        target = self.root / "real.json"
        target.write_text(json.dumps({"operation_id": "op", "ok": True}))
        link = self.root / "link.json"
        os.symlink(target, link)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            completion(link, "op")
        empty = self.root / "empty.json"
        empty.write_text("")
        with self.assertRaisesRegex(ValueError, "unsafe or too large"):
            completion(empty, "op")

    def test_archive_is_idempotent_and_refuses_a_different_outcome(self):
        path = self.root / "recovery.json"
        archive(path, {"operation_id": "op"}, {"resolution": "abandoned"})
        archive(path, {"operation_id": "op"}, {"resolution": "abandoned"})
        self.assertEqual(json.loads(path.read_text()),
                         {"prior_operation": {"operation_id": "op"}, "resolution": "abandoned"})
        with self.assertRaisesRegex(ValueError, "different outcome"):
            archive(path, {"operation_id": "op"}, {"resolution": "completed"})


if __name__ == "__main__":
    unittest.main()
