"""Bounded, workspace-confined file reads shared by local tool workflows."""

import hashlib
import json
import os
import stat
from pathlib import Path


def inside(workspace, value):
    """``value`` resolved inside ``workspace``; refuses traversal, escapes and the root itself."""
    root = Path(workspace).resolve(strict=True)
    path = Path(value)
    if '..' in path.parts:
        raise ValueError('Use workspace paths without parent traversal')
    logical = Path(os.path.abspath(root / path))
    resolved = logical.resolve()
    if (not logical.is_relative_to(root) or not resolved.is_relative_to(root)
            or resolved == root):
        raise ValueError('Path must remain inside the shared workspace')
    return resolved


def read_bytes(path, limit):
    """A regular file's bytes, at most ``limit``; refuses a file that changes while read."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Expected a bounded regular file')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = Path(path).stat()
    if (len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)
            or (current.st_size, current.st_mtime_ns, current.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise ValueError('Input changed while reading')
    return data


def digest(path):
    """SHA-256 of a regular file of at most 64 MiB."""
    return hashlib.sha256(read_bytes(path, 64 * 1024 * 1024)).hexdigest()


def parse_json(raw):
    """JSON without NaN or Infinity."""
    def invalid(value):
        raise ValueError('Non-finite JSON number: ' + value)
    return json.loads(raw, parse_constant=invalid)


def load_json(path):
    """``parse_json`` of a regular file of at most 1 MiB."""
    return parse_json(read_bytes(path, 1024 * 1024))
