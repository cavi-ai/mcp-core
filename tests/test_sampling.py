import ctypes
import errno
import os
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cavi_mcp_core import sampling


class SamplingBoundaryTests(unittest.TestCase):
    def test_unsupported_platform_refuses_before_a_native_worker(self):
        with (
            patch.object(sampling.sys, "platform", "linux"),
            patch.object(sampling, "_BoundedSampler") as worker,
        ):
            for operation in (
                lambda: sampling.inventory(),
                lambda: sampling.tree(1),
                lambda: sampling.identities([1]),
                lambda: sampling.socket_count(1),
                lambda: sampling.birth(1),
            ):
                with self.assertRaises(RuntimeError):
                    operation()
            worker.assert_not_called()

    def test_invalid_budgets_and_pids_refuse_before_a_worker(self):
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_BoundedSampler") as worker,
        ):
            for budget in (0, -1, float("nan"), float("inf")):
                for operation in (
                    lambda budget=budget: sampling.inventory(timeout=budget),
                    lambda budget=budget: sampling.tree(1, timeout=budget),
                    lambda budget=budget: sampling.identities([1], timeout=budget),
                    lambda budget=budget: sampling.socket_count(1, timeout=budget),
                ):
                    with self.assertRaises(TimeoutError):
                        operation()
            for pid in (0, -1, True, 1.5, "1"):
                for operation in (
                    lambda pid=pid: sampling.tree(pid),
                    lambda pid=pid: sampling.identities([pid]),
                    lambda pid=pid: sampling.socket_count(pid),
                ):
                    with self.assertRaises(ValueError):
                        operation()
            worker.assert_not_called()

    def test_descriptor_timeout_is_bounded_and_leaves_identity_cleanup_available(self):
        release = threading.Event()
        descriptors = sampling._BoundedSampler(
            SimpleNamespace(socket_count=lambda _: release.wait(2))
        )
        cleanup = sampling._BoundedSampler(
            SimpleNamespace(identities=lambda pids: {pids[0]: "30:7"})
        )
        try:
            with (
                patch.object(sampling.sys, "platform", "darwin"),
                patch.object(sampling, "_descriptor_sampler", return_value=descriptors),
                patch.object(sampling, "_cleanup_sampler", return_value=cleanup),
            ):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    sampling.socket_count(101, timeout=0.02)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(sampling.identities([101]), {101: "30:7"})
                with self.assertRaises(RuntimeError):
                    sampling.socket_count(101)
        finally:
            release.set()

    def test_core_birth_tokens_use_the_shared_checked_identity(self):
        from cavi_mcp_core import processes

        cleanup = sampling._BoundedSampler(
            SimpleNamespace(identities=lambda pids: {pids[0]: "30:7"})
        )
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_cleanup_sampler", return_value=cleanup),
        ):
            self.assertEqual(processes.birth(101), "darwin:30:7")


