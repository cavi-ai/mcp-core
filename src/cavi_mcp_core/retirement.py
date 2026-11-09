"""Attempt-bound helper cleanup requested by the registered root worker."""

import hashlib
import math
import os
import socket
import subprocess
import time
from pathlib import Path

from . import journal, processes
from . import owned_exec as gate

KIND = "helper-retirement"
RECORD = "retirement.json"


def _retire(owner, request):
    fields = {"kind", "version", "attempt", "pid", "birth", "owner_sha256"}
    if (
        not isinstance(request, dict)
        or set(request) != fields
        or request["kind"] != KIND
        or type(request["version"]) is not int
        or request["version"] != 1
        or request["attempt"] != owner.attempt
        or type(request["pid"]) is not int
        or request["pid"] != owner.root_pid
        or "worker" not in owner.ids
        or request["owner_sha256"] != hashlib.sha256(gate.read_private(owner.config)).hexdigest()
    ):
        raise ValueError("Helper retirement differs from the registered root attempt")
    row = processes.inspect(owner.root_pid)
    if (
        row is None
        or row["pgid"] != owner.root_pid
        or row["birth"] != owner.known[owner.root_pid]["birth"]
        or request["birth"] != row["birth"]
    ):
        raise ValueError("Helper retirement root identity changed")
    if owner.retirement is not None:
        if owner.retirement["request_sha256"] != gate.digest(request):
            raise ValueError("Helper retirement request changed")
        return owner.retirement
    if owner.retiring:
        raise ValueError("Helper retirement has no durable receipt; retain admission")
    owner.retiring = True
    errors = []
    try:
        owner.known.update(processes.members(owner.groups, owner.known))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        errors.append(str(exc) or type(exc).__name__)
    groups = {pid: birth for pid, birth in owner.groups.items() if pid != owner.root_pid}
    try:
        processes.cleanup(groups, known=owner.known)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        errors.append(str(exc) or type(exc).__name__)
    # Include identities known only to the owner, including root-group children
    # and members that left a group. Never adopt or signal their new groups.
    for pid, previous in owner.known.items():
        if pid == owner.root_pid:
            continue
        try:
            row = processes.inspect(pid)
            if row is not None and row["birth"] == previous["birth"]:
                errors.append(
                    f"Observed helper {pid} ({row['birth']}) remains alive in group {row['pgid']}"
                )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(str(exc) or type(exc).__name__)
    record = {
        "version": 1,
        "request": request,
        "groups": {str(pid): birth for pid, birth in groups.items()},
        "cleanup_error": "; ".join(errors) if errors else None,
    }
    if len(gate.encoded(record)) > gate.LIMIT:
        raise ValueError("Helper retirement receipt exceeds 64 KiB; retain admission")
    digest = journal.create_json(owner.directory / RECORD, record)
    owner.retirement = {
        "kind": KIND,
        "request_sha256": gate.digest(request),
        "record": RECORD,
        "record_sha256": digest,
    }
    return owner.retirement


def _verify(owner, owner_sha, request, reply):
    if (
        not isinstance(reply, dict)
        or set(reply) != {"kind", "request_sha256", "record", "record_sha256"}
        or reply["kind"] != KIND
        or reply["request_sha256"] != gate.digest(request)
        or reply["record"] != RECORD
    ):
        raise ValueError("Helper retirement acknowledgment differs from its request")
    path = Path(owner["records"]).parent / RECORD
    raw = gate.read_private(path)
    record = gate.parse(raw)
    if (
        hashlib.sha256(raw).hexdigest() != reply["record_sha256"]
        or not isinstance(record, dict)
        or set(record) != {"version", "request", "groups", "cleanup_error"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["request"] != request
        or not isinstance(record["groups"], dict)
        or any(
            not key.isdecimal() or int(key) <= 0 or not isinstance(value, str) or not value
            for key, value in record["groups"].items()
        )
        or record["cleanup_error"] is not None
        and (not isinstance(record["cleanup_error"], str) or not record["cleanup_error"])
    ):
        raise ValueError("Helper retirement receipt differs from its acknowledgment")
    if gate.configuration(os.environ[gate.OWNER_ENV])[1] != owner_sha:
        raise ValueError("Process owner changed during helper retirement")
    return {"path": str(path), "sha256": reply["record_sha256"], **record}


def close(*, timeout=15):
    """Close helper registration and return durable cleanup proof; only the root may call.

    A non-null cleanup_error or a failed/missing response requires retaining
    admission. The root remains alive for report publication. Repeating the same
    request returns the same receipt and never starts another cleanup.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 15:
        raise ValueError("Use a finite helper retirement timeout greater than zero and at most 15")
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError(
                "Helper retirement timed out; retain admission and retry its receipt"
            )
        return value

    path = os.environ.get(gate.OWNER_ENV)
    if not path:
        raise ValueError("Helper retirement requires the configured Core root worker")
    owner, owner_sha = gate.configuration(path)
    request = {
        "kind": KIND,
        "version": 1,
        "attempt": owner["attempt"],
        "pid": os.getpid(),
        "birth": processes.birth(os.getpid()),
        "owner_sha256": owner_sha,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(remaining())
        channel.connect(owner["socket"])
        channel.settimeout(remaining())
        channel.sendall(gate.encoded({"token": owner["token"], "request": request}))
        raw = bytearray()
        while b"\n" not in raw:
            channel.settimeout(remaining())
            chunk = channel.recv(min(4096, gate.LIMIT + 1 - len(raw)))
            if not chunk or len(raw) + len(chunk) > gate.LIMIT:
                raise ValueError("Invalid bounded helper retirement response; retain admission")
            raw.extend(chunk)
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ValueError("Invalid helper retirement response framing")
    result = _verify(owner, owner_sha, request, gate.parse(raw))
    remaining()
    return result
