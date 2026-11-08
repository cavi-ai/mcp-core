"""Own registered helper groups; admission and workload policy belong to the caller."""

import hmac
import math
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from . import owned_exec as gate
from . import processes
from .journal import create_directory, create_json


class _Owner:
    def __init__(self, directory, command, cwd, environment, minimum_available_kib=0):
        with ExitStack() as resources:
            self.directory = Path(directory).resolve()
            self.minimum_available_kib = minimum_available_kib
            create_directory(self.directory)
            self.records = self.directory / "records"
            create_directory(self.records)
            # Only the disposable IPC endpoint uses a short OS temporary path.
            # All operation state and recovery records stay in the caller-selected directory.
            self.ipc = Path(tempfile.mkdtemp(prefix="cavi-", dir="/tmp")).resolve()
            resources.callback(shutil.rmtree, self.ipc)
            self.channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            resources.callback(self.channel.close)
            self.channel.bind(str(self.ipc / "owner.sock"))
            os.chmod(self.ipc / "owner.sock", 0o600)
            self.channel.listen(16)
            self.channel.settimeout(0.03)
            self.token, self.attempt = secrets.token_hex(32), secrets.token_hex(16)
            self.config = self.directory / "owner.json"
            create_json(
                self.config,
                {
                    "version": 1,
                    "attempt": self.attempt,
                    "token": self.token,
                    "socket": str(self.ipc / "owner.sock"),
                    "records": str(self.records),
                },
            )
            self.env = dict(environment)
            # Python may coerce an absent or C locale before the gate can hash it.
            # Normalize only that startup field; unrelated ambient values stay out.
            if not self.env.get("LC_ALL") and self.env.get("LC_CTYPE") in (None, "", "C", "POSIX"):
                self.env["LC_CTYPE"] = "UTF-8" if sys.platform == "darwin" else "C.UTF-8"
            if sys.platform == "darwin":
                self.env.setdefault(
                    "__CF_USER_TEXT_ENCODING",
                    os.environ.get("__CF_USER_TEXT_ENCODING", f"0x{os.getuid():X}:0x0:0x0"),
                )
            self.env[gate.OWNER_ENV] = str(self.config)
            self.command, self.cwd = list(map(str, command)), str(Path(cwd).resolve())
            self.groups, self.known, self.ids = {}, {}, set()
            self.root_pid = None
            self.deadline = math.inf
            self._close = resources.pop_all().close

    def record(self, request, row):
        name = request["launch_id"] + ".json"
        record = {"request": request, "birth_identity": row["birth"]}
        digest = create_json(self.records / name, record)
        return {
            "launch_permitted": True,
            "request_sha256": gate.digest(request),
            "record": name,
            "record_sha256": digest,
        }

    def register(self, request):
        fields = {
            "kind",
            "version",
            "attempt",
            "launch_id",
            "pid",
            "pgid",
            "parent_pid",
            "command",
            "cwd",
            "environment_sha256",
        }
        if (
            not isinstance(request, dict)
            or set(request) != fields
            or request["kind"] != "owned-process-registration"
            or type(request["version"]) is not int
            or request["version"] != 1
            or request["attempt"] != self.attempt
            or not isinstance(request["launch_id"], str)
            or not gate.IDENTIFIER.fullmatch(request["launch_id"])
            or request["launch_id"] in self.ids
            or any(
                type(request[k]) is not int or request[k] <= 0
                for k in ("pid", "pgid", "parent_pid")
            )
            or not isinstance(request["command"], list)
            or not request["command"]
            or any(not isinstance(arg, str) or "\0" in arg for arg in request["command"])
        ):
            raise ValueError("Invalid or replayed process registration")
        row = processes.inspect(request["pid"])
        if (
            row is None
            or (row["pid"], row["pgid"], row["parent_pid"])
            != (request["pid"], request["pgid"], request["parent_pid"])
            or row["pid"] != row["pgid"]
        ):
            raise ValueError("Process registration identity differs from the operating system")
        if row["pid"] == self.root_pid:
            if (
                request["launch_id"] != "worker"
                or request["command"] != self.command
                or request["cwd"] != self.cwd
                or row["parent_pid"] != os.getpid()
                or request["environment_sha256"] != gate.digest(self.env)
            ):
                raise ValueError("Root worker differs from its sealed launch")
        else:
            ancestor, seen = row["parent_pid"], {row["pid"]}
            while ancestor not in seen:
                seen.add(ancestor)
                parent = processes.inspect(ancestor)
                if parent is None:
                    break
                if self.groups.get(parent["pgid"]) is not None:
                    checked = self.known.get(parent["pid"])
                    if checked is not None and checked["birth"] == parent["birth"]:
                        break
                ancestor = parent["parent_pid"]
            else:
                parent = None
            if parent is None or self.known.get(parent["pid"], {}).get("birth") != parent["birth"]:
                raise ValueError("Helper has no checked registered ancestor")
        self.ids.add(request["launch_id"])
        self.groups[row["pid"]] = row["birth"]
        self.known[row["pid"]] = row
        if (
            time.monotonic() >= self.deadline
            or self.minimum_available_kib
            and processes.available_kib() < self.minimum_available_kib
        ):
            raise RuntimeError("Registration exceeds the native deadline or host memory headroom")
        reply = self.record(request, row)
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Native deadline expired before registration acknowledgment")
        return reply

    def receive(self):
        try:
            channel, _ = self.channel.accept()
        except TimeoutError:
            return
        with channel:
            deadline = min(self.deadline, time.monotonic() + 0.25)
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= gate.LIMIT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Absolute process registration frame deadline exceeded")
                channel.settimeout(remaining)
                chunk = channel.recv(min(4096, gate.LIMIT + 1 - len(raw)))
                if not chunk:
                    raise ValueError("Incomplete process registration")
                raw.extend(chunk)
            if len(raw) > gate.LIMIT or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
                raise ValueError("Invalid process registration frame")
            frame = gate.parse(raw)
            if (
                not isinstance(frame, dict)
                or set(frame) != {"token", "request"}
                or not isinstance(frame["token"], str)
                or not hmac.compare_digest(frame["token"], self.token)
            ):
                raise ValueError("Process registration owner differs")
            if sys.platform == "darwin":
                # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2, pid_t=int32.
                peer = struct.unpack("=i", channel.getsockopt(0, 2, 4))[0]
            elif sys.platform.startswith("linux"):
                peer, uid, _gid = struct.unpack(
                    "=iii", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    raise ValueError("Process registration peer belongs to another user")
            else:
                raise ValueError("Process registration peer identity is unavailable")
            if not isinstance(frame["request"], dict) or frame["request"].get("pid") != peer:
                raise ValueError("Process registration differs from its socket peer")
            channel.sendall(gate.encoded(self.register(frame["request"])))

    def close(self):
        self._close()


def _validate_limits(max_seconds, max_rss_kib, minimum_available_kib):
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not max_seconds > 0
        or type(max_rss_kib) is not int
        or max_rss_kib <= 0
        or type(minimum_available_kib) is not int
        or minimum_available_kib < 0
    ):
        raise ValueError("Process supervision requires explicit finite time and memory limits")


