"""Validate and materialize only fictional manifest-owned TXT in host staging.

No source registry or service owner is created here. The host must register
owned_base in the existing protected owned-exclusions seam before work scans.
Cleanup enumerates only this session's receipts, never an arbitrary directory.
"""
from dataclasses import dataclass, field
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import threading
import unicodedata
import uuid

from .contracts import CleanupResult, DemoSession
from .demo_provider import DemoSearchProvider


_MAX_BYTES = 65536
_FILE_COUNT = 8
_sessions_lock = threading.RLock()
_sessions = {}


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _check_chain(path, *, directory=False):
    """Reject every symlink/reparse ancestor, including junctions on Windows."""
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if _linked(info) or item != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('unsafe_demo_path')
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError('unsafe_demo_path')
    return info


def _read_file(path):
    before = _check_chain(path)
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= _MAX_BYTES:
        raise ValueError('unsafe_demo_path')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('unsafe_demo_path')
        data = stream.read(_MAX_BYTES + 1)
    after = _check_chain(path)
    if (len(data) > _MAX_BYTES or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('unsafe_demo_path')
    return data, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, hashlib.sha256(data).digest())


def _relative(value, *, txt=False):
    if (type(value) is not str or not value or len(value) > 1024 or '\\' in value
            or value.startswith('/') or ntpath.splitdrive(value)[0]):
        raise ValueError('invalid_demo_manifest')
    parts = value.split('/')
    for part in parts:
        normalized = unicodedata.normalize('NFKC', part)
        stem = normalized.split('.')[0].upper()
        if (not part or part in ('.', '..') or normalized.endswith((' ', '.'))
                or any(ord(character) < 32 or character in '<>:"|?*' for character in normalized)
                or stem in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                            *(f'LPT{i}' for i in range(1, 10))}):
            raise ValueError('invalid_demo_manifest')
    if txt and not value.endswith('.txt'):
        raise ValueError('invalid_demo_manifest')
    return value


def _manifest(path):
    try:
        raw, _ = _read_file(path)
        manifest = json.loads(raw.decode('utf-8'))
        if (type(manifest) is not dict or type(manifest['version']) is not int or manifest['version'] != 2
                or manifest['fictional'] is not True or manifest['label'] != 'Учебные данные'
                or manifest['source_id'] != 'onboarding-demo'
                or manifest['separate_temporary_index_required'] is not True
                or manifest['merge_with_working_index'] is not False
                or type(manifest['files']) is not list or len(manifest['files']) != _FILE_COUNT):
            raise ValueError()
        files, seen = [], set()
        for relative in manifest['files']:
            _relative(relative, txt=True)
            key = unicodedata.normalize('NFKC', relative).casefold()
            if key in seen:
                raise ValueError()
            seen.add(key)
            data, _ = _read_file(path.parent.joinpath(*relative.split('/')))
            text = data.decode('utf-8')
            if 'Учебные данные.' not in text or 'Вымышленный' not in text:
                raise ValueError()
            files.append((relative, data))
        projects = manifest['projects']
        if type(projects) is not list or not 1 <= len(projects) <= _FILE_COUNT:
            raise ValueError()
        project_names = {}
        for project in projects:
            identity, label = project['id'], project['label']
            if (type(identity) is not str or not re.fullmatch(r'[a-z0-9-]{1,64}', identity)
                    or identity in project_names or type(label) is not str or not 1 <= len(label) <= 128
                    or project['fictional'] is not True or type(project['path_prefixes']) is not list
                    or not 1 <= len(project['path_prefixes']) <= _FILE_COUNT):
                raise ValueError()
            project_names[identity] = label
            for prefix in project['path_prefixes']:
                _relative(prefix)
                if not any(relative.startswith(prefix + '/') for relative, _ in files):
                    raise ValueError()
        aliases = manifest['project_aliases']
        if (aliases['silent_ambiguous_expansion_allowed'] is not False
                or type(aliases['entries']) is not list or len(aliases['entries']) > 64):
            raise ValueError()
        for alias in aliases['entries']:
            if (type(alias['alias']) is not str or not 1 <= len(alias['alias']) <= 128
                    or alias['scope']['source_id'] != manifest['source_id']
                    or alias['scope']['project_id'] not in project_names
                    or alias['expansion'] != project_names[alias['scope']['project_id']]):
                raise ValueError()
        lessons = manifest['lessons']
        if type(lessons) is not list or not 1 <= len(lessons) <= 64:
            raise ValueError()
        for lesson in lessons:
            if (type(lesson['query']) is not str or not 1 <= len(lesson['query']) <= 512
                    or any(relative not in manifest['files'] for relative in lesson.get('expected_files', ()))
                    or 'selected_file' in lesson and lesson['selected_file'] not in manifest['files']):
                raise ValueError()
        return manifest, tuple(files)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('invalid_demo_manifest') from None


