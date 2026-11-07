import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cavi_mcp_core.journal import archive, completion, fingerprint, write_json


class JournalTests(unittest.TestCase):
    def test_unavailable_filesystem_root_refuses_without_looping(self):
        from unittest.mock import patch

        from cavi_mcp_core import journal

        root = self.root.resolve()
        probes = []

        def absent(path):
            probes.append(path)
            if len(probes) > len(root.parents) + 6:
                raise AssertionError("directory search did not terminate")
            return False

        with (
            patch.object(Path, "exists", autospec=True, side_effect=absent),
            self.assertRaises(FileNotFoundError),
        ):
            journal.create_directory(root / "unavailable")

    def test_new_directory_chain_syncs_the_created_links(self):
        from unittest.mock import patch

        from cavi_mcp_core import journal

        root = self.root.resolve()
        target = root / "parent" / "owner"
        synced = []
        sync = journal._sync_directory

        def observed(path):
            sync(path)
            synced.append(Path(path))

        with patch.object(journal, "_sync_directory", side_effect=observed):
            journal.create_directory(target)
        if os.name != "nt":
            self.assertTrue({root, root / "parent", target}.issubset(set(synced)))
        self.assertTrue(target.is_dir())
        (target / "retained").write_bytes(b"source")
        with self.assertRaises(FileExistsError):
            journal.create_directory(target)
        self.assertEqual((target / "retained").read_bytes(), b"source")

    def test_create_json_preserves_existing_record_and_returns_exact_byte_digest(self):
        from cavi_mcp_core.journal import create_json

        path = self.root / "registration.json"
        digest = create_json(path, {"b": 2, "a": 1})
        expected = b'{"a":1,"b":2}\n'
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
        with self.assertRaises(FileExistsError):
            create_json(path, {"a": 99})
        self.assertEqual(path.read_bytes(), expected)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_create_json_rejects_invalid_json_before_creating_a_record(self):
        from cavi_mcp_core.journal import create_json

        path = self.root / "invalid.json"
        with self.assertRaises(ValueError):
            create_json(path, {"value": float("nan")})
        self.assertFalse(path.exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_write_json_replaces_atomically_and_privately(self):
        path = self.root / "state.json"
        write_json(path, {"a": 1})
        write_json(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 2})
        if os.name != "nt":
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
        self.assertEqual(
            json.loads(path.read_text()),
            {"prior_operation": {"operation_id": "op"}, "resolution": "abandoned"},
        )
        with self.assertRaisesRegex(ValueError, "different outcome"):
            archive(path, {"operation_id": "op"}, {"resolution": "completed"})


if __name__ == "__main__":
    unittest.main()
