"""An opt-in registration gate before a supervised POSIX command executes.

This file also runs standalone so embedded hosts can use a suitable Python
interpreter without importing the server stack in the child.
"""

import base64
import hashlib
import json
import os
import re
import socket
import stat
import sys
import time
import uuid
from pathlib import Path

OWNER_ENV = "CAVI_MCP_PROCESS_OWNER"
LIMIT = 65536
TIMEOUT = 5
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate owned execution field")
        result[key] = value
    return result


def parse(raw):
    def invalid(_value):
        raise ValueError("Non-finite owned execution value")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid)


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_private(path):
    path = Path(path).absolute()
    if path != path.resolve(strict=True):
        raise ValueError("Owned execution files must use canonical paths without symbolic links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > LIMIT
        ):
            raise ValueError(
                "Owned execution files must be private, bounded and owned by this user"
            )
        raw = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Owned execution file changed while reading")
    if len(raw) > LIMIT:
        raise ValueError("Owned execution file exceeds 64 KiB")
    return raw


def configuration(path):
    raw = read_private(path)
    value = parse(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "attempt", "socket", "records", "token"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["attempt"], str)
        or not IDENTIFIER.fullmatch(value["attempt"])
        or not isinstance(value["token"], str)
        or not 1 <= len(value["token"]) <= 256
    ):
        raise ValueError("Invalid process owner configuration")
    for name in ("socket", "records"):
        target = Path(value[name])
        if not target.is_absolute() or target != target.resolve(strict=True):
            raise ValueError("Process owner paths must be canonical and absolute")
    info = Path(value["socket"]).stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Use a private process owner socket belonging to this user")
    if not Path(value["records"]).is_dir():
        raise ValueError("Process owner records directory is unavailable")
    return value, hashlib.sha256(raw).hexdigest()


def command(args, *, cwd=None, env=None, python=None, launch_id=None):
    """Wrap a command when an owner is configured; otherwise preserve its arguments.

    Pass the returned argv to the normal subprocess API with the same cwd/env.
    The owner is configured through a private CAVI_MCP_PROCESS_OWNER JSON file.
    Embedded hosts must supply a trusted Python interpreter explicitly.
    """
    argv = [str(arg) for arg in args]
    parent = os.environ.get(OWNER_ENV)
    owner = (os.environ if env is None else env).get(OWNER_ENV)
    if parent and owner != parent:
        raise ValueError("A child environment cannot omit or replace its configured process owner")
    if not owner:
        return argv
    if os.name != "posix":
        raise ValueError("Owned execution currently requires POSIX and private Unix sockets")
    _configuration, owner_sha256 = configuration(owner)
    if not argv or any("\0" in arg for arg in argv):
        raise ValueError("Owned execution requires nonempty arguments without NUL bytes")
    if python is None and not Path(sys.executable).name.lower().startswith(("python", "pypy")):
        raise ValueError("Embedded hosts must supply a Python interpreter for owned execution")
    launch_id = uuid.uuid4().hex if launch_id is None else launch_id
    if not IDENTIFIER.fullmatch(launch_id):
        raise ValueError("Invalid owned launch identifier")
    payload = {
        "args": argv,
        "cwd": str(Path(cwd or os.getcwd()).resolve(strict=True)),
        "owner": str(owner),
        "owner_sha256": owner_sha256,
        "launch_id": launch_id,
    }
    raw = encoded(payload)
    if len(raw) > LIMIT:
        raise ValueError("Owned command exceeds 64 KiB")
    return [
        str(python or sys.executable),
        "-I",
        str(Path(__file__).resolve()),
        base64.urlsafe_b64encode(raw).decode("ascii"),
    ]


def execute(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"args", "cwd", "owner", "owner_sha256", "launch_id"}
        or not isinstance(payload["args"], list)
        or not payload["args"]
        or any(not isinstance(arg, str) or "\0" in arg for arg in payload["args"])
        or not isinstance(payload["launch_id"], str)
        or not IDENTIFIER.fullmatch(payload["launch_id"])
        or payload["cwd"] != os.getcwd()
        or payload["owner"] != os.environ.get(OWNER_ENV)
    ):
        raise ValueError("Owned command changed before registration")
    owner, owner_sha256 = configuration(payload["owner"])
    if owner_sha256 != payload["owner_sha256"]:
        raise ValueError("Process owner configuration changed before registration")
    if os.getpgrp() != os.getpid():
        os.setpgid(0, 0)
    request = {
        "kind": "owned-process-registration",
        "version": 1,
        "attempt": owner["attempt"],
        "launch_id": payload["launch_id"],
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "parent_pid": os.getppid(),
        "command": payload["args"],
        "cwd": os.getcwd(),
        "environment_sha256": digest(dict(os.environ)),
    }
    frame = encoded({"token": owner["token"], "request": request})
    if len(frame) > LIMIT:
        raise ValueError("Process registration exceeds 64 KiB")
    deadline = time.monotonic() + TIMEOUT
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(TIMEOUT)
        channel.connect(owner["socket"])
        channel.settimeout(max(0.001, deadline - time.monotonic()))
        channel.sendall(frame)
        raw = bytearray()
        while b"\n" not in raw:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Process registration deadline exceeded")
            channel.settimeout(remaining)
            chunk = channel.recv(min(4096, LIMIT + 1 - len(raw)))
            if not chunk or len(raw) + len(chunk) > LIMIT:
                raise ValueError("Invalid bounded process registration response")
            raw.extend(chunk)
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ValueError("Invalid process registration response framing")
        reply = parse(raw)
    if (
        not isinstance(reply, dict)
        or set(reply) != {"launch_permitted", "request_sha256", "record", "record_sha256"}
        or reply["launch_permitted"] is not True
        or reply["request_sha256"] != digest(request)
        or not isinstance(reply["record"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.json", reply["record"])
    ):
        raise ValueError("Process registration did not permit this launch")
    raw = read_private(Path(owner["records"]) / reply["record"])
    record = parse(raw)
    if (
        hashlib.sha256(raw).hexdigest() != reply["record_sha256"]
        or not isinstance(record, dict)
        or record.get("request") != request
        or not isinstance(record.get("birth_identity"), str)
        or not record["birth_identity"]
    ):
        raise ValueError("Process registration record differs from its acknowledgment")
    if configuration(payload["owner"])[1] != owner_sha256:
        raise ValueError("Process owner configuration changed before exec")
    os.execvpe(payload["args"][0], payload["args"], dict(os.environ))


def main():
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 4 * (LIMIT + 2) // 3:
            raise ValueError("Invalid owned command envelope")
        raw = base64.b64decode(sys.argv[1], altchars=b"-_", validate=True)
        if len(raw) > LIMIT:
            raise ValueError("Owned command exceeds 64 KiB")
        execute(parse(raw))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Owned execution refused: {error}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
