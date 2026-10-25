"""Reveal indexed paths using metadata only and a bounded ancestor search.

Call ``reveal`` from a worker: both network metadata and Windows Shell parsing
can wait on a remote server. No document is opened, read, hashed, or executed.
"""

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import os
import stat
from typing import Protocol

from atom_search.content import message


@dataclass(frozen=True)
class RevealResult:
    opened: bool
    selected: bool
    folder: str | None
    message: str


class ExplorerShell(Protocol):
    """The external desktop side effect, injectable without launching windows."""

    def select_file(self, path: str) -> None: ...

    def open_folder(self, folder: str) -> None: ...


def _raise_hresult(result: int) -> None:
    code = result & 0xFFFFFFFF
    if code & 0x80000000:
        # Preserve Win32 error identity for friendly access/network reporting.
        error = OSError("Windows Shell operation failed")
        error.winerror = code & 0xFFFF if code & 0xFFFF0000 == 0x80070000 else code
        raise error


@contextmanager
def _windows_shell():
    if os.name != "nt":
        raise OSError("Windows Explorer is unavailable on this platform")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    initialized = ole32.CoInitializeEx(None, 2)  # apartment-threaded worker
    # An existing COM apartment is usable; only balance our successful init.
    if initialized & 0xFFFFFFFF != 0x80010106:  # RPC_E_CHANGED_MODE
        _raise_hresult(initialized)
    try:
        yield shell32, ole32
    finally:
        if initialized >= 0:
            ole32.CoUninitialize()


class WindowsExplorer:
    """Native Unicode shell APIs, without command strings or a command shell."""

    def select_file(self, path: str) -> None:
        with _windows_shell() as (shell32, ole32):
            shell32.SHParseDisplayName.argtypes = [
                ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_ulong, ctypes.c_void_p,
            ]
            shell32.SHParseDisplayName.restype = ctypes.c_long
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            item = ctypes.c_void_p()
            try:
                _raise_hresult(shell32.SHParseDisplayName(path, None, ctypes.byref(item), 0, None))
                # cidl=0 means select this absolute item in its parent folder.
                _raise_hresult(shell32.SHOpenFolderAndSelectItems(item, 0, None, 0))
            finally:
                if item:
                    ole32.CoTaskMemFree(item)

    def open_folder(self, folder: str) -> None:
        with _windows_shell() as (shell32, _):
            shell32.ShellExecuteW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
            ]
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            result = shell32.ShellExecuteW(None, "explore", folder, None, None, 1)
            if not result or result <= 32:
                error = OSError("Windows Explorer could not open the folder")
                error.winerror = result or 0
                raise error


_NETWORK_ERRORS = {21, 51, 53, 54, 55, 58, 59, 64, 67, 121, 1201, 1203,
                   1208, 1222, 1231, 1232, 1236, 2250}
_NETWORK_ERRNOS = {errno.ENETDOWN, errno.ENETUNREACH, errno.ECONNRESET,
                   errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENOTCONN,
                   errno.ETIMEDOUT}
_ACCESS_ERRORS = {5, 65, 86, 1314, 1326, 1385}


def _missing(error: Exception) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        # WinError 53 is also a FileNotFoundError, but means a lost network.
        return winerror in {2, 3}
    return (isinstance(error, (FileNotFoundError, NotADirectoryError))
            or getattr(error, "errno", None) in {errno.ENOENT, errno.ENOTDIR})


def _failure(error: Exception, path: str, *, root: bool = False,
             shell: bool = False) -> RevealResult:
    winerror = getattr(error, "winerror", None)
    if (isinstance(error, PermissionError) or winerror in _ACCESS_ERRORS
            or getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}):
        explanation = message("reveal.access_denied", path=path)
    elif winerror in _NETWORK_ERRORS or getattr(error, "errno", None) in _NETWORK_ERRNOS:
        explanation = message("reveal.network_unavailable", path=path)
    elif root and _missing(error):
        explanation = message("reveal.source_missing", path=path)
    elif shell:
        explanation = message("reveal.shell_failed", path=path)
    else:
        explanation = message("reveal.check_failed", path=path)
    return RevealResult(False, False, None, explanation)


def _same_path(first: str, second: str) -> bool:
    separators = os.sep + (os.altsep or "")
    return (os.path.normcase(first).rstrip(separators)
            == os.path.normcase(second).rstrip(separators))


def reveal(path: str, root: str, *, shell: ExplorerShell | None = None) -> RevealResult:
    """Select a file, or open its nearest available ancestor within ``root``.

    Access denial and network failures stop the search: they do not establish
    that a file was deleted. Only missing paths permit moving up one level.
    The return value reports Shell acceptance, not a guaranteed visible window.
    """
    try:
        if (not isinstance(path, str) or not isinstance(root, str)
                or not path or not root or "\x00" in path or "\x00" in root
                or not os.path.isabs(path) or not os.path.isabs(root)):
            raise ValueError("An absolute filesystem path is required")
        path, root = os.path.normpath(path), os.path.normpath(root)
        # commonpath rejects a bare UNC share root on Python 3.12. relpath
        # also handles a source root with/without the final backslash.
        relative = os.path.relpath(path, root)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return RevealResult(False, False, None, message("reveal.outside_source"))
    except (ValueError, TypeError):
        return RevealResult(False, False, None, message("reveal.invalid_path"))

    try:
        if not stat.S_ISDIR(os.stat(root).st_mode):
            return _failure(NotADirectoryError(errno.ENOTDIR, "Not a folder"), root, root=True)
    except OSError as error:
        return _failure(error, root, root=True)

    explorer = WindowsExplorer() if shell is None else shell
    try:
        info = os.stat(path)
    except OSError as error:
        if not _missing(error):
            return _failure(error, path)
        if _same_path(path, root):
            return _failure(error, root, root=True)
    else:
        folder = path if stat.S_ISDIR(info.st_mode) else os.path.dirname(path)
        try:
            if stat.S_ISDIR(info.st_mode):
                explorer.open_folder(path)
                return RevealResult(True, False, path, message("reveal.folder_opened", path=path))
            explorer.select_file(path)
            return RevealResult(True, True, folder, message("reveal.file_selected", folder=folder))
        except Exception as error:
            if not _missing(error):
                return _failure(error, path, shell=True)
            if _same_path(path, root):
                return _failure(error, root, root=True, shell=True)

    candidate = os.path.dirname(path)
    while True:
        if _same_path(candidate, root):
            candidate = root
        try:
            info = os.stat(candidate)
        except OSError as error:
            if not _missing(error):
                return _failure(error, candidate)
        else:
            if stat.S_ISDIR(info.st_mode):
                try:
                    explorer.open_folder(candidate)
                except Exception as error:
                    if not _missing(error):
                        return _failure(error, candidate, shell=True)
                else:
                    return RevealResult(True, False, candidate,
                                        message("reveal.nearest_folder", folder=candidate))
        if _same_path(candidate, root):
            return _failure(FileNotFoundError(errno.ENOENT, "Root disappeared"), root, root=True)
        candidate = os.path.dirname(candidate)
