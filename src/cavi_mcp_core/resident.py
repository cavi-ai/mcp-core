"""Caller-independent ownership for bounded, registered POSIX commands."""

import hashlib
import math
import os
import select
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import journal, processes, supervision
from . import owned_exec as gate

_children = {}
_HANDLE = {"version", "attempt", "directory", "pid", "birth", "request_sha256"}
_REQUEST = {
    "version",
    "attempt",
    "command",
    "cwd",
    "environment",
    "deadline",
    "max_rss_kib",
    "minimum_available_kib",
}


def _directory(value):
    path = Path(value).absolute()
    if path != path.resolve():
        raise ValueError("Use a canonical resident directory without symbolic links")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Resident state must be private and owned by this user")
    return path


def _request(directory, digest):
    raw = gate.read_private(directory / "request.json")
    value = gate.parse(raw)
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or not isinstance(value, dict)
        or set(value) != _REQUEST
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("Resident request differs from its sealed input")
    return value


def _handle(directory):
    value = gate.parse(gate.read_private(directory / "handle.json"))
    if (
        not isinstance(value, dict)
        or set(value) != _HANDLE
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["directory"] != str(directory)
        or type(value["pid"]) is not int
        or value["pid"] <= 0
        or not isinstance(value["birth"], str)
        or not value["birth"]
        or not isinstance(value["attempt"], str)
        or not gate.IDENTIFIER.fullmatch(value["attempt"])
        or not isinstance(value["request_sha256"], str)
    ):
        raise ValueError("Invalid resident handle")
    request = _request(directory, value["request_sha256"])
    if request["attempt"] != value["attempt"]:
        raise ValueError("Resident handle differs from its request attempt")
    return value, request


def _reap(attempt):
    child = _children.get(attempt)
    if child is not None and child.poll() is not None:
        del _children[attempt]


