import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cavi_mcp_core.files import digest, inside, load_json, parse_json, read_bytes


class InsideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "art").mkdir()

    def test_returns_the_resolved_path_inside_the_workspace(self):
        self.assertEqual(inside(self.root, "art/frame.png"), self.root / "art/frame.png")

    def test_refuses_traversal_escapes_and_the_root(self):
        outside = Path(self.temp.name).parent / "elsewhere"
        for value in ("../x", "art/../x", str(outside), ".", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inside(self.root, value)

    def test_refuses_a_symbolic_link_that_leaves_the_workspace(self):
        with tempfile.TemporaryDirectory() as other:
            os.symlink(other, self.root / "link")
            with self.assertRaisesRegex(ValueError, "inside the shared workspace"):
                inside(self.root, "link/file.png")

    def test_requires_an_existing_workspace(self):
        with self.assertRaises(FileNotFoundError):
            inside(self.root / "missing", "a.png")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_reads_a_bounded_regular_file(self):
        path = self.root / "a.bin"
        path.write_bytes(b"abc")
        self.assertEqual(read_bytes(path, 3), b"abc")
        self.assertEqual(digest(path), hashlib.sha256(b"abc").hexdigest())

    def test_digest_streams_past_the_default_bound_only_when_asked(self):
        path = self.root / "large.bin"
        size = 64 * 1024 * 1024 + 1
        with path.open("wb") as stream:
            stream.truncate(size)
        self.assertEqual(digest(path, limit=None), hashlib.sha256(bytes(size)).hexdigest())
        for limit in ((), (size - 1,)):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "bounded regular file"),
            ):
                digest(path, *limit)

    def test_digest_refuses_a_file_whose_status_changes_while_hashed(self):
        path = self.root / "a.bin"
        path.write_bytes(b"abc")
        os.chmod(path, 0o644)
        real = hashlib.file_digest

        def changed_during_read(stream, name):
            os.chmod(path, 0o600)
            return real(stream, name)

        with (
            mock.patch("hashlib.file_digest", changed_during_read),
            self.assertRaisesRegex(ValueError, "changed while reading"),
        ):
            digest(path)

    def test_refuses_oversized_files_and_directories(self):
        path = self.root / "a.bin"
        path.write_bytes(b"abcd")
        with self.assertRaisesRegex(ValueError, "bounded regular file"):
            read_bytes(path, 3)
        with self.assertRaises(OSError):
            read_bytes(self.root, 3)

    def test_json_refuses_non_finite_numbers(self):
        self.assertEqual(parse_json(b'{"a": 1.5}'), {"a": 1.5})
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'[-Infinity]'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Non-finite"):
                parse_json(raw)
        path = self.root / "a.json"
        path.write_text('{"ok": true}')
        self.assertEqual(load_json(path), {"ok": True})
        path.write_text(" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            load_json(path)


if __name__ == "__main__":
    unittest.main()
