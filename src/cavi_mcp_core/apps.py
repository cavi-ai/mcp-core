"""Find installed applications, ask their version and whether they are running.

Host side only. A lookup by location follows the same order everywhere:

1. an environment override, which is final: a wrong override is reported as not found or
   as the wrong application, never replaced;
2. the default install location the caller passes;
3. ``shutil.which`` on ``PATH``.

``bundle`` finds a macOS application by its bundle identifier instead, and ``processes``
lists the processes running one executable.
"""

import contextlib
import csv
import io
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from . import proc
from .contracts import OperationRejected

# Seconds ``<program> --version`` may take. The probe runs before the work it serves, so it
# is short: a program that does not answer in time is reported without a version.
VERSION_TIMEOUT = 5.0
VERSION_PATTERNS = (re.compile(r"version\s+(\S+)", re.IGNORECASE),
                    re.compile(r"\b(\d+\.\d+(?:\.\d+)*[\w.+-]*)"))
PROBE_TIMEOUT = 10
# Where macOS installs applications.
APPLICATIONS = Path("/Applications")

_VERSIONS: dict[tuple[str, int], str | None] = {}


class Found(NamedTuple):
    path: Path | None  # the located file or directory; None when nothing was found
    source: str        # the rule that found it: an environment variable, "default" or "PATH"


class Bundle(NamedTuple):
    path: Path        # the .app bundle
    info: dict        # its Info.plist
    executable: Path  # its main executable
    source: str       # the environment variable that named it, or "default"


class Process(NamedTuple):
    pid: int
    started: str  # the start time as ``ps -o lstart`` prints it


class AppUnavailable(OperationRejected):
    """The application is not installed, or not where an override says."""

    def __init__(self, message: str, code: str = "APP_UNAVAILABLE"):
        super().__init__(message, code)


class AppMismatch(OperationRejected):
    """The application at the path is not the one asked for."""

    def __init__(self, message: str, code: str = "APP_IDENTITY_MISMATCH"):
        super().__init__(message, code)


def platform_name() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def bundle_of(path) -> Path | None:
    """The macOS ``.app`` bundle that contains ``path`` (or is it), if any."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.suffix.lower() == ".app":
            return candidate
    return None


def bundle_info(app) -> dict:
    """A macOS bundle's Info.plist; empty when it is missing or unreadable."""
    try:
        with (Path(app) / "Contents" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def bundle_executable(app) -> Path | None:
    """The main executable inside a macOS bundle, named by its Info.plist."""
    app = Path(app)
    macos = app / "Contents" / "MacOS"
    if not macos.is_dir():
        return None
    name = bundle_info(app).get("CFBundleExecutable")
    candidate = macos / (name if isinstance(name, str) and name else app.stem)
    return candidate if candidate.is_file() else None


def tool(directory, name: str) -> Path | None:
    """``name`` as an executable inside ``directory`` (``.exe``, ``.cmd`` ... on Windows)."""
    found = shutil.which(name, path=str(directory))
    return Path(found) if found else None


def find_directory(default: Path, probe: str, env_dir: str, env_app: str) -> Found:
    """The directory holding a set of command-line tools; ``probe`` is one of them.

    When nothing is found ``default`` is returned with source ``"default"``, so a report
    can name where the tools were expected.
    """
    if os.environ.get(env_dir):
        return Found(Path(os.environ[env_dir]).expanduser(), env_dir)
    if os.environ.get(env_app):
        app = Path(os.environ[env_app]).expanduser()
        macos = app / "Contents" / "MacOS"
        return Found(macos if macos.is_dir() else app, env_app)
    if tool(default, probe) is not None:
        return Found(default, "default")
    on_path = shutil.which(probe)
    if on_path:
        return Found(Path(on_path).resolve().parent, "PATH")
    return Found(default, "default")


def find_app(default: Path, which: str, env_app: str | None) -> Found:
    """The application as installed: a macOS bundle, or its executable elsewhere.

    ``path`` is None when nothing was found. ``env_app`` is returned as given, whether or
    not it exists, so a wrong override is reported instead of replaced.
    """
    if env_app and os.environ.get(env_app):
        return Found(Path(os.environ[env_app]).expanduser(), env_app)
    if default.exists():
        return Found(default, "default")
    on_path = shutil.which(which)
    if on_path:
        return Found(Path(on_path), "PATH")
    return Found(None, "default")


def find_executable(default: Path, which: str, env_bin: str | None, env_app: str | None) -> Found:
    """The single executable of an application; ``path`` is None when it was not found.

    ``env_bin`` names an exact executable (a directory is completed with ``default``'s file
    name); ``env_app`` names a macOS bundle, or an executable on other platforms.
    """
    if env_bin and os.environ.get(env_bin):
        value = Path(os.environ[env_bin]).expanduser()
        if value.is_dir():
            value = value / default.name
        return Found(value if value.is_file() else None, env_bin)
    found = find_app(default, which, env_app)
    return Found(None if found.path is None else _resolve_app(found.path), found.source)


def _resolve_app(path: Path) -> Path | None:
    """A bundle's main executable, or ``path`` itself when it is already a file."""
    if path.is_dir():
        return bundle_executable(path)
    return path if path.is_file() else None


def _version_key(info: dict) -> tuple[int, ...]:
    value = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""
    return tuple(int(part) for part in re.findall(r"\d+", str(value)))


def bundle(name: str, identifier: str, pattern: str, env: str | None, root: Path = APPLICATIONS,
           path=None) -> Bundle:
    """The macOS application ``name`` with bundle ``identifier``.

    ``path``, when given, or else the override in ``env`` (None: no override), names the
    bundle to use; it must
    carry ``identifier``. Otherwise every bundle under ``root`` matching the glob ``pattern``
    is read, those with ``identifier`` are kept, and the newest version is chosen. Raises
    AppUnavailable when nothing is found and AppMismatch when the named bundle is another
    application. Bundles are read the same way on every platform; outside macOS, ``root``
    usually holds none.
    """
    named, source = ((path, "argument") if path is not None
                     else (os.environ.get(env) if env else None, env))
    if named:
        named = Path(named).expanduser().resolve()
        info = bundle_info(named)
        subject = f"{env} names {named}" if source == env else str(named)
        if not info:
            raise AppUnavailable(f"{subject}, which is not an application")
        if info.get("CFBundleIdentifier") != identifier:
            raise AppMismatch(f"{subject}, which is not {name}")
        candidates = [(named, info)]
    else:
        candidates = [(path.resolve(), info) for path in sorted(root.glob(pattern))
                      if (info := bundle_info(path)).get("CFBundleIdentifier") == identifier]
        if not candidates:
            hint = f"; set {env} to its .app" if env else ""
            raise AppUnavailable(f"{name} is not installed in {root}{hint}")
        source = "default"
    found, info = max(candidates, key=lambda item: _version_key(item[1]))
    executable = bundle_executable(found)
    if executable is None:
        raise AppUnavailable(f"{found} has no executable")
    return Bundle(found, info, executable, source)


def processes(executable) -> list[Process]:
    """The processes whose command is ``executable``, from ``ps``; macOS and Linux."""
    result = proc.run_sync(["/bin/ps", "-axo", "pid=,lstart=,comm="], PROBE_TIMEOUT)
    if result.returncode:
        raise RuntimeError(f"ps failed: {proc.diagnostics(result)}")
    matches = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=6)
        if len(parts) == 7 and parts[6] == str(executable):
            matches.append(Process(int(parts[0]), " ".join(parts[1:6])))
    return matches


