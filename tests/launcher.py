"""Write a Python script as a program the host can discover and run on any platform."""

import os
import sys
from pathlib import Path


def write_launcher(directory: Path, name: str, source: str, *,
                   windows: bool | None = None) -> Path:
    """Write ``source`` as the executable ``name`` in ``directory`` and return its path.

    POSIX gets a script with a shebang for this interpreter. Windows executes no shebang
    scripts, so it gets ``<name>.cmd`` that runs a sidecar script with this interpreter;
    the returned path is the ``.cmd``, which ``shutil.which`` finds through PATHEXT.
    ``windows`` forces a layout (a test can inspect the Windows files on any platform).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not (os.name == "nt" if windows is None else windows):
        launcher = directory / name
        launcher.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        launcher.chmod(0o755)
        return launcher
    script = directory / f"{name}-launcher.py"
    script.write_text(source, encoding="utf-8")
    launcher = directory / f"{name}.cmd"
    launcher.write_bytes(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n'.encode()
    )
    return launcher
