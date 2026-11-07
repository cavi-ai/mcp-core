"""Bounded read-only macOS process census, birth identities and RSS sampling."""

import ctypes
import errno
import math
import os
import queue
import subprocess
import sys
import threading
import time


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "flags",
            "status",
            "exit_status",
            "pid",
            "ppid",
            "uid",
            "gid",
            "ruid",
            "rgid",
            "svuid",
            "svgid",
            "reserved",
        )
    ] + [
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        *[
            (name, ctypes.c_uint32)
            for name in ("files", "pgid", "job_count", "device", "terminal_pgid")
        ],
        ("nice", ctypes.c_int32),
        ("started_seconds", ctypes.c_uint64),
        ("started_microseconds", ctypes.c_uint64),
    ]


class _ProcBsdShortInfo(ctypes.Structure):
    # Public proc_info.h: proc_bsdshortinfo, PROC_PIDT_SHORTBSDINFO=13.
    # Unlike full BSD/task info, this census metadata is readable across UIDs.
    _fields_ = (
        [(name, ctypes.c_uint32) for name in ("pid", "ppid", "pgid", "status")]
        + [("comm", ctypes.c_char * 16)]
        + [
            (name, ctypes.c_uint32)
            for name in ("flags", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")
        ]
    )


class _ProcTaskInfo(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "virtual_size",
            "resident_size",
            "total_user",
            "total_system",
            "threads_user",
            "threads_system",
        )
    ] + [("counters", ctypes.c_int32 * 12)]


class _ProcTaskAllInfo(ctypes.Structure):
    _fields_ = [("bsd", _ProcBsdInfo), ("task", _ProcTaskInfo)]


class _ProcFdInfo(ctypes.Structure):
    # Public proc_info.h: proc_fdinfo rows from PROC_PIDLISTFDS=1.
    _fields_ = [("fd", ctypes.c_int32), ("fdtype", ctypes.c_uint32)]


