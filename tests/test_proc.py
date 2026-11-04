import asyncio
import gc
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from cavi_mcp_core import proc

POSIX = os.name != "nt"
posix_only = unittest.skipUnless(POSIX, "needs POSIX signals and process groups")


def python(code):
    return [sys.executable, "-c", code]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers; read its state where the platform shows it.
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        return "\nState:\tZ" not in status.read_text()
    return True


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


class CaptureTests(unittest.TestCase):
    def test_returns_exit_code_and_both_streams(self):
        result = asyncio.run(proc.run(python(
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), 30))
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()),
                         (3, "out", "err"))
        self.assertFalse(result.truncated)

    def test_arguments_may_be_paths(self):
        result = asyncio.run(proc.run([Path(sys.executable), "-c", "print(1)"], 30))
        self.assertEqual(result.stdout.strip(), "1")

    def test_output_is_bounded_per_stream_and_truncation_is_reported(self):
        code = "import sys; sys.stdout.write('o' * 300000); sys.stderr.write('e' * 300000)"
        result = asyncio.run(proc.run(python(code), 30, stdout_limit=1000, stderr_limit=500))
        self.assertEqual((len(result.stdout), len(result.stderr)), (1000, 500))
        self.assertEqual((result.stdout_truncated, result.stderr_truncated), (True, True))
        self.assertTrue(result.truncated)

    def test_truncation_names_the_stream_that_overflowed(self):
        code = "import sys; sys.stdout.write('o' * 3000); sys.stderr.write('e' * 10)"
        result = asyncio.run(proc.run(python(code), 30, stdout_limit=1000, stderr_limit=500))
        self.assertEqual((result.stdout_truncated, result.stderr_truncated), (True, False))
        self.assertTrue(result.truncated)

    def test_output_exactly_at_the_limit_is_not_truncated(self):
        result = asyncio.run(proc.run(
            python("import sys; sys.stdout.write('o' * 1000)"), 30, stdout_limit=1000))
        self.assertEqual((len(result.stdout), result.truncated), (1000, False))

    def test_default_limits_are_large_enough_for_tool_reports_but_finite(self):
        self.assertEqual(proc.STDOUT_LIMIT, 16 * 1024 * 1024)
        self.assertEqual(proc.STDERR_LIMIT, 1024 * 1024)

    def test_the_diagnostics_say_when_output_was_cut(self):
        cut = proc.Result(1, "line\n", "boom\n", stdout_truncated=True)
        whole = proc.Result(1, "line\n", "boom\n")
        self.assertIn("truncated", proc.diagnostics(cut))
        self.assertNotIn("truncated", proc.diagnostics(whole))

    def test_diagnostics_skip_info_lines_and_keep_the_last_ones(self):
        stdout = "\n".join(["[2026] [INFO] noise", *[f"line {n}" for n in range(10)]])
        text = proc.diagnostics(proc.Result(1, stdout, ""))
        self.assertNotIn("INFO", text)
        self.assertTrue(text.endswith("line 9"))
        self.assertNotIn("line 3", text)
        self.assertEqual(proc.diagnostics(proc.Result(1, "", "")), "no diagnostics")


class DeadlineTests(unittest.TestCase):
    def test_a_silent_process_is_killed_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "exceeded 0.5 s and was killed"):
            asyncio.run(proc.run(python("import time; time.sleep(60)"), 0.5))
        self.assertLess(time.monotonic() - started, 15)

    def test_a_process_that_keeps_writing_still_hits_the_deadline(self):
        code = "import sys\nwhile True:\n    sys.stdout.write('x' * 4096); sys.stdout.flush()\n"
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 1, stdout_limit=1000))
        self.assertLess(time.monotonic() - started, 15)

    def test_the_error_names_the_program(self):
        with self.assertRaisesRegex(TimeoutError, Path(sys.executable).name):
            asyncio.run(proc.run(python("import time; time.sleep(60)"), 0.3))