def version(path, timeout: float | None = None) -> str | None:
    """The version of an installed application or tool, probed once per (path, mtime).

    Inside a macOS bundle the answer is the bundle's Info.plist, and the application is
    never run. Elsewhere it is parsed from ``<path> --version``. A failed probe is cached
    too, so an executable that cannot answer is not run again on every status call.

    The probe blocks for at most ``VERSION_TIMEOUT`` seconds; call it from a thread when
    inside an event loop. ``timeout`` shortens that for a caller with less time left, and
    cannot lengthen it. An answer that never came because ``timeout`` ran out is not
    cached, since a longer wait might have got one.
    """
    if path is None:
        return None
    path = Path(path)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _VERSIONS:
        limit = VERSION_TIMEOUT if timeout is None else min(timeout, VERSION_TIMEOUT)
        if limit <= 0:
            return None
        try:
            found = _probe_version(path, limit)
        except TimeoutError:
            if limit < VERSION_TIMEOUT:
                return None
            found = None
        _VERSIONS[key] = found
    return _VERSIONS[key]


def _probe_version(path: Path, timeout: float) -> str | None:
    """The version ``path`` reports; raises TimeoutError if it does not answer in time."""
    app = bundle_of(path) if platform_name() == "darwin" else None
    if app is not None:
        info = bundle_info(app)
        value = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
        return value if isinstance(value, str) else None
    try:
        result = proc.run_sync([path, "--version"], timeout)
    except TimeoutError:
        raise
    except OSError:
        return None
    return parse_version(result.stdout + "\n" + result.stderr)


def parse_version(text: str) -> str | None:
    for pattern in VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def running(executable) -> bool | None:
    """Whether the application whose executable this is has a process: True, False, or
    None when this machine cannot say (no ``pgrep`` or ``tasklist``, or it failed).

    ``pgrep`` on POSIX, ``tasklist`` on Windows.
    """
    if executable is None:
        return False
    path = Path(executable)
    return _running_windows(path) if sys.platform == "win32" else _running_posix(path)


def _ere(text: str) -> str:
    """Escape ``text`` for a POSIX extended regular expression."""
    return re.sub(r"([\\.\[\]{}()*+?^$|])", r"\\\1", text)


def pattern_for(path: Path) -> str:
    """A ``pgrep -f`` pattern that matches a process running the program.

    A binary must be the first argument; a ``#!`` script runs as an argument of its
    interpreter, so it may be any whole argument. Helpers that only name the application
    (``--appPath=<executable>``, ``-launchedbyvulcan <executable>``) do not count. Both the
    path as given and its symlink-resolved form are accepted.
    """
    forms = {str(path)}
    with contextlib.suppress(OSError):
        forms.add(str(path.resolve()))
        if path.is_symlink():
            # The target as written, without resolving its directories: macOS temp and
            # home folders sit behind /var and /private links, so the fully resolved form
            # can differ from the path the process was started with.
            forms.add(str(path.parent / os.readlink(path)))
    alternatives = "|".join(_ere(form) for form in sorted(forms))
    start = "(^|[[:space:]])" if _script(path) else "^"
    return f"{start}({alternatives})([[:space:]]|$)"


def _script(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def _running_posix(path: Path) -> bool | None:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return None
    try:
        found = subprocess.run([pgrep, "-f", pattern_for(path)], capture_output=True,
                               timeout=PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode == 0:
        return True
    return False if found.returncode == 1 else None


def _running_windows(path: Path) -> bool | None:
    name = path.name
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", check=True,
            timeout=PROBE_TIMEOUT,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return any(row and row[0].lower() == name.lower()
               for row in csv.reader(io.StringIO(output)))
