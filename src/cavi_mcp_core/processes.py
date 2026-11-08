"""Checked identities and cleanup for explicitly registered POSIX groups."""

import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def birth(pid):
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        except FileNotFoundError:
            return None
        if len(fields) < 20:
            raise OSError("Incomplete process identity")
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return "linux:" + boot + ":" + fields[19]
    if sys.platform == "darwin":
        from . import sampling

        return sampling.birth(pid)
    raise OSError("Native supervision supports Linux and macOS only")


def _ps(*args, timeout=3):
    result = subprocess.run(
        ["ps", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode not in (0, 1) or result.stderr.strip() or len(result.stdout) > 2097152:
        raise OSError("Cannot establish complete process inventory")
    return result.stdout


def available_kib():
    if sys.platform.startswith("linux"):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    elif sys.platform == "darwin":
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3, check=True)
        lines = result.stdout.splitlines()
        match = re.search(r"page size of (\d+) bytes", lines[0])
        rows = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        if match and all(
            key in rows for key in ("Pages free", "Pages inactive", "Pages speculative")
        ):
            return (
                sum(
                    int(rows[key].strip().removesuffix("."))
                    for key in ("Pages free", "Pages inactive", "Pages speculative")
                )
                * int(match[1])
                // 1024
            )
    raise OSError("Cannot establish available host memory")


def inspect(pid, *, timeout=3):
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("Use a positive finite process inspection timeout")
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Process inspection timed out")
        return value

    identity = birth(pid)
    remaining()
    if identity is None:
        return None
    text = _ps("-p", str(pid), "-o", "ppid=,pgid=,rss=", timeout=remaining())
    remaining()
    if not text.strip():
        vanished = birth(pid) is None
        remaining()
        if vanished:
            return None
        raise OSError("Process disappeared from its inventory")
    fields = text.split()
    if len(fields) != 3 or any(not f.isdecimal() for f in fields):
        raise OSError("Invalid process inventory")
    after = birth(pid)
    remaining()
    if after is None:
        return None
    if after != identity:
        raise ValueError("Process identity changed during inspection")
    return {
        "pid": pid,
        "parent_pid": int(fields[0]),
        "pgid": int(fields[1]),
        "rss_kib": int(fields[2]),
        "birth": identity,
    }


def group(pgid):
    result = {}
    for line in _ps("-ax", "-o", "pid=,pgid=").splitlines():
        fields = line.split()
        if len(fields) != 2 or any(not f.isdecimal() for f in fields):
            raise OSError("Invalid process group inventory")
        pid, number = map(int, fields)
        if number == pgid:
            row = inspect(pid)
            if row is not None and row["pgid"] == pgid:
                result[pid] = row
    return result


def _group_members(pgid, identity, known):
    leader = inspect(pgid)
    if leader is not None and (leader["birth"] != identity or leader["pgid"] != pgid):
        raise ValueError("Registered process identity changed or left its group")
    rows = group(pgid)
    anchors = {
        pgid: identity,
        **{pid: r["birth"] for pid, r in known.items() if r["pgid"] == pgid},
    }
    if rows and not any(anchors.get(pid) == row["birth"] for pid, row in rows.items()):
        raise ValueError("Process group has no surviving checked identity")
    return rows


def _members(groups, known, scope):
    result, transfers = {}, {}
    for pgid, identity in groups.items():
        result.update(_group_members(pgid, identity, known))
    # A sampled member can leave its group without going through registration.
    # Its earlier checked identity remains an obligation, not proof of exit.
    for pid, previous in known.items():
        if previous["pgid"] not in groups or pid in result:
            continue
        row = inspect(pid)
        if row is None or row["birth"] != previous["birth"]:
            continue
        destination = row["pgid"]
        if destination != previous["pgid"] and destination in scope:
            covered = _group_members(destination, scope[destination], known)
            if covered.get(pid, {}).get("birth") == previous["birth"]:
                transfers[pid] = covered[pid]
                continue
        raise ValueError(
            f"Previously observed process {pid} ({row['birth']}) remains outside "
            f"registered group {previous['pgid']}; current group {row['pgid']}"
        )
    return result, transfers


def members(groups, known):
    result, transfers = _members(groups, known, groups)
    result.update(transfers)
    return result


def cleanup(groups, *, known=None, seconds=5):
    known = dict(known or {})
    pending, errors = dict(groups), {}
    for number in (signal.SIGTERM, signal.SIGKILL):
        for pgid, identity in list(pending.items()):
            try:
                rows, transfers = _members({pgid: identity}, known, groups)
                known.update(rows)
                known.update(transfers)
                if not rows:
                    pending.pop(pgid)
                else:
                    os.killpg(pgid, number)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors[pgid] = str(exc)
                # A failed census must not prevent retiring a still-checked group.
                # Direct birth/PGID checks avoid depending on that failed census.
                anchors = {
                    pgid: identity,
                    **{pid: r["birth"] for pid, r in known.items() if r["pgid"] == pgid},
                }
                for pid, expected in anchors.items():
                    try:
                        if birth(pid) == expected and os.getpgid(pid) == pgid:
                            os.killpg(pgid, signal.SIGKILL)
                            break
                    except (OSError, ValueError, subprocess.SubprocessError) as failure:
                        errors[pgid] += "; " + str(failure)
        deadline = time.monotonic() + seconds
        while pending and time.monotonic() < deadline:
            for pgid, identity in list(pending.items()):
                try:
                    rows, transfers = _members({pgid: identity}, known, groups)
                    known.update(rows)
                    known.update(transfers)
                    if not rows:
                        pending.pop(pgid)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    errors[pgid] = str(exc)
            time.sleep(0.05)
    if errors:
        raise RuntimeError("Unresolved process cleanup: " + str(errors))
    if pending:
        raise RuntimeError("Registered process groups have not exited")
