"""Bounded subprocess execution for the command-line tools an MCP server drives.

Host side only (asyncio and process control). The limits, the deadline and the kill rules
are written once:

* stdout and stderr are each bounded; what was dropped is reported, not hidden;
* one deadline covers the whole run, however the process behaves;
* on a timeout or a cancellation the process gets SIGTERM, a short grace period and
  then SIGKILL, signalling its whole process group on POSIX; on Windows the whole tree
  is ended with ``taskkill /T /F``;
* the run ends when the process exits, not when its pipes reach end-of-file, so a
  daemonised grandchild that inherited stdout cannot hold the caller for as long as it
  lives.
"""

import asyncio
import concurrent.futures
import contextlib
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STDOUT_LIMIT = 16 * 1024 * 1024
STDERR_LIMIT = 1024 * 1024
# Seconds between asking a process to stop and killing it.
TERM_GRACE = 3.0
# Seconds, after the process has exited, to collect output still in the pipes.
DRAIN_GRACE = 2.0


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


class _Capture(asyncio.SubprocessProtocol):
    """Collects bounded output and reports process exit separately from pipe EOF."""

    def __init__(self, stdout_limit: int, stderr_limit: int):
        loop = asyncio.get_running_loop()
        self.exited = loop.create_future()
        self.drained = loop.create_future()
        self.limits = {1: stdout_limit, 2: stderr_limit}
        self.chunks = {1: [], 2: []}
        self.sizes = {1: 0, 2: 0}
        self.open = {1, 2}

    def pipe_data_received(self, fd, data):
        size = self.sizes[fd]
        if size < self.limits[fd]:
            self.chunks[fd].append(data[: self.limits[fd] - size])
        self.sizes[fd] = size + len(data)

    def pipe_connection_lost(self, fd, exc):
        self.open.discard(fd)
        if not self.open and not self.drained.done():
            self.drained.set_result(None)

    def process_exited(self):
        if not self.exited.done():
            self.exited.set_result(None)

    def text(self, fd) -> str:
        return b"".join(self.chunks[fd]).decode(errors="replace")

    def truncated(self, fd) -> bool:
        return self.sizes[fd] > self.limits[fd]


def _windows() -> bool:
    return sys.platform == "win32"


def _session() -> dict:
    """Start the child in its own session on POSIX so its group can be signalled."""
    return {} if _windows() else {"start_new_session": True}


def _signal_group(pid: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, number)


async def _taskkill(pid: int) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(pid),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), 10)
    except (OSError, TimeoutError):
        pass


async def _stop(transport, protocol, grace: float) -> None:
    """End the process and everything it started, politely first where the platform allows."""
    pid = transport.get_pid()
    if _windows():
        # Windows has no signal a console tool honours, so there is no polite step.
        await _taskkill(pid)
        with contextlib.suppress(ProcessLookupError):
            transport.kill()  # fallback; taskkill has usually ended it already
    else:
        _signal_group(pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(protocol.exited), grace)
        # Also sweeps members that outlived a leader that obeyed SIGTERM.
        _signal_group(pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(protocol.exited), 10)


async def run(args, timeout: float, *, cwd=None, env=None, stdout_limit: int | None = None,
              stderr_limit: int | None = None, grace: float | None = None,
              label: str | None = None) -> Result:
    """Run ``args`` to completion within ``timeout`` seconds and return its bounded output.

    Raises TimeoutError once the deadline passes (the process is stopped first) and lets a
    cancellation through after stopping the process. A process that is still holding its
    pipes ``DRAIN_GRACE`` seconds after it exited is left alone and its output so far kept.
    ``label`` names the program in the timeout message; it defaults to the file name.
    """
    argv = [str(arg) for arg in args]
    protocol = _Capture(STDOUT_LIMIT if stdout_limit is None else stdout_limit,
                        STDERR_LIMIT if stderr_limit is None else stderr_limit)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.subprocess_exec(
        lambda: protocol, *argv,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, **_session(),
    )
    try:
        try:
            await asyncio.wait_for(asyncio.shield(protocol.exited), timeout)
        except TimeoutError:
            await _stop(transport, protocol, TERM_GRACE if grace is None else grace)
            raise TimeoutError(
                f"{label or Path(argv[0]).name} exceeded {timeout:g} s and was killed") from None
        except asyncio.CancelledError:
            # Shielded so a second cancellation cannot leave the process running.
            await asyncio.shield(_stop(transport, protocol, TERM_GRACE if grace is None else grace))
            raise
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(protocol.drained, DRAIN_GRACE)
        return Result(
            transport.get_returncode(), protocol.text(1), protocol.text(2),
            protocol.truncated(1), protocol.truncated(2),
        )
    finally:
        transport.close()


def run_sync(args, timeout: float, **kwargs) -> Result:
    """``run`` for code that is not async.

    On a thread without a running event loop, which includes a worker thread started by
    ``asyncio.to_thread``, it runs on a new loop right there. Called from a running loop's
    own thread it is also safe: the work then happens on a short-lived worker thread with
    its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run(args, timeout, **kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run(args, timeout, **kwargs)).result()


def diagnostics(result: Result) -> str:
    """The last few meaningful output lines, for an error message."""
    lines = [line for line in (result.stderr + "\n" + result.stdout).splitlines()
             if line.strip() and "[info]" not in line.lower()]
    text = " | ".join(lines[-6:])[-1500:] or "no diagnostics"
    if result.truncated:
        text += " [output truncated; these lines are from the part that was kept]"
    return text