class DarwinInventoryTests(unittest.TestCase):
    def test_census_omits_zombies_after_validating_the_returned_structure(self):
        backend, library = self.backend(pids=(101, 102))
        original = library.proc_pidinfo.side_effect

        def info(pid, flavor, arg, buffer, size):
            result = original(pid, flavor, arg, buffer, size)
            row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdShortInfo)).contents
            row.status = 5 if pid == 102 else 2
            return result

        library.proc_pidinfo.side_effect = info
        self.assertEqual(backend.inventory(None), {101: (1, "Unity")})

    def backend(self, pids=(0, 1, 101, 102, 999)):
        records = {1: (0, b"launchd"), 101: (1, b"Unity"), 102: (101, b"dotnet")}

        def listing(kind, parent, buffer, size):
            self.assertEqual((kind, parent), (1, 0))
            if buffer is not None:
                target = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))
                for index, pid in enumerate(pids):
                    target[index] = pid
            return len(pids) * 4

        def info(pid, flavor, arg, buffer, size):
            self.assertEqual((flavor, arg, size), (13, 0, 64))
            if pid not in records:
                ctypes.set_errno(errno.ESRCH)
                return 0
            row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdShortInfo)).contents
            row.pid, row.ppid, row.comm = pid, *records[pid]
            return 64

        library = SimpleNamespace(
            proc_pidinfo=Mock(side_effect=info), proc_listpids=Mock(side_effect=listing)
        )
        return sampling._DarwinProcesses(library), library

    def test_complete_inventory_uses_short_bsd_for_system_and_child_processes(self):
        backend, library = self.backend()
        self.assertEqual(ctypes.sizeof(sampling._ProcBsdShortInfo), 64)
        self.assertEqual(sampling._ProcBsdShortInfo.comm.offset, 16)
        self.assertEqual(
            backend.inventory(None), {1: (0, "launchd"), 101: (1, "Unity"), 102: (101, "dotnet")}
        )
        self.assertEqual(library.proc_listpids.call_count, 2)
        self.assertEqual(
            [c.args[0] for c in library.proc_pidinfo.call_args_list], [1, 101, 102, 999]
        )

    def test_inventory_never_omits_denied_unknown_or_mismatched_process(self):
        for count, error, returned_pid, name in (
            (0, errno.EPERM, 101, b"Unity"),
            (0, 0, 101, b"Unity"),
            (63, 0, 101, b"Unity"),
            (64, 0, 999, b"Unity"),
            (64, 0, 101, b""),
        ):
            with self.subTest(count=count, error=error, pid=returned_pid, name=name):
                backend, library = self.backend(pids=(101,))

                def broken(
                    pid,
                    flavor,
                    arg,
                    buffer,
                    size,
                    *,
                    count=count,
                    error=error,
                    name=name,
                    returned_pid=returned_pid,
                ):
                    ctypes.set_errno(error)
                    row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdShortInfo)).contents
                    row.pid, row.comm = returned_pid, name
                    return count

                library.proc_pidinfo.side_effect = broken
                with self.assertRaises((OSError, ValueError)):
                    backend.inventory(None)

    def test_inventory_rejects_truncated_misaligned_or_failed_pid_lists(self):
        for needed, returned, error in (
            (3, 4, 0),
            (4, 3, 0),
            (4, 132, 0),
            (4, 0, 0),
            (4, -1, errno.EIO),
        ):
            with self.subTest(needed=needed, returned=returned, error=error):
                backend, library = self.backend()

                def listing(
                    kind, parent, buffer, size, *, error=error, needed=needed, returned=returned
                ):
                    ctypes.set_errno(error if buffer is not None else 0)
                    return needed if buffer is None else returned

                library.proc_listpids.side_effect = listing
                with self.assertRaises((OSError, ValueError)):
                    backend.inventory(None)

    def test_inventory_rejects_duplicate_or_negative_pids(self):
        for pids in ((101, 101), (-1,)):
            with self.subTest(pids=pids):
                backend, _ = self.backend(pids=pids)
                with self.assertRaises(ValueError):
                    backend.inventory(None)

    def test_inventory_has_one_bounded_call_and_rejects_late_return(self):
        now = [0.0]
        sampler = Mock()

        def call(*args, **kwargs):
            now[0] = 2.01
            return {101: (1, "Unity")}

        sampler.call.side_effect = call
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_darwin_inventory_sampler", sampler),
            patch.object(sampling.time, "monotonic", side_effect=lambda: now[0]),
            self.assertRaises(TimeoutError),
        ):
            sampling.inventory(timeout=3)
        sampler.call.assert_called_once_with("inventory", None, timeout=2)

    def test_census_timeout_does_not_poison_owned_identity_cleanup(self):
        release = threading.Event()
        census_backend, owned_backend = Mock(), Mock()
        census_backend.inventory.side_effect = lambda _: release.wait(2)
        owned_backend.identities.return_value = {101: "20:7"}
        census = sampling._BoundedSampler(census_backend)
        owned = sampling._BoundedSampler(owned_backend)
        try:
            with (
                patch.object(sampling.sys, "platform", "darwin"),
                patch.object(sampling, "_darwin_inventory_sampler", census),
                patch.object(sampling, "_darwin_cleanup_sampler", owned),
            ):
                with self.assertRaises(TimeoutError):
                    sampling.inventory(timeout=0.01)
                self.assertEqual(
                    sampling._cleanup_sampler().call("identities", [101]), {101: "20:7"}
                )
            self.assertTrue(census.failed)
            self.assertFalse(owned.failed)
        finally:
            release.set()


