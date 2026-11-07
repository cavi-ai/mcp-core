import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launcher import write_launcher

from cavi_mcp_core import apps, proc
from cavi_mcp_core.contracts import OperationRejected

POSIX = os.name != "nt"
MACOS = sys.platform == "darwin"
posix_only = unittest.skipUnless(POSIX, "needs pgrep and POSIX process names")
macos_only = unittest.skipUnless(MACOS, "needs macOS bundles and ps -o comm paths")
NEVER_RUN = AssertionError("ran the application")
OVERRIDES = ("CAVI_TOOLS_DIR", "CAVI_TOOLS_APP", "CAVI_THING_BIN", "CAVI_THING_APP", "CAVI_APP")


def platform(name):
    return patch.object(apps.sys, "platform", name)


def ere(pattern):
    """A pgrep pattern in Python's spelling of the POSIX space class."""
    return pattern.replace("[[:space:]]", r"\s")


class Scratch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in OVERRIDES:
            os.environ.pop(name, None)


class DirectoryTests(Scratch):
    def find(self, default):
        return apps.find_directory(default, "render", "CAVI_TOOLS_DIR", "CAVI_TOOLS_APP")

    def make_tools(self, folder):
        write_launcher(folder, "render", "print('x')\n")
        return folder

    def test_environment_override_comes_first_and_is_final(self):
        wrong = self.root / "wrong"
        os.environ["CAVI_TOOLS_DIR"] = str(wrong)
        default = self.make_tools(self.root / "default")
        on_path = self.make_tools(self.root / "bin")
        with patch.dict(os.environ, {"PATH": str(on_path)}):
            self.assertEqual(self.find(default), (wrong, "CAVI_TOOLS_DIR"))

    def test_app_override_uses_the_macos_directory_of_a_bundle(self):
        app = self.root / "Tools.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        os.environ["CAVI_TOOLS_APP"] = str(app)
        self.assertEqual(self.find(self.root / "none"), (app / "Contents/MacOS", "CAVI_TOOLS_APP"))

    def test_app_override_may_be_a_plain_directory(self):
        os.environ["CAVI_TOOLS_APP"] = str(self.root)
        self.assertEqual(self.find(self.root / "none"), (self.root, "CAVI_TOOLS_APP"))

    def test_dir_override_beats_app(self):
        os.environ["CAVI_TOOLS_DIR"] = "/x"
        os.environ["CAVI_TOOLS_APP"] = "/y"
        self.assertEqual(self.find(self.root / "none").path, Path("/x"))

    def test_default_location_comes_before_path(self):
        default = self.make_tools(self.root / "default")
        on_path = self.make_tools(self.root / "bin")
        with patch.dict(os.environ, {"PATH": str(on_path)}):
            self.assertEqual(self.find(default), (default, "default"))

    def test_path_is_the_last_resort(self):
        on_path = self.make_tools(self.root / "bin")
        with patch.dict(os.environ, {"PATH": str(on_path)}):
            found = self.find(self.root / "none")
        self.assertEqual(found, (on_path.resolve(), "PATH"))

    def test_nothing_found_names_the_default(self):
        with patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            self.assertEqual(self.find(self.root / "none"), (self.root / "none", "default"))

    def test_a_tool_is_found_through_the_platforms_executable_suffixes(self):
        folder = self.make_tools(self.root / "tools")
        self.assertEqual(apps.tool(folder, "render"),
                         Path(folder / ("render.cmd" if os.name == "nt" else "render")))
        self.assertIsNone(apps.tool(folder, "missing"))
        self.assertIsNone(apps.tool(self.root / "no-such-dir", "render"))

    @posix_only
    def test_a_tool_must_be_executable(self):
        folder = self.root / "tools"
        folder.mkdir()
        (folder / "render").write_text("#!/bin/sh\n")
        self.assertIsNone(apps.tool(folder, "render"))


