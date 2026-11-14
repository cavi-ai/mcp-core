import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cavi_mcp_core import workspace

ENV = "CAVI_TEST_WORKSPACE"


def can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as folder:
        try:
            (Path(folder) / "link").symlink_to(Path(folder), target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


needs_symlinks = unittest.skipUnless(can_symlink(), "symbolic links are not available")


class RootTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.default = self.base / "Documents" / "Workspace"
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(ENV, None)

    def set(self, value):
        os.environ[ENV] = str(value)

    def test_without_a_default_the_variable_is_required(self):
        with self.assertRaisesRegex(ValueError, f"Set {ENV} to the absolute workspace directory"):
            workspace.root(ENV)
        self.set("")
        with self.assertRaisesRegex(ValueError, f"Set {ENV} to the absolute workspace directory"):
            workspace.root(ENV)

    def test_the_default_applies_when_the_variable_is_unset_or_empty(self):
        self.assertEqual(workspace.root(ENV, default=self.default), self.default)
        self.set("")
        self.assertEqual(workspace.root(ENV, default=self.default), self.default)

    def test_a_relative_variable_is_refused(self):
        self.set("relative")
        for default in (None, self.default):
            with self.subTest(default=default), self.assertRaisesRegex(
                    ValueError, f"{ENV} must be an absolute path"):
                workspace.root(ENV, default=default)

    def test_the_variable_selects_the_root(self):
        chosen = self.base / "chosen"
        chosen.mkdir()
        self.set(chosen)
        for default in (None, self.default):
            with self.subTest(default=default):
                self.assertEqual(workspace.root(ENV, default=default), chosen)
                self.assertEqual(workspace.root(ENV, default=default, create=True), chosen)

    def test_the_argument_beats_the_variable(self):
        argument, variable = self.base / "argument", self.base / "variable"
        argument.mkdir()
        variable.mkdir()
        self.set(variable)
        self.assertEqual(workspace.root(ENV, argument), argument)

    @needs_symlinks
    def test_a_linked_root_resolves_to_its_target(self):
        target = self.base / "target"
        target.mkdir()
        (self.base / "link").symlink_to(target, target_is_directory=True)
        self.set(self.base / "link")
        self.assertEqual(workspace.root(ENV), target)

    def test_reading_never_creates_anything(self):
        workspace.root(ENV, default=self.default)
        self.set(self.base / "missing")
        self.assertEqual(workspace.root(ENV), self.base / "missing")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_the_default_is_created_on_demand(self):
        self.assertEqual(workspace.root(ENV, default=self.default, create=True), self.default)
        self.assertTrue(self.default.is_dir())
        self.assertEqual(workspace.root(ENV, default=self.default, create=True), self.default)

    def test_a_missing_variable_workspace_is_refused(self):
        missing = self.base / "missing"
        self.set(missing)
        with self.assertRaises(ValueError) as caught:
            workspace.root(ENV, default=self.default, create=True)
        message = str(caught.exception)
        for part in (ENV, str(missing), "does not exist", str(self.default)):
            self.assertIn(part, message)
        with self.assertRaisesRegex(ValueError, "does not exist. Create it first."):
            workspace.root(ENV, create=True)
        self.assertFalse(missing.exists())

    def test_a_missing_argument_workspace_is_refused(self):
        missing = self.base / "missing"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            workspace.root(ENV, missing, create=True)
        self.assertFalse(missing.exists())

    def test_a_file_is_not_a_workspace(self):
        occupied = self.base / "file"
        occupied.write_text("x")
        self.set(occupied)
        self.assertEqual(workspace.root(ENV), occupied)
        with self.assertRaisesRegex(ValueError, "not a directory"):
            workspace.root(ENV, create=True)
        with self.assertRaisesRegex(ValueError, "not a directory"):
            workspace.root(ENV, occupied, create=True)
        del os.environ[ENV]
        self.default.parent.mkdir()
        self.default.write_text("x")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            workspace.root(ENV, default=self.default, create=True)

    def test_existing_requires_a_directory(self):
        self.assertEqual(workspace.existing(self.base), self.base)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            workspace.existing(self.base / "missing")
        (self.base / "file").write_text("x")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            workspace.existing(self.base / "file")

    def test_existing_root_never_creates(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            workspace.existing_root(ENV, default=self.default)
        self.assertFalse(self.default.exists())
        self.default.mkdir(parents=True)
        self.assertEqual(workspace.existing_root(ENV, default=self.default), self.default)


class PathRuleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.root = self.base / "ws"
        self.outside = self.base / "outside"
        for path in (self.root / "maps", self.outside):
            path.mkdir(parents=True)
        (self.root / "maps/a.png").write_bytes(b"png")
        (self.root / "maps/B.PNG").write_bytes(b"png")
        (self.root / "maps/a.txt").write_bytes(b"txt")
        (self.outside / "secret.png").write_bytes(b"png")

    def test_input_inside_the_root(self):
        self.assertEqual(workspace.input_file(self.root, "maps/a.png", {".png"}),
                         self.root / "maps/a.png")
        self.assertEqual(workspace.input_file(self.root, "maps/../maps/a.png", {".png"}),
                         self.root / "maps/a.png")

    def test_suffix_match_ignores_case(self):
        self.assertEqual(workspace.input_file(self.root, "maps/B.PNG", {".png"}).name, "B.PNG")

    def test_input_refusals_share_one_message(self):
        for name in ("maps/a.txt", "maps", "maps/missing.png", "", "../outside/secret.png",
                     str(self.outside / "secret.png"), "maps/a\x00.png", None, 3,
                     ["maps/a.png"]):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "supported file inside the workspace"):
                workspace.input_file(self.root, name, {".png"})

    @needs_symlinks
    def test_input_symlinks_may_not_leave_the_root(self):
        (self.root / "escape.png").symlink_to(self.outside / "secret.png")
        (self.root / "via").symlink_to(self.outside, target_is_directory=True)
        (self.root / "inside.png").symlink_to(self.root / "maps/a.png")
        for name in ("escape.png", "via/secret.png"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "supported file inside the workspace"):
                workspace.input_file(self.root, name, {".png"})
        self.assertEqual(workspace.input_file(self.root, "inside.png", {".png"}),
                         self.root / "maps/a.png")

    def test_input_needs_an_existing_root(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            workspace.input_file(self.base / "gone", "a.png", {".png"})

    def test_new_output_directory(self):
        made = workspace.new_output_dir(self.root, "out")
        self.assertEqual(made, self.root / "out")
        self.assertTrue(made.is_dir())
        self.assertEqual(workspace.new_output_dir(self.root, "maps/sub"), self.root / "maps/sub")

    def test_existing_output_is_never_reused(self):
        (self.root / "used").mkdir()
        (self.root / "file").write_text("x")
        for name in ("used", "maps", "file"):
            with self.subTest(name=name), self.assertRaises(FileExistsError):
                workspace.new_output_dir(self.root, name)

    def test_output_must_be_a_new_directory_inside_the_root(self):
        for name in ("", ".", "..", "../escape", str(self.outside / "new"), "a\x00b", None):
            with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "inside the workspace"):
                workspace.new_output_dir(self.root, name)
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "secret.png"])

    def test_output_parent_must_exist_unless_requested(self):
        with self.assertRaisesRegex(ValueError, "parent directory must exist"):
            workspace.new_output_dir(self.root, "a/b/c")
        self.assertFalse((self.root / "a").exists())
        made = workspace.new_output_dir(self.root, "a/b/c", parents=True)
        self.assertEqual(made, self.root / "a/b/c")
        self.assertTrue(made.is_dir())

    @needs_symlinks
    def test_output_may_not_leave_the_root_through_a_link(self):
        (self.root / "via").symlink_to(self.outside, target_is_directory=True)
        for parents in (False, True):
            with self.subTest(parents=parents), self.assertRaisesRegex(
                    ValueError, "inside the workspace"):
                workspace.new_output_dir(self.root, "via/new", parents=parents)
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "secret.png"])

    def test_output_path_validates_without_creating(self):
        self.assertEqual(workspace.output_path(self.root, "later"), self.root / "later")
        self.assertFalse((self.root / "later").exists())

    def test_rel_is_posix_and_confined(self):
        self.assertEqual(workspace.rel(self.root, self.root / "maps" / "a.png"), "maps/a.png")
        with self.assertRaises(ValueError):
            workspace.rel(self.root, self.outside / "secret.png")


if __name__ == "__main__":
    unittest.main()