@posix_only
class SignalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_sigterm_comes_before_sigkill(self):
        marker = self.root / "terminated"
        code = (
            "import signal, sys, time\n"
            "def stop(*_):\n"
            f"    open({str(marker)!r}, 'w').write('term')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "time.sleep(60)\n"
        )
        with self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 1.0))
        self.assertTrue(marker.is_file())

    def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        code = ("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(60)\n")
        started = time.monotonic()
        with patch.object(proc, "TERM_GRACE", 0.5), self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 1.0))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.4)  # the deadline, then the grace period
        self.assertLess(elapsed, 15)

    def test_the_grace_can_be_set_per_call(self):
        code = ("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(60)\n")
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 0.5, grace=0.2))
        self.assertLess(time.monotonic() - started, 10)

    def test_the_whole_process_group_is_killed(self):
        pid_file = self.root / "grandchild.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        with self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 1.5))
        grandchild = int(pid_file.read_text())
        self.addCleanup(lambda: alive(grandchild) and os.kill(grandchild, signal.SIGKILL))
        self.assertTrue(wait_gone(grandchild), "the grandchild survived the kill")

    def test_stragglers_that_outlive_a_terminated_leader_are_killed(self):
        pid_file = self.root / "straggler.pid"
        code = (
            "import signal, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        with self.assertRaisesRegex(TimeoutError, "killed"):
            asyncio.run(proc.run(python(code), 1.5))
        straggler = int(pid_file.read_text())
        self.addCleanup(lambda: alive(straggler) and os.kill(straggler, signal.SIGKILL))
        self.assertTrue(wait_gone(straggler), "a group member ignored SIGTERM and survived")

    def test_cancelling_the_run_kills_the_process(self):
        pid_file = self.root / "child.pid"
        code = (f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n")

        async def scenario():
            task = asyncio.create_task(proc.run(python(code), 60))
            for _ in range(200):
                if pid_file.is_file() and pid_file.read_text():
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        child = int(pid_file.read_text())
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        self.assertTrue(wait_gone(child), "cancelling left the child running")

    def test_a_normal_exit_leaves_a_detached_helper_alone(self):
        # Adobe's licensing client outlives Sampler on purpose; a clean exit must not
        # sweep the process group.
        pid_file = self.root / "helper.pid"
        code = (
            "import subprocess, sys\n"
            "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(pid_file)!r}, 'w').write(str(helper.pid))\n"
        )
        result = asyncio.run(proc.run(python(code), 30))
        helper = int(pid_file.read_text())
        self.addCleanup(lambda: os.kill(helper, signal.SIGKILL))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(alive(helper))


class ExitNotEofTests(unittest.TestCase):
    """A grandchild that keeps stdout open must not hold the caller until it exits."""

    def start_holder(self, code_after):
        # The child starts a grandchild that inherits stdout, then leaves.
        return (
            "import subprocess, sys\n"
            "holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            f" stdout=sys.stdout{', start_new_session=True' if POSIX else ''})\n"
            "print(holder.pid, flush=True)\n" + code_after
        )

    def test_returns_when_the_process_exits_even_if_a_grandchild_holds_the_pipe(self):
        code = self.start_holder("print('done', flush=True)\n")
        started = time.monotonic()
        with patch.object(proc, "DRAIN_GRACE", 0.5):
            result = asyncio.run(proc.run(python(code), 60))
        elapsed = time.monotonic() - started
        holder = int(result.stdout.split()[0])
        self.addCleanup(lambda: os.kill(holder, signal.SIGTERM))
        self.assertEqual(result.returncode, 0)
        self.assertIn("done", result.stdout)
        self.assertLess(elapsed, 20, "the run waited for the grandchild's pipe")

    def test_the_exit_status_is_kept_when_the_pipe_is_still_held(self):
        code = self.start_holder("sys.exit(7)\n")
        with patch.object(proc, "DRAIN_GRACE", 0.5):
            result = asyncio.run(proc.run(python(code), 60))
        holder = int(result.stdout.split()[0])
        self.addCleanup(lambda: os.kill(holder, signal.SIGTERM))
        self.assertEqual(result.returncode, 7)

    def test_the_drain_wait_is_bounded_by_the_drain_grace(self):
        code = self.start_holder("")
        started = time.monotonic()
        with patch.object(proc, "DRAIN_GRACE", 1.0):
            result = asyncio.run(proc.run(python(code), 60))
        holder = int(result.stdout.split()[0])
        self.addCleanup(lambda: os.kill(holder, signal.SIGTERM))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)