def start(command, *, cwd, env, directory, max_seconds, max_rss_kib, minimum_available_kib):
    """Start one independent owner; an existing directory never launches another command.

    The resident continues to its original deadline after caller loss. Retain the
    returned handle or directory to inspect, wait or cancel without relaunching.
    """
    supervision._validate_limits(max_seconds, max_rss_kib, minimum_available_kib)
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ValueError("Resident supervision supports Linux and macOS")
    if (
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        or not isinstance(env, dict)
        or any(
            not isinstance(k, str)
            or not k
            or "=" in k
            or "\0" in k
            or not isinstance(v, str)
            or "\0" in v
            for k, v in env.items()
        )
    ):
        raise ValueError("Use nonempty command arguments and an explicit string environment")
    if os.environ.get(gate.OWNER_ENV) or env.get(gate.OWNER_ENV):
        raise ValueError("Reuse the configured process owner instead of replacing it")
    cwd, directory = Path(cwd).absolute(), Path(directory).absolute()
    if cwd != cwd.resolve(strict=True) or not cwd.is_dir() or directory != directory.resolve():
        raise ValueError("Resident paths must be canonical directories without symbolic links")
    attempt = uuid.uuid4().hex
    request = {
        "version": 1,
        "attempt": attempt,
        "command": list(command),
        "cwd": str(cwd),
        "environment": dict(env),
        "deadline": time.monotonic() + max_seconds,
        "max_rss_kib": max_rss_kib,
        "minimum_available_kib": minimum_available_kib,
    }
    if len(gate.encoded(request)) > gate.LIMIT:
        raise ValueError("Resident request exceeds 64 KiB")
    journal.create_directory(directory)
    digest = journal.create_json(directory / "request.json", request)
    descriptor = os.open(directory / "resident.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        child = subprocess.Popen(
            [sys.executable, "-I", "-m", "cavi_mcp_core.resident", str(directory), digest],
            cwd=cwd,
            env={},
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    _children[attempt] = child
    try:
        row = processes.inspect(child.pid)
        if row is None or row["pgid"] != child.pid:
            raise RuntimeError("Resident exited before its identity was recorded")
        handle = {
            "version": 1,
            "attempt": attempt,
            "directory": str(directory),
            "pid": child.pid,
            "birth": row["birth"],
            "request_sha256": digest,
        }
        journal.create_json(directory / "handle.json", handle)
        child.stdin.write(gate.encoded(handle))
        child.stdin.flush()
    finally:
        child.stdin.close()
        _reap(attempt)
    return handle


def inspect(directory):
    """Read durable completion, or report running/unresolved using the checked birth identity."""
    directory = _directory(directory)
    handle, _request_value = _handle(directory)
    _reap(handle["attempt"])
    path = directory / "result.json"
    if path.exists() or path.is_symlink():
        result = gate.parse(gate.read_private(path))
        if (
            not isinstance(result, dict)
            or set(result)
            != {"version", "attempt", "request_sha256", "resident_pid", "resident_birth", "receipt"}
            or type(result["version"]) is not int
            or result["version"] != 1
            or type(result["resident_pid"]) is not int
            or result["resident_pid"] != handle["pid"]
            or result["resident_birth"] != handle["birth"]
            or result["attempt"] != handle["attempt"]
            or result["request_sha256"] != handle["request_sha256"]
            or not isinstance(result["receipt"], dict)
        ):
            raise ValueError("Resident result differs from its sealed attempt")
        receipt = result["receipt"]
        fields = {
            "pid",
            "exit_code",
            "error",
            "cleanup_error",
            "peak_rss_kib",
            "seconds",
            "registered_groups",
        }
        if (
            set(receipt) != fields
            or any(
                receipt[k] is not None and not isinstance(receipt[k], str)
                for k in ("error", "cleanup_error")
            )
            or receipt["exit_code"] is not None
            and type(receipt["exit_code"]) is not int
            or receipt["pid"] is not None
            and (type(receipt["pid"]) is not int or receipt["pid"] <= 0)
            or type(receipt["peak_rss_kib"]) is not int
            or receipt["peak_rss_kib"] < 0
            or not isinstance(receipt["registered_groups"], dict)
            or type(receipt["seconds"]) not in (int, float)
            or receipt["seconds"] < 0
        ):
            raise ValueError("Invalid resident cleanup receipt")
        return {
            "state": "unresolved" if receipt["cleanup_error"] else "finished",
            "receipt": receipt,
        }
    row = processes.inspect(handle["pid"])
    alive = row is not None and row["birth"] == handle["birth"]
    return {"state": "running" if alive else "unresolved", "receipt": None}


def wait(directory, *, timeout):
    """Wait within a separate observation deadline; timing out never cancels or relaunches."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("Use a positive finite observation timeout")
    deadline = time.monotonic() + timeout
    while True:
        value = inspect(directory)
        if value["state"] != "running":
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Resident observation timed out; retain its handle")
        time.sleep(min(0.05, remaining))


def cancel(directory):
    """Request cancellation once for this attempt; the resident owns checked cleanup."""
    directory = _directory(directory)
    handle, _request_value = _handle(directory)
    value = inspect(directory)
    if value["state"] != "running":
        return value
    cancellation = {"version": 1, "attempt": handle["attempt"]}
    try:
        journal.create_json(directory / "cancel.json", cancellation)
    except FileExistsError:
        if gate.parse(gate.read_private(directory / "cancel.json")) != cancellation:
            raise ValueError("Resident cancellation differs from its attempt") from None
    return inspect(directory)


def _permit(deadline):
    raw = bytearray()
    while b"\n" not in raw and len(raw) <= gate.LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise TimeoutError("Resident handle acknowledgment timed out")
        part = os.read(0, min(4096, gate.LIMIT + 1 - len(raw)))
        if not part:
            raise ValueError("Caller exited before durable resident handle acknowledgment")
        raw.extend(part)
    if len(raw) > gate.LIMIT or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        raise ValueError("Invalid resident handle acknowledgment")
    return gate.parse(raw)


def _publish(directory, value):
    temporary = directory / (".result-" + uuid.uuid4().hex + ".json")
    journal.create_json(temporary, value)
    try:
        os.link(temporary, directory / "result.json")
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink()


def _run(directory, digest):
    directory = _directory(directory)
    request = _request(directory, digest)
    row = processes.inspect(os.getpid())
    if row is None:
        raise RuntimeError("Resident identity is unavailable")

    def check_owner():
        if (directory / "handle.json").exists():
            handle, _sealed = _handle(directory)
            if handle["pid"] != os.getpid() or handle["birth"] != row["birth"]:
                raise ValueError("Resident result belongs to another process")

    check_owner()
    receipt = {
        "pid": None,
        "exit_code": None,
        "error": None,
        "cleanup_error": None,
        "peak_rss_kib": 0,
        "seconds": 0,
        "registered_groups": {},
    }
    try:
        permit = _permit(min(request["deadline"], time.monotonic() + 5))
        handle, sealed = _handle(directory)
        row = processes.inspect(os.getpid())
        if (
            permit != handle
            or sealed != request
            or handle["pid"] != os.getpid()
            or row is None
            or row["birth"] != handle["birth"]
        ):
            raise ValueError("Resident handle differs from this process")
        remaining = request["deadline"] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Resident deadline expired before worker launch")
        # Recheck exact request bytes before the registered worker can execute.
        _request(directory, digest)
        receipt = supervision.run(
            request["command"],
            cwd=request["cwd"],
            env=request["environment"],
            directory=directory / "processes",
            max_seconds=remaining,
            max_rss_kib=request["max_rss_kib"],
            minimum_available_kib=request["minimum_available_kib"],
            cancel=directory / "cancel.json",
        )
    except Exception as error:  # no worker exists until supervision.run owns cleanup
        receipt["error"] = str(error) or type(error).__name__
    check_owner()
    _publish(
        directory,
        {
            "version": 1,
            "attempt": request["attempt"],
            "request_sha256": digest,
            "resident_pid": os.getpid(),
            "resident_birth": row["birth"],
            "receipt": receipt,
        },
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Resident execution requires its sealed directory and request digest")
    raise SystemExit(_run(Path(sys.argv[1]), sys.argv[2]))