class ExecutableTests(Scratch):
    def find(self, default):
        return apps.find_executable(default, "Thing", "CAVI_THING_BIN", "CAVI_THING_APP")

    def program(self, folder, name="Thing"):
        return write_launcher(self.root / folder, name, "print('x')\n")

    def test_bin_override_is_an_exact_executable(self):
        program = self.program("a")
        os.environ["CAVI_THING_BIN"] = str(program)
        self.assertEqual(self.find(self.root / "none"), (program, "CAVI_THING_BIN"))

    def test_bin_override_may_name_the_install_directory(self):
        if os.name == "nt":
            self.skipTest("the Windows launcher name ends in .cmd")
        program = self.program("a")
        os.environ["CAVI_THING_BIN"] = str(program.parent)
        self.assertEqual(self.find(self.root / "a" / "Thing"), (program, "CAVI_THING_BIN"))

    def test_a_missing_bin_override_is_reported_not_replaced(self):
        default = self.program("default")
        os.environ["CAVI_THING_BIN"] = str(self.root / "gone")
        self.assertEqual(self.find(default), (None, "CAVI_THING_BIN"))

    def test_app_override_reads_the_executable_name_from_the_bundle(self):
        app = self.root / "Fake Thing.app"
        macos = app / "Contents/MacOS"
        macos.mkdir(parents=True)
        info = plistlib.dumps({"CFBundleExecutable": "Custom Name"})
        (app / "Contents/Info.plist").write_bytes(info)
        (macos / "Custom Name").write_text("x")
        os.environ["CAVI_THING_APP"] = str(app)
        self.assertEqual(self.find(self.root / "none"), (macos / "Custom Name", "CAVI_THING_APP"))

    def test_app_override_falls_back_to_the_bundle_name_without_a_plist(self):
        app = self.root / "Fake Thing.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/Fake Thing").write_text("x")
        os.environ["CAVI_THING_APP"] = str(app)
        self.assertEqual(self.find(self.root / "none").path, app / "Contents/MacOS/Fake Thing")

    def test_app_override_may_be_the_executable_itself(self):
        program = self.program("a")
        os.environ["CAVI_THING_APP"] = str(program)
        self.assertEqual(self.find(self.root / "none"), (program, "CAVI_THING_APP"))

    def test_default_is_used_when_it_exists(self):
        program = self.program("default")
        self.assertEqual(self.find(program), (program, "default"))

    def test_a_default_bundle_resolves_to_its_executable(self):
        app = self.root / "Default.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/Default").write_text("x")
        self.assertEqual(self.find(app).path, app / "Contents/MacOS/Default")

    def test_path_lookup_is_after_the_default(self):
        program = self.program("bin")
        with patch.dict(os.environ, {"PATH": str(program.parent)}):
            found = self.find(self.root / "none")
        self.assertEqual(found.source, "PATH")
        # Windows spells the suffix as PATHEXT does (.CMD); its paths compare case-blind.
        self.assertEqual(Path(found.path), program)

    def test_nothing_found(self):
        with patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            self.assertEqual(self.find(self.root / "none"), (None, "default"))

    def test_without_a_bin_override_the_app_override_is_used(self):
        app = self.root / "Thing.app"
        app.mkdir()
        os.environ["CAVI_THING_APP"] = str(app)
        found = apps.find_executable(self.root / "none", "Thing", None, "CAVI_THING_APP")
        self.assertEqual(found.source, "CAVI_THING_APP")