class WindowsTreeKillTests(unittest.TestCase):
    """Windows cannot run here; check the code takes the Windows path by construction."""

    def run_as_windows(self, killer):
        calls = {"killpg": 0}

        def forbidden(*_):
            calls["killpg"] += 1

        code = "import time; time.sleep(60)"
        with (
            patch.object(proc.sys, "platform", "win32"),
            patch.object(proc.asyncio, "create_subprocess_exec", killer),
            patch.object(proc.os, "killpg", forbidden, create=True),
            self.assertRaisesRegex(TimeoutError, "killed"),
        ):
            asyncio.run(proc.run(python(code), 0.5))
        return calls

    def test_a_timeout_runs_taskkill_on_the_whole_tree(self):
        seen = []

        class Killer:
            async def wait(self):
                return 0

        async def fake_exec(*args, **_kwargs):
            seen.append(list(args))
            # taskkill would end the tree; SIGTERM is TerminateProcess on a real Windows too.
            os.kill(int(args[-1]), signal.SIGTERM)
            return Killer()

        calls = self.run_as_windows(fake_exec)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][:4], ["taskkill", "/T", "/F", "/PID"])
        self.assertTrue(seen[0][4].isdigit())
        self.assertEqual(calls["killpg"], 0, "a Windows run used POSIX process groups")

    def test_a_missing_taskkill_still_ends_the_process(self):
        async def missing(*_args, **_kwargs):
            raise FileNotFoundError("taskkill")

        started = time.monotonic()
        calls = self.run_as_windows(missing)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(calls["killpg"], 0)

    def test_a_windows_launch_does_not_ask_for_a_new_session(self):
        with patch.object(proc.sys, "platform", "win32"):
            self.assertEqual(proc._session(), {})
        with patch.object(proc.sys, "platform", "linux"):
            self.assertEqual(proc._session(), {"start_new_session": True})


class SyncTests(unittest.TestCase):
    def test_run_sync_from_plain_code(self):
        result = proc.run_sync(python("print('hi')"), 30)
        self.assertEqual(result.stdout.strip(), "hi")

    def test_run_sync_from_plain_code_starts_no_thread_pool(self):
        with patch.object(proc.concurrent.futures, "ThreadPoolExecutor",
                          side_effect=AssertionError("started a thread pool")):
            result = proc.run_sync(python("print('hi')"), 30)
        self.assertEqual(result.stdout.strip(), "hi")

    def test_run_sync_from_a_worker_thread_runs_on_that_thread(self):
        seen = {}
        real = proc.run

        async def spy(*args, **kwargs):
            seen["run"] = threading.get_ident()
            return await real(*args, **kwargs)

        def work():
            seen["caller"] = threading.get_ident()
            return proc.run_sync(python("print('hi')"), 30)

        async def scenario():
            return await asyncio.to_thread(work)

        with patch.object(proc, "run", spy):
            result = asyncio.run(scenario())
        self.assertEqual(result.stdout.strip(), "hi")
        self.assertEqual(seen["run"], seen["caller"])

    def test_run_sync_from_inside_a_running_loop(self):
        async def scenario():
            return proc.run_sync(python("print('hi')"), 30)

        self.assertEqual(asyncio.run(scenario()).stdout.strip(), "hi")

    def test_run_sync_propagates_the_deadline(self):
        with self.assertRaisesRegex(TimeoutError, "killed"):
            proc.run_sync(python("import time; time.sleep(60)"), 0.5)


class LeakTests(unittest.TestCase):
    def test_pipes_are_closed_even_when_a_grandchild_holds_them(self):
        code = ("import subprocess, sys\n"
                "holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
                f" stdout=sys.stdout{', start_new_session=True' if POSIX else ''})\n"
                "print(holder.pid, flush=True)\n")
        with warnings.catch_warnings(record=True) as caught, patch.object(proc, "DRAIN_GRACE", 0.3):
            warnings.simplefilter("always")
            result = asyncio.run(proc.run(python(code), 60))
            gc.collect()
        os.kill(int(result.stdout.split()[0]), signal.SIGTERM)
        self.assertEqual([str(item.message) for item in caught
                          if issubclass(item.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
