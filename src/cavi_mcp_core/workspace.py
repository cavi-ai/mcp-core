"""The workspace root and the path rules every server's tools follow.

Standard library only, and nothing else from this package is imported, so a server can
keep a byte-compatible copy for code that runs inside a host application's own Python.
"""

import os
import shutil
import stat
import sys
import warnings
from contextlib import contextmanager
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


def _unlock(tree):
    """An rmtree error handler: clear read-only bits, then retry once.

    Windows refuses to delete read-only files, and a directory without its write bit
    refuses to give up its entries. Only entries inside ``tree`` are touched, and never
    through a symbolic link.
    """
    tree = Path(tree)

    def handler(function, target, _error):
        path = Path(target)
        try:
            if not path.is_symlink():
                os.chmod(path, stat.S_IRWXU if path.is_dir() else stat.S_IRUSR | stat.S_IWUSR)
            parent = path.parent
            if parent != path and (parent == tree or tree in parent.parents):
                os.chmod(parent, stat.S_IRWXU)
            function(target)
        except OSError:
            pass

    return handler


def remove_tree(path) -> list[str]:
    """Remove a directory tree; return what is still there afterwards (empty on success)."""
    path = Path(path)
    try:
        # A host application's own Python can predate 3.12, which added ``onexc``.
        if sys.version_info >= (3, 12):  # noqa: UP036
            shutil.rmtree(path, onexc=_unlock(path))
        else:
            shutil.rmtree(path, onerror=_unlock(path))
    except OSError:
        pass
    if not path.exists():
        return []
    try:
        left = sorted((str(item) for item in path.rglob('*')), reverse=True)
    except OSError:
        left = []
    return [*left, str(path)]


# How many leftover entries an error message names before it counts the rest.
LEFTOVERS_SHOWN = 10


def _listing(left):
    more = len(left) - LEFTOVERS_SHOWN
    return ', '.join(left[:LEFTOVERS_SHOWN]) + (f' and {more} more' if more > 0 else '')


def remove_scratch(path) -> None:
    """``remove_tree`` for a scratch directory with no error to carry a report: anything
    still there afterwards is reported as a ``RuntimeWarning``."""
    left = remove_tree(path)
    if left:
        warnings.warn(f'could not remove {path}: {_listing(left)}', RuntimeWarning, stacklevel=2)


@contextmanager
def new_output_directory(workspace, name, *, parents=False):
    """``new_output_dir`` for an operation that fills the directory, removed again if the
    body raises.

    Whatever ``BaseException`` ends the body, cancellation included, the directory and
    everything in it are removed before the error continues. A directory that already
    existed is refused before the body runs, so it is never touched.

    Leftovers that cannot be removed are reported with the error. An ``Exception`` is
    replaced by a ``RuntimeError`` (chained to the original) whose text names them; any
    other ``BaseException`` is re-raised as it is, with the leftovers as a note.
    """
    path = new_output_dir(workspace, name, parents=parents)
    try:
        yield path
    except BaseException as exc:
        left = remove_tree(path)
        if not left:
            raise
        note = f'could not remove the incomplete output: {_listing(left)}'
        if not isinstance(exc, Exception):
            exc.add_note(note)
            raise
        reason = str(exc) if isinstance(exc, RuntimeError) else f'{type(exc).__name__}: {exc}'
        raise RuntimeError(f'{reason}; {note}') from exc
