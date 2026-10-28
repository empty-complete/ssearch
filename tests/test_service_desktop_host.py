"""Reusable desktop host contracts; runtime proofs execute only in Windows CI."""
from io import BytesIO
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from atom_search.http.serialization import decode, encode, envelope


def frame(value):
    content = encode(value)
    return struct.pack('!I', len(content)) + content


def frames(content):
    stream, result = BytesIO(content), []
    while header := stream.read(4):
        length = struct.unpack('!I', header)[0]
        body = stream.read(length)
        assert len(body) == length
        result.append(decode(body))
    return result


def context(**extra):
    return dict(schema_version=1, session_id=str(uuid.uuid4()), challenge=str(uuid.uuid4()), **extra)


def parent_identity(pid=None):
    from atom_search.runtime import _windows
    identity = _windows.process_identity(pid or os.getpid())
    assert identity is not None
    return dict(pid=identity.pid, executable=identity.executable, created=str(identity.created))


def invoke(monkeypatch, arguments, content):
    from atom_search.service.desktop_host import main
    output = BytesIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=BytesIO(content)))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
    result = main(arguments)
    return result, frames(output.getvalue())


@pytest.mark.parametrize('frozen', [False, True], ids=['source', 'frozen-image-contract'])
def test_profile_preserves_host_image_identity_and_parent_nonce(tmp_path, monkeypatch, frozen):
    # A frozen host must never tell Electron to launch an external base Python.
    parent = parent_identity()
    profile = tmp_path / 'profile Кириллица'
    executable = tmp_path / 'service' / 'ssearch-service.exe' if frozen else Path(sys.executable)
    base = tmp_path / 'external-python.exe' if frozen else Path(getattr(sys, '_base_executable', sys.executable))
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, '_base_executable', str(base), raising=False)
    sent = context(parent_pid=parent['pid'], parent_executable=parent['executable'])
    code, replies = invoke(monkeypatch, ['--profile-info', '--data-dir', str(profile)], frame(sent))
    assert code == 0 and len(replies) == 1
    reply = replies[0]
    assert reply['kind'] == 'profile' and reply['parent'] == parent
    assert reply['session_id'] == sent['session_id'] and reply['challenge'] == sent['challenge']
    assert reply['profile'] == str(profile.resolve())
    assert reply['python_base'] == str((executable if frozen else base).resolve())
    assert reply['python_venv'] == str(executable.absolute())
    assert len(reply['key']) == 64
    assert not profile.exists()  # Discovery neither creates an owner nor a database.


@pytest.mark.parametrize('content', [b'', b'\x00', struct.pack('!I', 70000),
    frame(context(token='private secret')), frame(dict(schema_version=1, session_id='private secret', challenge='x'))])
def test_invalid_handshake_closes_without_echo(tmp_path, monkeypatch, content):
    code, replies = invoke(monkeypatch, ['--desktop-client', '--data-dir', str(tmp_path / 'profile')], content)
    assert code == 1 and replies == []


def test_profile_rejects_foreign_parent_image(tmp_path, monkeypatch):
    parent = parent_identity()
    sent = context(parent_pid=parent['pid'], parent_executable=str(tmp_path / 'foreign.exe'))
    code, replies = invoke(monkeypatch, ['--profile-info', '--data-dir', str(tmp_path / 'profile')], frame(sent))
    assert code == 1 and replies == []


def test_broker_ready_proves_live_owner_and_preserves_p2_requests(owner_profile, monkeypatch):
    from atom_search.service.client import ServiceClient
    sent = context()
    request = dict(session_id=sent['session_id'], generation=owner_profile.generation,
                   envelope=envelope(dict(method='status', arguments={})))
    code, replies = invoke(monkeypatch, ['--desktop-client', '--data-dir', str(owner_profile.profile_dir)],
                           frame(sent) + frame(request))
    assert code == 0 and len(replies) == 2
    ready, response = replies
    assert ready['kind'] == 'ready' and ready['owner'] is True
    assert ready['generation'] == owner_profile.generation
    assert ready['process'] == parent_identity()  # Fixture owner and host share this real process.
    assert ready['broker'] == parent_identity()
    assert ready['session_id'] == sent['session_id'] and ready['challenge'] == sent['challenge']
    assert set(ready['capabilities']) == {'settings_validate', 'settings_apply', 'start_catalog', 'cancel_job'}
    assert response['envelope']['request_id'] == request['envelope']['request_id']
    assert response['envelope']['data']['api_version'] == '1'
    assert owner_profile.credentials.resolve(owner_profile.credential_ref) not in str(replies)
    assert ServiceClient(owner_profile.endpoint_file, None).status().api_version == '1'


