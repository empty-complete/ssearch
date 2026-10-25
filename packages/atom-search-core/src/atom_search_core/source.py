"""Filesystem metadata source: never opens document contents."""
import os
from pathlib import Path
import stat
from collections.abc import Iterable

from .models import DirectorySnapshot, FileMetadata
from .contracts import Cancellation


def native(path) -> str:
    value = str(path)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    return '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value


def canonical_root(root: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(root)))


def contains_root(parent, child) -> bool:
    """Lexical segment containment; never probe a filesystem or UNC share."""
    parent, child = canonical_root(str(parent)), canonical_root(str(child))
    try:
        return os.path.commonpath((parent, child)) == parent
    except ValueError:
        return False


class FileSystemSource:
    def snapshots(self, root: str, cancel: Cancellation, *,
                  excluded_roots: tuple[Path, ...] = ()) -> Iterable[DirectorySnapshot]:
        if any(contains_root(excluded, root) for excluded in excluded_roots):
            raise ValueError('owned_source_root')
        if not stat.S_ISDIR(os.stat(native(root)).st_mode):
            raise NotADirectoryError(root)
        stack = ['']
        while stack and not cancel.is_set():
            relative = stack.pop()
            directory = Path(root).joinpath(*relative.split('/')) if relative else Path(root)
            files, child_dirs, protected, errors = [], [], [], []
            complete, skipped = True, 0
            try:
                with os.scandir(native(directory)) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            complete = False
                            break
                        rel = f'{relative}/{entry.name}' if relative else entry.name
                        if any(contains_root(excluded, directory / entry.name) for excluded in excluded_roots):
                            # Owned files must also stay outside the work catalogue.
                            # Omit the branch so any former work rows are removed
                            # by this complete parent snapshot, without descent.
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            protected.append(entry.name)
                            errors.append({'path': rel, 'operation': 'metadata', 'message': str(exc),
                                'error_code': 'access_denied' if isinstance(exc, PermissionError) else 'source_unavailable'})
                            continue
                        # DFS (0x8000000A) is not a name surrogate and must be traversed.
                        linked = stat.S_ISLNK(info.st_mode) or bool(
                            getattr(info, 'st_reparse_tag', 0) & 0x20000000)
                        if linked:
                            protected.append(entry.name)
                            skipped += 1
                        elif stat.S_ISDIR(info.st_mode):
                            child_dirs.append(entry.name)
                        elif stat.S_ISREG(info.st_mode):
                            files.append(FileMetadata(rel, entry.name, info.st_size, info.st_mtime_ns))
            except OSError as exc:
                complete = False
                errors.append({'path': relative, 'operation': 'enumerate', 'message': str(exc),
                    'error_code': 'access_denied' if isinstance(exc, PermissionError) else 'source_unavailable'})
            yield DirectorySnapshot(relative, tuple(files), tuple(child_dirs), tuple(protected),
                                    complete, tuple(errors), skipped)
            for child in reversed(sorted(child_dirs, key=str.casefold)):
                stack.append(f'{relative}/{child}' if relative else child)
