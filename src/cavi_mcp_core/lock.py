"""An exclusive advisory lock on one file, shared by every process that opens the same path.

Host side only; standard library.
"""

import errno
import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Absent on Windows, where the link is checked before the file is opened.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Seconds between attempts while waiting on Windows, whose blocking lock gives up after 10 s.
WINDOWS_RETRY = 0.05


class FileLock:
    """Exclusive lock on ``path`` (created if missing), held by one thread of one process at a time.

    Other threads of the process wait on a thread lock, other processes on the file lock. The
    holding thread may take it again; the file lock is released when it has released it as often
    as it took it.
    ``with lock:`` waits for it; ``acquire(blocking=False)`` answers False instead of waiting.
    With ``follow=False`` a ``path`` that is a symbolic link is refused with ``OSError`` and its
    target is neither created nor opened.
    """

    def __init__(self, path, *, follow: bool = True):
        self.path = Path(path)
        self._flags = os.O_CREAT | os.O_RDWR | (0 if follow else _NOFOLLOW)
        self._follow = follow
        self._thread = threading.RLock()
        self._owner = None
        self._depth = 0
        self._fd = None

    def acquire(self, blocking: bool = True) -> bool:
        if not self._thread.acquire(blocking):
            return False
        if self._depth == 0:
            try:
                if not self._follow and not _NOFOLLOW and self.path.is_symlink():
                    raise OSError(errno.ELOOP, "The lock file is a symbolic link", str(self.path))
                fd = os.open(self.path, self._flags, 0o600)
            except BaseException:
                self._thread.release()
                raise
            try:
                taken = _lock(fd, blocking)
            except BaseException:
                os.close(fd)
                self._thread.release()
                raise
            if not taken:
                os.close(fd)
                self._thread.release()
                return False
            self._fd, self._owner = fd, threading.get_ident()
        self._depth += 1
        return True

    def release(self) -> None:
        if self._depth == 0 or self._owner != threading.get_ident():
            raise RuntimeError(f"This thread does not hold the lock on {self.path}")
        self._depth -= 1
        if self._depth == 0:
            fd, self._fd, self._owner = self._fd, None, None
            try:
                _unlock(fd)
            finally:
                os.close(fd)
        self._thread.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def held(path) -> bool:
    """Whether a holder has the lock on an existing ``path``; never creates it."""
    if not Path(path).is_file():
        return False
    probe = FileLock(path)
    if probe.acquire(blocking=False):
        probe.release()
        return False
    return True


def _lock(fd, blocking):
    if sys.platform == "win32":
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not blocking:
                    return False
                time.sleep(WINDOWS_RETRY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd):
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
