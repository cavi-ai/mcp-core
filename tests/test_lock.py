import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from cavi_mcp_core.lock import FileLock, held

HOLD = ("import sys, time\nfrom cavi_mcp_core.lock import FileLock\n"
        "with FileLock(sys.argv[1]):\n    start = time.time()\n    time.sleep(0.5)\n"
        "    print(start, time.time())\n")
PROBE = ("import sys\nfrom cavi_mcp_core.lock import FileLock\n"
         "print(FileLock(sys.argv[1]).acquire(blocking=False))")


class FileLockTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "app.lock"

    def test_two_processes_never_hold_it_at_once(self):
        children = [subprocess.Popen([sys.executable, "-c", HOLD, str(self.path)],
                                     stdout=subprocess.PIPE, text=True) for _ in range(2)]
        spans = sorted(tuple(map(float, child.communicate(timeout=60)[0].split()))
                       for child in children)
        self.assertLessEqual(spans[0][1], spans[1][0])

    def test_the_holding_thread_takes_it_again_and_frees_it_on_the_last_release(self):
        lock = FileLock(self.path)
        with lock:
            with lock:
                self.assertTrue(held(self.path))
            self.assertTrue(held(self.path))
        self.assertFalse(held(self.path))

    def test_without_blocking_another_thread_or_process_gets_false(self):
        lock, taken, release = FileLock(self.path), threading.Event(), threading.Event()

        def holder():
            with lock:
                taken.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        taken.wait(10)
        self.assertFalse(lock.acquire(blocking=False))
        probe = subprocess.run([sys.executable, "-c", PROBE, str(self.path)],
                               capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(probe.stdout.strip(), "False")
        release.set()
        thread.join(10)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_a_thread_that_does_not_hold_it_cannot_release_it(self):
        lock = FileLock(self.path)
        with self.assertRaisesRegex(RuntimeError, "does not hold"):
            lock.release()
        with lock:
            errors = []
            def other():
                with self.assertRaises(RuntimeError):
                    lock.release()
                errors.append(1)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(10)
            self.assertEqual(errors, [1])
            self.assertTrue(held(self.path))

    def test_held_never_creates_the_file(self):
        self.assertFalse(held(self.path))
        self.assertFalse(self.path.exists())

    def test_the_lock_file_is_private(self):
        with FileLock(self.path):
            pass
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
