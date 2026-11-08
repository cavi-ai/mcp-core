"""Real caller-loss and reattachment checks with neutral Python processes."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

supported = unittest.skipUnless(
    sys.platform == "darwin" or sys.platform.startswith("linux"), "requires POSIX process ownership"
)


@supported
class ResidentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.directory = self.root / "resident"

    def start(self, code="print('finished')", **changes):
        from cavi_mcp_core import resident

        return resident.start(
            [sys.executable, "-c", code],
            cwd=self.root,
            env={},
            directory=self.directory,
            **{"max_seconds": 3, "max_rss_kib": 256 * 1024, "minimum_available_kib": 0, **changes},
        )

    def until(self, predicate):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("neutral process fixture did not reach its checkpoint")

    def assert_exited(self, pid):
        from cavi_mcp_core import processes

        self.until(lambda: processes.inspect(pid) is None)

    def test_result_reattaches_and_explicit_environment_stays_minimal(self):
        from cavi_mcp_core import resident

        with patch.dict(os.environ, {"UNRELATED_AMBIENT": "private"}):
            handle = self.start("import os;assert 'UNRELATED_AMBIENT' not in os.environ")
        self.assertEqual(
            set(handle), {"version", "attempt", "directory", "pid", "birth", "request_sha256"}
        )
        result = resident.wait(self.directory, timeout=10)
        self.assertEqual(result["state"], "finished", result)
        self.assertEqual(result["receipt"]["exit_code"], 0, result)
        self.assertIsNone(result["receipt"]["error"], result)
        self.assertIsNone(result["receipt"]["cleanup_error"], result)
        self.assertEqual(resident.inspect(self.directory), result)
        self.assertEqual(len(result["receipt"]["registered_groups"]), 1)
        self.assertEqual((self.directory / "request.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory / "result.json").stat().st_mode & 0o777, 0o600)

    def test_killing_caller_keeps_resident_deadline_and_registered_helper_cleanup(self):
        from cavi_mcp_core import resident

        marker = self.root / "helper.pid"
        helper = (
            "import os,time;from pathlib import Path;"
            f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        worker = (
            "from cavi_mcp_core import proc;import sys;"
            f"proc.run_sync([sys.executable,'-c',{helper!r}],timeout=60)"
        )
        caller = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from cavi_mcp_core import resident;import sys,json,time;from pathlib import Path;"
                f"h=resident.start([sys.executable,'-c',{worker!r}],cwd=Path({str(self.root)!r}),env={{}},"
                f"directory=Path({str(self.directory)!r}),max_seconds=2,max_rss_kib=256*1024,minimum_available_kib=0);"
                "print(json.dumps(h),flush=True);time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.until(marker.exists)
            handle = json.loads((self.directory / "handle.json").read_text())
            caller.kill()
            caller.wait(timeout=3)
            result = resident.wait(self.directory, timeout=10)
            self.assertEqual(result["state"], "finished", result)
            self.assertIn("deadline", result["receipt"]["error"])
            self.assertIsNone(result["receipt"]["cleanup_error"], result)
            self.assertGreaterEqual(len(result["receipt"]["registered_groups"]), 2)
            self.assert_exited(int(marker.read_text()))
            self.assert_exited(handle["pid"])
        finally:
            if caller.poll() is None:
                caller.kill()
                caller.wait(timeout=3)
            caller.stdout.close()
            if (self.directory / "handle.json").exists():
                resident.cancel(self.directory)
                resident.wait(self.directory, timeout=10)

    def test_cancellation_is_attempt_bound_idempotent_and_checked(self):
        from cavi_mcp_core import resident

        marker = self.root / "worker.pid"
        handle = self.start(
            "import os,time;from pathlib import Path;"
            f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        self.until(marker.exists)
        resident.cancel(self.directory)
        before = (self.directory / "cancel.json").read_bytes()
        resident.cancel(self.directory)
        self.assertEqual((self.directory / "cancel.json").read_bytes(), before)
        self.assertEqual(json.loads(before)["attempt"], handle["attempt"])
        result = resident.wait(self.directory, timeout=10)
        self.assertEqual(result["state"], "finished", result)
        self.assertIn("cancel", result["receipt"]["error"])
        self.assertIsNone(result["receipt"]["cleanup_error"])
        self.assert_exited(int(marker.read_text()))

    def test_duplicate_directory_and_unsafe_policy_refuse_before_launch(self):
        from cavi_mcp_core import resident

        for policy in (
            {"max_seconds": True},
            {"max_seconds": float("inf")},
            {"max_rss_kib": 0},
            {"minimum_available_kib": -1},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.start(**policy)
            self.assertFalse(self.directory.exists())
        self.start()
        with self.assertRaises(FileExistsError):
            self.start("raise RuntimeError('second worker must not launch')")
        self.assertEqual(resident.wait(self.directory, timeout=10)["receipt"]["exit_code"], 0)

    def test_changed_inputs_or_result_binding_cannot_be_reported_as_finished(self):
        from cavi_mcp_core import resident

        self.start()
        resident.wait(self.directory, timeout=10)
        result = self.directory / "result.json"
        value = json.loads(result.read_text())
        value["attempt"] = "different"
        result.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            resident.inspect(self.directory)
        request = self.directory / "request.json"
        request.write_bytes(request.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            resident.inspect(self.directory)

    def test_dead_or_reused_resident_without_result_remains_unresolved(self):
        from cavi_mcp_core import resident

        self.start()
        resident.wait(self.directory, timeout=10)
        (self.directory / "result.json").unlink()
        with patch.object(resident.processes, "inspect", return_value={"birth": "different"}):
            self.assertEqual(resident.inspect(self.directory)["state"], "unresolved")
        self.assertEqual(resident.inspect(self.directory)["state"], "unresolved")

    def test_observation_timeout_does_not_cancel_the_operation(self):
        from cavi_mcp_core import resident

        self.start("import time;time.sleep(60)")
        try:
            with self.assertRaises(TimeoutError):
                resident.wait(self.directory, timeout=0.01)
            self.assertEqual(resident.inspect(self.directory)["state"], "running")
            self.assertFalse((self.directory / "cancel.json").exists())
        finally:
            resident.cancel(self.directory)
            resident.wait(self.directory, timeout=10)

    def test_controller_loss_retains_unresolved_state_until_external_reconciliation(self):
        import signal

        from cavi_mcp_core import processes, resident

        marker = self.root / "worker.pid"
        handle = self.start(
            "import os,time;from pathlib import Path;"
            f"Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
        )
        self.until(marker.exists)
        worker = processes.inspect(int(marker.read_text()))
        self.assertIsNotNone(worker)
        try:
            os.kill(handle["pid"], signal.SIGKILL)
            self.until(lambda: resident.inspect(self.directory)["state"] == "unresolved")
            result = resident.wait(self.directory, timeout=1)
            self.assertIsNone(result["receipt"])
            self.assertEqual(result["state"], "unresolved")
        finally:
            # Only fixture-owned, birth-checked workers are retired after intentional owner loss.
            processes.cleanup({worker["pgid"]: worker["birth"]}, known={worker["pid"]: worker})
        self.assert_exited(worker["pid"])

    def test_replaying_the_resident_entrypoint_cannot_publish_another_owners_result(self):
        from cavi_mcp_core import resident

        handle = self.start("import time;time.sleep(60)")
        try:
            replay = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "cavi_mcp_core.resident",
                    str(self.directory),
                    handle["request_sha256"],
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
            )
            self.assertNotEqual(replay.returncode, 0)
            self.assertFalse((self.directory / "result.json").exists())
            self.assertEqual(resident.inspect(self.directory)["state"], "running")
        finally:
            # Also retire the real owner if the regression exposes a forged result.
            (self.directory / "cancel.json").touch(exist_ok=True)
            self.until(lambda: resident.processes.inspect(handle["pid"]) is None)

    def test_invalid_environment_and_unsupported_host_refuse_before_state(self):
        from cavi_mcp_core import resident

        for environment in (None, [], {"": "value"}, {"A=B": "value"}, {"KEY": 1}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                resident.start(
                    [sys.executable],
                    cwd=self.root,
                    env=environment,
                    directory=self.directory,
                    max_seconds=1,
                    max_rss_kib=100000,
                    minimum_available_kib=0,
                )
            self.assertFalse(self.directory.exists())
        with patch.object(resident.sys, "platform", "win32"), self.assertRaises(ValueError):
            self.start()
        self.assertFalse(self.directory.exists())

    def test_submission_death_before_handle_cannot_start_worker(self):
        marker = self.root / "effect"
        caller = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from cavi_mcp_core import resident;from pathlib import Path;import os,sys;"
                "original=resident.journal.create_json;"
                "\ndef create(path,value):\n if Path(path).name=='handle.json':os._exit(0)"
                "\n return original(path,value)\n"
                "resident.journal.create_json=create\n"
                "resident.start([sys.executable,'-c',"
                f'"from pathlib import Path;Path({str(marker)!r}).touch()"],'
                f"cwd=Path({str(self.root)!r}),env={{}},directory=Path({str(self.directory)!r}),"
                "max_seconds=.5,max_rss_kib=256*1024,minimum_available_kib=0)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, stderr = caller.communicate(timeout=5)
        self.assertEqual(caller.returncode, 0, stderr)
        self.until(lambda: (self.directory / "result.json").exists())
        self.assertFalse(marker.exists())
        self.assertFalse((self.directory / "handle.json").exists())
        receipt = json.loads((self.directory / "result.json").read_text())["receipt"]
        self.assertIsNone(receipt["pid"])
        self.assertIn("handle", receipt["error"])


if __name__ == "__main__":
    unittest.main()
