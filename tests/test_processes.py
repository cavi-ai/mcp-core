"""Neutral subprocesses exercise the supervisor's OS identity boundary."""

import contextlib
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch


class KnownMemberTests(unittest.TestCase):
    def rows(self):
        root = {"pid": 101, "pgid": 101, "parent_pid": 1, "birth": "root", "rss_kib": 1}
        helper = {"pid": 202, "pgid": 101, "parent_pid": 101, "birth": "helper", "rss_kib": 2}
        return root, helper

    def test_missing_live_member_refuses_incomplete_group_census(self):
        from cavi_mcp_core import processes

        root, helper = self.rows()
        for current_group in (101, 202):
            current = {**helper, "pgid": current_group}
            with (
                self.subTest(pgid=current_group),
                patch.object(processes, "group", return_value={101: root}),
                patch.object(
                    processes,
                    "inspect",
                    side_effect=lambda pid, current=current: root if pid == 101 else current,
                ),
                self.assertRaisesRegex(ValueError, "observed process"),
            ):
                processes.members({101: "root"}, {101: root, 202: helper})

    def test_group_leader_exit_does_not_hide_a_live_observed_helper(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        with (
            patch.object(processes, "group", return_value={}),
            patch.object(
                processes,
                "inspect",
                side_effect=lambda pid: None if pid == 101 else {**helper, "pgid": 202},
            ),
            self.assertRaisesRegex(ValueError, "observed process"),
        ):
            processes.members({101: "root"}, {202: helper})

    def test_exited_or_reused_observed_pid_is_not_adopted(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        for current in (None, {**helper, "pgid": 202, "birth": "reused"}):
            with (
                self.subTest(current=current),
                patch.object(processes, "group", return_value={}),
                patch.object(
                    processes,
                    "inspect",
                    side_effect=lambda pid, current=current: None if pid == 101 else current,
                ),
            ):
                self.assertEqual(processes.members({101: "root"}, {202: helper}), {})

    def test_another_explicitly_registered_group_covers_the_member(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        current = {**helper, "pgid": 202}
        with (
            patch.object(
                processes, "group", side_effect=lambda pgid: {202: current} if pgid == 202 else {}
            ),
            patch.object(
                processes, "inspect", side_effect=lambda pid: current if pid == 202 else None
            ),
        ):
            self.assertEqual(
                processes.members({101: "root", 202: "helper"}, {202: helper}), {202: current}
            )

    def test_cleanup_honors_a_registration_after_an_earlier_group_observation(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        current = {**helper, "pgid": 202}
        live = {202: current}

        def inspect(pid):
            return live.get(pid)

        def group(pgid):
            return dict(live) if pgid == 202 else {}

        def stop(pgid, _signal):
            self.assertEqual(pgid, 202)
            live.clear()

        with (
            patch.object(processes, "inspect", side_effect=inspect),
            patch.object(processes, "group", side_effect=group),
            patch.object(processes.os, "killpg", side_effect=stop),
        ):
            processes.cleanup({101: "root", 202: "helper"}, known={202: helper}, seconds=0.01)
        self.assertEqual(live, {})
        self.assertEqual(helper["pgid"], 101)

    def test_cleanup_preserves_coverage_when_a_nonleader_changes_registered_groups(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        leader = {"pid": 303, "pgid": 303, "parent_pid": 1, "birth": "destination", "rss_kib": 1}
        live = {202: {**helper, "pgid": 303}, 303: leader}

        def group(pgid):
            return dict(live) if pgid == 303 else {}

        def stop(pgid, _signal):
            self.assertEqual(pgid, 303)
            live.clear()

        with (
            patch.object(processes, "inspect", side_effect=live.get),
            patch.object(processes, "group", side_effect=group),
            patch.object(processes.os, "killpg", side_effect=stop),
        ):
            processes.cleanup(
                {101: "root", 303: "destination"}, known={202: helper, 303: leader}, seconds=0.01
            )
        self.assertEqual(live, {})
        self.assertEqual(helper["pgid"], 101)

    def test_transfer_obligation_survives_source_group_retirement(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        leader = {"pid": 303, "pgid": 303, "parent_pid": 1, "birth": "destination", "rss_kib": 1}
        live = {202: {**helper, "pgid": 303}, 303: leader}
        transferred = False

        def group(pgid):
            nonlocal transferred
            if pgid == 303 and not transferred:
                transferred = True
                snapshot = dict(live)
                live.pop(303)
                live[202] = {**helper, "pgid": 404}
                return snapshot
            return {}

        with (
            patch.object(processes, "inspect", side_effect=live.get),
            patch.object(
                processes, "birth", side_effect=lambda pid: live.get(pid, {}).get("birth")
            ),
            patch.object(processes.os, "getpgid", side_effect=lambda pid: live[pid]["pgid"]),
            patch.object(processes, "group", side_effect=group),
            patch.object(processes.os, "killpg") as stop,
            self.assertRaisesRegex(RuntimeError, "observed process"),
        ):
            processes.cleanup(
                {101: "root", 303: "destination"}, known={202: helper, 303: leader}, seconds=0.01
            )
        stop.assert_not_called()

    def test_a_reused_destination_group_is_never_adopted(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()
        reused = {"pid": 303, "pgid": 303, "parent_pid": 1, "birth": "reused", "rss_kib": 1}
        live = {202: {**helper, "pgid": 303}, 303: reused}
        with (
            patch.object(processes, "inspect", side_effect=live.get),
            patch.object(
                processes, "birth", side_effect=lambda pid: live.get(pid, {}).get("birth")
            ),
            patch.object(processes.os, "getpgid", side_effect=lambda pid: live[pid]["pgid"]),
            patch.object(processes, "group", return_value={}),
            patch.object(processes.os, "killpg") as stop,
            self.assertRaisesRegex(RuntimeError, "identity"),
        ):
            processes.cleanup({101: "root", 303: "destination"}, known={202: helper}, seconds=0.01)
        stop.assert_not_called()

    def test_missing_identity_read_failure_remains_uncertain(self):
        from cavi_mcp_core import processes

        _root, helper = self.rows()

        def inspect(pid):
            if pid == 101:
                return None
            raise OSError("identity read unavailable")

        with (
            patch.object(processes, "group", return_value={}),
            patch.object(processes, "inspect", side_effect=inspect),
            self.assertRaisesRegex(OSError, "identity read unavailable"),
        ):
            processes.members({101: "root"}, {202: helper})

    def test_other_scope_known_identities_are_not_inspected(self):
        from cavi_mcp_core import processes

        root, helper = self.rows()

        def inspect(pid):
            if pid != 101:
                raise AssertionError("Unrelated scope must not be inspected")
            return root

        with (
            patch.object(processes, "group", return_value={101: root}),
            patch.object(processes, "inspect", side_effect=inspect),
        ):
            self.assertEqual(
                processes.members({101: "root"}, {202: {**helper, "pgid": 303}}), {101: root}
            )


@unittest.skipUnless(
    sys.platform == "darwin" or sys.platform.startswith("linux"),
    "requires Linux or macOS process ownership",
)
class ProcessTests(unittest.TestCase):
    def test_observed_helper_leaving_its_group_keeps_cleanup_unresolved(self):
        from cavi_mcp_core import processes

        helper_code = (
            "import os,sys,time;sys.stdin.read(1);os.setsid();"
            "print('escaped',flush=True);time.sleep(20)"
        )
        parent_code = (
            "import subprocess,sys;"
            f"child=subprocess.Popen([sys.executable,'-c',{helper_code!r}],"
            "stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True);"
            "print(child.pid,flush=True);sys.stdin.read(1);"
            "child.stdin.write('x');child.stdin.flush();"
            "print(child.stdout.readline().strip(),flush=True);sys.stdin.read(1)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        helper = int(parent.stdout.readline())
        observed = processes.group(parent.pid)
        self.assertIn(helper, observed)
        birth = observed[helper]["birth"]
        groups = {parent.pid: observed[parent.pid]["birth"]}
        try:
            parent.stdin.write("x")
            parent.stdin.flush()
            self.assertEqual(parent.stdout.readline().strip(), "escaped")
            self.assertEqual(processes.inspect(helper)["pgid"], helper)
            with self.assertRaisesRegex(RuntimeError, "observed process"):
                processes.cleanup(groups, known=observed, seconds=0.1)
            parent.wait(timeout=2)
            self.assertEqual(processes.birth(helper), birth)
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
            if processes.birth(helper) == birth:
                os.kill(helper, signal.SIGKILL)
            parent.wait(timeout=3)
            parent.stdin.close()
            parent.stdout.close()
            deadline = time.monotonic() + 3
            while processes.birth(helper) == birth and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(processes.birth(helper))

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
