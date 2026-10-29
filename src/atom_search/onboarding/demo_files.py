"""Validate fictional TXT and own files through retained Windows handles.

The host must register owned_base in the protected owned-exclusions seam before
work scans. This module neither creates an owner nor writes work settings.
Non-Windows lifecycle is deliberately unsupported until it has equivalent
race-safe ownership proof; the pure memory search ports remain portable.
"""
from dataclasses import dataclass, field
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import threading
import unicodedata
import uuid

from .contracts import CleanupResult, DemoSession
from .demo_provider import DemoSearchProvider


_FILE_COUNT = 8
_sessions_lock = threading.RLock()
_sessions = {}


def _backend():
    if os.name != 'nt':
        raise RuntimeError('unsupported_demo_platform')
    from ._windows_files import WindowsFiles
    return WindowsFiles()


def _read_file(path):
    return _backend().read_file(path, hashlib.sha256)

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


@dataclass
class _OwnedSession:
    root: Path
    files: dict = field(default_factory=dict)
    directories: dict = field(default_factory=dict)
    provider: DemoSearchProvider | None = None

    def unchanged(self, relative):
        try:
            _, fingerprint = _backend().read_file(self.root.joinpath(*relative.split('/')), hashlib.sha256,
                                                  receipts=self.directories, owned_root=self.root)
            return fingerprint == self.files.get(relative)
        except (OSError, ValueError):
            return False


def _cleanup(owned):
    removed, deferred, preserved = [], [], []
    backend = _backend()
    for relative in tuple(owned.files):
        path = owned.root.joinpath(*relative.split('/'))
        try:
            backend.delete_file(path, owned.files[relative], owned.directories, owned.root, hashlib.sha256)
            removed.append(path)
            del owned.files[relative]
        except FileNotFoundError:
            del owned.files[relative]
        except OSError:
            deferred.append(path)
        except ValueError:
            preserved.append(path)
    for directory in sorted(owned.directories, key=lambda path: len(path.parts), reverse=True):
        try:
            backend.delete_directory(directory, owned.directories, owned.root)
        except (OSError, ValueError):
            pass  # Unknown entries, occupied directories and replacements survive.
    return CleanupResult(tuple(removed), tuple(deferred), tuple(preserved))


def open_demo_session(manifest_path: Path, owned_base: Path, capabilities: tuple[str, ...]) -> DemoSession:
    """Host-only explicit demo creation; supported only by the Windows adapter."""
    if type(capabilities) is not tuple or any(type(item) is not str for item in capabilities):
        raise ValueError('invalid_demo_capabilities')
    backend = _backend()
    manifest_path = Path(os.path.abspath(manifest_path))
    manifest, files = _manifest(manifest_path)
    owned_base = Path(os.path.abspath(owned_base))
    if str(owned_base).startswith(('\\\\', '//')):
        raise ValueError('unsafe_demo_path')
    session_id = str(uuid.uuid4())
    root = owned_base / ('v2-' + session_id)
    owned = _OwnedSession(root)
    try:
        for relative, data in files:
            target = root.joinpath(*relative.split('/'))
            owned.files[relative] = backend.create_file(target, data, owned.directories, root, hashlib.sha256)
        owned.provider = DemoSearchProvider(root, manifest, capabilities, validate_file=owned.unchanged,
            file_stats={relative: (receipt[1], (receipt[2] - 116444736000000000) * 100)
                        for relative, receipt in owned.files.items()})
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
        if not result.deferred:
            _sessions.pop(session_id, None)
        return result