"""Only the registered root may close helpers through its authoritative owner."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cavi_mcp_core import journal, owned_exec, processes, supervision


class RetirementTests(unittest.TestCase):
    def fixture(self):
        from cavi_mcp_core import retirement

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        config = root / "owner.json"
        journal.create_json(config, {"fixture": True})
        pid = os.getpid()
        owner = SimpleNamespace(
            directory=root,
            config=config,
            attempt="attempt",
            root_pid=pid,
            ids={"worker"},
            groups={pid: "root", 12345: "native"},
            known={pid: {"pid": pid, "pgid": pid, "birth": "root"}},
            retiring=False,
            retirement=None,
        )
        request = {
            "kind": "helper-retirement",
            "version": 1,
            "attempt": "attempt",
            "pid": pid,
            "birth": "root",
            "owner_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        }
        return retirement, owner, request

    def test_same_request_returns_one_durable_cleanup_proof(self):
        api, owner, request = self.fixture()
        root = owner.known[owner.root_pid]
        with (
            patch.object(processes, "inspect", return_value=root),
            patch.object(processes, "members", return_value={owner.root_pid: root}),
            patch.object(processes, "cleanup") as cleanup,
        ):
            first = api._retire(owner, request)
            self.assertEqual(first, api._retire(owner, request))
        cleanup.assert_called_once()
        self.assertNotIn(owner.root_pid, cleanup.call_args.args[0])
        self.assertTrue(owner.retiring)
        raw = (owner.directory / "retirement.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), first["record_sha256"])
        self.assertIsNone(json.loads(raw)["cleanup_error"])

    def test_only_the_registered_root_identity_can_retire(self):
        api, owner, request = self.fixture()
        for field, value in [
            ("pid", 12345),
            ("birth", "reused"),
            ("attempt", "foreign"),
            ("owner_sha256", "wrong"),
        ]:
            with (
                self.subTest(field=field),
                patch.object(processes, "inspect", return_value=owner.known[owner.root_pid]),
                patch.object(processes, "cleanup") as cleanup,
                self.assertRaises(ValueError),
            ):
                api._retire(owner, {**request, field: value})
            cleanup.assert_not_called()
        self.assertFalse((owner.directory / "retirement.json").exists())

    def test_observed_identity_keeps_cleanup_uncertain_even_outside_native_groups(self):
        api, owner, request = self.fixture()
        pid = 23456
        row = {"pid": pid, "pgid": pid, "birth": "escaped"}
        owner.known[pid] = {**row, "pgid": owner.root_pid}
        with (
            patch.object(
                processes,
                "inspect",
                side_effect=lambda p: owner.known[owner.root_pid] if p == owner.root_pid else row,
            ),
            patch.object(processes, "members", side_effect=ValueError("uncertain census")),
            patch.object(processes, "cleanup") as cleanup,
        ):
            ack = api._retire(owner, request)
        cleanup.assert_called_once()
        proof = json.loads((owner.directory / ack["record"]).read_bytes())
        self.assertIn("uncertain census", proof["cleanup_error"])
        self.assertIn(str(pid), proof["cleanup_error"])

    def test_failed_publication_never_allows_a_replayed_clear_ack(self):
        api, owner, request = self.fixture()
        root = owner.known[owner.root_pid]
        with (
            patch.object(processes, "inspect", return_value=root),
            patch.object(processes, "members", return_value={owner.root_pid: root}),
            patch.object(processes, "cleanup"),
            patch.object(journal, "create_json", side_effect=OSError("disk failure")),
        ):
            with self.assertRaises(OSError):
                api._retire(owner, request)
            with self.assertRaisesRegex(ValueError, "durable receipt"):
                api._retire(owner, request)
        self.assertIsNone(owner.retirement)

    def test_client_checks_exact_ack_record_and_owner_binding(self):
        api, owner, request = self.fixture()
        root = owner.known[owner.root_pid]
        with (
            patch.object(processes, "inspect", return_value=root),
            patch.object(processes, "members", return_value={owner.root_pid: root}),
            patch.object(processes, "cleanup"),
        ):
            ack = api._retire(owner, request)
        config = {"records": str(owner.directory / "records")}
        with (
            patch.dict(os.environ, {owned_exec.OWNER_ENV: str(owner.config)}),
            patch.object(
                owned_exec, "configuration", return_value=(config, request["owner_sha256"])
            ),
        ):
            self.assertIsNone(
                api._verify(config, request["owner_sha256"], request, ack)["cleanup_error"]
            )
            for key, value in (
                ("record", "../foreign.json"),
                ("record_sha256", "wrong"),
                ("request_sha256", "foreign"),
                ("kind", "other"),
            ):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    api._verify(config, request["owner_sha256"], request, {**ack, key: value})
            with (
                patch.object(owned_exec, "configuration", return_value=(config, "changed")),
                self.assertRaisesRegex(ValueError, "owner changed"),
            ):
                api._verify(config, request["owner_sha256"], request, ack)

    def test_empty_or_timed_out_census_errors_cannot_be_clear_receipts(self):
        import subprocess

        for error in (OSError(), subprocess.TimeoutExpired("ps", 1)):
            api, owner, request = self.fixture()
            with (
                self.subTest(error=error),
                patch.object(processes, "inspect", return_value=owner.known[owner.root_pid]),
                patch.object(processes, "members", side_effect=error),
                patch.object(processes, "cleanup") as cleanup,
            ):
                ack = api._retire(owner, request)
            cleanup.assert_called_once()
            self.assertTrue(
                json.loads((owner.directory / ack["record"]).read_bytes())["cleanup_error"]
            )

    def test_invalid_client_budget_refuses_before_owner_lookup(self):
        from cavi_mcp_core import retirement

        for value in (0, -1, True, float("inf"), float("nan"), 16):
            with (
                self.subTest(value=value),
                patch.object(owned_exec, "configuration") as lookup,
                self.assertRaises(ValueError),
            ):
                retirement.close(timeout=value)
            lookup.assert_not_called()

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"), "POSIX owner required"
    )
    def test_real_worker_closes_helpers_retries_and_refuses_late_native_exec(self):
        from cavi_mcp_core import retirement

        self.assertTrue(callable(retirement.close))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            source = """import json,os,subprocess,sys,time
