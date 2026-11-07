"""Real Python children and a private one-attempt registration owner."""

import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cavi_mcp_core import proc


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Owner:
    def __init__(
        self,
        root,
        *,
        permit=True,
        corrupt=False,
        blocked=False,
        slow=False,
        disconnect=False,
        duplicate=False,
    ):
        self.root = root
        self.records = root / "records"
        self.records.mkdir(mode=0o700)
        self.endpoint = root / "owner.sock"
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.socket.listen(1)
        self.socket.settimeout(0.1)
        self.stop = threading.Event()
        self.config = root / "owner.json"
        self.config.write_bytes(
            encoded(
                {
                    "version": 1,
                    "attempt": "unit-attempt",
                    "socket": str(self.endpoint),
                    "records": str(self.records),
                    "token": "test-capability",
                }
            )
        )
        self.config.chmod(0o600)
        self.permit, self.corrupt, self.slow = permit, corrupt, slow
        self.disconnect = disconnect
        self.duplicate = duplicate
        self.received, self.allow = threading.Event(), threading.Event()
        self.request = None
        if not blocked:
            self.allow.set()
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        try:
            while not self.stop.is_set():
                try:
                    connection, _ = self.socket.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            with connection, connection.makefile("rb") as stream:
                frame = json.loads(stream.readline(65537))
                assert frame["token"] == "test-capability"
                self.request = frame["request"]
                self.received.set()
                if self.disconnect:
                    return
                if not self.allow.wait(5):
                    return
                proof = encoded({"request": self.request, "birth_identity": "recorded-birth"})
                record = self.records / "registered.json"
                with record.open("xb") as output:
                    record.chmod(0o600)
                    output.write(proof)
                    output.flush()
                    os.fsync(output.fileno())
                response = {
                    "launch_permitted": self.permit,
                    "request_sha256": hashlib.sha256(encoded(self.request)).hexdigest(),
                    "record": "registered.json",
                    "record_sha256": "0" * 64
                    if self.corrupt
                    else hashlib.sha256(proof).hexdigest(),
                }
                wire = encoded(response)
                if self.duplicate:
                    wire = b'{"launch_permitted":false,' + wire[1:]
                if self.slow:
                    for value in wire:
                        connection.sendall(bytes([value]))
                        time.sleep(0.2)
                else:
                    connection.sendall(wire)
        except OSError, ValueError:
            pass

    def env(self):
        return dict(os.environ, CAVI_MCP_PROCESS_OWNER=str(self.config))

    def close(self):
        self.allow.set()
        self.stop.set()
        self.socket.close()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise AssertionError("Registration fixture did not stop")


@unittest.skipUnless(os.name == "posix", "owned execution uses private Unix sockets")
class OwnedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def module(self):
        try:
            return importlib.import_module("cavi_mcp_core.owned_exec")
        except ImportError:
            self.fail("Owned subprocesses need registration before native exec")

    def owner(self, **kwargs):
        owner = Owner(self.root, **kwargs)
        self.addCleanup(owner.close)
        return owner

    def program(self):
        marker = self.root / "ran.json"
        code = (
            "import os,json,sys;from pathlib import Path;"
            'Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),"pgid":os.getpgrp()}))'
        )
        return marker, [sys.executable, "-c", code, str(marker)]

    def test_registration_is_durable_before_exec_and_pid_is_preserved(self):
        self.module()
        owner = self.owner(blocked=True)
        marker, command = self.program()
        result = []
        runner = threading.Thread(
            target=lambda: result.append(proc.run_sync(command, 15, env=owner.env()))
        )
        runner.start()
        try:
            self.assertTrue(owner.received.wait(5))
            self.assertFalse(marker.exists())
            owner.allow.set()
            runner.join(timeout=10)
            self.assertFalse(runner.is_alive())
            self.assertEqual(result[0].returncode, 0, result[0].stderr)
            observed = json.loads(marker.read_text())
            self.assertEqual(observed["pid"], owner.request["pid"])
            self.assertEqual(observed["pgid"], owner.request["pgid"])
            self.assertEqual(
                json.loads((owner.records / "registered.json").read_text())["request"],
                owner.request,
            )
            self.assertNotIn("test-capability", json.dumps(owner.request))
        finally:
            owner.allow.set()
            runner.join(timeout=10)

    def test_denied_or_changed_registration_never_executes(self):
        self.module()
        owner = self.owner(corrupt=True)
        marker, command = self.program()
        result = proc.run_sync(command, 10, env=owner.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertIn("registration", result.stderr.lower())

    def test_owner_denial_never_executes(self):
        self.module()
        owner = self.owner(permit=False)
        marker, command = self.program()
        result = proc.run_sync(command, 10, env=owner.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_child_environment_cannot_drop_a_configured_owner(self):
        gate = self.module()
        owner = self.owner()
        marker, command = self.program()
        from unittest.mock import patch

        with patch.dict(os.environ, owner.env(), clear=True), self.assertRaises(ValueError):
            gate.command(command, env={})
        self.assertFalse(marker.exists())

    def test_replaced_owner_configuration_cannot_redirect_a_prepared_command(self):
        gate = self.module()
        owner = self.owner()
        marker, command = self.program()
        wrapped = gate.command(command, env=owner.env())
        changed = json.loads(owner.config.read_text())
        changed["attempt"] = "different-attempt"
        owner.config.write_bytes(encoded(changed))
        result = subprocess.run(
            wrapped, env=owner.env(), capture_output=True, text=True, timeout=10, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_registration_has_an_absolute_deadline_even_with_partial_replies(self):
        self.module()
        owner = self.owner(slow=True)
        marker, command = self.program()
        started = time.monotonic()
        try:
            result = proc.run_sync(command, 8, env=owner.env())
        except TimeoutError:
            self.fail("Partial registration replies extended the gate beyond its startup deadline")
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 7)
        self.assertFalse(marker.exists())

    def test_other_subprocess_apis_receive_an_isolated_registered_group(self):
        gate = self.module()
        owner = self.owner()
        marker, command = self.program()
        result = subprocess.run(
            gate.command(command, env=owner.env()),
            env=owner.env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(marker.read_text())
        self.assertEqual(observed["pid"], observed["pgid"])
        self.assertEqual(owner.request["pgid"], observed["pid"])

    def test_lost_owner_before_authorization_never_executes(self):
        self.module()
        owner = self.owner(disconnect=True)
        marker, command = self.program()
        result = proc.run_sync(command, 10, env=owner.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_contradictory_duplicate_fields_cannot_authorize_exec(self):
        self.module()
        owner = self.owner(duplicate=True)
        marker, command = self.program()
        result = proc.run_sync(command, 10, env=owner.env())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