class VersionTests(Scratch):
    def bundle(self, info):
        app = self.root / "Thing.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        if info is not None:
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        executable = app / "Contents/MacOS/Thing"
        executable.write_text("x")
        return executable

    def test_bundle_version_comes_from_the_plist_without_running_anything(self):
        executable = self.bundle({"CFBundleShortVersionString": "6.0.4", "CFBundleVersion": "9"})
        with platform("darwin"), patch.object(proc, "run_sync", side_effect=NEVER_RUN):
            self.assertEqual(apps.version(executable), "6.0.4")

    def test_bundle_version_falls_back_to_the_build_number(self):
        executable = self.bundle({"CFBundleVersion": "9"})
        with platform("darwin"):
            self.assertEqual(apps.version(executable), "9")

    def test_an_unreadable_plist_is_unknown_and_the_application_is_not_run(self):
        executable = self.bundle(None)
        with platform("darwin"), patch.object(proc, "run_sync", side_effect=NEVER_RUN):
            self.assertIsNone(apps.version(executable))

    def test_other_platforms_ask_the_program(self):
        app = self.root / "Thing.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        info = plistlib.dumps({"CFBundleShortVersionString": "plist"})
        (app / "Contents/Info.plist").write_bytes(info)
        program = write_launcher(app / "Contents/MacOS", "Thing",
                                 "print('version 1.2.3 (build 9)')\n")
        with platform("linux"):
            self.assertEqual(apps.version(program), "1.2.3")

    def test_a_program_outside_any_bundle_is_asked_even_on_macos(self):
        program = write_launcher(self.root, "tool", "print('7.8.9-fake')\n")
        with platform("darwin"):
            self.assertEqual(apps.version(program), "7.8.9-fake")

    def test_the_output_formats_tools_use(self):
        for text, expected in (
            ("version 16.0.7 (build 123 commit 0x1)", "16.0.7"),
            ("Designer Version 16.0.7", "16.0.7"),
            ("0.0.0-fake", "0.0.0-fake"),
            ("Sampler 6.0.4\n", "6.0.4"),
            ("no digits here", None),
            ("", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(apps.parse_version(text), expected)

    def test_the_probe_runs_once_per_path_and_mtime(self):
        program = write_launcher(self.root, "tool", "print('1.0.0')\n")
        calls = []
        real = proc.run_sync

        def counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        with patch.object(proc, "run_sync", counting):
            self.assertEqual(apps.version(program), "1.0.0")
            self.assertEqual(apps.version(program), "1.0.0")
            self.assertEqual(len(calls), 1)
            later = program.stat().st_mtime + 10
            os.utime(program, (later, later))
            apps.version(program)
            self.assertEqual(len(calls), 2)

    def test_a_failed_probe_is_unknown_and_not_repeated(self):
        program = write_launcher(self.root, "tool", "import sys\nsys.exit(3)\n")
        with patch.object(proc, "run_sync", side_effect=TimeoutError("slow")) as probe:
            self.assertIsNone(apps.version(program))
            self.assertIsNone(apps.version(program))
        self.assertEqual(probe.call_count, 1)

    def test_a_program_that_never_answers_is_given_up_on_at_the_timeout(self):
        program = write_launcher(self.root, "hang", "import time\ntime.sleep(60)\n")
        self.assertLessEqual(apps.VERSION_TIMEOUT, 5)
        with patch.object(apps, "VERSION_TIMEOUT", 0.3):
            started = time.monotonic()
            self.assertIsNone(apps.version(program))
            self.assertLess(time.monotonic() - started, 5)

    def test_the_caller_can_shorten_the_timeout(self):
        program = write_launcher(self.root, "hang", "import time\ntime.sleep(60)\n")
        started = time.monotonic()
        self.assertIsNone(apps.version(program, timeout=0.3))
        self.assertLess(time.monotonic() - started, 4)

    def test_a_timeout_cannot_be_raised_above_the_default(self):
        program = write_launcher(self.root, "hang", "import time\ntime.sleep(60)\n")
        with patch.object(apps, "VERSION_TIMEOUT", 0.3):
            started = time.monotonic()
            self.assertIsNone(apps.version(program, timeout=600))
            self.assertLess(time.monotonic() - started, 5)

    def test_a_probe_cut_short_by_the_caller_is_not_remembered(self):
        program = write_launcher(self.root, "late", "print('2.0.0')\n")
        with patch.object(proc, "run_sync", side_effect=[
            TimeoutError("caller deadline"), proc.Result(0,"2.0.0\n","")
        ]):
            self.assertIsNone(apps.version(program, timeout=0.2))
            self.assertEqual(apps.version(program), "2.0.0")

    def test_a_probe_that_reaches_the_default_timeout_is_remembered(self):
        program = write_launcher(self.root, "hang", "import time\ntime.sleep(60)\n")
        with patch.object(proc, "run_sync", side_effect=TimeoutError("slow")) as probe:
            self.assertIsNone(apps.version(program, timeout=apps.VERSION_TIMEOUT))
            self.assertIsNone(apps.version(program))
        self.assertEqual(probe.call_count, 1)

    def test_a_missing_file_has_no_version(self):
        self.assertIsNone(apps.version(self.root / "nothing"))
        self.assertIsNone(apps.version(None))


class PatternTests(unittest.TestCase):
    def test_the_pattern_matches_a_binary_only_as_the_first_argument(self):
        path = Path("/Applications/Thing/Thing.app/Contents/MacOS/Thing")
        pattern = ere(apps.pattern_for(path))
        self.assertTrue(re.search(pattern, f"{path} --skip-version-check"))
        self.assertTrue(re.search(pattern, str(path)))
        self.assertFalse(re.search(pattern, f"/usr/bin/python {path}"))
        self.assertFalse(re.search(pattern, f"AdobeIPCBroker -launchedbyvulcan {path}"))
        self.assertFalse(re.search(pattern, f"licensing --appPath={path}"))
        self.assertFalse(re.search(pattern, f"{path}-helper"))

    def test_the_pattern_matches_a_script_as_a_whole_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_launcher(Path(folder), "Thing", "print(1)\n", windows=False)
            pattern = ere(apps.pattern_for(path))
            self.assertTrue(re.search(pattern, f"/usr/bin/python {path} --idle"))
            self.assertTrue(re.search(pattern, f"{path} --idle"))
            self.assertFalse(re.search(pattern, f"licensing --appPath={path}"))

    def test_regex_metacharacters_in_a_path_are_literal(self):
        path = Path("/opt/a+b (x)/[app]/tool.v1")
        pattern = ere(apps.pattern_for(path))
        self.assertTrue(re.search(pattern, f"{path} run"))
        self.assertFalse(re.search(pattern, "/opt/aab (x)/[app]/toolxv1 run"))

    def test_the_resolved_form_of_a_symlinked_path_matches_too(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder).resolve() / "real"
            real.write_text("x")
            link = Path(folder) / "link"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("no symbolic links")
            pattern = ere(apps.pattern_for(link))
            self.assertTrue(re.search(pattern, f"{real} arg"))
            self.assertTrue(re.search(pattern, f"{link} arg"))


@posix_only
class RunningPosixTests(Scratch):
    def hold(self, program, *args):
        child = subprocess.Popen([str(program), *args])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def idle(self, name="Thing"):
        return write_launcher(self.root / "app", name, "import time\ntime.sleep(60)\n")

    def wait_for(self, program, expected, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if apps.running(program) is expected:
                return True
            time.sleep(0.1)
        return apps.running(program) is expected

    def test_a_running_program_is_seen_and_a_stopped_one_is_not(self):
        program = self.idle()
        self.assertFalse(apps.running(program))
        child = self.hold(program, "--idle")
        self.assertTrue(self.wait_for(program, True))
        child.kill()
        child.wait()
        self.assertTrue(self.wait_for(program, False))

    def test_a_path_with_spaces_and_brackets_is_matched(self):
        program = write_launcher(self.root / "A (B) [C]", "Thing", "import time\ntime.sleep(60)\n")
        self.hold(program)
        self.assertTrue(self.wait_for(program, True))

    def test_the_licensing_client_does_not_count(self):
        program = self.idle()
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                   f"--appPath={program}"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        self.assertFalse(apps.running(program))

    def test_a_process_naming_a_binary_as_an_option_value_does_not_count(self):
        # A link, not a copy: macOS kills copies of its own signed binaries.
        program = self.root / "app" / "Thing"
        program.parent.mkdir()
        program.symlink_to(shutil.which("sleep"))
        broker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                   "-launchedbyvulcan", str(program)])
        self.addCleanup(broker.wait)
        self.addCleanup(broker.kill)
        time.sleep(0.5)
        self.assertFalse(apps.running(program))
        self.hold(program, "60")
        self.assertTrue(self.wait_for(program, True))

    def test_a_symlinked_executable_is_seen_when_the_process_uses_the_real_path(self):
        program = self.idle()
        link = self.root / "linked"
        link.symlink_to(program)
        self.hold(program)
        self.assertTrue(self.wait_for(link, True))

    def test_a_symlinked_executable_is_seen_when_the_process_uses_the_link(self):
        program = self.idle()
        link = self.root / "linked"
        link.symlink_to(program)
        self.hold(link)
        self.assertTrue(self.wait_for(link, True))

    def test_no_executable_is_not_running(self):
        self.assertFalse(apps.running(None))

    def test_unknown_without_pgrep(self):
        with patch.object(apps.shutil, "which", return_value=None):
            self.assertIsNone(apps.running(Path("/x/y")))

    def test_unknown_when_pgrep_fails(self):
        for outcome in (subprocess.CompletedProcess([], 2), subprocess.CompletedProcess([], 3)):
            with patch.object(apps.subprocess, "run", return_value=outcome):
                self.assertIsNone(apps.running(Path("/x/y")))
        with patch.object(apps.subprocess, "run", side_effect=OSError("denied")):
            self.assertIsNone(apps.running(Path("/x/y")))
        timeout = subprocess.TimeoutExpired("pgrep", 10)
        with patch.object(apps.subprocess, "run", side_effect=timeout):
            self.assertIsNone(apps.running(Path("/x/y")))


class RunningWindowsTests(unittest.TestCase):
    """The tasklist reading, from listings in its CSV format."""

    @staticmethod
    def listing(text, code=0):
        return subprocess.CompletedProcess([], code, stdout=text, stderr="")

    def check(self, output, executable="/fake/Thing.exe"):
        listing = self.listing(output)
        with platform("win32"), patch.object(apps.subprocess, "run", return_value=listing):
            return apps.running(Path(executable))

    def test_a_listed_image_is_running(self):
        self.assertTrue(self.check('"Thing.exe","1234","Console","1","1 K"\n'))

    def test_the_image_name_comparison_ignores_case(self):
        self.assertTrue(self.check('"THING.EXE","1","Console","1","1 K"\n'))

    def test_no_match_is_not_running(self):
        self.assertFalse(self.check(
            "INFO: No tasks are running which match the specified criteria.\n"))

    def test_another_image_does_not_count(self):
        self.assertFalse(self.check('"notepad.exe","1","Console","1","1 K"\n'))

    def test_unknown_when_tasklist_fails(self):
        for failure in (OSError("missing"), subprocess.CalledProcessError(1, "tasklist"),
                        subprocess.TimeoutExpired("tasklist", 10)):
            with platform("win32"), patch.object(apps.subprocess, "run", side_effect=failure):
                self.assertIsNone(apps.running(Path("/fake/x.exe")))


class BundleTests(Scratch):
    IDENTIFIER = "com.example.thing"

    def app(self, folder, name, identifier=IDENTIFIER, version="1.0", executable=True):
        app = self.root / folder / f"{name}.app"
        macos = app / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": name,
            "CFBundleShortVersionString": version}))
        if executable:
            (macos / name).write_text("x")
        return app

    def find(self):
        return apps.bundle("Thing", self.IDENTIFIER, "Thing*/*.app", "CAVI_APP", self.root)

    def rejected(self, code):
        with self.assertRaises(OperationRejected) as caught:
            self.find()
        self.assertEqual(caught.exception.error_code, code)
        return str(caught.exception)

    def test_the_newest_installed_version_with_the_identifier_is_chosen(self):
        self.app("Thing 2024", "Thing", version="24.9.1")
        newest = self.app("Thing 2026", "Thing", version="26.1")
        self.app("Thing Beta", "Thing", identifier="com.example.other", version="99.0")
        found = self.find()
        self.assertEqual((found.path, found.executable, found.source),
                         (newest, newest / "Contents/MacOS/Thing", "default"))
        self.assertEqual(found.info["CFBundleShortVersionString"], "26.1")

    def test_an_override_is_final_even_outside_the_search_root(self):
        self.app("Thing 2026", "Thing", version="26.1")
        chosen = self.app("elsewhere", "Thing", version="20.0")
        os.environ["CAVI_APP"] = str(chosen)
        found = self.find()
        self.assertEqual((found.path, found.source), (chosen, "CAVI_APP"))

    def test_a_path_argument_beats_the_override(self):
        chosen = self.app("argument", "Thing", version="21.0")
        os.environ["CAVI_APP"] = str(self.app("elsewhere", "Thing", version="26.0"))
        found = apps.bundle("Thing", self.IDENTIFIER, "Thing*/*.app", "CAVI_APP", self.root,
                            path=chosen)
        self.assertEqual((found.path, found.source), (chosen, "argument"))

    def test_a_path_argument_naming_another_application_is_a_mismatch(self):
        other = self.app("argument", "Other", identifier="com.example.other")
        with self.assertRaises(apps.AppMismatch):
            apps.bundle("Thing", self.IDENTIFIER, "Thing*/*.app", "CAVI_APP", self.root, path=other)

    def test_an_override_naming_another_application_is_a_mismatch(self):
        other = self.app("elsewhere", "Other", identifier="com.example.other")
        self.app("Thing 2026", "Thing")
        os.environ["CAVI_APP"] = str(other)
        message = self.rejected("APP_IDENTITY_MISMATCH")
        self.assertIn("CAVI_APP", message)
        self.assertIn(str(other), message)

    def test_an_override_that_is_not_an_application_is_unavailable(self):
        os.environ["CAVI_APP"] = str(self.root / "missing.app")
        self.assertIn("CAVI_APP", self.rejected("APP_UNAVAILABLE"))

    def test_nothing_installed_names_the_override_to_set(self):
        self.app("Thing 2026", "Thing", identifier="com.example.other")
        self.assertIn("CAVI_APP", self.rejected("APP_UNAVAILABLE"))

    def test_without_an_override_variable_the_hint_is_left_out(self):
        os.environ["CAVI_APP"] = str(self.app("elsewhere", "Thing"))
        with self.assertRaises(apps.AppUnavailable) as caught:
            apps.bundle("Thing", self.IDENTIFIER, "Thing*/*.app", None, self.root / "none")
        self.assertNotIn("set", str(caught.exception))

    def test_a_bundle_without_its_executable_is_unavailable(self):
        self.app("Thing 2026", "Thing", executable=False)
        self.assertIn("no executable", self.rejected("APP_UNAVAILABLE"))

    def test_a_missing_search_root_is_unavailable(self):
        with self.assertRaises(apps.AppUnavailable):
            apps.bundle("Thing", self.IDENTIFIER, "Thing*/*.app", "CAVI_APP", self.root / "none")


@macos_only
class ProcessTests(unittest.TestCase):
    def test_a_running_executable_is_listed_with_its_pid_and_start(self):
        sleep = Path(shutil.which("sleep")).resolve()
        child = subprocess.Popen([str(sleep), "30"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 10
        while not (found := apps.processes(sleep)) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIn(child.pid, [process.pid for process in found])
        self.assertTrue(all(process.started for process in found))

    def test_an_executable_that_is_not_running_has_no_processes(self):
        self.assertEqual(apps.processes(Path("/nonexistent/Thing.app/Contents/MacOS/Thing")), [])

    def test_a_failed_listing_is_an_error(self):
        with patch.object(proc, "run_sync", return_value=proc.Result(1, "", "denied")), \
                self.assertRaisesRegex(RuntimeError, "ps failed"):
            apps.processes(Path("/bin/sleep"))


if __name__ == "__main__":
    unittest.main()