def run(
    command, *, cwd, env, directory, max_seconds, max_rss_kib, minimum_available_kib, cancel=None
):
    """Supervise a command from an unowned resident process using caller-selected limits.

    Admission and the resident process's lifetime belong to the caller. Release a
    resource only after checking the returned exit status and cleanup_error.
    """
    _validate_limits(max_seconds, max_rss_kib, minimum_available_kib)
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ValueError("Process supervision supports Linux and macOS")
    if os.environ.get(gate.OWNER_ENV) or env.get(gate.OWNER_ENV):
        raise ValueError("Reuse the configured process owner instead of replacing it")
    directory = Path(directory).absolute()
    if directory != directory.resolve():
        raise ValueError("Use a canonical process-owner directory without symbolic links")
    begun = time.monotonic()
    owner = _Owner(directory, command, cwd, env, minimum_available_kib)
    owner.deadline = begun + max_seconds
    child, error, cleanup_error, peak = None, None, None, 0
    try:
        if minimum_available_kib and processes.available_kib() < minimum_available_kib:
            raise MemoryError("Process launch requires the selected host memory headroom")
        if cancel is not None and Path(cancel).exists():
            raise RuntimeError("Process execution cancelled before launch")
        argv = gate.command(command, cwd=cwd, env=owner.env, launch_id="worker")
        with (owner.directory / "worker.log").open("xb") as log:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                env=owner.env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            owner.root_pid = child.pid
            identity = processes.birth(child.pid)
            if identity is not None and os.getpgid(child.pid) == child.pid:
                owner.groups[child.pid] = identity
                owner.known[child.pid] = {"pid": child.pid, "birth": identity, "pgid": child.pid}
            row = processes.inspect(child.pid)
            if row is None:
                raise RuntimeError("Worker exited before its identity was recorded")
            owner.groups[child.pid] = row["birth"]
            owner.known[child.pid] = row
            while child.poll() is None:
                if time.monotonic() - begun >= max_seconds:
                    raise TimeoutError("Native worker deadline exceeded")
                if cancel is not None and Path(cancel).exists():
                    raise RuntimeError("Native worker cancelled")
                owner.receive()
                rows = processes.members(owner.groups, owner.known)
                owner.known.update(rows)
                rss = sum(row["rss_kib"] for row in rows.values())
                peak = max(peak, rss)
                if rss > max_rss_kib:
                    raise MemoryError("Native worker memory budget exceeded")
                if minimum_available_kib and processes.available_kib() < minimum_available_kib:
                    raise MemoryError("Native worker exhausted host memory headroom")
            if "worker" not in owner.ids:
                raise RuntimeError("Worker exited without launch registration")
    except BaseException as exc:  # noqa: BLE001 - preserve uncertain ownership after cancellation
        error = str(exc) or type(exc).__name__
    finally:
        # Stop registration before cleanup. A late helper cannot receive permission.
        try:
            owner.close()
        except BaseException as exc:  # noqa: BLE001 - endpoint failure cannot skip process retirement
            cleanup_error = str(exc) or type(exc).__name__
        try:
            processes.cleanup(owner.groups, known=owner.known)
        except BaseException as exc:  # noqa: BLE001 - uncertainty keeps the broker reservation
            cleanup_error = str(exc) or type(exc).__name__
        if child is not None:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                cleanup_error = (
                    cleanup_error + "; " if cleanup_error else ""
                ) + "Worker exit remains unresolved"
    return {
        "pid": child.pid if child else None,
        "exit_code": child.returncode if child else None,
        "error": error,
        "cleanup_error": cleanup_error,
        "peak_rss_kib": peak,
        "seconds": round(time.monotonic() - begun, 3),
        "registered_groups": owner.groups,
    }
