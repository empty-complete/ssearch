"""Owner-explicit UNC reads; pinned handles keep paths confined during each read."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
import os
from pathlib import Path

from atom_search.runtime import _windows

from ..contracts import UpdateError, VerifiedManifest
from ..manifest import POLICY, _path


class _FileInfo(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME), ('accessed', w.FILETIME),
                ('written', w.FILETIME), ('volume', w.DWORD), ('size_high', w.DWORD),
                ('size_low', w.DWORD), ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]


class _FileIdInfo(c.Structure):
    _fields_ = [('volume', c.c_ulonglong), ('identity', c.c_ubyte * 16)]


def _kernel():
    if os.name != 'nt':
        raise UpdateError('update_platform')
    return c.WinDLL('kernel32', use_last_error=True)


def _call(dll, name, args, result=w.BOOL):
    value = getattr(dll, name)
    value.argtypes, value.restype = args, result
    return value


def _close(handle):
    _call(_kernel(), 'CloseHandle', [w.HANDLE])(handle)


def _final_path(handle):
    buffer = c.create_unicode_buffer(32768)
    length = _call(_kernel(), 'GetFinalPathNameByHandleW', [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD)(
        handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise UpdateError('update_path')
    text = buffer.value
    if text.startswith('\\\\?\\UNC\\'):
        text = '\\\\' + text[8:]
    elif text.startswith('\\\\?\\'):
        text = text[4:]
    return Path(text)


def _same_path(left, right):
    return os.path.normpath(str(left)).casefold() == os.path.normpath(str(right)).casefold()


def _file_id(handle):
    identity = _FileIdInfo()
    if not _call(_kernel(), 'GetFileInformationByHandleEx', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])(
            handle, 18, c.byref(identity), c.sizeof(identity)) or not any(identity.identity):
        raise UpdateError('update_path')
    return identity.volume, bytes(identity.identity)


def _assert_private_handle(handle):
    descriptor = c.c_void_p()
    code = _windows._api(_windows._dll('advapi32'), 'GetSecurityInfo',
        [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)],
        w.DWORD)(handle, 1, 1 | 4, None, None, None, None, c.byref(descriptor))
    if code:
        raise UpdateError('update_path')
    try:
        _windows._assert_descriptor_private(descriptor)
    finally:
        _windows._free(descriptor)


def _dispose(handle):
    pending = c.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile (BOOLEAN).
    if not _call(_kernel(), 'SetFileInformationByHandle', [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])(
            handle, 4, c.byref(pending), c.sizeof(pending)):
        raise UpdateError('update_path')


def _open_handle(path, *, directory=False, write=False, delete=False):
    _windows.reject_reparse(path)
    access = 0x80 if directory else (0x40000080 if write else 0x80000000)
    if delete:
        access = 0x80 | 0x20000 | 0x10000  # attributes + READ_CONTROL + DELETE.
    handle = _call(_kernel(), 'CreateFileW',
                   [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)(
        str(path), access,
        3 if directory else 1, None, 1 if write else 3,
        0x00200000 | (0x02000000 if directory else 0x08000000), None)
    if not handle or handle == c.c_void_p(-1).value:
        raise UpdateError('update_path' if write else 'update_read')
    try:
        info = _FileInfo()
        if not _call(_kernel(), 'GetFileInformationByHandle', [w.HANDLE, c.POINTER(_FileInfo)])(handle, c.byref(info)):
            raise UpdateError('update_path')
        if bool(info.attributes & 0x10) != directory or info.attributes & 0x400:
            raise UpdateError('update_path')
        if not directory and (info.links != 1 or _call(_kernel(), 'GetFileType', [w.HANDLE], w.DWORD)(handle) != 1):
            raise UpdateError('update_path')
        return handle
    except BaseException:
        _close(handle)
        raise


class _PinnedFile:
    def __init__(self, stream, pins, *, owner=None):
        self._stream, self._pins, self._owner = stream, pins, owner
        self.name = Path(stream.name) if isinstance(stream.name, str) else None

    def read(self, size=-1): return self._stream.read(size)
    def write(self, raw): return self._stream.write(raw)
    def seek(self, offset, whence=0): return self._stream.seek(offset, whence)
    def tell(self): return self._stream.tell()
    def fileno(self): return self._stream.fileno()
    def flush(self): return self._stream.flush()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def close(self):
        try:
            self._stream.close()
        finally:
            for handle in reversed(self._pins):
                _close(handle)
            self._pins = []
            if self._owner is not None:
                self._owner.close()
                self._owner = None


class _PinnedTree:
    """A retained directory plus retained ancestors for each leaf handle.

    No handle shares deletion. A leaf is opened without following a final
    reparse point, checked by actual file identity/type/link count and final
    path, and only then converted to a Python stream. Source leaves deny writes.
    """

    def __init__(self, root, *, on_create=None, delete_root=False):
        self._ancestors, self._on_create, self._handle = [], on_create, None
        # Do not resolve a pathname and then promote its potentially swapped
        # final target to authority. Pin the explicit anchor, then every child
        # under its already pinned canonical parent, without following reparse
        # points. Ancestors remain held for this tree's entire lifetime.
        requested = _windows.reject_reparse(Path(root))
        text = str(requested)
        if text.startswith('\\\\?\\UNC\\'):
            text = '\\\\' + text[8:]
        elif text.startswith('\\\\?\\'):
            text = text[4:]
        requested = Path(text)
        pins = []
        try:
            if not requested.anchor:
                raise UpdateError('update_path')
            anchor = Path(requested.anchor)
            first = _open_handle(anchor, directory=True)
            pins.append(first)
            current = _final_path(first)
            if not _same_path(current, anchor):
                raise UpdateError('update_path')
            parts = requested.parts[1:]
            for index, part in enumerate(parts):
                child = _open_handle(current / part, directory=True,
                                     delete=delete_root and index == len(parts) - 1)
                pins.append(child)
                actual = _final_path(child)
                if not _same_path(actual.parent, current):
                    raise UpdateError('update_path')
                current = actual
            if delete_root and not parts:
                raise UpdateError('update_path')
            self.directory = current
            self._handle, self._ancestors = pins[-1], pins[:-1]
        except BaseException:
            for handle in reversed(pins):
                _close(handle)
            raise

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def close(self):
        if self._handle is not None:
            _close(self._handle)
            self._handle = None
        for handle in reversed(self._ancestors):
            _close(handle)
        self._ancestors = []

    def _directory(self, path, *, exclusive=False):
        with _windows._security(directory=True) as security:
            created = _call(_kernel(), 'CreateDirectoryW',
                            [w.LPCWSTR, c.POINTER(_windows._SecurityAttributes)])(str(path), c.byref(security))
            if not created and (exclusive or c.get_last_error() != 183):
                raise UpdateError('update_path')
        _windows.assert_private(path)
        return bool(created)

    def subtree(self, relative, *, exclusive=False):
        parts = _path(relative).split('/')
        current, pins = self.directory, []
        try:
            for index, part in enumerate(parts):
                current = current / part
                created = self._directory(current, exclusive=exclusive and index == len(parts) - 1)
                handle = _open_handle(current, directory=True)
                pins.append(handle)
                if not _same_path(_final_path(handle), current):
                    raise UpdateError('update_path')
                if created and self._on_create is not None:
                    self._on_create(current, _file_id(handle))
            result = object.__new__(_PinnedTree)
            result._handle, result._ancestors = pins[-1], pins[:-1]
            result._on_create, result.directory = self._on_create, _final_path(pins[-1])
            return result
        except BaseException:
            for pin in reversed(pins):
                _close(pin)
            raise

    def open_file(self, relative, *, write=False):
        import msvcrt
        parts = _path(relative).split('/')
        current, pins, handle = self.directory, [], None
        try:
            for part in parts[:-1]:
                current = current / part
                created = False
                if write:
                    created = self._directory(current)
                child = _open_handle(current, directory=True)
                pins.append(child)
                if not _same_path(_final_path(child), current):
                    raise UpdateError('update_path')
                if created and self._on_create is not None:
                    self._on_create(current, _file_id(child))
            path = current / parts[-1]
            handle = _open_handle(path, write=write)
            if not _same_path(_final_path(handle), path):
                raise UpdateError('update_path')
            descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_WRONLY if write else os.O_RDONLY))
            handle = None  # The descriptor owns it from this point.
            try:
                stream = os.fdopen(descriptor, 'wb' if write else 'rb', buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            return _PinnedFile(stream, pins)
        except BaseException:
            if handle is not None:
                _close(handle)
            for pin in reversed(pins):
                _close(pin)
            raise

    @contextmanager
    def retained_delete(self, relative, *, directory=False, expected_parents):
        parts = _path(relative).split('/')
        current, pins = self.directory, []
        try:
            for part in parts[:-1]:
                current = current / part
                handle = _open_handle(current, directory=True)
                pins.append(handle)
                if (not _same_path(_final_path(handle), current)
                        or _file_id(handle) != expected_parents.get(current)):
                    raise UpdateError('update_path')
            path = current / parts[-1]
            handle = _open_handle(path, directory=directory, delete=True)
            pins.append(handle)
            if not _same_path(_final_path(handle), path):
                raise UpdateError('update_path')
            yield handle
        finally:
            for handle in reversed(pins):
                _close(handle)


class UNCTransport:
    def __init__(self, root: Path, *, fixture_mode: bool = False):
        self.source_root = Path(root).absolute()
        text = str(self.source_root)
        unc = text.startswith('\\\\') and not text.startswith(('\\\\?\\', '\\\\.\\'))
        unc = unc or text.startswith('\\\\?\\UNC\\')
        if type(fixture_mode) is not bool or (not unc and not fixture_mode):
            raise UpdateError('update_source')
        self.fixture_mode = fixture_mode

    def _open(self, relative):
        tree = None
        try:
            tree = _PinnedTree(self.source_root)
            stream = tree.open_file(relative)
            stream._owner = tree
            return stream
        except (OSError, _windows.WindowsError):
            if tree is not None:
                tree.close()
            raise UpdateError('update_path') from None
        except BaseException:
            if tree is not None:
                tree.close()
            raise

    @staticmethod
    def _artifact(manifest, identity):
        if not isinstance(manifest, VerifiedManifest) or type(identity) is not str:
            raise UpdateError('update_manifest')
        for artifact in manifest.artifacts:
            if artifact.id == identity:
                return artifact
        raise UpdateError('update_manifest')

    def _bounded(self, relative, limit, maximum):
        if type(limit) is not int or not 0 <= limit <= maximum:
            raise UpdateError('update_limit')
        try:
            with self._open(relative) as stream:
                parts, remaining = [], limit + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
                raw = b''.join(parts)
            if len(raw) > limit:
                raise UpdateError('update_limit')
            return raw
        except OSError:
            raise UpdateError('update_read') from None

    def read_manifest(self, *, max_bytes):
        return self._bounded('update-manifest.json', max_bytes, POLICY.manifest_bytes)

    def read_inventory(self, manifest, artifact_id, *, max_bytes):
        reference = self._artifact(manifest, artifact_id).inventory
        if reference is None:
            raise UpdateError('update_inventory')
        return self._bounded(reference.relative_path, max_bytes, POLICY.inventory_bytes)

    def open_artifact(self, manifest, artifact_id):
        return self._open(self._artifact(manifest, artifact_id).relative_path)
