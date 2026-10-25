import ctypes
import hashlib
import os
import subprocess
import sys
from pathlib import Path
import uuid
import pytest
from atom_search.runtime import _windows


def test_private_directory_does_not_silently_rewrite_broad_existing_acl(tmp_path):
    broad = tmp_path / "existing"
    broad.mkdir()
    (broad / "valuable").write_text("keep")
    with pytest.raises(_windows.WindowsError, match="acl_not_private"):
        _windows.private_directory(broad)
    assert (broad / "valuable").read_text() == "keep"


def test_process_identity_is_observed_not_basename_or_pid_only():
    observed = _windows.process_identity(os.getpid())
    assert observed.pid == os.getpid()
    assert observed.created > 0
    assert Path(observed.executable).is_file()
    assert _windows.process_identity(os.getpid()) == observed


def test_verified_process_waits_for_its_own_child_exit():
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW) as child:
        try:
            identity = _windows.process_identity(child.pid)
            with _windows.verified_process(identity) as handle:
                assert not _windows.wait_process_exit(handle, timeout_seconds=0)
                child.stdin.write(b'x')
                child.stdin.flush()
                assert _windows.wait_process_exit(handle, timeout_seconds=2)
            assert child.wait(timeout=2) == 0
        finally:
            child.stdin.close()  # EOF also releases this owned child on RED.
            child.wait(timeout=2)


def test_process_exit_wait_refuses_an_invalid_handle():
    with pytest.raises(_windows.WindowsError, match='process_wait_failed'):
        _windows.wait_process_exit(0, timeout_seconds=0)


def test_process_exit_wait_refuses_an_unknown_kernel_result(monkeypatch):
    identity = _windows.process_identity(os.getpid())
    with _windows.verified_process(identity) as handle:
        original = _windows._api
        def unknown_wait(dll, name, arguments, result=ctypes.wintypes.BOOL):
            if name == 'WaitForSingleObject':
                return lambda handle, milliseconds: 0x80
            return original(dll, name, arguments, result)
        with monkeypatch.context() as patch:
            patch.setattr(_windows, '_api', unknown_wait)
            with pytest.raises(_windows.WindowsError, match='process_wait_failed'):
                _windows.wait_process_exit(handle, timeout_seconds=0)


def test_precreated_mutex_with_broad_dacl_is_refused():
    key = "test-" + uuid.uuid4().hex
    name = "Global\\AtomSearch." + _windows.user_sid() + "." + hashlib.sha256(key.encode()).hexdigest()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = _windows._api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                            [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p])
    assert convert("D:(A;;GA;;;WD)", 1, ctypes.byref(descriptor), None)
    attributes = _windows._SecurityAttributes(ctypes.sizeof(_windows._SecurityAttributes), descriptor, False)
    create = _windows._api(ctypes.WinDLL("kernel32", use_last_error=True), "CreateMutexW",
                          [ctypes.POINTER(_windows._SecurityAttributes), ctypes.c_int, ctypes.c_wchar_p],
                          ctypes.c_void_p)
    handle = create(ctypes.byref(attributes), False, name)
    assert handle
    try:
        with pytest.raises(_windows.WindowsError, match="acl_not_private"):
            with _windows.mutex(key):
                pytest.fail("Entered an object with untrusted permissions")
    finally:
        _windows._close(handle)
        _windows._free(descriptor)


def test_observed_process_arguments_preserve_unicode(tmp_path):
    directory = _windows.private_directory(tmp_path / "private")
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)", "профиль-данные"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
        try:
            identity = _windows.process_identity(child.pid)
            arguments = _windows.process_arguments(identity, directory)
            assert arguments[-1] == "профиль-данные"
        finally:
            child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize("junction", [False, True])
def test_process_arguments_cleanup_does_not_unlink_replaced_directory(tmp_path, monkeypatch, junction):
    import _winapi
    directory = _windows.private_directory(tmp_path / "private")
    outside = _windows.private_directory(tmp_path / "outside")
    name = None
    replaced = False
    def replace(*args, **kwargs):
        nonlocal name, replaced
        name = next(directory.glob("process-*.json")).name
        # Model an interleaving immediately after the output handle closes:
        # Windows forbids the directory rename while this handle remains open.
        # Closing is idempotent when the production context exits next.
        kwargs["stdout"].close()
        directory.rename(tmp_path / "original-private")
        if junction:
            _winapi.CreateJunction(str(outside), str(directory))
        else:
            _windows.private_directory(directory)
        (directory / name).write_bytes(b"unowned sentinel")
        replaced = True
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(_windows.subprocess, "run", replace)
    try:
        with pytest.raises(Exception) as caught:
            _windows.process_arguments(_windows.process_identity(os.getpid()), directory)
        assert replaced, str(caught.value)
        assert (directory / name).read_bytes() == b"unowned sentinel"
    finally:
        if directory.is_junction():
            directory.rmdir()