class DarwinSamplerTests(unittest.TestCase):
    def test_successful_empty_descriptor_tables_have_zero_sockets(self):
        for replies in ((0,), (8, 0)):
            with self.subTest(replies=replies):
                library = SimpleNamespace(
                    proc_pidinfo=Mock(side_effect=replies), proc_listpids=Mock()
                )
                backend = sampling._DarwinProcesses(library)
                self.assertEqual(backend.socket_count(101), 0)

    def test_descriptor_failures_and_partial_tables_still_refuse(self):
        for replies, error in (
            ((0,), errno.EPERM),
            ((8, 0), errno.ESRCH),
            ((8, 3), 0),
            ((8, 264), 0),
        ):
            with self.subTest(replies=replies, error=error):
                values = iter(replies)

                def info(*args, values=values, error=error):
                    value = next(values)
                    ctypes.set_errno(error if value == 0 else 0)
                    return value

                library = SimpleNamespace(proc_pidinfo=Mock(side_effect=info), proc_listpids=Mock())
                backend = sampling._DarwinProcesses(library)
                with self.assertRaises((OSError, ValueError)):
                    backend.socket_count(101)

    def backend(self, records=None, children=None):
        records = records or {101: (1, 4097, 20, "parent"), 102: (101, 8192, 30, "worker")}
        children = children or {101: [102]}

        def info(pid, flavor, arg, buffer, size):
            if pid not in records:
                ctypes.set_errno(errno.ESRCH)
                return 0
            parent, rss, started, name = records[pid]
            if flavor == 3:
                self.assertEqual((arg, size), (0, 136))
                row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
                row.pid, row.ppid = pid, parent
                row.started_seconds, row.started_microseconds = started, 7
                return 136
            self.assertEqual((flavor, arg, size), (2, 0, 232))
            row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcTaskAllInfo)).contents
            row.bsd.pid, row.bsd.ppid = pid, parent
            row.bsd.started_seconds, row.bsd.started_microseconds = started, 7
            row.bsd.name = name.encode()
            row.task.resident_size = rss
            return 232

        def listing(kind, parent, buffer, size):
            self.assertEqual(kind, 6)
            pids = children.get(parent, [])
            if buffer is not None:
                target = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))
                for index, pid in enumerate(pids):
                    target[index] = pid
            return len(pids) * 4

        library = SimpleNamespace(
            proc_pidinfo=Mock(side_effect=info), proc_listpids=Mock(side_effect=listing)
        )
        return sampling._DarwinProcesses(library), library

    def test_sample_rejects_unchecked_birth_fields(self):
        for seconds, micros in ((0, 7), (30, 1000000)):
            with self.subTest(seconds=seconds, micros=micros):
                backend, library = self.backend()

                def info(pid, flavor, arg, buffer, size, *, micros=micros, seconds=seconds):
                    row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcTaskAllInfo)).contents
                    row.bsd.pid = pid
                    row.bsd.started_seconds, row.bsd.started_microseconds = seconds, micros
                    row.task.resident_size = 4096
                    return 232

                library.proc_pidinfo.side_effect = info
                with self.assertRaises(ValueError):
                    backend.sample(101)

    def test_zombie_identity_and_rss_are_absent_without_live_birth_fields(self):
        backend, library = self.backend()

        def info(pid, flavor, arg, buffer, size):
            if flavor == 3:
                row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
            else:
                row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcTaskAllInfo)).contents.bsd
            row.pid, row.status = pid, 5
            return size

        library.proc_pidinfo.side_effect = info
        self.assertEqual(backend.sample(101), {})
        self.assertEqual(backend.identities([101]), {})

    def test_native_layout_and_owned_descendant_rss_use_kernel_units(self):
        self.assertEqual(ctypes.sizeof(sampling._ProcBsdInfo), 136)
        self.assertEqual(ctypes.sizeof(sampling._ProcTaskInfo), 96)
        backend, library = self.backend()
        rows = backend.sample(101)
        self.assertEqual(rows, {101: (5, "20:7"), 102: (8, "30:7")})
        self.assertEqual({call.args[0] for call in library.proc_pidinfo.call_args_list}, {101, 102})

    def test_cached_children_avoid_repeated_native_discovery_but_still_read_each_rss(self):
        backend, library = self.backend(
            records={
                101: (1, 4097, 20, "parent"),
                102: (101, 8192, 30, "worker"),
                103: (102, 1024, 40, "nested"),
                999: (1, 999999, 50, "unrelated"),
            }
        )
        children = {101: [102], 102: [103], 1: [999]}
        self.assertEqual(
            backend.sample_discovered((101, children)),
            {101: (5, "20:7"), 102: (8, "30:7"), 103: (1, "40:7")},
        )
        library.proc_listpids.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in library.proc_pidinfo.call_args_list], [101, 102, 103]
        )

    def test_cached_children_keep_live_denial_and_reparent_checks(self):
        backend, library = self.backend(
            records={
                101: (1, 4096, 20, "parent"),
                102: (999, 8192, 30, "reparented"),
            }
        )
        self.assertEqual(backend.sample_discovered((101, {101: [102]})), {101: (4, "20:7")})
        library.proc_listpids.assert_not_called()
        denied, _ = self.denied_child_backend(parent=101)
        with self.assertRaises(PermissionError):
            denied.sample_discovered((101, {101: [102]}))

    def test_bounded_process_tree_uses_cached_discovery(self):
        backend, library = self.backend()
        sampler = sampling._BoundedSampler(backend)
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_native_sampler", return_value=sampler),
        ):
            self.assertEqual(
                sampling.tree(101, children={101: [102]}), {101: (5, "20:7"), 102: (8, "30:7")}
            )
        library.proc_listpids.assert_not_called()

    def test_native_short_reads_and_permission_errors_fail_closed(self):
        for count, error in ((231, 0), (0, errno.EPERM), (0, 0)):
            with self.subTest(count=count, error=error):
                backend, library = self.backend()

                def broken(*args, count=count, error=error):
                    ctypes.set_errno(error)
                    return count

                library.proc_pidinfo.side_effect = broken
                with (
                    patch.object(backend, "_ps_exited", return_value=False),
                    self.assertRaises((OSError, ValueError)),
                ):
                    backend.sample(101)

    def test_only_confirmed_disappearance_can_remove_a_process_from_a_sample(self):
        backend, _ = self.backend(records={101: (1, 4096, 20, "parent")})
        self.assertEqual(backend.sample(101), {101: (4, "20:7")})
        self.assertEqual(backend.sample(999), {})

    def denied_task_backend(self, count=136, error=0, status=5, returned_pid=101):
        backend, library = self.backend()

        def denied(pid, flavor, arg, buffer, size):
            if flavor == 2:
                ctypes.set_errno(errno.EPERM)
                return 0
            self.assertEqual((pid, flavor, arg, size), (101, 3, 0, 136))
            ctypes.set_errno(error)
            if count == 136:
                row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
                row.pid, row.status = returned_pid, status
            return count

        library.proc_pidinfo.side_effect = denied
        return backend, library

    def test_denied_task_sample_can_confirm_exit_without_task_memory(self):
        for options in ({}, {"count": 0, "error": errno.ESRCH}):
            with self.subTest(options=options):
                backend, library = self.denied_task_backend(**options)
                self.assertEqual(backend.sample(101), {})
                self.assertEqual(
                    [call.args[1] for call in library.proc_pidinfo.call_args_list], [2, 3]
                )
                library.proc_listpids.assert_not_called()

    def test_denied_task_sample_still_rejects_live_unknown_or_mismatched_bsd_identity(self):
        for options in (
            {"status": 2},
            {"status": 3},
            {"returned_pid": 999},
            {"count": 135},
            {"count": 0, "error": errno.EPERM},
            {"count": 0},
        ):
            with self.subTest(options=options):
                backend, _ = self.denied_task_backend(**options)
                with (
                    patch.object(backend, "_ps_exited", return_value=False),
                    self.assertRaises(OSError) as error,
                ):
                    backend.sample(101)
                self.assertEqual(error.exception.errno, errno.EPERM)

    def test_double_denial_accepts_independently_confirmed_absence_or_zombie(self):
        for code, rows, signal_error in ((1, "", ProcessLookupError()), (0, "101 Z+\n", None)):
            with self.subTest(rows=rows):
                backend, _ = self.denied_task_backend(count=0, error=errno.EPERM)
                result = SimpleNamespace(returncode=code, stdout=rows, stderr="")
                with (
                    patch.object(subprocess, "run", return_value=result) as query,
                    patch.object(os, "kill", side_effect=signal_error),
                ):
                    try:
                        measured = backend.sample(101)
                    except OSError:
                        measured = None
                self.assertEqual(measured, {})
                self.assertEqual(query.call_args.args[0], ["ps", "-p", "101", "-o", "pid=,stat="])

    def test_double_denial_never_drops_live_protected_or_unconfirmed_process(self):
        cases = (
            (0, "101 S\n", "", None),
            (0, "999 Z\n", "", None),
            (0, "", "", None),
            (1, "", "", None),
            (1, "", "", PermissionError()),
            (1, "", "query denied", ProcessLookupError()),
        )
        for code, rows, stderr, signal_error in cases:
            with self.subTest(code=code, rows=rows, stderr=stderr, signal_error=signal_error):
                backend, _ = self.denied_task_backend(count=0, error=errno.EPERM)
                result = SimpleNamespace(returncode=code, stdout=rows, stderr=stderr)
                with (
                    patch.object(subprocess, "run", return_value=result),
                    patch.object(os, "kill", side_effect=signal_error),
                    self.assertRaises(OSError) as error,
                ):
                    backend.sample(101)
                self.assertEqual(error.exception.errno, errno.EPERM)

    def test_failed_exit_confirmation_preserves_sampling_failure(self):
        backend, _ = self.denied_task_backend(count=0, error=errno.EPERM)
        with (
            patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 0.5)),
            self.assertRaises(OSError) as error,
        ):
            backend.sample(101)
        self.assertEqual(error.exception.errno, errno.EPERM)

    def test_truncated_or_misaligned_child_lists_fail_closed(self):
        for returned in (5, 144):
            with self.subTest(returned=returned):
                backend, library = self.backend()
                library.proc_listpids.side_effect = (
                    lambda kind, pid, buffer, size, returned=returned: (
                        16 if buffer is None else returned
                    )
                )
                with self.assertRaises(ValueError):
                    backend.sample(101)

    def test_reparented_pid_is_not_adopted_as_a_descendant(self):
        backend, _ = self.backend(
            records={101: (1, 4096, 20, "parent"), 102: (999, 8192, 30, "other")}
        )
        self.assertEqual(backend.sample(101), {101: (4, "20:7")})

    def denied_child_backend(self, parent=999, returned_pid=102, count=136, started=30):
        backend, library = self.backend()
        normal_info = library.proc_pidinfo.side_effect

        def info(pid, flavor, arg, buffer, size):
            if pid != 102:
                return normal_info(pid, flavor, arg, buffer, size)
            if flavor == 2:
                ctypes.set_errno(errno.EPERM)
                return 0
            self.assertEqual((flavor, arg, size), (3, 0, 136))
            if count == 136:
                row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
                row.pid, row.ppid, row.status = returned_pid, parent, 2
                row.started_seconds, row.started_microseconds = started, 7
            return count

        library.proc_pidinfo.side_effect = info
        return backend, library

    def test_denied_memory_read_can_reject_a_confirmed_reparented_child(self):
        backend, library = self.denied_child_backend()
        self.assertEqual(backend.sample(101), {101: (5, "20:7")})
        self.assertEqual(
            [call.args[1] for call in library.proc_pidinfo.call_args_list if call.args[0] == 102],
            [2, 3],
        )

    def test_denied_live_child_and_unconfirmed_departure_fail_closed(self):
        for options in ({"parent": 101}, {"returned_pid": 999}, {"count": 135}, {"started": 0}):
            with self.subTest(options=options):
                backend, _ = self.denied_child_backend(**options)
                with self.assertRaises(PermissionError) as error:
                    backend.sample(101)
                self.assertEqual(error.exception.errno, errno.EPERM)

    def test_denied_sample_identifies_the_tree_root_and_expected_parent(self):
        backend, _ = self.denied_child_backend(parent=101)
        with self.assertRaises(PermissionError) as error:
            backend.sample(101)
        self.assertIn("tree root 101, expected parent 101", str(error.exception))

    def test_one_daemon_worker_is_reused_and_timeout_rejects_future_samples(self):
        released = threading.Event()
        calls = []

        def sample(pid):
            calls.append(threading.current_thread())
            if pid == 3:
                released.wait()
            return {pid: (1, "20:7")}

        sampler = sampling._BoundedSampler(SimpleNamespace(sample=sample))
        try:
            self.assertEqual(sampler.call("sample", 1), {1: (1, "20:7")})
            self.assertEqual(sampler.call("sample", 2), {2: (1, "20:7")})
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                sampler.call("sample", 3, timeout=0.03)
            self.assertLess(time.monotonic() - started, 0.5)
            with self.assertRaises(RuntimeError):
                sampler.call("sample", 4)
            self.assertIs(calls[0], calls[1])
            self.assertTrue(calls[0].daemon)
        finally:
            released.set()

    def test_transient_permission_denial_rereads_the_strong_native_snapshot(self):
        rows = {101: (4096, "20:7"), 102: (2048, "21:8")}
        sampler = Mock()
        sampler.call.side_effect = [PermissionError("transition"), rows]
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_native_sampler", return_value=sampler),
            patch.object(sampling.time, "sleep") as sleep,
        ):
            try:
                result = sampling.tree(101)
            except PermissionError:
                result = None
        self.assertEqual(result, rows)
        self.assertEqual(sampler.call.call_count, 2)
        sleep.assert_called_once_with(0.02)

    def test_persistent_permission_denial_is_not_retried_more_than_once(self):
        sampler = Mock()
        sampler.call.side_effect = PermissionError("protected live process")
        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_native_sampler", return_value=sampler),
            patch.object(sampling.time, "sleep"),
            self.assertRaises(PermissionError),
        ):
            sampling.tree(101)
        self.assertEqual(sampler.call.call_count, 2)

    def test_permission_reread_uses_remaining_original_budget(self):
        now, calls = [0.0], []
        rows = {101: (4096, "20:7")}

        def call(operation, pid, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                now[0] += 0.25
                raise PermissionError("transition")
            return rows

        def sleep(duration):
            now[0] += duration

        with (
            patch.object(sampling.sys, "platform", "darwin"),
            patch.object(sampling, "_native_sampler", return_value=SimpleNamespace(call=call)),
            patch.object(sampling.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(sampling.time, "sleep", side_effect=sleep),
        ):
            try:
                result = sampling.tree(101, timeout=0.5)
            except PermissionError:
                result = None
        self.assertEqual(result, rows)
        self.assertEqual(calls[0], 0.5)
        self.assertAlmostEqual(calls[1], 0.23)

    def test_timeout_never_retries_and_exhausted_permission_budget_cannot_reread(self):
        for failure in (TimeoutError("sample timed out"), PermissionError("late denial")):
            with self.subTest(failure=failure):
                now = [0.0]
                sampler = Mock()

                def call(*args, failure=failure, now=now, **kwargs):
                    now[0] = 0.5
                    raise failure

                sampler.call.side_effect = call
                with (
                    patch.object(sampling.sys, "platform", "darwin"),
                    patch.object(sampling, "_native_sampler", return_value=sampler),
                    patch.object(sampling.time, "monotonic", side_effect=lambda now=now: now[0]),
                    patch.object(sampling.time, "sleep") as sleep,
                    self.assertRaises(TimeoutError),
                ):
                    sampling.tree(101, timeout=0.5)
                self.assertEqual(sampler.call.call_count, 1)
                sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Native process sampling requires macOS.")
    def test_native_library_can_sample_this_process_without_ps(self):
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("ps must not run")),
            patch.object(subprocess, "check_output", side_effect=AssertionError("ps must not run")),
        ):
            rows = sampling.tree(os.getpid())
        self.assertGreater(rows[os.getpid()][0], 0)


