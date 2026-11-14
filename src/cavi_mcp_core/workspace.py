"""The workspace root and the path rules every server's tools follow.

Standard library only, and nothing else from this package is imported, so a server can
keep a byte-compatible copy for code that runs inside a host application's own Python.
"""

import os
from pathlib import Path

INPUT_MESSAGE = 'Input must be a supported file inside the workspace'
OUTPUT_MESSAGE = 'Output must remain inside the workspace'


def root(env, explicit=None, *, default=None, create=False) -> Path:
    """The resolved workspace root: ``explicit``, else ``$env``, else ``default``.

    ``$env`` must be an absolute path. With no ``default`` the variable is required.
    Nothing is touched unless ``create`` is true. With ``create`` the default is made on
    first use, but a root chosen by argument or variable must already be a directory, so
    a mistyped path is refused instead of becoming a new empty workspace.
    """
    if explicit is not None and str(explicit) != '':
        path = Path(explicit).expanduser().resolve()
        if create:
            _require_directory(path, f'Workspace {path}', ' Create it first.')
        return path
    value = os.environ.get(env, '')
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f'{env} must be an absolute path')
        path = path.resolve()
        if create:
            hint = (' Create it first.' if default is None else
                    f' Create it, or unset {env} to use the default workspace'
                    f' {Path(default).expanduser()}.')
            _require_directory(path, f'{env} points to {path}, which', hint)
        return path
    if default is None:
        raise ValueError(f'Set {env} to the absolute workspace directory')
    path = Path(default).expanduser()
    if create:
        if path.exists() and not path.is_dir():
            raise ValueError(f'Workspace {path} is not a directory.')
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f'Cannot create the default workspace {path}: {exc}') from None
    return path.resolve()


def _require_directory(path, subject, hint=''):
    if not path.is_dir():
        state = 'is not a directory' if path.exists() else 'does not exist'
        raise ValueError(f'{subject} {state}.{hint}')


def existing(path) -> Path:
    """Resolve a workspace root that must already be a directory; never creates it."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f'Workspace {path} does not exist.') from None
    _require_directory(resolved, f'Workspace {resolved}')
    return resolved


def existing_root(env, explicit=None, *, default=None) -> Path:
    """The configured root for an operation that reads from the workspace."""
    return existing(root(env, explicit, default=default))


def input_file(workspace, name, extensions) -> Path:
    """Resolve ``name`` to an existing file inside the workspace with a supported suffix.

    Symbolic links are followed before the containment check, so a link that points out
    of the workspace is refused. ``extensions`` holds lower-case suffixes with the dot.
    """
    base = existing(workspace)
    if not isinstance(name, str) or '\x00' in name:
        raise ValueError(INPUT_MESSAGE)
    try:
        path = (base / name).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ValueError(INPUT_MESSAGE) from None
    if not path.is_relative_to(base) or not path.is_file() or path.suffix.lower() not in extensions:
        raise ValueError(INPUT_MESSAGE)
    return path


def output_path(workspace, name, *, parents=False) -> Path:
    """Validate a new output directory inside the workspace without creating it.

    It may not be the workspace itself, leave it (including through links) or exist, and
    its parent must exist unless ``parents`` is true, in which case the missing parents
    are created.
    """
    base = existing(workspace)
    if not isinstance(name, str) or '\x00' in name:
        raise ValueError(OUTPUT_MESSAGE)
    try:
        path = (base / name).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError(OUTPUT_MESSAGE) from None
    if path == base or not path.is_relative_to(base):
        raise ValueError(OUTPUT_MESSAGE)
    if parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError('Output directory already exists; choose a new directory')
    if not path.parent.is_dir():
        raise ValueError('Output parent directory must exist')
    return path


def new_output_dir(workspace, name, *, parents=False) -> Path:
    """Create the new output directory ``name``; ``mkdir`` itself refuses a concurrent creator."""
    path = output_path(workspace, name, parents=parents)
    path.mkdir()
    return path


def rel(workspace, path) -> str:
    """``path`` relative to the workspace, with forward slashes."""
    return Path(path).resolve().relative_to(existing(workspace)).as_posix()