def test_broker_rejects_mismatched_service_version(owner_profile, monkeypatch):
    import atom_search
    monkeypatch.setattr(atom_search, 'VERSION', '0.0.0')
    code, replies = invoke(monkeypatch, ['--desktop-client', '--data-dir', str(owner_profile.profile_dir)],
                           frame(context()))
    assert code == 1 and len(replies) == 1
    assert replies[0]['kind'] == 'unavailable' and replies[0]['code'] == 'service_unavailable'


def test_broker_read_credential_cannot_advertise_or_gain_owner(owner_profile, monkeypatch):
    from atom_search.service.contracts import AccessContext
    owner = owner_profile
    reference = owner.issue_credential(AccessContext('reader', frozenset({'status', 'settings_get'}), ()))
    endpoint = owner.endpoint_file
    state_path = endpoint.parent / 'service-state.json'
    original_endpoint, original_state = endpoint.read_bytes(), state_path.read_bytes()
    descriptor, state = decode(original_endpoint), decode(original_state)
    descriptor['credential_ref'] = state['credential_ref'] = reference
    sent = context()
    request = dict(session_id=sent['session_id'], generation=owner.generation,
        envelope=envelope(dict(method='settings_apply', arguments=dict(
            candidate={}, expected_revision=owner.repository.snapshot().revision))))
    try:
        # Preserve the real private files/ACLs; discovery resolves a real DPAPI
        # reader credential and authenticates against the live owner's allowlist.
        endpoint.write_bytes(encode(descriptor))
        state_path.write_bytes(encode(state))
        code, replies = invoke(monkeypatch, ['--desktop-client', '--data-dir', str(owner.profile_dir)],
                               frame(sent) + frame(request))
        assert code == 0 and len(replies) == 2
        assert replies[0]['owner'] is False and replies[0]['capabilities'] == []
        assert replies[1]['envelope']['error']['code'] == 'access_denied'
        assert owner.credentials.resolve(reference) not in str(replies)
    finally:
        endpoint.write_bytes(original_endpoint)
        state_path.write_bytes(original_state)


def test_unavailable_reply_is_bounded_and_redacted(tmp_path, monkeypatch):
    import threading
    from atom_search.service import desktop_client
    from atom_search.service.desktop_host import main
    release = threading.Event()
    bounded = desktop_client._bounded

    class StalledOutput:
        def write(self, content):
            release.wait(5)
            raise OSError('private output detail')

    monkeypatch.setattr(desktop_client, '_bounded', lambda operation, timeout: bounded(operation, .05))
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=BytesIO(frame(context()))))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=StalledOutput()))
    started = time.monotonic()
    try:
        assert main(['--desktop-client', '--data-dir', str(tmp_path / 'missing-profile')]) == 1
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_ready_write_timeout_does_not_start_a_second_frame(owner_profile, monkeypatch):
    import threading
    from atom_search.service import desktop_client
    from atom_search.service.desktop_host import main
    release = threading.Event()
    bounded = desktop_client._bounded
    writes = []

    class PartialOutput:
        def write(self, content):
            writes.append(bytes(content[:1]))
            if len(writes) == 1:
                return 1  # A real partial frame has reached the pipe.
            release.wait(5)
            raise OSError('private output detail')

    monkeypatch.setattr(desktop_client, '_bounded', lambda operation, timeout: bounded(operation, .2))
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=BytesIO(frame(context()))))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=PartialOutput()))
    try:
        assert main(['--desktop-client', '--data-dir', str(owner_profile.profile_dir)]) == 1
        assert len(writes) == 2  # Partial write + stalled continuation; no new frame.
    finally:
        release.set()


