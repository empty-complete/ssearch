"""Small fail-closed Win32 boundary. No process enumeration or elevation.

Allocation counts regular-file storage, deduplicated by volume/file identity;
filesystem metadata allocation is deliberately not represented as exact.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


class WindowsError(RuntimeError):
    pass


def _dll(name):
    if os.name != "nt":
        raise WindowsError("windows_required")
    return c.WinDLL(name, use_last_error=True)


def _api(dll, name, args, result=w.BOOL):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


def _check(value, code):
    if not value:
        raise WindowsError(code)
    return value


def _close(handle):
    _api(_dll("kernel32"), "CloseHandle", [w.HANDLE])(handle)


def _free(pointer):
    _api(_dll("kernel32"), "LocalFree", [c.c_void_p], c.c_void_p)(pointer)


def user_sid():
    kernel, advapi = _dll("kernel32"), _dll("advapi32")
    token = w.HANDLE()
    process = _api(kernel, "GetCurrentProcess", [], w.HANDLE)()
    _check(_api(advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])(
        process, 8, c.byref(token)), "token_unavailable")
    try:
        size = w.DWORD()
        get = _api(advapi, "GetTokenInformation",
                   [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)])
        get(token, 1, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        _check(get(token, 1, buffer, size, c.byref(size)), "token_unavailable")
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        text = w.LPWSTR()
        _check(_api(advapi, "ConvertSidToStringSidW", [c.c_void_p, c.POINTER(w.LPWSTR)])(
            sid, c.byref(text)), "sid_unavailable")
        try:
            return text.value
        finally:
            _free(text)
    finally:
        _close(token)


class _SecurityAttributes(c.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p),
                ("bInheritHandle", w.BOOL)]


@contextmanager
def _security(*, directory=False):
    sid = user_sid()
    sddl = f"O:{sid}D:P(A;{'OICI' if directory else ''};GA;;;{sid})"
    descriptor = c.c_void_p()
    _check(_api(_dll("advapi32"), "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p])(
        sddl, 1, c.byref(descriptor), None), "security_descriptor_failed")
    try:
        yield _SecurityAttributes(c.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        _free(descriptor)


def assert_private(path: Path):
    """Read actual owner/DACL; deny NULL, broad, deny/unknown ACE layouts."""
    reject_reparse(path)
    advapi = _dll("advapi32")
    descriptor = c.c_void_p()
    result = _api(advapi, "GetNamedSecurityInfoW",
                  [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p,
                   c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)], w.DWORD)(
        str(path), 1, 1 | 4, None, None, None, None, c.byref(descriptor))
    if result:
        raise WindowsError("acl_unavailable")
    try:
        _assert_descriptor_private(descriptor)
    finally:
        _free(descriptor)


def assert_read_only_file(path: Path):
    """Inspect an external leaf without granting private-directory authority.

    Absence is allowed only after ancestor/reparse/local-volume inspection.
    Access denial and malformed replacements are never interpreted as absence.
    """
    path = reject_reparse(path)
    volume_identity(path)
    existing = path
    try:
        if not path.stat().st_mode & 0o100000 or not path.is_file():
            raise WindowsError("regular_file_required")
    except FileNotFoundError:
        existing = path.parent
        while True:
            try:
                existing.stat()
                break
            except FileNotFoundError:
                existing = existing.parent
    except OSError:
        raise WindowsError("source_unavailable") from None
    descriptor, owner = c.c_void_p(), c.c_void_p()
    result = _api(_dll("advapi32"), "GetNamedSecurityInfoW",
                  [w.LPWSTR, c.c_int, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p,
                   c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)], w.DWORD)(
        str(existing), 1, 1, c.byref(owner), None, None, None, c.byref(descriptor))
    if result:
        raise WindowsError("source_owner_unavailable")
    text = w.LPWSTR()
    try:
        _check(_api(_dll("advapi32"), "ConvertSidToStringSidW",
                    [c.c_void_p, c.POINTER(w.LPWSTR)])(owner, c.byref(text)),
               "source_owner_unavailable")
        if text.value != user_sid():
            raise WindowsError("source_owner_mismatch")
    finally:
        if text:
            _free(text)
        _free(descriptor)
    return path


def _assert_descriptor_private(descriptor):
    advapi = _dll("advapi32")
    text = w.LPWSTR()
    try:
        _check(_api(advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                    [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p])(
            descriptor, 1, 1 | 4, c.byref(text), None), "acl_unavailable")
        value = text.value
        sid = user_sid()
        if not value.startswith(f"O:{sid}D:"):
            raise WindowsError("profile_owner_mismatch")
        aces = re.findall(r"\([^()]+\)", value)
        if not aces or any(not re.fullmatch(r"\(A;[A-Z]*;[A-Z0-9a-fx]+;;;" +
                                           re.escape(sid) + r"\)", ace) for ace in aces):
            raise WindowsError("profile_acl_not_private")
    finally:
        if text:
            _free(text)


def reject_reparse(path: Path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            if item.lstat().st_file_attributes & 0x400:
                raise WindowsError("reparse_path")
        except FileNotFoundError:
            continue
    return path


def volume_identity(path: Path):
    path = reject_reparse(path)
    existing = path
    while not existing.exists():
        existing = existing.parent
    kernel = _dll("kernel32")
    mount = c.create_unicode_buffer(32768)
    _check(_api(kernel, "GetVolumePathNameW", [w.LPCWSTR, w.LPWSTR, w.DWORD])(
        str(existing), mount, len(mount)), "volume_unknown")
    if _api(kernel, "GetDriveTypeW", [w.LPCWSTR], w.UINT)(mount.value) != 3:
        raise WindowsError("local_fixed_volume_required")
    flags = w.DWORD()
    _check(_api(kernel, "GetVolumeInformationW",
                [w.LPCWSTR, w.LPWSTR, w.DWORD, c.c_void_p, c.c_void_p,
                 c.POINTER(w.DWORD), w.LPWSTR, w.DWORD])(
        mount.value, None, 0, None, None, c.byref(flags), None, 0), "volume_unknown")
    if not flags.value & 8:
        raise WindowsError("persistent_acls_required")
    guid = c.create_unicode_buffer(1024)
    _check(_api(kernel, "GetVolumeNameForVolumeMountPointW", [w.LPCWSTR, w.LPWSTR, w.DWORD])(
        mount.value, guid, len(guid)), "volume_unknown")
    return guid.value.casefold()


def private_directory(path: Path):
    path = reject_reparse(path)
    volume_identity(path)
    if path.exists():
        if not path.is_dir():
            raise WindowsError("directory_required")
        assert_private(path)
        return path.resolve()
    if not path.parent.exists():
        private_directory(path.parent)
    with _security(directory=True) as security:
        ok = _api(_dll("kernel32"), "CreateDirectoryW",
                  [w.LPCWSTR, c.POINTER(_SecurityAttributes)])(str(path), c.byref(security))
        if not ok and c.get_last_error() != 183:
            raise WindowsError("private_directory_failed")
    assert_private(path)
    return path.resolve()


@contextmanager
def mutex(identity: str, *, timeout_seconds=30):
    name = "Global\\AtomSearch." + user_sid() + "." + hashlib.sha256(
        identity.casefold().encode("utf-8")).hexdigest()
    kernel = _dll("kernel32")
    with _security() as security:
        handle = _api(kernel, "CreateMutexW",
                      [c.POINTER(_SecurityAttributes), w.BOOL, w.LPCWSTR], w.HANDLE)(
            c.byref(security), False, name)
    _check(handle, "mutex_unavailable")
    acquired = False
    try:
        descriptor = c.c_void_p()
        code = _api(_dll("advapi32"), "GetSecurityInfo",
                    [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p,
                     c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)], w.DWORD)(
            handle, 6, 1 | 4, None, None, None, None, c.byref(descriptor))
        if code:
            raise WindowsError("mutex_acl_unavailable")
        try:
            _assert_descriptor_private(descriptor)
        finally:
            _free(descriptor)
        result = _api(kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)(
            handle, int(timeout_seconds * 1000))
        if result not in (0, 0x80):
            raise WindowsError("mutex_timeout" if result == 258 else "mutex_failed")
        acquired = True
        yield
    finally:
        if acquired:
            _check(_api(kernel, "ReleaseMutex", [w.HANDLE])(handle), "mutex_release_failed")
        _close(handle)


class _Blob(c.Structure):
    _fields_ = [("cbData", w.DWORD), ("pbData", c.POINTER(c.c_ubyte))]


def crypt(data: bytes, *, decrypt=False):
    buffer = c.create_string_buffer(data)
    source = _Blob(len(data), c.cast(buffer, c.POINTER(c.c_ubyte)))
    output = _Blob()
    try:
        function = _api(_dll("crypt32"), "CryptUnprotectData" if decrypt else "CryptProtectData",
                        [c.POINTER(_Blob), c.c_void_p, c.c_void_p, c.c_void_p,
                         c.c_void_p, w.DWORD, c.POINTER(_Blob)])
        _check(function(c.byref(source), None, None, None, None, 1, c.byref(output)),
               "credential_decryption_failed" if decrypt else "credential_encryption_failed")
        return c.string_at(output.pbData, output.cbData)
    finally:
        c.memset(buffer, 0, len(buffer))
        if output.pbData:
            c.memset(output.pbData, 0, output.cbData)
            _free(output.pbData)


def atomic_json(path: Path, value):
    assert_private(path.parent)
    reject_reparse(path)
    descriptor, name = tempfile.mkstemp(prefix="record-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        assert_private(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    executable: str
    created: int


@contextmanager
def process_handle(pid: int):
    kernel = _dll("kernel32")
    handle = _api(kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)(
        0x1000 | 0x100000, False, pid)
    if not handle:
        if c.get_last_error() == 87:  # definitely no such PID, not access denied
            yield None
            return
        raise WindowsError("process_identity_unavailable")
    try:
        yield handle
    finally:
        _close(handle)


def _identity(handle, pid):
    kernel = _dll("kernel32")
    if _api(kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)(handle, 0) == 0:
        return None
    executable = c.create_unicode_buffer(32768)
    size = w.DWORD(len(executable))
    _check(_api(kernel, "QueryFullProcessImageNameW",
                [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)])(
        handle, 0, executable, c.byref(size)), "process_image_unavailable")
    times = [w.FILETIME() for _ in range(4)]
    _check(_api(kernel, "GetProcessTimes", [w.HANDLE, *[c.POINTER(w.FILETIME)] * 4])(
        handle, *(c.byref(item) for item in times)), "process_time_unavailable")
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return ProcessIdentity(pid, os.path.normcase(str(Path(executable.value).resolve())), created)


def process_identity(pid):
    with process_handle(pid) as handle:
        return _identity(handle, pid) if handle else None


@contextmanager
def verified_process(identity):
    """Retain the actual process object through a shutdown decision/tool wait."""
    with process_handle(identity.pid) as handle:
        if not handle or _identity(handle, identity.pid) != identity:
            raise WindowsError("process_changed")
        yield handle


def wait_process_exit(handle, *, timeout_seconds):
    """Only a signalled retained process object proves exit, never a PID error."""
    result = _api(_dll('kernel32'), 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)(
        handle, max(0, int(timeout_seconds * 1000)))
    if result not in (0, 258):
        raise WindowsError('process_wait_failed')
    return result == 0


def _file_identity(path):
    reject_reparse(path)
    stat = path.stat()
    if not stat.st_ino:
        raise WindowsError("file_identity_unavailable")
    return stat.st_dev, stat.st_ino


def process_arguments(identity: ProcessIdentity, directory: Path, *, guard=None):
    """Observe one process through CIM, bracketed by a retained kernel handle."""
    if guard:
        guard()
    assert_private(directory)
    directory_identity = _file_identity(directory)
    with process_handle(identity.pid) as handle:
        if not handle or _identity(handle, identity.pid) != identity:
            raise WindowsError("process_changed")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = ("$ErrorActionPreference='Stop'; "
                   "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                   f"(Get-CimInstance Win32_Process -Filter 'ProcessId={identity.pid}').CommandLine "
                   "| ConvertTo-Json -Compress")
        descriptor, name = tempfile.mkstemp(prefix="process-", suffix=".json", dir=directory)
        path = Path(name)
        file_identity = _file_identity(path)

        def validate_temporary():
            if guard:
                guard()
            assert_private(directory)
            assert_private(path)
            if _file_identity(directory) != directory_identity or _file_identity(path) != file_identity:
                raise WindowsError("temporary_ownership_changed")

        try:
            with os.fdopen(descriptor, "wb") as output:
                result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                        stdout=output, stderr=subprocess.DEVNULL,
                                        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            validate_temporary()
            if result.returncode or _identity(handle, identity.pid) != identity:
                raise WindowsError("process_changed")
            commandline = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(commandline, str) or not commandline:
                raise WindowsError("process_arguments_unavailable")
            count = c.c_int()
            argv = _api(_dll("shell32"), "CommandLineToArgvW",
                        [w.LPCWSTR, c.POINTER(c.c_int)], c.POINTER(w.LPWSTR))(
                commandline, c.byref(count))
            _check(argv, "process_arguments_unavailable")
            try:
                return [argv[i] for i in range(count.value)]
            finally:
                _free(argv)
        finally:
            validate_temporary()
            path.unlink()


def available_bytes(path):
    available = c.c_ulonglong()
    _check(_api(_dll("kernel32"), "GetDiskFreeSpaceExW",
                [w.LPCWSTR, c.POINTER(c.c_ulonglong), c.c_void_p, c.c_void_p])(
        str(path), c.byref(available), None, None), "disk_probe_failed")
    return available.value


class _FileBasicInfo(c.Structure):
    _fields_ = [("created", c.c_longlong), ("accessed", c.c_longlong),
                ("written", c.c_longlong), ("changed", c.c_longlong), ("attributes", w.DWORD)]


class _FileStandardInfo(c.Structure):
    _fields_ = [("allocation", c.c_longlong), ("eof", c.c_longlong), ("links", w.DWORD),
                ("pending", c.c_ubyte), ("directory", c.c_ubyte)]


class _FileCompressionInfo(c.Structure):
    _fields_ = [("physical", c.c_longlong), ("format", w.WORD), ("unit", c.c_ubyte),
                ("chunk", c.c_ubyte), ("cluster", c.c_ubyte), ("reserved", c.c_ubyte * 3)]


class _FileIdInfo(c.Structure):
    _fields_ = [("volume", c.c_ulonglong), ("identity", c.c_ubyte * 16)]


class _AllocationTreeChanged(WindowsError):
    """An enumerated entry disappeared; discard the whole partial scan."""


def _allocation_failure(phase, winerror=None, *, code="allocation_probe_failed"):
    # Only these Win32 codes establish disappearance. Access denied (5), even
    # when possibly delete-pending, and sharing/unknown failures stay closed.
    error_type = _AllocationTreeChanged if winerror in (2, 3, 303) else WindowsError
    error = error_type(code)
    error.phase = phase
    if winerror:  # No invented errno for metadata validation or unknown errors.
        error.winerror = winerror
    raise error


def _file_allocation(path):
    """Attributes, allocation and dedup identity belong to one retained file."""
    kernel = _dll("kernel32")
    open_file = _api(kernel, "CreateFileW",
                     [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE],
                     w.HANDLE)
    c.set_last_error(0)
    handle = open_file(str(path), 0x80, 7, None, 3, 0x00200000, None)
    open_error = c.get_last_error()
    if not handle or handle == c.c_void_p(-1).value:
        _allocation_failure("open", open_error)
    try:
        if _api(kernel, "GetFileType", [w.HANDLE], w.DWORD)(handle) != 1:
            _allocation_failure("type")
        query = _api(kernel, "GetFileInformationByHandleEx",
                     [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])

        def information(kind, structure):
            value = structure()
            c.set_last_error(0)
            ok = query(handle, kind, c.byref(value), c.sizeof(value))
            query_error = c.get_last_error()
            if not ok:
                _allocation_failure("query", query_error)
            return value

        basic = information(0, _FileBasicInfo)
        standard = information(1, _FileStandardInfo)
        identity = information(18, _FileIdInfo)
        if basic.attributes & (0x400 | 0x10) or standard.directory or not any(identity.identity):
            _allocation_failure("attributes")
        # Ordinary EOF is not disk allocation. For compressed/sparse streams the
        # compression query reports underlying physical storage, which can be
        # smaller than the stream's standard allocation (MS-FSA 2.1.5.12.8).
        allocated = (information(8, _FileCompressionInfo).physical
                     if basic.attributes & (0x800 | 0x200) else standard.allocation)
        if allocated < 0 or standard.allocation < 0:
            _allocation_failure("allocation")
        return (identity.volume, bytes(identity.identity)), allocated
    finally:
        _close(handle)


class AllocationScan:
    """Cooperative, read-only observation of the P0 allocation domain.

    Partial totals never escape. Admission continues to use allocated_bytes()
    synchronously from scratch, without this observational cache.
    """
    def __init__(self, roots, *, read_only_files=(), groups=None, clock=time.monotonic):
        self.roots, self.read_only_files = tuple(roots), tuple(read_only_files)
        self.groups, self.clock = dict(groups or {}), clock
        self._attempts = 0
        self._reset()

    def _reset(self):
        self._seen = {name: set() for name in ('physical_owned_bytes', *self.groups)}
        self._totals = {name: 0 for name in self._seen}
        self._entries = self._walk()

    def _walk(self):
        def failed(error):
            _allocation_failure('enumerate', getattr(error, 'winerror', None), code='allocation_enumeration_failed')
        for root in self.roots:
            reject_reparse(root)
            volume = volume_identity(root)
            try:
                root.stat()
            except FileNotFoundError:
                continue
            except OSError:
                raise WindowsError('allocation_enumeration_failed') from None
            yield None, 0, root
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
                for name in dirs + files:
                    path = Path(directory) / name
                    reject_reparse(path)
                    if volume_identity(path) != volume:
                        raise WindowsError('volume_changed')
                    if name in dirs:
                        yield None, 0, path  # A deadline/cancel point for empty trees, too.
                    else:
                        key, size = _file_allocation(path)
                        yield key, size, path
        for path in self.read_only_files:
            assert_read_only_file(path)
            try:
                path.stat()
            except FileNotFoundError:
                # Legacy WAL/SHM may legitimately disappear after migration.
                # Match the ordinary P0 measurement; denial remains an error.
                continue
            except OSError:
                raise WindowsError('source_unavailable') from None
            key, size = _file_allocation(path)
            yield key, size, path

    def advance(self, *, deadline, stop=None):
        while self.clock() < deadline and not (stop is not None and stop.is_set()):
            try:
                key, size, path = next(self._entries)
            except StopIteration:
                return dict(self._totals)
            except _AllocationTreeChanged:
                self._attempts += 1
                if self._attempts >= 3:
                    raise
                self._reset()
                return None
            if key is None:
                continue
            memberships = ['physical_owned_bytes']
            for name, selection in self.groups.items():
                if (selection(path) if callable(selection) else any(path.is_relative_to(root) for root in selection)):
                    memberships.append(name)
            for name in memberships:
                if key not in self._seen[name]:
                    self._seen[name].add(key)
                    self._totals[name] += size
        return None


def allocated_bytes(roots, *, read_only_files=(), deadline=None):
    # Restart every root with fresh identity/total state; never omit a failed
    # entry or substitute zero. Three attempts bound churn cost without sleeps.
    roots = tuple(roots)
    for attempt in range(3):
        try:
            if deadline is not None:
                return _allocated_bytes_once(roots, read_only_files=read_only_files, deadline=deadline)
            return (_allocated_bytes_once(roots, read_only_files=read_only_files)
                    if read_only_files else _allocated_bytes_once(roots))
        except _AllocationTreeChanged:
            if attempt == 2:
                raise


def _allocated_bytes_once(roots, *, read_only_files=(), deadline=None):
    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise WindowsError('allocation_probe_timeout')
    def enumeration_failed(error):
        _allocation_failure("enumerate", getattr(error, "winerror", None),
                            code="allocation_enumeration_failed")

    seen, total = set(), 0
    for root in roots:
        check_deadline()
        reject_reparse(root)
        volume = volume_identity(root)
        try:
            root.stat()
        except FileNotFoundError:
            # A future root may be absent when reserving before its first write.
            continue
        except OSError:
            raise WindowsError("allocation_enumeration_failed") from None
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=enumeration_failed):
            check_deadline()
            for name in dirs + files:
                check_deadline()
                path = Path(directory) / name
                reject_reparse(path)
                if volume_identity(path) != volume:
                    raise WindowsError("volume_changed")
                if name in dirs:
                    continue
                key, allocated = _file_allocation(path)
                if key in seen:
                    continue
                seen.add(key)
                total += allocated
    for path in read_only_files:
        check_deadline()
        assert_read_only_file(path)
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            raise WindowsError("source_unavailable") from None
        key, allocated = _file_allocation(path)
        if key not in seen:
            seen.add(key)
            total += allocated
    return total
