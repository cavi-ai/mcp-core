"""Durable JSON state and retained native completion receipts."""

import hashlib
import json
import os
import uuid
from pathlib import Path


def _sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def create_directory(path):
    """Create a new private canonical directory and durably link missing parents."""
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError("Use a canonical directory without symbolic links")
    if path.exists():
        raise FileExistsError(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            raise FileNotFoundError("Directory filesystem root is unavailable: " + str(current))
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        _sync_directory(directory)
        _sync_directory(directory.parent)
    return path


def create_json(path, value):
    """Create a durable private record once and return the digest of its exact bytes."""
    path = Path(path)
    raw = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    """Replace ``path`` with ``value`` atomically and durably; the file is private to the user."""
    path = Path(path)
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Windows cannot open a directory to flush it; NTFS journals the rename itself.
        if os.name != "nt":
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(value):
    """SHA-256 of ``value`` as canonical JSON."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def completion(path, operation_id):
    """The completion receipt for ``operation_id`` at ``path``, or None before it exists."""
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Native receipt must not be a symbolic link")
    if not path.exists():
        return None
    if not path.is_file() or not 1 <= path.stat().st_size <= 8 * 1024 * 1024:
        raise ValueError("Native completion receipt is missing, unsafe or too large")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("operation_id") != operation_id
        or type(value.get("ok")) is not bool
    ):
        raise ValueError("Native completion receipt does not match the operation")
    return value


def archive(path, record, resolution):
    """Record how an interrupted operation was resolved; repeating the same record is a no-op."""
    path = Path(path)
    value = {"prior_operation": record, **resolution}
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError("Recovery record already exists with a different outcome")
        return
    write_json(path, value)
