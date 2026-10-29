"""Windows-only retained-handle operations for owned onboarding files.

Each relative NtCreateFile name is one validated path component. Parent handles
remain open without write/delete sharing until the leaf operation completes.
Creation receipts and cleanup fingerprints belong to the same returned handle;
FileDispositionInfo deletes that handle, never a path checked earlier.

https://learn.microsoft.com/windows/win32/api/winternl/nf-winternl-ntcreatefile
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
"""
from contextlib import contextmanager, ExitStack
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path


_MAX_BYTES = 65536
_SYNCHRONIZE = 0x100000
_READ_ATTRIBUTES = 0x80
_WRITE_ATTRIBUTES = 0x100
_DELETE = 0x10000
_SHARE_READ = 1
_FILE_OPEN = 1
_FILE_CREATE = 2
_DIRECTORY = 1
_NON_DIRECTORY = 0x40
_SYNCHRONOUS = 0x20
_OPEN_REPARSE_POINT = 0x200000


class _UnicodeString(c.Structure):
    _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', w.LPWSTR)]


class _ObjectAttributes(c.Structure):
    _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                ('ObjectName', c.POINTER(_UnicodeString)), ('Attributes', w.ULONG),
                ('SecurityDescriptor', c.c_void_p), ('SecurityQualityOfService', c.c_void_p)]


class _IoStatus(c.Structure):
    _fields_ = [('Status', c.c_void_p), ('Information', c.c_size_t)]


class _BasicInfo(c.Structure):
    _fields_ = [('created', c.c_longlong), ('accessed', c.c_longlong),
                ('written', c.c_longlong), ('changed', c.c_longlong), ('attributes', w.DWORD)]


class _StandardInfo(c.Structure):
    _fields_ = [('allocation', c.c_longlong), ('eof', c.c_longlong), ('links', w.DWORD),
                ('pending', c.c_ubyte), ('directory', c.c_ubyte)]


class _FileId(c.Structure):
    _fields_ = [('volume', c.c_ulonglong), ('identity', c.c_ubyte * 16)]


class _Disposition(c.Structure):
    _fields_ = [('delete', c.c_ubyte)]


def _component(name):
    if (not name or name in ('.', '..') or any(character in name for character in '/\\:\0')):
        raise ValueError('unsafe_demo_path')
    return name