from pathlib import Path
from cavi_mcp_core import owned_exec,processes,retirement
helper_code="import time;from pathlib import Path;Path('ready').touch();time.sleep(20)"
child=subprocess.Popen(owned_exec.command([sys.executable,'-c',helper_code]),start_new_session=True)
until=time.monotonic()+5
while not Path('ready').exists() and time.monotonic()<until:time.sleep(.01)
assert Path('ready').exists()
try:
    retirement.close()
except ValueError as exc:
    assert 'response' in str(exc),exc
else:
    raise AssertionError('Lost acknowledgment must not be treated as success')
first=retirement.close()
assert first['cleanup_error'] is None,first
assert processes.birth(child.pid) is None
child.wait(timeout=3)
assert retirement.close()==first
late_code="from pathlib import Path;Path('forbidden').touch()"
late_command=owned_exec.command([sys.executable,'-c',late_code])
late=subprocess.run(late_command,start_new_session=True,capture_output=True)
assert late.returncode==78,late
assert not Path('forbidden').exists()
Path('proof.json').write_text(json.dumps(first))
"""
            import socket

            send = socket.socket.sendall
            dropped = []

            def sendall(channel, data, *args):
                frame = owned_exec.parse(data)
                if frame.get("kind") == "helper-retirement" and not dropped:
                    self.assertTrue((root / "owner/retirement.json").is_file())
                    dropped.append(True)
                    raise BrokenPipeError("reply lost after durable publication")
                return send(channel, data, *args)

            with patch.object(socket.socket, "sendall", sendall):
                result = supervision.run(
                    [sys.executable, "-c", source],
                    cwd=root,
                    env={},
                    directory=root / "owner",
                    max_seconds=10,
                    max_rss_kib=262144,
                    minimum_available_kib=0,
                )
            self.assertEqual(dropped, [True])
            self.assertEqual(result["exit_code"], 0, result)
            self.assertIsNone(result["cleanup_error"], result)
            proof = json.loads((root / "proof.json").read_text())
            self.assertEqual(Path(proof["path"]), root / "owner/retirement.json")
            self.assertEqual(
                proof["sha256"], hashlib.sha256(Path(proof["path"]).read_bytes()).hexdigest()
            )