def _mkdir(path):
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError('unsafe_demo_path')
        missing.append(current)
        current = current.parent
    _check_chain(current, directory=True)
    for directory in reversed(missing):
        directory.mkdir()
        _check_chain(directory, directory=True)


@dataclass
class _OwnedSession:
    root: Path
    files: dict = field(default_factory=dict)
    directories: dict = field(default_factory=dict)
    provider: DemoSearchProvider | None = None

    def verify_parent(self, path):
        if not path.is_relative_to(self.root):
            raise ValueError('unsafe_demo_path')
        for directory in (self.root, *(parent for parent in reversed(path.parents)
                                      if parent != self.root and parent.is_relative_to(self.root))):
            info = _check_chain(directory, directory=True)
            if self.directories.get(directory) != (info.st_dev, info.st_ino):
                raise ValueError('unsafe_demo_path')

    def unchanged(self, relative):
        try:
            self.verify_parent(self.root.joinpath(*relative.split('/')))
            _, fingerprint = _read_file(self.root.joinpath(*relative.split('/')))
            return fingerprint == self.files.get(relative)
        except (OSError, ValueError):
            return False


def _cleanup(owned):
    removed, deferred, preserved = [], [], []
    for relative in tuple(owned.files):
        path = owned.root.joinpath(*relative.split('/'))
        try:
            owned.verify_parent(path)
            _, fingerprint = _read_file(path)
            if fingerprint != owned.files[relative]:
                preserved.append(path)
                continue
            # Recheck ancestors immediately before unlink, including junctions.
            owned.verify_parent(path)
            _check_chain(path)
            path.unlink()
            removed.append(path)
            del owned.files[relative]
        except FileNotFoundError:
            del owned.files[relative]
        except PermissionError:
            deferred.append(path)
        except OSError:
            deferred.append(path)
        except ValueError:
            preserved.append(path)
    for directory in sorted(owned.directories, key=lambda path: len(path.parts), reverse=True):
        try:
            owned.verify_parent(directory / '_cleanup')
            directory.rmdir()  # Empty directories only; unknown files always survive.
        except (OSError, ValueError):
            pass
    return CleanupResult(tuple(removed), tuple(deferred), tuple(preserved))


def open_demo_session(manifest_path: Path, owned_base: Path, capabilities: tuple[str, ...]) -> DemoSession:
    """Host-only explicit demo creation, with no work settings or owner authority."""
    if type(capabilities) is not tuple or any(type(item) is not str for item in capabilities):
        raise ValueError('invalid_demo_capabilities')
    manifest_path = Path(os.path.abspath(manifest_path))
    manifest, files = _manifest(manifest_path)
    owned_base = Path(os.path.abspath(owned_base))
    if str(owned_base).startswith(('\\\\', '//')):
        raise ValueError('unsafe_demo_path')
    _mkdir(owned_base)
    session_id = str(uuid.uuid4())
    root = owned_base / ('v2-' + session_id)
    root.mkdir()  # Exclusive, never adopt or overwrite an existing user directory.
    owned = _OwnedSession(root)
    root_info = _check_chain(root, directory=True)
    owned.directories[root] = (root_info.st_dev, root_info.st_ino)
    try:
        for relative, data in files:
            owned.verify_parent(root / '_create')
            target = root.joinpath(*relative.split('/'))
            _mkdir(target.parent)
            for directory in target.parents:
                if directory.is_relative_to(root):
                    info = _check_chain(directory, directory=True)
                    previous = owned.directories.setdefault(directory, (info.st_dev, info.st_ino))
                    if previous != (info.st_dev, info.st_ino):
                        raise ValueError('unsafe_demo_path')
            owned.verify_parent(target)
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                 | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            _, owned.files[relative] = _read_file(target)
        owned.provider = DemoSearchProvider(root, manifest, capabilities, validate_file=owned.unchanged)
    except Exception:
        _cleanup(owned)
        raise
    with _sessions_lock:
        _sessions[session_id] = owned
    return DemoSession(session_id, 2, root, owned.provider, owned.provider.capabilities)


def close_demo_session(session_id: str) -> CleanupResult:
    with _sessions_lock:
        owned = _sessions.get(session_id)
        if owned is None:
            return CleanupResult()
        owned.provider.close()
        result = _cleanup(owned)
        # Keep busy receipts for an explicit cleanup retry. Unknown/changed files
        # retain their directory and never become another session's owned files.
        if not result.deferred:
            _sessions.pop(session_id, None)
        return result