@pytest.mark.parametrize('change', [dict(created='1'), dict(pid=True), dict(created='0'),
                                    dict(executable='relative.exe'), dict(extra='private secret')])
def test_owner_rejects_invalid_or_reused_parent_before_composition(tmp_path, monkeypatch, change):
    parent = parent_identity()
    parent.update(change)
    profile = tmp_path / 'profile'
    code, replies = invoke(monkeypatch, ['--serve', '--parent-lease', '--data-dir', str(profile),
                           '--model-dir', str(tmp_path / 'missing-model')], frame(context(parent=parent)))
    assert code == 1
    assert not profile.exists()
    assert all(reply.get('code') == 'service_unavailable' for reply in replies)
    assert 'private secret' not in str(replies)


@pytest.mark.parametrize('arguments', [[], ['--parent-lease'], ['--desktop-client', '--parent-lease'],
    ['--profile-info', '--serve'], ['--serve'], ['--serve', '--parent-lease', '--data-dir', 'profile'],
    ['--serv', '--data-dir', 'profile'], ['--s', '--data-dir', 'profile']])
def test_invalid_role_combinations_fail_before_io(arguments):
    from atom_search.service.desktop_host import main
    with pytest.raises(SystemExit) as failure:
        main(arguments)
    assert failure.value.code == 2


def launch_arguments(*arguments):
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path('src').absolute()),
        str(Path('packages/atom-search-core/src').absolute()))), PYTHONNOUSERSITE='1')
    executable = sys.executable
    base = getattr(sys, '_base_executable', None) or executable
    if os.name == 'nt' and os.path.normcase(base) != os.path.normcase(executable):
        environment['__PYVENV_LAUNCHER__'] = executable
        executable = base
    return [executable, *arguments], environment


def finish(child):
    if child.stdin and not child.stdin.closed:
        child.stdin.close()
    try:
        child.wait(50)
    except subprocess.TimeoutExpired:
        child.kill()  # Only the test's direct child, never a discovered owner.
        child.wait(10)


def test_attached_host_control_eof_leaves_existing_owner_running(owner_profile, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from atom_search.service import owner as owner_module
    from atom_search.service.client import ServiceClient
    from atom_search.service.desktop_host import serve_with_parent_lease
    attached = threading.Event()
    original_open = owner_module.open_service_owner

    def observe_attach(*args, **kwargs):
        service = original_open(*args, **kwargs)  # Real P2 attach and retained runtime identity.
        assert service.attached
        attached.set()
        return service

    monkeypatch.setattr(owner_module, 'open_service_owner', observe_attach)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, 'rb', buffering=0) as incoming, os.fdopen(write_fd, 'wb', buffering=0) as control:
        monkeypatch.setattr(sys, 'stdin', incoming)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(serve_with_parent_lease, owner_profile.profile_dir,
                                 owner_profile.model_dir, parent_identity())
            try:
                assert attached.wait(30), 'host never attached to the existing owner'
            finally:
                control.close()
            assert future.result(timeout=50) == 0
    client = ServiceClient(owner_profile.endpoint_file, None)
    assert client.status().api_version == '1'
    assert client.descriptor['generation'] == owner_profile.generation
    assert client.descriptor['pid'] == os.getpid()