class WindowsFiles:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('unsupported_demo_platform')
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.native = c.WinDLL('ntdll', use_last_error=True)
        self._create = self.native.NtCreateFile
        self._create.argtypes = [c.POINTER(w.HANDLE), w.ULONG, c.POINTER(_ObjectAttributes),
            c.POINTER(_IoStatus), c.c_void_p, w.ULONG, w.ULONG, w.ULONG, w.ULONG, c.c_void_p, w.ULONG]
        self._create.restype = c.c_long
        self._dos_error = self.native.RtlNtStatusToDosError
        self._dos_error.argtypes = [c.c_long]
        self._dos_error.restype = w.ULONG
        self._query = self.kernel.GetFileInformationByHandleEx
        self._query.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self._query.restype = w.BOOL
        self._set = self.kernel.SetFileInformationByHandle
        self._set.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self._set.restype = w.BOOL
        self._close = self.kernel.CloseHandle
        self._close.argtypes = [w.HANDLE]
        self._close.restype = w.BOOL
        self._read = self.kernel.ReadFile
        self._read.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        self._read.restype = w.BOOL
        self._write = self.kernel.WriteFile
        self._write.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        self._write.restype = w.BOOL
        self._seek = self.kernel.SetFilePointerEx
        self._seek.argtypes = [w.HANDLE, c.c_longlong, c.c_void_p, w.DWORD]
        self._seek.restype = w.BOOL
        self._flush = self.kernel.FlushFileBuffers
        self._flush.argtypes = [w.HANDLE]
        self._flush.restype = w.BOOL
        self._time = self.kernel.GetSystemTimeAsFileTime
        self._time.argtypes = [c.POINTER(w.FILETIME)]
        self._time.restype = None
        self._set_time = self.kernel.SetFileTime
        self._set_time.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.POINTER(w.FILETIME)]
        self._set_time.restype = w.BOOL

    def _checked(self, success):
        if not success:
            raise c.WinError(c.get_last_error())

    def _information(self, handle, kind, structure):
        value = structure()
        self._checked(self._query(handle, kind, c.byref(value), c.sizeof(value)))
        return value

    def _identity(self, handle, *, directory):
        basic = self._information(handle, 0, _BasicInfo)
        standard = self._information(handle, 1, _StandardInfo)
        identity = self._information(handle, 18, _FileId)
        if (basic.attributes & 0x400 or bool(basic.attributes & 0x10) != directory
                or bool(standard.directory) != directory or standard.pending or not any(identity.identity)):
            raise ValueError('unsafe_demo_path')
        return (identity.volume, bytes(identity.identity)), standard.eof, basic.written

    @contextmanager
    def _handle(self, parent, name, *, directory, create=False, delete=False, anchor=False):
        if not anchor:
            _component(name)
        name_buffer = c.create_unicode_buffer(name)
        length = len(name.encode('utf-16-le'))
        unicode = _UnicodeString(length, length + 2, c.cast(name_buffer, w.LPWSTR))
        attributes = _ObjectAttributes(c.sizeof(_ObjectAttributes), parent, c.pointer(unicode), 0x40, None, None)
        handle, status = w.HANDLE(), _IoStatus()
        access = _READ_ATTRIBUTES | _SYNCHRONIZE
        if directory:
            access |= 0x20  # FILE_TRAVERSE; not the file FILE_EXECUTE access path.
        else:
            access |= 1  # FILE_READ_DATA
        if create and not directory:
            access |= 2 | _WRITE_ATTRIBUTES  # FILE_WRITE_DATA, explicit final last-write stamp.
        if delete or create:
            access |= _DELETE
        result = self._create(c.byref(handle), access, c.byref(attributes), c.byref(status), None,
            0, _SHARE_READ, _FILE_CREATE if create else _FILE_OPEN,
            (_DIRECTORY if directory else _NON_DIRECTORY) | _SYNCHRONOUS | _OPEN_REPARSE_POINT, None, 0)
        if result < 0:
            raise c.WinError(self._dos_error(result))
        try:
            self._identity(handle.value, directory=directory)
            yield handle.value
        finally:
            self._close(handle.value)

    @contextmanager
    def _parents(self, directory, *, receipts=None, create=False, owned_root=None, delete_last=False):
        directory = Path(os.path.abspath(directory))
        if (not directory.drive or directory.drive.startswith('\\\\')
                or len(directory.drive) != 2 or directory.drive[1] != ':'):
            raise ValueError('unsafe_demo_path')
        with ExitStack() as stack:
            # The sole absolute Native name is the local volume root. Every
            # subsequent lookup is one component relative to a retained parent.
            parent = stack.enter_context(self._handle(None, '\\??\\' + directory.anchor,
                                                     directory=True, anchor=True))
            current = Path(directory.anchor)
            for index, part in enumerate(directory.parts[1:], 1):
                current /= _component(part)
                is_owned = owned_root is not None and current.is_relative_to(owned_root)
                expected = receipts.get(current) if receipts is not None else None
                new_owned = create and is_owned and expected is None
                delete = delete_last and index == len(directory.parts) - 1
                try:
                    next_handle = stack.enter_context(self._handle(parent, part, directory=True,
                                                                  create=new_owned, delete=delete))
                except FileNotFoundError:
                    if not create or is_owned:
                        raise
                    # Host staging ancestors may be created if absent; FILE_CREATE
                    # fails on collision instead of adopting a path swapped in.
                    next_handle = stack.enter_context(self._handle(parent, part, directory=True, create=True))
                identity = self._identity(next_handle, directory=True)[0]
                if expected is not None and identity != expected:
                    raise ValueError('unsafe_demo_path')
                if is_owned:
                    if expected is None:
                        if not new_owned:
                            raise ValueError('unsafe_demo_path')
                        receipts[current] = identity
                parent = next_handle
            yield parent

    def _fingerprint(self, handle, digest):
        before = self._identity(handle, directory=False)
        if not 0 <= before[1] <= _MAX_BYTES:
            raise ValueError('unsafe_demo_path')
        self._checked(self._seek(handle, 0, None, 0))
        buffer, count = c.create_string_buffer(_MAX_BYTES + 1), w.DWORD()
        self._checked(self._read(handle, buffer, _MAX_BYTES + 1, c.byref(count), None))
        data = buffer.raw[:count.value]
        after = self._identity(handle, directory=False)
        if before != after or len(data) != before[1]:
            raise ValueError('unsafe_demo_path')
        return data, (*after, digest(data).digest())

    def read_file(self, path, digest, *, receipts=None, owned_root=None):
        with self._parents(path.parent, receipts=receipts, owned_root=owned_root) as parent:
            with self._handle(parent, path.name, directory=False) as handle:
                return self._fingerprint(handle, digest)

    def create_file(self, path, data, receipts, owned_root, digest):
        with self._parents(path.parent, receipts=receipts, create=True, owned_root=owned_root) as parent:
            with self._handle(parent, path.name, directory=False, create=True) as handle:
                try:
                    buffer, count = c.create_string_buffer(data), w.DWORD()
                    self._checked(self._write(handle, buffer, len(data), c.byref(count), None))
                    if count.value != len(data):
                        raise OSError('incomplete_demo_write')
                    self._checked(self._flush(handle))
                    timestamp = w.FILETIME()
                    self._time(c.byref(timestamp))
                    self._checked(self._set_time(handle, None, None, c.byref(timestamp)))
                    return self._fingerprint(handle, digest)[1]
                except Exception:
                    # The failed creation is still the handle we exclusively
                    # created, so cleanup cannot adopt a replacement leaf.
                    self._dispose(handle)
                    raise

    def _dispose(self, handle):
        disposition = _Disposition(1)
        self._checked(self._set(handle, 4, c.byref(disposition), c.sizeof(disposition)))

    def delete_file(self, path, expected, receipts, owned_root, digest):
        with self._parents(path.parent, receipts=receipts, owned_root=owned_root) as parent:
            with self._handle(parent, path.name, directory=False, delete=True) as handle:
                if self._fingerprint(handle, digest)[1] != expected:
                    raise ValueError('unsafe_demo_path')
                self._dispose(handle)

    def delete_directory(self, path, receipts, owned_root):
        with self._parents(path, receipts=receipts, owned_root=owned_root, delete_last=True) as handle:
            self._dispose(handle)  # NTFS refuses nonempty directories; no recursive removal.
