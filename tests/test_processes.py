"""Neutral subprocesses exercise the supervisor's OS identity boundary."""

import contextlib
import os
import signal
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(
    sys.platform == "darwin" or sys.platform.startswith("linux"),
    "requires Linux or macOS process ownership",
)
class ProcessTests(unittest.TestCase):
    def test_inventory_failure_does_not_abandon_other_checked_groups(self):
        self.failed_inventory(OSError("inventory unavailable"))

    def test_inventory_timeout_does_not_abandon_other_checked_groups(self):
        self.failed_inventory(subprocess.TimeoutExpired("ps", 3))

    def failed_inventory(self, failure):
        from unittest.mock import patch

        from cavi_mcp_core import processes

        children = [
            subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
            )
            for _ in range(2)
        ]
        try:
            rows = {child.pid: processes.inspect(child.pid) for child in children}
            group = processes.group

            def uncertain(pgid):
                if pgid == children[0].pid:
                    raise failure
                return group(pgid)

            with (
                patch.object(processes, "group", side_effect=uncertain),
                self.assertRaisesRegex((OSError, RuntimeError), "inventory|timed out"),
            ):
                processes.cleanup(
                    {pid: r["birth"] for pid, r in rows.items()}, known=rows, seconds=0.1
                )
            children[1].wait(timeout=2)
            self.assertIsNotNone(children[1].returncode)
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_identity_memory_and_cleanup_of_only_the_owned_group(self):
        from cavi_mcp_core import processes

        with (
            subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
            ) as owned,
            subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
            ) as unrelated,
        ):
            try:
                row = processes.inspect(owned.pid)
                self.assertEqual((row["pid"], row["pgid"]), (owned.pid, owned.pid))
                self.assertGreaterEqual(row["rss_kib"], 0)
                birth = row["birth"]
                self.assertEqual(processes.inspect(owned.pid)["birth"], birth)
                processes.cleanup({owned.pid: birth}, seconds=1)
                owned.wait(timeout=3)
                self.assertIsNone(processes.inspect(owned.pid))
                self.assertIsNone(unrelated.poll())
            finally:
                for child in (owned, unrelated):
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()

    def test_changed_birth_refuses_to_signal(self):
        from cavi_mcp_core import processes

        with subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
        ) as child:
            try:
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    processes.cleanup({child.pid: "different-birth"}, seconds=0.1)
                self.assertIsNone(child.poll())
            finally:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    def test_orphan_group_is_retired_after_leader_exits(self):
        from cavi_mcp_core import processes

        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,sys;"
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"],'
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL);"
                    "print(p.pid,flush=True);sys.stdin.read(1)"
                ),
            ],
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        helper = int(child.stdout.readline())
        try:
            birth = processes.inspect(child.pid)["birth"]
            observed = processes.group(child.pid)
            self.assertIn(helper, observed)
            child.communicate("x", timeout=3)
            processes.cleanup({child.pid: birth}, known=observed, seconds=1)
            deadline = time.monotonic() + 3
            while processes.inspect(helper) is not None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(processes.inspect(helper))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            child.stdin.close()
            child.stdout.close()
