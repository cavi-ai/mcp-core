"""Exercise real Core gates with neutral Python commands, never native apps."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

supported = unittest.skipUnless(
    sys.platform == "darwin" or sys.platform.startswith("linux"),
    "requires Linux or macOS process ownership",
)


class SupervisorTests(unittest.TestCase):
    @supported
    def test_minimal_environment_does_not_inherit_unrelated_ambient_values(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"UNRELATED_AMBIENT": "private"}),
        ):
            root = Path(folder).resolve()
            result = supervision.run(
                [sys.executable, "-c", "import os;assert 'UNRELATED_AMBIENT' not in os.environ"],
                cwd=root,
                env={},
                directory=root / "owner",
                max_seconds=5,
                max_rss_kib=100000,
                minimum_available_kib=0,
            )
            self.assertEqual(result["exit_code"], 0, result)
            self.assertIsNone(result["error"], result)
            self.assertIsNone(result["cleanup_error"], result)

    def test_existing_owner_cannot_be_replaced_by_a_new_supervisor(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve() / "owner"
            for ambient, environment in (
                ({}, {"CAVI_MCP_PROCESS_OWNER": "existing"}),
                ({"CAVI_MCP_PROCESS_OWNER": "existing"}, {}),
            ):
                with patch.dict(os.environ, ambient, clear=True), self.assertRaises(ValueError):
                    supervision.run(
                        ["unused"],
                        cwd=folder,
                        env=environment,
                        directory=directory,
                        max_seconds=3,
                        max_rss_kib=100000,
                        minimum_available_kib=0,
                    )
                self.assertFalse(directory.exists())

    def test_invalid_policy_refuses_before_creating_an_owner(self):
        from cavi_mcp_core import supervision

        invalid = (
            {"max_seconds": float("nan")},
            {"max_seconds": True},
            {"max_rss_kib": 0},
            {"minimum_available_kib": -1},
            {"minimum_available_kib": True},
        )
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve() / "unused"
            for change in invalid:
                policy = {
                    "max_seconds": 5,
                    "max_rss_kib": 1048576,
                    "minimum_available_kib": 0,
                    **change,
                }
                with self.subTest(change=change), self.assertRaises(ValueError):
                    supervision.run(["unused"], cwd=folder, env={}, directory=directory, **policy)
                self.assertFalse(directory.exists())

    @supported
    def test_early_cancellation_never_creates_a_child(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            cancel = root / "cancel"
            cancel.touch()
            with patch.object(
                supervision.subprocess, "Popen", side_effect=AssertionError("must not launch")
            ):
                result = supervision.run(
                    ["unused"],
                    cwd=root,
                    env={},
                    directory=root / "owner",
                    max_seconds=3,
                    max_rss_kib=100000,
                    minimum_available_kib=0,
                    cancel=cancel,
                )
            self.assertIsNone(result["pid"])
            self.assertIn("cancel", result["error"])

    def test_unsupported_platform_refuses_before_creating_state(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder).resolve() / "unused"
            with patch.object(supervision.sys, "platform", "win32"), self.assertRaises(ValueError):
                supervision.run(
                    ["unused"],
                    cwd=folder,
                    env={},
                    directory=directory,
                    max_seconds=3,
                    max_rss_kib=100000,
                    minimum_available_kib=0,
                )
            self.assertFalse(directory.exists())

    @supported
    def test_symbolic_output_parent_refuses_without_writing_elsewhere(self):
        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                supervision.run(
                    ["unused"],
                    cwd=root,
                    env={},
                    directory=link / "owner",
                    max_seconds=3,
                    max_rss_kib=100000,
                    minimum_available_kib=0,
                )
            self.assertEqual(list(target.iterdir()), [])

    @supported
    def test_dripped_frame_cannot_extend_the_operation_deadline(self):
        import socket

        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            owner = supervision._Owner(Path(folder).resolve() / "owner", ["unused"], folder, {})
            stop = threading.Event()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(str(owner.ipc / "owner.sock"))

                def drip():
                    for _ in range(5):
                        channel.sendall(b" ")
                        if stop.wait(0.08):
                            return

                thread = threading.Thread(target=drip)
                begun = time.monotonic()
                owner.deadline = begun + 0.1
                thread.start()
                try:
                    with self.assertRaises(TimeoutError):
                        owner.receive()
                    self.assertLess(time.monotonic() - begun, 0.3)
                finally:
                    stop.set()
                    thread.join()
                    owner.close()

    @supported
    def test_socket_peer_cannot_register_another_pid(self):
        import socket

        from cavi_mcp_core import owned_exec, supervision

        with tempfile.TemporaryDirectory() as folder:
            owner = supervision._Owner(Path(folder).resolve() / "owner", ["unused"], folder, {})
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                    channel.connect(str(owner.ipc / "owner.sock"))
                    channel.sendall(
                        owned_exec.encoded(
                            {"token": owner.token, "request": {"pid": os.getpid() + 1}}
                        )
                    )
                    with self.assertRaisesRegex(ValueError, "peer"):
                        owner.receive()
                self.assertEqual(list(owner.records.iterdir()), [])
            finally:
                owner.close()

    @supported
    def test_low_host_headroom_refuses_before_process_creation(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(supervision.processes, "available_kib", return_value=1),
            patch.object(
                supervision.subprocess, "Popen", side_effect=AssertionError("must not launch")
            ),
        ):
            result = supervision.run(
                ["unused"],
                cwd=folder,
                env=dict(os.environ),
                directory=Path(folder).resolve() / "owner",
                max_seconds=3,
                max_rss_kib=1048576,
                minimum_available_kib=2 * 1024 * 1024,
            )
        self.assertIn("headroom", result["error"])
        self.assertIsNone(result["pid"])

    def run_command(self, source, **limits):
        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            command = [sys.executable, "-c", source]
            receipt = supervision.run(
                command,
                cwd=root,
                env=dict(os.environ),
                directory=root / "owner",
                max_seconds=limits.get("seconds", 5),
                max_rss_kib=limits.get("memory", 1048576),
                minimum_available_kib=2 * 1024 * 1024,
            )
            records = [json.loads(p.read_text()) for p in (root / "owner/records").glob("*.json")]
            return receipt, records

    @supported
    def test_durable_registration_precedes_execution(self):
        result, records = self.run_command('print("executed")')
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["cleanup_error"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request"]["pid"], result["pid"])
        self.assertEqual(records[0]["request"]["pgid"], result["pid"])
        self.assertTrue(records[0]["birth_identity"])

    @supported
    def test_execution_limit_is_selected_by_the_consumer(self):
        result, _ = self.run_command("pass", seconds=3601)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIsNone(result["error"])

    @supported
    def test_timeout_retires_owned_process_before_return(self):
        from cavi_mcp_core import processes

        result, _ = self.run_command("import time;time.sleep(20)", seconds=0.2)
        self.assertIn("deadline", result["error"])
        self.assertIsNone(result["cleanup_error"])
        self.assertIsNone(processes.inspect(result["pid"]))

    @supported
    def test_memory_limit_retires_the_process(self):
        result, _ = self.run_command("import time;x=bytearray(20000000);time.sleep(20)", memory=1)
        self.assertIn("memory", result["error"])
        self.assertIsNone(result["cleanup_error"])

    @supported
    def test_registered_detached_helper_is_cleaned_after_root_exit(self):
        from cavi_mcp_core import processes

        source = (
            "import subprocess,sys,time;from cavi_mcp_core import owned_exec;"
            'subprocess.Popen(owned_exec.command([sys.executable,"-c",'
            '"import time;time.sleep(20)"]),start_new_session=True);time.sleep(.4)'
        )
        result, records = self.run_command(source)
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["cleanup_error"])
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIsNone(processes.inspect(record["request"]["pid"]))

    @supported
    def test_observed_group_escape_is_retained_in_supervisor_receipt(self):
        import signal
        from unittest.mock import patch

        from cavi_mcp_core import processes, supervision

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            gate, marker = root / "observed", root / "helper.json"
            helper_code = (
                "import os,sys,time;from pathlib import Path;"
                f"gate=Path({str(gate)!r});deadline=time.monotonic()+15;"
                "\nwhile not gate.exists() and time.monotonic()<deadline:time.sleep(.01)"
                "\nif not gate.exists():sys.exit(3)"
                "\nos.setsid();time.sleep(20)"
            )
            source = (
                "import json,subprocess,sys,time;from pathlib import Path;"
                "from cavi_mcp_core import processes;"
                f"helper=subprocess.Popen([sys.executable,'-c',{helper_code!r}]);"
                f"marker=Path({str(marker)!r});"
                "marker.with_suffix('.tmp').write_text(json.dumps({'pid':helper.pid,'birth':processes.birth(helper.pid)}));"
                "marker.with_suffix('.tmp').replace(marker);time.sleep(20)"
            )
            members, cleanup = processes.members, processes.cleanup
            observed = {}

            def census(groups, known):
                rows = members(groups, known)
                if marker.exists():
                    pid = json.loads(marker.read_text())["pid"]
                    if pid in rows and not gate.exists():
                        observed[pid] = rows[pid]
                        gate.touch()
                return rows

            try:
                with (
                    patch.object(processes, "members", side_effect=census),
                    patch.object(
                        processes,
                        "cleanup",
                        side_effect=lambda groups, **kwargs: cleanup(groups, seconds=0.1, **kwargs),
                    ),
                ):
                    result = supervision.run(
                        [sys.executable, "-c", source],
                        cwd=root,
                        env={},
                        directory=root / "owner",
                        max_seconds=5,
                        max_rss_kib=100000,
                        minimum_available_kib=0,
                    )
                self.assertEqual(len(observed), 1)
                self.assertIn("observed process", result["error"])
                self.assertIn("observed process", result["cleanup_error"])
                self.assertIsNone(processes.inspect(result["pid"]))
                pid, identity = next(iter(observed.items()))
                self.assertEqual(processes.inspect(pid)["birth"], identity["birth"])
                self.assertNotIn(pid, result["registered_groups"])
            finally:
                if marker.exists():
                    fixture = json.loads(marker.read_text())
                    pid = fixture["pid"]
                    row = processes.inspect(pid)
                    if row is not None:
                        self.assertEqual(row["birth"], fixture["birth"])
                        os.kill(pid, signal.SIGKILL)
                        deadline = time.monotonic() + 3
                        while processes.birth(pid) is not None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertIsNone(processes.birth(pid))

    @supported
    def test_failure_to_persist_registration_never_executes_target(self):
        from unittest.mock import patch

        from cavi_mcp_core import supervision

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            marker = root / "executed"
            with patch.object(supervision._Owner, "record", side_effect=OSError("disk refused")):
                result = supervision.run(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path;Path({str(marker)!r}).touch()",
                    ],
                    cwd=root,
                    env=dict(os.environ),
                    directory=root / "owner",
                    max_seconds=3,
                    max_rss_kib=1048576,
                    minimum_available_kib=2 * 1024 * 1024,
                )
            self.assertFalse(marker.exists())
            self.assertIn("disk refused", result["error"])
            self.assertIsNone(result["cleanup_error"])

    @supported
    def test_endpoint_cleanup_error_still_retires_owned_processes(self):
        from unittest.mock import patch

        from cavi_mcp_core import processes, supervision

        close = supervision._Owner.close

        def failed(owner):
            close(owner)
            raise OSError("endpoint cleanup failed")

        with patch.object(supervision._Owner, "close", failed):
            result, _ = self.run_command("import time;time.sleep(20)", seconds=0.2)
        self.assertIsNone(processes.inspect(result["pid"]))
        self.assertIn("endpoint cleanup failed", result["cleanup_error"])