def test_parent_exit_during_dependency_startup_returns_bounded_failure(tmp_path, monkeypatch):
    import builtins
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from atom_search.service import headless
    from atom_search.service.desktop_host import serve_with_parent_lease
    entered, release, finished = (threading.Event() for _ in range(3))
    original_import, original_serve = builtins.__import__, headless.serve

    def delayed(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'owner' and level == 1 and (globals or {}).get('__package__') == 'atom_search.service':
            entered.set()
            try:
                release.wait(10)
                raise ImportError('controlled dependency startup failure')
            finally:
                finished.set()
        return original_import(name, globals, locals, fromlist, level)

    def short_deadlines(profile, *, model_dir, stop):
        return original_serve(profile, model_dir=model_dir, stop=stop, startup_timeout=5, shutdown_timeout=.1)

    monkeypatch.setattr(builtins, '__import__', delayed)
    monkeypatch.setattr(headless, 'serve', short_deadlines)
    arguments, environment = launch_arguments('-c', 'import sys; sys.stdin.buffer.read()')
    parent = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as incoming, os.fdopen(write_fd, 'wb', buffering=0) as control:
            monkeypatch.setattr(sys, 'stdin', incoming)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(serve_with_parent_lease, tmp_path / 'profile',
                                     tmp_path / 'missing-model', parent_identity(parent.pid))
                try:
                    assert entered.wait(5)
                    started = time.monotonic()
                    parent.stdin.close()
                    assert parent.wait(10) == 0
                    assert future.result(timeout=2) == 1
                    assert time.monotonic() - started < 2
                    assert not control.closed  # Stop came from the retained parent, not EOF.
                    assert not (tmp_path / 'profile').exists()
                finally:
                    release.set()
                    control.close()
        assert finished.wait(5)
    finally:
        release.set()
        finish(parent)


@pytest.mark.parametrize('stop_kind', ['control-eof', 'parent-exit', 'ordinary-headless'])
def test_real_host_owns_one_service_and_drains_on_lease_loss(tmp_path, stop_kind):
    from atom_search.service.client import ServiceClient
    from atom_search.service.contracts import ServiceError
    parent_args, environment = launch_arguments('-c', 'import sys; sys.stdin.buffer.read()')
    parent = subprocess.Popen(parent_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    child = None
    profile = tmp_path / 'desktop host Кириллица'
    endpoint = profile / 'engine' / 'runtime' / 'endpoint.json'
    try:
        arguments = ['-m', 'atom_search.service.desktop_host', '--serve', '--data-dir', str(profile),
                     '--model-dir', str(tmp_path / 'missing-model')]
        arguments += ['--control-stdin'] if stop_kind == 'ordinary-headless' else ['--parent-lease']
        args, environment = launch_arguments(*arguments)
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        if stop_kind != 'ordinary-headless':
            child.stdin.write(frame(context(parent=parent_identity(parent.pid))))
            child.stdin.flush()
        deadline = time.monotonic() + 120
        client = None
        while time.monotonic() < deadline and child.poll() is None:
            if endpoint.exists():
                try:
                    client = ServiceClient(endpoint, None)
                    client.status()
                    break
                except ServiceError:
                    client = None
            time.sleep(.05)
        assert client is not None and child.poll() is None, 'desktop host did not become ready'
        assert client.descriptor['pid'] == child.pid
        second = ServiceClient(endpoint, None)
        assert second.descriptor == client.descriptor
        assert second.settings_get().settings['sources'] == []
        assert client.start_catalog([]) == ()
        if stop_kind == 'parent-exit':
            parent.stdin.close()
            assert parent.wait(10) == 0
            assert not child.stdin.closed  # Retained parent exit, independent of control EOF.
        else:
            child.stdin.close()
        assert child.wait(50) == 0
        assert not endpoint.exists()
        assert decode((endpoint.parent / 'service-state.json').read_bytes())['state'] == 'stopped'
        assert child.stdout.read() == child.stderr.read() == b''
    finally:
        if child is not None:
            finish(child)
        finish(parent)


def test_cold_host_does_not_import_gui_or_owner():
    script = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('PySide6', 'PyQt6', 'PyQt5') or fullname in (
                'atom_search.service.owner', 'atom_search.search_runtime'):
            raise AssertionError('unexpected composition or GUI import')
sys.meta_path.insert(0, Guard())
import atom_search.service.desktop_host
'''
    arguments, environment = launch_arguments('-c', script)
    result = subprocess.run(arguments, capture_output=True, timeout=10, env=environment)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