class _DarwinProcesses:
    def __init__(self, library=None):
        self.library = (
            library
            if library is not None
            else ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        )
        self.library.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.library.proc_listpids.restype = ctypes.c_int

    def inventory(self, _request):
        # PROC_ALL_PIDS=1 returns bytes, with the same buffer contract as
        # child discovery. A full buffer cannot establish a complete census.
        ctypes.set_errno(0)
        needed = self.library.proc_listpids(1, 0, None, 0)
        error = ctypes.get_errno()
        if error:
            raise OSError(error, "Cannot enumerate native processes.")
        if needed <= 0 or needed % ctypes.sizeof(ctypes.c_int):
            raise ValueError("Invalid native process-list size.")
        buffer = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 32))()
        ctypes.set_errno(0)
        size = self.library.proc_listpids(1, 0, buffer, ctypes.sizeof(buffer))
        error = ctypes.get_errno()
        if error:
            raise OSError(error, "Cannot enumerate native processes.")
        if size <= 0 or size >= ctypes.sizeof(buffer) or size % ctypes.sizeof(ctypes.c_int):
            raise ValueError("Incomplete or misaligned native process list.")
        rows, seen = {}, set()
        for pid in buffer[: size // ctypes.sizeof(ctypes.c_int)]:
            if pid == 0:  # Empty PID slots are not user processes.
                continue
            if pid < 0 or pid in seen:
                raise ValueError("Invalid or duplicate native process PID.")
            seen.add(pid)
            info = _ProcBsdShortInfo()
            ctypes.set_errno(0)
            count = self.library.proc_pidinfo(pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info))
            error = ctypes.get_errno()
            if count == 0 and error == errno.ESRCH:
                continue
            if error:
                raise OSError(error, f"Cannot inspect native process {pid}: {os.strerror(error)}")
            if count != ctypes.sizeof(info) or info.pid != pid:
                raise ValueError(f"Incomplete native process identity for {pid}.")
            if info.status == 5:
                continue
            if not info.comm:
                raise ValueError(f"Incomplete native process identity for {pid}.")
            rows[pid] = (info.ppid, os.fsdecode(info.comm))
        if not rows:
            raise ValueError("Native process inventory is empty.")
        return rows

    def _info(self, pid, parent=None):
        info = _ProcTaskAllInfo()
        ctypes.set_errno(0)
        size = self.library.proc_pidinfo(pid, 2, 0, ctypes.byref(info), ctypes.sizeof(info))
        error = ctypes.get_errno()
        if size == 0 and error == errno.ESRCH:
            return None
        if size != ctypes.sizeof(info):
            if error == errno.EPERM and self._left_tree(pid, parent):
                return None
            if error:
                raise OSError(error, f"Cannot sample process {pid}: {os.strerror(error)}")
            raise ValueError(f"Incomplete native process information for {pid}.")
        if info.bsd.pid != pid:
            raise ValueError("The native process query returned a different PID.")
        return info

    def _left_tree(self, pid, parent):
        # PROC_PIDTBSDINFO can confirm exit when task memory is no longer
        # available. SZOMB=5 means awaiting collection by the parent.
        info = _ProcBsdInfo()
        ctypes.set_errno(0)
        size = self.library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        error = ctypes.get_errno()
        if size == 0 and error == errno.ESRCH:
            return True
        if size == 0 and error == errno.EPERM:
            return self._ps_exited(pid)
        if size != ctypes.sizeof(info) or error or info.pid != pid:
            return False
        # A discovered child may be reparented before its task query. The same
        # parent check applies to successful TASKALLINFO reads in sample().
        reparented = (
            parent is not None
            and info.ppid != parent
            and info.started_seconds > 0
            and info.started_microseconds < 1_000_000
        )
        return info.status == 5 or reparented

    @staticmethod
    def _ps_exited(pid):
        """Confirm disappearance or zombie state without reading process arguments."""
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid=,stat="],
                text=True,
                capture_output=True,
                timeout=0.5,
                check=False,
            )
            if result.stderr.strip():
                return False
            if result.returncode == 1 and not result.stdout.strip():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except OSError:
                    return False
                return False
            rows = result.stdout.splitlines()
            if result.returncode == 0 and len(rows) == 1:
                fields = rows[0].split()
                return len(fields) == 2 and int(fields[0]) == pid and fields[1].startswith("Z")
        except OSError, ValueError, subprocess.SubprocessError:
            pass
        return False

    def _children(self, pid):
        # proc_listpids returns BYTES; proc_listchildpids uses a different
        # return convention. PROC_PPID_ONLY=6 limits discovery to this parent.
        ctypes.set_errno(0)
        needed = self.library.proc_listpids(6, pid, None, 0)
        error = ctypes.get_errno()
        if needed == 0 and error in (0, errno.ESRCH):
            return []
        if needed < 0 or error:
            raise OSError(error or errno.EIO, "Cannot enumerate owned child processes.")
        if needed % ctypes.sizeof(ctypes.c_int):
            raise ValueError("Misaligned native child-process list.")
        # Spare capacity handles normal concurrent creation without retrying a
        # full buffer or accepting a potentially truncated process family.
        buffer = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 32))()
        ctypes.set_errno(0)
        size = self.library.proc_listpids(6, pid, buffer, ctypes.sizeof(buffer))
        error = ctypes.get_errno()
        if size == 0 and error == errno.ESRCH:
            return []
        if size < 0 or error:
            raise OSError(error or errno.EIO, "Cannot enumerate owned child processes.")
        if size >= ctypes.sizeof(buffer) or size % ctypes.sizeof(ctypes.c_int):
            raise ValueError("Truncated or misaligned native child-process list.")
        return [value for value in buffer[: size // ctypes.sizeof(ctypes.c_int)] if value > 0]

    @staticmethod
    def _row(info):
        # Start time survives exec/name changes and distinguishes reused PIDs.
        if info.bsd.started_seconds == 0 or info.bsd.started_microseconds >= 1_000_000:
            raise ValueError("Invalid native process birth identity.")
        identity = f"{info.bsd.started_seconds}:{info.bsd.started_microseconds}"
        return ((info.task.resident_size + 1023) // 1024, identity)

    def sample(self, pid, children=None):
        pending = [(pid, None)]
        measured = {}
        while pending:
            current, parent = pending.pop()
            if current in measured:
                continue
            try:
                info = self._info(current, parent)
            except PermissionError as error:
                raise PermissionError(
                    error.errno, f"{error.strerror}; tree root {pid}, expected parent {parent}"
                ) from error
            if (
                info is None
                or info.bsd.status == 5
                or (parent is not None and info.bsd.ppid != parent)
            ):
                continue
            measured[current] = self._row(info)
            discovered = self._children(current) if children is None else children.get(current, ())
            pending.extend((child, current) for child in discovered)
        return measured

    def sample_discovered(self, request):
        # Reuse the census only for discovery. sample() still reads native RSS
        # and birth identity and rejects children whose parent changed.
        return self.sample(*request)

    def identities(self, pids):
        result = {}
        for pid in pids:
            # Cleanup needs birth identity, not access to task memory, which
            # can be unavailable while a compiler or worker is exiting.
            info = _ProcBsdInfo()
            ctypes.set_errno(0)
            size = self.library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            error = ctypes.get_errno()
            if size == 0 and error == errno.ESRCH:
                continue
            if size == 0 and error == errno.EPERM and self._ps_exited(pid):
                continue
            if size != ctypes.sizeof(info) or error:
                raise OSError(error or errno.EIO, f"Cannot verify birth identity of process {pid}.")
            if info.pid != pid:
                raise ValueError("Invalid native process birth identity.")
            if info.status == 5:  # SZOMB has exited.
                continue
            if info.started_seconds == 0 or info.started_microseconds >= 1_000_000:
                raise ValueError("Invalid native process birth identity.")
            result[pid] = f"{info.started_seconds}:{info.started_microseconds}"
        return result

    def socket_count(self, pid):
        # A NULL buffer returns the byte size needed for the descriptor table.
        ctypes.set_errno(0)
        needed = self.library.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
        error = ctypes.get_errno()
        if needed < 0 or error:
            raise OSError(error or errno.EIO, f"Cannot list descriptors of process {pid}.")
        if needed == 0:
            return 0
        row = ctypes.sizeof(_ProcFdInfo)
        buffer = (_ProcFdInfo * (needed // row + 32))()
        ctypes.set_errno(0)
        size = self.library.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buffer, ctypes.sizeof(buffer))
        error = ctypes.get_errno()
        if size < 0 or error:
            raise OSError(error or errno.EIO, f"Cannot list descriptors of process {pid}.")
        if size >= ctypes.sizeof(buffer) or size % row:
            raise ValueError(f"Truncated or misaligned descriptor list for process {pid}.")
        return sum(1 for entry in buffer[: size // row] if entry.fdtype == _PROX_FDTYPE_SOCKET)


class _BoundedSampler:
    """One reusable read-only worker; a timed-out native call is never reused."""

    def __init__(self, backend=None):
        self.backend = backend
        self.requests = queue.Queue()
        self.lock = threading.Lock()
        self.failed = False
        threading.Thread(target=self._run, name="mcp-process-sampler", daemon=True).start()

    def _run(self):
        while True:
            operation, argument, done, reply = self.requests.get()
            try:
                if self.backend is None:
                    self.backend = _DarwinProcesses()
                reply.append((True, getattr(self.backend, operation)(argument)))
            except BaseException as error:  # noqa: BLE001 -- retain cancellation and cleanup evidence
                reply.append((False, error))
            finally:
                done.set()

    def call(self, operation, argument, timeout=2):
        deadline = time.monotonic() + min(2, timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("The native process sample has no time remaining.")
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            self.failed = True
            raise TimeoutError("Native process sampling timed out.")
        try:
            if self.failed:
                raise RuntimeError(
                    "Native process sampling is disabled after an interrupted or timed-out sample."
                )
            done, reply = threading.Event(), []
            self.requests.put((operation, argument, done, reply))
            try:
                if not done.wait(timeout=max(0, deadline - time.monotonic())):
                    self.failed = True
                    raise TimeoutError("Native process sampling timed out.")
            except BaseException:
                if not done.is_set():
                    self.failed = True
                raise
            successful, result = reply[0]
            if not successful:
                raise result
            return result
        finally:
            self.lock.release()


_PROC_PIDLISTFDS = 1
_PROX_FDTYPE_SOCKET = 2
_darwin_sampler = None
_darwin_inventory_sampler = None
_darwin_cleanup_sampler = None
_darwin_descriptor_sampler = None
_darwin_sampler_lock = threading.Lock()


def _native_sampler():
    global _darwin_sampler
    if _darwin_sampler is None:
        with _darwin_sampler_lock:
            if _darwin_sampler is None:
                _darwin_sampler = _BoundedSampler()
    return _darwin_sampler


def _cleanup_sampler():
    global _darwin_cleanup_sampler
    if _darwin_cleanup_sampler is None:
        with _darwin_sampler_lock:
            if _darwin_cleanup_sampler is None:
                # Never reuse an RSS worker quarantined after a timeout.
                _darwin_cleanup_sampler = _BoundedSampler()
    return _darwin_cleanup_sampler


def _remaining_sample_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Process sampling timed out.")
    return remaining


def _deadline(timeout):
    if sys.platform != "darwin":
        raise RuntimeError("Native process sampling currently requires macOS.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("Use a finite positive process-sampling budget.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("The native process sample has no time remaining.")
    return time.monotonic() + min(2, timeout)


def _pid(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError("Use a positive process PID.")
    return pid


def inventory(timeout=2):
    """Read a complete PID/parent/kernel-name census without sampling unrelated RSS."""
    deadline = _deadline(timeout)
    global _darwin_inventory_sampler
    if _darwin_inventory_sampler is None:
        with _darwin_sampler_lock:
            if _darwin_inventory_sampler is None:
                _darwin_inventory_sampler = _BoundedSampler()
    result = _darwin_inventory_sampler.call(
        "inventory", None, timeout=_remaining_sample_time(deadline)
    )
    _remaining_sample_time(deadline)
    return result


def tree(pid, timeout=2, *, children=None):
    """Sample checked descendants; supplied discovery never bypasses parent checks."""
    deadline = _deadline(timeout)
    _pid(pid)
    sampler = _native_sampler()
    operation = "sample" if children is None else "sample_discovered"
    argument = pid if children is None else (pid, children)
    try:
        result = sampler.call(operation, argument, timeout=_remaining_sample_time(deadline))
    except PermissionError:
        time.sleep(min(0.02, _remaining_sample_time(deadline)))
        result = sampler.call(operation, argument, timeout=_remaining_sample_time(deadline))
    _remaining_sample_time(deadline)
    return result


def _descriptor_sampler():
    global _darwin_descriptor_sampler
    if _darwin_descriptor_sampler is None:
        with _darwin_sampler_lock:
            if _darwin_descriptor_sampler is None:
                _darwin_descriptor_sampler = _BoundedSampler()
    return _darwin_descriptor_sampler


def identities(pids, timeout=2):
    """Read checked birth identities independently of census and RSS workers."""
    deadline = _deadline(timeout)
    pids = tuple(_pid(pid) for pid in pids)
    result = _cleanup_sampler().call("identities", pids, timeout=_remaining_sample_time(deadline))
    _remaining_sample_time(deadline)
    return result


def birth(pid, timeout=2):
    """Return Core's Darwin birth token, or None for an exited process."""
    identity = identities([pid], timeout=timeout).get(pid)
    return None if identity is None else "darwin:" + identity


def socket_count(pid, timeout=2):
    """Count open socket descriptors within the caller's bounded budget."""
    deadline = _deadline(timeout)
    _pid(pid)
    result = _descriptor_sampler().call(
        "socket_count", pid, timeout=_remaining_sample_time(deadline)
    )
    _remaining_sample_time(deadline)
    return result