class DarwinIdentityTests(unittest.TestCase):
    def test_cleanup_rejects_missing_or_mismatched_birth_identity(self):
        for count, returned_pid, seconds, micros in (
            (135, 102, 30, 7),
            (136, 999, 30, 7),
            (136, 102, 0, 7),
            (136, 102, 30, 1000000),
        ):
            with self.subTest(count=count, pid=returned_pid, seconds=seconds, micros=micros):

                def info(
                    pid,
                    flavor,
                    arg,
                    buffer,
                    size,
                    *,
                    returned_pid=returned_pid,
                    seconds=seconds,
                    micros=micros,
                    count=count,
                ):
                    row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
                    row.pid, row.started_seconds, row.started_microseconds = (
                        returned_pid,
                        seconds,
                        micros,
                    )
                    return count

                backend = sampling._DarwinProcesses(
                    SimpleNamespace(proc_pidinfo=Mock(side_effect=info), proc_listpids=Mock())
                )
                with self.assertRaises((OSError, ValueError)):
                    backend.identities([102])

    def test_cleanup_reads_bsd_birth_identity_without_requesting_task_memory(self):
        def info(pid, flavor, arg, buffer, size):
            self.assertEqual((flavor, arg, size), (3, 0, 136))
            row = ctypes.cast(buffer, ctypes.POINTER(sampling._ProcBsdInfo)).contents
            row.pid, row.started_seconds, row.started_microseconds = pid, 30, 7
            return 136

        library = SimpleNamespace(proc_pidinfo=Mock(side_effect=info), proc_listpids=Mock())
        backend = sampling._DarwinProcesses(library)
        self.assertEqual(backend.identities([102]), {102: "30:7"})
